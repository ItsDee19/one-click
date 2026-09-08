"""Explicit, offline evidence checks shared by both analysis engines.

Coverage is a completeness measure, never a probability of a profitable trade.
Provider timestamps are checked separately from the bundle's processing time.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MAX_INTRADAY_AGE_MINUTES = 20
# Calendar days, allowing weekends/short holiday closures without inventing
# an exchange calendar. Older snapshots require a new provider observation.
MAX_DAILY_AGE_DAYS = 4

NUMERIC_FIELDS = {
    "price": "live day_open day_high day_low prev_close day_change_pct volume",
    "range_52w": "high low pct_from_high position_pct",
    "technicals": "rvol rvol_raw sma_period price_vs_sma_pct window_return_pct swing_high swing_low day_range_position_pct atr_pct move_vs_atr observations",
    "intraday": "bars gap_pct opening_range_high opening_range_low opening_range_pct vwap price_vs_vwap_pct session_volume session_high session_low",
    "analyst": "num_analysts buy_pct hold_pct sell_pct target_mean target_low target_high upside_pct",
    "news": "total positive negative neutral net_tone",
    "market": "session_pct session_fraction minutes_to_close",
    "relative": "benchmark_day_change_pct benchmark_window_return_pct rel_day_change_pct rel_window_return_pct sector_median_pct sector_rel_pct",
    "events": "days_to_earnings",
    "regime": "pct_vs_sma sma_period",
}


def finite_number(value):
    """A real, finite numeric measurement; bool and malformed values are absent."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def sanitize_evidence(evidence):
    """Return a copy with nonfinite measurements represented as missing values."""
    def copy(node):
        if isinstance(node, dict):
            return {key: copy(value) for key, value in node.items()}
        if isinstance(node, (list, tuple)):
            return [copy(value) for value in node]
        if isinstance(node, float) and not math.isfinite(node):
            return None
        return node

    result = copy(evidence) if isinstance(evidence, dict) else {}
    for section, fields in NUMERIC_FIELDS.items():
        block = result.get(section)
        if not isinstance(block, dict):
            result[section] = {}
            continue
        for field in fields.split():
            if field in block:
                value = block[field]
                number = finite_number(value)
                block[field] = value if number is not None and isinstance(value, (int, float)) else number
    return result


def _get(evidence, path):
    value = evidence
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def parse_timestamp(value):
    """Accept provider epoch seconds, ISO timestamps and the desk's IST format."""
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, (float, int)) and not isinstance(value, bool):
        try:
            stamp = datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    elif isinstance(value, str) and value.strip():
        value = value.strip()
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                stamp = datetime.strptime(value, "%d %b %Y, %H:%M:%S IST")
            except ValueError:
                return None
    else:
        return None
    return stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)


def assess_evidence(evidence, now=None):
    """Report missing/stale evidence and explicit blockers for each horizon.

    Demo snapshots retain illustrative scoring; ``actionable`` is always false
    for demos. Callers must not represent demo verdicts as live observations.
    """
    ev = sanitize_evidence(evidence)
    moment = parse_timestamp(now) or datetime.now(IST)
    demo = ev.get("source") == "demo"
    blockers = {"intraday": [], "positional": []}
    warnings = []
    core = ("price.live", "price.volume", "technicals.rvol",
            "technicals.price_vs_sma_pct", "technicals.atr_pct",
            "technicals.window_return_pct", "range_52w.position_pct",
            "analyst.target_mean", "news.total", "relative.rel_window_return_pct")
    missing = [path for path in core if _get(ev, path) is None]
    coverage = round(100 * (len(core) - len(missing)) / len(core), 1)

    price = finite_number(_get(ev, "price.live"))
    if price is None or price <= 0:
        for track in blockers:
            blockers[track].append("current price is missing or invalid")
    rvol = finite_number(_get(ev, "technicals.rvol"))
    if rvol is None or rvol < 0:
        for track in blockers:
            blockers[track].append("relative volume is missing or invalid")
    if _get(ev, "technicals.price_vs_sma_pct") is None:
        blockers["positional"].append("trend reference is unavailable")
    observations = finite_number(_get(ev, "technicals.observations"))
    period = finite_number(_get(ev, "technicals.sma_period"))
    if observations is not None and period is not None and observations < period:
        blockers["positional"].append("price history is shorter than the stated trend window")

    if _get(ev, "intraday.available") is not True:
        blockers["intraday"].append("no usable intraday session")
    if _get(ev, "market.live_session") is not True:
        blockers["intraday"].append("market session is not live")
    if (_get(ev, "intraday.bars") or 0) < 3:
        blockers["intraday"].append("the opening range has fewer than three bars")
    if _get(ev, "intraday.coverage_complete") is False:
        blockers["intraday"].append("session bars are incomplete; VWAP coverage is partial")
    if _get(ev, "intraday.vwap") is None:
        blockers["intraday"].append("VWAP is unavailable")
    if _get(ev, "intraday.above_opening_range") is None:
        blockers["intraday"].append("opening range confirmation is unavailable")

    freshness = {}
    for field, key in (("price.as_of", "quote"), ("technicals.last_bar", "daily"),
                       ("intraday.last_bar", "intraday")):
        stamp = parse_timestamp(_get(ev, field))
        age = (moment - stamp).total_seconds() / 60 if stamp else None
        status = "unknown"
        if age is not None:
            if age < -5:
                status = "future"
            elif key == "intraday":
                status = "fresh" if age <= MAX_INTRADAY_AGE_MINUTES and stamp.date() == moment.date() else "stale"
            elif key == "quote" and _get(ev, "market.live_session") is True:
                status = "fresh" if age <= MAX_INTRADAY_AGE_MINUTES else "stale"
            else:
                status = "fresh" if age <= MAX_DAILY_AGE_DAYS * 1440 else "stale"
        freshness[key] = {"status": status, "as_of": stamp.isoformat() if stamp else None,
                          "age_minutes": round(age, 1) if age is not None else None}

    if not demo:
        # A date-only daily bar cannot establish intraday quote freshness.
        for key in ("quote", "intraday"):
            if freshness[key]["status"] != "fresh":
                blockers["intraday"].append(f"{key} timestamp is {freshness[key]['status']}")
        for key in ("quote", "daily"):
            status = freshness[key]["status"]
            if status != "fresh":
                blockers["positional"].append(f"{key} timestamp is {status}")
    else:
        warnings.append("illustrative demo snapshot; not actionable live evidence")

    if _get(ev, "analyst.target_mean") is None:
        warnings.append("no analyst target; positional objective may be unavailable")
    if _get(ev, "news.total") is None:
        warnings.append("news feed unavailable; absence of headlines is not neutral sentiment")
    if missing:
        warnings.append("missing measurements are excluded rather than imputed as neutral or zero")
    actionable = {track: not demo and not reasons for track, reasons in blockers.items()}
    status = "demo" if demo else ("complete" if not missing and all(actionable.values()) else "partial")
    if price is None or price <= 0:
        status = "unavailable"
    return {"status": status, "demo": demo, "coverage_pct": coverage,
            "coverage_basis": "presence of ten core evidence fields, not a success probability",
            "missing_fields": missing, "warnings": warnings, "blockers": blockers,
            "actionable": actionable, "freshness": freshness,
            "checked_at": moment.isoformat()}
