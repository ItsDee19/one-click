"""Discover NSE equities independently of price coverage and trading eligibility.

The exchange publishes separate main-board and SME lists at
https://www.nseindia.com/static/market-data/securities-available-for-trading.
These are *available-for-trading* lists, not all Indian companies: BSE-only
listings and securities absent from these files are outside the declared scope.
No prices, turnover thresholds, or inferred market-cap ranks belong here.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / ".stock_universe_cache.json"
CURATED_FILE = HERE / "universe.json"
CACHE_HOURS = 24
CACHE_VERSION = 1
BUCKETS = ("large", "mid", "small", "unclassified")
LIST_PAGE = "https://www.nseindia.com/static/market-data/securities-available-for-trading"
SOURCES = {
    "main_board": "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "sme": "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv",
}
# Reject a valid-looking but truncated response instead of replacing a full list.
MIN_SOURCE_ROWS = {"main_board": 100, "sme": 10}
EXCLUSIONS = [
    "BSE-only listings: no BSE security-master provider is configured.",
    "Suspended, delisted, or other securities absent from NSE's available-for-trading lists.",
    "ETFs, funds, REITs, InvITs, debt, preference shares, warrants, IDRs, and the separate permitted-to-trade list.",
]
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&.\-]{0,39}\Z")
_SERIES = re.compile(r"[A-Z0-9]{1,3}\Z")
_LOCK = threading.RLock()


def _now():
    return datetime.now(timezone.utc)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing cache timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("cache timestamp has no timezone")
    return stamp.astimezone(timezone.utc)


def _normalise_row(symbol, name, series, isin, segment):
    symbol = str(symbol or "").strip().upper()
    name = str(name or "").strip()
    series = str(series or "").strip().upper()
    isin = str(isin or "").strip().upper()
    if not _SYMBOL.fullmatch(symbol) or not name or not _SERIES.fullmatch(series):
        raise ValueError("invalid symbol, company name, or series")
    if isin and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
        raise ValueError("invalid ISIN")
    return {"symbol": symbol, "ticker": f"{symbol}.NS", "name": name,
            "exchange": "NSE", "series": series, "isin": isin or None,
            "segment": segment}


def _deduplicate(rows):
    """Retain one row per ticker, with deterministic main-board precedence.

    Distinct symbols are retained even if they share an ISIN; dropping them
    could hide a listed share class or silently choose a stale symbol mapping.
    """
    ordered = sorted(rows, key=lambda row: (
        row["ticker"], row["segment"] != "main_board", row["series"] != "EQ",
        row["series"], row["name"], row.get("isin") or ""))
    by_ticker = {}
    for row in ordered:
        ticker = row["ticker"]
        if ticker not in by_ticker:
            by_ticker[ticker] = dict(row, series_codes=[])
        codes = by_ticker[ticker]["series_codes"]
        for code in row.get("series_codes", [row["series"]]):
            if code not in codes:
                codes.append(code)
    for row in by_ticker.values():
        row["series_codes"].sort()
    return list(by_ticker.values())


def _parse_equity_csv(content, segment):
    """Validate an equity-specific CSV; do not discard BE/BZ/SM/ST series."""
    if segment not in SOURCES:
        raise ValueError("unknown exchange segment")
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ValueError("empty equity CSV")
    headers = [field.strip().upper() for field in reader.fieldnames]
    if len(set(headers)) != len(headers):
        raise ValueError("duplicate equity CSV columns")
    if not {"SYMBOL", "NAME OF COMPANY", "SERIES"}.issubset(headers):
        raise ValueError("equity CSV is missing required columns")
    rows, rejected, total = [], 0, 0
    for raw in reader:
        total += 1
        if None in raw:  # Extra cells usually mean a malformed or truncated row.
            rejected += 1
            continue
        row = {key.strip().upper(): value for key, value in raw.items()}
        try:
            rows.append(_normalise_row(row.get("SYMBOL"), row.get("NAME OF COMPANY"),
                                       row.get("SERIES"), row.get("ISIN NUMBER"), segment))
        except ValueError:
            rejected += 1
    unique = _deduplicate(rows)
    if len(unique) < MIN_SOURCE_ROWS[segment]:
        raise ValueError(f"equity CSV has only {len(unique)} valid symbols; refusing an incomplete list")
    if rejected > max(1, total * 0.05):
        raise ValueError(f"equity CSV contains {rejected}/{total} malformed rows")
    return unique, {"raw_rows": total, "rejected_rows": rejected,
                    "duplicate_rows": len(rows) - len(unique)}


def _fetch_segment(segment, log):
    # Import only when refreshing: module imports and fresh-cache reads are offline.
    import requests

    with requests.Session() as session:
        session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36"),
            "Accept": "text/csv,*/*;q=0.5",
            "Referer": LIST_PAGE,
        })
        for attempt in range(2):
            try:
                response = session.get(SOURCES[segment], timeout=(8, 25))
                response.raise_for_status()
                return _parse_equity_csv(response.content, segment)
            except (requests.RequestException, ValueError, UnicodeError, csv.Error):
                if attempt:
                    raise
                log(f"NSE {segment} list request failed; retrying once")
                # A normal public page visit can establish the exchange session.
                try:
                    session.get(LIST_PAGE, timeout=(5, 8))
                except requests.RequestException:
                    pass
                time.sleep(1)


def _read_cache(path, now, errors):
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            raise ValueError("unsupported cache schema")
        segments = raw.get("segments")
        if not isinstance(segments, dict):
            raise ValueError("invalid cached segments")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"Universe cache unavailable ({type(exc).__name__})")
        return {}
    valid = {}
    for segment in SOURCES:
        block = segments.get(segment)
        if block is None:
            continue
        try:
            if not isinstance(block, dict) or block.get("url") != SOURCES[segment]:
                raise ValueError("invalid cached source")
            age = (now - _timestamp(block.get("fetched_at"))).total_seconds() / 3600
            if age < -5 / 60:
                raise ValueError("cache timestamp is in the future")
            stored = block.get("rows")
            if not isinstance(stored, list):
                raise ValueError("invalid cached rows")
            rows = []
            for entry in stored:
                if not isinstance(entry, dict):
                    raise ValueError("invalid cached security")
                row = _normalise_row(entry.get("symbol"), entry.get("name"),
                                     entry.get("series"), entry.get("isin"), segment)
                if entry.get("ticker") != row["ticker"]:
                    raise ValueError("cached ticker and symbol disagree")
                codes = entry.get("series_codes", [row["series"]])
                if not isinstance(codes, list) or not codes or any(
                        not isinstance(code, str) or not _SERIES.fullmatch(code) for code in codes):
                    raise ValueError("invalid cached series")
                row["series_codes"] = codes
                rows.append(row)
            rows = _deduplicate(rows)
            if len(rows) < MIN_SOURCE_ROWS[segment]:
                raise ValueError("cached segment is incomplete")
            validation = block.get("validation", {})
            if not isinstance(validation, dict) or any(
                    not isinstance(value, int) or value < 0 for value in validation.values()):
                raise ValueError("invalid cached validation counts")
            valid[segment] = dict(block, rows=rows, validation=validation)
        except (ValueError, TypeError, OverflowError):
            errors.append(f"Ignoring malformed {segment} universe cache")
    return valid


def _write_cache(path, segments):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temp_path = handle.name
            json.dump({"version": CACHE_VERSION, "segments": segments}, handle, ensure_ascii=False)
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _curated_entries(path, errors):
    try:
        with open(path, encoding="utf-8") as handle:
            curated = json.load(handle)
        if not isinstance(curated, dict):
            raise ValueError("invalid curated universe")
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"Curated classifications unavailable ({type(exc).__name__})")
        return {}
    out = {}
    for bucket in BUCKETS:
        items = curated.get(bucket, []) or []
        if not isinstance(items, list):
            errors.append(f"Ignoring malformed curated {bucket} bucket")
            continue
        for item in items:
            if isinstance(item, str):
                item = {"ticker": item}
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker.endswith(".NS") or not _SYMBOL.fullmatch(ticker[:-3]):
                continue
            out.setdefault(ticker, {"bucket": bucket, "ticker": ticker,
                                   "name": item.get("name") or ticker[:-3],
                                   "sector": item.get("sector")})
    return out


def load_exchange_universe(log=None, *, force_refresh=False, cache_path=None, curated_path=None):
    """Return ``(bucketed securities, coverage metadata)`` without quote calls.

    A fresh cache is used for 24 hours. Failed refreshes retain each segment's
    last successful list with its original timestamp and visible stale status.
    Only when neither exchange segment is available is the curated fallback
    used, explicitly degraded. Unknown market-cap classifications stay unknown.
    ``complete_for_declared_scope`` never promises BSE or price-data coverage.
    """
    say = log or (lambda _message: None)
    # Resolve at call time: app.py loads .env after importing this module.
    data_dir = os.environ.get("DB_DIR", "").strip()
    path = (Path(cache_path) if cache_path is not None else
            Path(data_dir) / ".stock_universe_cache.json" if data_dir else CACHE_FILE)
    curated_path = Path(curated_path) if curated_path is not None else CURATED_FILE
    with _LOCK:
        now, errors = _now(), []
        cached = _read_cache(path, now, errors)
        selected, coverage, refreshed = {}, {}, False
        for segment, url in SOURCES.items():
            block = cached.get(segment)
            age = ((now - _timestamp(block["fetched_at"])).total_seconds() / 3600
                   if block else None)
            source = "cache"
            if force_refresh or block is None or age >= CACHE_HOURS:
                try:
                    rows, validation = _fetch_segment(segment, say)
                    if block and len(rows) < len(block["rows"]) * 0.8:
                        raise ValueError("exchange list shrank by more than 20%; keeping last complete list")
                    block = {"url": url, "fetched_at": now.isoformat(), "rows": rows,
                             "validation": validation}
                    source, age, refreshed = "live", 0.0, True
                except Exception as exc:  # Independent source failure must not erase another segment.
                    detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    errors.append(f"NSE {segment} list unavailable ({detail})")
                    source = "stale_cache" if block else "unavailable"
            if block:
                selected[segment] = block
            coverage[segment] = {
                "url": url, "source": source, "count": len(block["rows"]) if block else 0,
                "fetched_at": block["fetched_at"] if block else None,
                "cache_age_hours": round(max(0, age), 3) if age is not None else None,
                "stale": source == "stale_cache",
                "validation": block.get("validation", {}) if block else {},
            }
        if refreshed:
            try:
                _write_cache(path, selected)
            except OSError as exc:
                errors.append(f"Universe cache could not be saved ({type(exc).__name__})")
        curated = _curated_entries(curated_path, errors)
        rows = _deduplicate([row for block in selected.values() for row in block["rows"]])
        fallback = not rows
        if fallback:
            rows = [{"ticker": ticker, "symbol": ticker[:-3], "name": item["name"],
                     "exchange": "NSE", "series": None, "series_codes": [],
                     "isin": None, "segment": "unknown"} for ticker, item in sorted(curated.items())]
            errors.append("Using the limited curated universe; full exchange coverage is unavailable")
        universe = {bucket: [] for bucket in BUCKETS}
        for entry in rows:
            known = curated.get(entry["ticker"], {})
            bucket = known.get("bucket", "unclassified")
            universe[bucket].append(dict(entry, sector=known.get("sector"),
                                         classification_source="curated_universe" if known and bucket != "unclassified" else "unknown"))
        stale = any(block["stale"] for block in coverage.values())
        missing = [segment for segment, block in coverage.items() if not block["count"]]
        malformed = any(block["validation"].get("rejected_rows", 0) for block in coverage.values())
        degraded = fallback or stale or bool(missing) or malformed
        sources = {block["source"] for block in coverage.values()}
        source = ("curated_fallback" if fallback else "nse_live" if sources == {"live"}
                  else "nse_cache" if sources == {"cache"} else "nse_mixed")
        timestamps = [block["fetched_at"] for block in coverage.values() if block["fetched_at"]]
        ages = [block["cache_age_hours"] for block in coverage.values() if block["cache_age_hours"] is not None]
        exclusions = list(EXCLUSIONS)
        for segment in missing:
            exclusions.append(f"NSE {segment}: listing source and its cache are unavailable.")
        metadata = {
            "scope": "NSE equities available for trading (main board and SME)",
            "source": source, "source_page": LIST_PAGE, "listed_count": len(rows),
            "fetched_at": min(timestamps, key=_timestamp) if timestamps and not fallback else None,
            "cache_age_hours": max(ages) if ages and not fallback else None,
            "stale": stale, "degraded": bool(degraded),
            "complete_for_declared_scope": not degraded,
            "segment_coverage": coverage, "exclusions": exclusions, "errors": errors,
            "bucket_counts": {bucket: len(entries) for bucket, entries in universe.items()},
            "classification_note": "Known classifications come from universe.json and may be dated; all others remain unclassified. Turnover is never market capitalization.",
            "filters_applied": [],
            "cross_segment_duplicates": sum(len(block["rows"]) for block in selected.values()) - len(rows) if not fallback else 0,
        }
        say(f"Universe: {len(rows)} securities ({source}); "
            f"{len(universe['unclassified'])} without market-cap classification"
            + ("; DEGRADED coverage" if degraded else ""))
        for error in errors:
            say(error)
        return universe, metadata
