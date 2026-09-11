"""Spread-level position state.

The old bot reasoned about individual option legs. That is why it closed every
open position whenever any single leg hit a profit target, and why it tried to
sell-to-open a strike it was already long (the "position intent mismatch"
errors that filled the log). A credit spread is one position with two or four
legs, and it has to be tracked, managed and closed as one.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from . import config

log = logging.getLogger(__name__)


@dataclass
class PositionLeg:
    symbol: str
    kind: str
    strike: float
    side: str            # "sell" | "buy"  (as opened)
    ratio: int = 1


@dataclass
class Position:
    id: str
    structure: str
    expiry: str                      # ISO date
    qty: int
    entry_credit: float              # per share, net
    width: float
    max_loss_per_contract: float     # dollars
    legs: list = field(default_factory=list)

    opened_at: str = ""
    entry_spot: float = 0.0
    entry_iv: float = 0.0
    entry_pop: float = 0.0
    entry_ev: float = 0.0

    status: str = "open"             # open | closing | closed
    peak_profit_pct: float = 0.0
    last_mark: float = 0.0           # per-share debit to close
    close_reason: str = ""
    closed_at: str = ""
    realized_pnl: float = 0.0
    entry_order_id: str = ""
    exit_order_id: str = ""

    @property
    def expiry_date(self) -> date:
        return datetime.strptime(self.expiry, "%Y-%m-%d").date()

    @property
    def dte(self) -> int:
        return (self.expiry_date - date.today()).days

    @property
    def total_risk(self) -> float:
        return self.max_loss_per_contract * self.qty

    @property
    def max_profit(self) -> float:
        return self.entry_credit * 100.0 * self.qty

    def short_symbols(self) -> list:
        return [l["symbol"] if isinstance(l, dict) else l.symbol
                for l in self.legs
                if (l["side"] if isinstance(l, dict) else l.side) == "sell"]

    def all_symbols(self) -> list:
        return [l["symbol"] if isinstance(l, dict) else l.symbol for l in self.legs]

    def profit_pct(self, current_debit: float) -> float:
        """Fraction of the credit captured. 1.0 == the spread went worthless."""
        if self.entry_credit <= 0:
            return 0.0
        return (self.entry_credit - current_debit) / self.entry_credit

    def unrealized_pnl(self, current_debit: float) -> float:
        return (self.entry_credit - current_debit) * 100.0 * self.qty


class PositionStore:
    def __init__(self, path: str = config.STATE_PATH):
        self.path = path
        self.positions: list = []
        self.meta: dict = {}
        self.load()

    # ---- persistence ---------------------------------------------------- #
    def load(self) -> None:
        if not os.path.exists(self.path):
            self.positions, self.meta = [], {}
            return
        try:
            with open(self.path) as fh:
                blob = json.load(fh)
            self.positions = [Position(**p) for p in blob.get("positions", [])]
            self.meta = blob.get("meta", {})
        except Exception as exc:
            log.error("could not read %s (%s); starting with empty state", self.path, exc)
            self.positions, self.meta = [], {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"positions": [asdict(p) for p in self.positions],
                       "meta": self.meta}, fh, indent=2, default=str)
        os.replace(tmp, self.path)          # atomic: never leave a half-written file

    # ---- queries -------------------------------------------------------- #
    def open_positions(self) -> list:
        return [p for p in self.positions if p.status in ("open", "closing")]

    def total_open_risk(self) -> float:
        return sum(p.total_risk for p in self.open_positions())

    def held_symbols(self) -> set:
        """Every contract we already have on. Re-using one of these on a new
        spread is what produced the sell_to_close / sell_to_open conflicts."""
        out = set()
        for p in self.open_positions():
            out.update(p.all_symbols())
        return out

    def count_for_expiry(self, expiry: date) -> int:
        return sum(1 for p in self.open_positions() if p.expiry_date == expiry)

    def opened_today(self) -> int:
        today = date.today().isoformat()
        return sum(1 for p in self.positions if p.opened_at[:10] == today)

    def closed_today(self) -> list:
        today = date.today().isoformat()
        return [p for p in self.positions
                if p.status == "closed" and p.closed_at[:10] == today]

    def realized_pnl_today(self) -> float:
        return sum(p.realized_pnl for p in self.closed_today())

    def consecutive_losses(self) -> int:
        closed = sorted([p for p in self.positions if p.status == "closed"],
                        key=lambda p: p.closed_at, reverse=True)
        n = 0
        for p in closed:
            if p.realized_pnl < 0:
                n += 1
            else:
                break
        return n

    # ---- mutation ------------------------------------------------------- #
    def add(self, position: Position) -> None:
        self.positions.append(position)
        self.save()

    def close(self, position: Position, reason: str, realized_pnl: float) -> None:
        position.status = "closed"
        position.close_reason = reason
        position.closed_at = datetime.now(timezone.utc).isoformat()
        position.realized_pnl = round(realized_pnl, 2)
        self.save()
        self._append_trade_log(position)

    def _append_trade_log(self, p: Position) -> None:
        os.makedirs(os.path.dirname(config.TRADE_LOG_PATH) or ".", exist_ok=True)
        new = not os.path.exists(config.TRADE_LOG_PATH)
        with open(config.TRADE_LOG_PATH, "a", newline="") as fh:
            if new:
                fh.write("closed_at,id,structure,expiry,qty,entry_credit,width,"
                         "entry_spot,entry_pop,entry_ev,realized_pnl,max_risk,reason\n")
            fh.write(f"{p.closed_at},{p.id},{p.structure},{p.expiry},{p.qty},"
                     f"{p.entry_credit},{p.width},{p.entry_spot},{p.entry_pop:.4f},"
                     f"{p.entry_ev},{p.realized_pnl},{p.total_risk},{p.close_reason}\n")

    def prune_expired(self) -> int:
        """Anything past expiry that the broker has settled is no longer ours."""
        n = 0
        for p in self.open_positions():
            if p.dte < 0:
                p.status = "closed"
                p.close_reason = p.close_reason or "expired"
                p.closed_at = datetime.now(timezone.utc).isoformat()
                n += 1
        if n:
            self.save()
        return n

    # ---- broker reconciliation ------------------------------------------ #
    def reconcile(self, broker_symbols: set) -> list:
        """Flag positions whose legs the broker no longer reports (assigned,
        expired, or closed by hand). Silent divergence between our state and
        the broker's is the most dangerous failure mode a bot has."""
        drifted = []
        for p in self.open_positions():
            missing = [s for s in p.all_symbols() if s not in broker_symbols]
            if missing and len(missing) == len(p.all_symbols()):
                p.status = "closed"
                p.close_reason = p.close_reason or "gone_at_broker"
                p.closed_at = datetime.now(timezone.utc).isoformat()
                drifted.append((p, "all legs gone"))
            elif missing:
                drifted.append((p, f"partial: {missing} missing at broker"))
        if drifted:
            self.save()
        return drifted


def new_position(candidate, qty: int, spot: float, iv: float, order_id: str = "") -> Position:
    return Position(
        id=uuid.uuid4().hex[:12],
        structure=candidate.structure,
        expiry=str(candidate.expiry),
        qty=qty,
        entry_credit=candidate.credit,
        width=candidate.width,
        max_loss_per_contract=candidate.max_loss,
        legs=[asdict(PositionLeg(l.symbol, l.kind, l.strike, l.side)) for l in candidate.legs],
        opened_at=datetime.now(timezone.utc).isoformat(),
        entry_spot=spot,
        entry_iv=iv or 0.0,
        entry_pop=candidate.pop,
        entry_ev=candidate.ev_per_contract,
        entry_order_id=order_id)
