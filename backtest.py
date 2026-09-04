"""
backtest.py — does the panel's verdict predict anything?

Runs the real scoring engine over historical data, one trading day at a time,
and measures what happened next.

WHAT THIS TESTS, AND WHAT IT CANNOT
-----------------------------------
Evidence is rebuilt **point-in-time**: on day t the bundle contains only what
was knowable on day t. Nothing is computed from a future bar. That rules out
the single most common way a backtest flatters a strategy.

The cost is that two inputs cannot be reconstructed at all:

  * analyst targets, consensus and the buy/hold/sell split — yfinance serves
    only today's values, and using today's target to judge a trade from two
    years ago is severe look-ahead bias
  * news sentiment — no historical headline archive is available here

Both are therefore null and named in `data_gaps`, exactly as the live engine
handles a stock with no analyst coverage. So this validates the **technical
core** of the panel — RVOL, trend, SMA distance, 52-week position, relative
strength, exhaustion — and says nothing about the analyst or news seats.

It also tests the deterministic engine, not the LLM. Thousands of debate calls
is neither affordable nor reproducible, and the deterministic judge is the one
that must always work anyway.

THE EXPERIMENT
--------------
Two things are measured:

  1. Forward returns by verdict. If BUY days do not beat WATCH and AVOID days
     over the following week or month, the verdict carries no information and
     nothing else matters.
  2. A benchmark. The same forward window on the NIFTY, plus the average
     across every screened stock. A strategy returning 8% while the index
     returned 15% is not smart, it is expensive.

Usage:
    python backtest.py                      # 3 years, default universe
    python backtest.py --years 5 --hold 10
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import datetime

import data_sources
import scoring

WARMUP = 252            # trading days needed before the first verdict
FORWARD_WINDOWS = (1, 5, 10, 20)
SMA_PERIOD = data_sources.SMA_PERIOD
ATR_PERIOD = data_sources.ATR_PERIOD


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def load_history(tickers, years, log=print):
    """Daily OHLCV per ticker as plain lists, plus the aligned date axis."""
    import yfinance as yf

    log(f"downloading {years}y of daily bars for {len(tickers)} tickers "
        f"+ {data_sources.BENCHMARK_NAME}")
    frame = yf.download(" ".join(tickers + [data_sources.BENCHMARK]),
                        period=f"{years}y", interval="1d", group_by="ticker",
                        auto_adjust=False, actions=False, progress=False,
                        threads=True)

    series = {}
    for ticker in tickers + [data_sources.BENCHMARK]:
        try:
            bars = frame[ticker]
        except (KeyError, TypeError):
            continue
        if bars is None or getattr(bars, "empty", True):
            continue

        rows = {"date": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
        for stamp, row in bars.iterrows():
            close = row.get("Close")
            if close != close or close is None:          # NaN
                continue
            rows["date"].append(stamp.date().isoformat())
            rows["open"].append(float(row.get("Open") or close))
            rows["high"].append(float(row.get("High") or close))
            rows["low"].append(float(row.get("Low") or close))
            rows["close"].append(float(close))
            volume = row.get("Volume")
            rows["volume"].append(float(volume) if volume == volume and volume else 0.0)

        if len(rows["close"]) > WARMUP + max(FORWARD_WINDOWS) + 5:
            series[ticker] = rows

    log(f"usable series: {len(series)} of {len(tickers) + 1}")
    return series


# ---------------------------------------------------------------------------
# point-in-time evidence
# ---------------------------------------------------------------------------

def build_evidence_at(ticker, meta, rows, i, bench_rows, bench_i, sector_move):
    """
    The evidence bundle as it would have looked at the close of day `i`.

    Every slice is [... : i + 1] — inclusive of today, exclusive of tomorrow.
    """
    close = rows["close"][i]
    prev = rows["close"][i - 1]
    high, low, day_open = rows["high"][i], rows["low"][i], rows["open"][i]
    volume = rows["volume"][i]

    day_change = (close - prev) / prev * 100.0 if prev else None

    # trailing year, ending today
    year_hi = max(rows["high"][max(0, i - 251):i + 1])
    year_lo = min(rows["low"][max(0, i - 251):i + 1])
    position = ((close - year_lo) / (year_hi - year_lo) * 100.0
                if year_hi > year_lo else None)
    from_high = (close - year_hi) / year_hi * 100.0 if year_hi else None

    # RVOL against the prior 20 sessions; a full historical day, so no
    # time-of-day scaling is needed
    prior_vol = [v for v in rows["volume"][max(0, i - 20):i] if v > 0]
    rvol = (volume / (sum(prior_vol) / len(prior_vol))
            if prior_vol and volume > 0 else None)

    window = rows["close"][max(0, i - SMA_PERIOD + 1):i + 1]
    sma = sum(window) / len(window) if len(window) >= 5 else None
    vs_sma = (close - sma) / sma * 100.0 if sma else None

    older = rows["close"][max(0, i - SMA_PERIOD - 2):i - 2]
    prev_sma = sum(older) / len(older) if older else None
    if sma and prev_sma:
        trend = ("up" if close > sma and sma > prev_sma else
                 "down" if close < sma and sma < prev_sma else "sideways")
    else:
        trend = None

    month = rows["close"][max(0, i - 21):i + 1]
    window_return = (month[-1] - month[0]) / month[0] * 100.0 if month[0] else None

    trs = []
    for k in range(max(1, i - ATR_PERIOD + 1), i + 1):
        trs.append(max(rows["high"][k] - rows["low"][k],
                       abs(rows["high"][k] - rows["close"][k - 1]),
                       abs(rows["low"][k] - rows["close"][k - 1])))
    atr_pct = (sum(trs) / len(trs) / close * 100.0) if trs and close else None
    move_vs_atr = (abs(day_change) / atr_pct
                   if day_change is not None and atr_pct else None)

    day_pos = ((close - low) / (high - low) * 100.0) if high > low else None

    # benchmark, same day
    bench_change = bench_window = None
    if bench_i is not None and bench_i > 21:
        b_close = bench_rows["close"][bench_i]
        b_prev = bench_rows["close"][bench_i - 1]
        b_month = bench_rows["close"][max(0, bench_i - 21):bench_i + 1]
        bench_change = (b_close - b_prev) / b_prev * 100.0 if b_prev else None
        bench_window = ((b_month[-1] - b_month[0]) / b_month[0] * 100.0
                        if b_month[0] else None)

    evidence = {
        "symbol": ticker.split(".")[0], "ticker": ticker,
        "name": meta.get("name"), "cap_segment": meta.get("bucket"),
        "sector": meta.get("sector"), "as_of": rows["date"][i], "source": "backtest",
        "price": {
            "live": round(close, 2), "day_open": round(day_open, 2),
            "day_high": round(high, 2), "day_low": round(low, 2),
            "prev_close": round(prev, 2),
            "day_change_pct": round(day_change, 2) if day_change is not None else None,
            "volume": int(volume) or None,
        },
        "range_52w": {
            "high": round(year_hi, 2), "low": round(year_lo, 2),
            "pct_from_high": round(from_high, 2) if from_high is not None else None,
            "position_pct": round(position, 2) if position is not None else None,
        },
        "technicals": {
            "rvol": round(rvol, 2) if rvol else None,
            "rvol_raw": round(rvol, 2) if rvol else None,
            "rvol_method": "full historical session",
            "price_vs_sma_pct": round(vs_sma, 2) if vs_sma is not None else None,
            "sma_period": SMA_PERIOD,
            "window_return_pct": round(window_return, 2) if window_return is not None else None,
            "swing_high": round(max(rows["high"][max(0, i - 21):i + 1]), 2),
            "swing_low": round(min(rows["low"][max(0, i - 21):i + 1]), 2),
            "day_range_position_pct": round(day_pos, 2) if day_pos is not None else None,
            "trend": trend,
            "atr_pct": round(atr_pct, 2) if atr_pct else None,
            "move_vs_atr": round(move_vs_atr, 2) if move_vs_atr else None,
        },
        # Not reconstructible point-in-time. Null and declared, which is
        # exactly how the live engine treats an uncovered stock.
        "analyst": {"consensus": None, "num_analysts": None, "buy_pct": None,
                    "hold_pct": None, "sell_pct": None, "target_mean": None,
                    "target_low": None, "target_high": None, "upside_pct": None},
        "news": {"total": 0, "positive": 0, "negative": 0, "neutral": 0,
                 "net_tone": 0, "recent": []},
        "intraday": {"available": False,
                     "reason": "backtest runs on daily bars — no session tape"},
        "market": {"phase": "backtest", "label": "historical daily bar",
                   "live_session": False, "session_fraction": 1.0,
                   "minutes_to_close": 0},
        "relative": {
            "benchmark": data_sources.BENCHMARK_NAME,
            "benchmark_day_change_pct": round(bench_change, 2) if bench_change is not None else None,
            "benchmark_window_return_pct": round(bench_window, 2) if bench_window is not None else None,
            "rel_day_change_pct": (round(day_change - bench_change, 2)
                                   if day_change is not None and bench_change is not None else None),
            "rel_window_return_pct": (round(window_return - bench_window, 2)
                                      if window_return is not None and bench_window is not None else None),
            "outperforming": None,
            "sector": meta.get("sector"),
            "sector_median_pct": round(sector_move, 2) if sector_move is not None else None,
            "sector_rel_pct": (round(day_change - sector_move, 2)
                               if day_change is not None and sector_move is not None else None),
        },
        "events": {"next_earnings": None, "days_to_earnings": None,
                   "earnings_inside_horizon": None},
        "data_gaps": ["analyst.consensus", "analyst.num_analysts", "analyst.buy_pct",
                      "analyst.hold_pct", "analyst.sell_pct", "analyst.target_mean",
                      "analyst.target_low", "analyst.target_high", "analyst.upside_pct",
                      "news (no historical archive)"],
        "notes": ["Backtest bundle: analyst and news inputs are unavailable "
                  "point-in-time and are declared as gaps."],
    }
    return evidence


def forward_return(rows, i, days):
    """Close-to-close return over the next `days` sessions, or None."""
    j = i + days
    if j >= len(rows["close"]):
        return None
    entry = rows["close"][i]
    return (rows["close"][j] - entry) / entry * 100.0 if entry else None


# ---------------------------------------------------------------------------
# the walk
# ---------------------------------------------------------------------------

def run(years=3, shortlist_per_bucket=4, step=1, log=print):
    universe = data_sources.load_universe()
    meta = {}
    tickers = []
    for bucket in data_sources.BUCKETS:
        for entry in universe.get(bucket, []):
            meta[entry["ticker"]] = {"bucket": bucket, "sector": entry.get("sector"),
                                     "name": entry.get("name")}
            tickers.append(entry["ticker"])

    series = load_history(tickers, years, log=log)
    bench = series.get(data_sources.BENCHMARK)
    if not bench:
        log("benchmark unavailable — relative strength will be null throughout")
        bench = {"date": [], "close": []}

    bench_index = {d: k for k, d in enumerate(bench["date"])}
    live = [t for t in tickers if t in series]

    # a common date axis: days on which most of the universe traded
    axis = sorted({d for t in live for d in series[t]["date"]})
    axis = [d for d in axis
            if sum(1 for t in live if d in _index_of(series[t])) >= max(3, len(live) // 2)]

    start = WARMUP
    end = len(axis) - max(FORWARD_WINDOWS) - 1
    log(f"{len(live)} tickers · {len(axis)} sessions · "
        f"evaluating {axis[start]} to {axis[end]}")

    records = []
    market_days = 0

    for a in range(start, end, step):
        day = axis[a]
        market_days += 1

        # --- screen the universe as of this close ---------------------------
        candidates = []
        sector_moves = {}
        for ticker in live:
            idx = _index_of(series[ticker]).get(day)
            if idx is None or idx < WARMUP:
                continue
            rows = series[ticker]
            prev = rows["close"][idx - 1]
            change = (rows["close"][idx] - prev) / prev * 100.0 if prev else None
            if change is None:
                continue
            sector = meta[ticker].get("sector")
            if sector:
                sector_moves.setdefault(sector, []).append(change)

            prior_vol = [v for v in rows["volume"][max(0, idx - 20):idx] if v > 0]
            rvol = (rows["volume"][idx] / (sum(prior_vol) / len(prior_vol))
                    if prior_vol and rows["volume"][idx] > 0 else None)
            candidates.append({"ticker": ticker, "idx": idx, "day_change_pct": change,
                               "rvol": rvol, "bucket": meta[ticker]["bucket"]})

        if not candidates:
            continue

        b_idx = bench_index.get(day)
        b_change = None
        if b_idx is not None and b_idx > 0 and bench["close"][b_idx - 1]:
            b_change = ((bench["close"][b_idx] - bench["close"][b_idx - 1])
                        / bench["close"][b_idx - 1] * 100.0)
        for c in candidates:
            c["rel_day_change_pct"] = (c["day_change_pct"] - b_change
                                       if b_change is not None else None)

        sector_median = {s: statistics.median(v) for s, v in sector_moves.items()
                         if len(v) >= 2}

        shortlist = []
        for bucket in data_sources.BUCKETS:
            in_bucket = [c for c in candidates if c["bucket"] == bucket]
            shortlist.extend(
                data_sources.screen_bucket(in_bucket, shortlist_per_bucket))

        # --- judge, then look forward ---------------------------------------
        for pick in shortlist:
            ticker, idx = pick["ticker"], pick["idx"]
            rows = series[ticker]
            evidence = build_evidence_at(
                ticker, meta[ticker], rows, idx, bench, b_idx,
                sector_median.get(meta[ticker].get("sector")))

            result = scoring.evaluate(evidence)
            verdict = result["tracks"]["positional"]

            record = {
                "date": day, "symbol": evidence["symbol"],
                "sector": meta[ticker].get("sector"),
                "verdict": verdict["verdict"], "confidence": verdict["confidence"],
                "net": verdict["net"], "close": rows["close"][idx],
                "rvol": evidence["technicals"]["rvol"],
                "position_52w": evidence["range_52w"]["position_pct"],
            }
            for w in FORWARD_WINDOWS:
                record[f"fwd_{w}"] = forward_return(rows, idx, w)
                if b_idx is not None:
                    record[f"bench_{w}"] = forward_return(bench, b_idx, w)
            records.append(record)

    log(f"{len(records)} verdicts across {market_days} sessions")
    return records


def _index_of(rows, _cache={}):
    key = id(rows)
    if key not in _cache:
        _cache[key] = {d: i for i, d in enumerate(rows["date"])}
    return _cache[key]


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    mean = sum(values) / len(values)
    positive = sum(1 for v in values if v > 0)
    sd = statistics.pstdev(values) if len(values) > 1 else 0.0
    # t against zero: is the average move distinguishable from noise?
    t = (mean / (sd / math.sqrt(len(values)))) if sd and len(values) > 1 else 0.0
    return {"n": len(values), "mean": round(mean, 3), "median": round(statistics.median(values), 3),
            "hit_rate": round(positive / len(values) * 100, 1), "sd": round(sd, 2),
            "t_stat": round(t, 2)}


def analyse(records):
    out = {"total": len(records), "by_verdict": {}, "benchmark": {}, "edge": {}}
    if not records:
        return out

    for verdict in ("BUY", "WATCH", "AVOID"):
        subset = [r for r in records if r["verdict"] == verdict]
        block = {"count": len(subset)}
        for w in FORWARD_WINDOWS:
            block[f"fwd_{w}"] = _stats([r.get(f"fwd_{w}") for r in subset])
        out["by_verdict"][verdict] = block

    for w in FORWARD_WINDOWS:
        out["benchmark"][f"fwd_{w}"] = _stats([r.get(f"bench_{w}") for r in records])
        out["edge"][f"all_screened_{w}"] = _stats([r.get(f"fwd_{w}") for r in records])

    # the question that matters: BUY minus everything else
    for w in FORWARD_WINDOWS:
        buys = [r.get(f"fwd_{w}") for r in records if r["verdict"] == "BUY"]
        rest = [r.get(f"fwd_{w}") for r in records if r["verdict"] != "BUY"]
        b, o = _stats(buys), _stats(rest)
        if b and o:
            out["edge"][f"buy_minus_rest_{w}"] = round(b["mean"] - o["mean"], 3)
        bench = _stats([r.get(f"bench_{w}") for r in records if r["verdict"] == "BUY"])
        if b and bench:
            out["edge"][f"buy_minus_bench_{w}"] = round(b["mean"] - bench["mean"], 3)
    return out


def report(analysis, log=print):
    log("")
    log("=" * 78)
    log(f"  BACKTEST — {analysis['total']} verdicts")
    log("=" * 78)

    header = f"  {'verdict':<9}{'n':>6}" + "".join(f"{'+' + str(w) + 'd mean':>12}" for w in FORWARD_WINDOWS)
    log(header)
    log("  " + "-" * 74)
    for verdict in ("BUY", "WATCH", "AVOID"):
        block = analysis["by_verdict"].get(verdict) or {}
        line = f"  {verdict:<9}{block.get('count', 0):>6}"
        for w in FORWARD_WINDOWS:
            s = block.get(f"fwd_{w}")
            line += f"{(str(s['mean']) + '%') if s else '—':>12}"
        log(line)

    bench_line = f"  {'NIFTY':<9}{'':>6}"
    for w in FORWARD_WINDOWS:
        s = analysis["benchmark"].get(f"fwd_{w}")
        bench_line += f"{(str(s['mean']) + '%') if s else '—':>12}"
    log(bench_line)

    log("")
    log("  hit rate (% of signals with a positive forward return)")
    for verdict in ("BUY", "WATCH", "AVOID"):
        block = analysis["by_verdict"].get(verdict) or {}
        line = f"  {verdict:<9}{'':>6}"
        for w in FORWARD_WINDOWS:
            s = block.get(f"fwd_{w}")
            line += f"{(str(s['hit_rate']) + '%') if s else '—':>12}"
        log(line)

    log("")
    log("  edge — BUY mean minus the alternatives (percentage points)")
    for w in FORWARD_WINDOWS:
        rest = analysis["edge"].get(f"buy_minus_rest_{w}")
        bench = analysis["edge"].get(f"buy_minus_bench_{w}")
        log(f"    +{w:<3}d   vs other verdicts: {rest if rest is not None else '—':>8}"
            f"      vs NIFTY: {bench if bench is not None else '—':>8}")

    log("")
    log("  t-statistic of the BUY forward return against zero")
    log("  (|t| under ~2 means the average move is indistinguishable from noise)")
    buy = analysis["by_verdict"].get("BUY") or {}
    for w in FORWARD_WINDOWS:
        s = buy.get(f"fwd_{w}")
        if s:
            log(f"    +{w:<3}d   t = {s['t_stat']:>6}   (n={s['n']}, sd={s['sd']})")
    log("=" * 78)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backtest the deterministic panel")
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--shortlist", type=int, default=4)
    parser.add_argument("--step", type=int, default=1,
                        help="evaluate every Nth session (speed vs coverage)")
    parser.add_argument("--out", default="backtest_results.json")
    args = parser.parse_args(argv)

    records = run(years=args.years, shortlist_per_bucket=args.shortlist, step=args.step)
    analysis = analyse(records)
    report(analysis)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"generated": datetime.now().isoformat(),
                   "params": vars(args), "analysis": analysis,
                   "records": records[:4000]}, fh, indent=1, default=str)
    print(f"\nfull results -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
