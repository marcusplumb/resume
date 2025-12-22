import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

# -----------------------------------------
# Config / paths
# -----------------------------------------

API_KEY = "Y0X9MB5C8T3VW67R"  # your Alpha Vantage key

ROOT = Path(__file__).resolve().parent.parent  # repo root
CONFIG_FILE = ROOT / "portfolio_config.json"
PRICES_FILE = ROOT / "prices.json"
HISTORY_FILE = ROOT / "prices_history.json"

# Extra tickers to track *even if* they are not in the portfolio
# Used here so we get an index for beta (SPY) but don't treat it as a holding.
EXTRA_TICKERS = ["SPY"]


# -----------------------------------------
# Helpers
# -----------------------------------------

def fetch_price_cents(symbol: str) -> int:
    """
    Fetch latest price for symbol from Alpha Vantage, return integer cents.
    """
    url = (
        "https://www.alphavantage.co/query"
        f"?function=GLOBAL_QUOTE&symbol={symbol}&apikey={API_KEY}"
    )
    with urlopen(url) as resp:
        data = json.load(resp)

    # Handle common Alpha Vantage messages
    if "Note" in data:
        # Rate limit / usage note
        raise RuntimeError(f"Rate limit hit for {symbol}: {data['Note']}")
    if "Error Message" in data:
        # Invalid symbol or other API error
        raise RuntimeError(f"API error for {symbol}: {data['Error Message']}")

    quote = data.get("Global Quote", {})
    price_str = quote.get("05. price")
    if not price_str:
        raise RuntimeError(f"No price for {symbol}: {data}")

    price_float = float(price_str)
    return int(round(price_float * 100))  # cents


def load_history():
    """
    Load prices_history.json if it exists; otherwise start with empty structure.
    If the file is corrupted/empty, we reset it instead of crashing.
    """
    if not HISTORY_FILE.exists():
        return {"symbols": {}}

    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print("Warning: prices_history.json is invalid; starting fresh.")
        return {"symbols": {}}


def save_history(history):
    with HISTORY_FILE.open("w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
        f.write("\n")


def load_config():
    """
    Load portfolio_config.json (single source of truth for holdings / cash / transactions).
    """
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


# -----------------------------------------
# Main
# -----------------------------------------

def main():
    cfg = load_config()

    # Today as "YYYY-MM-DD"
    today = datetime.now(timezone.utc).date().isoformat()

    # Snapshot time in ISO 8601 with Z suffix
    now_iso = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    latest = {
        "updatedAt": now_iso,
        "symbols": {}
    }

    history = load_history()
    history_symbols = history.setdefault("symbols", {})

    # Collect all tickers that belong to the portfolio
    symbol_set = set()

    for pos in cfg.get("positions", []):
        t = pos.get("ticker")
        if t:
            symbol_set.add(t)

    for tx in cfg.get("transactions", []):
        t = tx.get("ticker")
        if t:
            symbol_set.add(t)

    # Add SPY (or any other benchmarks you want) for beta calculations
    for extra in EXTRA_TICKERS:
        symbol_set.add(extra)

    symbols_to_track = sorted(symbol_set)

    print("Tracking symbols:", ", ".join(symbols_to_track))

    # Fetch prices and update both snapshot and history
    for i, symbol in enumerate(symbols_to_track):
        print(f"Fetching price for {symbol}...")
        try:
            cents = fetch_price_cents(symbol)
        except Exception as e:
            # If one symbol fails (rate limit, etc.), log it and move on
            print(f"  Error fetching {symbol}: {e}")
            continue

        # Update latest snapshot
        latest["symbols"][symbol] = {
            "priceCents": cents,
            "currency": "USD",  # you can make this smarter if you want
        }

        # Update history series for this symbol (no shares, just priceCents)
        series = history_symbols.setdefault(symbol, [])
        if series and series[-1].get("date") == today:
            # Overwrite today's price if already there
            series[-1]["priceCents"] = cents
        else:
            series.append({
                "date": today,
                "priceCents": cents
            })

        # Be nice to the free Alpha Vantage API (max 5 calls/minute, 25/day)
        if i < len(symbols_to_track) - 1:
            time.sleep(15)

    # Write snapshot
    with PRICES_FILE.open("w", encoding="utf-8") as f:
        json.dump(latest, f, indent=2)
        f.write("\n")

    # Write history
    save_history(history)

    print("Updated prices.json and prices_history.json")


if __name__ == "__main__":
    main()
