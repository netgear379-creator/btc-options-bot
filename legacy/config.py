import os
from dotenv import load_dotenv

load_dotenv()

# Alpaca API Credentials
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "True").lower() == "true"

# Strategy Parameters
UNDERLYING_SYMBOL = "IBIT"         # iShares Bitcoin Trust ETF
MIN_DTE = 1                        # Near-term expiration (1 DTE for fast intraday/weekly theta decay)
MAX_DTE = 10                       # Maximum days to expiration (short-dated weeklies for higher turnover)
OTM_PERCENT_TARGET = 0.04          # Target ~4-6% OTM (~0.12-0.18 Delta for high win-rate)
SPREAD_WIDTH = 1.0                 # Target spread width ($1.00 - $1.50)
MIN_NET_CREDIT = 0.04              # Minimum net credit per share ($4 per contract)

# Dynamic Position Sizing (20% of Available Cash per Trade)
CASH_ALLOCATION_PCT = 0.20         # Allocate 20% of available cash to each trade

# Risk Management & High-Frequency Rules
PROFIT_TARGET_PCT = 0.35           # Take profit at 35% max profit for rapid turnover
STOP_LOSS_MULTIPLIER = 1.5         # Stop loss at 1.5x credit to protect capital on fast moves
MAX_OPEN_POSITIONS = 5             # Allow up to 5 concurrent active spreads

# Check interval (seconds)
LOOP_INTERVAL_SECONDS = 30         # Fast scan every 30 seconds during market hours
