"""
backtest_intraday.py — measure the intraday strategies on real NSE sessions.

    python backtest_intraday.py                # whole universe, ~59 sessions
    python backtest_intraday.py --symbols RELIANCE,TCS

Yahoo serves about 60 calendar days of 5-minute bars, so this is roughly 59
sessions of one recent regime. That is the ceiling, not a choice. It is enough
to reject a rule that does not work and not enough to certify one that does,
which is why every number here is printed with its sample size and why
strategies.MIN_TRADES_TO_REPORT gates the hit rate entirely.

The output is deliberately two columns wide: win rate *and* expectancy. A rule
that wins often while losing money is the thing this file exists to expose.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

import data_sources
import strategies

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(HERE, "backtest_intraday.json")

PERIOD = "60d"
INTERVAL = "5m"


def _import_yf():
    import yfinance as yf
    return yf


def sessions_for(ticker, log=print):
    """
    Split one ticker's 5-minute history into per-session bar lists.

    The previous session's close and a trailing average volume come from the
    sample itself, so each day is judged against what was knowable that
    morning rather than against the whole period.
    """
    yf = _import_yf()
    try:
        frame = yf.download(ticker, period=PERIOD, interval=INTERVAL,
                            progress=False, auto_adjust=False, threads=False)
    except Exception as exc:                                       # noqa: BLE001
        log(f"  {ticker}: download failed ({type(exc).__name__})")
        return []
    if frame is None or getattr(frame, "empty", True):
        log(f"  {ticker}: no intraday history returned")
        return []

    if hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1:
        try:
            frame = frame.xs(ticker, axis=1, level=1)
        except (KeyError, ValueError):
            frame = frame.droplevel(1, axis=1)

    days = defaultdict(list)
    for stamp, row in frame.iterrows():
        key = str(stamp)[:10]
        days[key].append({
            "open": row.get("Open"), "high": row.get("High"),
            "low": row.get("Low"), "close": row.get("Close"),
            "volume": row.get("Volume"),
        })

    ordered = sorted(days.items())
    out, prev_close, volumes = [], None, []
    for date, bars in ordered:
        avg_volume = sum(volumes[-20:]) / len(volumes[-20:]) if volumes else None
        s = strategies.session(bars, prev_close=prev_close, avg_volume=avg_volume)
        if s:
            out.append((date, s))
            prev_close = s["close"][-1]
            volumes.append(s["day_volume"])
        else:                       # keep the close even from an unusable day
            closes = [b.get("close") for b in bars if b.get("close") == b.get("close")]
            if closes:
                prev_close = float(closes[-1])
    return out


def run(symbols=None, log=print):
    universe = data_sources.load_universe()
    entries = [dict(e, bucket=bucket)
               for bucket, rows in universe.items() for e in rows]
    if symbols:
        wanted = {s.strip().upper() for s in symbols}
        entries = [e for e in entries
                   if e["ticker"].split(".")[0].upper() in wanted]
    if not entries:
        log("no symbols to test")
        return None

    log(f"intraday backtest: {len(entries)} symbols, {PERIOD} of {INTERVAL} bars")
    log("")

    trades = defaultdict(list)
    sessions_seen = 0

    for entry in entries:
        ticker = entry.get("ticker")
        symbol = ticker.split(".")[0] if ticker else None
        if not ticker:
            continue
        days = sessions_for(ticker, log=log)
        if not days:
            continue
        sessions_seen += len(days)
        fired = 0
        for date, s in days:
            for name, result in strategies.evaluate(s).items():
                trades[name].append({
                    "symbol": symbol, "date": date,
                    "r": result["outcome"]["r"],
                    "outcome": result["outcome"]["outcome"],
                    "bars_held": result["outcome"]["bars_held"],
                })
                fired += 1
        log(f"  {symbol:<12} {len(days):>3} sessions, {fired:>3} setups")

    summary = {name: strategies.summarise(rows) for name, rows in trades.items()}
    for name in strategies.STRATEGIES:
        summary.setdefault(name, strategies.summarise([]))

    blob = {
        "generated": data_sources.now_ist_str(),
        "window": f"{PERIOD} of {INTERVAL} bars",
        "symbols": len(entries),
        "stock_sessions": sessions_seen,
        "strategies": summary,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=1)
    return blob


def report(blob, log=print):
    if not blob:
        return
    log("")
    log("=" * 78)
    log(f"  Intraday strategies · {blob['stock_sessions']} stock-sessions "
        f"· {blob['window']}")
    log("=" * 78)
    log("")
    log(f"  {'strategy':<16}{'trades':>7}{'win %':>8}{'expectancy':>12}"
        f"{'profit factor':>15}")
    log("  " + "-" * 74)

    ranked = sorted(blob["strategies"].items(),
                    key=lambda kv: (kv[1].get("expectancy_r") or -99), reverse=True)
    for name, st in ranked:
        if not st.get("trades"):
            log(f"  {name:<16}{'0':>7}   no setups triggered")
            continue
        win = f"{st['win_rate_pct']:.1f}"
        exp = f"{st['expectancy_r']:+.3f}R"
        pf = f"{st['profit_factor']:.2f}" if st.get("profit_factor") else "—"
        flag = "" if st["enough"] else "  (thin)"
        log(f"  {name:<16}{st['trades']:>7}{win:>8}{exp:>12}{pf:>15}{flag}")

    log("")
    log("  Win rate and expectancy have to be read together. A rule can win")
    log("  most of its trades and still lose money, which is what a high win")
    log("  percentage beside a negative expectancy means.")
    log("")
    thin = [n for n, s in blob["strategies"].items()
            if s.get("trades") and not s.get("enough")]
    if thin:
        log(f"  Thin samples ({', '.join(thin)}) are reported but not trusted:")
        log(f"  under {strategies.MIN_TRADES_TO_REPORT} trades a hit rate is noise.")
        log("")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backtest the intraday strategies")
    parser.add_argument("--symbols", default="",
                        help="comma-separated subset, e.g. RELIANCE,TCS")
    args = parser.parse_args(argv)

    symbols = [s for s in args.symbols.split(",") if s.strip()] or None
    blob = run(symbols=symbols)
    report(blob)
    if blob:
        print(f"  written to {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
