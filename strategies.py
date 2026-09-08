"""Shared causal intraday hypotheses and a cost-aware five-minute simulator.

The fifteen-minute NSE opening-range rules here are adaptations, not replicas
of Zarattini, Barbon and Aziz's 2024 US-stock study using five-minute ranges.
Research inspiration does not validate this implementation or its short side.

The backtest requests 60 calendar days of intraday history, not 60 trading
sessions. Actual complete sessions and independent evaluation dates must be
counted from returned data. Current-universe history is not survivorship-free.

Named rules produce confirmed patterns. Historical simulation uses subsequent
bar opens, adverse fills and declared cost assumptions. Both-level touches take
the stop; partial sessions remain open. Net expectancy, uncertainty, unseen
evaluation and forward results matter alongside win rate. No rule is promoted
to a proven strategy by this module.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

import trade_stats

IST = timezone(timedelta(hours=5, minutes=30))

BAR_MINUTES = 5
OPENING_RANGE_BARS = 3          # first 15 minutes
MIN_BARS_FOR_SETUP = 4
LAST_ENTRY_BAR = 60             # ~14:15; no new entries into the close
MIN_TRADES_TO_REPORT = 20       # below this a hit rate is noise, not evidence
SESSION_BARS = 75              # NSE cash regular session: 09:15 through 15:30 IST
ROUND_TRIP_COST_BPS = 10.0     # assumed total fees, not a broker-specific tariff
SLIPPAGE_BPS = 5.0             # adverse adjustment on each entry and exit
COST_MODEL_VERSION = "next-open-adverse-gap-v1"

# A trade risks (entry - stop); targets are expressed as a multiple of that,
# so expectancy is directly comparable across strategies.


def _f(value):
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


class Setup(dict):
    """One proposed trade. A dict so it serialises straight to the API."""

    def __init__(self, strategy, bar, entry, stop, target, direction, why):
        risk = abs(entry - stop) if entry is not None and stop is not None else None
        # A short is analysis too: this project never places an order, and
        # refusing to name a downside setup would just make the desk
        # one-sided rather than safer.
        super().__init__(
            strategy=strategy, bar=bar, entry=round(entry, 2), stop=round(stop, 2),
            target=round(target, 2), direction=direction, why=why,
            risk_per_share=round(risk, 2) if risk else None,
            reward_risk=round(abs(target - entry) / risk, 2) if risk else None,
        )


# ---------------------------------------------------------------------------
# the session, in a shape the strategies can read
# ---------------------------------------------------------------------------

def _timestamp(value):
    """Provider timestamps without an offset are explicitly interpreted as IST."""
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=IST)
        return value.astimezone(IST)
    except (ValueError, TypeError, OverflowError):
        return None


def session(bars, prev_close=None, avg_volume=None, rvol=None, now=None,
            research_compat=False, volume_profile=None):
    """
    Normalise one day of 5-minute bars, with running VWAP and the opening range.

    A timestamp is the bar's opening time. Strict sessions must begin at 09:15,
    remain contiguous, and contain only valid regular-session bars. When ``now``
    is supplied, a bar must have closed before it can inform a decision. The
    explicit timestamp-free compatibility mode is unvalidated research only.

    RVOL at a decision uses only volume through that bar divided by expected
    cumulative volume. A supplied profile must come exclusively from prior
    sessions. Otherwise a prior full-day average is scaled by elapsed bars;
    that is a causal approximation, not a measured time-of-day volume curve.
    Legacy scalar ``rvol`` is deliberately ignored: it cannot describe earlier
    decision bars without leaking subsequent information.
    """
    try:
        bars = list(bars)
    except TypeError:
        return None
    if not bars or any(not isinstance(b, dict) for b in bars):
        return None
    stamps = [_timestamp(b.get("timestamp")) for b in bars]
    timestamp_free = all(b.get("timestamp") is None for b in bars)
    validated = not timestamp_free
    if timestamp_free:
        if not research_compat or now is not None or len(bars) > SESSION_BARS:
            return None
    else:
        if any(t is None for t in stamps) or len(bars) > SESSION_BARS:
            return None
        origin = datetime.combine(stamps[0].date(), time(9, 15), tzinfo=IST)
        if any(t != origin + timedelta(minutes=i * BAR_MINUTES)
               for i, t in enumerate(stamps)):
            return None
        if now is not None:
            asof = _timestamp(now)
            if asof is None:
                return None
            closed = sum(t + timedelta(minutes=BAR_MINUTES) <= asof for t in stamps)
            bars, stamps = bars[:closed], stamps[:closed]
    if len(bars) < MIN_BARS_FOR_SETUP:
        return None
    o, h, l, c, v = [], [], [], [], []
    for b in bars:
        values = [_f(b.get(k)) for k in ("open", "high", "low", "close", "volume")]
        if any(value is None for value in values):
            return None
        op, hi, lo, cl, vol = values
        if min(op, hi, lo, cl) <= 0 or vol < 0 or not lo <= min(op, cl) <= max(op, cl) <= hi:
            return None
        o.append(op); h.append(hi); l.append(lo); c.append(cl); v.append(vol)
    n = len(bars)
    profile = None
    if volume_profile is not None:
        try:
            profile = [_f(value) for value in volume_profile]
        except TypeError:
            return None
        if (len(profile) != SESSION_BARS or any(x is None or x <= 0 for x in profile)
                or any(a > b for a, b in zip(profile, profile[1:]))):
            return None
    avg_volume = _f(avg_volume)
    avg_volume = avg_volume if avg_volume is not None and avg_volume > 0 else None
    prev_close = _f(prev_close)
    prev_close = prev_close if prev_close is not None and prev_close > 0 else None

    vwap, rvol_by_bar, turnover, traded = [], [], 0.0, 0.0
    for i in range(n):
        turnover += (h[i] / 3.0 + l[i] / 3.0 + c[i] / 3.0) * v[i]
        traded += v[i]
        if not math.isfinite(turnover) or not math.isfinite(traded):
            return None
        vwap.append(turnover / traded if traded > 0 else c[i])
        expected = profile[i] if profile is not None else (avg_volume * ((i + 1) / SESSION_BARS) if avg_volume else None)
        rvol_by_bar.append(_f(traded / expected) if expected else None)

    take = min(OPENING_RANGE_BARS, n)
    or_high, or_low = max(h[:take]), min(l[:take])

    day_volume = sum(v)
    gap = _f((o[0] - prev_close) / prev_close * 100.0) if prev_close else None

    return {
        "open": o, "high": h, "low": l, "close": c, "volume": v, "vwap": vwap,
        "n": n, "or_high": or_high, "or_low": or_low,
        "or_width": or_high - or_low,
        "prev_close": prev_close, "rvol": rvol_by_bar[-1], "gap_pct": gap,
        "day_volume": day_volume,
        "timestamps": stamps, "validated": validated,
        "complete": validated and n == SESSION_BARS,
        "rvol_by_bar": rvol_by_bar,
        "rvol_method": ("prior_session_cumulative_profile" if profile is not None else
                        "elapsed_scaled_prior_full_day_average" if avg_volume else "unavailable"),
        "timestamp_method": "bar_open_IST_naive_assumed" if validated else "unvalidated_index_only",
    }


def _rvol_at(s, i):
    values = s.get("rvol_by_bar", [])
    return _f(values[i]) if i < len(values) else None


# ---------------------------------------------------------------------------
# the strategies
# ---------------------------------------------------------------------------
# Each takes a normalised session and returns a Setup or None. They look only
# at bars up to the decision point — never at what happens afterwards, which
# is the whole discipline of this file.

def orb_breakout(s):
    """
    Fifteen-minute opening-range continuation hypothesis.

    The first 15 minutes set the range; a qualifying close confirms a signal.
    Historical entry is the next bar open. Abnormal participation is a required
    condition to test, measured causally at each decision bar.
    """
    if s["or_width"] <= 0:
        return None
    for i in range(OPENING_RANGE_BARS, min(s["n"], LAST_ENTRY_BAR)):
        rvol = _rvol_at(s, i)
        if rvol is None or rvol < 1.2:
            continue
        if s["close"][i] > s["or_high"] and s["close"][i] > s["vwap"][i]:
            entry = s["close"][i]
            stop = s["or_low"]
            if entry <= stop:
                return None
            return Setup("ORB breakout", i, entry, stop, entry + 2 * (entry - stop),
                         "long", f"closed above the 15-minute range high "
                                 f"{s['or_high']:.2f} and above VWAP")
    return None


def vwap_reclaim(s):
    """
    VWAP reclaim — price loses VWAP, takes it back, and holds.

    The reclaim rather than the first touch: a stock below VWAP that recovers
    it has absorbed the sellers who were offering into it, which is a
    different situation from one that never lost it.
    """
    lost = False
    for i in range(OPENING_RANGE_BARS, min(s["n"], LAST_ENTRY_BAR)):
        if s["close"][i] < s["vwap"][i]:
            lost = True
            continue
        if lost and s["close"][i] > s["vwap"][i] and s["close"][i] > s["open"][i]:
            entry = s["close"][i]
            stop = min(s["low"][max(0, i - 3):i + 1])
            if entry <= stop:
                return None
            return Setup("VWAP reclaim", i, entry, stop, entry + 2 * (entry - stop),
                         "long", f"reclaimed VWAP {s['vwap'][i]:.2f} after losing it")
    return None


def gap_and_go(s):
    """
    Gap and go — an opening gap that does not fill.

    The gap has to be big enough to signal something happened overnight, and
    the stock has to hold above the opening range low; a gap that gives back
    its own range is a fade, not a continuation.
    """
    if s["gap_pct"] is None or s["gap_pct"] < 1.0:
        return None
    for i in range(OPENING_RANGE_BARS, min(s["n"], LAST_ENTRY_BAR)):
        if s["close"][i] > s["or_high"] and min(s["low"][:i + 1]) > s["or_low"] * 0.998:
            entry = s["close"][i]
            stop = s["or_low"]
            if entry <= stop:
                return None
            return Setup("Gap and go", i, entry, stop, entry + 2 * (entry - stop),
                         "long", f"gapped {s['gap_pct']:.2f}% and held the opening range")
    return None


def vwap_reversion(s):
    """
    VWAP mean reversion — fade an extension back toward VWAP.

    Included deliberately as the counter-example. Its target is close and its
    stop is far, which is exactly the shape that manufactures a high win rate,
    so the backtest should show it winning often. Whether it *makes money* is
    the question the expectancy column answers.
    """
    for i in range(OPENING_RANGE_BARS, min(s["n"], LAST_ENTRY_BAR)):
        vw = s["vwap"][i]
        if vw <= 0:
            continue
        stretch = (s["close"][i] - vw) / vw * 100.0
        if stretch < -2.0:
            entry = s["close"][i]
            stop = entry - 1.5 * (vw - entry)      # deliberately wide
            target = vw                            # deliberately close
            if entry <= stop or target <= entry:
                return None
            return Setup("VWAP reversion", i, entry, stop, target,
                         "long", f"{abs(stretch):.2f}% below VWAP {vw:.2f}, "
                                 f"fading back toward it")
    return None


def momentum_rvol(s):
    """
    Stocks in play — high relative volume, trending above VWAP.

    No breakout level required: the condition is participation. This is the
    abnormal-participation hypothesis used on its own, to test whether an
    opening-range condition adds value to it.
    """
    for i in range(OPENING_RANGE_BARS, min(s["n"], LAST_ENTRY_BAR)):
        rvol = _rvol_at(s, i)
        if rvol is None or rvol < 2.0:
            continue
        window = s["close"][max(0, i - 2):i + 1]
        if len(window) < 3:
            continue
        rising = window[0] < window[1] < window[2]
        if rising and s["close"][i] > s["vwap"][i]:
            entry = s["close"][i]
            stop = min(s["low"][max(0, i - 3):i + 1])
            if entry <= stop:
                return None
            return Setup("RVOL momentum", i, entry, stop, entry + 2 * (entry - stop),
                         "long", f"RVOL {rvol:.1f}x with price trending above VWAP")
    return None


def orb_breakdown(s):
    """
    The short mirror of the opening range breakout.

    The short rule must be evaluated independently; evidence for the long
    rule cannot establish an edge or execution eligibility for a short.
    """
    if s["or_width"] <= 0:
        return None
    for i in range(OPENING_RANGE_BARS, min(s["n"], LAST_ENTRY_BAR)):
        rvol = _rvol_at(s, i)
        if rvol is None or rvol < 1.2:
            continue
        if s["close"][i] < s["or_low"] and s["close"][i] < s["vwap"][i]:
            entry = s["close"][i]
            stop = s["or_high"]
            if stop <= entry:
                return None
            return Setup("ORB breakdown", i, entry, stop, entry - 2 * (stop - entry),
                         "short", f"closed below the 15-minute range low "
                                  f"{s['or_low']:.2f} and below VWAP")
    return None


def vwap_rejection(s):
    """
    Price returns to VWAP from below and is turned away.

    The failure to reclaim is the signal. A stock that cannot get back above
    the level the day's volume was transacted at has sellers waiting there.
    """
    below = False
    for i in range(OPENING_RANGE_BARS, min(s["n"], LAST_ENTRY_BAR)):
        vw = s["vwap"][i]
        if s["close"][i] < vw:
            if below and s["high"][i] >= vw and s["close"][i] < s["open"][i]:
                entry = s["close"][i]
                stop = max(s["high"][max(0, i - 3):i + 1])
                if stop <= entry:
                    return None
                return Setup("VWAP rejection", i, entry, stop,
                             entry - 2 * (stop - entry), "short",
                             f"tagged VWAP {vw:.2f} from below and was rejected")
            below = True
        else:
            below = False
    return None


STRATEGIES = {
    "ORB breakout": orb_breakout,
    "ORB breakdown": orb_breakdown,
    "VWAP reclaim": vwap_reclaim,
    "VWAP rejection": vwap_rejection,
    "Gap and go": gap_and_go,
    "RVOL momentum": momentum_rvol,
    "VWAP reversion": vwap_reversion,
}
STRATEGY_DIRECTIONS = {name: "short" if name in {"ORB breakdown", "VWAP rejection"} else "long"
                       for name in STRATEGIES}


def signals(s):
    """Causal confirmed patterns, independently of any subsequent trade outcome."""
    if not s:
        return {}
    results = {}
    for name, fn in STRATEGIES.items():
        setup = fn(s)
        if setup is None:
            continue
        i = setup["bar"]
        stamps = s.get("timestamps", [])
        stamp = stamps[i] if i < len(stamps) else None
        setup.update(
            signal_at=stamp.isoformat() if stamp else None,
            confirmed_at=(stamp + timedelta(minutes=BAR_MINUTES)).isoformat() if stamp else None,
            rvol=_rvol_at(s, i), rvol_method=s.get("rvol_method", "unavailable"),
            session_validated=bool(s.get("validated")),
        )
        results[name] = setup
    return results


def strategy_version():
    """Fingerprint signal, validation and execution code plus live cost settings."""
    configuration = {"cost_model": COST_MODEL_VERSION,
                     "round_trip_cost_bps": ROUND_TRIP_COST_BPS,
                     "slippage_bps_per_side": SLIPPAGE_BPS,
                     "session_bars": SESSION_BARS, "bar_minutes": BAR_MINUTES,
                     "opening_range_bars": OPENING_RANGE_BARS,
                     "last_entry_bar": LAST_ENTRY_BAR}
    source = Path(__file__).read_text(encoding="utf-8")
    return hashlib.sha256((source + json.dumps(configuration, sort_keys=True)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------

def simulate(setup, s, cost_bps=ROUND_TRIP_COST_BPS, slippage_bps=SLIPPAGE_BPS):
    """
    Enter at the next bar open after confirmation, then walk observable bars.

    Entry and exit slippage is always adverse. Stops through gaps fill at the
    worse opening price; target gaps receive only the target price. Ambiguous
    stop/target touches resolve to the stop. ``cost_bps`` is an assumed total
    round-trip rate on the average entry/exit notional, not exact broker fees.
    ``gross_r`` excludes friction; ``costs_r`` includes fees and slippage.
    R uses the raw next-open entry-to-stop risk. A partial session is marked
    open with unrealized fields and cannot count as a completed winning trade.
    """
    if not setup or not s or setup.get("direction") not in {"long", "short"}:
        return None
    bar = setup.get("bar")
    if not isinstance(bar, int) or bar < 0 or bar + 1 >= s["n"]:
        return None
    stop, target = _f(setup.get("stop")), _f(setup.get("target"))
    proposed = _f(setup.get("entry"))
    cost_bps, slippage_bps = _f(cost_bps), _f(slippage_bps)
    if any(x is None for x in (stop, target, proposed, cost_bps, slippage_bps)):
        return None
    if min(stop, target, proposed) <= 0 or min(cost_bps, slippage_bps) < 0 or slippage_bps >= 10000:
        return None
    side = 1 if setup["direction"] == "long" else -1
    if (proposed - stop) * side <= 0 or (target - proposed) * side <= 0:
        return None
    entry_bar = bar + 1
    raw_entry = s["open"][entry_bar]
    slip = slippage_bps / 10000.0
    entry = raw_entry * (1.0 + side * slip)
    risk = (raw_entry - stop) * side
    if risk <= 0 or (entry - stop) * side <= 0 or (target - entry) * side <= 0:
        return None
    stamps = s.get("timestamps", [])

    def at(i, end=False):
        stamp = stamps[i] if i < len(stamps) else None
        return (stamp + timedelta(minutes=BAR_MINUTES if end else 0)).isoformat() if stamp else None

    def result(outcome, i, raw_exit, at_open=False):
        exit_fill = raw_exit * (1.0 - side * slip)
        gross = (raw_exit - raw_entry) * side / risk
        slippage = ((entry - raw_entry) + (raw_exit - exit_fill)) * side / risk
        fee = (entry + exit_fill) / 2.0 * cost_bps / 10000.0 / risk
        costs, net = slippage + fee, gross - slippage - fee
        data = {"outcome": outcome, "direction": setup["direction"],
                "entry_price": round(entry, 6), "raw_entry_price": raw_entry,
                "entry_at": at(entry_bar), "risk_per_share": round(risk, 6),
                "bars_held": i - entry_bar + 1, "cost_model": COST_MODEL_VERSION,
                "cost_bps": cost_bps, "slippage_bps": slippage_bps,
                "exit_time_precision": ("bar_open" if at_open else "session_close"
                                        if outcome == "close" else "five_minute_bar")}
        if outcome == "open":
            data.update(gross_r=None, costs_r=None, net_r=None, r=None,
                        exit_price=None, exit_at=None,
                        mark_price=raw_exit, mark_at=at(i, end=True),
                        unrealized_gross_r=round(gross, 6),
                        estimated_close_costs_r=round(costs, 6),
                        unrealized_net_r=round(net, 6))
        else:
            data.update(gross_r=round(gross, 6), costs_r=round(costs, 6),
                        fee_r=round(fee, 6), slippage_r=round(slippage, 6),
                        net_r=round(net, 6), r=round(net, 6),
                        exit_price=round(exit_fill, 6), raw_exit_price=raw_exit,
                        exit_at=at(i, end=not at_open))
        return data

    for i in range(entry_bar, s["n"]):
        op = s["open"][i]
        if (op - stop) * side <= 0:
            return result("stop", i, op, at_open=True)
        if (op - target) * side >= 0:
            return result("target", i, target, at_open=True)
        hit_stop = s["low"][i] <= stop if side > 0 else s["high"][i] >= stop
        hit_target = s["high"][i] >= target if side > 0 else s["low"][i] <= target
        if hit_stop:                                  # checked first, deliberately
            return result("stop", i, stop)
        if hit_target:
            return result("target", i, target)
    return result("close" if s.get("complete") else "open", s["n"] - 1, s["close"][-1])


def evaluate(s):
    """Every strategy's setup and outcome for one stock-session."""
    results = {}
    for name, setup in signals(s).items():
        outcome = simulate(setup, s)
        if outcome:
            results[name] = {"setup": setup, "outcome": outcome}
    return results


# ---------------------------------------------------------------------------
# scoring a strategy over many sessions
# ---------------------------------------------------------------------------

def summarise(trades):
    """Only completed, finite net returns can enter a rule's measured record."""
    closed = [dict(t, r=_f(t.get("r"))) for t in trades
              if isinstance(t, dict) and t.get("outcome") != "open" and _f(t.get("r")) is not None]
    return trade_stats.summarise(closed, MIN_TRADES_TO_REPORT, noun="setups")
