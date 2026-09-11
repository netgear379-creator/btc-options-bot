import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest

load_dotenv()
api_key = os.getenv("ALPACA_API_KEY")
secret_key = os.getenv("ALPACA_SECRET_KEY")
paper = os.getenv("ALPACA_PAPER", "True").lower() == "true"

stock_client = StockHistoricalDataClient(api_key, secret_key)
quote_req = StockLatestQuoteRequest(symbol_or_symbols="IBIT")
quote = stock_client.get_stock_latest_quote(quote_req)
ibit_quote = quote["IBIT"]
print(f"IBIT Current Quote -> Bid: ${ibit_quote.bid_price:.2f}, Ask: ${ibit_quote.ask_price:.2f}")

current_price = (ibit_quote.ask_price + ibit_quote.bid_price) / 2 if (ibit_quote.ask_price and ibit_quote.bid_price) else (ibit_quote.ask_price or ibit_quote.bid_price)
print(f"IBIT Mid Price: ${current_price:.2f}")

trading_client = TradingClient(api_key, secret_key, paper=paper)

# Let's check contracts expiring in 14-35 days
now = datetime.now()
start_exp = (now + timedelta(days=14)).strftime("%Y-%m-%d")
end_exp = (now + timedelta(days=35)).strftime("%Y-%m-%d")

print(f"\nFiltering IBIT PUT contracts expiring between {start_exp} and {end_exp}...")
req = GetOptionContractsRequest(
    underlying_symbols=["IBIT"],
    status="active",
    expiration_date_gte=start_exp,
    expiration_date_lte=end_exp,
    limit=50
)
res = trading_client.get_option_contracts(req)
contracts = res.option_contracts if hasattr(res, 'option_contracts') else res

puts = [c for c in contracts if "P" in c.symbol or (hasattr(c, 'type') and "PUT" in str(c.type).upper())]
print(f"Found {len(puts)} candidate PUT contracts:")
for p in sorted(puts, key=lambda x: (x.expiration_date, float(x.strike_price)))[:8]:
    print(f"  Symbol: {p.symbol:<22} Exp: {p.expiration_date} Strike: ${float(p.strike_price):.2f}")
