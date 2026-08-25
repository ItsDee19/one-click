"""
scoring.py — the deterministic panel, on two horizons.

Every stock is judged twice, because the same evidence supports two different
questions:

  intraday   — is there a move to trade inside this session? Reads VWAP, the
               opening range, the gap and time-adjusted RVOL. Dies at the bell.
  positional — is there a move worth holding for? Reads trend, the 52-week
               position, analyst headroom and news. Carries a holding window
               derived from how far the target is and how fast this particular
               stock actually moves.

This is the engine of last resort: no LLM, no API key, no network, nothing
outside the standard library. It must ALWAYS return both tracks, even when
half the evidence is None.

Grounding rule (shared with the LLM engine): every figure in a `reasons`
string is read straight out of the evidence bundle. When a value is missing
the agent says "data unavailable" and the rule simply does not fire.

Nothing here is advice, and no figure here is a promise. A holding window is
an order-of-magnitude estimate of how long a thesis needs, not a forecast that
it will work.
"""

from __future__ import annotations

ENGINE_NAME = "deterministic"

AGENT_KEYS = ("bull", "bear", "fundamentals", "technicals", "news")
TRACKS = ("intraday", "positional")

# --- positional judge thresholds -------------------------------------------
BUY_NET = 25
AVOID_NET = -15
LEADERSHIP_POSITION = 60
LEADERSHIP_RVOL = 3.0

# --- intraday judge thresholds ---------------------------------------------
INTRADAY_BUY_NET = 25
INTRADAY_AVOID_NET = -15
INTRADAY_MIN_RVOL = 1.5
# Below this many minutes left, a fresh intraday long has no room to work.
INTRADAY_MIN_MINUTES_LEFT = 45

# Minimum reward-to-risk before a BUY is allowed, measured on the track's own
# objective and invalidation levels.
MIN_RR_INTRADAY = 1.5
MIN_RR_POSITIONAL = 1.5

# --- holding-window model ---------------------------------------------------
# Share of a stock's average daily range that accrues as *net* directional
# drift on a trending day. Stated openly because the whole holding window
# hangs off it: it is an assumption, not a measurement.
DRIFT_SHARE_OF_ATR = 0.30
HORIZON_BAND_LOW = 0.6
HORIZON_BAND_HIGH = 1.5
MIN_HOLD_DAYS = 10          # two trading weeks
MAX_HOLD_DAYS = 250         # sell-side targets are 12-month by convention


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _get(evidence, *path):
    node = evidence
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _clamp(value, low, high):
    return max(low, min(high, value))


def _fmt(value, suffix="", digits=2):
    if value is None:
        return "data unavailable"
    if isinstance(value, str):
        return value
    return f"{round(value, digits):g}{suffix}"


def _sentence(text):
    text = (text or "").strip()
    if not text:
        return ""
    if not text[0].isdigit() and not text[:1].isupper():
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


class _Tally:
    """Accumulates points and the human-readable reason behind each one."""

    def __init__(self, start=0.0):
        self.points = float(start)
        self.reasons = []

    def add(self, points, reason):
        if points <= 0:
            return
        self.points += points
        self.reasons.append(reason)

    def note(self, reason):
        self.reasons.append(reason)

    def score(self):
        return int(round(_clamp(self.points, 0, 100)))


# --------------------------------------------------------------------------
# positional seats
# --------------------------------------------------------------------------

def _bull_case(ev) -> _Tally:
    t = _Tally()

    rvol = _get(ev, "technicals", "rvol")
    if rvol is not None and rvol >= 1.5:
        t.add(min(20.0, (rvol - 1.0) * 10.0),
              f"RVOL {_fmt(rvol)}x — participation well above its own average")

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

    sector_rel = _get(ev, "relative", "sector_rel_pct")
    if sector_rel is not None and sector_rel > 1.0:
        t.add(min(10.0, sector_rel),
              f"leading its own sector by {_fmt(sector_rel, '%')} today")

    # Strength that is genuinely the stock's own, not the index carrying it
    rel_window = _get(ev, "relative", "rel_window_return_pct")
    if rel_window is not None and rel_window > 0:
        t.add(min(12.0, rel_window),
              f"outperforming the {_get(ev, 'relative', 'benchmark')} by "
              f"{_fmt(rel_window, '%')} over the window")

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

    move_vs_atr = _get(ev, "technicals", "move_vs_atr")
    day_change = _get(ev, "price", "day_change_pct")
    if move_vs_atr is not None and move_vs_atr >= 2.5 and (day_change or 0) > 0:
        t.add(min(14.0, (move_vs_atr - 1.5) * 6.0),
              f"today's {_fmt(day_change, '%')} is {_fmt(move_vs_atr)}x its average daily "
              f"range of {_fmt(_get(ev, 'technicals', 'atr_pct'), '%')} — extended, "
              f"poor place to start a position")

    sector_rel = _get(ev, "relative", "sector_rel_pct")
    if sector_rel is not None and sector_rel < -1.0:
        t.add(min(10.0, -sector_rel),
              f"lagging its own sector by {_fmt(abs(sector_rel), '%')} today")

    rel_window = _get(ev, "relative", "rel_window_return_pct")
    if rel_window is not None and rel_window < 0:
        t.add(min(12.0, -rel_window),
              f"lagging the {_get(ev, 'relative', 'benchmark')} by "
              f"{_fmt(abs(rel_window), '%')} over the window")

    days_to_earnings = _get(ev, "events", "days_to_earnings")
    if days_to_earnings is not None and days_to_earnings <= 7:
        t.add(8.0, f"earnings in {int(days_to_earnings)} day(s) "
                   f"({_get(ev, 'events', 'next_earnings')}) — binary event risk")

    gaps = ev.get("data_gaps") or []
    if len(gaps) >= 6:
        t.add(6.0, f"{len(gaps)} fields could not be computed — thin evidence base")

    if not t.reasons:
        t.note("no bearish trigger present in the evidence")
    return t


# --------------------------------------------------------------------------
# intraday seats
# --------------------------------------------------------------------------

def _intraday_bull(ev) -> _Tally:
    t = _Tally()

    vs_vwap = _get(ev, "intraday", "price_vs_vwap_pct")
    if vs_vwap is not None and vs_vwap > 0:
        t.add(18.0, f"{_fmt(vs_vwap, '%')} above VWAP {_fmt(_get(ev, 'intraday', 'vwap'))} — "
                    f"buyers control the session")

    if _get(ev, "intraday", "above_opening_range") is True:
        t.add(15.0, f"holding above the opening range high of "
                    f"{_fmt(_get(ev, 'intraday', 'opening_range_high'))}")

    rvol = _get(ev, "technicals", "rvol")
    if rvol is not None and rvol >= INTRADAY_MIN_RVOL:
        t.add(min(20.0, (rvol - 1.0) * 10.0),
              f"RVOL {_fmt(rvol)}x ({_get(ev, 'technicals', 'rvol_method')}) — real participation")

    gap = _get(ev, "intraday", "gap_pct")
    if gap is not None:
        if 0.5 <= gap <= 4.0:
            t.add(10.0, f"constructive {_fmt(gap, '%')} gap up, still holding")
        elif gap > 6.0:
            t.note(f"{_fmt(gap, '%')} gap is large enough to be exhaustion — no credit taken")

    day_pos = _get(ev, "technicals", "day_range_position_pct")
    if day_pos is not None and day_pos >= 70:
        t.add(12.0, f"trading at {_fmt(day_pos, '%')} of the day's range")

    change = _get(ev, "price", "day_change_pct")
    if change is not None and change > 0:
        t.add(min(8.0, change * 2.0), f"up {_fmt(change, '%')} on the day")

    tone = _get(ev, "news", "net_tone")
    if tone is not None and tone > 0:
        t.add(min(8.0, tone * 3.0), f"news tone net +{int(tone)} today")

    if not t.reasons:
        t.note("nothing in the session tape supports a long")
    return t


def _intraday_bear(ev) -> _Tally:
    t = _Tally()

    vs_vwap = _get(ev, "intraday", "price_vs_vwap_pct")
    if vs_vwap is not None and vs_vwap < 0:
        t.add(20.0, f"{_fmt(abs(vs_vwap), '%')} below VWAP {_fmt(_get(ev, 'intraday', 'vwap'))} — "
                    f"sellers control the session")

    if _get(ev, "intraday", "above_opening_range") is False:
        t.add(15.0, f"never cleared the opening range high of "
                    f"{_fmt(_get(ev, 'intraday', 'opening_range_high'))}")

    rvol = _get(ev, "technicals", "rvol")
    if rvol is not None and rvol < 0.8:
        t.add(12.0, f"RVOL {_fmt(rvol)}x ({_get(ev, 'technicals', 'rvol_method')}) — "
                    f"nobody is showing up for this move")

    gap = _get(ev, "intraday", "gap_pct")
    change = _get(ev, "price", "day_change_pct")
    if gap is not None and gap < -0.5:
        t.add(10.0, f"opened {_fmt(gap, '%')} below yesterday's close")
    if gap is not None and change is not None and gap > 1.0 and change < gap:
        t.add(10.0, f"gapped {_fmt(gap, '%')} up but has given back into the session")

    move_vs_atr = _get(ev, "technicals", "move_vs_atr")
    if move_vs_atr is not None and move_vs_atr >= 2.0:
        t.add(min(15.0, (move_vs_atr - 1.0) * 7.0),
              f"already {_fmt(move_vs_atr)}x a normal day's range — the move to trade "
              f"has largely happened")

    day_pos = _get(ev, "technicals", "day_range_position_pct")
    if day_pos is not None and day_pos <= 30:
        t.add(12.0, f"stuck at {_fmt(day_pos, '%')} of the day's range")

    tone = _get(ev, "news", "net_tone")
    if tone is not None and tone < 0:
        t.add(min(10.0, -tone * 3.0), f"news tone net {int(tone)} today")

    if not t.reasons:
        t.note("nothing in the session tape argues against a long")
    return t


# --------------------------------------------------------------------------
# supporting seats (shared by both tracks)
# --------------------------------------------------------------------------

def _technicals_seat(ev) -> _Tally:
    t = _Tally(50.0)
    rvol = _get(ev, "technicals", "rvol")
    vs_sma = _get(ev, "technicals", "price_vs_sma_pct")
    trend = _get(ev, "technicals", "trend")
    window = _get(ev, "technicals", "window_return_pct")
    position = _get(ev, "range_52w", "position_pct")

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
    t = _Tally(50.0)
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
    t = _Tally(50.0)
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
# holding window — the "how long do I hold this" answer
# --------------------------------------------------------------------------

def holding_window(ev) -> dict:
    """
    Roughly how long a positional thesis needs to play out.

    Distance to the mean analyst target, divided by the share of this stock's
    own average daily range that tends to accrue as net direction. Expressed
    as a band, capped at twelve months because that is the horizon sell-side
    targets are set on.

    This is arithmetic on two evidence figures and one stated assumption. It
    is emphatically NOT a claim that the position becomes profitable in that
    time, or at all.
    """
    upside = _get(ev, "analyst", "upside_pct")
    atr_pct = _get(ev, "technicals", "atr_pct")

    if upside is None or atr_pct is None or atr_pct <= 0:
        missing = "analyst target" if upside is None else "daily range (ATR)"
        return {
            "days_min": None, "days_max": None, "label": "data unavailable",
            "basis": f"{missing} data unavailable — no holding window can be derived",
        }

    if upside <= 0:
        return {
            "days_min": None, "days_max": None, "label": "no headroom",
            "basis": f"price is already at or through the mean target "
                     f"({_fmt(upside, '%')} headroom) — nothing to wait for",
        }

    drift = atr_pct * DRIFT_SHARE_OF_ATR
    base_days = upside / drift
    days_min = int(_clamp(round(base_days * HORIZON_BAND_LOW), MIN_HOLD_DAYS, MAX_HOLD_DAYS))
    days_max = int(_clamp(round(base_days * HORIZON_BAND_HIGH), MIN_HOLD_DAYS, MAX_HOLD_DAYS))
    if days_max <= days_min:
        days_max = min(MAX_HOLD_DAYS, days_min + 5)

    return {
        "days_min": days_min,
        "days_max": days_max,
        "label": _horizon_label(days_min, days_max),
        "basis": (
            f"{_fmt(upside, '%')} to the mean target at an average daily range of "
            f"{_fmt(atr_pct, '%')}, assuming ~{int(DRIFT_SHARE_OF_ATR * 100)}% of that "
            f"range accrues as net drift"
        ),
    }


def risk_reward(price, levels) -> dict:
    """
    Distance to the objective against distance to the invalidation.

    A high-conviction setup that risks more than it stands to make is still a
    bad trade, and score alone can never see that — the levels have to be
    compared. Returns nulls when either level is missing rather than guessing
    a stop.
    """
    out = {"risk_pct": None, "reward_pct": None, "ratio": None}
    if price is None or not levels:
        return out

    objective = levels.get("objective")
    invalidation = levels.get("invalidation")
    if objective is None or invalidation is None or not price:
        return out
    if invalidation >= price or objective <= price:
        return out          # stop above price or target below it: not a long

    risk = (price - invalidation) / price * 100.0
    reward = (objective - price) / price * 100.0
    if risk <= 0:
        return out

    out["risk_pct"] = round(risk, 2)
    out["reward_pct"] = round(reward, 2)
    out["ratio"] = round(reward / risk, 2)
    return out


def _apply_rr_gate(verdict_block, ev, minimum):
    """Hold a BUY down to WATCH when the levels do not justify it."""
    if verdict_block.get("verdict") != "BUY":
        return verdict_block

    rr = risk_reward(_get(ev, "price", "live"), verdict_block.get("levels"))
    verdict_block["risk_reward"] = rr

    if rr["ratio"] is None or rr["ratio"] >= minimum:
        return verdict_block

    verdict_block["verdict"] = "WATCH"
    verdict_block["confidence"] = min(6, verdict_block.get("confidence") or 6)
    verdict_block["gated"] = True
    verdict_block["rationale"] = (
        f"Held to WATCH on risk/reward: {rr['reward_pct']}% to the objective "
        f"against {rr['risk_pct']}% to the invalidation is {rr['ratio']}:1, "
        f"under the {minimum}:1 the desk requires. "
        f"Original read: {verdict_block['rationale']}"
    )
    return verdict_block


def _horizon_label(days_min, days_max):
    """Trading days -> a phrase a human reads without converting anything."""
    def phrase(days):
        if days < 10:
            return f"{days}d"
        weeks = days / 5.0
        if weeks < 8:
            return f"{round(weeks)}w"
        return f"{round(days / 21.0)}mo"

    low, high = phrase(days_min), phrase(days_max)
    return low if low == high else f"{low}–{high}"


# --------------------------------------------------------------------------
# judges
# --------------------------------------------------------------------------

def judge_positional(ev, bull_score, bear_score, bull_reasons, bear_reasons) -> dict:
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
    lead = bull_reasons if winner == "Bull" else bear_reasons
    headline = _sentence(lead[0]) if lead else None
    window = holding_window(ev)

    if verdict == "BUY":
        rationale = (f"Bull {bull_score} vs Bear {bear_score} (net +{net}) with confirmation. "
                     f"{headline or 'Momentum is intact.'}")
    elif verdict == "AVOID":
        rationale = (f"Bear {bear_score} outweighs Bull {bull_score} (net {net}). "
                     f"{headline or 'Risk/reward is unfavourable.'}")
    elif net >= BUY_NET:
        rationale = (f"Bull {bull_score} vs Bear {bear_score} (net +{net}) — the case is there, "
                     f"but neither 52-week position nor volume confirms leadership yet.")
    elif net > 0:
        rationale = (f"Bull {bull_score} vs Bear {bear_score} (net +{net}) — a mild edge to the "
                     f"bulls, not enough to act on.")
    else:
        rationale = (f"Bull {bull_score} vs Bear {bear_score} (net {net}) — the two sides "
                     f"roughly cancel; nothing decisive either way.")

    block = {
        "track": "positional",
        "verdict": verdict,
        "confidence": confidence,
        "winner": winner,
        "rationale": rationale,
        "key_catalyst": lead[0] if lead else "no single dominant factor in the evidence",
        "bull_score": bull_score,
        "bear_score": bear_score,
        "net": net,
        "horizon": window["label"],
        "horizon_days_min": window["days_min"],
        "horizon_days_max": window["days_max"],
        "horizon_basis": window["basis"],
        "levels": _positional_levels(ev),
    }
    return _apply_rr_gate(block, ev, MIN_RR_POSITIONAL)


def judge_intraday(ev, bull_score, bear_score, bull_reasons, bear_reasons) -> dict:
    intraday = ev.get("intraday") or {}
    phase = (ev.get("market") or {})

    # No session behind the numbers -> no intraday call. This is the 09:00 case.
    if not intraday.get("available"):
        return _intraday_unavailable(
            intraday.get("reason") or "no intraday session data", ev)

    net = bull_score - bear_score
    above_vwap = (_get(ev, "intraday", "price_vs_vwap_pct") or 0) > 0
    above_or = _get(ev, "intraday", "above_opening_range") is True
    rvol = _get(ev, "technicals", "rvol")
    rvol_ok = rvol is not None and rvol >= INTRADAY_MIN_RVOL
    minutes_left = phase.get("minutes_to_close") or 0

    confirmed = above_vwap and above_or and rvol_ok

    if net >= INTRADAY_BUY_NET and confirmed and minutes_left >= INTRADAY_MIN_MINUTES_LEFT:
        verdict = "BUY"
    elif net <= INTRADAY_AVOID_NET:
        verdict = "AVOID"
    else:
        verdict = "WATCH"

    confidence = int(_clamp(round(4 + net / 15.0), 1, 10))
    confidence = max(7, confidence) if verdict == "BUY" else min(6, confidence)

    winner = "Bull" if bull_score >= bear_score else "Bear"
    lead = bull_reasons if winner == "Bull" else bear_reasons
    headline = _sentence(lead[0]) if lead else None

    if verdict == "BUY":
        rationale = (f"Bull {bull_score} vs Bear {bear_score} (net +{net}), confirmed above both "
                     f"VWAP and the opening range. {headline or ''}").strip()
    elif verdict == "AVOID":
        rationale = (f"Bear {bear_score} outweighs Bull {bull_score} (net {net}) on the session "
                     f"tape. {headline or ''}").strip()
    elif net >= INTRADAY_BUY_NET and minutes_left < INTRADAY_MIN_MINUTES_LEFT:
        rationale = (f"Setup is there (net +{net}) but only {minutes_left} minutes remain — "
                     f"too late in the session to start a new intraday position.")
    elif net >= INTRADAY_BUY_NET:
        missing = []
        if not above_vwap:
            missing.append("price is not above VWAP")
        if not above_or:
            missing.append("the opening range high is not cleared")
        if not rvol_ok:
            missing.append(f"RVOL {_fmt(rvol)}x is under {INTRADAY_MIN_RVOL}x")
        rationale = (f"Net +{net} on the tape, but unconfirmed: {'; '.join(missing)}.")
    else:
        rationale = (f"Bull {bull_score} vs Bear {bear_score} (net {net}) — the session tape "
                     f"offers no decisive intraday edge.")

    block = {
        "track": "intraday",
        "verdict": verdict,
        "confidence": confidence,
        "winner": winner,
        "rationale": rationale,
        "key_catalyst": lead[0] if lead else "no single dominant factor on the tape",
        "bull_score": bull_score,
        "bear_score": bear_score,
        "net": net,
        "horizon": f"same session — {minutes_left} min to close" if minutes_left
                   else "same session",
        "horizon_days_min": 0,
        "horizon_days_max": 0,
        "horizon_basis": "intraday positions are closed before the bell by definition",
        "levels": _intraday_levels(ev),
        "minutes_to_close": minutes_left,
    }
    return _apply_rr_gate(block, ev, MIN_RR_INTRADAY)


def _intraday_unavailable(reason, ev) -> dict:
    """
    The honest pre-open answer.

    Before 09:15 there is no open, no range and no volume — nothing an
    intraday call can rest on. Rather than reuse yesterday's tape and pretend,
    the track reports UNAVAILABLE and hands over the levels worth watching
    when the session does start.
    """
    prev_close = _get(ev, "price", "prev_close")
    swing_high = _get(ev, "technicals", "swing_high")
    swing_low = _get(ev, "technicals", "swing_low")

    return {
        "track": "intraday",
        "verdict": "UNAVAILABLE",
        "confidence": None,
        "winner": None,
        "rationale": f"No intraday call possible: {reason}. VWAP, the opening range and "
                     f"today's RVOL do not exist until the session has traded.",
        "key_catalyst": "watch the open, then re-run once the first 30 minutes have printed",
        "bull_score": None,
        "bear_score": None,
        "net": None,
        "horizon": "same session (once open)",
        "horizon_days_min": 0,
        "horizon_days_max": 0,
        "horizon_basis": "not applicable before the session opens",
        "levels": {
            "reference": prev_close,
            "watch_above": swing_high,
            "watch_below": swing_low,
            "note": "previous close, and the recent swing high/low from the daily window",
        },
        "minutes_to_close": 0,
    }


def _intraday_levels(ev) -> dict:
    """Levels that define the intraday setup — all read from the evidence."""
    price = _get(ev, "price", "live")
    vwap = _get(ev, "intraday", "vwap")
    or_high = _get(ev, "intraday", "opening_range_high")
    or_low = _get(ev, "intraday", "opening_range_low")
    atr_pct = _get(ev, "technicals", "atr_pct")

    # Invalidation is the nearest structural support beneath price.
    below = [level for level in (vwap, or_low) if level is not None and price is not None
             and level < price]
    invalidation = max(below) if below else or_low

    objective = None
    if price is not None and atr_pct:
        objective = round(price * (1 + atr_pct / 100.0), 2)

    return {
        "trigger": or_high,
        "invalidation": invalidation,
        "objective": objective,
        "note": "trigger = opening range high; invalidation = nearest of VWAP / opening "
                "range low below price; objective = one average daily range higher",
    }


def _positional_levels(ev) -> dict:
    price = _get(ev, "price", "live")
    sma_pct = _get(ev, "technicals", "price_vs_sma_pct")
    sma = None
    if price is not None and sma_pct is not None and sma_pct != -100:
        sma = round(price / (1 + sma_pct / 100.0), 2)

    return {
        "trigger": _get(ev, "technicals", "swing_high"),
        "invalidation": sma if sma is not None else _get(ev, "technicals", "swing_low"),
        "objective": _get(ev, "analyst", "target_mean"),
        "note": f"trigger = recent swing high; invalidation = the "
                f"{_get(ev, 'technicals', 'sma_period')}-day SMA; objective = mean analyst target",
    }


# --------------------------------------------------------------------------
# public interface — identical shape to llm.evaluate()
# --------------------------------------------------------------------------

def evaluate(evidence: dict) -> dict:
    """evidence -> {scores, tracks: {intraday, positional}, ...}"""
    bull = _bull_case(evidence)
    bear = _bear_case(evidence)
    ib = _intraday_bull(evidence)
    ibear = _intraday_bear(evidence)

    scores = {
        "bull": {"score": bull.score(), "reasons": bull.reasons},
        "bear": {"score": bear.score(), "reasons": bear.reasons},
        "fundamentals": _as_entry(_fundamentals_seat(evidence)),
        "technicals": _as_entry(_technicals_seat(evidence)),
        "news": _as_entry(_news_seat(evidence)),
    }

    tracks = {
        "positional": judge_positional(evidence, scores["bull"]["score"],
                                       scores["bear"]["score"], bull.reasons, bear.reasons),
        "intraday": judge_intraday(evidence, ib.score(), ibear.score(),
                                   ib.reasons, ibear.reasons),
    }

    return {
        "scores": scores,
        "tracks": tracks,
        "engine": ENGINE_NAME,
        "ungrounded_numbers": [],   # rule engine only ever quotes evidence values
    }


def _as_entry(tally: _Tally) -> dict:
    return {"score": tally.score(), "reasons": tally.reasons}
