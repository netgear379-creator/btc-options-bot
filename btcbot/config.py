"""Configuration for the IBIT (spot-Bitcoin ETF) options bot.

Every number here is a risk decision. The defaults are deliberately
conservative; `python -m btcbot.backtest --help` shows how to sweep them.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "True").lower() == "true"

# Hard safety interlock: live (real-money) trading requires BOTH
# ALPACA_PAPER=False *and* ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK.
ALLOW_LIVE = os.getenv("ALLOW_LIVE_TRADING", "") == "I_UNDERSTAND_THE_RISK"

# --------------------------------------------------------------------------- #
# instrument
# --------------------------------------------------------------------------- #
UNDERLYING = os.getenv("UNDERLYING", "IBIT")
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.043"))
DIVIDEND_YIELD = 0.0                  # IBIT pays none

DATA_DIR = os.getenv("BTCBOT_DATA_DIR", "data")
IV_HISTORY_PATH = os.path.join(DATA_DIR, "iv_history.csv")
STATE_PATH = os.path.join(DATA_DIR, "positions.json")
TRADE_LOG_PATH = os.path.join(DATA_DIR, "trades.csv")

# --------------------------------------------------------------------------- #
# expiry selection
# --------------------------------------------------------------------------- #
# 0-2 DTE is where the old bot lived. Gamma there is brutal: the position can
# go from "95% safe" to max loss inside one candle, and the credit collected is
# too small to pay for that risk. 7-45 DTE is the premium-selling sweet spot.
MIN_DTE = int(os.getenv("MIN_DTE", "14"))
MAX_DTE = int(os.getenv("MAX_DTE", "45"))
TARGET_DTE = int(os.getenv("TARGET_DTE", "30"))
IV_REFERENCE_DTE = 30                 # tenor used for the ATM-IV / IV-rank series

# --------------------------------------------------------------------------- #
# EDGE FILTERS -- the reason to trade at all
# --------------------------------------------------------------------------- #
# A credit spread priced at fair value is a zero-EV bet before costs and a
# losing one after. The only durable edge in selling options is the variance
# risk premium: implied vol systematically exceeding subsequent realized vol.
# If that premium is not present, the correct number of trades is zero.
MIN_VRP_RATIO = float(os.getenv("MIN_VRP_RATIO", "1.10"))   # ATM IV >= 1.10x forecast RV
MIN_VRP_ABS = float(os.getenv("MIN_VRP_ABS", "0.03"))       # and >= 3 vol points richer
MIN_IV_RANK = float(os.getenv("MIN_IV_RANK", "25"))         # skip the bottom of the IV range
REQUIRE_IV_RANK = os.getenv("REQUIRE_IV_RANK", "False").lower() == "true"

# --------------------------------------------------------------------------- #
# structure selection
# --------------------------------------------------------------------------- #
SHORT_DELTA_TARGET = float(os.getenv("SHORT_DELTA_TARGET", "0.20"))
SHORT_DELTA_MIN = float(os.getenv("SHORT_DELTA_MIN", "0.10"))
SHORT_DELTA_MAX = float(os.getenv("SHORT_DELTA_MAX", "0.32"))

SPREAD_WIDTHS = [float(w) for w in os.getenv("SPREAD_WIDTHS", "1,2,2.5,5").split(",")]

# The single most important number in the file. The old bot sold $1.50-wide
# spreads for $0.05 -- risking $1.45 to make $0.05, which needs a 96.7% win
# rate merely to break even. Requiring credit >= 22% of width caps the
# risk/reward at ~3.5:1 and puts the break-even win rate near 78%, which a
# ~20-delta short strike can actually clear.
MIN_CREDIT_RATIO = float(os.getenv("MIN_CREDIT_RATIO", "0.22"))
MAX_CREDIT_RATIO = float(os.getenv("MAX_CREDIT_RATIO", "0.50"))   # above this it is not really OTM
MIN_NET_CREDIT = float(os.getenv("MIN_NET_CREDIT", "0.10"))       # absolute floor, per share

# Directional gate: never sell puts into a downtrend or calls into a melt-up.
USE_REGIME_FILTER = os.getenv("USE_REGIME_FILTER", "True").lower() == "true"
TREND_FAST = 20
TREND_SLOW = 50
RSI_OVERBOUGHT = 75.0
RSI_OVERSOLD = 25.0

ALLOW_PUT_SPREADS = True
ALLOW_CALL_SPREADS = os.getenv("ALLOW_CALL_SPREADS", "True").lower() == "true"
ALLOW_IRON_CONDORS = os.getenv("ALLOW_IRON_CONDORS", "True").lower() == "true"

# --------------------------------------------------------------------------- #
# liquidity -- slippage is the silent account-killer
# --------------------------------------------------------------------------- #
MAX_LEG_SPREAD_PCT = float(os.getenv("MAX_LEG_SPREAD_PCT", "0.25"))   # (ask-bid)/mid
MAX_LEG_SPREAD_ABS = float(os.getenv("MAX_LEG_SPREAD_ABS", "0.15"))   # or <= 15c wide
MIN_OPEN_INTEREST = float(os.getenv("MIN_OPEN_INTEREST", "50"))
MIN_QUOTE_SIZE = float(os.getenv("MIN_QUOTE_SIZE", "5"))

# --------------------------------------------------------------------------- #
# expected value -- every candidate must clear this after costs
# --------------------------------------------------------------------------- #
COMMISSION_PER_CONTRACT = float(os.getenv("COMMISSION_PER_CONTRACT", "0.05"))  # regulatory fees
SLIPPAGE_PER_LEG = float(os.getenv("SLIPPAGE_PER_LEG", "0.02"))  # per share, vs mid
MIN_EV_PER_CONTRACT = float(os.getenv("MIN_EV_PER_CONTRACT", "3.0"))   # dollars
MIN_EV_ON_RISK = float(os.getenv("MIN_EV_ON_RISK", "0.02"))            # EV / max-loss

# --------------------------------------------------------------------------- #
# position sizing -- risk-based, not "20% of cash"
# --------------------------------------------------------------------------- #
# The old bot committed 20% of cash per trade and allowed 5 of them: 100% of
# the account at risk, and it did open a 400-lot. Here, sizing is driven by
# the loss we are willing to take, and the portfolio total is capped too.
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "0.01"))   # 1% of equity at risk
MAX_PORTFOLIO_RISK_PCT = float(os.getenv("MAX_PORTFOLIO_RISK_PCT", "0.06"))
MAX_CONTRACTS_PER_TRADE = int(os.getenv("MAX_CONTRACTS_PER_TRADE", "25"))
MAX_BP_UTILISATION = float(os.getenv("MAX_BP_UTILISATION", "0.50"))   # of options buying power
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))           # quarter-Kelly cap

MAX_OPEN_SPREADS = int(os.getenv("MAX_OPEN_SPREADS", "4"))
MAX_PER_EXPIRY = int(os.getenv("MAX_PER_EXPIRY", "2"))
MAX_NEW_TRADES_PER_DAY = int(os.getenv("MAX_NEW_TRADES_PER_DAY", "2"))

# --------------------------------------------------------------------------- #
# exits
# --------------------------------------------------------------------------- #
PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "0.50"))   # buy back at 50% of credit
STOP_LOSS_MULT = float(os.getenv("STOP_LOSS_MULT", "2.0"))          # exit if debit = 2x credit
# A stop expressed only as a multiple of credit silently disables itself on
# high-credit spreads: at a 0.33 credit ratio, "3x credit" is a $0.99 debit on
# a $1 wide spread, i.e. max loss. A backtest then shows a ~100% win rate
# because every loser is held to recovery -- right up until one does not
# recover. The stop is therefore also capped as a fraction of width, so it
# always has teeth.
STOP_MAX_DEBIT_FRAC = float(os.getenv("STOP_MAX_DEBIT_FRAC", "0.75"))
CLOSE_AT_DTE = int(os.getenv("CLOSE_AT_DTE", "7"))                  # flatten before gamma week
SHORT_DELTA_EXIT = float(os.getenv("SHORT_DELTA_EXIT", "0.45"))     # strike under threat
TRAIL_AFTER_PCT = float(os.getenv("TRAIL_AFTER_PCT", "0.35"))       # lock in once this much is banked
TRAIL_GIVEBACK_PCT = float(os.getenv("TRAIL_GIVEBACK_PCT", "0.20")) # ... then keep >=  peak-20%

# --------------------------------------------------------------------------- #
# circuit breakers
# --------------------------------------------------------------------------- #
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.03"))
MAX_DRAWDOWN_HALT_PCT = float(os.getenv("MAX_DRAWDOWN_HALT_PCT", "0.15"))
CONSECUTIVE_LOSS_HALT = int(os.getenv("CONSECUTIVE_LOSS_HALT", "4"))
HALT_COOLDOWN_HOURS = float(os.getenv("HALT_COOLDOWN_HOURS", "24"))

# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "60"))
ENTRY_PRICE_MODE = os.getenv("ENTRY_PRICE_MODE", "mid_minus")  # mid_minus | mid | natural
ENTRY_PRICE_PAD = float(os.getenv("ENTRY_PRICE_PAD", "0.02"))  # give up this much vs mid
ORDER_FILL_TIMEOUT_S = int(os.getenv("ORDER_FILL_TIMEOUT_S", "90"))
ORDER_TICK = 0.01

# Do not open new risk in the first/last minutes of the session: the opening
# auction and the closing imbalance both produce quotes you cannot trust.
NO_ENTRY_FIRST_MINUTES = int(os.getenv("NO_ENTRY_FIRST_MINUTES", "15"))
NO_ENTRY_LAST_MINUTES = int(os.getenv("NO_ENTRY_LAST_MINUTES", "20"))

# --------------------------------------------------------------------------- #
# IV-rank series
# --------------------------------------------------------------------------- #
IV_RANK_LOOKBACK_DAYS = int(os.getenv("IV_RANK_LOOKBACK_DAYS", "365"))
IV_RANK_MIN_SAMPLES = int(os.getenv("IV_RANK_MIN_SAMPLES", "40"))


def validate() -> list:
    """Returns a list of configuration problems; empty means sane."""
    problems = []
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        problems.append("ALPACA_API_KEY / ALPACA_SECRET_KEY are not set (.env)")
    if MIN_CREDIT_RATIO < 0.15:
        problems.append(
            f"MIN_CREDIT_RATIO={MIN_CREDIT_RATIO} implies a break-even win rate of "
            f"{(1 - MIN_CREDIT_RATIO) * 100:.0f}%, which is not realistically achievable")
    if RISK_PER_TRADE_PCT * MAX_OPEN_SPREADS > MAX_PORTFOLIO_RISK_PCT + 1e-9:
        problems.append(
            f"RISK_PER_TRADE_PCT x MAX_OPEN_SPREADS "
            f"({RISK_PER_TRADE_PCT * MAX_OPEN_SPREADS:.1%}) exceeds "
            f"MAX_PORTFOLIO_RISK_PCT ({MAX_PORTFOLIO_RISK_PCT:.1%})")
    inert_at = 1.0 / MAX_CREDIT_RATIO
    if STOP_LOSS_MULT >= inert_at and STOP_MAX_DEBIT_FRAC >= 1.0:
        problems.append(
            f"STOP_LOSS_MULT={STOP_LOSS_MULT} is >= 1/MAX_CREDIT_RATIO ({inert_at:.2f}): "
            f"the stop would sit at or beyond max loss and never bind")
    if MIN_DTE < 3:
        problems.append(f"MIN_DTE={MIN_DTE}: sub-3-DTE short gamma is not a strategy, it is a coin flip")
    if MIN_DTE <= CLOSE_AT_DTE:
        problems.append(
            f"MIN_DTE={MIN_DTE} <= CLOSE_AT_DTE={CLOSE_AT_DTE}: a trade entered at the minimum "
            f"tenor would be closed for time on the next cycle, paying both spreads for nothing")
    if not ALPACA_PAPER and not ALLOW_LIVE:
        problems.append("ALPACA_PAPER=False but ALLOW_LIVE_TRADING is not set -- refusing to trade live")
    return problems
