#!/usr/bin/env python
"""IBIT (spot-Bitcoin ETF) options bot -- orchestrator.

    python bot.py --status          account, risk budget and open spreads
    python bot.py --scan            find and rank candidates, place nothing
    python bot.py --once            one full manage-then-maybe-enter cycle
    python bot.py --loop            run continuously during market hours
    python bot.py --close-all       flatten every tracked spread
    python bot.py --doctor          check config, credentials and data access

Add --dry-run to any trading mode to log intended orders without sending them.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from btcbot import config, strategy
from btcbot.execution import Executor
from btcbot.marketdata import MarketData, parse_occ
from btcbot.portfolio import PositionStore, new_position
from btcbot.risk import AccountState, RiskGate, size_position

log = logging.getLogger("bot")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler("bot.log"), logging.StreamHandler(sys.stdout)])
    logging.getLogger("alpaca").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


class Bot:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.executor = Executor()
        self.md = MarketData(self.executor.trading)
        self.store = PositionStore()
        self.gate = RiskGate(self.store)

    # ---- account ---------------------------------------------------------- #
    def account_state(self) -> AccountState:
        a = self.executor.account_snapshot()
        return AccountState(
            equity=float(a.equity),
            cash=float(a.cash),
            options_buying_power=float(getattr(a, "options_buying_power", 0) or 0),
            high_water_mark=self.store.meta.get("high_water_mark", 0.0))

    # ---- reporting --------------------------------------------------------- #
    def status(self) -> None:
        acct = self.account_state()
        clock = self.executor.trading.get_clock()
        self.store.prune_expired()
        drift = self.store.reconcile(set(self.executor.net_positions()))

        log.info("=" * 72)
        log.info("  IBIT OPTIONS BOT  --  %s", "PAPER" if config.ALPACA_PAPER else "LIVE")
        log.info("=" * 72)
        log.info("  market open        : %s (next close %s)", clock.is_open, clock.next_close)
        log.info("  equity             : $%s", f"{acct.equity:,.2f}")
        log.info("  cash               : $%s", f"{acct.cash:,.2f}")
        log.info("  options buying pwr : $%s", f"{acct.options_buying_power:,.2f}")

        open_risk = self.store.total_open_risk()
        budget = acct.equity * config.MAX_PORTFOLIO_RISK_PCT
        log.info("  risk deployed      : $%s of $%s budget (%.0f%%)",
                 f"{open_risk:,.0f}", f"{budget:,.0f}",
                 100 * open_risk / budget if budget else 0)

        halted, why = self.gate.halted(acct)
        log.info("  trading            : %s", f"HALTED -- {why}" if halted else "enabled")
        log.info("  realized P&L today : $%s", f"{self.store.realized_pnl_today():,.2f}")

        try:
            snap = self.md.chain()
            v = snap.vol
            log.info("-" * 72)
            log.info("  spot %s   regime %s   RSI %.0f", f"${snap.spot:,.2f}", v.regime, v.rsi14)
            log.info("  ATM IV %s   forecast RV %s   IV/RV %s   IV rank %s",
                     f"{v.atm_iv:.1%}" if v.atm_iv else "n/a",
                     f"{v.rv_forecast:.1%}",
                     f"{v.vrp_ratio:.2f}" if v.vrp_ratio else "n/a",
                     f"{v.iv_rank:.0f}" if v.iv_rank is not None else "building")
            has_edge, reasons = strategy.edge_check(v)
            log.info("  edge present       : %s", "YES" if has_edge else f"no -- {'; '.join(reasons)}")
        except Exception as exc:
            log.warning("  market snapshot unavailable: %s", exc)

        positions = self.store.open_positions()
        log.info("-" * 72)
        log.info("  open spreads: %d (max %d)", len(positions), config.MAX_OPEN_SPREADS)
        if positions:
            log.info("  %-13s %-14s %-11s %5s %8s %9s %8s",
                     "id", "structure", "expiry", "dte", "qty", "credit", "risk")
            for p in positions:
                log.info("  %-13s %-14s %-11s %5d %8d %9.2f %8.0f",
                         p.id, p.structure, p.expiry, p.dte, p.qty, p.entry_credit, p.total_risk)
        for pos, note in drift:
            log.warning("  STATE DRIFT %s: %s", pos.id, note)
        log.info("=" * 72)

    # ---- scanning ----------------------------------------------------------- #
    def scan(self, show: int = 8, ignore_edge: bool = False):
        snap = self.md.chain()
        v = snap.vol
        log.info("spot $%.2f | regime %s | ATM IV %s | RV %.1f%% | IV/RV %s",
                 snap.spot, v.regime,
                 f"{v.atm_iv:.1%}" if v.atm_iv else "n/a",
                 v.rv_forecast * 100,
                 f"{v.vrp_ratio:.2f}" if v.vrp_ratio else "n/a")

        held = self.store.held_symbols() | set(self.executor.net_positions())
        cands, reasons = strategy.find_candidates(snap, held, ignore_edge=ignore_edge)
        if not cands:
            log.info("no candidates: %s", "; ".join(reasons))
            return []

        acct = self.account_state()
        log.info("-" * 96)
        log.info("%-13s %-11s %4s %-22s %7s %6s %6s %8s %7s %5s",
                 "structure", "expiry", "dte", "legs", "credit", "w", "cr/w", "EV/ct", "POP", "qty")
        log.info("-" * 96)
        for c in cands[:show]:
            legs = " ".join(f"{'S' if l.side == 'sell' else 'B'}{l.strike:g}{l.kind[0].upper()}"
                            for l in c.legs)
            sizing = size_position(c, acct, self.store)
            log.info("%-13s %-11s %4d %-22s %7.2f %6g %6.0f%% %8.2f %6.1f%% %5d",
                     c.structure, c.expiry, c.dte, legs[:22], c.credit, c.width,
                     c.credit_ratio * 100, c.ev_per_contract, c.pop * 100, sizing.qty)
        log.info("-" * 96)
        return cands

    # ---- position management -------------------------------------------------- #
    def manage(self) -> None:
        self.store.prune_expired()
        broker = self.executor.net_positions()
        for pos, note in self.store.reconcile(set(broker)):
            log.warning("state drift on %s: %s", pos.id, note)

        positions = self.store.open_positions()
        if not positions:
            return

        try:
            snap = self.md.chain(0, 60)
            quotes = {q.symbol: q for qs in snap.by_expiry.values() for q in qs}
        except Exception as exc:
            log.error("cannot mark positions (%s); skipping management", exc)
            return

        for pos in positions:
            debit, short_delta, missing = 0.0, None, False
            for raw in pos.legs:
                leg = raw if isinstance(raw, dict) else raw.__dict__
                q = quotes.get(leg["symbol"])
                if q is None:
                    missing = True
                    break
                # closing costs: pay the ask on shorts, hit the bid on longs
                debit += q.ask if leg["side"] == "sell" else -q.bid
                if leg["side"] == "sell" and q.delta is not None:
                    short_delta = max(short_delta or 0.0, abs(q.delta))

            if missing:
                log.warning("%s: missing quotes for a leg; not managing this cycle", pos.id)
                continue

            pos.last_mark = round(debit, 3)
            pnl = pos.unrealized_pnl(debit)
            log.info("  %s %s %s dte=%d debit=$%.2f (entry $%.2f) P&L=$%.0f (%.0f%% of credit)",
                     pos.id, pos.structure, pos.expiry, pos.dte, debit,
                     pos.entry_credit, pnl, pos.profit_pct(debit) * 100)

            should_exit, reason = self.gate.exit_decision(pos, debit, short_delta)
            if should_exit:
                log.info("  EXIT %s -- %s", pos.id, reason)
                if self.dry_run:
                    continue
                # pay up a little to actually get out; a stop you cannot fill is not a stop
                order = self.executor.close_spread(pos, debit + config.ENTRY_PRICE_PAD)
                if order is None and reason.startswith(("stop", "short strike")):
                    log.warning("  limit close failed on a risk exit; going to market")
                    self.executor.force_close(pos)
                    order = True
                if order:
                    self.store.close(pos, reason, pnl)
                    if pnl < 0 and self.store.consecutive_losses() >= config.CONSECUTIVE_LOSS_HALT:
                        self.gate.engage_halt(f"{config.CONSECUTIVE_LOSS_HALT} consecutive losses")
        self.store.save()

    # ---- entry ----------------------------------------------------------------- #
    def maybe_enter(self) -> None:
        acct = self.account_state()
        halted, why = self.gate.halted(acct)
        if halted:
            log.info("not entering: %s", why)
            return
        if len(self.store.open_positions()) >= config.MAX_OPEN_SPREADS:
            log.info("not entering: at MAX_OPEN_SPREADS (%d)", config.MAX_OPEN_SPREADS)
            return
        if self.store.opened_today() >= config.MAX_NEW_TRADES_PER_DAY:
            log.info("not entering: daily trade cap reached")
            return
        if not self._entry_window_open():
            return

        snap = self.md.chain()
        held = self.store.held_symbols() | set(self.executor.net_positions())
        cands, reasons = strategy.find_candidates(snap, held)
        if not cands:
            log.info("no qualifying trade: %s", "; ".join(reasons))
            return

        for cand in cands[:5]:
            ok, why = self.gate.can_open(cand, acct)
            if not ok:
                log.info("skip %s: %s", cand.describe(), why)
                continue
            sizing = size_position(cand, acct, self.store)
            if sizing.qty < 1:
                log.info("skip %s: size 0 (%s binding: %s)",
                         cand.describe(), sizing.binding_constraint, sizing.detail)
                continue

            log.info("selected %s", cand.describe())
            log.info("  sizing: qty=%d risk=$%.0f (%.1f%% of equity), binding=%s",
                     sizing.qty, sizing.total_risk,
                     100 * sizing.total_risk / acct.equity, sizing.binding_constraint)

            order = self.executor.open_spread(cand, sizing.qty, dry_run=self.dry_run)
            if order:
                pos = new_position(cand, sizing.qty, snap.spot, snap.vol.atm_iv, str(order.id))
                self.store.add(pos)
                log.info("  opened position %s", pos.id)
            return
        log.info("no candidate passed the risk gate this cycle")

    def _entry_window_open(self) -> bool:
        clock = self.executor.trading.get_clock()
        if not clock.is_open:
            return False
        now = clock.timestamp
        since_open = (now - (clock.next_close - timedelta(hours=6, minutes=30))).total_seconds() / 60
        until_close = (clock.next_close - now).total_seconds() / 60
        if 0 <= since_open < config.NO_ENTRY_FIRST_MINUTES:
            log.info("not entering: within %d min of the open (untrustworthy quotes)",
                     config.NO_ENTRY_FIRST_MINUTES)
            return False
        if until_close < config.NO_ENTRY_LAST_MINUTES:
            log.info("not entering: within %d min of the close", config.NO_ENTRY_LAST_MINUTES)
            return False
        return True

    # ---- cycle ------------------------------------------------------------------ #
    def cycle(self) -> None:
        clock = self.executor.trading.get_clock()
        if not clock.is_open:
            log.info("market closed; next open %s", clock.next_open)
            return
        self.executor.cancel_stale_orders()
        self.manage()
        self.maybe_enter()

    def loop(self) -> None:
        log.info("daemon start | %s | risk/trade %.1f%% | max portfolio risk %.1f%% | "
                 "max spreads %d",
                 "PAPER" if config.ALPACA_PAPER else "LIVE",
                 config.RISK_PER_TRADE_PCT * 100, config.MAX_PORTFOLIO_RISK_PCT * 100,
                 config.MAX_OPEN_SPREADS)
        failures = 0
        while True:
            try:
                self.cycle()
                failures = 0
            except KeyboardInterrupt:
                log.info("interrupted; exiting cleanly")
                return
            except Exception as exc:
                failures += 1
                log.exception("cycle failed (%d consecutive): %s", failures, exc)
                if failures >= 10:
                    log.error("10 consecutive failures -- stopping rather than flailing")
                    return
            # back off when the market is shut instead of spinning every minute
            try:
                sleep_s = config.LOOP_INTERVAL_SECONDS
                if not self.executor.trading.get_clock().is_open:
                    sleep_s = 300
            except Exception:
                sleep_s = config.LOOP_INTERVAL_SECONDS
            time.sleep(sleep_s)

    def close_all(self) -> None:
        for pos in self.store.open_positions():
            log.info("closing %s", pos.id)
            if self.dry_run:
                continue
            self.executor.force_close(pos)
            self.store.close(pos, "manual_close_all", 0.0)


def doctor() -> int:
    print("=" * 60)
    print("  PREFLIGHT")
    print("=" * 60)
    problems = config.validate()
    for p in problems:
        print(f"  [FAIL] {p}")
    if not problems:
        print("  [ok]   configuration is self-consistent")

    print(f"  [ok]   mode: {'PAPER' if config.ALPACA_PAPER else 'LIVE'}")
    be = (1 - config.MIN_CREDIT_RATIO) * 100
    print(f"  [ok]   min credit/width {config.MIN_CREDIT_RATIO:.0%}"
          f" -> break-even win rate {be:.0f}%")
    print(f"  [ok]   worst case if every slot fills and every one loses: "
          f"{config.MAX_PORTFOLIO_RISK_PCT:.0%} of equity")

    try:
        ex = Executor()
        a = ex.account_snapshot()
        print(f"  [ok]   broker reachable: equity ${float(a.equity):,.2f}, "
              f"options BP ${float(getattr(a, 'options_buying_power', 0) or 0):,.2f}")
        lvl = getattr(a, "options_trading_level", None)
        if lvl is not None and int(lvl) < 3:
            print(f"  [FAIL] options level {lvl}; spreads need level 3")
            problems.append("options level")
    except Exception as exc:
        print(f"  [FAIL] broker unreachable: {exc}")
        problems.append("broker")

    try:
        md = MarketData()
        snap = md.chain()
        n = sum(len(v) for v in snap.by_expiry.values())
        print(f"  [ok]   chain: {n} quotes across {len(snap.by_expiry)} expiries, "
              f"spot ${snap.spot:,.2f}")
        v = snap.vol
        print(f"  [ok]   ATM IV {v.atm_iv:.1%} vs forecast RV {v.rv_forecast:.1%} "
              f"(IV/RV {v.vrp_ratio:.2f})" if v.atm_iv else "  [warn] no ATM IV")
    except Exception as exc:
        print(f"  [FAIL] market data: {exc}")
        problems.append("market data")

    print("=" * 60)
    print("  READY" if not problems else f"  {len(problems)} PROBLEM(S) -- fix before trading")
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IBIT options bot")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--close-all", action="store_true")
    ap.add_argument("--doctor", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="log orders, send none")
    ap.add_argument("--show-all", action="store_true",
                    help="with --scan, list candidates even when the edge filter says no")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)

    if args.doctor:
        return doctor()

    problems = config.validate()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        return 1

    bot = Bot(dry_run=args.dry_run)
    if args.status:
        bot.status()
    elif args.scan:
        bot.scan(ignore_edge=args.show_all)
    elif args.loop:
        bot.loop()
    elif args.close_all:
        bot.close_all()
    else:
        bot.status()
        bot.cycle()
    return 0


if __name__ == "__main__":
    sys.exit(main())
