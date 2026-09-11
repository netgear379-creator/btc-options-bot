import os
import sys
from dotenv import load_dotenv

# Load credentials from .env
load_dotenv()

api_key = os.getenv("ALPACA_API_KEY")
secret_key = os.getenv("ALPACA_SECRET_KEY")
paper = os.getenv("ALPACA_PAPER", "True").lower() == "true"

if not api_key or api_key == "YOUR_API_KEY_HERE":
    print("[-] Error: ALPACA_API_KEY is not set in .env file.")
    print("    Please edit .env and insert your actual Paper API Key and Secret.")
    sys.exit(1)

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOptionContractsRequest
except ImportError:
    print("[-] alpaca-py is still installing or not installed.")
    sys.exit(1)

print("[*] Connecting to Alpaca Paper Trading API...")
try:
    trading_client = TradingClient(api_key, secret_key, paper=paper)
    account = trading_client.get_account()
    
    print("\n[+] SUCCESS: Connected to Alpaca Paper Account!")
    print(f"    Account ID     : {account.id}")
    print(f"    Status         : {account.status}")
    print(f"    Cash           : ${float(account.cash):,.2f}")
    print(f"    Portfolio Value: ${float(account.portfolio_value):,.2f}")
    print(f"    Buying Power   : ${float(account.buying_power):,.2f}")
    
    print("\n[*] Querying live IBIT (Bitcoin ETF) options contracts...")
    req = GetOptionContractsRequest(
        underlying_symbols=["IBIT"],
        status="active",
        limit=10
    )
    res = trading_client.get_option_contracts(req)
    contracts = res.option_contracts if hasattr(res, 'option_contracts') else res
    
    print(f"[+] Found {len(contracts)} sample active IBIT contracts:")
    for c in contracts[:5]:
        print(f"    - Symbol: {c.symbol:<22} Type: {c.type:<4} Strike: ${float(c.strike_price):<6.2f} Expiration: {c.expiration_date}")
        
    print("\n[+] All connection and options data checks passed!")

except Exception as e:
    print(f"\n[-] Connection or API error: {e}")
    sys.exit(1)
