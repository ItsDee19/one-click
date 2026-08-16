"""
scoring.py — the deterministic panel.

This is the engine of last resort: no LLM, no API key, no network, no
dependencies beyond the standard library. It must ALWAYS return a verdict,
even when half the evidence bundle is None.

Grounding rule (same as the LLM engine): every figure in a `reasons` string is
read straight out of the evidence bundle. When a value is missing the agent
says "data unavailable" and the corresponding rule simply does not fire.
"""

from __future__ import annotations

ENGINE_NAME = "deterministic"

AGENT_KEYS = ("bull", "bear", "fundamentals", "technicals", "news")

# Judge thresholds — kept as named constants so the README and the UI can
# quote the same numbers the code actually uses.
BUY_NET = 25
AVOID_NET = -15
LEADERSHIP_POSITION = 60
LEADERSHIP_RVOL = 3.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _get(evidence, *path):
    """Safe nested read: _get(ev, "technicals", "rvol") -> value or None."""
    node = evidence
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _clamp(value, low, high):
    return max(low, min(high, value))


def _fmt(value, suffix="", digits=2):
    """Format a number for a reason string, or the honest fallback."""
    if value is None:
        return "data unavailable"
    if isinstance(value, str):
        return value
    return f"{round(value, digits):g}{suffix}"


def _sentence(text):
    """Turn a reason fragment into a standalone sentence."""
    text = (text or "").strip()
    if not text:
        return ""
    if not text[0].isdigit() and not text[:1].isupper():
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


class _Tally:
    """Accumulates points and the human-readable reason behind each one."""

    def __init__(self):
        self.points = 0.0
        self.reasons = []

    def add(self, points, reason):
        if points <= 0:
            return
        self.points += points
        self.reasons.append(reason)

    def note(self, reason):
        """Record an observation that carries no points (e.g. a data gap)."""
        self.reasons.append(reason)

    def score(self):
        return int(round(_clamp(self.points, 0, 100)))


# --------------------------------------------------------------------------
# the five debating seats
# --------------------------------------------------------------------------

def _bull_case(ev) -> _Tally:
    t = _Tally()

    rvol = _get(ev, "technicals", "rvol")
    if rvol is not None and rvol >= 1.5:
        t.add(min(20.0, (rvol - 1.0) * 10.0), f"RVOL {_fmt(rvol)}x — participation well above its own average")

    position = _get(ev, "range_52w", "position_pct")
    if position is not None and position >= 85:
        t.add(15.0, f"trading at {_fmt(position, '%')} of the 52-week range — breakout territory")

    vs_sma = _get(ev, "technicals", "price_vs_sma_pct")
    trend = _get(ev, "technicals", "trend")
    period = _get(ev, "technicals", "sma_period")
    if vs_sma is not None and vs_sma > 0:
        if trend == "up":
            t.add(15.0, f"{_fmt(vs_sma, '%')} above a rising {period}-day SMA")
        else:
            t.add(8.0, f"{_fmt(vs_sma, '%')} above the {period}-day SMA, trend {trend or 'unclassified'}")

    day_pos = _get(ev, "technicals", "day_range_position_pct")
    if day_pos is not None and day_pos >= 70:
        t.add(10.0, f"closing at {_fmt(day_pos, '%')} of the day's range — buyers held the top")

    upside = _get(ev, "analyst", "upside_pct")
    if upside is not None and upside >= 10:
        t.add(min(20.0, upside / 2.0),
              f"{_fmt(upside, '%')} upside to the mean analyst target of {_fmt(_get(ev, 'analyst', 'target_mean'))}")

    buy_pct = _get(ev, "analyst", "buy_pct")
    if buy_pct is not None and buy_pct >= 80:
        t.add(10.0, f"{_fmt(buy_pct, '%')} of {_fmt(_get(ev, 'analyst', 'num_analysts'), '', 0)} analysts rate it buy")

    net_tone = _get(ev, "news", "net_tone")
    if net_tone is not None and net_tone > 0:
        t.add(min(10.0, net_tone * 4.0),
              f"news tone net +{int(net_tone)} across {_fmt(_get(ev, 'news', 'total'), '', 0)} headlines")

    window = _get(ev, "technicals", "window_return_pct")
    if window is not None and window > 0:
        t.add(min(10.0, window / 2.0), f"up {_fmt(window, '%')} over the pulled window")

    if not t.reasons:
        t.note("no bullish trigger present in the evidence")
    return t


def _bear_case(ev) -> _Tally:
    t = _Tally()

    rvol = _get(ev, "technicals", "rvol")
    if rvol is not None and rvol < 1:
        t.add(12.0, f"RVOL {_fmt(rvol)}x — the move is happening on below-average volume")

    position = _get(ev, "range_52w", "position_pct")
    if position is not None and position < 30:
        t.add(15.0, f"only {_fmt(position, '%')} up the 52-week range — sitting near the lows")

    vs_sma = _get(ev, "technicals", "price_vs_sma_pct")
    period = _get(ev, "technicals", "sma_period")
    if vs_sma is not None and vs_sma < 0:
        t.add(12.0, f"{_fmt(abs(vs_sma), '%')} below the {period}-day SMA")

    if _get(ev, "technicals", "trend") == "down":
        t.add(8.0, f"{period}-day trend classified down")

    upside = _get(ev, "analyst", "upside_pct")
    if upside is not None and upside <= 0:
        t.add(15.0, f"price is already at or through the mean target — {_fmt(upside, '%')} headroom")

    buy_pct = _get(ev, "analyst", "buy_pct")
    if buy_pct is not None and buy_pct < 55:
        t.add(10.0, f"only {_fmt(buy_pct, '%')} of the desk is on buy — weak conviction")

    from_high = _get(ev, "range_52w", "pct_from_high")
    if from_high is not None and from_high <= -20:
        t.add(12.0, f"{_fmt(from_high, '%')} from the 52-week high")

    sell_pct = _get(ev, "analyst", "sell_pct")
    if sell_pct is not None and sell_pct >= 20:
        t.add(8.0, f"{_fmt(sell_pct, '%')} of analysts carry an outright sell")

    net_tone = _get(ev, "news", "net_tone")
    if net_tone is not None and net_tone < 0:
        t.add(min(12.0, -net_tone * 4.0),
              f"news tone net {int(net_tone)} across {_fmt(_get(ev, 'news', 'total'), '', 0)} headlines")

    day_pos = _get(ev, "technicals", "day_range_position_pct")
    if day_pos is not None and day_pos <= 30:
        t.add(8.0, f"closed at only {_fmt(day_pos, '%')} of the day's range — sellers had the last word")

    gaps = ev.get("data_gaps") or []
    if len(gaps) >= 6:
        t.add(6.0, f"{len(gaps)} fields could not be computed — thin evidence base")

    if not t.reasons:
        t.note("no bearish trigger present in the evidence")
    return t


def _technicals_seat(ev) -> _Tally:
    t = _Tally()
    rvol = _get(ev, "technicals", "rvol")
    vs_sma = _get(ev, "technicals", "price_vs_sma_pct")
    trend = _get(ev, "technicals", "trend")
    window = _get(ev, "technicals", "window_return_pct")
    position = _get(ev, "range_52w", "position_pct")

    t.points = 50.0  # neutral start; this seat reads the tape rather than argues
    if rvol is None:
        t.note("RVOL data unavailable")
    else:
        t.points += _clamp((rvol - 1.0) * 12.0, -15.0, 20.0)
        t.reasons.append(f"RVOL {_fmt(rvol)}x")
    if vs_sma is None:
        t.note(f"{_get(ev, 'technicals', 'sma_period')}-day SMA data unavailable")
    else:
        t.points += _clamp(vs_sma, -15.0, 15.0)
        t.reasons.append(f"{_fmt(vs_sma, '%')} vs {_get(ev, 'technicals', 'sma_period')}-day SMA")
    if trend:
        t.points += {"up": 10.0, "sideways": 0.0, "down": -12.0}.get(trend, 0.0)
        t.reasons.append(f"trend {trend}")
    if window is not None:
        t.points += _clamp(window / 2.0, -10.0, 10.0)
        t.reasons.append(f"window return {_fmt(window, '%')}")
    if position is not None:
        t.points += _clamp((position - 50.0) / 5.0, -10.0, 10.0)
        t.reasons.append(f"{_fmt(position, '%')} of the 52-week range")
    return t


def _fundamentals_seat(ev) -> _Tally:
    t = _Tally()
    t.points = 50.0
    upside = _get(ev, "analyst", "upside_pct")
    buy_pct = _get(ev, "analyst", "buy_pct")
    consensus = _get(ev, "analyst", "consensus")
    num = _get(ev, "analyst", "num_analysts")

    if upside is None:
        t.note("analyst target data unavailable")
    else:
        t.points += _clamp(upside, -25.0, 25.0)
        t.reasons.append(f"{_fmt(upside, '%')} to mean target {_fmt(_get(ev, 'analyst', 'target_mean'))}")
    if buy_pct is None:
        t.note("buy/hold/sell split data unavailable")
    else:
        t.points += _clamp((buy_pct - 50.0) / 3.0, -15.0, 15.0)
        t.reasons.append(f"{_fmt(buy_pct, '%')} buy ratings")
    if consensus:
        t.reasons.append(f"consensus '{consensus}'" + (f" from {_fmt(num, '', 0)} analysts" if num else ""))
    t.note("no raw valuation ratios in this feed — target-based view only")
    return t


def _news_seat(ev) -> _Tally:
    t = _Tally()
    t.points = 50.0
    total = _get(ev, "news", "total")
    net_tone = _get(ev, "news", "net_tone")
    if not total:
        t.note("no headlines in the pulled window — data unavailable")
        return t
    t.points += _clamp((net_tone or 0) * 8.0, -30.0, 30.0)
    t.reasons.append(
        f"{int(total)} headlines: {int(_get(ev, 'news', 'positive') or 0)} positive, "
        f"{int(_get(ev, 'news', 'negative') or 0)} negative, net {int(net_tone or 0)}"
    )
    top = (_get(ev, "news", "recent") or [])[:1]
    if top:
        t.reasons.append(f"lead story reads {top[0].get('sentiment')}")
    return t


# --------------------------------------------------------------------------
# the judge
# --------------------------------------------------------------------------

def judge(ev, bull_score, bear_score, bull_reasons, bear_reasons) -> dict:
    net = bull_score - bear_score
    position = _get(ev, "range_52w", "position_pct")
    rvol = _get(ev, "technicals", "rvol")

    leadership = (
        (position is not None and position >= LEADERSHIP_POSITION)
        or (rvol is not None and rvol >= LEADERSHIP_RVOL)
    )

    if net >= BUY_NET and leadership:
        verdict = "BUY"
    elif net <= AVOID_NET:
        verdict = "AVOID"
    else:
        verdict = "WATCH"

    confidence = int(_clamp(round(4 + net / 15.0), 1, 10))
    confidence = max(7, confidence) if verdict == "BUY" else min(6, confidence)

    winner = "Bull" if bull_score >= bear_score else "Bear"
    lead = (bull_reasons if winner == "Bull" else bear_reasons)
    key_catalyst = lead[0] if lead else "no single dominant factor in the evidence"
    headline = _sentence(lead[0]) if lead else None

    if verdict == "BUY":
        rationale = (
            f"Bull {bull_score} vs Bear {bear_score} (net +{net}) with confirmation. "
            f"{headline or 'Momentum is intact.'}"
        )
    elif verdict == "AVOID":
        rationale = (
            f"Bear {bear_score} outweighs Bull {bull_score} (net {net}). "
            f"{headline or 'Risk/reward is unfavourable.'}"
        )
    elif net >= BUY_NET:
        rationale = (f"Bull {bull_score} vs Bear {bear_score} (net +{net}) — the case is there, "
                     f"but neither 52-week position nor volume confirms leadership yet.")
    elif net > 0:
        rationale = (f"Bull {bull_score} vs Bear {bear_score} (net +{net}) — a mild edge to the "
                     f"bulls, not enough to act on.")
    else:
        rationale = (f"Bull {bull_score} vs Bear {bear_score} (net {net}) — the two sides "
                     f"roughly cancel; nothing decisive either way.")

    return {
        "winner": winner,
        "verdict": verdict,
        "confidence": confidence,
        "rationale": rationale,
        "key_catalyst": key_catalyst,
        "bull_score": bull_score,
        "bear_score": bear_score,
        "net": net,
    }


# --------------------------------------------------------------------------
# public interface — identical shape to llm.evaluate()
# --------------------------------------------------------------------------

def evaluate(evidence: dict) -> dict:
    """evidence -> {scores: {agent: {score, reasons}}, verdict: {...}}"""
    bull = _bull_case(evidence)
    bear = _bear_case(evidence)

    scores = {
        "bull": {"score": bull.score(), "reasons": bull.reasons},
        "bear": {"score": bear.score(), "reasons": bear.reasons},
        "fundamentals": _as_entry(_fundamentals_seat(evidence)),
        "technicals": _as_entry(_technicals_seat(evidence)),
        "news": _as_entry(_news_seat(evidence)),
    }

    verdict = judge(evidence, scores["bull"]["score"], scores["bear"]["score"],
                    bull.reasons, bear.reasons)

    return {
        "scores": scores,
        "verdict": verdict,
        "engine": ENGINE_NAME,
        "ungrounded_numbers": [],   # rule engine only ever quotes evidence values
    }


def _as_entry(tally: _Tally) -> dict:
    return {"score": tally.score(), "reasons": tally.reasons}
