"""Evidence admission for intraday picks; sample size is not a trust label.

The floors below are engineering admission gates, not a promise of profitability.
Every consumer uses this module so old, gross-only records cannot earn confidence.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import math
from pathlib import Path
import random

import strategies

SCHEMA_VERSION = 2
MIN_OOS_TRADES = 100
MIN_OOS_DATES = 60
MAX_RECORD_AGE_DAYS = 90
BOOTSTRAP_REPETITIONS = 2000
BOOTSTRAP_SEED = 190826
RANKING_POLICY_VERSION = "intraday-current-entry-v1"
COST_MODEL_VERSION = strategies.COST_MODEL_VERSION
IST = timezone(timedelta(hours=5, minutes=30))


def validation_policy_hash():
    """Bind records to the actual replay, live admission and strategy-vote code."""
    filenames = ("intraday_validation.py", "intraday_policy.py", "intraday_desk.py",
                 "intraday_data.py", "backtest_intraday.py", "strategy_edge.py", "data_sources.py")
    paths = [Path(__file__).with_name(name) for name in filenames]
    source = "\n".join(path.read_text(encoding="utf-8").replace("\r\n", "\n") for path in paths)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_time(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(IST) if parsed.tzinfo is not None else None


@lru_cache(maxsize=128)
def _cluster_ci(clusters):
    """Resample whole market dates, preserving within-day stock correlation."""
    if len(clusters) < 2:
        return (None, None)
    rng = random.Random(BOOTSTRAP_SEED)
    means = []
    for _ in range(BOOTSTRAP_REPETITIONS):
        selected = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        means.append(sum(total for total, count in selected) /
                     sum(count for total, count in selected))
    means.sort()
    return (round(means[int(.025 * (len(means) - 1))], 6),
            round(means[int(.975 * (len(means) - 1))], 6))


def net_summary(rows):
    """Net results with chronological drawdown and a day-cluster uncertainty band."""
    clean = [row for row in rows if finite(row.get("net_r")) is not None and row.get("date")]
    clean.sort(key=lambda row: (row["date"], str(row.get("exit_at", "")), str(row.get("ticker", ""))))
    values = [float(row["net_r"]) for row in clean]
    days = defaultdict(list)
    for row in clean:
        days[row["date"]].append(float(row["net_r"]))
    clusters = tuple((sum(days[day]), len(days[day])) for day in sorted(days))
    low, high = _cluster_ci(clusters)
    gains, losses = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values), "distinct_dates": len(days),
        "first_date": min(days) if days else None, "last_date": max(days) if days else None,
        "net_expectancy_r": round(sum(values) / len(values), 6) if values else None,
        "expectancy_r": round(sum(values) / len(values), 6) if values else None,
        "win_rate_pct": round(100 * sum(v > 0 for v in values) / len(values), 2) if values else None,
        "profit_factor": round(gains / losses, 4) if losses else None,
        "max_drawdown_r": round(drawdown, 6) if values else None,
        "net_expectancy_ci95": [low, high], "uncertainty_method": "trading_date_cluster_bootstrap",
        "bootstrap_repetitions": BOOTSTRAP_REPETITIONS,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "enough": False,
    }


def _sample_reasons(metrics, prefix):
    reasons = []
    if metrics.get("trades", 0) < MIN_OOS_TRADES:
        reasons.append(f"{prefix}: fewer than {MIN_OOS_TRADES} out-of-sample trades")
    if metrics.get("distinct_dates", 0) < MIN_OOS_DATES:
        reasons.append(f"{prefix}: fewer than {MIN_OOS_DATES} distinct out-of-sample dates")
    mean = finite(metrics.get("net_expectancy_r"))
    if mean is None or mean <= 0:
        reasons.append(f"{prefix}: positive net expectancy is not established")
    band = metrics.get("net_expectancy_ci95") or []
    low = finite(band[0]) if len(band) == 2 else None
    if low is None or low <= 0:
        reasons.append(f"{prefix}: the 95% day-cluster lower bound is not positive")
    return reasons


def _ledger_issues(rows, start, end):
    identities = set()
    for row in rows:
        gross, costs, net = (finite(row.get(key)) for key in ("gross_r", "costs_r", "net_r"))
        decision = parse_time(row.get("confirmed_at"))
        entry, exit_at = parse_time(row.get("entry_at")), parse_time(row.get("exit_at"))
        day = row.get("date")
        if (None in (gross, costs, net) or costs <= 0 or abs(gross - costs - net) > .0001
                or decision is None or entry is None or exit_at is None
                or entry < decision or exit_at < entry or entry.date() != exit_at.date()
                or str(entry.date()) != day or not (start <= day <= end)):
            return ["Out-of-sample ledger has invalid timing, dates, or net-cost reconciliation"]
        identity = (row.get("ticker"), row.get("strategy"), row.get("direction"), row.get("confirmed_at"))
        if not row.get("ticker") or identity in identities:
            return ["Out-of-sample ledger has duplicate trades or missing ticker identities"]
        identities.add(identity)
    return []


def assess_record(blob, strategy, direction, now=None):
    """Assess a direction-specific rule AND evidence for the actual daily selector.

    A schema-correct historical replay can be useful research while failing the
    promotion gates. Missing/version-stale records are explicitly unverified.
    """
    result = {"status": "unverified", "qualified": False, "reasons": [], "metrics": {}}
    reasons = result["reasons"]
    if not isinstance(blob, dict) or blob.get("schema_version") != SCHEMA_VERSION:
        reasons.append("Missing or legacy gross-only record; a new net replay is required")
        return result
    for field in ("cost_model", "strategies", "holdout", "data", "ranking"):
        if not isinstance(blob.get(field), dict):
            reasons.append(f"Invalid record structure: {field} must be an object")
    if reasons:
        return result
    if direction not in ("long", "short"):
        reasons.append("BUY/long or SELL/short direction is missing")
        return result
    if strategies.STRATEGY_DIRECTIONS.get(strategy) != direction:
        reasons.append("Strategy direction does not match the current rule")
        return result
    if blob.get("strategy_version") != strategies.strategy_version():
        reasons.append("Record does not match the current strategy and execution code")
    if blob.get("validation_policy_hash") != validation_policy_hash():
        reasons.append("Record does not match the current validation policy")
    model = blob.get("cost_model") or {}
    fee_bps = finite(model.get("round_trip_cost_bps"))
    slip_bps = finite(model.get("slippage_bps_per_fill"))
    if (model.get("version") != COST_MODEL_VERSION
            or fee_bps is None or slip_bps is None
            or fee_bps < strategies.ROUND_TRIP_COST_BPS
            or slip_bps < strategies.SLIPPAGE_BPS):
        reasons.append("Current transaction-cost and adverse-slippage assumptions are missing")
    current = parse_time(now) if now is not None else datetime.now(IST)
    generated = parse_time(blob.get("generated_at"))
    if current is None or generated is None or generated > current + timedelta(minutes=5):
        reasons.append("Record has no valid, timezone-aware generation time")
    elif current - generated > timedelta(days=MAX_RECORD_AGE_DAYS):
        reasons.append(f"Record is older than {MAX_RECORD_AGE_DAYS} days")
    if reasons:
        return result

    result["status"] = "research_only"
    strategy_record = blob["strategies"].get(strategy) or {}
    record = strategy_record.get("directions") if isinstance(strategy_record, dict) else None
    record = record if isinstance(record, dict) else {}
    if direction not in record:
        reasons.append("No separate evidence for this strategy direction")
    holdout = blob.get("holdout") or {}
    start, end, development_end = (holdout.get(key) for key in ("start", "end", "development_end"))
    try:
        start_date, end_date, development_date = (datetime.fromisoformat(value).date()
                                                   for value in (start, end, development_end))
        valid_split = development_date < start_date <= end_date
    except (TypeError, ValueError):
        valid_split = False
    if not valid_split or holdout.get("method") != "chronological_global_dates":
        reasons.append("A global chronological development/holdout split is missing")
        return result
    if current.date() - end_date > timedelta(days=MAX_RECORD_AGE_DAYS):
        reasons.append(f"Out-of-sample observations are older than {MAX_RECORD_AGE_DAYS} days")
    if end_date > current.date():
        reasons.append("Holdout includes future observations")
    embargo = holdout.get("embargo_dates") or []
    if (not isinstance(embargo, list) or not embargo
            or any(not isinstance(day, str) or not (development_end < day < start) for day in embargo)):
        reasons.append("No complete trading-date embargo between development and evaluation")
    preregistered = parse_time(holdout.get("preregistered_at"))
    if (holdout.get("untouched") is not True or preregistered is None
            or preregistered.date() >= start_date or not holdout.get("protocol_evidence")):
        reasons.append("Untouched holdout and a protocol fixed before evaluation are not substantiated")

    ledger = blob.get("ledger")
    if not isinstance(ledger, list):
        reasons.append("Auditable per-trade ledger is missing")
        ledger = []
    related = [row for row in ledger if isinstance(row, dict) and row.get("strategy") == strategy
               and row.get("sample") == "oos"]
    if any(row.get("direction") not in ("long", "short") for row in related):
        reasons.append("Out-of-sample ledger omits trade direction")
    rows = [row for row in related if row.get("direction") == direction]
    ledger_issues = _ledger_issues(rows, start, end)
    if ledger_issues:
        reasons.extend(ledger_issues)
        return result
    metrics = net_summary(rows)
    result["metrics"] = metrics
    reasons.extend(_sample_reasons(metrics, "Rule"))
    data = blob.get("data") or {}
    if data.get("point_in_time_universe") is not True or not data.get("universe_evidence"):
        reasons.append("Historical universe membership is unverified; current-universe research has survivorship bias")

    ranking = blob.get("ranking") or {}
    if (ranking.get("policy_version") != RANKING_POLICY_VERSION or ranking.get("tested") is not True
            or ranking.get("method") != "event_time_full_policy_replay"
            or not ranking.get("execution_eligibility_evidence")):
        reasons.append("The current daily ranking, eligibility and exposure policy has not been replayed")
    else:
        portfolio_rows = ranking.get("ledger") or []
        if not isinstance(portfolio_rows, list):
            reasons.append("Daily selection trade ledger is missing")
            return result
        selected = [row for row in portfolio_rows if isinstance(row, dict)
                    and row.get("direction") == direction and row.get("sample") == "oos"]
        ledger_issues = _ledger_issues(selected, start, end)
        if ledger_issues:
            reasons.extend(ledger_issues)
            return result
        reasons.extend(_sample_reasons(net_summary(selected), "Daily selected portfolio"))
    if not reasons:
        result.update(status="historically_validated", qualified=True)
    return result
