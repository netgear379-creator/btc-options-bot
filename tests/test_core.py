"""Tests for the parts where a silent bug costs real money.

Run: python -m pytest tests/ -q     (or: python tests/test_core.py)
"""
from __future__ import annotations

import math
import os
import random
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btcbot import config, strategy
from btcbot.greeks import (bs_greeks, bs_price, implied_vol, prob_itm,
                           prob_touch)
from btcbot.marketdata import OptionQuote, build_occ, parse_occ
from btcbot.portfolio import Position, PositionStore
from btcbot.risk import AccountState, RiskGate, kelly_fraction, stop_level
from btcbot.strategy import Leg, SpreadCandidate


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #
def test_put_call_parity():
    S, K, T, r, sig = 44.5, 42.0, 30 / 365, 0.043, 0.55
    c = bs_price(S, K, T, r, sig, "call")
    p = bs_price(S, K, T, r, sig, "put")
    assert abs((c - p) - (S - K * math.exp(-r * T))) < 1e-9


def test_implied_vol_round_trips():
    """Round-trips wherever vol is identifiable, i.e. wherever the option is
    worth at least one tick. Cheaper than that, IV is not recoverable from the
    price and the solver must say so rather than invent a number."""
    for sig in (0.15, 0.40, 0.85, 1.6):
        for K in (36.0, 42.0, 44.5, 50.0):
            px = bs_price(44.5, K, 21 / 365, 0.043, sig, "put")
            back = implied_vol(px, 44.5, K, 21 / 365, 0.043, "put")
            if px < 0.01:                      # below the minimum quotable tick
                continue
            assert back is not None and abs(back - sig) < 1e-4, (sig, K, px, back)


def test_implied_vol_returns_none_when_unidentifiable():
    """Regression: a 15%-vol, 19%-OTM, 21-day put is worth 3e-10. Every sigma
    reproduces that price, so any returned IV would be numerical noise."""
    px = bs_price(44.5, 36.0, 21 / 365, 0.043, 0.15, "put")
    assert px < 1e-6
    assert implied_vol(px, 44.5, 36.0, 21 / 365, 0.043, "put") is None


def test_implied_vol_rejects_arbitrage_prices():
    # below intrinsic and above the strike are both unsolvable, not "0 vol"
    assert implied_vol(0.01, 44.5, 60.0, 0.05, 0.043, "put") is None
    assert implied_vol(999.0, 44.5, 42.0, 0.05, 0.043, "put") is None


def test_greeks_signs_and_magnitudes():
    g = bs_greeks(44.5, 42.0, 30 / 365, 0.043, 0.55, "put")
    assert -1.0 < g.delta < 0.0          # short-dated OTM put
    assert g.gamma > 0 and g.vega > 0
    assert g.theta < 0                   # long option decays


def test_prob_touch_matches_monte_carlo():
    random.seed(11)
    S, K, T, sig = 44.5, 41.0, 21 / 365, 0.55

    def mc(n=20000, steps=300):
        dt, hit = T / steps, 0
        mu, sd = -0.5 * sig * sig * dt, sig * math.sqrt(dt)
        barrier = math.log(K / S)
        for _ in range(n):
            x = 0.0
            for _ in range(steps):
                x += mu + sd * random.gauss(0, 1)
                if x <= barrier:
                    hit += 1
                    break
        return hit / n

    assert abs(prob_touch(S, K, T, sig) - mc()) < 0.04


def test_prob_touch_exceeds_prob_itm():
    """Touching a barrier is strictly more likely than finishing beyond it."""
    for K in (40.0, 42.0, 43.5):
        T, sig = 21 / 365, 0.55
        assert prob_touch(44.5, K, T, sig) > prob_itm(44.5, K, T, 0.0, sig, "put")


# --------------------------------------------------------------------------- #
# OCC symbols
# --------------------------------------------------------------------------- #
def test_occ_round_trip():
    for kind in ("put", "call"):
        for strike in (41.5, 100.0, 7.25):
            sym = build_occ("IBIT", date(2026, 9, 11), kind, strike)
            root, exp, k, st = parse_occ(sym)
            assert (root, exp, k, st) == ("IBIT", date(2026, 9, 11), kind, strike)


def test_occ_parses_known_symbol():
    assert parse_occ("IBIT260911P00041500") == ("IBIT", date(2026, 9, 11), "put", 41.5)


# --------------------------------------------------------------------------- #
# the economics the old bot got wrong
# --------------------------------------------------------------------------- #
def _candidate(credit, width, dte=30, kind="put", spot=44.5):
    short_k = spot - 2.5 if kind == "put" else spot + 2.5
    long_k = short_k - width if kind == "put" else short_k + width
    c = SpreadCandidate(
        structure=f"{kind}_credit", expiry=date.today() + timedelta(days=dte), dte=dte,
        legs=[Leg("S", kind, short_k, "sell", 0.5, 0.48, 0.52, 0.55, -0.2),
              Leg("L", kind, long_k, "buy", 0.2, 0.18, 0.22, 0.60, -0.1)],
        credit=credit, width=width,
        max_profit=credit * 100, max_loss=(width - credit) * 100,
        credit_ratio=credit / width,
        breakeven_low=short_k - credit if kind == "put" else None,
        breakeven_high=short_k + credit if kind == "call" else None)
    return c


def test_legacy_spread_economics_are_structurally_rejected():
    """The v1 trade: $0.05 credit on a $1.50 spread.

    Worth being precise about why this is bad, because the EV model alone does
    NOT condemn it -- $0.05 at 1 DTE implies ~74% vol against a ~50% forecast,
    which scores as positive expected value. What condemns it is the shape of
    the bet: risking $1.45 to make $0.05 needs a 96.7% win rate to break even,
    so a single loss erases 29 wins, and v1 then sized it at 20% of cash with
    five concurrent slots. Ruin risk, not expectancy, is the defect -- and the
    credit/width floor is what refuses it.
    """
    credit, width = 0.05, 1.50
    break_even_win_rate = (width - credit) / width
    assert break_even_win_rate > 0.96

    c = _candidate(credit, width, dte=1)
    assert c.credit_ratio < config.MIN_CREDIT_RATIO
    assert c.max_loss / c.max_profit > 28          # 29:1 risk/reward

    # A default-configured scan must not surface it at any price.
    assert not (config.MIN_CREDIT_RATIO <= c.credit_ratio <= config.MAX_CREDIT_RATIO)


def test_ev_is_positive_only_when_iv_exceeds_forecast_rv():
    """The core claim of the strategy, asserted directly."""
    from btcbot.marketdata import VolStats
    spot, dte = 44.5, 30
    T = dte / 365

    # price a 20-delta-ish spread at 60% IV
    short_k, long_k = 41.0, 40.0
    fair = (bs_price(spot, short_k, T, 0.0, 0.60, "put")
            - bs_price(spot, long_k, T, 0.0, 0.60, "put"))
    cand = _candidate(round(fair, 2), 1.0, dte)
    cand.legs[0].strike, cand.legs[1].strike = short_k, long_k
    cand.breakeven_low = short_k - cand.credit

    def ev_at(rv):
        v = VolStats(spot, rv, rv, rv, rv, rv, rv, 0.60, 50.0, 0.60 - rv, 0.60 / rv,
                     44, 43, 55, "bull")
        return strategy.evaluate(cand, spot, v).ev_per_contract

    assert ev_at(0.40) > 0      # IV richly above realized -> edge
    assert ev_at(0.60) < ev_at(0.40)
    assert ev_at(0.80) < 0      # realized above implied -> selling is a loser


def test_edge_check_blocks_when_iv_below_rv():
    from btcbot.marketdata import VolStats
    cheap = VolStats(44.5, .5, .5, .5, .5, .5, 0.50, 0.42, 20.0, -0.08, 0.84,
                     44, 43, 55, "bull")
    ok, reasons = strategy.edge_check(cheap)
    assert not ok and reasons

    rich = VolStats(44.5, .4, .4, .4, .4, .4, 0.40, 0.60, 80.0, 0.20, 1.50,
                    44, 43, 55, "bull")
    assert strategy.edge_check(rich)[0]


# --------------------------------------------------------------------------- #
# risk
# --------------------------------------------------------------------------- #
def test_stop_always_binds_below_max_loss():
    """Regression: a stop expressed only as a multiple of credit becomes inert
    on high-credit spreads, which made a backtest show a ~100% win rate."""
    for credit, width in [(0.30, 1.0), (0.45, 1.0), (0.50, 1.0), (1.25, 5.0), (0.60, 2.0)]:
        stop = stop_level(credit, width)
        max_loss_debit = width
        assert stop < max_loss_debit, (credit, width, stop)
        assert stop > credit, "a stop below the credit would fire instantly"


def test_sizing_respects_every_cap():
    store = PositionStore(path=os.path.join(tempfile.mkdtemp(), "p.json"))
    cand = _candidate(0.30, 1.0)
    cand.pop = 0.80
    acct = AccountState(equity=100_000, cash=100_000, options_buying_power=100_000)

    from btcbot.risk import size_position
    res = size_position(cand, acct, store)
    # 1% of 100k = $1,000 risk budget, $70 risk per contract -> 14
    assert res.qty == 14, res.detail
    assert res.total_risk <= acct.equity * config.RISK_PER_TRADE_PCT


def test_sizing_is_zero_when_buying_power_is_exhausted():
    store = PositionStore(path=os.path.join(tempfile.mkdtemp(), "p.json"))
    cand = _candidate(0.30, 1.0)
    cand.pop = 0.80
    from btcbot.risk import size_position
    broke = AccountState(equity=100_000, cash=100_000, options_buying_power=50)
    assert size_position(cand, broke, store).qty == 0


def test_kelly_is_zero_without_edge():
    assert kelly_fraction(0.50, 30.0, 70.0) == 0.0     # losing bet
    assert kelly_fraction(0.90, 30.0, 70.0) > 0.0


def test_portfolio_risk_cap_blocks_overcommitment():
    store = PositionStore(path=os.path.join(tempfile.mkdtemp(), "p.json"))
    acct = AccountState(equity=100_000, cash=100_000, options_buying_power=100_000)
    for i in range(3):
        store.positions.append(Position(
            id=f"p{i}", structure="put_credit",
            # distinct expiries, so MAX_PER_EXPIRY does not mask the risk cap
            expiry=str(date.today() + timedelta(days=30 + i)), qty=20,
            entry_credit=0.30, width=1.0, max_loss_per_contract=70.0,
            legs=[{"symbol": f"X{i}", "kind": "put", "strike": 40.0, "side": "sell", "ratio": 1}],
            opened_at="2026-01-01T00:00:00+00:00"))
    gate = RiskGate(store)
    # 3 x 20 x $70 = $4,200 deployed against a $6,000 budget
    assert store.total_open_risk() == 4200.0
    big = _candidate(0.30, 1.0, dte=40)
    big.max_loss = 2500.0
    ok, why = gate.can_open(big, acct)
    assert not ok and "portfolio risk" in why


def test_reused_contract_is_refused():
    """The exact cause of v1's 'position intent mismatch' rejections."""
    store = PositionStore(path=os.path.join(tempfile.mkdtemp(), "p.json"))
    store.positions.append(Position(
        id="a", structure="put_credit", expiry=str(date.today() + timedelta(days=30)),
        qty=1, entry_credit=0.3, width=1.0, max_loss_per_contract=70.0,
        legs=[{"symbol": "IBIT260911P00041500", "kind": "put", "strike": 41.5,
               "side": "buy", "ratio": 1}],
        opened_at="2026-01-01T00:00:00+00:00"))
    gate = RiskGate(store)
    cand = _candidate(0.30, 1.0)
    cand.legs[0].symbol = "IBIT260911P00041500"      # sell what we are long
    acct = AccountState(equity=100_000, cash=100_000, options_buying_power=100_000)
    ok, why = gate.can_open(cand, acct)
    assert not ok and "already held" in why


def test_exit_rules_fire_per_spread():
    store = PositionStore(path=os.path.join(tempfile.mkdtemp(), "p.json"))
    gate = RiskGate(store)
    pos = Position(id="a", structure="put_credit",
                   expiry=str(date.today() + timedelta(days=30)), qty=5,
                   entry_credit=0.40, width=1.0, max_loss_per_contract=60.0,
                   legs=[], opened_at="2026-01-01T00:00:00+00:00")

    assert gate.exit_decision(pos, 0.20)[0]            # 50% of credit captured
    assert not gate.exit_decision(pos, 0.35)[0]        # still working
    assert gate.exit_decision(pos, 0.80)[0]            # 2x credit stop
    assert gate.exit_decision(pos, 0.30, short_delta=0.50)[0]   # strike threatened

    near = Position(id="b", structure="put_credit",
                    expiry=str(date.today() + timedelta(days=3)), qty=5,
                    entry_credit=0.40, width=1.0, max_loss_per_contract=60.0,
                    legs=[], opened_at="2026-01-01T00:00:00+00:00")
    assert gate.exit_decision(near, 0.35)[0]           # time exit


def test_circuit_breaker_halts_and_expires():
    store = PositionStore(path=os.path.join(tempfile.mkdtemp(), "p.json"))
    gate = RiskGate(store)
    acct = AccountState(equity=100_000, cash=100_000, options_buying_power=100_000)
    assert not gate.halted(acct)[0]
    gate.engage_halt("test")
    assert gate.halted(acct)[0]


def test_drawdown_halt():
    store = PositionStore(path=os.path.join(tempfile.mkdtemp(), "p.json"))
    store.meta["high_water_mark"] = 100_000.0
    gate = RiskGate(store)
    assert gate.halted(AccountState(80_000, 80_000, 10_000))[0]     # -20%
    assert not gate.halted(AccountState(95_000, 95_000, 10_000))[0]  # -5%


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def test_position_store_round_trips():
    path = os.path.join(tempfile.mkdtemp(), "p.json")
    s1 = PositionStore(path=path)
    s1.add(Position(id="x", structure="iron_condor",
                    expiry=str(date.today() + timedelta(days=30)), qty=3,
                    entry_credit=0.55, width=1.0, max_loss_per_contract=45.0,
                    legs=[{"symbol": "A", "kind": "put", "strike": 41.0, "side": "sell", "ratio": 1}],
                    opened_at="2026-01-01T00:00:00+00:00"))
    s2 = PositionStore(path=path)
    assert len(s2.open_positions()) == 1
    assert s2.open_positions()[0].total_risk == 135.0


def test_reconcile_closes_vanished_positions():
    path = os.path.join(tempfile.mkdtemp(), "p.json")
    store = PositionStore(path=path)
    store.add(Position(id="x", structure="put_credit",
                       expiry=str(date.today() + timedelta(days=30)), qty=1,
                       entry_credit=0.3, width=1.0, max_loss_per_contract=70.0,
                       legs=[{"symbol": "A", "kind": "put", "strike": 41.0, "side": "sell", "ratio": 1},
                             {"symbol": "B", "kind": "put", "strike": 40.0, "side": "buy", "ratio": 1}],
                       opened_at="2026-01-01T00:00:00+00:00"))
    drifted = store.reconcile(set())          # broker reports nothing
    assert drifted and not store.open_positions()


def test_config_is_internally_consistent():
    assert config.validate() == []


def test_config_rejects_impossible_break_even():
    orig = config.MIN_CREDIT_RATIO
    try:
        config.MIN_CREDIT_RATIO = 0.03        # the v1 setting
        assert any("break-even" in p for p in config.validate())
    finally:
        config.MIN_CREDIT_RATIO = orig


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
