# IBIT Options Bot v2

A defined-risk options-selling bot on **IBIT** (the iShares spot-Bitcoin ETF),
with a real backtester attached.

---

## Read this first

**The v1 bot in `legacy/` was structurally guaranteed to lose money.** That is
not a stylistic judgement — it is measurable, and the backtester measures it.

Replaying v1's own parameters (1–10 DTE, 3% credit/width, 20% of cash per
trade, five concurrent slots) through the backtester on real IBIT option prices:

| Nov 2024 – Sep 2026 | v1 economics | v2 default |
|---|---|---|
| Total return | **-58.5%** | **+16.8%** |
| Max drawdown | **-69.1%** | **-4.3%** |
| Sharpe | -0.96 | **2.22** |
| Profit factor | 0.69 | **2.51** |
| Win rate | 56.1% | 78.1% |
| Expectancy / trade | -$372 | +$132 |

The core defect is arithmetic. v1 sold $1.50-wide credit spreads for **$0.05**.
That risks $1.45 to make $0.05, so it needs a **96.7% win rate just to break
even**. It logged those trades as "Win Probability ~74.7%", from this line:

```python
estimated_pop = min(95.0, round(50.0 + (distance_pct * 5.0), 1))   # legacy/options_strategy.py
```

That formula has no relationship to option pricing. It contains no volatility,
no time to expiry, and no market data. It was a number that looked like a
probability.

To be precise about the failure, since it is easy to get wrong: the defect is
**not** that each individual trade had negative expected value. A $0.05 credit
at 1 DTE implies roughly 74% volatility against a ~50% realized forecast, which
actually scores as *positive* EV. The defect is the shape of the bet. At 29:1
risk/reward a single loss erases 29 wins, and v1 then sized each one at 20% of
cash across five concurrent slots — so a normal cluster of losses becomes an
account-ending event rather than a bad week. **Ruin risk, not expectancy.**

v1 also never placed a single working trade in the session recorded in
`bot.log`. Every order failed with `insufficient options buying power` (it
sized against `cash`, which was 40x the real `options_buying_power`) or
`position intent mismatch` (it tried to sell-to-open a strike it was already
long), and it then retried the identical failing order every 30 seconds for
hours.

---

## What the edge actually is

There is exactly one honest reason to sell options, and it is worth being
blunt about it:

> Under the risk-neutral measure implied by option prices, **every** credit
> spread has an expected value of exactly zero before costs, and a negative one
> after. Searching for "high probability" trades cannot beat this — the market
> already charged you for the probability.

The only durable edge is the **variance risk premium**: implied volatility
tends to exceed the volatility that subsequently shows up. So the bot separates
the two things v1 conflated:

```
credit received      priced by the MARKET's implied vol
probability of loss  evaluated under OUR forecast realized vol

EV = credit - fair_value_at_forecast_vol - costs
```

When IV equals forecast RV, EV is zero and **nothing trades**. When IV is rich,
EV is positive and the size of that gap is the entire edge.

This is not theoretical. Run `python bot.py --scan` on a day when IV sits below
realized vol and the bot refuses to trade, reporting exactly why.

The backtest confirms the filter is load-bearing rather than decoration:

| | Sharpe | Profit factor | Max DD | Trades |
|---|---|---|---|---|
| With VRP edge filter | **2.22** | **2.51** | -4.3% | 128 |
| Filter removed | 1.20 | 1.38 | -6.4% | 285 |

Removing it more than doubles the trade count and halves the risk-adjusted
return. Most of the work this bot does is **declining to trade**.

### Walk-forward

Parameters were chosen on priors rather than fitted, but the sample is split
anyway. In-sample is Nov 2024 – Oct 2025; out-of-sample is Oct 2025 – Sep 2026.

| preset | IS return | IS Sharpe | **OOS return** | **OOS Sharpe** | OOS win% |
|---|---|---|---|---|---|
| default (2x stop) | 14.6% | 3.04 | **2.5%** | **1.32** | 82.8% |
| no regime filter | 17.0% | 3.41 | **3.7%** | **1.88** | 82.4% |
| 3x stop (width-capped) | 14.4% | 2.92 | **3.3%** | **1.70** | 86.2% |
| conservative (10-20 delta) | 4.0% | 2.81 | **0.5%** | **1.79** | 100% (n=3) |
| aggressive (2%/trade) | 23.0% | 2.91 | **4.8%** | **1.35** | 82.8% |
| 3x slippage stress | 6.9% | 2.24 | **0.3%** | **0.27** | 80.0% |

Two things to take from this. Every preset stays positive out-of-sample, so the
result is not knife-edge on parameters. But **returns are materially lower
out-of-sample** — the variance risk premium in IBIT compressed through 2026, and
the bot responded by trading less rather than by losing. The last row is the
real risk boundary: at 3x the modelled slippage the edge is essentially gone.
Execution quality is not a detail here, it is the margin.

### One finding worth stating separately

An earlier version of this backtest reported a **100% win rate** on a "tuned"
preset with a 3x stop. That was not a good result, it was a broken one. With a
$1-wide spread and $0.30 credit, a stop at "3x credit" sits at a $0.96 debit —
but max loss *is* $1.00. The stop had quietly disabled itself, so every loser
was held to recovery, and in a 22-month sample containing no terminal crash
they all recovered.

The fix is in `risk.stop_level()`: the stop is the tighter of a multiple of
credit **and** a fraction of spread width, so it always binds. An inert stop is
worse than no stop, because it looks like risk control in the config file.

---

## Install

```bash
pip install -r requirements.txt
```

Put credentials in `.env`:

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=True
```

Then:

```bash
python bot.py --doctor
```

---

## Use

```bash
python bot.py --doctor      # preflight: config, credentials, data, options level
python bot.py --status      # account, risk budget, vol regime, open spreads
python bot.py --scan        # rank candidates, place nothing
python bot.py --once        # one manage-then-maybe-enter cycle
python bot.py --loop        # run continuously
python bot.py --close-all   # flatten every tracked spread
```

`--dry-run` on any trading mode logs intended orders without sending them.
`--scan --show-all` bypasses the edge filter so you can see *why* it said no.

Backtest:

```bash
python -m btcbot.backtest --start 2024-11-25 --end 2026-09-10
```

The first run downloads ~213k real option bars (a few minutes) and caches them
under `data/cache/`. Every parameter is overridable by environment variable, so
sweeping is just:

```bash
STOP_LOSS_MULT=3.0 RISK_PER_TRADE_PCT=0.02 python -m btcbot.backtest
```

Tests:

```bash
python tests/test_core.py
```

---

## How it decides

1. **Is there edge?** ATM IV vs a blended realized-vol forecast (EWMA 0.94,
   HV20, Yang-Zhang 20, HV60). Needs `IV/RV >= 1.10` **and** `IV - RV >= 3` vol
   points. No edge means no trade, full stop.
2. **Which direction is allowed?** Trend (20/50 SMA) and RSI gate the
   structure. Put spreads are never sold into a downtrend.
3. **Which structure?** Put credit spreads, call credit spreads and iron
   condors at 14-45 DTE, short strike at 10-32 delta, credit >= 22% of width.
4. **Is it liquid?** Bid/ask <= 25% of mid (or <= $0.15), open interest >= 50,
   quote size >= 5. Slippage is the silent account-killer.
5. **Is EV positive after costs?** Fees, both-way slippage, and a stressed-vol
   check (EV must survive forecast vol x 1.15).
6. **How big?** The smallest of four independent caps — 1% of equity at risk,
   the remaining 6% portfolio risk budget, 50% of options buying power, and a
   quarter-Kelly ceiling.
7. **When to get out?** 50% of credit, or the stop, or 7 DTE, or short delta
   >= 0.45, or a trailing give-back — whichever fires first, evaluated **per
   spread**, not per leg.

---

## Layout

```
bot.py                  orchestrator / CLI
btcbot/config.py        every risk parameter, env-overridable, self-validating
btcbot/greeks.py        Black-Scholes, IV inversion, first-passage probability
btcbot/marketdata.py    chain, vol estimators, persisted IV-rank series
btcbot/strategy.py      candidate construction + EV scoring
btcbot/risk.py          sizing, portfolio caps, circuit breakers, stop level
btcbot/portfolio.py     spread-level state, atomic writes, broker reconciliation
btcbot/execution.py     order construction, intent inference, fill management
btcbot/backtest.py      historical replay on real option bars
tests/test_core.py      25 tests over pricing, sizing, exits and state
legacy/                 v1, kept for reference
```

`greeks.py` is dependency-free (`math.erf`, no scipy) and validated: IV
inversion round-trips to 1e-6, put-call parity residual is 0, the first-passage
barrier probability matches Monte Carlo to ~2%, and the solver returns `None`
rather than noise when vega is too small for vol to be identifiable.

---

## Safety

- Live trading requires **both** `ALPACA_PAPER=False` **and**
  `ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK`. Paper is the default.
- `config.validate()` refuses to start on incoherent settings — a credit/width
  ratio implying an impossible break-even win rate, per-trade risk x max
  positions exceeding the portfolio cap, a stop that cannot bind, or a minimum
  tenor below the time-exit threshold.
- Circuit breakers: 3% daily loss limit, 15% drawdown halt, 4 consecutive
  losses, each with a 24h cooldown.
- Orders that do not fill within 90s are cancelled, not left resting. An order
  rejected 3 times is abandoned for the session rather than retried forever.
- Position state is written atomically and reconciled against the broker every
  cycle; divergence is logged loudly rather than silently tolerated.

---

## What this backtest does *not* prove

Stated plainly, because the difference between a strategy and a story is
whether the limitations are disclosed:

- **The sample is 22 months and one broad regime.** IBIT options only began
  trading in November 2024. That is 128 trades. It is evidence, not proof.
- **Daily bars only.** Stops are evaluated at the next daily close, so an
  intraday stop fills at a different price than modelled. 98.2% of legs were
  marked from real printed bars; the remaining 1.8% were modelled.
- **Bid/ask is modelled**, calibrated from the live chain (median $0.02-0.03
  for the relevant strikes), because historical option NBBO is not available on
  this data plan.
- **Fills are assumed at the limit price.** Real fills are worse some fraction
  of the time, and the 3x-slippage row above shows how little headroom that
  leaves.
- **Edge is not constant.** The strategy earned +$13.3k in 2025 and roughly
  +$1.0k in 2026, because IV/RV compressed and the filter correctly stood the
  bot down. A premium-selling edge that has decayed shows up as *no trades*,
  not as losses — the intended failure mode. As of the last run IBIT ATM IV was
  *below* realized vol (IV/RV 0.92) and the bot declined to trade at all.
- **The tail is real.** Defined risk caps the loss per spread, not the
  correlation between spreads. A gap through both short strikes over one
  weekend hits every open position at once.

No configuration of this bot is both "very highly profitable" and "very
accurate" at once. Those two goals trade off directly and mechanically:
further-OTM strikes raise the win rate to 88% and cut the return to +4.5%;
closer strikes raise return and drawdown together; doubling risk per trade
doubles both return and drawdown at an unchanged Sharpe. The only thing that
improves both at once is the edge filter, and its ceiling is set by how rich
implied vol actually is — which is not something a bot controls.

`data/walkforward_summary.json` and `data/headline_comparison.json` hold the
full numbers so you can pick your point on that trade-off rather than be sold
one.
#   b t c - o p t i o n s - b o t  
 