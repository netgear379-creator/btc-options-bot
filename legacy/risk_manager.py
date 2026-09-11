from alpaca.trading.client import TradingClient
import config

class RiskManager:
    def __init__(self):
        self.trading_client = TradingClient(
            config.ALPACA_API_KEY, 
            config.ALPACA_SECRET_KEY, 
            paper=config.ALPACA_PAPER
        )

    def get_ibit_option_positions(self):
        """Retrieves all currently open IBIT option positions."""
        positions = self.trading_client.get_all_positions()
        ibit_positions = [
            p for p in positions 
            if p.symbol.startswith(config.UNDERLYING_SYMBOL) and len(p.symbol) > len(config.UNDERLYING_SYMBOL)
        ]
        return ibit_positions

    def get_active_short_symbols(self):
        """Returns symbols of currently held short options to avoid duplicate entries."""
        positions = self.get_ibit_option_positions()
        return [p.symbol for p in positions if float(p.qty) < 0]

    def can_open_new_position(self) -> bool:
        """Determines if the account has room for another spread position."""
        positions = self.get_ibit_option_positions()
        # Count short legs as active spreads
        short_legs = [p for p in positions if float(p.qty) < 0]
        return len(short_legs) < config.MAX_OPEN_POSITIONS

    def evaluate_and_manage_positions(self):
        """
        High-Frequency Position Monitor:
        - Takes profit at +35% gain for fast turnover.
        - Cuts loss at -150% (1.5x credit) to protect capital.
        - Closes both legs when a target is hit.
        """
        positions = self.get_ibit_option_positions()
        if not positions:
            print("[*] Risk Manager: No active IBIT option positions open.")
            return

        total_unrealized_pl = sum(float(p.unrealized_pl) for p in positions)
        print(f"[*] Open IBIT Option Positions: {len(positions)} legs | Total Unrealized P&L: ${total_unrealized_pl:,.2f}")

        # Check short legs for exit triggers
        for p in positions:
            qty = float(p.qty)
            cost_basis = float(p.cost_basis)
            unrealized_pl = float(p.unrealized_pl)
            market_value = float(p.market_value)
            
            print(f"    - Leg: {p.symbol:<24} | Qty: {qty:<5.0f} | Value: ${market_value:<9.2f} | P&L: ${unrealized_pl:<8.2f}")

            if qty < 0 and cost_basis != 0:
                gain_pct = (unrealized_pl / abs(cost_basis))
                if gain_pct >= config.PROFIT_TARGET_PCT:
                    print(f"[!] TAKE PROFIT TRIGGERED: {p.symbol} gain is {gain_pct*100:.1f}%. Closing all positions to lock in profit...")
                    self.close_all_ibit_positions()
                    break
                elif gain_pct <= -config.STOP_LOSS_MULTIPLIER:
                    print(f"[!] STOP LOSS TRIGGERED: {p.symbol} loss is {gain_pct*100:.1f}%. Closing all positions...")
                    self.close_all_ibit_positions()
                    break

    def close_all_ibit_positions(self):
        """Safely closes all IBIT option positions to reset for the next trade."""
        positions = self.get_ibit_option_positions()
        for p in positions:
            try:
                order = self.trading_client.close_position(symbol_or_asset_id=p.symbol)
                print(f"[+] Submitted closing order for {p.symbol}. Order ID: {order.id}")
            except Exception as e:
                print(f"[-] Failed to close leg {p.symbol}: {e}")
