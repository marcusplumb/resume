<<<<<<< HEAD
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent

FUNDAMENTALS_FILE = ROOT / "fundamentals.json"
PRICES_HISTORY_FILE = ROOT / "prices_history.json"
PRICES_FILE = ROOT / "prices.json"
DCF_CONFIG_FILE = ROOT / "dcf_config.json"
COMPS_CONFIG_FILE = ROOT / "comps_config.json"
THESIS_TARGETS_FILE = ROOT / "thesis_targets.json"
MARKET_INPUTS_FILE = ROOT / "market_inputs.json"
DCF_DETAILS_FILE = ROOT / "dcf_details.json"


# ---------------- Market inputs (kept out of code) ----------------

DEFAULT_MARKET_INPUTS: Dict[str, Any] = {
    # Auto-updated (best-effort) from FRED
    "riskFree": {
        "source": "FRED",
        "series": "DGS5",   # 5-year treasury constant maturity
        "valuePct": None,   # filled by update_market_inputs()
        "updatedAt": None,
    },
    # You can edit these without touching code
    "equityRiskPremiumPct": 5.0,
    "companyRiskPremiumPct": 1.0,

    # Debt defaults (only used if no company implied cost and no case override)
    "defaultDebtSpreadPct": 2.0,
    "defaultCostOfDebtPct": None,

    # Optional: cap terminal growth to avoid ke <= g accidents (still respects config g if <= cap)
    "maxTerminalGrowthPct": 4.0,
}

FRED_SERIES_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# ---------------- Generic helpers ----------------

def load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[WARN] {path} not found, using default.")
        return default
    except json.JSONDecodeError as e:
        print(f"[WARN] Could not decode JSON from {path}: {e}. Using default.")
        return default

def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")

def iso_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def latest_price_from_history(history: dict, symbol: str) -> Optional[float]:
    """Latest price from prices_history.json in dollars."""
    syms = history.get("symbols", {})
    series = syms.get(symbol)
    if not isinstance(series, list) or not series:
        return None
    last = series[-1]
    cents = last.get("priceCents")
    if not isinstance(cents, (int, float)):
        return None
    return cents / 100.0

def spot_price(history: dict, prices_snapshot: dict, ticker: str) -> Optional[float]:
    """Prefer prices.json snapshot; else fall back to latest history."""
    if isinstance(prices_snapshot, dict):
        entry = (prices_snapshot.get("symbols", {}) or {}).get(ticker) or {}
        pc = entry.get("priceCents")
        if isinstance(pc, (int, float)) and pc > 0:
            return pc / 100.0
    return latest_price_from_history(history, ticker)

def avg_and_median(values):
    vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    if not vals:
        return None, None
    n = len(vals)
    avg = sum(vals) / n
    s = sorted(vals)
    mid = n // 2
    if n % 2 == 1:
        med = s[mid]
    else:
        med = 0.5 * (s[mid - 1] + s[mid])
    return avg, med

def detect_outliers_iqr(pairs):
    """JS-style outlier detection via IQR; pairs = [(sym, val), ...]."""
    vals = [v for (_, v) in pairs if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(vals) < 4:
        return set()

    s = sorted(vals)
    n = len(s)
    q1 = s[int((n - 1) * 0.25)]
    q3 = s[int((n - 1) * 0.75)]
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr

    out = set()
    for sym, val in pairs:
        if not isinstance(val, (int, float)) or not math.isfinite(val):
            continue
        if val < lower or val > upper:
            out.add(sym)
    return out

def pick_implied(implied_avg, implied_med, spot, avg, med):
    """Pick implied (avg vs median) based on which is closer to spot."""
    if avg is None or med is None:
        return None, None, None
    if implied_avg is None or implied_med is None:
        return None, None, None
    if spot is None or not math.isfinite(spot):
        return None, None, None
    diff_avg = abs(implied_avg - spot)
    diff_med = abs(implied_med - spot)
    if diff_avg <= diff_med:
        return "Average", avg, implied_avg
    return "Median", med, implied_med


# ---------------- Market inputs refresh ----------------

def fetch_fred_last_value_pct(series: str) -> Optional[float]:
    """
    Fetch latest non-missing observation from FRED CSV (no API key needed).
    Returns percent (e.g., 4.12), or None on failure.
    """
    url = FRED_SERIES_URL.format(series=series)
    try:
        with urlopen(url, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[WARN] FRED fetch failed for {series}: {e}")
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None

    # CSV: DATE,VALUE (VALUE may be ".")
    last_val = None
    for ln in reversed(lines[1:]):
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        v = parts[1].strip()
        if v == "." or v == "":
            continue
        if not re.match(r"^-?\d+(\.\d+)?$", v):
            continue
        try:
            last_val = float(v)
            break
        except Exception:
            continue
    return last_val

def update_market_inputs(market_inputs: Dict[str, Any]) -> Dict[str, Any]:
    mi = dict(DEFAULT_MARKET_INPUTS)
    mi.update(market_inputs or {})

    rf_obj = dict(DEFAULT_MARKET_INPUTS["riskFree"])
    rf_obj.update((mi.get("riskFree") or {}))

    series = rf_obj.get("series") or "DGS5"
    rf_latest = fetch_fred_last_value_pct(series)

    if rf_latest is not None and math.isfinite(rf_latest):
        rf_obj["valuePct"] = float(rf_latest)
        rf_obj["updatedAt"] = iso_now_z()
        mi["riskFree"] = rf_obj
    else:
        # Keep prior saved value if it exists; otherwise leave None and warn.
        mi["riskFree"] = rf_obj
        if rf_obj.get("valuePct") is None:
            print("[WARN] No risk-free rate available (FRED failed and no saved value).")

    return mi

def get_risk_free_pct(market_inputs: Dict[str, Any]) -> Optional[float]:
    rf = (market_inputs or {}).get("riskFree") or {}
    v = rf.get("valuePct")
    if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
        return float(v)
    return None


# ---------------- CAPM / cost of debt ----------------

def compute_cost_of_equity_pct(fundamentals: Dict[str, Any], ticker: str, market_inputs: Dict[str, Any]) -> Optional[float]:
    f = fundamentals.get(ticker, {}) or {}
    beta = f.get("beta")
    if not isinstance(beta, (int, float)) or not math.isfinite(beta) or beta <= 0:
        beta = 1.0
        print(f"[WARN] {ticker}: missing/invalid beta in fundamentals; using 1.0")

    rf = get_risk_free_pct(market_inputs)
    if rf is None:
        return None

    try:
        erp = float(market_inputs.get("equityRiskPremiumPct", DEFAULT_MARKET_INPUTS["equityRiskPremiumPct"]))
    except Exception:
        erp = float(DEFAULT_MARKET_INPUTS["equityRiskPremiumPct"])

    try:
        crp = float(market_inputs.get("companyRiskPremiumPct", DEFAULT_MARKET_INPUTS["companyRiskPremiumPct"]))
    except Exception:
        crp = float(DEFAULT_MARKET_INPUTS["companyRiskPremiumPct"])

    return rf + float(beta) * erp + crp

def compute_cost_of_debt_pct(
    fundamentals: Dict[str, Any],
    ticker: str,
    market_inputs: Dict[str, Any],
    case_cfg: Dict[str, Any],
) -> Optional[float]:
    """
    Priority:
      1) case_cfg.costOfDebtPct
      2) fundamentals.impliedCostOfDebtPct
      3) market_inputs.defaultCostOfDebtPct
      4) rf + case_cfg.debtSpreadPct
      5) rf + market_inputs.defaultDebtSpreadPct
    """
    cod_case = case_cfg.get("costOfDebtPct")
    if isinstance(cod_case, (int, float)) and math.isfinite(cod_case) and cod_case > 0:
        return float(cod_case)

    f = fundamentals.get(ticker, {}) or {}
    cod_impl = f.get("impliedCostOfDebtPct")
    if isinstance(cod_impl, (int, float)) and math.isfinite(cod_impl) and cod_impl > 0:
        return float(cod_impl)

    cod_default = market_inputs.get("defaultCostOfDebtPct", DEFAULT_MARKET_INPUTS["defaultCostOfDebtPct"])
    if isinstance(cod_default, (int, float)) and math.isfinite(cod_default) and cod_default > 0:
        return float(cod_default)

    rf = get_risk_free_pct(market_inputs)
    if rf is None:
        return None

    spread_case = case_cfg.get("debtSpreadPct")
    if isinstance(spread_case, (int, float)) and math.isfinite(spread_case) and spread_case >= 0:
        return rf + float(spread_case)

    try:
        spread_default = float(market_inputs.get("defaultDebtSpreadPct", DEFAULT_MARKET_INPUTS["defaultDebtSpreadPct"]))
    except Exception:
        spread_default = float(DEFAULT_MARKET_INPUTS["defaultDebtSpreadPct"])
    return rf + spread_default


# ---------------- DCF config extraction ----------------

def _normalize_segment_weights(ticker: str, cfg_for_ticker: Dict[str, Any]) -> Dict[str, float]:
    """
    Reads ticker-level segments from dcf_config.json.

    Supported forms:
      - segmentWeights: {"Segment A": 0.6, "Segment B": 0.4}
      - segmentWeights: {"Segment A": 60, "Segment B": 40}  (percent form)
      - segments: ["Segment A", "Segment B"]                (equal weights)
      - segments: [{"name":"A","weight":0.6}, {"name":"B","weight":0.4}]

    Enforces: max 3 segments (top 3 by weight), renormalized to sum to 1.0.
    """
    raw = None
    if isinstance(cfg_for_ticker, dict):
        raw = cfg_for_ticker.get("segmentWeights")
        if raw is None:
            raw = cfg_for_ticker.get("segments") or cfg_for_ticker.get("segmentMix")

    segs: Dict[str, float] = {}

    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(k, str) or not k.strip():
                continue
            if isinstance(v, (int, float)) and math.isfinite(v) and v != 0:
                segs[k.strip()] = float(v)

    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                segs[item.strip()] = 1.0
            elif isinstance(item, dict):
                name = item.get("name") or item.get("segment")
                w = item.get("weight") or item.get("mix") or item.get("pct")
                if isinstance(name, str) and name.strip() and isinstance(w, (int, float)) and math.isfinite(w) and w != 0:
                    segs[name.strip()] = float(w)

    if not segs:
        return {"total": 1.0}

    # If weights look like percents (e.g., 55/26/19), convert to decimals.
    total_abs = sum(abs(v) for v in segs.values())
    if total_abs > 1.5:
        segs = {k: v / 100.0 for k, v in segs.items()}

    # Force positive weights and drop zeros
    segs = {k: abs(v) for k, v in segs.items() if isinstance(v, (int, float)) and math.isfinite(v) and v != 0}
    if not segs:
        return {"total": 1.0}

    ordered = sorted(segs.items(), key=lambda kv: kv[1], reverse=True)
    if len(ordered) > 3:
        print(f"[WARN] {ticker}: segmentWeights has {len(ordered)} segments; using top 3 by weight.")
        ordered = ordered[:3]

    total = sum(w for _, w in ordered)
    if total <= 0:
        return {"total": 1.0}

    return {k: w / total for k, w in ordered}

def extract_dcf_case(ticker: str, cfg_for_ticker: dict, case_name: str) -> Tuple[Optional[Dict[str, float]], Optional[dict]]:
    if not cfg_for_ticker:
        return None, None

    seg_weights = _normalize_segment_weights(ticker, cfg_for_ticker)

    cases = cfg_for_ticker.get("cases")
    if cases and isinstance(cases, dict):
        case_cfg = cases.get(case_name)
    else:
        case_cfg = cfg_for_ticker.get(case_name)

    if not isinstance(case_cfg, dict):
        return None, None

    return seg_weights, case_cfg

def get_cash_flow_model(cfg_for_ticker: Dict[str, Any]) -> str:
    """
    Reads ticker-level model selector from dcf_config.json.
    Defaults to FCFE to match your current intended direction.
    """
    raw = None
    if isinstance(cfg_for_ticker, dict):
        raw = cfg_for_ticker.get("cashFlowModel")
    if not isinstance(raw, str) or not raw.strip():
        return "FCFE"
    return raw.strip().upper()

def _sched(arr, idx, default=0.0) -> float:
    if not arr:
        return float(default)
    if idx < len(arr):
        return float(arr[idx])
    return float(arr[-1])


# ---------------- DCF (FCFE-only) ----------------

def compute_dcf_case_fcfe(
    ticker: str,
    fundamentals: Dict[str, Any],
    history: Dict[str, Any],
    prices_snapshot: Dict[str, Any],
    dcf_cfg_for_ticker: Dict[str, Any],
    market_inputs: Dict[str, Any],
    case_name: str,
) -> Optional[Dict[str, Any]]:
    """
    FCFE-only DCF case details.

    FCFE = FCFF - Interest*(1-tax) + NetBorrowing

    - FCFF from operating bridge: NOPAT + D&A - Capex - ΔNWC
    - Interest modeled as avg(debt) * cost_of_debt
    - NetBorrowing from config (schedule or % of sales); defaults to 0 (flat debt)
    - Discount at cost of equity (Ke)
    - Equity value = PV(FCFE + TV) + netCashMultiplier*(cash - debt0)
    """
    model = get_cash_flow_model(dcf_cfg_for_ticker)
    if model != "FCFE":
        print(f"[WARN] {ticker}: cashFlowModel={model} (not FCFE). Skipping DCF for {case_name}.")
        return None

    seg_weights, case_cfg = extract_dcf_case(ticker, dcf_cfg_for_ticker, case_name)
    if not case_cfg or not seg_weights:
        return None

    seg_order = list(seg_weights.keys())

    f = fundamentals.get(ticker, {}) or {}
    revenue_ttm = f.get("revenueTtm") or 0
    market_cap = f.get("marketCap") or 0
    cash = f.get("cash") or 0
    debt0 = f.get("debt") or f.get("totalDebt") or 0

    if revenue_ttm <= 0:
        return None

    spot = spot_price(history, prices_snapshot, ticker)
    if not spot or spot <= 0:
        return None

    # Shares: prefer explicit sharesOutstanding if present
    shares_out = f.get("sharesOutstanding")
    if isinstance(shares_out, (int, float)) and math.isfinite(shares_out) and shares_out > 0:
        shares = float(shares_out)
    else:
        if market_cap <= 0:
            return None
        shares = float(market_cap) / float(spot)

    if not shares or shares <= 0:
        return None

    terminal_growth = float(case_cfg.get("terminalGrowth") or 0.0) / 100.0
    tax_rate = float(case_cfg.get("taxRate") or 25.0) / 100.0
    da_pct = float(case_cfg.get("daPct") or 3.0) / 100.0
    capex_pct = float(case_cfg.get("capexPct") or 2.0) / 100.0
    nwc_pct = float(case_cfg.get("nwcPct") or 1.0) / 100.0

    # Safety cap on terminal growth
    max_g = market_inputs.get("maxTerminalGrowthPct", DEFAULT_MARKET_INPUTS["maxTerminalGrowthPct"])
    try:
        max_g = float(max_g) / 100.0
    except Exception:
        max_g = float(DEFAULT_MARKET_INPUTS["maxTerminalGrowthPct"]) / 100.0
    if terminal_growth > max_g:
        terminal_growth = max_g

    ke_pct = compute_cost_of_equity_pct(fundamentals, ticker, market_inputs)
    if ke_pct is None:
        return None
    discount_rate = ke_pct / 100.0
    if discount_rate <= terminal_growth:
        return None

    kd_pct = compute_cost_of_debt_pct(fundamentals, ticker, market_inputs, case_cfg)
    if kd_pct is None:
        return None

    # Net cash convention
    net_cash = float(cash) - float(debt0)
    net_cash_mult = case_cfg.get("netCashMultiplier")
    if not isinstance(net_cash_mult, (int, float)) or not math.isfinite(net_cash_mult):
        net_cash_mult = 1.0
    net_cash_effective = net_cash * float(net_cash_mult)

    # Net borrowing inputs (FCFE-specific)
    nb_abs = case_cfg.get("netBorrowingSchedule")                   # [$Mn,...] length 5
    nb_pct_sched = case_cfg.get("netBorrowingPctOfSalesSchedule")   # [% of sales,...]
    nb_pct_scalar = case_cfg.get("netBorrowingPctOfSales")          # scalar %

    growth = case_cfg.get("growthSchedule") or {}
    margin_sched = case_cfg.get("ebitMarginSchedule") or []

    # Base-year sales in $Mn
    sales0_mn = float(revenue_ttm) / 1_000_000.0
    seg_sales_mn: Dict[str, float] = {seg: sales0_mn * float(w) for seg, w in seg_weights.items()}

    prev_nwc_mn = sales0_mn * nwc_pct
    debt_mn = float(debt0) / 1_000_000.0

    rows = []
    pv_sum_mn = 0.0
    pv_tv_mn = 0.0
    tv5_mn = 0.0

    for t in range(1, 6):
        # Update segment sales
        for seg in seg_order:
            g_arr = growth.get(seg) or []
            seg_sales_mn[seg] *= (1.0 + _sched(g_arr, t - 1, 0.0) / 100.0)

        sales_mn = sum(seg_sales_mn.values())

        margin_pct = _sched(margin_sched, t - 1, 0.0) / 100.0
        ebit_mn = sales_mn * margin_pct
        nopat_mn = ebit_mn * (1.0 - tax_rate)
        da_mn = sales_mn * da_pct
        capex_mn = sales_mn * capex_pct

        nwc_mn = sales_mn * nwc_pct
        delta_nwc_mn = nwc_mn - prev_nwc_mn

        fcff_mn = nopat_mn + da_mn - capex_mn - delta_nwc_mn

        # Net borrowing (defaults to 0 => flat debt)
        net_borrow_mn = 0.0
        if isinstance(nb_abs, list) and nb_abs:
            net_borrow_mn = _sched(nb_abs, t - 1, 0.0)
        elif isinstance(nb_pct_sched, list) and nb_pct_sched:
            net_borrow_mn = sales_mn * (_sched(nb_pct_sched, t - 1, 0.0) / 100.0)
        elif isinstance(nb_pct_scalar, (int, float)) and math.isfinite(nb_pct_scalar):
            net_borrow_mn = sales_mn * (float(nb_pct_scalar) / 100.0)

        # Interest on average debt
        debt_start = debt_mn
        debt_end = debt_mn + net_borrow_mn
        avg_debt = 0.5 * (debt_start + debt_end)
        interest_mn = avg_debt * (kd_pct / 100.0)

        # FCFE
        fcfe_mn = fcff_mn - interest_mn * (1.0 - tax_rate) + net_borrow_mn

        df = (1.0 + discount_rate) ** t
        pv_mn = fcfe_mn / df
        pv_sum_mn += pv_mn

        row = {
            "year": t,
            "salesMn": sales_mn,
            "segmentsMn": {seg: seg_sales_mn[seg] for seg in seg_order},
            "ebitMn": ebit_mn,
            "nopatMn": nopat_mn,
            "daMn": da_mn,
            "capexMn": capex_mn,
            "deltaNwcMn": delta_nwc_mn,
            "fcfeMn": fcfe_mn,
            "pvMn": pv_mn,
        }

        # Backwards-compatible fields for GME-style templates (only if present)
        if "collectibles" in seg_sales_mn:
            row["collectiblesMn"] = seg_sales_mn["collectibles"]
        if "hardware" in seg_sales_mn:
            row["hardwareMn"] = seg_sales_mn["hardware"]
        if "software" in seg_sales_mn:
            row["softwareMn"] = seg_sales_mn["software"]

        rows.append(row)

        if t == 5:
            fcfe6_mn = fcfe_mn * (1.0 + terminal_growth)
            tv5_mn = fcfe6_mn / (discount_rate - terminal_growth)
            pv_tv_mn = tv5_mn / df
            pv_sum_mn += pv_tv_mn

        prev_nwc_mn = nwc_mn
        debt_mn = debt_end

    # Terminal row (for table display)
    if tv5_mn and pv_tv_mn:
        rows.append({
            "year": "TV",
            "fcfeMn": tv5_mn,
            "pvMn": pv_tv_mn,
        })

    equity_pv = pv_sum_mn * 1_000_000.0
    equity_value = equity_pv + net_cash_effective
    price = equity_value / shares

    if not math.isfinite(price) or price <= 0:
        return None

    return {
        "label": case_cfg.get("label") or case_name,
        "cashFlowModel": "FCFE",
        "spot": spot,
        "shares": shares,
        "discountRatePct": ke_pct,
        "costOfEquityPct": ke_pct,
        "costOfDebtPct": kd_pct,
        "terminalGrowthPct": terminal_growth * 100.0,
        "taxRatePct": tax_rate * 100.0,
        "pvFcfeMn": pv_sum_mn,
        "netCashEffectiveMn": net_cash_effective / 1_000_000.0,
        "equityValueMn": equity_value / 1_000_000.0,
        "price": price,
        "segmentOrder": seg_order,
        "segmentWeights": seg_weights,
        "rows": rows,
    }


def compute_dcf_price_fcfe(
    ticker: str,
    fundamentals: Dict[str, Any],
    history: Dict[str, Any],
    prices_snapshot: Dict[str, Any],
    dcf_cfg_for_ticker: Dict[str, Any],
    market_inputs: Dict[str, Any],
    case_name: str,
) -> Optional[float]:
    det = compute_dcf_case_fcfe(
        ticker, fundamentals, history, prices_snapshot, dcf_cfg_for_ticker, market_inputs, case_name
    )
    if not det:
        return None
    return det.get("price")


# ---------------- Multiples (unchanged logic, but safer universe handling) ----------------

def _dedupe_preserve_order(items):
    seen = set()
    out = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

def compute_multiples_range(ticker, fundamentals, history, comps_cfg):
    universe = comps_cfg.get(ticker)
    if not universe:
        universe = [ticker]
    else:
        if not isinstance(universe, list):
            universe = list(universe)
        universe = [u for u in universe if isinstance(u, str) and u.strip()]
        universe = _dedupe_preserve_order(universe)
        if ticker not in universe:
            universe.append(ticker)

    comps_data = []
    for sym in universe:
        f = fundamentals.get(sym, {}) or {}
        price = latest_price_from_history(history, sym) or 0.0
        mkt_cap = f.get("marketCap") or 0
        rev = f.get("revenueTtm") or 0
        ebitda = f.get("ebitdaTtm") or 0
        pe = f.get("pe") or 0
        cash = f.get("cash") or 0
        debt = f.get("totalDebt") or f.get("debt") or 0

        ev_raw = mkt_cap + debt - cash
        ev_sales = ev_raw / rev if ev_raw > 0 and rev > 0 else float("nan")
        ev_ebitda = ev_raw / ebitda if ev_raw > 0 and ebitda > 0 else float("nan")

        comps_data.append({
            "sym": sym, "price": price, "mktCap": mkt_cap, "rev": rev, "ebitda": ebitda,
            "pe": pe, "cash": cash, "debt": debt, "evRaw": ev_raw,
            "evSales": ev_sales, "evEbitda": ev_ebitda,
        })

    root_row = next((d for d in comps_data if d["sym"] == ticker), None)
    if not root_row or not root_row["price"] or not root_row["mktCap"]:
        print(f"[WARN] Missing price or market cap for {ticker}, skipping multiples.")
        return None

    price_root = root_row["price"]
    mkt_cap_root = root_row["mktCap"]
    rev_root = root_row["rev"]
    ebitda_root = root_row["ebitda"]
    pe_root = root_row["pe"]
    cash_root = root_row["cash"]
    debt_root = root_row["debt"]

    shares = mkt_cap_root / price_root if price_root > 0 else None
    if not shares or shares <= 0:
        print(f"[WARN] Invalid shares for {ticker}, skipping multiples.")
        return None

    net_cash = cash_root - debt_root
    net_cash_per_share = net_cash / shares
    cash_per_share = cash_root / shares if shares > 0 else 0.0

    ev_sales_pairs = [
        (d["sym"], d["evSales"])
        for d in comps_data
        if d["sym"] != ticker and isinstance(d["evSales"], (int, float)) and math.isfinite(d["evSales"])
    ]
    ev_ebitda_pairs = [
        (d["sym"], d["evEbitda"])
        for d in comps_data
        if d["sym"] != ticker and isinstance(d["evEbitda"], (int, float)) and math.isfinite(d["evEbitda"])
    ]
    pe_pairs = [
        (d["sym"], d["pe"])
        for d in comps_data
        if d["sym"] != ticker and isinstance(d["pe"], (int, float)) and d["pe"] > 0
    ]

    outliers_ev_sales = detect_outliers_iqr(ev_sales_pairs)
    outliers_ev_ebitda = detect_outliers_iqr(ev_ebitda_pairs)
    outliers_pe = detect_outliers_iqr(pe_pairs)

    implied_prices_all = []

    # EV/Sales
    peer_ev_sales_vals = [
        d["evSales"] for d in comps_data
        if d["sym"] != ticker
        and isinstance(d["evSales"], (int, float)) and math.isfinite(d["evSales"])
        and d["sym"] not in outliers_ev_sales
    ]
    if rev_root > 0 and shares > 0 and peer_ev_sales_vals:
        avg_ev_sales, med_ev_sales = avg_and_median(peer_ev_sales_vals)
        revenue_per_share = rev_root / shares
        implied_avg_ev_per_share = avg_ev_sales * revenue_per_share
        implied_med_ev_per_share = med_ev_sales * revenue_per_share
        current_ev_per_share = price_root - net_cash_per_share
        _, _, chosen_ev_per_share = pick_implied(
            implied_avg_ev_per_share, implied_med_ev_per_share, current_ev_per_share,
            avg_ev_sales, med_ev_sales
        )
        if chosen_ev_per_share is not None:
            equity_price = chosen_ev_per_share + net_cash_per_share
            if math.isfinite(equity_price) and equity_price > 0:
                implied_prices_all.append(equity_price)

    # EV/EBITDA
    peer_ev_ebitda_vals = [
        d["evEbitda"] for d in comps_data
        if d["sym"] != ticker
        and isinstance(d["evEbitda"], (int, float)) and math.isfinite(d["evEbitda"])
        and d["sym"] not in outliers_ev_ebitda
    ]
    if ebitda_root > 0 and shares > 0 and peer_ev_ebitda_vals:
        avg_ev_ebitda, med_ev_ebitda = avg_and_median(peer_ev_ebitda_vals)
        ebitda_per_share = ebitda_root / shares
        implied_avg_ev_per_share = avg_ev_ebitda * ebitda_per_share
        implied_med_ev_per_share = med_ev_ebitda * ebitda_per_share
        current_ev_per_share = price_root - net_cash_per_share
        _, _, chosen_ev_per_share = pick_implied(
            implied_avg_ev_per_share, implied_med_ev_per_share, current_ev_per_share,
            avg_ev_ebitda, med_ev_ebitda
        )
        if chosen_ev_per_share is not None:
            equity_price = chosen_ev_per_share + net_cash_per_share
            if math.isfinite(equity_price) and equity_price > 0:
                implied_prices_all.append(equity_price)

    # P/E
    peer_pe_vals = [
        d["pe"] for d in comps_data
        if d["sym"] != ticker
        and isinstance(d["pe"], (int, float)) and d["pe"] > 0
        and d["sym"] not in outliers_pe
    ]
    if pe_root and pe_root > 0 and peer_pe_vals:
        avg_pe, med_pe = avg_and_median(peer_pe_vals)
        eps = price_root / pe_root
        implied_avg_price = avg_pe * eps
        implied_med_price = med_pe * eps
        _, _, chosen_price = pick_implied(implied_avg_price, implied_med_price, price_root, avg_pe, med_pe)
        if chosen_price is not None and math.isfinite(chosen_price) and chosen_price > 0:
            implied_prices_all.append(chosen_price)

    if not implied_prices_all:
        print(f"[WARN] No implied prices could be computed for {ticker}")
        return {
            "spot": price_root,
            "netCashPerShare": round(net_cash_per_share, 2),
            "cashPerShare": round(cash_per_share, 2),
            "low": None,
            "high": None,
        }

    low = round(min(implied_prices_all), 2)
    high = round(max(implied_prices_all), 2)

    return {
        "spot": price_root,
        "netCashPerShare": round(net_cash_per_share, 2),
        "cashPerShare": round(cash_per_share, 2),
        "low": low,
        "high": high,
    }


# ---------------- Central estimate selection ----------------

def choose_central_estimate(ticker, comps_info, dcf_base, dcf_bull):
    def has(x):
        return isinstance(x, (int, float)) and math.isfinite(x) and x > 0

    comps_low = comps_info.get("low") if comps_info else None
    comps_high = comps_info.get("high") if comps_info else None

    if has(comps_low) and has(comps_high) and has(dcf_bull):
        mid = 0.5 * (comps_low + comps_high)
        central = 0.5 * (mid + dcf_bull)
        source = "blend(multiples_midpoint, dcf_bull)"
    elif has(dcf_bull):
        central = dcf_bull
        source = "bull_dcf"
    elif has(comps_low) and has(comps_high):
        central = 0.5 * (comps_low + comps_high)
        source = "multiples_midpoint"
    elif has(dcf_base):
        central = dcf_base
        source = "base_dcf"
    else:
        return None, "insufficient_data"

    # round to nearest $0.50
    central = round(central * 2.0) / 2.0
    return central, source


# ---------------- Main ----------------

def main():
    fundamentals = load_json(FUNDAMENTALS_FILE, {})
    history = load_json(PRICES_HISTORY_FILE, {})
    prices_snapshot = load_json(PRICES_FILE, {})
    dcf_config = load_json(DCF_CONFIG_FILE, {})
    comps_config = load_json(COMPS_CONFIG_FILE, {})

    # Market inputs: update (risk-free) and persist
    market_inputs = load_json(MARKET_INPUTS_FILE, {})
    market_inputs = update_market_inputs(market_inputs)
    save_json(MARKET_INPUTS_FILE, market_inputs)

    tickers = sorted(set(dcf_config.keys()) | set(comps_config.keys()))
    if not tickers:
        print("[WARN] No tickers found in dcf_config.json or comps_config.json.")
        return

    now_iso = iso_now_z()
    out: Dict[str, Any] = {}
    dcf_details_out: Dict[str, Any] = {}

    for ticker in tickers:
        print(f"\n=== Computing thesis target for {ticker} ===")

        comps_info = compute_multiples_range(ticker, fundamentals, history, comps_config)
        if comps_info:
            print(f" -> multiples range: {comps_info['low']} – {comps_info['high']} (if not None)")
        else:
            print(" -> multiples range: n/a")

        dcf_cfg_for_ticker = dcf_config.get(ticker, {}) or {}
        model = get_cash_flow_model(dcf_cfg_for_ticker)

        case_details = {}
        for case_name in ("base", "bull", "bear"):
            det = compute_dcf_case_fcfe(
                ticker, fundamentals, history, prices_snapshot, dcf_cfg_for_ticker, market_inputs, case_name
            )
            if det:
                # keep stable precision in the files
                det["price"] = round(float(det["price"]), 4)
                case_details[case_name] = det

        dcf_base = case_details.get("base", {}).get("price") if case_details.get("base") else None
        dcf_bull = case_details.get("bull", {}).get("price") if case_details.get("bull") else None
        dcf_bear = case_details.get("bear", {}).get("price") if case_details.get("bear") else None

        if case_details:
            dcf_details_out[ticker] = {
                "ticker": ticker,
                "updatedAt": now_iso,
                "cashFlowModel": model,
                "cases": case_details,
            }

        print(f" -> dcfBase: {dcf_base:.2f}" if dcf_base is not None else " -> dcfBase: n/a")
        print(f" -> dcfBull: {dcf_bull:.2f}" if dcf_bull is not None else " -> dcfBull: n/a")

        spot = comps_info["spot"] if comps_info and comps_info.get("spot") is not None else spot_price(history, prices_snapshot, ticker)

        central, source = choose_central_estimate(ticker, comps_info, dcf_base, dcf_bull)
        print(f" -> centralEstimate: {central} (source={source})")

        row: Dict[str, Any] = {
            "ticker": ticker,
            "updatedAt": now_iso,
            "centralEstimate": central,
            "source": source,
            "spot": spot,
            "dcfModel": model,
        }

        if comps_info:
            row["compsLow"] = comps_info.get("low")
            row["compsHigh"] = comps_info.get("high")
            row["netCashPerShare"] = comps_info.get("netCashPerShare")
            row["cashPerShare"] = comps_info.get("cashPerShare")

        if dcf_base is not None:
            row["dcfBase"] = round(dcf_base, 2)
        if dcf_bull is not None:
            row["dcfBull"] = round(dcf_bull, 2)
        if dcf_bear is not None:
            row["dcfBear"] = round(dcf_bear, 2)

        out[ticker] = row

    save_json(THESIS_TARGETS_FILE, out)
    save_json(DCF_DETAILS_FILE, dcf_details_out)
    print(f"Wrote dcf_details.json to {DCF_DETAILS_FILE}")
    print(f"\nWrote updated thesis_targets.json to {THESIS_TARGETS_FILE}")
    print(f"Wrote/updated market_inputs.json to {MARKET_INPUTS_FILE}")

if __name__ == "__main__":
    main()
=======
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent

FUNDAMENTALS_FILE = ROOT / "fundamentals.json"
PRICES_HISTORY_FILE = ROOT / "prices_history.json"
PRICES_FILE = ROOT / "prices.json"
DCF_CONFIG_FILE = ROOT / "dcf_config.json"
COMPS_CONFIG_FILE = ROOT / "comps_config.json"
THESIS_TARGETS_FILE = ROOT / "thesis_targets.json"
MARKET_INPUTS_FILE = ROOT / "market_inputs.json"
DCF_DETAILS_FILE = ROOT / "dcf_details.json"


# ---------------- Market inputs (kept out of code) ----------------

DEFAULT_MARKET_INPUTS: Dict[str, Any] = {
    # Auto-updated (best-effort) from FRED
    "riskFree": {
        "source": "FRED",
        "series": "DGS5",   # 5-year treasury constant maturity
        "valuePct": None,   # filled by update_market_inputs()
        "updatedAt": None,
    },
    # You can edit these without touching code
    "equityRiskPremiumPct": 5.0,
    "companyRiskPremiumPct": 1.0,

    # Debt defaults (only used if no company implied cost and no case override)
    "defaultDebtSpreadPct": 2.0,
    "defaultCostOfDebtPct": None,

    # Optional: cap terminal growth to avoid ke <= g accidents (still respects config g if <= cap)
    "maxTerminalGrowthPct": 4.0,
}

FRED_SERIES_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# ---------------- Generic helpers ----------------

def load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[WARN] {path} not found, using default.")
        return default
    except json.JSONDecodeError as e:
        print(f"[WARN] Could not decode JSON from {path}: {e}. Using default.")
        return default

def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")

def iso_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def latest_price_from_history(history: dict, symbol: str) -> Optional[float]:
    """Latest price from prices_history.json in dollars."""
    syms = history.get("symbols", {})
    series = syms.get(symbol)
    if not isinstance(series, list) or not series:
        return None
    last = series[-1]
    cents = last.get("priceCents")
    if not isinstance(cents, (int, float)):
        return None
    return cents / 100.0

def spot_price(history: dict, prices_snapshot: dict, ticker: str) -> Optional[float]:
    """Prefer prices.json snapshot; else fall back to latest history."""
    if isinstance(prices_snapshot, dict):
        entry = (prices_snapshot.get("symbols", {}) or {}).get(ticker) or {}
        pc = entry.get("priceCents")
        if isinstance(pc, (int, float)) and pc > 0:
            return pc / 100.0
    return latest_price_from_history(history, ticker)

def avg_and_median(values):
    vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    if not vals:
        return None, None
    n = len(vals)
    avg = sum(vals) / n
    s = sorted(vals)
    mid = n // 2
    if n % 2 == 1:
        med = s[mid]
    else:
        med = 0.5 * (s[mid - 1] + s[mid])
    return avg, med

def detect_outliers_iqr(pairs):
    """JS-style outlier detection via IQR; pairs = [(sym, val), ...]."""
    vals = [v for (_, v) in pairs if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(vals) < 4:
        return set()

    s = sorted(vals)
    n = len(s)
    q1 = s[int((n - 1) * 0.25)]
    q3 = s[int((n - 1) * 0.75)]
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr

    out = set()
    for sym, val in pairs:
        if not isinstance(val, (int, float)) or not math.isfinite(val):
            continue
        if val < lower or val > upper:
            out.add(sym)
    return out

def pick_implied(implied_avg, implied_med, spot, avg, med):
    """Pick implied (avg vs median) based on which is closer to spot."""
    if avg is None or med is None:
        return None, None, None
    if implied_avg is None or implied_med is None:
        return None, None, None
    if spot is None or not math.isfinite(spot):
        return None, None, None
    diff_avg = abs(implied_avg - spot)
    diff_med = abs(implied_med - spot)
    if diff_avg <= diff_med:
        return "Average", avg, implied_avg
    return "Median", med, implied_med


# ---------------- Market inputs refresh ----------------

def fetch_fred_last_value_pct(series: str) -> Optional[float]:
    """
    Fetch latest non-missing observation from FRED CSV (no API key needed).
    Returns percent (e.g., 4.12), or None on failure.
    """
    url = FRED_SERIES_URL.format(series=series)
    try:
        with urlopen(url, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[WARN] FRED fetch failed for {series}: {e}")
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None

    # CSV: DATE,VALUE (VALUE may be ".")
    last_val = None
    for ln in reversed(lines[1:]):
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        v = parts[1].strip()
        if v == "." or v == "":
            continue
        if not re.match(r"^-?\d+(\.\d+)?$", v):
            continue
        try:
            last_val = float(v)
            break
        except Exception:
            continue
    return last_val

def update_market_inputs(market_inputs: Dict[str, Any]) -> Dict[str, Any]:
    mi = dict(DEFAULT_MARKET_INPUTS)
    mi.update(market_inputs or {})

    rf_obj = dict(DEFAULT_MARKET_INPUTS["riskFree"])
    rf_obj.update((mi.get("riskFree") or {}))

    series = rf_obj.get("series") or "DGS5"
    rf_latest = fetch_fred_last_value_pct(series)

    if rf_latest is not None and math.isfinite(rf_latest):
        rf_obj["valuePct"] = float(rf_latest)
        rf_obj["updatedAt"] = iso_now_z()
        mi["riskFree"] = rf_obj
    else:
        # Keep prior saved value if it exists; otherwise leave None and warn.
        mi["riskFree"] = rf_obj
        if rf_obj.get("valuePct") is None:
            print("[WARN] No risk-free rate available (FRED failed and no saved value).")

    return mi

def get_risk_free_pct(market_inputs: Dict[str, Any]) -> Optional[float]:
    rf = (market_inputs or {}).get("riskFree") or {}
    v = rf.get("valuePct")
    if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
        return float(v)
    return None


# ---------------- CAPM / cost of debt ----------------

def compute_cost_of_equity_pct(fundamentals: Dict[str, Any], ticker: str, market_inputs: Dict[str, Any]) -> Optional[float]:
    f = fundamentals.get(ticker, {}) or {}
    beta = f.get("beta")
    if not isinstance(beta, (int, float)) or not math.isfinite(beta) or beta <= 0:
        beta = 1.0
        print(f"[WARN] {ticker}: missing/invalid beta in fundamentals; using 1.0")

    rf = get_risk_free_pct(market_inputs)
    if rf is None:
        return None

    try:
        erp = float(market_inputs.get("equityRiskPremiumPct", DEFAULT_MARKET_INPUTS["equityRiskPremiumPct"]))
    except Exception:
        erp = float(DEFAULT_MARKET_INPUTS["equityRiskPremiumPct"])

    try:
        crp = float(market_inputs.get("companyRiskPremiumPct", DEFAULT_MARKET_INPUTS["companyRiskPremiumPct"]))
    except Exception:
        crp = float(DEFAULT_MARKET_INPUTS["companyRiskPremiumPct"])

    return rf + float(beta) * erp + crp

def compute_cost_of_debt_pct(
    fundamentals: Dict[str, Any],
    ticker: str,
    market_inputs: Dict[str, Any],
    case_cfg: Dict[str, Any],
) -> Optional[float]:
    """
    Priority:
      1) case_cfg.costOfDebtPct
      2) fundamentals.impliedCostOfDebtPct
      3) market_inputs.defaultCostOfDebtPct
      4) rf + case_cfg.debtSpreadPct
      5) rf + market_inputs.defaultDebtSpreadPct
    """
    cod_case = case_cfg.get("costOfDebtPct")
    if isinstance(cod_case, (int, float)) and math.isfinite(cod_case) and cod_case > 0:
        return float(cod_case)

    f = fundamentals.get(ticker, {}) or {}
    cod_impl = f.get("impliedCostOfDebtPct")
    if isinstance(cod_impl, (int, float)) and math.isfinite(cod_impl) and cod_impl > 0:
        return float(cod_impl)

    cod_default = market_inputs.get("defaultCostOfDebtPct", DEFAULT_MARKET_INPUTS["defaultCostOfDebtPct"])
    if isinstance(cod_default, (int, float)) and math.isfinite(cod_default) and cod_default > 0:
        return float(cod_default)

    rf = get_risk_free_pct(market_inputs)
    if rf is None:
        return None

    spread_case = case_cfg.get("debtSpreadPct")
    if isinstance(spread_case, (int, float)) and math.isfinite(spread_case) and spread_case >= 0:
        return rf + float(spread_case)

    try:
        spread_default = float(market_inputs.get("defaultDebtSpreadPct", DEFAULT_MARKET_INPUTS["defaultDebtSpreadPct"]))
    except Exception:
        spread_default = float(DEFAULT_MARKET_INPUTS["defaultDebtSpreadPct"])
    return rf + spread_default


# ---------------- DCF config extraction ----------------

def _normalize_segment_weights(ticker: str, cfg_for_ticker: Dict[str, Any]) -> Dict[str, float]:
    """
    Reads ticker-level segments from dcf_config.json.

    Supported forms:
      - segmentWeights: {"Segment A": 0.6, "Segment B": 0.4}
      - segmentWeights: {"Segment A": 60, "Segment B": 40}  (percent form)
      - segments: ["Segment A", "Segment B"]                (equal weights)
      - segments: [{"name":"A","weight":0.6}, {"name":"B","weight":0.4}]

    Enforces: max 3 segments (top 3 by weight), renormalized to sum to 1.0.
    """
    raw = None
    if isinstance(cfg_for_ticker, dict):
        raw = cfg_for_ticker.get("segmentWeights")
        if raw is None:
            raw = cfg_for_ticker.get("segments") or cfg_for_ticker.get("segmentMix")

    segs: Dict[str, float] = {}

    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(k, str) or not k.strip():
                continue
            if isinstance(v, (int, float)) and math.isfinite(v) and v != 0:
                segs[k.strip()] = float(v)

    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                segs[item.strip()] = 1.0
            elif isinstance(item, dict):
                name = item.get("name") or item.get("segment")
                w = item.get("weight") or item.get("mix") or item.get("pct")
                if isinstance(name, str) and name.strip() and isinstance(w, (int, float)) and math.isfinite(w) and w != 0:
                    segs[name.strip()] = float(w)

    if not segs:
        return {"total": 1.0}

    # If weights look like percents (e.g., 55/26/19), convert to decimals.
    total_abs = sum(abs(v) for v in segs.values())
    if total_abs > 1.5:
        segs = {k: v / 100.0 for k, v in segs.items()}

    # Force positive weights and drop zeros
    segs = {k: abs(v) for k, v in segs.items() if isinstance(v, (int, float)) and math.isfinite(v) and v != 0}
    if not segs:
        return {"total": 1.0}

    ordered = sorted(segs.items(), key=lambda kv: kv[1], reverse=True)
    if len(ordered) > 3:
        print(f"[WARN] {ticker}: segmentWeights has {len(ordered)} segments; using top 3 by weight.")
        ordered = ordered[:3]

    total = sum(w for _, w in ordered)
    if total <= 0:
        return {"total": 1.0}

    return {k: w / total for k, w in ordered}

def extract_dcf_case(ticker: str, cfg_for_ticker: dict, case_name: str) -> Tuple[Optional[Dict[str, float]], Optional[dict]]:
    if not cfg_for_ticker:
        return None, None

    seg_weights = _normalize_segment_weights(ticker, cfg_for_ticker)

    cases = cfg_for_ticker.get("cases")
    if cases and isinstance(cases, dict):
        case_cfg = cases.get(case_name)
    else:
        case_cfg = cfg_for_ticker.get(case_name)

    if not isinstance(case_cfg, dict):
        return None, None

    return seg_weights, case_cfg

def get_cash_flow_model(cfg_for_ticker: Dict[str, Any]) -> str:
    """
    Reads ticker-level model selector from dcf_config.json.
    Defaults to FCFE to match your current intended direction.
    """
    raw = None
    if isinstance(cfg_for_ticker, dict):
        raw = cfg_for_ticker.get("cashFlowModel")
    if not isinstance(raw, str) or not raw.strip():
        return "FCFE"
    return raw.strip().upper()

def _sched(arr, idx, default=0.0) -> float:
    if not arr:
        return float(default)
    if idx < len(arr):
        return float(arr[idx])
    return float(arr[-1])


# ---------------- DCF (FCFE-only) ----------------

def compute_dcf_case_fcfe(
    ticker: str,
    fundamentals: Dict[str, Any],
    history: Dict[str, Any],
    prices_snapshot: Dict[str, Any],
    dcf_cfg_for_ticker: Dict[str, Any],
    market_inputs: Dict[str, Any],
    case_name: str,
) -> Optional[Dict[str, Any]]:
    """
    FCFE-only DCF case details.

    FCFE = FCFF - Interest*(1-tax) + NetBorrowing

    - FCFF from operating bridge: NOPAT + D&A - Capex - ΔNWC
    - Interest modeled as avg(debt) * cost_of_debt
    - NetBorrowing from config (schedule or % of sales); defaults to 0 (flat debt)
    - Discount at cost of equity (Ke)
    - Equity value = PV(FCFE + TV) + netCashMultiplier*(cash - debt0)
    """
    model = get_cash_flow_model(dcf_cfg_for_ticker)
    if model != "FCFE":
        print(f"[WARN] {ticker}: cashFlowModel={model} (not FCFE). Skipping DCF for {case_name}.")
        return None

    seg_weights, case_cfg = extract_dcf_case(ticker, dcf_cfg_for_ticker, case_name)
    if not case_cfg or not seg_weights:
        return None

    seg_order = list(seg_weights.keys())

    f = fundamentals.get(ticker, {}) or {}
    revenue_ttm = f.get("revenueTtm") or 0
    market_cap = f.get("marketCap") or 0
    cash = f.get("cash") or 0
    debt0 = f.get("debt") or f.get("totalDebt") or 0

    if revenue_ttm <= 0:
        return None

    spot = spot_price(history, prices_snapshot, ticker)
    if not spot or spot <= 0:
        return None

    # Shares: prefer explicit sharesOutstanding if present
    shares_out = f.get("sharesOutstanding")
    if isinstance(shares_out, (int, float)) and math.isfinite(shares_out) and shares_out > 0:
        shares = float(shares_out)
    else:
        if market_cap <= 0:
            return None
        shares = float(market_cap) / float(spot)

    if not shares or shares <= 0:
        return None

    terminal_growth = float(case_cfg.get("terminalGrowth") or 0.0) / 100.0
    tax_rate = float(case_cfg.get("taxRate") or 25.0) / 100.0
    da_pct = float(case_cfg.get("daPct") or 3.0) / 100.0
    capex_pct = float(case_cfg.get("capexPct") or 2.0) / 100.0
    nwc_pct = float(case_cfg.get("nwcPct") or 1.0) / 100.0

    # Safety cap on terminal growth
    max_g = market_inputs.get("maxTerminalGrowthPct", DEFAULT_MARKET_INPUTS["maxTerminalGrowthPct"])
    try:
        max_g = float(max_g) / 100.0
    except Exception:
        max_g = float(DEFAULT_MARKET_INPUTS["maxTerminalGrowthPct"]) / 100.0
    if terminal_growth > max_g:
        terminal_growth = max_g

    ke_pct = compute_cost_of_equity_pct(fundamentals, ticker, market_inputs)
    if ke_pct is None:
        return None
    discount_rate = ke_pct / 100.0
    if discount_rate <= terminal_growth:
        return None

    kd_pct = compute_cost_of_debt_pct(fundamentals, ticker, market_inputs, case_cfg)
    if kd_pct is None:
        return None

    # Net cash convention
    net_cash = float(cash) - float(debt0)
    net_cash_mult = case_cfg.get("netCashMultiplier")
    if not isinstance(net_cash_mult, (int, float)) or not math.isfinite(net_cash_mult):
        net_cash_mult = 1.0
    net_cash_effective = net_cash * float(net_cash_mult)

    # Net borrowing inputs (FCFE-specific)
    nb_abs = case_cfg.get("netBorrowingSchedule")                   # [$Mn,...] length 5
    nb_pct_sched = case_cfg.get("netBorrowingPctOfSalesSchedule")   # [% of sales,...]
    nb_pct_scalar = case_cfg.get("netBorrowingPctOfSales")          # scalar %

    growth = case_cfg.get("growthSchedule") or {}
    margin_sched = case_cfg.get("ebitMarginSchedule") or []

    # Base-year sales in $Mn
    sales0_mn = float(revenue_ttm) / 1_000_000.0
    seg_sales_mn: Dict[str, float] = {seg: sales0_mn * float(w) for seg, w in seg_weights.items()}

    prev_nwc_mn = sales0_mn * nwc_pct
    debt_mn = float(debt0) / 1_000_000.0

    rows = []
    pv_sum_mn = 0.0
    pv_tv_mn = 0.0
    tv5_mn = 0.0

    for t in range(1, 6):
        # Update segment sales
        for seg in seg_order:
            g_arr = growth.get(seg) or []
            seg_sales_mn[seg] *= (1.0 + _sched(g_arr, t - 1, 0.0) / 100.0)

        sales_mn = sum(seg_sales_mn.values())

        margin_pct = _sched(margin_sched, t - 1, 0.0) / 100.0
        ebit_mn = sales_mn * margin_pct
        nopat_mn = ebit_mn * (1.0 - tax_rate)
        da_mn = sales_mn * da_pct
        capex_mn = sales_mn * capex_pct

        nwc_mn = sales_mn * nwc_pct
        delta_nwc_mn = nwc_mn - prev_nwc_mn

        fcff_mn = nopat_mn + da_mn - capex_mn - delta_nwc_mn

        # Net borrowing (defaults to 0 => flat debt)
        net_borrow_mn = 0.0
        if isinstance(nb_abs, list) and nb_abs:
            net_borrow_mn = _sched(nb_abs, t - 1, 0.0)
        elif isinstance(nb_pct_sched, list) and nb_pct_sched:
            net_borrow_mn = sales_mn * (_sched(nb_pct_sched, t - 1, 0.0) / 100.0)
        elif isinstance(nb_pct_scalar, (int, float)) and math.isfinite(nb_pct_scalar):
            net_borrow_mn = sales_mn * (float(nb_pct_scalar) / 100.0)

        # Interest on average debt
        debt_start = debt_mn
        debt_end = debt_mn + net_borrow_mn
        avg_debt = 0.5 * (debt_start + debt_end)
        interest_mn = avg_debt * (kd_pct / 100.0)

        # FCFE
        fcfe_mn = fcff_mn - interest_mn * (1.0 - tax_rate) + net_borrow_mn

        df = (1.0 + discount_rate) ** t
        pv_mn = fcfe_mn / df
        pv_sum_mn += pv_mn

        row = {
            "year": t,
            "salesMn": sales_mn,
            "segmentsMn": {seg: seg_sales_mn[seg] for seg in seg_order},
            "ebitMn": ebit_mn,
            "nopatMn": nopat_mn,
            "daMn": da_mn,
            "capexMn": capex_mn,
            "deltaNwcMn": delta_nwc_mn,
            "fcfeMn": fcfe_mn,
            "pvMn": pv_mn,
        }

        # Backwards-compatible fields for GME-style templates (only if present)
        if "collectibles" in seg_sales_mn:
            row["collectiblesMn"] = seg_sales_mn["collectibles"]
        if "hardware" in seg_sales_mn:
            row["hardwareMn"] = seg_sales_mn["hardware"]
        if "software" in seg_sales_mn:
            row["softwareMn"] = seg_sales_mn["software"]

        rows.append(row)

        if t == 5:
            fcfe6_mn = fcfe_mn * (1.0 + terminal_growth)
            tv5_mn = fcfe6_mn / (discount_rate - terminal_growth)
            pv_tv_mn = tv5_mn / df
            pv_sum_mn += pv_tv_mn

        prev_nwc_mn = nwc_mn
        debt_mn = debt_end

    # Terminal row (for table display)
    if tv5_mn and pv_tv_mn:
        rows.append({
            "year": "TV",
            "fcfeMn": tv5_mn,
            "pvMn": pv_tv_mn,
        })

    equity_pv = pv_sum_mn * 1_000_000.0
    equity_value = equity_pv + net_cash_effective
    price = equity_value / shares

    if not math.isfinite(price) or price <= 0:
        return None

    return {
        "label": case_cfg.get("label") or case_name,
        "cashFlowModel": "FCFE",
        "spot": spot,
        "shares": shares,
        "discountRatePct": ke_pct,
        "costOfEquityPct": ke_pct,
        "costOfDebtPct": kd_pct,
        "terminalGrowthPct": terminal_growth * 100.0,
        "taxRatePct": tax_rate * 100.0,
        "pvFcfeMn": pv_sum_mn,
        "netCashEffectiveMn": net_cash_effective / 1_000_000.0,
        "equityValueMn": equity_value / 1_000_000.0,
        "price": price,
        "segmentOrder": seg_order,
        "segmentWeights": seg_weights,
        "rows": rows,
    }


def compute_dcf_price_fcfe(
    ticker: str,
    fundamentals: Dict[str, Any],
    history: Dict[str, Any],
    prices_snapshot: Dict[str, Any],
    dcf_cfg_for_ticker: Dict[str, Any],
    market_inputs: Dict[str, Any],
    case_name: str,
) -> Optional[float]:
    det = compute_dcf_case_fcfe(
        ticker, fundamentals, history, prices_snapshot, dcf_cfg_for_ticker, market_inputs, case_name
    )
    if not det:
        return None
    return det.get("price")


# ---------------- Multiples (unchanged logic, but safer universe handling) ----------------

def _dedupe_preserve_order(items):
    seen = set()
    out = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

def compute_multiples_range(ticker, fundamentals, history, comps_cfg):
    universe = comps_cfg.get(ticker)
    if not universe:
        universe = [ticker]
    else:
        if not isinstance(universe, list):
            universe = list(universe)
        universe = [u for u in universe if isinstance(u, str) and u.strip()]
        universe = _dedupe_preserve_order(universe)
        if ticker not in universe:
            universe.append(ticker)

    comps_data = []
    for sym in universe:
        f = fundamentals.get(sym, {}) or {}
        price = latest_price_from_history(history, sym) or 0.0
        mkt_cap = f.get("marketCap") or 0
        rev = f.get("revenueTtm") or 0
        ebitda = f.get("ebitdaTtm") or 0
        pe = f.get("pe") or 0
        cash = f.get("cash") or 0
        debt = f.get("totalDebt") or f.get("debt") or 0

        ev_raw = mkt_cap + debt - cash
        ev_sales = ev_raw / rev if ev_raw > 0 and rev > 0 else float("nan")
        ev_ebitda = ev_raw / ebitda if ev_raw > 0 and ebitda > 0 else float("nan")

        comps_data.append({
            "sym": sym, "price": price, "mktCap": mkt_cap, "rev": rev, "ebitda": ebitda,
            "pe": pe, "cash": cash, "debt": debt, "evRaw": ev_raw,
            "evSales": ev_sales, "evEbitda": ev_ebitda,
        })

    root_row = next((d for d in comps_data if d["sym"] == ticker), None)
    if not root_row or not root_row["price"] or not root_row["mktCap"]:
        print(f"[WARN] Missing price or market cap for {ticker}, skipping multiples.")
        return None

    price_root = root_row["price"]
    mkt_cap_root = root_row["mktCap"]
    rev_root = root_row["rev"]
    ebitda_root = root_row["ebitda"]
    pe_root = root_row["pe"]
    cash_root = root_row["cash"]
    debt_root = root_row["debt"]

    shares = mkt_cap_root / price_root if price_root > 0 else None
    if not shares or shares <= 0:
        print(f"[WARN] Invalid shares for {ticker}, skipping multiples.")
        return None

    net_cash = cash_root - debt_root
    net_cash_per_share = net_cash / shares
    cash_per_share = cash_root / shares if shares > 0 else 0.0

    ev_sales_pairs = [
        (d["sym"], d["evSales"])
        for d in comps_data
        if d["sym"] != ticker and isinstance(d["evSales"], (int, float)) and math.isfinite(d["evSales"])
    ]
    ev_ebitda_pairs = [
        (d["sym"], d["evEbitda"])
        for d in comps_data
        if d["sym"] != ticker and isinstance(d["evEbitda"], (int, float)) and math.isfinite(d["evEbitda"])
    ]
    pe_pairs = [
        (d["sym"], d["pe"])
        for d in comps_data
        if d["sym"] != ticker and isinstance(d["pe"], (int, float)) and d["pe"] > 0
    ]

    outliers_ev_sales = detect_outliers_iqr(ev_sales_pairs)
    outliers_ev_ebitda = detect_outliers_iqr(ev_ebitda_pairs)
    outliers_pe = detect_outliers_iqr(pe_pairs)

    implied_prices_all = []

    # EV/Sales
    peer_ev_sales_vals = [
        d["evSales"] for d in comps_data
        if d["sym"] != ticker
        and isinstance(d["evSales"], (int, float)) and math.isfinite(d["evSales"])
        and d["sym"] not in outliers_ev_sales
    ]
    if rev_root > 0 and shares > 0 and peer_ev_sales_vals:
        avg_ev_sales, med_ev_sales = avg_and_median(peer_ev_sales_vals)
        revenue_per_share = rev_root / shares
        implied_avg_ev_per_share = avg_ev_sales * revenue_per_share
        implied_med_ev_per_share = med_ev_sales * revenue_per_share
        current_ev_per_share = price_root - net_cash_per_share
        _, _, chosen_ev_per_share = pick_implied(
            implied_avg_ev_per_share, implied_med_ev_per_share, current_ev_per_share,
            avg_ev_sales, med_ev_sales
        )
        if chosen_ev_per_share is not None:
            equity_price = chosen_ev_per_share + net_cash_per_share
            if math.isfinite(equity_price) and equity_price > 0:
                implied_prices_all.append(equity_price)

    # EV/EBITDA
    peer_ev_ebitda_vals = [
        d["evEbitda"] for d in comps_data
        if d["sym"] != ticker
        and isinstance(d["evEbitda"], (int, float)) and math.isfinite(d["evEbitda"])
        and d["sym"] not in outliers_ev_ebitda
    ]
    if ebitda_root > 0 and shares > 0 and peer_ev_ebitda_vals:
        avg_ev_ebitda, med_ev_ebitda = avg_and_median(peer_ev_ebitda_vals)
        ebitda_per_share = ebitda_root / shares
        implied_avg_ev_per_share = avg_ev_ebitda * ebitda_per_share
        implied_med_ev_per_share = med_ev_ebitda * ebitda_per_share
        current_ev_per_share = price_root - net_cash_per_share
        _, _, chosen_ev_per_share = pick_implied(
            implied_avg_ev_per_share, implied_med_ev_per_share, current_ev_per_share,
            avg_ev_ebitda, med_ev_ebitda
        )
        if chosen_ev_per_share is not None:
            equity_price = chosen_ev_per_share + net_cash_per_share
            if math.isfinite(equity_price) and equity_price > 0:
                implied_prices_all.append(equity_price)

    # P/E
    peer_pe_vals = [
        d["pe"] for d in comps_data
        if d["sym"] != ticker
        and isinstance(d["pe"], (int, float)) and d["pe"] > 0
        and d["sym"] not in outliers_pe
    ]
    if pe_root and pe_root > 0 and peer_pe_vals:
        avg_pe, med_pe = avg_and_median(peer_pe_vals)
        eps = price_root / pe_root
        implied_avg_price = avg_pe * eps
        implied_med_price = med_pe * eps
        _, _, chosen_price = pick_implied(implied_avg_price, implied_med_price, price_root, avg_pe, med_pe)
        if chosen_price is not None and math.isfinite(chosen_price) and chosen_price > 0:
            implied_prices_all.append(chosen_price)

    if not implied_prices_all:
        print(f"[WARN] No implied prices could be computed for {ticker}")
        return {
            "spot": price_root,
            "netCashPerShare": round(net_cash_per_share, 2),
            "cashPerShare": round(cash_per_share, 2),
            "low": None,
            "high": None,
        }

    low = round(min(implied_prices_all), 2)
    high = round(max(implied_prices_all), 2)

    return {
        "spot": price_root,
        "netCashPerShare": round(net_cash_per_share, 2),
        "cashPerShare": round(cash_per_share, 2),
        "low": low,
        "high": high,
    }


# ---------------- Central estimate selection ----------------

def choose_central_estimate(ticker, comps_info, dcf_base, dcf_bull):
    def has(x):
        return isinstance(x, (int, float)) and math.isfinite(x) and x > 0

    comps_low = comps_info.get("low") if comps_info else None
    comps_high = comps_info.get("high") if comps_info else None

    if has(comps_low) and has(comps_high) and has(dcf_bull):
        mid = 0.5 * (comps_low + comps_high)
        central = 0.5 * (mid + dcf_bull)
        source = "blend(multiples_midpoint, dcf_bull)"
    elif has(dcf_bull):
        central = dcf_bull
        source = "bull_dcf"
    elif has(comps_low) and has(comps_high):
        central = 0.5 * (comps_low + comps_high)
        source = "multiples_midpoint"
    elif has(dcf_base):
        central = dcf_base
        source = "base_dcf"
    else:
        return None, "insufficient_data"

    # round to nearest $0.50
    central = round(central * 2.0) / 2.0
    return central, source


# ---------------- Main ----------------

def main():
    fundamentals = load_json(FUNDAMENTALS_FILE, {})
    history = load_json(PRICES_HISTORY_FILE, {})
    prices_snapshot = load_json(PRICES_FILE, {})
    dcf_config = load_json(DCF_CONFIG_FILE, {})
    comps_config = load_json(COMPS_CONFIG_FILE, {})

    # Market inputs: update (risk-free) and persist
    market_inputs = load_json(MARKET_INPUTS_FILE, {})
    market_inputs = update_market_inputs(market_inputs)
    save_json(MARKET_INPUTS_FILE, market_inputs)

    tickers = sorted(set(dcf_config.keys()) | set(comps_config.keys()))
    if not tickers:
        print("[WARN] No tickers found in dcf_config.json or comps_config.json.")
        return

    now_iso = iso_now_z()
    out: Dict[str, Any] = {}
    dcf_details_out: Dict[str, Any] = {}

    for ticker in tickers:
        print(f"\n=== Computing thesis target for {ticker} ===")

        comps_info = compute_multiples_range(ticker, fundamentals, history, comps_config)
        if comps_info:
            print(f" -> multiples range: {comps_info['low']} – {comps_info['high']} (if not None)")
        else:
            print(" -> multiples range: n/a")

        dcf_cfg_for_ticker = dcf_config.get(ticker, {}) or {}
        model = get_cash_flow_model(dcf_cfg_for_ticker)

        case_details = {}
        for case_name in ("base", "bull", "bear"):
            det = compute_dcf_case_fcfe(
                ticker, fundamentals, history, prices_snapshot, dcf_cfg_for_ticker, market_inputs, case_name
            )
            if det:
                # keep stable precision in the files
                det["price"] = round(float(det["price"]), 4)
                case_details[case_name] = det

        dcf_base = case_details.get("base", {}).get("price") if case_details.get("base") else None
        dcf_bull = case_details.get("bull", {}).get("price") if case_details.get("bull") else None
        dcf_bear = case_details.get("bear", {}).get("price") if case_details.get("bear") else None

        if case_details:
            dcf_details_out[ticker] = {
                "ticker": ticker,
                "updatedAt": now_iso,
                "cashFlowModel": model,
                "cases": case_details,
            }

        print(f" -> dcfBase: {dcf_base:.2f}" if dcf_base is not None else " -> dcfBase: n/a")
        print(f" -> dcfBull: {dcf_bull:.2f}" if dcf_bull is not None else " -> dcfBull: n/a")

        spot = comps_info["spot"] if comps_info and comps_info.get("spot") is not None else spot_price(history, prices_snapshot, ticker)

        central, source = choose_central_estimate(ticker, comps_info, dcf_base, dcf_bull)
        print(f" -> centralEstimate: {central} (source={source})")

        row: Dict[str, Any] = {
            "ticker": ticker,
            "updatedAt": now_iso,
            "centralEstimate": central,
            "source": source,
            "spot": spot,
            "dcfModel": model,
        }

        if comps_info:
            row["compsLow"] = comps_info.get("low")
            row["compsHigh"] = comps_info.get("high")
            row["netCashPerShare"] = comps_info.get("netCashPerShare")
            row["cashPerShare"] = comps_info.get("cashPerShare")

        if dcf_base is not None:
            row["dcfBase"] = round(dcf_base, 2)
        if dcf_bull is not None:
            row["dcfBull"] = round(dcf_bull, 2)
        if dcf_bear is not None:
            row["dcfBear"] = round(dcf_bear, 2)

        out[ticker] = row

    save_json(THESIS_TARGETS_FILE, out)
    save_json(DCF_DETAILS_FILE, dcf_details_out)
    print(f"Wrote dcf_details.json to {DCF_DETAILS_FILE}")
    print(f"\nWrote updated thesis_targets.json to {THESIS_TARGETS_FILE}")
    print(f"Wrote/updated market_inputs.json to {MARKET_INPUTS_FILE}")

if __name__ == "__main__":
    main()
>>>>>>> eedc683f54f207075398e339bfe5f027bc8bfa38
