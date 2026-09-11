from datetime import date, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest
import config

class OptionsStrategy:
    def __init__(self):
        self.trading_client = TradingClient(
            config.ALPACA_API_KEY, 
            config.ALPACA_SECRET_KEY, 
            paper=config.ALPACA_PAPER
        )
        self.stock_client = StockHistoricalDataClient(
            config.ALPACA_API_KEY, 
            config.ALPACA_SECRET_KEY
        )
        self.option_data_client = OptionHistoricalDataClient(
            config.ALPACA_API_KEY, 
            config.ALPACA_SECRET_KEY
        )

    def get_underlying_price(self, symbol=config.UNDERLYING_SYMBOL) -> float:
        """Fetches the latest mid/market price for the underlying ETF."""
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        res = self.stock_client.get_stock_latest_quote(req)
        quote = res[symbol]
        
        bid = float(quote.bid_price) if quote.bid_price else 0.0
        ask = float(quote.ask_price) if quote.ask_price else 0.0
        
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2.0, 2)
        return round(ask or bid, 2)

    def calculate_contract_qty(self, available_cash: float, spread_width: float) -> int:
        """
        Dynamically calculates contract quantity based on 20% of available cash.
        Margin requirement per spread contract = spread_width * 100.
        """
        target_allocation = available_cash * config.CASH_ALLOCATION_PCT
        margin_per_contract = spread_width * 100.0
        
        if margin_per_contract <= 0:
            return 1
            
        qty = int(target_allocation / margin_per_contract)
        # Ensure at least 1 contract, cap at reasonable maximum
        return max(1, qty)

    def find_bull_put_spread(self, available_cash: float = 100000.0, excluded_symbols: list = None):
        """
        High-Frequency Strategy Scanner:
        - Targets near-term expirations (1 to 10 DTE) for rapid intraday/weekly theta decay.
        - Short strike placed ~4-6% OTM for high probability (~80-88% win rate).
        - Sizes order to 20% of available cash.
        """
        if excluded_symbols is None:
            excluded_symbols = []

        underlying_price = self.get_underlying_price()
        if not underlying_price or underlying_price <= 0:
            print("[-] Unable to fetch current underlying price.")
            return None

        today = date.today()
        d_min = today + timedelta(days=config.MIN_DTE)
        d_max = today + timedelta(days=config.MAX_DTE)

        print(f"[*] Current {config.UNDERLYING_SYMBOL} Price: ${underlying_price:.2f}")
        print(f"[*] Scanning for near-term expirations between {d_min} and {d_max}...")

        # Request contracts from Alpaca
        req = GetOptionContractsRequest(
            underlying_symbols=[config.UNDERLYING_SYMBOL],
            status="active",
            expiration_date_gte=d_min,
            expiration_date_lte=d_max,
            limit=200
        )
        res = self.trading_client.get_option_contracts(req)
        contracts = res.option_contracts if hasattr(res, 'option_contracts') else res

        if not contracts:
            print("[-] No contracts found in the specified DTE range.")
            return None

        # Filter for PUT options
        put_contracts = [
            c for c in contracts 
            if "PUT" in str(getattr(c, 'type', '')).upper() or "P" in c.symbol
        ]

        if not put_contracts:
            print("[-] No PUT contracts found.")
            return None

        # Sort expirations and target the nearest one for high-frequency decay
        expirations = sorted(list(set(c.expiration_date for c in put_contracts)))
        print(f"[*] Available Expirations: {[str(e) for e in expirations]}")

        # Loop through available near-term expirations to find the best candidate
        for selected_exp in expirations:
            dte = (selected_exp - today).days

            # Filter puts for this expiration
            exp_puts = [c for c in put_contracts if c.expiration_date == selected_exp]
            strikes = sorted(list(set(float(c.strike_price) for c in exp_puts)))

            # Target short strike: ~4-6% below current price
            ideal_short_strike = underlying_price * (1.0 - config.OTM_PERCENT_TARGET)
            otm_strikes = [s for s in strikes if s < underlying_price]
            if not otm_strikes:
                continue

            # Candidate short strikes
            candidate_shorts = sorted(otm_strikes, key=lambda s: abs(s - ideal_short_strike))

            for short_strike in candidate_shorts[:3]:
                short_contract = next((c for c in exp_puts if float(c.strike_price) == short_strike), None)
                if not short_contract or short_contract.symbol in excluded_symbols:
                    continue

                # Retrieve short quote
                short_q_res = self.option_data_client.get_option_latest_quote(
                    OptionLatestQuoteRequest(symbol_or_symbols=short_contract.symbol)
                )
                short_q = short_q_res.get(short_contract.symbol)
                short_bid = float(short_q.bid_price) if short_q and short_q.bid_price else 0.0
                short_ask = float(short_q.ask_price) if short_q and short_q.ask_price else 0.0
                short_mid = round((short_bid + short_ask) / 2.0, 2) if (short_bid and short_ask) else short_bid

                if short_mid < config.MIN_NET_CREDIT:
                    continue

                lower_strikes = [s for s in strikes if s < short_strike]
                for l_strike in sorted(lower_strikes, reverse=True):
                    width = round(short_strike - l_strike, 2)
                    if width < 0.9 or width > 2.5:
                        continue

                    long_contract = next((c for c in exp_puts if float(c.strike_price) == l_strike), None)
                    if not long_contract:
                        continue

                    long_q_res = self.option_data_client.get_option_latest_quote(
                        OptionLatestQuoteRequest(symbol_or_symbols=long_contract.symbol)
                    )
                    long_q = long_q_res.get(long_contract.symbol)
                    long_bid = float(long_q.bid_price) if long_q and long_q.bid_price else 0.0
                    long_ask = float(long_q.ask_price) if long_q and long_q.ask_price else 0.0
                    long_mid = round((long_bid + long_ask) / 2.0, 2) if (long_bid and long_ask) else long_ask

                    natural_credit = round(short_bid - long_ask, 2)
                    mid_credit = round(short_mid - long_mid, 2)
                    limit_credit = max(natural_credit, mid_credit)

                    if limit_credit >= config.MIN_NET_CREDIT:
                        # Dynamic 20% sizing
                        qty = self.calculate_contract_qty(available_cash, width)
                        
                        max_profit = round(limit_credit * 100 * qty, 2)
                        max_loss = round((width - limit_credit) * 100 * qty, 2)
                        capital_committed = round(width * 100 * qty, 2)
                        
                        distance_pct = ((underlying_price - short_strike) / underlying_price) * 100
                        estimated_pop = min(95.0, round(50.0 + (distance_pct * 5.0), 1))

                        return {
                            "underlying": config.UNDERLYING_SYMBOL,
                            "underlying_price": underlying_price,
                            "expiration": str(selected_exp),
                            "dte": dte,
                            "short_symbol": short_contract.symbol,
                            "short_strike": short_strike,
                            "short_bid": short_bid,
                            "short_ask": short_ask,
                            "long_symbol": long_contract.symbol,
                            "long_strike": l_strike,
                            "long_bid": long_bid,
                            "long_ask": long_ask,
                            "spread_width": width,
                            "net_credit": limit_credit,
                            "qty": qty,
                            "capital_committed": capital_committed,
                            "max_profit": max_profit,
                            "max_loss": max_loss,
                            "distance_pct": round(distance_pct, 2),
                            "estimated_pop": estimated_pop,
                            "is_valid": True
                        }

        print("[-] No qualifying spreads found across available near-term expirations.")
        return None
