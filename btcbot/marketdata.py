"""Market data: underlying quotes/bars, realized-vol estimators, option chain
normalisation and a persisted IV-rank series.

Everything the strategy needs in order to judge *whether there is edge* lives
here. The old bot had none of this -- it inferred win probability from a
made-up linear formula.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (OptionChainRequest, StockBarsRequest,
                                  StockLatestQuoteRequest)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest

from . import config
from .greeks import bs_greeks, implied_vol

log = logging.getLogger(__name__)

TRADING_DAYS = 252.0


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
@dataclass
class OptionQuote:
    symbol: str
    kind: str                 # "put" | "call"
    strike: float
    expiry: date
    dte: int
    bid: float
    ask: float
    mid: float
    spread_abs: float
    spread_pct: float         # bid-ask spread / mid
    bid_size: float
    ask_size: float
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    open_interest: float | None = None

    @property
    def tradable(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid


@dataclass
class VolStats:
    spot: float
    hv10: float
    hv20: float
    hv60: float
    hv_yz20: float            # Yang-Zhang (OHLC-efficient) 20d
    ewma: float
    rv_forecast: float        # blended forward realized-vol estimate
    atm_iv: float | None
    iv_rank: float | None     # 0-100 percentile of atm_iv in trailing history
    vrp: float | None         # atm_iv - rv_forecast  (variance risk premium)
    vrp_ratio: float | None   # atm_iv / rv_forecast
    trend_fast: float
    trend_slow: float
    rsi14: float
    regime: str               # "bull" | "bear" | "neutral"


@dataclass
class ChainSnapshot:
    asof: datetime
    spot: float
    vol: VolStats
    by_expiry: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# volatility estimators
# --------------------------------------------------------------------------- #
def _log_returns(closes: pd.Series) -> pd.Series:
    ratio = closes / closes.shift(1)
    return ratio[ratio > 0].apply(math.log).dropna()


def close_to_close_vol(closes: pd.Series, window: int) -> float:
    rets = _log_returns(closes)
    if len(rets) < 5:
        return 0.0
    window = min(window, len(rets))
    return float(rets.tail(window).std(ddof=1) * math.sqrt(TRADING_DAYS))


def yang_zhang_vol(df: pd.DataFrame, window: int = 20) -> float:
    """Yang-Zhang: drift-independent and gap-aware -- the right estimator for a
    crypto proxy that keeps moving while the US market is shut."""
    d = df.tail(window + 1).copy()
    if len(d) < 6:
        return 0.0
    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    pc = c.shift(1)

    log_oc = (o / pc).apply(math.log).dropna()          # overnight gap
    log_co = (c / o).apply(math.log).dropna()           # intraday open->close
    rs = ((h / c).apply(math.log) * (h / o).apply(math.log)
          + (l / c).apply(math.log) * (l / o).apply(math.log)).dropna()

    n = min(len(log_oc), len(log_co), len(rs))
    if n < 5:
        return 0.0
    log_oc, log_co, rs = log_oc.tail(n), log_co.tail(n), rs.tail(n)

    v_o = float(log_oc.var(ddof=1))
    v_c = float(log_co.var(ddof=1))
    v_rs = float(rs.mean())
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    var = v_o + k * v_c + (1 - k) * v_rs
    return math.sqrt(max(var, 0.0) * TRADING_DAYS)


def ewma_vol(closes: pd.Series, lam: float = 0.94) -> float:
    rets = _log_returns(closes)
    if len(rets) < 10:
        return 0.0
    tail = rets.tail(90)
    var = float(tail.var(ddof=1))
    for r in tail:
        var = lam * var + (1 - lam) * r * r
    return math.sqrt(max(var, 0.0) * TRADING_DAYS)


def rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff().dropna()
    if len(delta) < period + 1:
        return 50.0
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + float(gain.iloc[-1]) / last_loss))


def compute_vol_stats(spot: float, atm_iv, bars: pd.DataFrame,
                      iv_rank=None) -> VolStats:
    """Shared by the live bot and the backtester so both see identical inputs."""
    closes = bars["close"]
    hv10, hv20, hv60 = (close_to_close_vol(closes, w) for w in (10, 20, 60))
    hv_yz = yang_zhang_vol(bars, 20)
    ew = ewma_vol(closes)

    parts = [v for v in (ew, hv20, hv_yz, hv60) if v > 0]
    if len(parts) == 4:
        rv_forecast = 0.40 * ew + 0.25 * hv20 + 0.20 * hv_yz + 0.15 * hv60
    else:
        rv_forecast = sum(parts) / len(parts) if parts else 0.0

    vrp = vrp_ratio = None
    if atm_iv and rv_forecast > 0:
        vrp = atm_iv - rv_forecast
        vrp_ratio = atm_iv / rv_forecast

    fast = float(closes.tail(config.TREND_FAST).mean())
    slow = float(closes.tail(config.TREND_SLOW).mean())

    if spot > slow and fast > slow:
        regime = "bull"
    elif spot < slow and fast < slow:
        regime = "bear"
    else:
        regime = "neutral"

    return VolStats(spot, hv10, hv20, hv60, hv_yz, ew, rv_forecast, atm_iv,
                    iv_rank, vrp, vrp_ratio, fast, slow, rsi(closes), regime)


# --------------------------------------------------------------------------- #
# IV history (persisted, so IV-rank is measured rather than guessed)
# --------------------------------------------------------------------------- #
class IVHistory:
    def __init__(self, path: str = config.IV_HISTORY_PATH):
        self.path = path
        self._df = None

    def load(self) -> pd.DataFrame:
        if self._df is None:
            if os.path.exists(self.path):
                self._df = pd.read_csv(self.path, parse_dates=["date"])
            else:
                self._df = pd.DataFrame(columns=["date", "atm_iv"])
        return self._df

    def record(self, day: date, atm_iv: float) -> None:
        df = self.load()
        stamp = pd.Timestamp(day)
        df = df[df["date"] != stamp]
        row = pd.DataFrame([{"date": stamp, "atm_iv": atm_iv}])
        df = (row if df.empty else pd.concat([df, row], ignore_index=True)).sort_values("date")
        self._df = df
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        df.to_csv(self.path, index=False)

    def rank(self, atm_iv: float, asof: date = None,
             lookback_days: int = config.IV_RANK_LOOKBACK_DAYS):
        """Percentile of `atm_iv` within the trailing window. None until we
        have enough samples -- callers must handle that rather than fake it."""
        df = self.load()
        if df.empty:
            return None
        asof = asof or date.today()
        window = df[(df["date"] >= pd.Timestamp(asof - timedelta(days=lookback_days)))
                    & (df["date"] <= pd.Timestamp(asof))]["atm_iv"].dropna()
        if len(window) < config.IV_RANK_MIN_SAMPLES:
            return None
        return float((window < atm_iv).mean() * 100.0)


# --------------------------------------------------------------------------- #
# provider
# --------------------------------------------------------------------------- #
class MarketData:
    def __init__(self, trading_client: TradingClient = None):
        self.trading = trading_client or TradingClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=config.ALPACA_PAPER)
        self.stock = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        self.option = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        self.iv_history = IVHistory()
        self._oi_cache: dict = {}
        self._oi_cache_day = None
        self._bars_cache = None
        self._bars_cache_day = None

    # ---- underlying -------------------------------------------------- #
    def spot(self) -> float:
        try:
            res = self.stock.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=config.UNDERLYING))
            q = res[config.UNDERLYING]
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2.0, 4)
        except Exception as exc:
            log.warning("spot quote failed (%s); falling back to last bar", exc)
        bars = self.daily_bars()
        return float(bars["close"].iloc[-1]) if len(bars) else 0.0

    def daily_bars(self, lookback_days: int = 500) -> pd.DataFrame:
        today = date.today()
        if self._bars_cache is not None and self._bars_cache_day == today:
            return self._bars_cache
        df = self.stock.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=config.UNDERLYING, timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=lookback_days))).df
        if not df.empty and isinstance(df.index, pd.MultiIndex):
            df = df.droplevel("symbol")
        self._bars_cache, self._bars_cache_day = df, today
        return df

    # ---- open interest ------------------------------------------------ #
    def open_interest_map(self, min_exp: date, max_exp: date) -> dict:
        """OI lives on the contracts endpoint, not the chain snapshot."""
        today = date.today()
        if self._oi_cache_day == today and self._oi_cache:
            return self._oi_cache
        out: dict = {}
        token = None
        try:
            for _ in range(20):
                res = self.trading.get_option_contracts(GetOptionContractsRequest(
                    underlying_symbols=[config.UNDERLYING], status="active",
                    expiration_date_gte=min_exp, expiration_date_lte=max_exp,
                    limit=10_000, page_token=token))
                for c in (getattr(res, "option_contracts", res) or []):
                    oi = getattr(c, "open_interest", None)
                    if oi is not None:
                        out[c.symbol] = float(oi)
                token = getattr(res, "next_page_token", None)
                if not token:
                    break
        except Exception as exc:          # OI is one filter among several
            log.warning("open-interest fetch failed (%s); OI filter relaxed", exc)
        self._oi_cache, self._oi_cache_day = out, today
        return out

    # ---- chain --------------------------------------------------------- #
    def chain(self, min_dte: int = None, max_dte: int = None) -> ChainSnapshot:
        min_dte = config.MIN_DTE if min_dte is None else min_dte
        max_dte = config.MAX_DTE if max_dte is None else max_dte

        spot = self.spot()
        if spot <= 0:
            raise RuntimeError("no usable underlying price")

        today = date.today()
        lo, hi = today + timedelta(days=min_dte), today + timedelta(days=max_dte)

        raw = self.option.get_option_chain(OptionChainRequest(
            underlying_symbol=config.UNDERLYING,
            expiration_date_gte=lo, expiration_date_lte=hi))
        oi_map = self.open_interest_map(lo, hi)

        by_expiry: dict = {}
        for symbol, snap in raw.items():
            parsed = parse_occ(symbol)
            if not parsed:
                continue
            _, expiry, kind, strike = parsed
            dte = (expiry - today).days
            if dte < min_dte or dte > max_dte:
                continue

            q = getattr(snap, "latest_quote", None)
            if q is None:
                continue
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            mid = (bid + ask) / 2.0
            spread_abs = ask - bid

            iv = getattr(snap, "implied_volatility", None)
            g = getattr(snap, "greeks", None)
            delta = getattr(g, "delta", None) if g else None
            gamma = getattr(g, "gamma", None) if g else None
            theta = getattr(g, "theta", None) if g else None
            vega = getattr(g, "vega", None) if g else None

            T = max(dte, 0.5) / 365.0
            if iv is None:                       # feed withheld it -> solve for it
                iv = implied_vol(mid, spot, strike, T, config.RISK_FREE_RATE, kind)
            if iv and delta is None:
                gk = bs_greeks(spot, strike, T, config.RISK_FREE_RATE, iv, kind)
                delta, gamma, theta, vega = gk.delta, gk.gamma, gk.theta, gk.vega

            by_expiry.setdefault(expiry, []).append(OptionQuote(
                symbol=symbol, kind=kind, strike=strike, expiry=expiry, dte=dte,
                bid=bid, ask=ask, mid=round(mid, 4), spread_abs=round(spread_abs, 4),
                spread_pct=(spread_abs / mid) if mid > 0 else 9.99,
                bid_size=float(q.bid_size or 0), ask_size=float(q.ask_size or 0),
                iv=iv, delta=delta, gamma=gamma, theta=theta, vega=vega,
                open_interest=oi_map.get(symbol)))

        for exp in by_expiry:
            by_expiry[exp].sort(key=lambda o: (o.kind, o.strike))

        atm_iv = self._atm_iv(by_expiry, spot)
        iv_rank = None
        if atm_iv:
            self.iv_history.record(today, atm_iv)
            iv_rank = self.iv_history.rank(atm_iv, today)
        vol = compute_vol_stats(spot, atm_iv, self.daily_bars(), iv_rank)
        return ChainSnapshot(datetime.now(timezone.utc), spot, vol, by_expiry)

    @staticmethod
    def _atm_iv(by_expiry: dict, spot: float):
        """ATM IV taken from the expiry nearest the 30-day reference point,
        averaging the closest put and call to cancel put/call skew bias."""
        if not by_expiry:
            return None
        today = date.today()
        target = min(by_expiry, key=lambda e: abs((e - today).days - config.IV_REFERENCE_DTE))
        quotes = [q for q in by_expiry[target] if q.iv and 0.01 < q.iv < 5.0]
        if not quotes:
            return None
        ivs = []
        for kind in ("put", "call"):
            side = [q for q in quotes if q.kind == kind]
            if side:
                ivs.append(min(side, key=lambda q: abs(q.strike - spot)).iv)
        return round(sum(ivs) / len(ivs), 4) if ivs else None


# --------------------------------------------------------------------------- #
# OCC symbol helpers
# --------------------------------------------------------------------------- #
def parse_occ(symbol: str):
    """'IBIT260911P00041500' -> ('IBIT', date(2026, 9, 11), 'put', 41.5)"""
    if len(symbol) < 16:
        return None
    tail, root = symbol[-15:], symbol[:-15]
    try:
        expiry = datetime.strptime(tail[:6], "%y%m%d").date()
        kind = "call" if tail[6].upper() == "C" else "put"
        strike = int(tail[7:]) / 1000.0
    except (ValueError, IndexError):
        return None
    return root, expiry, kind, strike


def build_occ(root: str, expiry: date, kind: str, strike: float) -> str:
    side = "C" if kind.lower().startswith("c") else "P"
    return f"{root}{expiry:%y%m%d}{side}{int(round(strike * 1000)):08d}"
