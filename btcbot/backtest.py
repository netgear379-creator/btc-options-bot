"""Historical backtest on real traded IBIT option prices.

This is the part the old bot had no equivalent of. It claimed "~75% win
probability" from a formula that was invented rather than derived, and nothing
ever checked that claim against history.

What this does
--------------
* Pulls real daily OHLCV bars for expired IBIT option contracts from Alpaca.
* Replays the calendar day by day, using only data available on that day.
* Feeds a reconstructed chain through the *same* strategy/risk code the live
  bot uses, so the backtest tests the shipped logic rather than a copy of it.
* Models costs explicitly: modelled bid/ask, slippage vs mid, per-contract fees.

Known limitations, stated plainly
---------------------------------
* Daily bars only. Intraday stops are evaluated at the next daily close, so a
  stop that would have triggered midday is filled worse or better than modelled.
* Bid/ask is modelled from a calibration of the live chain, not the historical
  NBBO (Alpaca does not serve historical option quotes on this plan).
* Fills are assumed at the limit price when the modelled market touches it.
* IBIT options only began trading in late November 2024, so the sample is
  short and covers one broad regime. Treat the output as evidence, not proof.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from . import config, strategy
from .greeks import bs_greeks, bs_price, implied_vol
from .marketdata import ChainSnapshot, OptionQuote, build_occ, compute_vol_stats
from .risk import stop_level

log = logging.getLogger("backtest")

CACHE_DIR = os.path.join(config.DATA_DIR, "cache")
STRIKE_STEP = 0.5
OPTIONS_START = date(2024, 11, 25)      # IBIT listed options late Nov 2024


# --------------------------------------------------------------------------- #
# modelled microstructure
# --------------------------------------------------------------------------- #
def modelled_spread(price: float) -> float:
    """Bid/ask width, calibrated against the live IBIT chain.

    Measured medians: ~$0.02 for $0.25-$1.00 options, $0.03 for $1-2,
    $0.08 for $2-5, $0.15 above $5. 2.5% of price with a $0.02 floor and a
    $0.25 cap reproduces that and is slightly wide (i.e. pessimistic).
    """
    return max(0.02, min(0.25, 0.025 * price))


def synth_quote(symbol: str, kind: str, strike: float, expiry: date, dte: int,
                price: float, spot: float, volume: float) -> OptionQuote:
    half = modelled_spread(price) / 2.0
    bid, ask = max(0.01, price - half), price + half
    mid = (bid + ask) / 2.0
    T = max(dte, 0.5) / 365.0
    iv = implied_vol(mid, spot, strike, T, config.RISK_FREE_RATE, kind)
    delta = gamma = theta = vega = None
    if iv:
        g = bs_greeks(spot, strike, T, config.RISK_FREE_RATE, iv, kind)
        delta, gamma, theta, vega = g.delta, g.gamma, g.theta, g.vega
    return OptionQuote(
        symbol=symbol, kind=kind, strike=strike, expiry=expiry, dte=dte,
        bid=round(bid, 2), ask=round(ask, 2), mid=round(mid, 4),
        spread_abs=round(ask - bid, 4),
        spread_pct=((ask - bid) / mid) if mid > 0 else 9.99,
        bid_size=50.0, ask_size=50.0,       # size is proxied by the volume filter
        iv=iv, delta=delta, gamma=gamma, theta=theta, vega=vega,
        open_interest=None)


# --------------------------------------------------------------------------- #
# data acquisition
# --------------------------------------------------------------------------- #
class HistoricalChainLoader:
    def __init__(self, start: date, end: date, refresh: bool = False):
        self.start, self.end, self.refresh = start, end, refresh
        self.stock = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        self.option = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        os.makedirs(CACHE_DIR, exist_ok=True)

    # ---- underlying ----------------------------------------------------- #
    def underlying(self) -> pd.DataFrame:
        path = os.path.join(CACHE_DIR, f"{config.UNDERLYING}_daily.csv")
        if os.path.exists(path) and not self.refresh:
            df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
            if df.index.max().date() >= self.end - timedelta(days=5):
                return df
        req = StockBarsRequest(
            symbol_or_symbols=config.UNDERLYING, timeframe=TimeFrame.Day,
            start=datetime.combine(self.start - timedelta(days=200), datetime.min.time()),
            end=datetime.combine(self.end, datetime.min.time()))
        df = self.stock.get_stock_bars(req).df
        if isinstance(df.index, pd.MultiIndex):
            df = df.droplevel("symbol")
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        df.index.name = "date"
        df = df[["open", "high", "low", "close", "volume"]]
        df.to_csv(path)
        return df

    # ---- expiry calendar -------------------------------------------------- #
    @staticmethod
    def candidate_expiries(start: date, end: date) -> list:
        """IBIT lists weekly Friday expiries plus monthlies. Probing a Friday
        that does not exist simply returns no bars, so over-generating is safe."""
        out, day = [], start
        while day <= end:
            if day.weekday() == 4:            # Friday
                out.append(day)
            day += timedelta(days=1)
        return out

    # ---- option bars ------------------------------------------------------ #
    def option_bars(self, underlying: pd.DataFrame) -> pd.DataFrame:
        path = os.path.join(CACHE_DIR,
                            f"opt_{self.start:%Y%m%d}_{self.end:%Y%m%d}.parquet")
        csv_path = path.replace(".parquet", ".csv")
        for p in (path, csv_path):
            if os.path.exists(p) and not self.refresh:
                log.info("using cached option bars: %s", p)
                df = pd.read_parquet(p) if p.endswith(".parquet") else pd.read_csv(p, parse_dates=["date"])
                return df

        expiries = self.candidate_expiries(self.start, self.end + timedelta(days=config.MAX_DTE))
        log.info("fetching option bars for %d candidate expiries "
                 "(this runs once, then it is cached)", len(expiries))

        frames, done = [], 0
        for expiry in expiries:
            window_start = expiry - timedelta(days=config.MAX_DTE + 10)
            window = underlying[(underlying.index >= pd.Timestamp(window_start))
                                & (underlying.index <= pd.Timestamp(expiry))]
            if window.empty:
                continue
            lo_spot, hi_spot = float(window["low"].min()), float(window["high"].max())
            lo_k = math.floor(lo_spot * 0.70 / STRIKE_STEP) * STRIKE_STEP
            hi_k = math.ceil(hi_spot * 1.30 / STRIKE_STEP) * STRIKE_STEP

            symbols = []
            k = lo_k
            while k <= hi_k:
                for kind in ("put", "call"):
                    symbols.append(build_occ(config.UNDERLYING, expiry, kind, k))
                k = round(k + STRIKE_STEP, 2)

            for chunk in _chunks(symbols, 250):
                try:
                    res = self.option.get_option_bars(OptionBarsRequest(
                        symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                        start=datetime.combine(window_start, datetime.min.time()),
                        end=datetime.combine(expiry + timedelta(days=1), datetime.min.time())))
                    df = res.df
                except Exception as exc:
                    log.warning("bar fetch failed for %s (%s)", expiry, exc)
                    continue
                if df is None or df.empty:
                    continue
                df = df.reset_index()
                frames.append(df[["symbol", "timestamp", "close", "volume", "trade_count"]])
                time.sleep(0.05)              # stay polite with the API

            done += 1
            if done % 10 == 0:
                log.info("  ... %d/%d expiries", done, len(expiries))

        if not frames:
            raise RuntimeError("no historical option bars returned")

        out = pd.concat(frames, ignore_index=True)
        out["date"] = pd.to_datetime(out["timestamp"]).dt.tz_localize(None).dt.normalize()
        out = out.drop(columns=["timestamp"]).drop_duplicates(["symbol", "date"])
        try:
            out.to_parquet(path, index=False)
        except Exception:
            out.to_csv(csv_path, index=False)
        log.info("cached %d option bar rows", len(out))
        return out


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------------- #
# simulation
# --------------------------------------------------------------------------- #
class SimPosition:
    def __init__(self, cand, qty, entry_day, spot, iv):
        self.cand, self.qty = cand, qty
        self.entry_day, self.entry_spot, self.entry_iv = entry_day, spot, iv
        self.entry_credit = cand.credit
        self.peak_profit_pct = 0.0
        self.exit_day = None
        self.exit_debit = None
        self.reason = ""
        self.pnl = 0.0

    @property
    def expiry(self):
        return self.cand.expiry

    @property
    def risk(self):
        return self.cand.max_loss * self.qty


class Backtester:
    def __init__(self, start: date, end: date, starting_equity: float = 100_000.0,
                 refresh: bool = False, pessimistic: bool = False):
        self.start, self.end = start, end
        self.equity0 = starting_equity
        self.pessimistic = pessimistic
        self.loader = HistoricalChainLoader(start, end, refresh)
        self.iv_series: list = []           # (date, atm_iv) built as we go
        self.marks_real = 0                 # legs marked from a real printed bar
        self.marks_modelled = 0             # ... and legs we had to model

    # ---- helpers ---------------------------------------------------------- #
    def _iv_rank(self, atm_iv: float, today: date):
        cutoff = today - timedelta(days=config.IV_RANK_LOOKBACK_DAYS)
        hist = [v for d, v in self.iv_series if cutoff <= d <= today]
        if len(hist) < config.IV_RANK_MIN_SAMPLES:
            return None
        return sum(1 for v in hist if v < atm_iv) / len(hist) * 100.0

    @staticmethod
    def _price_on(day_bars: dict, symbol: str):
        row = day_bars.get(symbol)
        return (float(row["close"]), float(row["volume"])) if row is not None else None

    def _theoretical(self, kind, strike, spot, dte, iv):
        return bs_price(spot, strike, max(dte, 0.02) / 365.0, config.RISK_FREE_RATE,
                        max(iv, 0.05), kind)

    def _mark_spread(self, pos: SimPosition, day: date, day_bars: dict,
                     spot: float, fallback_iv: float):
        """Debit to buy the spread back, from that day's option closes."""
        debit = 0.0
        dte = (pos.expiry - day).days
        for leg in pos.cand.legs:
            got = self._price_on(day_bars, leg.symbol)
            if got is not None and got[0] > 0:
                px = got[0]
                self.marks_real += 1
            else:                               # no print that day -> model it
                px = self._theoretical(leg.kind, leg.strike, spot, dte, fallback_iv)
                self.marks_modelled += 1
            half = modelled_spread(px) / 2.0
            # closing a short leg means paying the ask; closing a long leg
            # means hitting the bid. Both go against us.
            debit += (px + half) if leg.side == "sell" else -(px - half)
        return max(0.0, debit)

    @staticmethod
    def _settle(pos: SimPosition, final_spot: float) -> float:
        """Intrinsic value of the spread at expiry, per share."""
        val = 0.0
        for leg in pos.cand.legs:
            intr = max(0.0, leg.strike - final_spot) if leg.kind == "put" \
                else max(0.0, final_spot - leg.strike)
            val += intr if leg.side == "sell" else -intr
        return max(0.0, val)

    # ---- main loop --------------------------------------------------------- #
    def run(self) -> dict:
        under = self.loader.underlying()
        opts = self.loader.option_bars(under)
        opts_by_day = {d: g.set_index("symbol").to_dict("index")
                       for d, g in opts.groupby("date")}

        trading_days = [d for d in under.index
                        if self.start <= d.date() <= self.end]

        equity = self.equity0
        open_pos: list = []
        closed: list = []
        curve = []
        halted_until = None
        daily_realized = {}

        for ts in trading_days:
            today = ts.date()
            spot = float(under.loc[ts, "close"])
            day_bars = opts_by_day.get(pd.Timestamp(today), {})
            hist_bars = under[under.index <= ts]
            if len(hist_bars) < 60:
                continue

            # ---------- mark and manage what is already on ----------------- #
            still_open = []
            for pos in open_pos:
                dte = (pos.expiry - today).days
                atm_iv_guess = pos.entry_iv or 0.5

                if dte <= 0:
                    final = float(under.loc[ts, "close"])
                    settle = self._settle(pos, final)
                    pnl = ((pos.entry_credit - settle) * 100.0 * pos.qty
                           - self._costs(pos))
                    pos.exit_day, pos.exit_debit = today, settle
                    pos.reason, pos.pnl = "expired", pnl
                    equity += pnl
                    daily_realized[today] = daily_realized.get(today, 0.0) + pnl
                    closed.append(pos)
                    continue

                debit = self._mark_spread(pos, today, day_bars, spot, atm_iv_guess)
                profit_pct = ((pos.entry_credit - debit) / pos.entry_credit
                              if pos.entry_credit > 0 else 0.0)
                pos.peak_profit_pct = max(pos.peak_profit_pct, profit_pct)

                reason = ""
                if profit_pct >= config.PROFIT_TARGET_PCT:
                    reason = "profit_target"
                elif debit >= stop_level(pos.entry_credit, pos.cand.width):
                    reason = "stop_loss"
                elif dte <= config.CLOSE_AT_DTE:
                    reason = "time_exit"
                elif (pos.peak_profit_pct >= config.TRAIL_AFTER_PCT
                      and profit_pct <= pos.peak_profit_pct - config.TRAIL_GIVEBACK_PCT):
                    reason = "trail"

                if reason:
                    pnl = (pos.entry_credit - debit) * 100.0 * pos.qty - self._costs(pos)
                    pos.exit_day, pos.exit_debit = today, debit
                    pos.reason, pos.pnl = reason, pnl
                    equity += pnl
                    daily_realized[today] = daily_realized.get(today, 0.0) + pnl
                    closed.append(pos)
                else:
                    pos.last_debit = debit
                    still_open.append(pos)
            open_pos = still_open

            # Equity must include open-position marks, otherwise drawdown only
            # measures the moments we chose to realise losses.
            mtm = equity + sum((p.entry_credit - getattr(p, "last_debit", p.entry_credit))
                               * 100.0 * p.qty for p in open_pos)

            # ---------- circuit breakers ------------------------------------ #
            if halted_until and today < halted_until:
                curve.append((today, mtm, spot, equity))
                continue
            halted_until = None

            if daily_realized.get(today, 0.0) < -equity * config.DAILY_LOSS_LIMIT_PCT:
                halted_until = today + timedelta(days=1)
                curve.append((today, mtm, spot, equity))
                continue

            recent = sorted(closed, key=lambda p: (p.exit_day, p.entry_day))[-config.CONSECUTIVE_LOSS_HALT:]
            if (len(recent) == config.CONSECUTIVE_LOSS_HALT
                    and all(p.pnl < 0 for p in recent)
                    and all(p.exit_day >= today - timedelta(days=5) for p in recent)):
                halted_until = today + timedelta(days=3)
                curve.append((today, mtm, spot, equity))
                continue

            # ---------- look for a new trade -------------------------------- #
            if len(open_pos) < config.MAX_OPEN_SPREADS:
                snap = self._build_snapshot(today, spot, day_bars, hist_bars)
                if snap is not None:
                    held = {s for p in open_pos for s in p.cand.all_symbols}
                    cands, _ = strategy.find_candidates(snap, held)
                    opened_today = 0
                    for cand in cands:
                        if opened_today >= config.MAX_NEW_TRADES_PER_DAY:
                            break
                        if len(open_pos) >= config.MAX_OPEN_SPREADS:
                            break
                        if sum(1 for p in open_pos if p.expiry == cand.expiry) >= config.MAX_PER_EXPIRY:
                            continue
                        if set(cand.all_symbols) & {s for p in open_pos for s in p.cand.all_symbols}:
                            continue

                        qty = self._size(cand, equity, open_pos)
                        if qty < 1:
                            continue
                        open_pos.append(SimPosition(cand, qty, today, spot, snap.vol.atm_iv))
                        opened_today += 1

            curve.append((today, mtm, spot, equity))

        return self._report(closed, curve, open_pos)

    # ---- sizing (mirrors risk.size_position without broker state) ---------- #
    @staticmethod
    def _size(cand, equity: float, open_pos: list) -> int:
        per_risk = cand.max_loss
        if per_risk <= 0:
            return 0
        by_trade = int((equity * config.RISK_PER_TRADE_PCT) // per_risk)
        used = sum(p.risk for p in open_pos)
        budget = equity * config.MAX_PORTFOLIO_RISK_PCT - used
        by_portfolio = int(max(0.0, budget) // per_risk)

        f = 0.0
        if cand.max_loss > 0 and cand.max_profit > 0:
            b = cand.max_profit / cand.max_loss
            f = max(0.0, (b * cand.pop - (1 - cand.pop)) / b)
        by_kelly = int((equity * f * config.KELLY_FRACTION) // per_risk)

        return max(0, min(by_trade, by_portfolio, by_kelly, config.MAX_CONTRACTS_PER_TRADE))

    @staticmethod
    def _costs(pos: SimPosition) -> float:
        legs = len(pos.cand.legs)
        return (config.COMMISSION_PER_CONTRACT * legs * 2 * pos.qty
                + config.SLIPPAGE_PER_LEG * legs * 100.0 * pos.qty)

    # ---- chain reconstruction ---------------------------------------------- #
    def _build_snapshot(self, today: date, spot: float, day_bars: dict,
                        hist_bars: pd.DataFrame):
        by_expiry: dict = {}
        for symbol, row in day_bars.items():
            close = float(row.get("close") or 0)
            volume = float(row.get("volume") or 0)
            if close <= 0.01 or volume < 5:       # stands in for the live liquidity filter
                continue
            from .marketdata import parse_occ
            parsed = parse_occ(symbol)
            if not parsed:
                continue
            _, expiry, kind, strike = parsed
            dte = (expiry - today).days
            if dte < config.MIN_DTE or dte > config.MAX_DTE:
                continue
            if not (0.6 * spot < strike < 1.4 * spot):
                continue
            q = synth_quote(symbol, kind, strike, expiry, dte, close, spot, volume)
            if q.iv is None or not (0.05 < q.iv < 3.0):
                continue
            by_expiry.setdefault(expiry, []).append(q)

        if not by_expiry:
            return None

        atm_iv = self._atm_iv(by_expiry, spot, today)
        if atm_iv is None:
            return None
        self.iv_series.append((today, atm_iv))
        iv_rank = self._iv_rank(atm_iv, today)
        vol = compute_vol_stats(spot, atm_iv, hist_bars, iv_rank)
        return ChainSnapshot(datetime.combine(today, datetime.min.time(), timezone.utc),
                             spot, vol, by_expiry)

    @staticmethod
    def _atm_iv(by_expiry: dict, spot: float, today: date):
        target = min(by_expiry, key=lambda e: abs((e - today).days - config.IV_REFERENCE_DTE))
        quotes = [q for q in by_expiry[target] if q.iv]
        if not quotes:
            return None
        ivs = []
        for kind in ("put", "call"):
            side = [q for q in quotes if q.kind == kind]
            if side:
                ivs.append(min(side, key=lambda q: abs(q.strike - spot)).iv)
        return round(sum(ivs) / len(ivs), 4) if ivs else None

    # ---- reporting ---------------------------------------------------------- #
    def _report(self, closed: list, curve: list, still_open: list) -> dict:
        if not curve:
            return {"error": "no trading days in range"}

        eq = pd.DataFrame(curve, columns=["date", "equity", "spot", "realized_equity"]).set_index("date")
        rets = eq["equity"].pct_change().dropna()

        wins = [p for p in closed if p.pnl > 0]
        losses = [p for p in closed if p.pnl <= 0]
        gross_win = sum(p.pnl for p in wins)
        gross_loss = abs(sum(p.pnl for p in losses))

        peak = eq["equity"].cummax()
        dd = (eq["equity"] - peak) / peak
        years = max((curve[-1][0] - curve[0][0]).days / 365.25, 1e-9)
        total_ret = eq["equity"].iloc[-1] / self.equity0 - 1.0

        bh = eq["spot"].iloc[-1] / eq["spot"].iloc[0] - 1.0
        bh_curve = self.equity0 * eq["spot"] / eq["spot"].iloc[0]
        bh_dd = ((bh_curve - bh_curve.cummax()) / bh_curve.cummax()).min()

        sharpe = 0.0
        if len(rets) > 5 and rets.std() > 0:
            sharpe = float(rets.mean() / rets.std() * math.sqrt(252))
        downside = rets[rets < 0]
        sortino = float(rets.mean() / downside.std() * math.sqrt(252)) \
            if len(downside) > 2 and downside.std() > 0 else 0.0

        by_reason = {}
        for p in closed:
            r = by_reason.setdefault(p.reason, {"n": 0, "pnl": 0.0})
            r["n"] += 1
            r["pnl"] += p.pnl

        return {
            "period": f"{curve[0][0]} to {curve[-1][0]}",
            "trading_days": len(curve),
            "starting_equity": self.equity0,
            "ending_equity": round(float(eq["equity"].iloc[-1]), 2),
            "total_return_pct": round(total_ret * 100, 2),
            "cagr_pct": round(((1 + total_ret) ** (1 / years) - 1) * 100, 2),
            "max_drawdown_pct": round(float(dd.min()) * 100, 2),
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
            "trades": len(closed),
            "still_open_at_end": len(still_open),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            "largest_loss": round(min((p.pnl for p in closed), default=0.0), 2),
            "expectancy_per_trade": round(sum(p.pnl for p in closed) / len(closed), 2) if closed else 0.0,
            "exits_by_reason": {k: {"n": v["n"], "pnl": round(v["pnl"], 2)}
                                for k, v in sorted(by_reason.items())},
            "pct_legs_marked_from_real_prints": round(
                100 * self.marks_real / max(1, self.marks_real + self.marks_modelled), 1),
            "trades_by_year": {int(y): int(n) for y, n in
                               pd.Series([p.exit_day.year for p in closed]).value_counts().sort_index().items()}
            if closed else {},
            "pnl_by_year": {int(y): round(float(v), 2) for y, v in
                            pd.DataFrame({"y": [p.exit_day.year for p in closed],
                                          "p": [p.pnl for p in closed]}).groupby("y").p.sum().items()}
            if closed else {},
            "buy_hold_return_pct": round(bh * 100, 2),
            "buy_hold_max_drawdown_pct": round(float(bh_dd) * 100, 2),
            "_equity_curve": eq,
            "_closed": closed,
        }


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def _fmt(result: dict) -> str:
    lines = ["", "=" * 68, "  BACKTEST RESULT", "=" * 68]
    order = ["period", "trading_days", "starting_equity", "ending_equity",
             "total_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe",
             "sortino", "trades", "win_rate_pct", "profit_factor",
             "expectancy_per_trade", "avg_win", "avg_loss", "largest_loss",
             "still_open_at_end", "buy_hold_return_pct", "buy_hold_max_drawdown_pct"]
    for k in order:
        if k in result:
            lines.append(f"  {k:<28}: {result[k]}")
    if result.get("exits_by_reason"):
        lines.append("  " + "-" * 64)
        lines.append(f"  {'exit reason':<28}  {'n':>5}  {'pnl':>12}")
        for reason, v in result["exits_by_reason"].items():
            lines.append(f"  {reason:<28}  {v['n']:>5}  {v['pnl']:>12,.2f}")
    lines.append("=" * 68)
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest the IBIT credit-spread strategy")
    ap.add_argument("--start", default=str(OPTIONS_START))
    ap.add_argument("--end", default=str(date.today() - timedelta(days=1)))
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--refresh", action="store_true", help="ignore the bar cache")
    ap.add_argument("--out", default=os.path.join(config.DATA_DIR, "backtest"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if start < OPTIONS_START:
        log.warning("IBIT options did not exist before %s; clamping start", OPTIONS_START)
        start = OPTIONS_START

    bt = Backtester(start, end, args.equity, args.refresh)
    result = bt.run()
    print(_fmt(result))

    os.makedirs(args.out, exist_ok=True)
    eq = result.pop("_equity_curve", None)
    closed = result.pop("_closed", [])
    if eq is not None:
        eq.to_csv(os.path.join(args.out, "equity_curve.csv"))
    if closed:
        pd.DataFrame([{
            "entry": p.entry_day, "exit": p.exit_day, "structure": p.cand.structure,
            "expiry": p.cand.expiry, "dte_at_entry": p.cand.dte, "qty": p.qty,
            "credit": p.entry_credit, "width": p.cand.width,
            "credit_ratio": p.cand.credit_ratio, "pop": round(p.cand.pop, 4),
            "ev_per_ct": p.cand.ev_per_contract, "entry_spot": p.entry_spot,
            "exit_debit": p.exit_debit, "reason": p.reason, "pnl": round(p.pnl, 2),
            "risk": p.risk,
        } for p in closed]).to_csv(os.path.join(args.out, "trades.csv"), index=False)
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"\nwrote {args.out}/summary.json, equity_curve.csv, trades.csv")
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
