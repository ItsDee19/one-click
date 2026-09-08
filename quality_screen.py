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
Thousands of NSE equities times several API calls each is substantial work.
The quality-screen funnel spends cheap calls on everything and expensive ones only on
survivors: price and liquidity first, the summary block next, and full
statements only for the handful still standing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "quality_screen.json")
PROGRESS_FILE = os.path.join(HERE, ".quality_screen_progress.json")
CACHE_HOURS = 20                      # statements change quarterly, not hourly

SCREEN_VERSION = 2
LIQUIDITY_MIN_TURNOVER_CR = 0.5

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

def fetch_nse_list(log=None, *, include_metadata=False):
    """Share main-board/SME discovery and its honest cache/fallback coverage."""
    import stock_universe
    universe, metadata = stock_universe.load_exchange_universe(log=log)
    rows = sorted((dict(entry) for entries in universe.values() for entry in entries),
                  key=lambda entry: entry["ticker"])
    return (rows, metadata) if include_metadata else rows


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
    common_issuance = _row(cf, "Common Stock Issuance", 0)

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
    # Piotroski (2000), section 2.3.2: no common-equity issuance during the
    # preceding fiscal year. Gross issuance is direct evidence; book equity
    # and net share-count changes can hide issuance behind profits/buybacks.
    # https://www.gsb.stanford.edu/faculty-research/publications/value-investing-use-historical-financial-statement-information
    check("no common-equity issuance",
          None if common_issuance is None or not math.isfinite(common_issuance) or common_issuance < 0
          else common_issuance == 0)
    tests[-1]["basis"] = ("Provider-reported gross Common Stock Issuance in the latest annual cash-flow statement; "
                          "book-equity growth and net share counts are not issuance evidence. "
                          "This cash-flow measure does not verify noncash share issuance.")
    check("gross margin improving",
          None if None in (gp0, rev0, gp1, rev1) or not rev0 or not rev1
          else (gp0 / rev0) > (gp1 / rev1))
    check("asset turnover improving",
          None if None in (rev0, ta0, rev1, ta1) or not ta0 or not ta1
          else (rev0 / ta0) > (rev1 / ta1))

    return {"score": passed, "max": 9 - skipped, "skipped": skipped, "tests": tests}


def cagr(frame, name):
    """
    Compound growth between valid dated endpoints, with actual elapsed years.

    Missing intermediate years do not shorten the denominator. If an endpoint
    is missing, use the remaining dated endpoints and report that shorter
    actual span. Undated or duplicate periods cannot support an annual rate.
    """
    if frame is None or getattr(frame, "empty", True) or name not in frame.index:
        return None, None
    try:
        series = frame.loc[name]
        pairs, dates = [], set()
        for period, raw in zip(series.index, series.tolist()):
            # Refuse numeric/TTM labels instead of treating their positions as years.
            period_end = datetime.fromisoformat(str(period)).date()
            if period_end in dates:
                return None, None
            dates.add(period_end)
            value = None if isinstance(raw, bool) else _f(raw)
            if value is not None and math.isfinite(value):
                pairs.append((period_end, value))
    except Exception:                                              # noqa: BLE001
        return None, None
    if len(pairs) < 2:
        return None, None
    pairs.sort(key=lambda pair: pair[0])
    (first_date, earliest), (last_date, latest) = pairs[0], pairs[-1]
    years = (last_date - first_date).days / 365.2425
    if years <= 0:
        return None, None
    reported_years = round(years, 3)
    if earliest <= 0 or latest <= 0:
        return None, reported_years
    try:
        # Log form avoids overflowing the ratio before annualization.
        growth = math.expm1((math.log(latest) - math.log(earliest)) / years) * 100.0
    except (OverflowError, ValueError):
        return None, reported_years
    return (round(growth, 1) if math.isfinite(growth) else None), reported_years


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
        "sales_growth": _check(sales_growth, criteria["sales_growth_min"], "gt",
                               f"{sales_years:g} elapsed years between dated annual observations" if sales_years is not None else None),
        "profit_growth": _check(profit_growth, criteria["profit_growth_min"], "gt",
                                f"{profit_years:g} elapsed years between dated annual observations" if profit_years is not None else None),
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
        "growth_window_years": sales_years if sales_years == profit_years else None,
        "growth_windows_years": {"sales": sales_years, "profit": profit_years},
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
    request. Listing discovery itself retains every main-board and SME entry.
    """
    say = log or (lambda _m: None)
    criteria = {**DEFAULTS, **(criteria or {})}
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
        raise ValueError("limit must be a positive integer")

    if force:
        _clear_progress()
    if not force:
        cached = read_cache(criteria)
        if cached and cached.get("screen_version") == SCREEN_VERSION and cached.get("requested_limit") == limit:
            say("quality screen: serving cached result")
            return cached

    listed, universe_metadata = fetch_nse_list(log=say, include_metadata=True)
    discovered = len(listed)
    if not listed:
        return {"generated": _now().strftime("%d %b %Y, %H:%M IST"),
                "error": "the NSE equity list could not be fetched",
                "matches": [], "criteria": criteria, "universe": universe_metadata,
                "discovered": 0, "listed": 0, "screen_version": SCREEN_VERSION,
                "requested_limit": limit}
    if limit:
        listed = listed[:limit]
    _prepare_progress(criteria, listed, say)

    # The liquidity scan is a dozen batched downloads over the whole exchange.
    # Recomputing it on every resume would make an interrupted run more
    # expensive to restart than to finish, so its result is checkpointed too.
    survivors = _resume_survivors(criteria, say)
    if survivors is None:
        survivors = _liquidity_stage(listed, say)
        _save_survivors(criteria, survivors)
    say(f"quality screen: {len(survivors)} of {len(listed)} worth a fundamentals call")

    # A full exchange screen is several hours of API calls and will be
    # interrupted sooner or later — a closed laptop, a dropped connection, a
    # process killed by whatever is supervising it. Progress is written as it
    # goes and picked up on the next run, so an interruption costs the current
    # company rather than the whole night.
    done, matches, errors = _load_progress(criteria, say)
    examined = len(done)
    remaining = [e for e in survivors if e["symbol"] not in done]
    if examined:
        say(f"resuming: {examined} already examined, {len(remaining)} to go")

    for position, entry in enumerate(remaining, start=1):
        row = evaluate(entry["ticker"], entry["name"], entry["symbol"], criteria)
        done.add(entry["symbol"])
        examined += 1
        if row.get("error"):
            errors += 1
        elif row["clears"]:
            matches.append(row)
            say(f"  * {row['symbol']} clears all {row['criteria']} criteria")
        if position % 10 == 0:
            _save_progress(criteria, done, matches, errors)
            say(f"  ...{examined}/{len(survivors)} examined, "
                f"{len(matches)} clearing (progress saved)")

    _save_progress(criteria, done, matches, errors)

    matches.sort(key=lambda r: ((r["checks"]["piotroski"]["value"] or 0),
                                (r["checks"]["altman_z"]["value"] or 0)), reverse=True)

    blob = {
        "generated": _now().strftime("%d %b %Y, %H:%M IST"),
        "criteria": criteria,
        "screen_version": SCREEN_VERSION,
        "requested_limit": limit,
        "universe": universe_metadata,
        "discovered": discovered,
        "listed": len(listed),
        "liquidity_survivors": len(survivors),
        "not_advanced_to_fundamentals": len(listed) - len(survivors),
        "screen_filters": {
            "minimum_average_daily_turnover_cr": LIQUIDITY_MIN_TURNOVER_CR,
            "turnover_basis": "latest available close times mean volume over one month",
            "limited": len(listed) < discovered,
            "not_advanced_note": "Includes below-threshold liquidity and unavailable price data; these counts are not interchangeable.",
        },
        "examined": examined,
        "errors": errors,
        "matches": matches,
        "source": f"{universe_metadata.get('source', 'NSE trading lists')} + company statements via Yahoo Finance",
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
            "The quality screen applies a separate turnover filter; the intelligence engine's all-stock analysis does not apply this filter.",
            *universe_metadata.get("exclusions", []),
        ],
    }
    if universe_metadata.get("degraded"):
        blob["caveats"].append("Listing coverage is degraded; inspect universe source, timestamps and errors before interpreting an empty screen.")
    if len(listed) < discovered:
        blob["caveats"].append(f"This run explicitly screened only {len(listed)} of {discovered} discovered securities (--limit).")
    write_cache(blob)
    _clear_progress()
    return blob


def _prepare_progress(criteria, listed, say):
    """Old EQ-only or limited survivor lists must not become an all-stock run."""
    identity = {"screen_version": SCREEN_VERSION,
                "tickers": sorted(entry["ticker"] for entry in listed)}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    previous = _read_progress() or {}
    if previous.get("criteria") != criteria or previous.get("universe_fingerprint") != fingerprint:
        if previous:
            say("quality-screen progress belongs to a different listing scope; starting fresh")
        _clear_progress()
        _write_progress({"criteria": criteria, "universe_fingerprint": fingerprint})


def _load_progress(criteria, say):
    """What a previous interrupted run already established, if anything."""
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return set(), [], 0
    if blob.get("criteria") != criteria:
        say("previous progress was for a different screen — starting fresh")
        return set(), [], 0
    return set(blob.get("done") or []), blob.get("matches") or [], blob.get("errors", 0)


def _save_progress(criteria, done, matches, errors):
    blob = _read_progress() or {}
    blob.update({"criteria": criteria, "done": sorted(done),
                 "matches": matches, "errors": errors})
    _write_progress(blob)


def _read_progress():
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        return blob if isinstance(blob, dict) else None
    except (OSError, ValueError):
        return None


def _write_progress(blob):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)
    except OSError:
        pass


def _resume_survivors(criteria, say):
    """The survivor list a previous run already paid for, if it matches."""
    blob = _read_progress() or {}
    if blob.get("criteria") != criteria or not blob.get("survivors"):
        return None
    say(f"reusing the liquidity scan from an earlier run "
        f"({len(blob['survivors'])} survivors)")
    return blob["survivors"]


def _save_survivors(criteria, survivors):
    blob = _read_progress() or {}
    blob.update({"criteria": criteria, "survivors": survivors})
    _write_progress(blob)


def _clear_progress():
    try:
        os.remove(PROGRESS_FILE)
    except OSError:
        pass


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
            if turnover_cr >= LIQUIDITY_MIN_TURNOVER_CR:
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
        if not isinstance(blob, dict):
            return None
        fetched = datetime.fromisoformat(blob["fetched_at"])
        if fetched.tzinfo is None:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = (_now() - fetched).total_seconds()
    if age < -300 or age > max_age_hours * 3600:
        return None
    if criteria and blob.get("criteria") != criteria:
        return None                     # a different screen is a different answer
    if blob.get("screen_version") != SCREEN_VERSION:
        blob["universe"] = {"source": "legacy_quality_cache", "degraded": True,
                            "scope": "Legacy EQ-only NSE quality screen; SME and non-EQ series were excluded",
                            "errors": ["Rerun the quality screen for unified main-board and SME discovery."]}
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
