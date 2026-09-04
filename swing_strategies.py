"""
swing_strategies.py — positional rules, measured on years rather than weeks.

WHY A SECOND FILE
-----------------
strategies.py is bounded by what Yahoo serves for intraday: about 59 sessions
of 5-minute bars, one market regime. Daily bars go back years, so the
positional horizon — the one this project has always judged alongside intraday
but never measured — can be tested across bull phases, corrections and the
2024-25 drawdown rather than a single quarter.

WHERE THESE COME FROM
---------------------
Each rule is a published anomaly with evidence behind it, not a shape found by
searching this data:

  52-week high      George & Hwang. Indian evidence (SSRN 4587697) finds it a
                    distinct and robust NSE anomaly with more stable alpha
                    than academic momentum, and without its long-run reversal.
  12-1 momentum     Jegadeesh & Titman. Indian evidence confirms momentum for
                    holding periods out to 12 months.
  Short-term        The other side of the same literature: reversals exist in
  reversal          India at short holding periods and disappear by 6-12
                    months, so this is deliberately held for days, not months.
  RSI(2)            Connors. Buy pullbacks in an uptrend rather than breakouts.
  Donchian 20       The Turtle breakout, the oldest published trend rule there
                    is, kept as the trend-following counterweight.

Choosing rules first and testing second is the whole discipline. Picking the
best-performing variant after seeing the results would fit this sample and
nothing else — the same trap the daily backtest already talked us out of once.

EXITS
-----
Every rule exits on a fixed horizon or an ATR stop, whichever comes first, so
the holding period is part of the rule rather than something decided later
with the benefit of hindsight.
"""

from __future__ import annotations

ATR_PERIOD = 14
DEFAULT_HORIZON = 10          # trading days
MIN_TRADES_TO_REPORT = 20
WARMUP = 260                  # a 52-week high needs a year of bars first


def _sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _atr(highs, lows, closes, n=ATR_PERIOD):
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(len(closes) - n, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


def _rsi(closes, n=2):
    """Wilder's RSI. n=2 is Connors' setting — deliberately hair-trigger."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(len(closes) - n, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def context(highs, lows, closes, volumes, i):
    """
    Everything a rule may look at, computed strictly from bars up to `i`.

    The slicing is the point: a rule that can see closes[i+1] would produce
    a backtest that cannot be traded.
    """
    h, l, c = highs[:i + 1], lows[:i + 1], closes[:i + 1]
    v = volumes[:i + 1]
    if len(c) < WARMUP:
        return None

    year_high = max(h[-252:])
    year_low = min(l[-252:])
    atr = _atr(h, l, c)
    if not atr or not year_high:
        return None

    return {
        "close": c[-1], "high": h[-1], "low": l[-1],
        "atr": atr,
        "sma200": _sma(c, 200), "sma50": _sma(c, 50), "sma5": _sma(c, 5),
        "rsi2": _rsi(c, 2),
        "year_high": year_high, "year_low": year_low,
        "pct_of_52w_high": c[-1] / year_high * 100.0,
        "donchian20": max(h[-21:-1]) if len(h) > 21 else None,
        "donchian20_low": min(l[-21:-1]) if len(l) > 21 else None,
        "ret_12_1": ((c[-21] / c[-252] - 1.0) * 100.0
                     if len(c) >= 252 and c[-252] else None),
        "ret_5d": (c[-1] / c[-6] - 1.0) * 100.0 if len(c) >= 6 and c[-6] else None,
        "ret_21d": (c[-1] / c[-22] - 1.0) * 100.0 if len(c) >= 22 and c[-22] else None,
        "avg_volume": sum(v[-20:]) / 20 if len(v) >= 20 else None,
    }


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------

def near_52w_high(ctx):
    """
    Within 5% of the 52-week high, in an uptrend.

    The Indian evidence is specifically that proximity to the 52-week high
    predicts, and that unlike academic momentum it does not hand the gains
    back later.
    """
    if ctx["pct_of_52w_high"] < 95.0:
        return None
    if not ctx["sma200"] or ctx["close"] < ctx["sma200"]:
        return None
    stop = ctx["close"] - 2.0 * ctx["atr"]
    return {"direction": "long", "stop": stop,
            "target": ctx["close"] + 4.0 * ctx["atr"],
            "why": f"{ctx['pct_of_52w_high']:.1f}% of its 52-week high, above the 200dma"}


def momentum_12_1(ctx):
    """
    Twelve-month return excluding the most recent month.

    The skipped month is not decoration — it is there to avoid the short-term
    reversal that the same literature documents, and which the rule below
    trades deliberately.
    """
    if ctx["ret_12_1"] is None or ctx["ret_12_1"] < 25.0:
        return None
    if not ctx["sma200"] or ctx["close"] < ctx["sma200"]:
        return None
    stop = ctx["close"] - 2.5 * ctx["atr"]
    return {"direction": "long", "stop": stop,
            "target": ctx["close"] + 5.0 * ctx["atr"],
            "why": f"up {ctx['ret_12_1']:.0f}% over 12 months excluding the last, "
                   f"still above the 200dma"}


def rsi2_pullback(ctx):
    """
    Connors: buy the pullback inside an uptrend, not the breakout.

    RSI(2) below 10 is a genuinely stretched short-term reading; requiring
    price above the 200dma keeps it a pullback rather than a falling knife.
    """
    if ctx["rsi2"] is None or ctx["rsi2"] > 10.0:
        return None
    if not ctx["sma200"] or ctx["close"] < ctx["sma200"]:
        return None
    stop = ctx["close"] - 2.0 * ctx["atr"]
    return {"direction": "long", "stop": stop,
            "target": ctx["close"] + 3.0 * ctx["atr"],
            "why": f"RSI(2) at {ctx['rsi2']:.0f} — short-term oversold inside an uptrend"}


def donchian_breakout(ctx):
    """The Turtle rule: a new 20-day high, with the trend filter intact."""
    if not ctx["donchian20"] or ctx["close"] <= ctx["donchian20"]:
        return None
    if not ctx["sma50"] or ctx["close"] < ctx["sma50"]:
        return None
    stop = ctx["close"] - 2.0 * ctx["atr"]
    return {"direction": "long", "stop": stop,
            "target": ctx["close"] + 4.0 * ctx["atr"],
            "why": f"broke the 20-day high {ctx['donchian20']:.2f}"}


def short_term_reversal(ctx):
    """
    A sharp one-month loser that is still in a long-term uptrend.

    Reversal and momentum look contradictory and are not: the Indian evidence
    finds reversals at short horizons and momentum at long ones, so this asks
    for both — beaten up over a month, healthy over a year.
    """
    if ctx["ret_21d"] is None or ctx["ret_21d"] > -10.0:
        return None
    if not ctx["sma200"] or ctx["close"] < ctx["sma200"]:
        return None
    stop = ctx["close"] - 2.0 * ctx["atr"]
    return {"direction": "long", "stop": stop,
            "target": ctx["close"] + 3.0 * ctx["atr"],
            "why": f"down {abs(ctx['ret_21d']):.0f}% in a month but still above "
                   f"its 200dma"}


SWING_STRATEGIES = {
    "52-week high": near_52w_high,
    "12-1 momentum": momentum_12_1,
    "RSI(2) pullback": rsi2_pullback,
    "Donchian 20": donchian_breakout,
    "Short-term reversal": short_term_reversal,
}


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------

def simulate(signal, highs, lows, closes, i, horizon=DEFAULT_HORIZON):
    """
    Hold until the stop, the target, or the horizon — whichever comes first.

    Daily bars cannot say whether the high or the low came first within a day,
    so a day touching both is scored a loss. Same convention as the intraday
    file, same reason.
    """
    entry = closes[i]
    stop, target = signal["stop"], signal["target"]
    risk = entry - stop
    if risk <= 0:
        return None

    end = min(i + horizon, len(closes) - 1)
    for j in range(i + 1, end + 1):
        if lows[j] <= stop:
            return {"outcome": "stop", "r": -1.0, "days_held": j - i}
        if highs[j] >= target:
            return {"outcome": "target", "r": round((target - entry) / risk, 3),
                    "days_held": j - i}
    if end <= i:
        return None
    return {"outcome": "horizon", "r": round((closes[end] - entry) / risk, 3),
            "days_held": end - i}


def evaluate_at(highs, lows, closes, volumes, i, horizon=DEFAULT_HORIZON):
    """Every swing rule's signal and outcome at one bar."""
    ctx = context(highs, lows, closes, volumes, i)
    if not ctx:
        return {}
    out = {}
    for name, fn in SWING_STRATEGIES.items():
        try:
            signal = fn(ctx)
        except Exception:                                          # noqa: BLE001
            signal = None
        if not signal:
            continue
        result = simulate(signal, highs, lows, closes, i, horizon)
        if result:
            out[name] = {"signal": dict(signal, entry=round(ctx["close"], 2)),
                         "outcome": result, "ctx": ctx}
    return out


def signals_now(highs, lows, closes, volumes):
    """What each rule says about the latest bar, with no outcome attached."""
    ctx = context(highs, lows, closes, volumes, len(closes) - 1)
    if not ctx:
        return {}
    out = {}
    for name, fn in SWING_STRATEGIES.items():
        try:
            signal = fn(ctx)
        except Exception:                                          # noqa: BLE001
            signal = None
        if signal:
            out[name] = {
                "entry": round(ctx["close"], 2),
                "stop": round(signal["stop"], 2),
                "target": round(signal["target"], 2),
                "direction": signal["direction"],
                "why": signal["why"],
                "reward_risk": round((signal["target"] - ctx["close"]) /
                                     (ctx["close"] - signal["stop"]), 2)
                                if ctx["close"] > signal["stop"] else None,
            }
    return out
