import sys
import time
import argparse
import logging
from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
from alpaca.trading.enums import OrderSide, OrderClass, TimeInForce, PositionIntent

import config
from options_strategy import OptionsStrategy
from risk_manager import RiskManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

class BitcoinOptionsBot:
    def __init__(self):
        self.trading_client = TradingClient(
            config.ALPACA_API_KEY, 
            config.ALPACA_SECRET_KEY, 
            paper=config.ALPACA_PAPER
        )
        self.strategy = OptionsStrategy()
        self.risk_manager = RiskManager()

    def print_status(self):
        """Displays account summary, market status, and active positions."""
        account = self.trading_client.get_account()
        clock = self.trading_client.get_clock()
        positions = self.risk_manager.get_ibit_option_positions()

        logging.info("=" * 65)
        logging.info("BITCOIN OPTIONS BOT - STATUS REPORT")
        logging.info("=" * 65)
        logging.info(f"Market Open         : {clock.is_open}")
        logging.info(f"Next Close          : {clock.next_close}")
        logging.info(f"Portfolio Value     : ${float(account.portfolio_value):,.2f}")
        logging.info(f"Available Cash      : ${float(account.cash):,.2f}")
        logging.info(f"20% Cash Allocation : ${float(account.cash) * config.CASH_ALLOCATION_PCT:,.2f}")
        logging.info(f"Active IBIT Legs    : {len(positions)}")

        if positions:
            logging.info("-" * 65)
            logging.info(f"{'Symbol':<24} {'Qty':<6} {'Market Val':<12} {'Unrealized P&L':<15}")
            logging.info("-" * 65)
            for p in positions:
                logging.info(f"{p.symbol:<24} {float(p.qty):<6.0f} ${float(p.market_value):<11.2f} ${float(p.unrealized_pl):<14.2f}")
        logging.info("=" * 65)

    def execute_spread_trade(self, spread: dict):
        """Places a defined-risk Bull Put Spread sized at 20% of cash."""
        qty = spread.get("qty", 1)
        logging.info("[*] Executing High-Frequency Bull Put Credit Spread Order...")
        logging.info(f"    - Quantity (20% Cash): {qty} contracts (Committed: ${spread['capital_committed']:,.2f})")
        logging.info(f"    - Short Leg (Sell)   : {spread['short_symbol']} @ Strike ${spread['short_strike']}")
        logging.info(f"    - Long Leg  (Buy)    : {spread['long_symbol']} @ Strike ${spread['long_strike']}")
        logging.info(f"    - Expiration         : {spread['expiration']} ({spread['dte']} DTE)")
        logging.info(f"    - Net Credit Target  : ${spread['net_credit']:.2f}/share (${spread['max_profit']:,.2f} total max gain)")
        logging.info(f"    - Max Risk           : ${spread['max_loss']:,.2f}")
        logging.info(f"    - Win Probability    : ~{spread['estimated_pop']}%")

        req = LimitOrderRequest(
            order_class=OrderClass.MLEG,
            qty=qty,
            time_in_force=TimeInForce.DAY,
            limit_price=spread['net_credit'],
            legs=[
                OptionLegRequest(
                    symbol=spread['short_symbol'],
                    ratio_qty=1,
                    side=OrderSide.SELL,
                    position_intent=PositionIntent.SELL_TO_OPEN
                ),
                OptionLegRequest(
                    symbol=spread['long_symbol'],
                    ratio_qty=1,
                    side=OrderSide.BUY,
                    position_intent=PositionIntent.BUY_TO_OPEN
                )
            ]
        )

        try:
            order = self.trading_client.submit_order(req)
            logging.info(f"[+] Spread order submitted successfully! Order ID: {order.id}, Status: {order.status}")
            return order
        except Exception as e:
            logging.error(f"[-] Order submission failed: {e}")
            return None

    def run_cycle(self):
        """Executes one scan & position management cycle."""
        clock = self.trading_client.get_clock()
        if not clock.is_open:
            logging.info(f"[*] US Market is currently CLOSED. Next open: {clock.next_open}")
            return

        logging.info("[*] Cycle Start: Checking open positions and risk thresholds...")
        self.risk_manager.evaluate_and_manage_positions()

        if not self.risk_manager.can_open_new_position():
            logging.info(f"[*] Max positions reached ({config.MAX_OPEN_POSITIONS} active). Holding.")
            return

        account = self.trading_client.get_account()
        cash = float(account.cash)
        excluded = self.risk_manager.get_active_short_symbols()

        logging.info(f"[*] Scanning near-term market for setups (Cash: ${cash:,.2f} | 20% Target: ${cash * config.CASH_ALLOCATION_PCT:,.2f})...")
        spread = self.strategy.find_bull_put_spread(available_cash=cash, excluded_symbols=excluded)

        if not spread or not spread.get("is_valid"):
            logging.info("[-] No qualifying high-probability spread found in this cycle.")
            return

        logging.info(f"[+] High-probability trade found (POP: {spread['estimated_pop']}%, Qty: {spread['qty']} contracts).")
        self.execute_spread_trade(spread)

    def run_loop(self):
        """Runs the bot continuously in high-frequency monitoring mode."""
        logging.info("[*] Starting Bitcoin Options High-Frequency Bot daemon loop...")
        logging.info(f"[*] Dynamic Sizing : {config.CASH_ALLOCATION_PCT*100:.0f}% of available cash per trade")
        logging.info(f"[*] Polling Speed  : every {config.LOOP_INTERVAL_SECONDS} seconds.")
        logging.info(f"[*] Max Spreads    : up to {config.MAX_OPEN_POSITIONS} concurrent active spreads.")
        
        while True:
            try:
                self.run_cycle()
            except Exception as e:
                logging.error(f"[-] Unexpected error in cycle: {e}")
            
            time.sleep(config.LOOP_INTERVAL_SECONDS)

def main():
    parser = argparse.ArgumentParser(description="Bitcoin Options Autonomous Trading Bot")
    parser.add_argument("--status", action="store_true", help="Print account status and open positions")
    parser.add_argument("--scan", action="store_true", help="Scan for candidate spreads without placing orders")
    parser.add_argument("--loop", action="store_true", help="Run bot in continuous loop mode")
    parser.add_argument("--once", action="store_true", help="Run one evaluation cycle and exit")

    args = parser.parse_args()
    bot = BitcoinOptionsBot()

    if args.status:
        bot.print_status()
    elif args.scan:
        bot.print_status()
        account = bot.trading_client.get_account()
        spread = bot.strategy.find_bull_put_spread(available_cash=float(account.cash))
        if spread:
            import json
            print(json.dumps(spread, indent=2))
    elif args.loop:
        bot.run_loop()
    else:
        # Default action: run once
        bot.print_status()
        bot.run_cycle()

if __name__ == "__main__":
    main()
