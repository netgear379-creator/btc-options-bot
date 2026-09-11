"""Position sizing, portfolio limits and circuit breakers.

The old bot sized every trade at "20% of available cash" and allowed five of
them at once -- 100% of the account at risk. It did in fact open a 400-contract
spread on a $100k account, roughly $60k of max loss in a single position, to
chase $2,000 of credit. Sizing here starts from the loss we are willing to
take, and is then cut by four independent caps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config
from .portfolio import PositionStore

log = logging.getLogger(__name__)


@dataclass
class AccountState:
    equity: float
    cash: float
    options_buying_power: float
    high_water_mark: float = 0.0


@dataclass
class SizingResult:
    qty: int
    total_risk: float
    binding_constraint: str
    detail: dict


def stop_level(entry_credit: float, width: float) -> float:
    """Debit at which we cut the spread.

    Two conditions, whichever is tighter: a multiple of the credit collected,
    and a fraction of the spread width. The second exists because the first
    alone becomes inert on high-credit spreads -- and an inert stop is worse
    than no stop, because it looks like risk control in the config file.
    """
    by_credit = entry_credit * config.STOP_LOSS_MULT
    by_width = width * config.STOP_MAX_DEBIT_FRAC
    return min(by_credit, by_width)


def kelly_fraction(pop: float, win: float, loss: float) -> float:
    """Kelly on a two-outcome bet: f* = p/loss - q/win (in units of bankroll).

    Used only as a *ceiling*. Full Kelly on a strategy with fat left tails and
    an estimated edge is a route to ruin; config.KELLY_FRACTION scales it down.
    """
    if win <= 0 or loss <= 0:
        return 0.0
    b = win / loss                      # payoff odds
    q = 1.0 - pop
    f = (b * pop - q) / b
    return max(0.0, f)


def size_position(candidate, account: AccountState, store: PositionStore) -> SizingResult:
    """Contracts to trade, or 0 with the reason recorded."""
    per_contract_risk = candidate.max_loss
    if per_contract_risk <= 0:
        return SizingResult(0, 0.0, "degenerate risk", {})

    caps = {}

    # 1. risk budget for a single trade
    caps["risk_per_trade"] = int((account.equity * config.RISK_PER_TRADE_PCT)
                                 // per_contract_risk)

    # 2. remaining portfolio risk budget
    budget_left = account.equity * config.MAX_PORTFOLIO_RISK_PCT - store.total_open_risk()
    caps["portfolio_risk"] = int(max(0.0, budget_left) // per_contract_risk)

    # 3. buying power actually available for options
    margin_per_contract = candidate.width * 100.0
    usable_bp = account.options_buying_power * config.MAX_BP_UTILISATION
    caps["buying_power"] = int(usable_bp // margin_per_contract) if margin_per_contract > 0 else 0

    # 4. fractional-Kelly ceiling
    f_star = kelly_fraction(candidate.pop, candidate.max_profit, candidate.max_loss)
    kelly_dollars = account.equity * f_star * config.KELLY_FRACTION
    caps["kelly"] = int(kelly_dollars // per_contract_risk)

    # 5. hard ceiling
    caps["hard_cap"] = config.MAX_CONTRACTS_PER_TRADE

    binding = min(caps, key=caps.get)
    qty = max(0, min(caps.values()))
    return SizingResult(qty, round(qty * per_contract_risk, 2), binding,
                        {**caps, "kelly_f_star": round(f_star, 4),
                         "per_contract_risk": per_contract_risk})


class RiskGate:
    """Every reason the bot is allowed to say 'no' before it risks money."""

    def __init__(self, store: PositionStore):
        self.store = store

    # ---- circuit breakers ------------------------------------------------ #
    def halted(self, account: AccountState) -> tuple:
        meta = self.store.meta

        until = meta.get("halt_until")
        if until:
            if datetime.now(timezone.utc) < datetime.fromisoformat(until):
                return True, f"halted until {until} ({meta.get('halt_reason', 'unspecified')})"
            meta.pop("halt_until", None)
            meta.pop("halt_reason", None)
            self.store.save()

        daily = self.store.realized_pnl_today()
        limit = -account.equity * config.DAILY_LOSS_LIMIT_PCT
        if daily < limit:
            return True, (f"daily realized P&L ${daily:,.2f} breached the "
                          f"{config.DAILY_LOSS_LIMIT_PCT:.0%} limit (${limit:,.2f})")

        losses = self.store.consecutive_losses()
        if losses >= config.CONSECUTIVE_LOSS_HALT:
            return True, f"{losses} consecutive losing trades"

        hwm = max(meta.get("high_water_mark", 0.0), account.equity)
        if hwm != meta.get("high_water_mark"):
            meta["high_water_mark"] = hwm
            self.store.save()
        if hwm > 0:
            dd = (hwm - account.equity) / hwm
            if dd > config.MAX_DRAWDOWN_HALT_PCT:
                return True, (f"drawdown {dd:.1%} from high-water mark "
                              f"${hwm:,.2f} exceeds {config.MAX_DRAWDOWN_HALT_PCT:.0%}")
        return False, ""

    def engage_halt(self, reason: str) -> None:
        until = datetime.now(timezone.utc) + timedelta(hours=config.HALT_COOLDOWN_HOURS)
        self.store.meta["halt_until"] = until.isoformat()
        self.store.meta["halt_reason"] = reason
        self.store.save()
        log.error("TRADING HALTED until %s -- %s", until.isoformat(timespec="minutes"), reason)

    # ---- per-entry gates -------------------------------------------------- #
    def can_open(self, candidate, account: AccountState) -> tuple:
        halted, why = self.halted(account)
        if halted:
            return False, why

        open_positions = self.store.open_positions()
        if len(open_positions) >= config.MAX_OPEN_SPREADS:
            return False, f"already at MAX_OPEN_SPREADS ({config.MAX_OPEN_SPREADS})"
        if self.store.opened_today() >= config.MAX_NEW_TRADES_PER_DAY:
            return False, f"already opened {config.MAX_NEW_TRADES_PER_DAY} trades today"
        if self.store.count_for_expiry(candidate.expiry) >= config.MAX_PER_EXPIRY:
            return False, f"already at MAX_PER_EXPIRY for {candidate.expiry}"

        overlap = set(candidate.all_symbols) & self.store.held_symbols()
        if overlap:
            return False, f"contract already held, would confuse position intent: {sorted(overlap)}"

        projected = self.store.total_open_risk() + candidate.max_loss
        if projected > account.equity * config.MAX_PORTFOLIO_RISK_PCT:
            return False, (f"would push portfolio risk to ${projected:,.0f} "
                           f"(> {config.MAX_PORTFOLIO_RISK_PCT:.0%} of ${account.equity:,.0f})")
        return True, ""

    # ---- exits ------------------------------------------------------------ #
    def exit_decision(self, position, current_debit: float, short_delta=None) -> tuple:
        """Returns (should_exit, reason). Evaluated per spread, not per leg."""
        profit_pct = position.profit_pct(current_debit)

        if profit_pct >= config.PROFIT_TARGET_PCT:
            return True, f"profit target ({profit_pct:.0%} of credit)"

        stop_debit = stop_level(position.entry_credit, position.width)
        if current_debit >= stop_debit:
            return True, (f"stop loss (debit ${current_debit:.2f} >= "
                          f"${stop_debit:.2f})")

        if position.dte <= config.CLOSE_AT_DTE:
            return True, f"time exit ({position.dte} DTE <= {config.CLOSE_AT_DTE})"

        if short_delta is not None and abs(short_delta) >= config.SHORT_DELTA_EXIT:
            return True, f"short strike under threat (delta {abs(short_delta):.2f})"

        # Ratchet: once a decent chunk of the credit is banked, protect it.
        if profit_pct > position.peak_profit_pct:
            position.peak_profit_pct = profit_pct
        if (position.peak_profit_pct >= config.TRAIL_AFTER_PCT
                and profit_pct <= position.peak_profit_pct - config.TRAIL_GIVEBACK_PCT):
            return True, (f"trailing exit (peaked at {position.peak_profit_pct:.0%}, "
                          f"now {profit_pct:.0%})")

        return False, ""
