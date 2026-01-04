<<<<<<< HEAD
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

# Prefer env var so you don't hardcode secrets into the repo
API_KEY = os.getenv("ALPHAVANTAGE_API_KEY") or "Y0X9MB5C8T3VW67R"

ROOT = Path(__file__).resolve().parent.parent
FUNDAMENTALS_FILE = ROOT / "fundamentals.json"
import json
from pathlib import Path

def _load_comps_config() -> dict:
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "comps_config.json",
    ]

    for p in candidates:
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)

    raise FileNotFoundError(
        "Could not find comps_config.json. Tried: " + ", ".join(str(p) for p in candidates)
    )

def _build_universe_from_comps(comps_cfg: dict) -> list[str]:
    seen = set()
    ordered = []

    def add(x):
        if x is None:
            return
        t = str(x).strip().upper()
        if not t or t in seen:
            return
        seen.add(t)
        ordered.append(t)

    # preserve JSON order (Python 3.7+ keeps insertion order)
    for base, peers in (comps_cfg or {}).items():
        add(base)
        if isinstance(peers, list):
            for p in peers:
                add(p)

    return ordered

# ✅ Pull tickers directly from comps_config.json
COMPS_CONFIG = _load_comps_config()
TICKERS = _build_universe_from_comps(COMPS_CONFIG)

# Free-tier friendly pacing:
SLEEP_BETWEEN_CALLS_SEC = 20
SLEEP_BETWEEN_TICKERS_SEC = 20



# ------------------- Low-level fetch helpers ----------------------------

def _load_json_from_url(url: str) -> dict:
    with urlopen(url) as resp:
        return json.load(resp)


def _raise_if_bad(symbol: str, data: dict, tag: str):
    if "Note" in data:
        raise RuntimeError(f"[{tag}] Note for {symbol}: {data['Note']}")
    if "Information" in data:
        raise RuntimeError(f"[{tag}] Information for {symbol}: {data['Information']}")
    if "Error Message" in data:
        raise RuntimeError(f"[{tag}] Error for {symbol}: {data['Error Message']}")


def fetch_overview(symbol: str) -> dict:
    url = (
        "https://www.alphavantage.co/query"
        f"?function=OVERVIEW&symbol={symbol}&apikey={API_KEY}"
    )
    data = _load_json_from_url(url)
    _raise_if_bad(symbol, data, "OVERVIEW")
    if "Symbol" not in data:
        raise RuntimeError(f"[OVERVIEW] Unexpected response for {symbol}: {data}")
    return data


def fetch_balance_sheet(symbol: str) -> dict:
    url = (
        "https://www.alphavantage.co/query"
        f"?function=BALANCE_SHEET&symbol={symbol}&apikey={API_KEY}"
    )
    data = _load_json_from_url(url)
    _raise_if_bad(symbol, data, "BALANCE_SHEET")
    return data


def fetch_income_statement(symbol: str) -> dict:
    url = (
        "https://www.alphavantage.co/query"
        f"?function=INCOME_STATEMENT&symbol={symbol}&apikey={API_KEY}"
    )
    data = _load_json_from_url(url)
    _raise_if_bad(symbol, data, "INCOME_STATEMENT")
    return data


# ------------------- Parsing helpers ------------------------------------

def to_int(s):
    try:
        if s in (None, "", "None"):
            return 0
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def to_float(s):
    try:
        if s in (None, "", "None"):
            return 0.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def safe_div(a, b):
    if b is None or b == 0:
        return None
    return a / b


def _first_nonzero_int(d: dict, keys: list[str]) -> int:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "None"):
            x = to_int(v)
            if x:
                return x
    return 0


def _get_reports(data: dict) -> list[dict]:
    return data.get("quarterlyReports") or data.get("annualReports") or []


def parse_balance_sheet_report(bs_report: dict) -> dict:
    """
    Normalize cash + debt components from a single balance sheet report.
    """
    cash = _first_nonzero_int(
        bs_report,
        [
            "cashAndCashEquivalentsAtCarryingValue",
            "cashAndShortTermInvestments",
            "cash",
        ],
    )

    short_debt = _first_nonzero_int(bs_report, ["currentDebt", "shortTermDebt"])
    long_debt = _first_nonzero_int(bs_report, ["longTermDebtNoncurrent", "longTermDebt"])

    # Prefer total if available, else build
    total_debt = _first_nonzero_int(bs_report, ["shortLongTermDebtTotal"])
    if total_debt == 0:
        total_debt = short_debt + long_debt

    return {
        "cash": cash,
        "shortTermDebt": short_debt,
        "longTermDebt": long_debt,
        "totalDebt": total_debt,
        "fiscalDateEnding": bs_report.get("fiscalDateEnding") or None,
    }


def sum_ttm_from_quarters(reports: list[dict], field: str) -> int:
    """
    Sum last 4 quarters for a field (newest first). Returns 0 if insufficient or missing.
    """
    if not reports or len(reports) < 4:
        return 0
    total = 0
    for r in reports[:4]:
        total += to_int(r.get(field))
    return total


def latest_annual_value(reports: list[dict], field: str) -> int:
    """
    Take latest annual report value as fallback (not truly TTM but better than 0).
    """
    if not reports:
        return 0
    return to_int(reports[0].get(field))


def parse_income_ttm(is_data: dict) -> dict:
    """
    Try to compute TTM values by summing last 4 quarters. If quarterly missing,
    fallback to latest annual.
    """
    q = is_data.get("quarterlyReports") or []
    a = is_data.get("annualReports") or []

    def ttm(field: str) -> int:
        v = sum_ttm_from_quarters(q, field)
        if v != 0:
            return v
        return latest_annual_value(a, field)

    # Alpha Vantage fields often exist under these names:
    interest_raw = ttm("interestExpense")
    # Sometimes interestExpense is negative (as a cost). Normalize to positive expense.
    interest_expense = abs(interest_raw)

    income_before_tax = ttm("incomeBeforeTax")
    income_tax_expense_raw = ttm("incomeTaxExpense")
    # Tax expense can be negative in some datasets; keep sign for effective rate logic,
    # but also store raw
    income_tax_expense = income_tax_expense_raw

    net_income = ttm("netIncome")
    ebit = ttm("ebit")
    operating_income = ttm("operatingIncome")

    eff_tax = None
    if income_before_tax and income_before_tax > 0:
        eff_tax = income_tax_expense / income_before_tax
        # Clamp to sensible range if garbage comes back
        if not math.isfinite(eff_tax):
            eff_tax = None

    # Best available period end for the newest quarter included
    ttm_end = None
    if q and len(q) >= 1:
        ttm_end = q[0].get("fiscalDateEnding") or None
    elif a:
        ttm_end = a[0].get("fiscalDateEnding") or None

    return {
        "interestExpenseTtm": interest_expense,  # positive number
        "interestExpenseTtmRaw": interest_raw,
        "incomeBeforeTaxTtm": income_before_tax,
        "incomeTaxExpenseTtm": income_tax_expense,
        "netIncomeTtm": net_income,
        "ebitTtm": ebit,
        "operatingIncomeTtm": operating_income,
        "effectiveTaxRateTtm": eff_tax,  # decimal (e.g. 0.23) or None
        "fiscalDateEndingIncomeTtm": ttm_end,
    }


# ------------------- Main script ----------------------------------------

def main():
    if API_KEY == "REPLACE_ME":
        raise RuntimeError(
            "Missing Alpha Vantage API key. Set ALPHAVANTAGE_API_KEY in your environment "
            "or replace API_KEY in this script (not recommended)."
        )

    # Preserve existing file if partial failure
    if FUNDAMENTALS_FILE.exists():
        try:
            with FUNDAMENTALS_FILE.open("r", encoding="utf-8") as f:
                out = json.load(f)
        except json.JSONDecodeError:
            print("[WARN] Existing fundamentals.json is invalid. Starting fresh.")
            out = {}
    else:
        out = {}

    now_iso = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    rate_limited = False

    for i, sym in enumerate(TICKERS):
        print(f"=== {sym} ===")

        # -------- OVERVIEW --------
        try:
            print(f"Fetching OVERVIEW for {sym}...")
            ov = fetch_overview(sym)
        except RuntimeError as e:
            msg = str(e)
            print(f"[WARN] Skipping {sym} OVERVIEW: {msg}")
            if any(x in msg.lower() for x in ("note", "information", "limit")):
                rate_limited = True
            continue

        time.sleep(SLEEP_BETWEEN_CALLS_SEC)

        # -------- BALANCE SHEET (latest + prior for avg debt) --------
        bs_latest = {}
        bs_prev = {}
        bs_parsed_latest = {"cash": 0, "shortTermDebt": 0, "longTermDebt": 0, "totalDebt": 0, "fiscalDateEnding": None}
        bs_parsed_prev = {"cash": 0, "shortTermDebt": 0, "longTermDebt": 0, "totalDebt": 0, "fiscalDateEnding": None}

        try:
            print(f"Fetching BALANCE_SHEET for {sym}...")
            bs_data = fetch_balance_sheet(sym)
            reports = _get_reports(bs_data)
            if reports:
                bs_latest = reports[0]
                bs_parsed_latest = parse_balance_sheet_report(bs_latest)
            if len(reports) >= 2:
                bs_prev = reports[1]
                bs_parsed_prev = parse_balance_sheet_report(bs_prev)
        except RuntimeError as e:
            msg = str(e)
            print(f"[WARN] Could not fetch balance sheet for {sym}: {msg}")
            if any(x in msg.lower() for x in ("note", "information", "limit")):
                rate_limited = True

        time.sleep(SLEEP_BETWEEN_CALLS_SEC)

        # -------- INCOME STATEMENT (TTM interest + tax fields) --------
        income_ttm = {
            "interestExpenseTtm": 0,
            "interestExpenseTtmRaw": 0,
            "incomeBeforeTaxTtm": 0,
            "incomeTaxExpenseTtm": 0,
            "netIncomeTtm": 0,
            "ebitTtm": 0,
            "operatingIncomeTtm": 0,
            "effectiveTaxRateTtm": None,
            "fiscalDateEndingIncomeTtm": None,
        }

        try:
            print(f"Fetching INCOME_STATEMENT for {sym}...")
            is_data = fetch_income_statement(sym)
            income_ttm = parse_income_ttm(is_data)
        except RuntimeError as e:
            msg = str(e)
            print(f"[WARN] Could not fetch income statement for {sym}: {msg}")
            if any(x in msg.lower() for x in ("note", "information", "limit")):
                rate_limited = True

        # -------- Derived FCFE helpers --------
        debt_latest = bs_parsed_latest["totalDebt"]
        debt_prev = bs_parsed_prev["totalDebt"]
        avg_debt_2q = None
        if debt_latest > 0 and debt_prev > 0:
            avg_debt_2q = 0.5 * (debt_latest + debt_prev)
        elif debt_latest > 0:
            avg_debt_2q = float(debt_latest)

        implied_kd_pct = None
        if avg_debt_2q and avg_debt_2q > 0 and income_ttm["interestExpenseTtm"] > 0:
            implied_kd_pct = 100.0 * (income_ttm["interestExpenseTtm"] / avg_debt_2q)
            if not math.isfinite(implied_kd_pct) or implied_kd_pct <= 0:
                implied_kd_pct = None

        market_cap = to_int(ov.get("MarketCapitalization"))
        shares_out = to_int(ov.get("SharesOutstanding"))
        beta = to_float(ov.get("Beta"))

        cash = bs_parsed_latest["cash"]
        total_debt = bs_parsed_latest["totalDebt"]
        net_cash = cash - total_debt

        cash_ps = debt_ps = net_cash_ps = None
        if shares_out and shares_out > 0:
            cash_ps = cash / shares_out
            debt_ps = total_debt / shares_out
            net_cash_ps = net_cash / shares_out

        # Data quality flags to help downstream logic be strict (FCFE-only)
        flags = []
        if shares_out <= 0:
            flags.append("missing_shares_outstanding")
        if beta <= 0:
            flags.append("missing_or_bad_beta")
        if total_debt < 0:
            flags.append("bad_debt_value")
        if income_ttm["interestExpenseTtm"] <= 0:
            flags.append("missing_interest_expense_ttm")

        out[sym] = {
            "name": ov.get("Name", sym),
            "currency": ov.get("Currency", "USD"),

            # Core valuation inputs
            "marketCap": market_cap,
            "sharesOutstanding": shares_out,
            "beta": beta,

            # TTM operating scale
            "revenueTtm": to_int(ov.get("RevenueTTM")),
            "ebitdaTtm": to_int(ov.get("EBITDA")),
            "pe": to_float(ov.get("PERatio")),

            # Balance sheet
            "cash": cash,
            "shortTermDebt": bs_parsed_latest["shortTermDebt"],
            "longTermDebt": bs_parsed_latest["longTermDebt"],
            "totalDebt": total_debt,
            "netCash": net_cash,

            # Per-share convenience (if shares available)
            "cashPerShare": cash_ps,
            "debtPerShare": debt_ps,
            "netCashPerShare": net_cash_ps,

            # FCFE-specific (interest + implied cost of debt)
            **income_ttm,
            "avgDebt2Q": avg_debt_2q,
            "impliedCostOfDebtPct": implied_kd_pct,

            # Metadata
            "fiscalDateEndingBalanceSheet": bs_parsed_latest["fiscalDateEnding"],
            "updatedAt": now_iso,
            "dataQualityFlags": flags,
        }

        # Delay before next ticker
        if i < len(TICKERS) - 1:
            time.sleep(SLEEP_BETWEEN_TICKERS_SEC)

    with FUNDAMENTALS_FILE.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")

    print("Updated fundamentals.json")

    if rate_limited:
        print(
            "\n[WARN] Some requests appear to have hit Alpha Vantage rate limits.\n"
            "      Not all tickers may have fully updated fields.\n"
            "      Re-run later or reduce tickers / increase sleep."
        )


if __name__ == "__main__":
    main()
=======
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

# Prefer env var so you don't hardcode secrets into the repo
API_KEY = os.getenv("ALPHAVANTAGE_API_KEY") or "Y0X9MB5C8T3VW67R"

ROOT = Path(__file__).resolve().parent.parent
FUNDAMENTALS_FILE = ROOT / "fundamentals.json"
import json
from pathlib import Path

def _load_comps_config() -> dict:
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "comps_config.json",
    ]

    for p in candidates:
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)

    raise FileNotFoundError(
        "Could not find comps_config.json. Tried: " + ", ".join(str(p) for p in candidates)
    )

def _build_universe_from_comps(comps_cfg: dict) -> list[str]:
    seen = set()
    ordered = []

    def add(x):
        if x is None:
            return
        t = str(x).strip().upper()
        if not t or t in seen:
            return
        seen.add(t)
        ordered.append(t)

    # preserve JSON order (Python 3.7+ keeps insertion order)
    for base, peers in (comps_cfg or {}).items():
        add(base)
        if isinstance(peers, list):
            for p in peers:
                add(p)

    return ordered

# ✅ Pull tickers directly from comps_config.json
COMPS_CONFIG = _load_comps_config()
TICKERS = _build_universe_from_comps(COMPS_CONFIG)

# Free-tier friendly pacing:
SLEEP_BETWEEN_CALLS_SEC = 20
SLEEP_BETWEEN_TICKERS_SEC = 20



# ------------------- Low-level fetch helpers ----------------------------

def _load_json_from_url(url: str) -> dict:
    with urlopen(url) as resp:
        return json.load(resp)


def _raise_if_bad(symbol: str, data: dict, tag: str):
    if "Note" in data:
        raise RuntimeError(f"[{tag}] Note for {symbol}: {data['Note']}")
    if "Information" in data:
        raise RuntimeError(f"[{tag}] Information for {symbol}: {data['Information']}")
    if "Error Message" in data:
        raise RuntimeError(f"[{tag}] Error for {symbol}: {data['Error Message']}")


def fetch_overview(symbol: str) -> dict:
    url = (
        "https://www.alphavantage.co/query"
        f"?function=OVERVIEW&symbol={symbol}&apikey={API_KEY}"
    )
    data = _load_json_from_url(url)
    _raise_if_bad(symbol, data, "OVERVIEW")
    if "Symbol" not in data:
        raise RuntimeError(f"[OVERVIEW] Unexpected response for {symbol}: {data}")
    return data


def fetch_balance_sheet(symbol: str) -> dict:
    url = (
        "https://www.alphavantage.co/query"
        f"?function=BALANCE_SHEET&symbol={symbol}&apikey={API_KEY}"
    )
    data = _load_json_from_url(url)
    _raise_if_bad(symbol, data, "BALANCE_SHEET")
    return data


def fetch_income_statement(symbol: str) -> dict:
    url = (
        "https://www.alphavantage.co/query"
        f"?function=INCOME_STATEMENT&symbol={symbol}&apikey={API_KEY}"
    )
    data = _load_json_from_url(url)
    _raise_if_bad(symbol, data, "INCOME_STATEMENT")
    return data


# ------------------- Parsing helpers ------------------------------------

def to_int(s):
    try:
        if s in (None, "", "None"):
            return 0
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def to_float(s):
    try:
        if s in (None, "", "None"):
            return 0.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def safe_div(a, b):
    if b is None or b == 0:
        return None
    return a / b


def _first_nonzero_int(d: dict, keys: list[str]) -> int:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "None"):
            x = to_int(v)
            if x:
                return x
    return 0


def _get_reports(data: dict) -> list[dict]:
    return data.get("quarterlyReports") or data.get("annualReports") or []


def parse_balance_sheet_report(bs_report: dict) -> dict:
    """
    Normalize cash + debt components from a single balance sheet report.
    """
    cash = _first_nonzero_int(
        bs_report,
        [
            "cashAndCashEquivalentsAtCarryingValue",
            "cashAndShortTermInvestments",
            "cash",
        ],
    )

    short_debt = _first_nonzero_int(bs_report, ["currentDebt", "shortTermDebt"])
    long_debt = _first_nonzero_int(bs_report, ["longTermDebtNoncurrent", "longTermDebt"])

    # Prefer total if available, else build
    total_debt = _first_nonzero_int(bs_report, ["shortLongTermDebtTotal"])
    if total_debt == 0:
        total_debt = short_debt + long_debt

    return {
        "cash": cash,
        "shortTermDebt": short_debt,
        "longTermDebt": long_debt,
        "totalDebt": total_debt,
        "fiscalDateEnding": bs_report.get("fiscalDateEnding") or None,
    }


def sum_ttm_from_quarters(reports: list[dict], field: str) -> int:
    """
    Sum last 4 quarters for a field (newest first). Returns 0 if insufficient or missing.
    """
    if not reports or len(reports) < 4:
        return 0
    total = 0
    for r in reports[:4]:
        total += to_int(r.get(field))
    return total


def latest_annual_value(reports: list[dict], field: str) -> int:
    """
    Take latest annual report value as fallback (not truly TTM but better than 0).
    """
    if not reports:
        return 0
    return to_int(reports[0].get(field))


def parse_income_ttm(is_data: dict) -> dict:
    """
    Try to compute TTM values by summing last 4 quarters. If quarterly missing,
    fallback to latest annual.
    """
    q = is_data.get("quarterlyReports") or []
    a = is_data.get("annualReports") or []

    def ttm(field: str) -> int:
        v = sum_ttm_from_quarters(q, field)
        if v != 0:
            return v
        return latest_annual_value(a, field)

    # Alpha Vantage fields often exist under these names:
    interest_raw = ttm("interestExpense")
    # Sometimes interestExpense is negative (as a cost). Normalize to positive expense.
    interest_expense = abs(interest_raw)

    income_before_tax = ttm("incomeBeforeTax")
    income_tax_expense_raw = ttm("incomeTaxExpense")
    # Tax expense can be negative in some datasets; keep sign for effective rate logic,
    # but also store raw
    income_tax_expense = income_tax_expense_raw

    net_income = ttm("netIncome")
    ebit = ttm("ebit")
    operating_income = ttm("operatingIncome")

    eff_tax = None
    if income_before_tax and income_before_tax > 0:
        eff_tax = income_tax_expense / income_before_tax
        # Clamp to sensible range if garbage comes back
        if not math.isfinite(eff_tax):
            eff_tax = None

    # Best available period end for the newest quarter included
    ttm_end = None
    if q and len(q) >= 1:
        ttm_end = q[0].get("fiscalDateEnding") or None
    elif a:
        ttm_end = a[0].get("fiscalDateEnding") or None

    return {
        "interestExpenseTtm": interest_expense,  # positive number
        "interestExpenseTtmRaw": interest_raw,
        "incomeBeforeTaxTtm": income_before_tax,
        "incomeTaxExpenseTtm": income_tax_expense,
        "netIncomeTtm": net_income,
        "ebitTtm": ebit,
        "operatingIncomeTtm": operating_income,
        "effectiveTaxRateTtm": eff_tax,  # decimal (e.g. 0.23) or None
        "fiscalDateEndingIncomeTtm": ttm_end,
    }


# ------------------- Main script ----------------------------------------

def main():
    if API_KEY == "REPLACE_ME":
        raise RuntimeError(
            "Missing Alpha Vantage API key. Set ALPHAVANTAGE_API_KEY in your environment "
            "or replace API_KEY in this script (not recommended)."
        )

    # Preserve existing file if partial failure
    if FUNDAMENTALS_FILE.exists():
        try:
            with FUNDAMENTALS_FILE.open("r", encoding="utf-8") as f:
                out = json.load(f)
        except json.JSONDecodeError:
            print("[WARN] Existing fundamentals.json is invalid. Starting fresh.")
            out = {}
    else:
        out = {}

    now_iso = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    rate_limited = False

    for i, sym in enumerate(TICKERS):
        print(f"=== {sym} ===")

        # -------- OVERVIEW --------
        try:
            print(f"Fetching OVERVIEW for {sym}...")
            ov = fetch_overview(sym)
        except RuntimeError as e:
            msg = str(e)
            print(f"[WARN] Skipping {sym} OVERVIEW: {msg}")
            if any(x in msg.lower() for x in ("note", "information", "limit")):
                rate_limited = True
            continue

        time.sleep(SLEEP_BETWEEN_CALLS_SEC)

        # -------- BALANCE SHEET (latest + prior for avg debt) --------
        bs_latest = {}
        bs_prev = {}
        bs_parsed_latest = {"cash": 0, "shortTermDebt": 0, "longTermDebt": 0, "totalDebt": 0, "fiscalDateEnding": None}
        bs_parsed_prev = {"cash": 0, "shortTermDebt": 0, "longTermDebt": 0, "totalDebt": 0, "fiscalDateEnding": None}

        try:
            print(f"Fetching BALANCE_SHEET for {sym}...")
            bs_data = fetch_balance_sheet(sym)
            reports = _get_reports(bs_data)
            if reports:
                bs_latest = reports[0]
                bs_parsed_latest = parse_balance_sheet_report(bs_latest)
            if len(reports) >= 2:
                bs_prev = reports[1]
                bs_parsed_prev = parse_balance_sheet_report(bs_prev)
        except RuntimeError as e:
            msg = str(e)
            print(f"[WARN] Could not fetch balance sheet for {sym}: {msg}")
            if any(x in msg.lower() for x in ("note", "information", "limit")):
                rate_limited = True

        time.sleep(SLEEP_BETWEEN_CALLS_SEC)

        # -------- INCOME STATEMENT (TTM interest + tax fields) --------
        income_ttm = {
            "interestExpenseTtm": 0,
            "interestExpenseTtmRaw": 0,
            "incomeBeforeTaxTtm": 0,
            "incomeTaxExpenseTtm": 0,
            "netIncomeTtm": 0,
            "ebitTtm": 0,
            "operatingIncomeTtm": 0,
            "effectiveTaxRateTtm": None,
            "fiscalDateEndingIncomeTtm": None,
        }

        try:
            print(f"Fetching INCOME_STATEMENT for {sym}...")
            is_data = fetch_income_statement(sym)
            income_ttm = parse_income_ttm(is_data)
        except RuntimeError as e:
            msg = str(e)
            print(f"[WARN] Could not fetch income statement for {sym}: {msg}")
            if any(x in msg.lower() for x in ("note", "information", "limit")):
                rate_limited = True

        # -------- Derived FCFE helpers --------
        debt_latest = bs_parsed_latest["totalDebt"]
        debt_prev = bs_parsed_prev["totalDebt"]
        avg_debt_2q = None
        if debt_latest > 0 and debt_prev > 0:
            avg_debt_2q = 0.5 * (debt_latest + debt_prev)
        elif debt_latest > 0:
            avg_debt_2q = float(debt_latest)

        implied_kd_pct = None
        if avg_debt_2q and avg_debt_2q > 0 and income_ttm["interestExpenseTtm"] > 0:
            implied_kd_pct = 100.0 * (income_ttm["interestExpenseTtm"] / avg_debt_2q)
            if not math.isfinite(implied_kd_pct) or implied_kd_pct <= 0:
                implied_kd_pct = None

        market_cap = to_int(ov.get("MarketCapitalization"))
        shares_out = to_int(ov.get("SharesOutstanding"))
        beta = to_float(ov.get("Beta"))

        cash = bs_parsed_latest["cash"]
        total_debt = bs_parsed_latest["totalDebt"]
        net_cash = cash - total_debt

        cash_ps = debt_ps = net_cash_ps = None
        if shares_out and shares_out > 0:
            cash_ps = cash / shares_out
            debt_ps = total_debt / shares_out
            net_cash_ps = net_cash / shares_out

        # Data quality flags to help downstream logic be strict (FCFE-only)
        flags = []
        if shares_out <= 0:
            flags.append("missing_shares_outstanding")
        if beta <= 0:
            flags.append("missing_or_bad_beta")
        if total_debt < 0:
            flags.append("bad_debt_value")
        if income_ttm["interestExpenseTtm"] <= 0:
            flags.append("missing_interest_expense_ttm")

        out[sym] = {
            "name": ov.get("Name", sym),
            "currency": ov.get("Currency", "USD"),

            # Core valuation inputs
            "marketCap": market_cap,
            "sharesOutstanding": shares_out,
            "beta": beta,

            # TTM operating scale
            "revenueTtm": to_int(ov.get("RevenueTTM")),
            "ebitdaTtm": to_int(ov.get("EBITDA")),
            "pe": to_float(ov.get("PERatio")),

            # Balance sheet
            "cash": cash,
            "shortTermDebt": bs_parsed_latest["shortTermDebt"],
            "longTermDebt": bs_parsed_latest["longTermDebt"],
            "totalDebt": total_debt,
            "netCash": net_cash,

            # Per-share convenience (if shares available)
            "cashPerShare": cash_ps,
            "debtPerShare": debt_ps,
            "netCashPerShare": net_cash_ps,

            # FCFE-specific (interest + implied cost of debt)
            **income_ttm,
            "avgDebt2Q": avg_debt_2q,
            "impliedCostOfDebtPct": implied_kd_pct,

            # Metadata
            "fiscalDateEndingBalanceSheet": bs_parsed_latest["fiscalDateEnding"],
            "updatedAt": now_iso,
            "dataQualityFlags": flags,
        }

        # Delay before next ticker
        if i < len(TICKERS) - 1:
            time.sleep(SLEEP_BETWEEN_TICKERS_SEC)

    with FUNDAMENTALS_FILE.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")

    print("Updated fundamentals.json")

    if rate_limited:
        print(
            "\n[WARN] Some requests appear to have hit Alpha Vantage rate limits.\n"
            "      Not all tickers may have fully updated fields.\n"
            "      Re-run later or reduce tickers / increase sleep."
        )


if __name__ == "__main__":
    main()
>>>>>>> eedc683f54f207075398e339bfe5f027bc8bfa38
