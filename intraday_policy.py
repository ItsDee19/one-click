"""Deterministic snapshot selection; thresholds are research policy, not proven edge."""
from __future__ import annotations

import math
from datetime import datetime

POLICY_VERSION = "intraday-current-entry-v1"
MAX_PICKS = 5
MAX_PER_SECTOR = 2
MIN_NET_REWARD_RISK = 1.5
MAX_ENTRY_DRIFT_R = 0.25
SIGNAL_LIFETIME_MINUTES = 10
MIN_DAILY_TURNOVER_INR = 10_000_000


def finite(value, default=None):
    if value is None or isinstance(value, bool):
        return default
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def rank_key(item):
    try:
        stamp = datetime.fromisoformat(str(item.get("confirmed_at", "")).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        stamp = 0
    return (-finite(item.get("current_reward_risk"), -999),
            -min(10, finite(item.get("rvol"), 0)),
            finite(item.get("entry_drift_r"), 999), -stamp,
            str(item.get("ticker", "")), str(item.get("strategy", "")))


def select_candidates(items, max_picks=MAX_PICKS, max_per_sector=MAX_PER_SECTOR, separate_admission=False):
    """Resolve direction before ranking; no outcomes or evaluation-period returns."""
    grouped, rejected = {}, []
    for item in items:
        grouped.setdefault(item["ticker"], []).append(dict(item))
    unique = []
    for ticker, group in grouped.items():
        if len({p.get("direction") for p in group}) > 1:
            rejected.extend(dict(p, state="conflict", reasons=list(p.get("reasons") or []) +
                                 ["Opposing active signals for the same stock; no directional pick."]) for p in group)
            continue
        ordered = sorted(group, key=lambda p: ((0 if _qualified(p) else 1) if separate_admission else 0, rank_key(p)))
        unique.append(ordered[0])
        rejected.extend(dict(p, state="duplicate", reasons=list(p.get("reasons") or []) +
                             ["A higher-ranked setup already represents this stock."]) for p in ordered[1:])
    selected, sectors, counts = [], {}, {}
    for item in sorted(unique, key=rank_key):
        # Unknown sector is one uncertainty group, not unlimited diversification.
        pool = _qualified(item) if separate_admission else True
        sector = (pool, item.get("sector") or "Unknown")
        if counts.get(pool, 0) >= max_picks or sectors.get(sector, 0) >= max_per_sector:
            rejected.append(dict(item, state="not_selected", reasons=list(item.get("reasons") or []) +
                                 ["Outside the snapshot's stock/sector selection limits."]))
            continue
        selected.append(item)
        counts[pool] = counts.get(pool, 0) + 1
        sectors[sector] = sectors.get(sector, 0) + 1
    return {"selected": selected, "rejected": rejected}


def _qualified(item):
    return item.get("state") == "entry_ready" and (item.get("validation") or {}).get("qualified") is True
