import json
import math
from datetime import date
from pathlib import Path

# ---- Settings ---------------------------------------------------------

MAX_ABS_WEIGHT = 0.15  # 15% max absolute weight per position

ROOT = Path(__file__).resolve().parent.parent  # repo root (where index.html lives)
CFG_PATH = ROOT / "portfolio_config.json"
PRICES_PATH = ROOT / "prices.json"


# ---- Helpers: load config & prices -----------------------------------

def load_config():
    if not CFG_PATH.exists():
        raise FileNotFoundError(f"portfolio_config.json not found at {CFG_PATH}")
    with CFG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with CFG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def load_prices():
    if not PRICES_PATH.exists():
        raise FileNotFoundError(f"prices.json not found at {PRICES_PATH}")
    with PRICES_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    symbols = data.get("symbols", {})
    price_cents = {}
    for ticker, info in symbols.items():
        pc = info.get("priceCents")
        if isinstance(pc, (int, float)):
            price_cents[ticker] = int(pc)
    return price_cents


# ---- Rebuild holdings & cash from transactions -----------------------

def compute_holdings_and_cash(cfg, as_of_date_str=None):
    """
    Rebuild net share counts and cash from startingCashCents + all transactions.
    If as_of_date_str is provided ('YYYY-MM-DD'), ignore any tx after that date.
    """
    transactions = cfg.get("transactions", [])
    cash = int(cfg.get("startingCashCents", 0))
    holdings = {}

    if as_of_date_str is None:
        cut_off = "9999-12-31"
    else:
        cut_off = as_of_date_str

    for tx in transactions:
        tx_date = tx.get("date")
        if tx_date and tx_date > cut_off:
            continue

        ttype = (tx.get("type") or "").upper()
        ticker = tx.get("ticker")
        shares = int(tx.get("shares", 0))
        price_cents = int(tx.get("priceCents", 0))

        if not ticker or shares <= 0 or price_cents < 0:
            continue

        amount = shares * price_cents

        if ttype == "BUY":
            # Buy increases shares, decreases cash
            holdings[ticker] = holdings.get(ticker, 0) + shares
            cash -= amount
        elif ttype == "SELL":
            # Sell decreases shares (can go short), increases cash
            holdings[ticker] = holdings.get(ticker, 0) - shares
            cash += amount
        else:
            # Unknown type – ignore
            continue

    return holdings, cash


# ---- Rebalance logic --------------------------------------------------

def compute_weights(holdings, cash_cents, live_prices):
    """
    Compute per-ticker weights and total NAV (in cents).
    Returns (weights_dict, total_nav_cents, positions_nav_cents)
    Weight is signed (negative for shorts). Absolute weight is used for constraint.
    """
    positions_nav_cents = 0
    for ticker, shares in holdings.items():
        price = live_prices.get(ticker)
        if price is None:
            continue
        positions_nav_cents += price * shares  # shares can be negative

    total_nav_cents = positions_nav_cents + cash_cents

    if total_nav_cents <= 0:
        raise RuntimeError(f"Total NAV is non-positive ({total_nav_cents}).")

    weights = {}
    for ticker, shares in holdings.items():
        price = live_prices.get(ticker)
        if price is None:
            continue
        pos_nav = price * shares
        weight = pos_nav / total_nav_cents
        weights[ticker] = weight

    return weights, total_nav_cents, positions_nav_cents


def build_rebalance_trades(holdings, cash_cents, live_prices, max_abs_weight):
    """
    Given current holdings, cash and live prices, build a list of trades
    that trim any position whose absolute weight exceeds max_abs_weight.
    Only trims – no new buys of underweight names.
    """
    trades = []

    # Compute current weights
    weights, total_nav_cents, _ = compute_weights(holdings, cash_cents, live_prices)

    for ticker, net_shares in holdings.items():
        if net_shares == 0:
            continue

        price = live_prices.get(ticker)
        if price is None:
            continue

        weight = weights.get(ticker, 0.0)
        abs_weight = abs(weight)

        if abs_weight <= max_abs_weight:
            continue  # within limit

        # Current position NAV
        pos_nav_cents = price * net_shares  # signed

        # Target NAV at boundary (preserving sign)
        target_nav_cents = math.copysign(int(max_abs_weight * total_nav_cents), pos_nav_cents)

        # Desired absolute shares at boundary
        desired_abs_shares = abs(target_nav_cents) / price

        # Floor so we don't overshoot
        desired_abs_shares = math.floor(desired_abs_shares)

        current_abs_shares = abs(net_shares)

        if desired_abs_shares >= current_abs_shares:
            # Shouldn't normally happen if abs_weight > max_abs_weight,
            # but guard against weird rounding.
            continue

        shares_to_trade_abs = current_abs_shares - desired_abs_shares
        if shares_to_trade_abs <= 0:
            continue

        if net_shares > 0:
            # Long position: SELL to reduce
            trade_type = "SELL"
        else:
            # Short position: BUY to cover
            trade_type = "BUY"

        trades.append({
            "ticker": ticker,
            "type": trade_type,
            "shares": shares_to_trade_abs,
            "priceCents": price,
            "oldWeight": weight,
            "targetAbsWeight": max_abs_weight,
        })

    return trades


# ---- Main -------------------------------------------------------------

def main():
    cfg = load_config()
    prices = load_prices()

    # Use today's date for the rebalance transaction timestamps
    today_str = date.today().isoformat()

    # Rebuild holdings and cash from all existing transactions
    holdings, cash_cents = compute_holdings_and_cash(cfg)

    if not holdings:
        print("No holdings found – nothing to rebalance.")
        return

    # Build rebalance trades
    trades = build_rebalance_trades(holdings, cash_cents, prices, MAX_ABS_WEIGHT)

    if not trades:
        print("Portfolio already within the max absolute weight of "
              f"{MAX_ABS_WEIGHT:.0%} for all positions. No trades needed.")
        return

    print("Proposed rebalance trades:")
    for t in trades:
        ticker = t["ticker"]
        ttype = t["type"]
        shares = t["shares"]
        px = t["priceCents"] / 100
        old_w = t["oldWeight"]
        print(
            f"  {ttype} {shares:,} {ticker} @ ${px:.2f} "
            f"(old weight {old_w:.2%} → target ≤ {t['targetAbsWeight']:.0%})"
        )

    # Append these trades as transactions to portfolio_config.json
    tx_list = cfg.setdefault("transactions", [])
    for t in trades:
        tx_list.append({
            "date": today_str,
            "ticker": t["ticker"],
            "type": t["type"],
            "shares": t["shares"],
            "priceCents": t["priceCents"],
            "note": "Automated portfolio rebalance",
        })

    save_config(cfg)
    print(f"\nWrote {len(trades)} rebalance transactions into {CFG_PATH.name}.")


if __name__ == "__main__":
    main()
