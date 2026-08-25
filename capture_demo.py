"""
capture_demo.py — replace demo_data/*.json with genuine live snapshots.

The bundles shipped in demo_data/ are illustrative: real NSE tickers, plausible
but hand-built numbers, clearly labelled as such inside each file. Run this
during market hours and every bundle becomes a real capture pulled through the
exact same yfinance path that live mode uses.

    python capture_demo.py                # capture the whole universe.json
    python capture_demo.py RELIANCE.NS DIXON.NS
"""

from __future__ import annotations

import json
import os
import sys

import data_sources

OUT_DIR = data_sources.DEMO_DIR

CAPTURE_NOTE = ("Captured from live yfinance data by capture_demo.py — a real "
                "snapshot, frozen at the timestamp in `as_of`.")


def main(argv):
    universe = data_sources.load_universe()
    wanted = {t.upper() for t in argv}

    if wanted:
        entries = [
            dict(entry, bucket=bucket)
            for bucket in data_sources.BUCKETS
            for entry in universe.get(bucket, [])
            if entry["ticker"].upper() in wanted
        ]
        missing = wanted - {e["ticker"].upper() for e in entries}
        for ticker in sorted(missing):
            print(f"  ! {ticker} is not in universe.json — skipped")
    else:
        entries = [
            dict(entry, bucket=bucket)
            for bucket in data_sources.BUCKETS
            for entry in universe.get(bucket, [])
        ]

    if not entries:
        print("nothing to capture")
        return 1

    print(f"capturing {len(entries)} tickers into {OUT_DIR}")
    os.makedirs(OUT_DIR, exist_ok=True)

    quotes, benchmark = data_sources.fetch_quotes(
        {bucket: [e for e in entries if e["bucket"] == bucket]
         for bucket in data_sources.BUCKETS},
        log=lambda m: print(f"  · {m}"),
    )
    for rows in quotes.values():
        for quote in rows:
            quote["benchmark"] = benchmark

    written = 0
    for bucket in data_sources.BUCKETS:
        for quote in quotes.get(bucket, []):
            symbol = quote["ticker"].split(".")[0]
            try:
                bundle = data_sources.build_evidence_live(
                    quote, log=lambda m: print(f"  · {m}"))
            except Exception as exc:                               # noqa: BLE001
                print(f"  ! {symbol}: {type(exc).__name__}: {exc} — skipped")
                continue

            bundle["source"] = "demo"
            bundle["notes"] = [data_sources.NO_RATIOS_NOTE, CAPTURE_NOTE]

            path = os.path.join(OUT_DIR, f"{symbol}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh, indent=2, ensure_ascii=False)
                fh.write("\n")

            gaps = len(bundle["data_gaps"])
            change = (bundle["price"] or {}).get("day_change_pct")
            print(f"  ✓ {symbol:<12} {bucket:<5} "
                  f"{change if change is not None else 'n/a':>7} % day  ·  {gaps} gap(s)")
            written += 1

    print(f"\n{written} bundle(s) written. Demo mode now runs on real captured data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
