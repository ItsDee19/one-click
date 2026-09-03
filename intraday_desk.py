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
import market
import strategies

HERE = os.path.dirname(os.path.abspath(__file__))
RECORD_FILE = os.path.join(HERE, "backtest_intraday.json")

MAX_PICKS = 12


def load_record():
    """The measured performance of each strategy, if the backtest has been run."""
    try:
        with open(RECORD_FILE, "r", encoding="utf-8") as fh:
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
    phase = market.phase_from_clock()
    if not trading.get("trading"):
        return {"generated": data_sources.now_ist_str(), "picks": [],
                "tradeable": False,
                "note": f"not a trading day — {trading.get('reason', '')}".strip(" —"),
                "strategies": record, "record_window": (blob or {}).get("window")}

    universe = universe or data_sources.load_universe()
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
    for entry in entries:
        ticker = entry["ticker"]
        frame = frames.get(ticker)
        if frame is None or getattr(frame, "empty", True):
            continue

        quote = by_ticker.get(ticker) or {}
        bars = _bars_from(frame)
        s = strategies.session(bars, prev_close=_prev_close(quote),
                               rvol=quote.get("rvol"))
        if not s:
            continue

        for name, result in strategies.evaluate(s).items():
            setup = result["setup"]
            stat = record.get(name) or {}
            picks.append({
                "symbol": ticker.split(".")[0],
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

    # expectancy first: a rule that wins more often but earns less per unit of
    # risk should not outrank one that earns more.
    picks.sort(key=lambda p: (p["record"].get("expectancy_r") or -99,
                              p["confidence"]), reverse=True)
    picks = picks[:MAX_PICKS]

    say(f"intraday desk: {len(picks)} setup(s) from {len(strategies.STRATEGIES)} strategies")
    return {
        "generated": data_sources.now_ist_str(),
        "tradeable": phase in ("regular", "open"),
        "phase": phase,
        "picks": picks,
        "strategies": record,
        "record_window": (blob or {}).get("window"),
        "record_sessions": (blob or {}).get("stock_sessions"),
        "note": None if record else
                "no measured record yet — run `python backtest_intraday.py` so "
                "each strategy can show what it actually did on this universe",
    }


def _prev_close(quote):
    """
    Yesterday's close, from the daily frame the screener already downloaded.

    During a live session the last daily row is today's forming bar, so the
    previous close is the one before it — the same convention the screener
    itself uses to compute the day change.
    """
    frame = quote.get("frame")
    if frame is None or getattr(frame, "empty", True):
        return None
    closes = data_sources._series_values(frame, "Close")
    return closes[-2] if len(closes) >= 2 else None


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
