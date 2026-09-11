"""Candidate generation and expected-value scoring for defined-risk credit
structures on IBIT.

The central idea
----------------
Under the risk-neutral measure implied by option prices, *every* credit spread
has an expected value of exactly zero before costs, and a negative one after.
So a bot that just hunts for "high probability" trades is guaranteed to lose:
the market already charged it for that probability.

The only durable edge in selling options is the variance risk premium -- the
tendency of implied vol to exceed the volatility that subsequently shows up.
This module makes that explicit:

    credit received      is priced by the MARKET's implied vol
    probability of loss  is evaluated under OUR forecast realized vol

    EV = credit - fair_value_at_forecast_vol - costs

When IV == forecast RV the EV is zero and nothing trades. When IV is rich the
EV is positive and the size of that gap is the whole edge. Nothing here is
scored on "win rate" alone.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from . import config
from .greeks import bs_price, norm_cdf, prob_touch
from .marketdata import ChainSnapshot, OptionQuote, VolStats

log = logging.getLogger(__name__)

# Forecast vol is itself uncertain. A candidate must stay positive-EV when the
# forecast is stressed upward by this factor, otherwise we are trading noise.
EV_STRESS_VOL_MULT = 1.15


@dataclass
class Leg:
    symbol: str
    kind: str
    strike: float
    side: str          # "sell" | "buy"
    mid: float
    bid: float
    ask: float
    iv: float = None
    delta: float = None


@dataclass
class SpreadCandidate:
    structure: str                 # put_credit | call_credit | iron_condor
    expiry: date
    dte: int
    legs: list = field(default_factory=list)

    credit: float = 0.0            # per share, net, at our limit price
    width: float = 0.0             # widest single-side width -> margin driver
    max_profit: float = 0.0        # per contract, dollars, before costs
    max_loss: float = 0.0          # per contract, dollars
    credit_ratio: float = 0.0      # credit / width

    pop: float = 0.0               # P(profit at expiry) under forecast RV
    pop_rn: float = 0.0            # ... under market-implied vol (for contrast)
    p_touch: float = 0.0           # P(short strike tagged before expiry)
    ev_per_contract: float = 0.0   # dollars, after modelled costs
    ev_on_risk: float = 0.0        # ev / max_loss
    ev_stressed: float = 0.0       # EV with forecast vol x EV_STRESS_VOL_MULT

    short_delta: float = 0.0
    net_delta: float = 0.0
    worst_leg_spread_pct: float = 0.0
    min_open_interest: float = 0.0
    breakeven_low: float = None
    breakeven_high: float = None
    score: float = 0.0
    notes: list = field(default_factory=list)

    @property
    def short_symbols(self) -> list:
        return [l.symbol for l in self.legs if l.side == "sell"]

    @property
    def all_symbols(self) -> list:
        return [l.symbol for l in self.legs]

    def describe(self) -> str:
        legs = " / ".join(
            f"{'S' if l.side == 'sell' else 'B'}{l.strike:g}{l.kind[0].upper()}"
            for l in self.legs)
        return (f"{self.structure} {self.expiry} ({self.dte}d) {legs} "
                f"cr=${self.credit:.2f} w=${self.width:g} "
                f"({self.credit_ratio:.0%}) POP={self.pop:.1%} "
                f"EV=${self.ev_per_contract:.2f}/ct")


# --------------------------------------------------------------------------- #
# expected value
# --------------------------------------------------------------------------- #
def _expected_short_payout(spot: float, strike: float, T: float, sigma: float,
                           kind: str) -> float:
    """E[max(0, K - S_T)] (put) under a driftless lognormal with vol `sigma`.

    Using r=0 makes the BS formula the *undiscounted real-world expectation*
    of the payoff under a martingale price process, which is exactly the
    quantity we need -- not a risk-neutral valuation.
    """
    return bs_price(spot, strike, T, 0.0, sigma, kind, 0.0)


def _round_trip_cost(n_legs: int) -> float:
    """Per-contract dollars: fees both ways plus slippage vs mid on every leg."""
    fees = config.COMMISSION_PER_CONTRACT * n_legs * 2
    slip = config.SLIPPAGE_PER_LEG * n_legs * 100.0
    return fees + slip


def evaluate(candidate: SpreadCandidate, spot: float, vol: VolStats) -> SpreadCandidate:
    """Fill in POP / EV / score for a structurally complete candidate."""
    T = max(candidate.dte, 0.5) / 365.0
    rv = vol.rv_forecast if vol.rv_forecast > 0 else (vol.hv20 or 0.5)

    shorts = [l for l in candidate.legs if l.side == "sell"]
    longs = [l for l in candidate.legs if l.side == "buy"]

    def fair_value(sigma: float) -> float:
        val = sum(_expected_short_payout(spot, l.strike, T, sigma, l.kind) for l in shorts)
        val -= sum(_expected_short_payout(spot, l.strike, T, sigma, l.kind) for l in longs)
        return val

    cost = _round_trip_cost(len(candidate.legs))
    candidate.ev_per_contract = round((candidate.credit - fair_value(rv)) * 100.0 - cost, 2)
    candidate.ev_stressed = round(
        (candidate.credit - fair_value(rv * EV_STRESS_VOL_MULT)) * 100.0 - cost, 2)
    candidate.ev_on_risk = (candidate.ev_per_contract / candidate.max_loss
                            if candidate.max_loss > 0 else 0.0)

    # Probability of profit: price must stay inside the break-evens.
    lo, hi = candidate.breakeven_low, candidate.breakeven_high
    candidate.pop = _prob_between(spot, lo, hi, T, rv)
    iv_ref = (sum(l.iv for l in shorts if l.iv) / max(1, len([l for l in shorts if l.iv]))
              if any(l.iv for l in shorts) else rv)
    candidate.pop_rn = _prob_between(spot, lo, hi, T, iv_ref)

    candidate.p_touch = max(
        (prob_touch(spot, l.strike, T, iv_ref, config.RISK_FREE_RATE) for l in shorts),
        default=0.0)
    candidate.short_delta = max((abs(l.delta) for l in shorts if l.delta is not None),
                                default=0.0)
    candidate.net_delta = sum(
        (l.delta or 0.0) * (-1 if l.side == "sell" else 1) for l in candidate.legs)

    # Rank primarily on return-on-risk, with a liquidity tax and a bonus for
    # structures whose edge survives the stressed-vol test.
    liquidity_tax = candidate.worst_leg_spread_pct * 0.5
    robustness = 0.25 if candidate.ev_stressed > 0 else 0.0
    candidate.score = round(candidate.ev_on_risk + robustness - liquidity_tax, 5)
    return candidate


def _prob_between(spot: float, lo, hi, T: float, sigma: float) -> float:
    """P(lo <= S_T <= hi) under a driftless lognormal."""
    import math
    if sigma <= 0 or T <= 0:
        return 0.0
    vol_t = sigma * math.sqrt(T)

    def cdf_at(k):
        if k is None:
            return None
        if k <= 0:
            return 0.0
        # driftless: ln(S_T/S) ~ N(-0.5 s^2 T, s^2 T)
        z = (math.log(k / spot) + 0.5 * sigma * sigma * T) / vol_t
        return norm_cdf(z)

    p_lo, p_hi = cdf_at(lo), cdf_at(hi)
    if p_lo is None:
        p_lo = 0.0
    if p_hi is None:
        p_hi = 1.0
    return max(0.0, min(1.0, p_hi - p_lo))


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
def leg_is_liquid(q: OptionQuote) -> bool:
    if not q.tradable:
        return False
    if q.spread_pct > config.MAX_LEG_SPREAD_PCT and q.spread_abs > config.MAX_LEG_SPREAD_ABS:
        return False
    if q.open_interest is not None and q.open_interest < config.MIN_OPEN_INTEREST:
        return False
    if min(q.bid_size, q.ask_size) < config.MIN_QUOTE_SIZE:
        return False
    return True


def edge_check(vol: VolStats) -> tuple:
    """Is there a variance risk premium worth harvesting right now?"""
    reasons = []
    if vol.atm_iv is None:
        return False, ["no ATM implied vol available"]
    if vol.rv_forecast <= 0:
        return False, ["no realized-vol estimate"]

    if vol.vrp_ratio < config.MIN_VRP_RATIO:
        reasons.append(f"IV/RV {vol.vrp_ratio:.2f} < {config.MIN_VRP_RATIO:.2f}")
    if vol.vrp < config.MIN_VRP_ABS:
        reasons.append(f"IV-RV {vol.vrp:+.3f} < {config.MIN_VRP_ABS:.3f}")
    if config.REQUIRE_IV_RANK:
        if vol.iv_rank is None:
            reasons.append("IV rank unavailable and REQUIRE_IV_RANK is on")
        elif vol.iv_rank < config.MIN_IV_RANK:
            reasons.append(f"IV rank {vol.iv_rank:.0f} < {config.MIN_IV_RANK:.0f}")
    return (not reasons), reasons


def allowed_structures(vol: VolStats) -> set:
    """Directional gate. Selling puts into a downtrend is how premium sellers
    blow up; this refuses to do it."""
    if not config.USE_REGIME_FILTER:
        out = set()
        if config.ALLOW_PUT_SPREADS:
            out.add("put_credit")
        if config.ALLOW_CALL_SPREADS:
            out.add("call_credit")
        if config.ALLOW_IRON_CONDORS:
            out.add("iron_condor")
        return out

    out = set()
    if config.ALLOW_PUT_SPREADS and vol.regime in ("bull", "neutral") \
            and vol.rsi14 > config.RSI_OVERSOLD:
        out.add("put_credit")
    if config.ALLOW_CALL_SPREADS and vol.regime in ("bear", "neutral") \
            and vol.rsi14 < config.RSI_OVERBOUGHT:
        out.add("call_credit")
    if config.ALLOW_IRON_CONDORS and vol.regime == "neutral" \
            and config.RSI_OVERSOLD < vol.rsi14 < config.RSI_OVERBOUGHT:
        out.add("iron_condor")
    return out


# --------------------------------------------------------------------------- #
# construction
# --------------------------------------------------------------------------- #
def _entry_credit(short_q: OptionQuote, long_q: OptionQuote) -> float:
    """The limit price we will actually work, not a fantasy mid."""
    mid_credit = short_q.mid - long_q.mid
    natural = short_q.bid - long_q.ask            # immediately marketable
    if config.ENTRY_PRICE_MODE == "natural":
        px = natural
    elif config.ENTRY_PRICE_MODE == "mid":
        px = mid_credit
    else:                                          # mid_minus: concede a little
        px = mid_credit - config.ENTRY_PRICE_PAD
    px = max(px, natural)                          # never bid below marketable
    return round(max(px, 0.0) / config.ORDER_TICK) * config.ORDER_TICK


def _build_vertical(kind: str, expiry: date, dte: int, quotes: list, spot: float,
                    excluded: set) -> list:
    """One-sided credit verticals: short an OTM strike, buy further OTM."""
    side = [q for q in quotes if q.kind == kind and leg_is_liquid(q)]
    if len(side) < 2:
        return []

    # Short strike must be OTM on the correct side.
    if kind == "put":
        shorts = [q for q in side if q.strike < spot]
    else:
        shorts = [q for q in side if q.strike > spot]

    by_strike = {q.strike: q for q in side}
    out = []

    for sq in shorts:
        if sq.delta is None or sq.symbol in excluded:
            continue
        d = abs(sq.delta)
        if not (config.SHORT_DELTA_MIN <= d <= config.SHORT_DELTA_MAX):
            continue

        for width in config.SPREAD_WIDTHS:
            long_strike = round(sq.strike - width, 2) if kind == "put" else round(sq.strike + width, 2)
            lq = by_strike.get(long_strike)
            if lq is None or lq.symbol in excluded:
                continue

            credit = _entry_credit(sq, lq)
            if credit < config.MIN_NET_CREDIT:
                continue
            ratio = credit / width
            if not (config.MIN_CREDIT_RATIO <= ratio <= config.MAX_CREDIT_RATIO):
                continue

            if kind == "put":
                be_low, be_high = sq.strike - credit, None
            else:
                be_low, be_high = None, sq.strike + credit

            cand = SpreadCandidate(
                structure=f"{kind}_credit", expiry=expiry, dte=dte,
                legs=[Leg(sq.symbol, kind, sq.strike, "sell", sq.mid, sq.bid, sq.ask, sq.iv, sq.delta),
                      Leg(lq.symbol, kind, lq.strike, "buy", lq.mid, lq.bid, lq.ask, lq.iv, lq.delta)],
                credit=round(credit, 2), width=width,
                max_profit=round(credit * 100, 2),
                max_loss=round((width - credit) * 100, 2),
                credit_ratio=round(ratio, 4),
                worst_leg_spread_pct=max(sq.spread_pct, lq.spread_pct),
                min_open_interest=min(x for x in (sq.open_interest, lq.open_interest)
                                      if x is not None) if (sq.open_interest is not None
                                                            and lq.open_interest is not None) else 0.0,
                breakeven_low=be_low, breakeven_high=be_high)
            out.append(cand)
    return out


def _build_condors(put_side: list, call_side: list, spot: float) -> list:
    """Iron condor = put credit spread + call credit spread, same expiry.
    Margin is the wider single side, so the return on risk roughly doubles."""
    out = []
    for pc in put_side[:6]:
        for cc in call_side[:6]:
            if pc.expiry != cc.expiry:
                continue
            credit = round(pc.credit + cc.credit, 2)
            width = max(pc.width, cc.width)
            if credit >= width:                 # arbitrage-looking: bad data
                continue
            ratio = credit / width
            if not (config.MIN_CREDIT_RATIO <= ratio <= config.MAX_CREDIT_RATIO):
                continue

            short_put = min(l.strike for l in pc.legs if l.side == "sell")
            short_call = max(l.strike for l in cc.legs if l.side == "sell")
            out.append(SpreadCandidate(
                structure="iron_condor", expiry=pc.expiry, dte=pc.dte,
                legs=pc.legs + cc.legs,
                credit=credit, width=width,
                max_profit=round(credit * 100, 2),
                max_loss=round((width - credit) * 100, 2),
                credit_ratio=round(ratio, 4),
                worst_leg_spread_pct=max(pc.worst_leg_spread_pct, cc.worst_leg_spread_pct),
                min_open_interest=min(pc.min_open_interest, cc.min_open_interest),
                breakeven_low=short_put - credit,
                breakeven_high=short_call + credit))
    return out


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def find_candidates(snap: ChainSnapshot, excluded_symbols=None,
                    ignore_edge: bool = False) -> tuple:
    """Returns (ranked_candidates, rejection_reasons)."""
    excluded = set(excluded_symbols or [])
    vol = snap.vol

    has_edge, reasons = edge_check(vol)
    if not has_edge and not ignore_edge:
        return [], reasons

    structures = allowed_structures(vol)
    if not structures:
        return [], [f"regime '{vol.regime}' (RSI {vol.rsi14:.0f}) permits no structure"]

    all_cands = []
    for expiry, quotes in sorted(snap.by_expiry.items()):
        dte = quotes[0].dte
        if not (config.MIN_DTE <= dte <= config.MAX_DTE):
            continue

        puts = _build_vertical("put", expiry, dte, quotes, snap.spot, excluded) \
            if "put_credit" in structures or "iron_condor" in structures else []
        calls = _build_vertical("call", expiry, dte, quotes, snap.spot, excluded) \
            if "call_credit" in structures or "iron_condor" in structures else []

        for c in puts:
            if "put_credit" in structures:
                all_cands.append(c)
        for c in calls:
            if "call_credit" in structures:
                all_cands.append(c)

        if "iron_condor" in structures and puts and calls:
            ranked_p = sorted((evaluate(c, snap.spot, vol) for c in puts),
                              key=lambda c: -c.score)
            ranked_c = sorted((evaluate(c, snap.spot, vol) for c in calls),
                              key=lambda c: -c.score)
            all_cands.extend(_build_condors(ranked_p, ranked_c, snap.spot))

    if not all_cands:
        return [], ["no structurally valid spreads (credit/width, delta or liquidity filters)"]

    scored = [evaluate(c, snap.spot, vol) for c in all_cands]

    # Prefer the expiry closest to TARGET_DTE when scores are close.
    survivors = [c for c in scored
                 if c.ev_per_contract >= config.MIN_EV_PER_CONTRACT
                 and c.ev_on_risk >= config.MIN_EV_ON_RISK]
    if not survivors:
        best = max(scored, key=lambda c: c.ev_per_contract)
        return [], [f"no positive-EV structure (best EV ${best.ev_per_contract:.2f}/ct "
                    f"on {best.describe()})"]

    survivors.sort(key=lambda c: (-c.score, abs(c.dte - config.TARGET_DTE)))
    return survivors, []
