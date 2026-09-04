"""
quality_screen.py — the fundamental quality screen, computed rather than quoted.

THE SCREEN
----------
    Altman Z > 3, Piotroski F > 7, market cap > 500 Cr,
    operating margin > 15%, sales growth > 15%, profit growth > 15%,
    debt/equity < 1, promoter holding > 50%

WHAT THIS SOURCE CAN AND CANNOT ANSWER
--------------------------------------
Six of the eight criteria are computed here from filed statements. Two are
served with a shorter window than asked for, and one is a proxy. Those
compromises are stated on every row rather than buried:

  Altman Z          computed, five components from the balance sheet and
                    income statement. **Not meaningful for banks, NBFCs or
                    insurers** — the model was fitted on manufacturers and a
                    lender's balance sheet breaks its assumptions, so
                    financials are scored `None` rather than given a number
                    that looks comparable and is not.
  Piotroski F       computed, all nine tests, needing two consecutive years.
  Market cap        as reported.
  Debt / equity     as reported.
  Operating margin  as reported (trailing), not a ten-year average.
  Sales growth      **five-year CAGR, not ten.** This feed carries five years
  Profit growth     of statements. A ten-year figure would have to be invented.
  Promoter holding  **proxied** by the insider-held percentage. For Indian
                    listings that tracks the promoter block closely, but it is
                    not the exchange's shareholding-pattern filing and is
                    labelled as an estimate everywhere it appears.

Asking for ten years and being given five is a real difference, so the screen
reports the window it actually used and never relabels it.

WHY IT IS STAGED
----------------
2,570 NSE equities times four API calls each is tens of thousands of requests.
The funnel spends cheap calls on everything and expensive ones only on
survivors: price and liquidity first, the summary block next, and full
statements only for the handful still standing.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "quality_screen.json")
CACHE_HOURS = 20                      # statements change quarterly, not hourly

NSE_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}

CRORE = 1e7

DEFAULTS = {
    "altman_z_min": 3.0,
    "piotroski_min": 7,
    "market_cap_cr_min": 500.0,
    "operating_margin_min": 15.0,
    "sales_growth_min": 15.0,
    "profit_growth_min": 15.0,
    "debt_to_equity_max": 1.0,
    "promoter_holding_min": 50.0,
}

# Altman's model was fitted on manufacturers. A bank's balance sheet is
# mostly other people's money by design, so the ratios do not mean what the
# model assumes and the score would read as distress for healthy lenders.
FINANCIAL_SECTORS = {"Financial Services", "Financials", "Banks", "Insurance"}


def _now():
    return datetime.now(IST)


def _f(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _row(frame, name, col=0):
    """One line item from a yfinance statement frame, or None."""
    if frame is None or getattr(frame, "empty", True):
        return None
    try:
        if name not in frame.index:
            return None
        series = frame.loc[name]
        if col >= len(series):
            return None
        return _f(series.iloc[col])
    except Exception:                                              # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# the universe
# ---------------------------------------------------------------------------

def fetch_nse_list(log=None):
    """
    Every equity listed on the NSE, from the exchange's own CSV.

    BSE publishes an equivalent list but its download endpoint currently
    answers 404, and BSE-only names are overwhelmingly illiquid, so this is
    NSE for now and says so rather than implying wider coverage.
    """
    import requests
    say = log or (lambda _m: None)

    # NSE hands out cookies on its HTML pages and throttles bare hits on the
    # archive host. Priming a session and retrying is the same treatment the
    # offer-document reader needs, and for the same reason.
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get("https://www.nseindia.com/market-data/securities-available-for-trading",
                    timeout=20)
    except Exception:                                              # noqa: BLE001
        pass

    response = None
    for attempt in range(3):
        try:
            response = session.get(NSE_LIST_URL, timeout=45)
            response.raise_for_status()
            break
        except Exception as exc:                                   # noqa: BLE001
            if attempt == 2:
                say(f"NSE equity list unavailable after 3 tries ({type(exc).__name__})")
                return []
            say(f"NSE equity list attempt {attempt + 1} failed "
                f"({type(exc).__name__}) — retrying")
            time.sleep(3 * (attempt + 1))
    if response is None:
        return []

    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8", "replace"))))
    out = []
    for row in rows:
        symbol = (row.get("SYMBOL") or "").strip()
        series = (row.get(" SERIES") or row.get("SERIES") or "").strip()
        if not symbol or series != "EQ":          # EQ only: no SME, no debt series
            continue
        out.append({
            "symbol": symbol,
            "ticker": f"{symbol}.NS",
            "name": (row.get("NAME OF COMPANY") or "").strip(),
        })
    say(f"NSE equity list: {len(out)} EQ-series companies")
    return out


# ---------------------------------------------------------------------------
# the two scores
# ---------------------------------------------------------------------------

def altman_z(bs, fin, market_cap, is_financial=False):
    """
    Altman Z for a public manufacturer:

        1.2 A + 1.4 B + 3.3 C + 0.6 D + 1.0 E

    Returns None for financials rather than a misleading number, and None if
    any component is missing — a Z built from four of five terms is not a Z.
    """
    if is_financial:
        return None, "not meaningful for a lender — Altman assumes an operating balance sheet"

    total_assets = _row(bs, "Total Assets")
    if not total_assets:
        return None, "total assets unavailable"

    current_assets = _row(bs, "Current Assets")
    current_liabilities = _row(bs, "Current Liabilities")
    retained = _row(bs, "Retained Earnings")
    total_liabilities = _row(bs, "Total Liabilities Net Minority Interest")
    ebit = _row(fin, "EBIT")
    revenue = _row(fin, "Total Revenue")

    missing = [n for n, v in (("current assets", current_assets),
                              ("current liabilities", current_liabilities),
                              ("retained earnings", retained),
                              ("total liabilities", total_liabilities),
                              ("EBIT", ebit), ("revenue", revenue),
                              ("market cap", market_cap)) if v is None]
    if missing:
        return None, f"missing {', '.join(missing)}"

    working_capital = current_assets - current_liabilities
    a = working_capital / total_assets
    b = retained / total_assets
    c = ebit / total_assets
    d = (market_cap / total_liabilities) if total_liabilities else None
    e = revenue / total_assets
    if d is None:
        return None, "no liabilities figure to value equity against"

    return round(1.2 * a + 1.4 * b + 3.3 * c + 0.6 * d + 1.0 * e, 2), None


def piotroski_f(bs, fin, cf):
    """
    The nine-point F-score, needing this year and last.

    Each test is reported by name so a 7 can be inspected rather than trusted,
    and a test whose inputs are missing is not silently counted as a pass —
    it is dropped and the maximum falls with it.
    """
    tests, passed, skipped = [], 0, 0

    def check(label, condition):
        nonlocal passed, skipped
        if condition is None:
            skipped += 1
            tests.append({"test": label, "result": None})
            return
        ok = bool(condition)
        passed += 1 if ok else 0
        tests.append({"test": label, "result": ok})

    ni0, ni1 = _row(fin, "Net Income", 0), _row(fin, "Net Income", 1)
    ta0, ta1 = _row(bs, "Total Assets", 0), _row(bs, "Total Assets", 1)
    cfo0 = _row(cf, "Operating Cash Flow", 0)
    ltd0, ltd1 = _row(bs, "Long Term Debt", 0), _row(bs, "Long Term Debt", 1)
    ca0, ca1 = _row(bs, "Current Assets", 0), _row(bs, "Current Assets", 1)
    cl0, cl1 = _row(bs, "Current Liabilities", 0), _row(bs, "Current Liabilities", 1)
    gp0, gp1 = _row(fin, "Gross Profit", 0), _row(fin, "Gross Profit", 1)
    rev0, rev1 = _row(fin, "Total Revenue", 0), _row(fin, "Total Revenue", 1)
    eq0, eq1 = _row(bs, "Stockholders Equity", 0), _row(bs, "Stockholders Equity", 1)

    roa0 = (ni0 / ta0) if ni0 is not None and ta0 else None
    roa1 = (ni1 / ta1) if ni1 is not None and ta1 else None

    check("positive net income", None if ni0 is None else ni0 > 0)
    check("positive operating cash flow", None if cfo0 is None else cfo0 > 0)
    check("return on assets improving",
          None if roa0 is None or roa1 is None else roa0 > roa1)
    check("cash flow exceeds net income",
          None if cfo0 is None or ni0 is None or not ta0 else cfo0 > ni0)
    check("long-term debt reduced",
          None if ltd0 is None or ltd1 is None else ltd0 <= ltd1)
    check("current ratio improving",
          None if None in (ca0, cl0, ca1, cl1) or not cl0 or not cl1
          else (ca0 / cl0) > (ca1 / cl1))
    check("no equity dilution",
          None if eq0 is None or eq1 is None else eq0 >= eq1)
    check("gross margin improving",
          None if None in (gp0, rev0, gp1, rev1) or not rev0 or not rev1
          else (gp0 / rev0) > (gp1 / rev1))
    check("asset turnover improving",
          None if None in (rev0, ta0, rev1, ta1) or not ta0 or not ta1
          else (rev0 / ta0) > (rev1 / ta1))

    return {"score": passed, "max": 9 - skipped, "skipped": skipped, "tests": tests}


def cagr(frame, name):
    """
    Compound growth across every year this feed carries, with the span named.

    The screen asked for ten years; five is what is on file, so the window is
    returned alongside the number and the caller labels it honestly.
    """
    if frame is None or getattr(frame, "empty", True) or name not in frame.index:
        return None, None
    try:
        series = [_f(v) for v in frame.loc[name].tolist()]
    except Exception:                                              # noqa: BLE001
        return None, None
    series = [v for v in series if v is not None]
    if len(series) < 2:
        return None, None

    latest, earliest = series[0], series[-1]        # yfinance is newest-first
    years = len(series) - 1
    if earliest is None or earliest <= 0 or latest <= 0:
        return None, years
    return round(((latest / earliest) ** (1.0 / years) - 1.0) * 100.0, 1), years


# ---------------------------------------------------------------------------
# the funnel
# ---------------------------------------------------------------------------

def _import_yf():
    import yfinance as yf
    return yf


def evaluate(ticker, name, symbol, criteria, log=None):
    """
    Every criterion for one company, each with its own verdict.

    A criterion that cannot be computed is `None`, never a guess and never a
    silent pass. A company clears the screen only if every criterion was
    actually evaluated and every one of them passed.
    """
    yf = _import_yf()
    try:
        handle = yf.Ticker(ticker)
        info = handle.info or {}
    except Exception as exc:                                       # noqa: BLE001
        return {"symbol": symbol, "error": f"summary unavailable ({type(exc).__name__})"}

    sector = info.get("sector")
    is_financial = sector in FINANCIAL_SECTORS
    market_cap = _f(info.get("marketCap"))
    market_cap_cr = round(market_cap / CRORE, 1) if market_cap else None

    d2e_raw = _f(info.get("debtToEquity"))
    d2e = round(d2e_raw / 100.0, 3) if d2e_raw is not None else None   # reported as %
    margin = _f(info.get("operatingMargins"))
    margin_pct = round(margin * 100.0, 2) if margin is not None else None
    insiders = _f(info.get("heldPercentInsiders"))
    promoter_pct = round(insiders * 100.0, 2) if insiders is not None else None

    try:
        bs, fin, cf = handle.balance_sheet, handle.financials, handle.cashflow
    except Exception:                                              # noqa: BLE001
        bs = fin = cf = None

    z, z_note = altman_z(bs, fin, market_cap, is_financial=is_financial)
    f = piotroski_f(bs, fin, cf)
    sales_growth, sales_years = cagr(fin, "Total Revenue")
    profit_growth, profit_years = cagr(fin, "Net Income")

    checks = {
        "altman_z": _check(z, criteria["altman_z_min"], "gt", z_note),
        "piotroski": _check(f["score"] if f["max"] >= 7 else None,
                            criteria["piotroski_min"], "gt",
                            None if f["max"] >= 7 else
                            f"only {f['max']} of 9 tests could be computed"),
        "market_cap_cr": _check(market_cap_cr, criteria["market_cap_cr_min"], "gt"),
        "operating_margin": _check(margin_pct, criteria["operating_margin_min"], "gt"),
        "sales_growth": _check(sales_growth, criteria["sales_growth_min"], "gt"),
        "profit_growth": _check(profit_growth, criteria["profit_growth_min"], "gt"),
        "debt_to_equity": _check(d2e, criteria["debt_to_equity_max"], "lt"),
        "promoter_holding": _check(promoter_pct, criteria["promoter_holding_min"], "gt"),
    }

    evaluated = [c for c in checks.values() if c["value"] is not None]
    passes = [c for c in evaluated if c["pass"]]
    clears = len(evaluated) == len(checks) and len(passes) == len(checks)

    return {
        "symbol": symbol, "name": name, "ticker": ticker, "sector": sector,
        "is_financial": is_financial,
        "clears": clears,
        "passed": len(passes), "evaluated": len(evaluated), "criteria": len(checks),
        "checks": checks,
        "piotroski_detail": f,
        "growth_window_years": sales_years or profit_years,
        "price": _f(info.get("currentPrice")) or _f(info.get("regularMarketPrice")),
    }


def _check(value, threshold, mode, note=None):
    if value is None:
        return {"value": None, "threshold": threshold, "pass": None,
                "note": note or "not available from this feed"}
    ok = (value >= threshold if mode == "gte" else
          value > threshold if mode == "gt" else value < threshold)
    return {"value": value, "threshold": threshold, "pass": bool(ok), "note": note}


def run(criteria=None, limit=None, log=None, force=False):
    """
    Screen the exchange, cheapest test first.

    Stage one costs a few batched downloads for the whole list and drops
    anything too illiquid to act on. Only survivors are worth a per-company
    request, which is what makes 2,500 companies tractable at all.
    """
    say = log or (lambda _m: None)
    criteria = {**DEFAULTS, **(criteria or {})}

    if not force:
        cached = read_cache(criteria)
        if cached:
            say("quality screen: serving cached result")
            return cached

    listed = fetch_nse_list(log=say)
    if not listed:
        return {"generated": _now().strftime("%d %b %Y, %H:%M IST"),
                "error": "the NSE equity list could not be fetched",
                "matches": [], "criteria": criteria}
    if limit:
        listed = listed[:limit]

    survivors = _liquidity_stage(listed, say)
    say(f"quality screen: {len(survivors)} of {len(listed)} worth a fundamentals call")

    matches, examined, errors = [], 0, 0
    for entry in survivors:
        row = evaluate(entry["ticker"], entry["name"], entry["symbol"], criteria)
        examined += 1
        if row.get("error"):
            errors += 1
            continue
        if row["clears"]:
            matches.append(row)
            say(f"  * {row['symbol']} clears all {row['criteria']} criteria")
        if examined % 50 == 0:
            say(f"  ...{examined}/{len(survivors)} examined, {len(matches)} clearing")

    matches.sort(key=lambda r: ((r["checks"]["piotroski"]["value"] or 0),
                                (r["checks"]["altman_z"]["value"] or 0)), reverse=True)

    blob = {
        "generated": _now().strftime("%d %b %Y, %H:%M IST"),
        "criteria": criteria,
        "listed": len(listed),
        "examined": examined,
        "errors": errors,
        "matches": matches,
        "source": "NSE equity list + filed statements via Yahoo Finance",
        "caveats": [
            "Sales and profit growth are compounded over the years this feed "
            "carries - typically five, not the ten the screen asks for. The "
            "window actually used is shown on every row.",
            "Promoter holding is the insider-held percentage. It tracks the "
            "promoter block closely for Indian listings but is not the "
            "exchange shareholding-pattern filing.",
            "Altman Z is not computed for banks, NBFCs or insurers: the model "
            "assumes an operating balance sheet, so those are left unscored "
            "rather than given a number that looks comparable and is not.",
            "BSE-only listings are not covered - the exchange list endpoint "
            "currently answers 404.",
        ],
    }
    write_cache(blob)
    return blob


def _liquidity_stage(listed, say):
    """Batched price downloads to drop the untradeable before paying per name."""
    yf = _import_yf()
    tickers = [e["ticker"] for e in listed]
    kept = []
    chunk = 200
    for start in range(0, len(tickers), chunk):
        batch = tickers[start:start + chunk]
        try:
            data = yf.download(" ".join(batch), period="1mo", interval="1d",
                               group_by="ticker", auto_adjust=False, actions=False,
                               progress=False, threads=True)
        except Exception:                                          # noqa: BLE001
            continue
        for entry in listed[start:start + chunk]:
            try:
                frame = data[entry["ticker"]] if len(batch) > 1 else data
                closes = [c for c in frame["Close"].tolist() if c == c]
                volumes = [v for v in frame["Volume"].tolist() if v == v]
            except Exception:                                      # noqa: BLE001
                continue
            if not closes or not volumes:
                continue
            turnover_cr = (closes[-1] * (sum(volumes) / len(volumes))) / CRORE
            # a name trading a few lakh a day cannot be acted on even if its
            # statements are immaculate, so it is not worth four API calls
            if turnover_cr >= 0.5:
                kept.append(entry)
        say(f"  liquidity stage: {min(start + chunk, len(tickers))}/{len(tickers)} "
            f"screened, {len(kept)} kept")
    return kept


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def read_cache(criteria=None, max_age_hours=CACHE_HOURS):
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        fetched = datetime.fromisoformat(blob["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None
    if (_now() - fetched).total_seconds() > max_age_hours * 3600:
        return None
    if criteria and blob.get("criteria") != criteria:
        return None                     # a different screen is a different answer
    return blob


def write_cache(blob):
    blob = dict(blob, fetched_at=_now().isoformat())
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, indent=1)
    except OSError:
        pass
    return blob


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def report(blob, log=print):
    if not blob:
        return
    if blob.get("error"):
        log(f"  {blob['error']}")
        return
    matches = blob.get("matches") or []
    log("")
    log("=" * 78)
    log(f"  Quality screen · {blob['examined']} examined of {blob['listed']} listed")
    log("=" * 78)
    log("")
    if not matches:
        log("  Nothing clears every criterion.")
        log("  That is a result, not a failure: this screen is deliberately strict,")
        log("  and a screen that always returns something is not screening.")
    for row in matches:
        c = row["checks"]
        log(f"  {row['symbol']:<14}{(row['name'] or '')[:38]:<40}")
        log(f"      Z {c['altman_z']['value']}  F {c['piotroski']['value']}/9  "
            f"cap {c['market_cap_cr']['value']} Cr  OPM {c['operating_margin']['value']}%  "
            f"D/E {c['debt_to_equity']['value']}  promoter {c['promoter_holding']['value']}%")
        log(f"      sales {c['sales_growth']['value']}%  profit {c['profit_growth']['value']}%  "
            f"(compounded over {row['growth_window_years']} years)")
    log("")
    for caveat in blob.get("caveats") or []:
        log(f"  - {caveat}")
    log("")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Run the fundamental quality screen")
    parser.add_argument("--limit", type=int, default=None,
                        help="only look at the first N listed companies")
    parser.add_argument("--force", action="store_true",
                        help="ignore the cache and screen again")
    args = parser.parse_args(argv)

    blob = run(limit=args.limit, force=args.force, log=print)
    report(blob)
    if blob and not blob.get("error"):
        print(f"  written to {CACHE_FILE}")
    return 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
