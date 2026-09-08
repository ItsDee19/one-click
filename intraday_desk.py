"""
intraday_desk.py — today's setups, from the strategies in strategies.py.

Each pick is produced by one named rule and carries that rule's *measured*
record from backtest_intraday.json — the hit rate and expectancy it actually
produced on this universe over the sampled sessions, not a number from a
trading blog. A rule with too thin a sample says so on the card instead of
showing a percentage.

Picks are ranked by the strategy's measured expectancy rather than by its win
rate, because expectancy is the one that corresponds to making money.

This is analysis. Nothing here places an order, and the levels shown are the
ones the rule defines, not advice to take them.
"""

from __future__ import annotations

import json
import os

import data_sources
import evidence_quality
import market
import strategies

HERE = os.path.dirname(os.path.abspath(__file__))
RECORD_FILE = os.path.join(HERE, "backtest_intraday.json")
SWING_RECORD_FILE = os.path.join(HERE, "backtest_swing.json")

MAX_PICKS = 12


def load_record():
    """The measured performance of each strategy, if the backtest has been run."""
    try:
        with open(RECORD_FILE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return {}, None
    return blob.get("strategies") or {}, blob


def load_swing_record():
    """The positional rules' record, shown alongside so both horizons are visible."""
    try:
        with open(SWING_RECORD_FILE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return {}, None
    return blob.get("strategies") or {}, blob


def _confidence(record):
    """
    A 1-10 read on one setup, driven by the strategy's own measured record.

    Expectancy carries it, because that is what corresponds to profit. A rule
    with no trustworthy sample is capped low no matter how good the sample it
    does have looks — a 100% win rate over three trades is not evidence.
    """
    if not record or not record.get("trades"):
        return 3
    exp = record.get("expectancy_r") or 0.0
    score = 5.0 + exp * 4.0
    if not record.get("enough"):
        score = min(score, 5.0)
    return int(max(1, min(10, round(score))))


def scan(log=None, universe=None):
    """Run every strategy over today's live 5-minute bars."""
    say = log or (lambda _m: None)
    record, blob = load_record()

    trading = market.is_trading_day(log=say)
    if not trading.get("trading"):
        return {"generated": data_sources.now_ist_str(), "picks": [],
                "tradeable": False,
                "note": f"not a trading day — {trading.get('reason', '')}".strip(" —"),
                "strategies": record, "record_window": (blob or {}).get("window"),
                "swing": _swing_payload()}

    universe = data_sources.load_full_exchange(log=say) if universe is None else universe
    entries = [dict(e, bucket=b) for b, rows in universe.items() for e in rows]

    # fetch_quotes returns (quotes, benchmark) — it also hands back RVOL already
    # adjusted for how much of the session has elapsed, which is the number the
    # "stocks in play" filter needs. Recomputing it here would be both
    # duplicated and wrong before the close.
    quotes, _benchmark = data_sources.fetch_quotes(universe, log=say)
    by_ticker = {}
    for rows in quotes.values():
        for q in rows or []:
            if q.get("ticker"):
                by_ticker[q["ticker"]] = q

    tickers = [e["ticker"] for e in entries]
    say(f"intraday desk: pulling session bars for {len(tickers)} symbols")
    frames = data_sources.fetch_intraday(tickers, log=say)

    picks = []
    available, stale = 0, 0
    session_observations = {}
    for entry in entries:
        ticker = entry["ticker"]
        frame = frames.get(ticker)
        if frame is None or getattr(frame, "empty", True):
            continue

        observed = evidence_quality.parse_timestamp(frame.index[-1])
        now = market.now_ist()
        if not _session_is_fresh(observed, now):
            stale += 1
            continue
        available += 1
        session_observations[ticker] = observed

        quote = by_ticker.get(ticker) or {}
        bars = _bars_from(frame)
        s = strategies.session(bars, prev_close=_prev_close(quote, observed.date()),
                               rvol=quote.get("rvol"))
        if not s:
            continue

        for name, result in strategies.evaluate(s).items():
            setup = result["setup"]
            stat = record.get(name) or {}
            picks.append({
                "ticker": ticker,
                "symbol": ticker.split(".")[0],
                "as_of": observed.isoformat(),
                "name": entry.get("name"),
                "sector": entry.get("sector"),
                "bucket": entry.get("bucket"),
                "strategy": name,
                "why": setup["why"],
                "entry": setup["entry"],
                "stop": setup["stop"],
                "target": setup["target"],
                "reward_risk": setup["reward_risk"],
                "risk_per_share": setup["risk_per_share"],
                "last": s["close"][-1],
                "vwap": round(s["vwap"][-1], 2),
                "rvol": round(s["rvol"], 2) if s["rvol"] else None,
                "gap_pct": round(s["gap_pct"], 2) if s["gap_pct"] is not None else None,
                "confidence": _confidence(stat),
                "record": {
                    "trades": stat.get("trades"),
                    "win_rate_pct": stat.get("win_rate_pct"),
                    "expectancy_r": stat.get("expectancy_r"),
                    "profit_factor": stat.get("profit_factor"),
                    "trusted": bool(stat.get("enough")),
                    "note": stat.get("note"),
                },
            })

    # A whole-exchange pass can cross market close or outlive early snapshots.
    # Recheck at publication time before choosing the displayed setups.
    completed_at = market.now_ist()
    expired = {ticker for ticker, observed in session_observations.items()
               if not _session_is_fresh(observed, completed_at)}
    available -= len(expired)
    stale += len(expired)
    picks = [pick for pick in picks if pick["ticker"] not in expired]
    phase = market.phase_from_clock(completed_at)

    # expectancy first: a rule that wins more often but earns less per unit of
    # risk should not outrank one that earns more.
    picks.sort(key=lambda p: (p["record"].get("expectancy_r") or -99,
                              p["confidence"]), reverse=True)
    picks = picks[:MAX_PICKS]

    say(f"intraday desk: {len(picks)} setup(s) from {len(strategies.STRATEGIES)} strategies")
    return {
        "generated": data_sources.now_ist_str(),
        "coverage": {"listed": len(entries), "usable_sessions": available,
                     "stale_sessions": stale, "missing_sessions": len(entries) - available - stale,
                     "universe": data_sources.LAST_UNIVERSE_METADATA},
        "tradeable": phase in ("regular", "open"),
        "phase": phase,
        "picks": picks,
        "strategies": record,
        "record_window": (blob or {}).get("window"),
        "record_sessions": (blob or {}).get("stock_sessions"),
        "swing": _swing_payload(),
        "note": None if record else
                "no measured record yet — run `python backtest_intraday.py` so "
                "each strategy can show what it actually did on this universe",
    }


def _session_is_fresh(observed, moment):
    return (observed is not None and observed.date() == moment.date()
            and -5 <= (moment - observed).total_seconds() / 60 <= evidence_quality.MAX_INTRADAY_AGE_MINUTES)


def _prev_close(quote, session_date=None):
    """
    Latest available daily close strictly before the intraday session date.

    The daily response may include today's forming candle or may still end
    yesterday. Selecting by date handles both without shifting the gap base.
    """
    frame = quote.get("frame")
    if frame is None or getattr(frame, "empty", True):
        return None
    session_date = session_date or market.now_ist().date()
    if "Close" not in frame:
        return None
    previous, latest = None, None
    for stamp, raw in frame["Close"].items():
        observed = evidence_quality.parse_timestamp(stamp)
        close = evidence_quality.finite_number(raw)
        if observed is None or observed.date() >= session_date or close is None or close <= 0:
            continue
        if latest is None or observed > latest:
            latest, previous = observed, close
    return previous


def _bars_from(frame):
    """yfinance frame -> the plain dicts strategies.session expects."""
    out = []
    try:
        rows = frame.itertuples(index=False)
    except AttributeError:
        return out
    for row in rows:
        d = row._asdict() if hasattr(row, "_asdict") else {}
        out.append({
            "open": d.get("Open"), "high": d.get("High"), "low": d.get("Low"),
            "close": d.get("Close"), "volume": d.get("Volume"),
        })
    return out


def _swing_payload():
    """The swing scoreboard, so the page can show both horizons together."""
    record, blob = load_swing_record()
    return {"strategies": record,
            "window": (blob or {}).get("window"),
            "symbols": (blob or {}).get("symbols")}
