#!/usr/bin/env python3
import argparse
import json
import math
from datetime import date
from pathlib import Path

# --- Strategy settings ---
MAX_SHORT_WEIGHT = 0.30        # short exposure <= 30% of equity
CASH_BUFFER_CENTS = 50_000_00  # keep ~$50k cash
MIN_LONG_SHARES = 1            # ensure every LONG name has at least 1 share

# Script is /scripts/rebalance_portfolio.py, config + prices are one folder up
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CFG = "../portfolio_config.json"
DEFAULT_PRICES = "../prices.json"


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path.resolve() if path.is_absolute() else (SCRIPT_DIR / path).resolve()


def load_json(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def ensure_transactions_list(cfg: dict) -> list:
    txs = cfg.get("transactions", [])
    if txs is None:
        txs = []
    if not isinstance(txs, list):
        raise ValueError("'transactions' must be a list in portfolio_config.json")
    cfg["transactions"] = txs
    return txs


def get_universe_and_targets(cfg: dict):
    positions = cfg.get("positions", [])
    if not isinstance(positions, list) or not positions:
        raise ValueError("portfolio_config.json missing a non-empty 'positions' array.")

    universe = []
    targets = {}
    for p in positions:
        if not isinstance(p, dict):
            continue
        t = p.get("ticker")
        if not t:
            continue
        if "targetPriceCents" not in p:
            raise ValueError(f"positions[] missing targetPriceCents for ticker {t}")
        universe.append(t)
        targets[t] = int(p["targetPriceCents"])

    if not universe:
        raise ValueError("No tickers found in positions[].ticker")

    # de-dupe preserving order
    seen = set()
    universe2 = []
    for t in universe:
        if t not in seen:
            seen.add(t)
            universe2.append(t)

    return universe2, targets


def load_prices_snapshot(prices_path: Path, universe: list):
    prices_json = load_json(prices_path)
    symbols = prices_json.get("symbols")
    if not isinstance(symbols, dict):
        raise ValueError("prices.json must contain { 'symbols': { TICKER: { priceCents, ... } } }")

    live_prices = {}
    for t in universe:
        node = symbols.get(t)
        if not isinstance(node, dict) or "priceCents" not in node:
            raise ValueError(f"prices.json missing symbols['{t}'].priceCents")
        live_prices[t] = int(node["priceCents"])

    updated_at = prices_json.get("updatedAt") or date.today().isoformat()
    asof = str(updated_at)[:10]
    return asof, live_prices


def compute_holdings_and_cash(cfg: dict, asof: str):
    cash = int(cfg.get("startingCashCents", 0))
    txs = ensure_transactions_list(cfg)
    holdings = {}

    for tx in txs:
        d = tx.get("date")
        if not d or d > asof:
            continue

        t = tx.get("ticker")
        typ = str(tx.get("type", "")).upper()
        sh = int(tx.get("shares", 0))
        px = int(tx.get("priceCents", 0))
        if not t or sh <= 0 or px <= 0:
            continue

        amt = sh * px
        if typ == "BUY":
            holdings[t] = holdings.get(t, 0) + sh
            cash -= amt
        elif typ == "SELL":
            holdings[t] = holdings.get(t, 0) - sh
            cash += amt

    return holdings, cash


def equity_cents(universe: list, holdings: dict, cash: int, prices: dict) -> int:
    return cash + sum(int(holdings.get(t, 0)) * int(prices[t]) for t in universe)


def split_long_short(universe: list, targets: dict, prices: dict):
    longs, shorts = [], []
    for t in universe:
        # Rule: if price > target => SHORT, else LONG
        if prices[t] > targets[t]:
            shorts.append(t)
        else:
            longs.append(t)
    return longs, shorts


def build_desired_shares(universe, longs, shorts, prices, equity):
    desired = {t: 0 for t in universe}

    # Short notional capped at 30% of equity (if any shorts)
    short_notional = int(math.floor(MAX_SHORT_WEIGHT * equity)) if shorts else 0

    # Keep cash near buffer by investing equity + short proceeds:
    # Equity = Cash + Long - Short  => Long = Equity - Cash + Short
    long_notional = max(0, equity - CASH_BUFFER_CENTS + short_notional)

    long_each = long_notional // max(1, len(longs))
    short_each = short_notional // max(1, len(shorts)) if shorts else 0

    for t in longs:
        px = prices[t]
        desired[t] = max(MIN_LONG_SHARES, int(long_each // px))

    for t in shorts:
        px = prices[t]
        desired[t] = -max(1, int(short_each // px))

    return desired


def apply_trade_state(holdings: dict, cash: int, tr: dict):
    t = tr["ticker"]
    sh = int(tr["shares"])
    px = int(tr["priceCents"])
    amt = sh * px
    if tr["type"] == "BUY":
        holdings[t] = holdings.get(t, 0) + sh
        cash -= amt
    else:  # SELL
        holdings[t] = holdings.get(t, 0) - sh
        cash += amt
    return holdings, cash


def build_delta_trades(universe, current, desired, prices, asof):
    trades = []
    for t in universe:
        cur = int(current.get(t, 0))
        tgt = int(desired.get(t, 0))
        delta = tgt - cur
        if delta == 0:
            continue
        trades.append({
            "date": asof,
            "ticker": t,
            "type": "BUY" if delta > 0 else "SELL",
            "shares": abs(int(delta)),
            "priceCents": int(prices[t]),
            "note": "Rebalance (even longs, <=30% shorts)"
        })
    # sells first (helps avoid temporary negative cash)
    trades.sort(key=lambda x: 0 if x["type"] == "SELL" else 1)
    return trades


def adjust_cash(longs, holdings, cash, prices, asof):
    """
    Push ending cash toward CASH_BUFFER_CENTS:
    - if too high: BUY 1 share round-robin longs
    - if too low:  SELL 1 share round-robin longs (keep MIN_LONG_SHARES)
    """
    extra = []
    if not longs:
        return extra, holdings, cash

    min_long_px = min(prices[t] for t in longs)

    # Spend excess cash
    i = 0
    while cash > CASH_BUFFER_CENTS + min_long_px:
        t = longs[i % len(longs)]
        px = prices[t]
        if cash - px < CASH_BUFFER_CENTS:
            break
        tr = {"date": asof, "ticker": t, "type": "BUY", "shares": 1, "priceCents": int(px), "note": "Spend excess cash"}
        extra.append(tr)
        holdings, cash = apply_trade_state(holdings, cash, tr)
        i += 1

    # Raise cash if below buffer
    i = 0
    safety = 200000
    while cash < CASH_BUFFER_CENTS and safety > 0:
        safety -= 1
        t = longs[i % len(longs)]
        i += 1
        if int(holdings.get(t, 0)) <= MIN_LONG_SHARES:
            continue
        px = prices[t]
        tr = {"date": asof, "ticker": t, "type": "SELL", "shares": 1, "priceCents": int(px), "note": "Raise cash to buffer"}
        extra.append(tr)
        holdings, cash = apply_trade_state(holdings, cash, tr)

    return extra, holdings, cash


def main():
    parser = argparse.ArgumentParser(
        description="Rebalance using prices.json snapshot. Assumes portfolio_config.json is one folder up from /scripts."
    )
    parser.add_argument("--config", default=DEFAULT_CFG, help="Path to portfolio_config.json (default ../portfolio_config.json)")
    parser.add_argument("--prices", default=DEFAULT_PRICES, help="Path to prices.json (default ../prices.json)")
    parser.add_argument("--asof", default=None, help="Override transaction date YYYY-MM-DD (default prices.json updatedAt)")
    parser.add_argument("--dry-run", action="store_true", help="Print trades only; do not write portfolio_config.json")
    args = parser.parse_args()

    cfg_path = resolve_path(args.config)
    prices_path = resolve_path(args.prices)

    cfg = load_json(cfg_path)
    txs = ensure_transactions_list(cfg)
    universe, targets = get_universe_and_targets(cfg)

    inferred_asof, live_prices = load_prices_snapshot(prices_path, universe)
    asof = args.asof or inferred_asof

    holdings, cash = compute_holdings_and_cash(cfg, asof)
    eq = equity_cents(universe, holdings, cash, live_prices)
    if eq <= 0:
        raise RuntimeError(f"Equity is non-positive (${eq/100:,.2f}). Check starting cash / transactions.")

    longs, shorts = split_long_short(universe, targets, live_prices)
    desired = build_desired_shares(universe, longs, shorts, live_prices, eq)

    base_trades = build_delta_trades(universe, holdings, desired, live_prices, asof)
    if not base_trades:
        print(f"As-of {asof}: no trades needed.")
        return

    # simulate base trades
    sim_hold = dict(holdings)
    sim_cash = int(cash)
    for tr in base_trades:
        sim_hold, sim_cash = apply_trade_state(sim_hold, sim_cash, tr)

    # adjust cash near buffer
    extra_trades, sim_hold, sim_cash = adjust_cash(longs, sim_hold, sim_cash, live_prices, asof)
    trades = base_trades + extra_trades

    end_eq = equity_cents(universe, sim_hold, sim_cash, live_prices)
    short_mv = sum(-sim_hold[t] * live_prices[t] for t in universe if sim_hold.get(t, 0) < 0)
    long_mv = sum(sim_hold[t] * live_prices[t] for t in universe if sim_hold.get(t, 0) > 0)

    print(f"As-of {asof}: proposed trades ({len(trades)})")
    for tr in trades:
        print(f"  {tr['type']:4s} {tr['shares']:>8,} {tr['ticker']:6s} @ ${tr['priceCents']/100:,.2f}  {tr.get('note','')}")

    print("\nPost-trade (simulated):")
    print(f"  Equity:    ${end_eq/100:,.2f}")
    print(f"  Cash:      ${sim_cash/100:,.2f} (buffer target ${CASH_BUFFER_CENTS/100:,.2f})")
    print(f"  Long MV:   ${long_mv/100:,.2f}")
    print(f"  Short MV:  ${short_mv/100:,.2f} ({(short_mv/end_eq*100 if end_eq else 0):.1f}% of equity)")
    print(f"\nWill write to: {cfg_path}")

    if args.dry_run:
        print("Dry run: not writing portfolio_config.json")
        return

    # append trades + save
    txs.extend(trades)
    save_json(cfg_path, cfg)
    print(f"Saved updated config to: {cfg_path}")


if __name__ == "__main__":
    main()
