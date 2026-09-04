"""
strategy_edge.py — let the measured strategies vote in the debate.

Until now the named strategies lived on their own page: they were measured,
displayed, and then ignored by the engine that actually issues verdicts. This
connects them, so that Bull and Bear can cite a rule that has a track record
on this universe rather than only the raw indicators underneath it.

HOW MUCH A RULE IS ALLOWED TO MOVE A VERDICT
--------------------------------------------
Weight comes from **measured expectancy**, not from the rule's reputation and
not from its win rate. A rule that fires today contributes:

    points = expectancy_R x SCALE,  clamped to +/- MAX_POINTS

with three conditions, each there for a reason:

  * a rule with fewer than its module's minimum trades contributes **nothing**.
    A promising number over nine trades is not evidence, and this is the same
    bar the pages already apply before printing a hit rate.
  * a rule with negative measured expectancy argues for the *other* side. If
    the data says a setup lost money on this universe, seeing it should reduce
    conviction rather than being quietly dropped.
  * the total is capped, so a stock cannot be talked into a BUY by strategy
    agreement alone. The cap exists for the same reason GMP is capped on the
    IPO desk: a single class of evidence should not be able to carry a verdict
    by itself.

The records are read from the backtest artefacts. If they are missing, this
contributes nothing at all and says so — the engine degrades to exactly the
behaviour it had before, rather than to a guess.
"""

from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
INTRADAY_RECORD = os.path.join(HERE, "backtest_intraday.json")
SWING_RECORD = os.path.join(HERE, "backtest_swing.json")

# An expectancy of +0.5R is a strong rule; scaled here it earns 12 points,
# meaningful next to a ~25-point BUY threshold but not sufficient alone.
SCALE = 24.0
MAX_POINTS_PER_RULE = 12.0
MAX_POINTS_TOTAL = 20.0

_CACHE = {"intraday": None, "swing": None, "mtime": {}}


def _load(path, key):
    """Read a record file, reloading only when it changes on disk."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _CACHE[key] = {}
        return {}
    if _CACHE[key] is not None and _CACHE["mtime"].get(key) == mtime:
        return _CACHE[key]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        record = blob.get("strategies") or {}
    except (OSError, ValueError):
        record = {}
    _CACHE[key] = record
    _CACHE["mtime"][key] = mtime
    return record


def intraday_record():
    return _load(INTRADAY_RECORD, "intraday")


def swing_record():
    return _load(SWING_RECORD, "swing")


def _points_for(stat):
    """Points a single rule earns, or None when it has not earned the right."""
    if not stat or not stat.get("trades") or not stat.get("enough"):
        return None
    expectancy = stat.get("expectancy_r")
    if expectancy is None:
        return None
    points = expectancy * SCALE
    return max(-MAX_POINTS_PER_RULE, min(MAX_POINTS_PER_RULE, points))


def edge(fired, record):
    """
    Turn the rules firing right now into points for and against.

    `fired` maps strategy name -> a short description of what triggered.
    Returns the two tallies plus the rules that were skipped for want of
    evidence, so the caller can say so rather than silently ignoring them.
    """
    for_points, against_points = 0.0, 0.0
    for_reasons, against_reasons, untrusted = [], [], []

    for name, detail in (fired or {}).items():
        stat = record.get(name) or {}
        points = _points_for(stat)
        if points is None:
            untrusted.append(name)
            continue

        trades = stat.get("trades")
        win = stat.get("win_rate_pct")
        expectancy = stat.get("expectancy_r")
        note = (f"{name}: {detail}. Measured on this universe over "
                f"{trades:,} trades — won {win}%, expectancy "
                f"{expectancy:+.3f}R per trade")

        if points >= 0:
            for_points += points
            for_reasons.append(note)
        else:
            # A rule that lost money here argues against the trade. Dropping
            # it would be the same as pretending we never measured it.
            against_points += abs(points)
            against_reasons.append(
                f"{name}: {detail}, but this rule *lost* money on this "
                f"universe — {expectancy:+.3f}R over {trades:,} trades")

    return {
        "for_points": round(min(for_points, MAX_POINTS_TOTAL), 1),
        "against_points": round(min(against_points, MAX_POINTS_TOTAL), 1),
        "for_reasons": for_reasons,
        "against_reasons": against_reasons,
        "untrusted": untrusted,
        "measured": bool(record),
    }


def intraday_edge(fired):
    return edge(fired, intraday_record())


def swing_edge(fired):
    return edge(fired, swing_record())


def describe():
    """A one-line summary of what evidence the engine currently has."""
    intra, swing = intraday_record(), swing_record()
    if not intra and not swing:
        return ("no measured strategy record — run backtest_intraday.py and "
                "backtest_swing.py to let the rules vote")
    parts = []
    if intra:
        good = sum(1 for s in intra.values()
                   if s.get("enough") and (s.get("expectancy_r") or 0) > 0)
        parts.append(f"{good}/{len(intra)} intraday rules with a positive record")
    if swing:
        good = sum(1 for s in swing.values()
                   if s.get("enough") and (s.get("expectancy_r") or 0) > 0)
        parts.append(f"{good}/{len(swing)} swing rules with a positive record")
    return " · ".join(parts)
