"""
backtest_swing.py — measure the positional rules across years, not weeks.

    python backtest_swing.py --years 5

Daily bars are not rate-limited the way intraday is, so this covers several
market phases instead of one quarter. That matters more than it sounds: the
existing daily backtest of this project found BUY signals were anti-predictive
below the 200-day average, which only became visible because the sample
spanned a regime change.

Every rule is evaluated with a strict point-in-time context — the indicators
at bar `i` are computed from bars up to `i` and nothing after — so a rule
cannot be rewarded for information it would not have had.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

import data_sources
import swing_strategies

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(HERE, "backtest_swing.json")


def _series(frame, column):
    try:
        return [float(x) for x in frame[column].tolist() if x == x]
    except Exception:                                              # noqa: BLE001
        return []


def history_for(ticker, years, log=print):
    import yfinance as yf
    try:
        frame = yf.download(ticker, period=f"{years}y", interval="1d",
                            progress=False, auto_adjust=False, threads=False)
    except Exception as exc:                                       # noqa: BLE001
        log(f"  {ticker}: download failed ({type(exc).__name__})")
        return None
    if frame is None or getattr(frame, "empty", True):
        return None
    if hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1:
        try:
            frame = frame.xs(ticker, axis=1, level=1)
        except (KeyError, ValueError):
            frame = frame.droplevel(1, axis=1)
    return frame


def run(years=5, horizon=swing_strategies.DEFAULT_HORIZON, symbols=None, step=1,
        log=print):
    universe = data_sources.load_universe()
    entries = [dict(e, bucket=b) for b, rows in universe.items() for e in rows]
    if symbols:
        wanted = {s.strip().upper() for s in symbols}
        entries = [e for e in entries
                   if e["ticker"].split(".")[0].upper() in wanted]
    if not entries:
        log("no symbols to test")
        return None

    log(f"swing backtest: {len(entries)} symbols, {years}y of daily bars, "
        f"{horizon}-day horizon")
    log("")

    trades = defaultdict(list)
    bars_seen = 0

    for entry in entries:
        ticker = entry["ticker"]
        symbol = ticker.split(".")[0]
        frame = history_for(ticker, years, log=log)
        if frame is None:
            continue
        highs = _series(frame, "High")
        lows = _series(frame, "Low")
        closes = _series(frame, "Close")
        volumes = _series(frame, "Volume")
        n = min(len(highs), len(lows), len(closes), len(volumes))
        if n < swing_strategies.WARMUP + horizon + 5:
            log(f"  {symbol:<12} too little history ({n} bars)")
            continue

        fired = 0
        for i in range(swing_strategies.WARMUP, n - horizon - 1, step):
            for name, res in swing_strategies.evaluate_at(
                    highs, lows, closes, volumes, i, horizon).items():
                trades[name].append({
                    "symbol": symbol, "bar": i,
                    "r": res["outcome"]["r"],
                    "outcome": res["outcome"]["outcome"],
                    "days_held": res["outcome"]["days_held"],
                })
                fired += 1
        bars_seen += max(0, n - swing_strategies.WARMUP - horizon)
        log(f"  {symbol:<12} {n:>5} bars, {fired:>4} signals")

    summary = {}
    for name in swing_strategies.SWING_STRATEGIES:
        summary[name] = _summarise(trades.get(name, []))

    blob = {
        "generated": data_sources.now_ist_str(),
        "window": f"{years}y of daily bars, {horizon}-day horizon",
        "symbols": len(entries),
        "bars_tested": bars_seen,
        "strategies": summary,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=1)
    return blob


def _summarise(rows):
    n = len(rows)
    if not n:
        return {"trades": 0, "enough": False,
                "note": "no signals triggered in the sample"}
    rs = [t["r"] for t in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    enough = n >= swing_strategies.MIN_TRADES_TO_REPORT
    return {
        "trades": n,
        "enough": enough,
        "win_rate_pct": round(len(wins) / n * 100.0, 1),
        "expectancy_r": round(sum(rs) / n, 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_days_held": round(sum(t["days_held"] for t in rows) / n, 1),
        "best_r": round(max(rs), 2),
        "worst_r": round(min(rs), 2),
        "note": None if enough else
                f"only {n} signals — below the "
                f"{swing_strategies.MIN_TRADES_TO_REPORT} needed to mean anything",
    }


def report(blob, log=print):
    if not blob:
        return
    log("")
    log("=" * 80)
    log(f"  Swing strategies · {blob['symbols']} symbols · {blob['window']}")
    log("=" * 80)
    log("")
    log(f"  {'strategy':<22}{'trades':>8}{'win %':>8}{'expectancy':>13}"
        f"{'PF':>7}{'held':>8}")
    log("  " + "-" * 76)
    ranked = sorted(blob["strategies"].items(),
                    key=lambda kv: (kv[1].get("expectancy_r") or -99), reverse=True)
    for name, st in ranked:
        if not st.get("trades"):
            log(f"  {name:<22}{'0':>8}   no signals")
            continue
        pf = f"{st['profit_factor']:.2f}" if st.get("profit_factor") else "—"
        flag = "" if st["enough"] else "  (thin)"
        log(f"  {name:<22}{st['trades']:>8}{st['win_rate_pct']:>8.1f}"
            f"{st['expectancy_r']:>+12.3f}R{pf:>7}"
            f"{st['avg_days_held']:>7.1f}d{flag}")
    log("")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backtest the swing strategies")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--horizon", type=int,
                        default=swing_strategies.DEFAULT_HORIZON)
    parser.add_argument("--step", type=int, default=1,
                        help="evaluate every Nth bar; raise it to go faster")
    parser.add_argument("--symbols", default="")
    args = parser.parse_args(argv)

    symbols = [s for s in args.symbols.split(",") if s.strip()] or None
    blob = run(years=args.years, horizon=args.horizon, symbols=symbols,
               step=args.step)
    report(blob)
    if blob:
        print(f"  written to {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
