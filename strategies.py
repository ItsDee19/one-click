"""
strategies.py — named intraday strategies, each measured on our own data.

WHY THIS MODULE EXISTS
----------------------
The dashboard already computed VWAP, the opening range and RVOL, but fused
them into a single verdict. That makes it impossible to ask the only question
that matters: *which rule is actually working, and how well?*

Here each strategy is a separate, explicit rule with its own entry, stop and
target, so it can be simulated independently and carry its own measured hit
rate rather than a borrowed one.

ON "70% WIN RATE" STRATEGIES
----------------------------
There is no credible source for intraday strategies with a documented 70%+ win
rate. Published win rates for day traders cluster around 49-51%, and the
large-sample academic work reports *Sharpe*, not win rate — the ORB study
below (Zarattini, Barbon & Aziz, 2023, 7,000 US stocks over 2016-2023) reports
a 2.81 Sharpe from a strategy whose win rate is nowhere near 70%.

That is not a technicality. Win rate on its own is trivially gameable: take a
tiny target and a distant stop and you can manufacture 80% winners that still
lose money, because the rare loss erases many wins. So every strategy here
reports **expectancy in R** (average profit per unit risked) next to its win
rate, and the two are meant to be read together. A strategy that wins often
and loses money shows up immediately.

Anything claiming a reliable 70%+ intraday win rate is selling something.

WHAT IS MEASURED, AND ON WHAT
-----------------------------
Yahoo serves roughly 60 sessions of 5-minute bars for NSE symbols. That is the
honest limit of this backtest: ~59 sessions, one recent market regime, and no
survivorship correction. It is enough to reject a broken rule and not enough
to certify a good one. Sample sizes are reported with every number, and a
strategy with too few trades reports "not enough evidence" rather than a
flattering percentage.

Because the bars are 5-minute rather than tick, a bar that touches both the
stop and the target is scored as a **loss**. The real path inside that bar is
unknowable here, and the conservative reading is the honest one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

BAR_MINUTES = 5
OPENING_RANGE_BARS = 3          # first 15 minutes
MIN_BARS_FOR_SETUP = 4
LAST_ENTRY_BAR = 60             # ~14:15; no new entries into the close
MIN_TRADES_TO_REPORT = 20       # below this a hit rate is noise, not evidence

# A trade risks (entry - stop); targets are expressed as a multiple of that,
# so expectancy is directly comparable across strategies.


def _f(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


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

def session(bars, prev_close=None, avg_volume=None, rvol=None):
    """
    Normalise one day of 5-minute bars, with running VWAP and the opening range.

    VWAP is cumulative from the open on typical price, which is what intraday
    participants actually benchmark against — not a moving average of closes.
    """
    o = [_f(b.get("open")) for b in bars]
    h = [_f(b.get("high")) for b in bars]
    l = [_f(b.get("low")) for b in bars]
    c = [_f(b.get("close")) for b in bars]
    v = [_f(b.get("volume")) or 0.0 for b in bars]

    n = len(c)
    keep = [i for i in range(n) if None not in (o[i], h[i], l[i], c[i])]
    if len(keep) < MIN_BARS_FOR_SETUP:
        return None

    o = [o[i] for i in keep]; h = [h[i] for i in keep]
    l = [l[i] for i in keep]; c = [c[i] for i in keep]
    v = [v[i] for i in keep]
    n = len(c)

    vwap, turnover, traded = [], 0.0, 0.0
    for i in range(n):
        turnover += ((h[i] + l[i] + c[i]) / 3.0) * v[i]
        traded += v[i]
        vwap.append(turnover / traded if traded > 0 else c[i])

    take = min(OPENING_RANGE_BARS, n)
    or_high, or_low = max(h[:take]), min(l[:take])

    day_volume = sum(v)
    # A caller that already knows RVOL (the live desk does, session-adjusted)
    # passes it in; the backtest derives it from its own trailing average.
    if rvol is None and avg_volume:
        rvol = day_volume / avg_volume
    gap = ((o[0] - prev_close) / prev_close * 100.0) if prev_close else None

    return {
        "open": o, "high": h, "low": l, "close": c, "volume": v, "vwap": vwap,
        "n": n, "or_high": or_high, "or_low": or_low,
        "or_width": or_high - or_low,
        "prev_close": prev_close, "rvol": rvol, "gap_pct": gap,
        "day_volume": day_volume,
    }


# ---------------------------------------------------------------------------
# the strategies
# ---------------------------------------------------------------------------
# Each takes a normalised session and returns a Setup or None. They look only
# at bars up to the decision point — never at what happens afterwards, which
# is the whole discipline of this file.

def orb_breakout(s):
    """
    Opening Range Breakout — Zarattini, Barbon & Aziz (2023).

    The first 15 minutes set the range; the first close above it is the entry.
    The paper's central finding is that this works on *stocks in play* — names
    with abnormal volume — and is unremarkable applied indiscriminately, so
    RVOL is a condition here rather than a bonus.
    """
    if s["or_width"] <= 0 or (s["rvol"] is not None and s["rvol"] < 1.2):
        return None
    for i in range(OPENING_RANGE_BARS, min(s["n"], LAST_ENTRY_BAR)):
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
    ORB paper's filter used on its own, to see how much of the edge is the
    breakout and how much is simply being in the right name.
    """
    if s["rvol"] is None or s["rvol"] < 2.0:
        return None
    for i in range(OPENING_RANGE_BARS, min(s["n"], LAST_ENTRY_BAR)):
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
                         "long", f"RVOL {s['rvol']:.1f}x with price trending above VWAP")
    return None


def orb_breakdown(s):
    """
    The short mirror of the opening range breakout.

    Every other rule here is long-only, which quietly assumes the market only
    offers one direction. It does not, and a desk that can only see upside
    setups will keep finding them in a falling market.
    """
    if s["or_width"] <= 0 or (s["rvol"] is not None and s["rvol"] < 1.2):
        return None
    for i in range(OPENING_RANGE_BARS, min(s["n"], LAST_ENTRY_BAR)):
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


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------

def simulate(setup, s):
    """
    Walk the rest of the session and see which level was reached first.

    A bar that spans both stop and target is scored a loss: at 5-minute
    granularity the path inside the bar is unknowable, and assuming the
    favourable ordering is how backtests come to flatter their authors.

    Shorts are the mirror image — stop above, target below — so the sign of
    the move is folded into `side` rather than duplicated in every rule.
    """
    entry, stop, target = setup["entry"], setup["stop"], setup["target"]
    side = 1 if setup["direction"] == "long" else -1
    risk = (entry - stop) * side
    if risk <= 0:
        return None

    for i in range(setup["bar"] + 1, s["n"]):
        hit_stop = s["low"][i] <= stop if side > 0 else s["high"][i] >= stop
        hit_target = s["high"][i] >= target if side > 0 else s["low"][i] <= target
        if hit_stop:                                  # checked first, deliberately
            return {"outcome": "stop", "r": -1.0, "bars_held": i - setup["bar"]}
        if hit_target:
            return {"outcome": "target",
                    "r": round((target - entry) * side / risk, 3),
                    "bars_held": i - setup["bar"]}

    close = s["close"][-1]
    return {"outcome": "close", "r": round((close - entry) * side / risk, 3),
            "bars_held": s["n"] - 1 - setup["bar"]}


def evaluate(s):
    """Every strategy's setup and outcome for one stock-session."""
    results = {}
    for name, fn in STRATEGIES.items():
        try:
            setup = fn(s)
        except Exception:                                          # noqa: BLE001
            setup = None
        if not setup:
            continue
        outcome = simulate(setup, s)
        if outcome:
            results[name] = {"setup": setup, "outcome": outcome}
    return results


# ---------------------------------------------------------------------------
# scoring a strategy over many sessions
# ---------------------------------------------------------------------------

def summarise(trades):
    """
    Win rate and expectancy together — neither means much alone.

    Expectancy is the average R per trade. Positive means the rule made money
    per unit risked over this sample; a high win rate beside a negative
    expectancy means the losses are bigger than the wins, which is the usual
    shape of a strategy that markets well.
    """
    n = len(trades)
    if not n:
        return {"trades": 0, "enough": False,
                "note": "no setups triggered in the sample"}

    wins = [t for t in trades if t["r"] > 0]
    rs = [t["r"] for t in trades]
    expectancy = sum(rs) / n
    gross_win = sum(r for r in rs if r > 0)
    gross_loss = abs(sum(r for r in rs if r < 0))

    enough = n >= MIN_TRADES_TO_REPORT
    return {
        "trades": n,
        "enough": enough,
        "win_rate_pct": round(len(wins) / n * 100.0, 1),
        "expectancy_r": round(expectancy, 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_win_r": round(sum(r for r in rs if r > 0) / len(wins), 2) if wins else None,
        "avg_loss_r": round(sum(r for r in rs if r < 0) / (n - len(wins)), 2)
                      if n - len(wins) else None,
        "best_r": round(max(rs), 2),
        "worst_r": round(min(rs), 2),
        "note": None if enough else
                f"only {n} setups — below the {MIN_TRADES_TO_REPORT} needed "
                f"before a hit rate means anything",
    }
