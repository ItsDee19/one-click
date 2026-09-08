"""Whole-universe research, independent of LLM shortlist and signal delivery.

Run `python intelligence.py` to research all NSE equity trading-list entries.
This entry point never imports app.py, sends messages, or books paper trades.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import os
from statistics import median

import data_sources as ds
from intelligence_store import IntelligenceStore
import market
import scoring


def _refresh_sector_context(quotes, bundles, records, store, run_id, say):
    """Sector comparisons use only dated daily observations from this scan."""
    from evidence_quality import finite_number, parse_timestamp
    groups, classified, dated = {}, {}, {}
    for quote in quotes:
        ticker = quote["ticker"]
        bundle = bundles.get(ticker) or {}
        sector = bundle.get("sector") or quote.get("sector")
        if sector:
            quote["sector"] = sector
            classified[sector] = classified.get(sector, 0) + 1
        stamp = parse_timestamp((bundle.get("technicals") or {}).get("last_bar"))
        day = stamp.date().isoformat() if stamp else None
        dated[ticker] = day
        change = finite_number(quote.get("day_change_pct"))
        if sector and day and change is not None:
            groups.setdefault((sector, day), []).append(change)
    classification_pct = round(sum(classified.values()) / len(quotes) * 100, 1) if quotes else 0
    for quote in quotes:
        ticker = quote["ticker"]
        bundle = bundles.get(ticker)
        if not bundle or records.get(ticker) == "failed":
            continue
        sector, day = quote.get("sector"), dated[ticker]
        samples = groups.get((sector, day), [])
        center = round(median(samples), 4) if len(samples) >= 2 else None
        change = finite_number(quote.get("day_change_pct"))
        relative = round(change - center, 4) if center is not None and change is not None else None
        context = {"sector": sector, "sector_median_pct": center, "sector_rel_pct": relative,
                   "sector_peer_count": len(samples), "sector_comparison_date": day,
                   "sector_coverage_pct": round(len(samples) / classified[sector] * 100, 1) if sector in classified else None,
                   "sector_classification_coverage_pct": classification_pct,
                   "sector_coverage_basis": "same-date observations among classified stocks in this scan, including this stock; not verified full-sector coverage"}
        quote.update(context)
        existing = bundle.get("relative") or {}
        if all(existing.get(key) == value for key, value in context.items()):
            continue
        bundle["relative"] = dict(existing, **context)
        try:
            result = scoring.evaluate(bundle)
            store.save(run_id, bundle, result, records[ticker], ds.screen_score(quote))
        except Exception as exc:
            say(f"{ticker}: sector comparison rescore failed ({type(exc).__name__}); prior stored analysis retained")
    return {"sector_classified": sum(classified.values()),
            "sector_classification_coverage_pct": classification_pct}


def analyze_universe(universe, shortlist_per_bucket=4, log=None, progress=None, store=None,
                     research_scope=None, universe_metadata=None):
    import company_data
    say = log or (lambda message: None)
    store = store or IntelligenceStore()
    # A ticker is studied once even if a custom universe repeats it in two buckets.
    entries, seen = [], set()
    for bucket, rows in universe.items():
        for entry in rows:
            if entry["ticker"] not in seen:
                seen.add(entry["ticker"])
                entries.append(dict(entry, bucket=bucket))
    normalized = {bucket: [] for bucket in ds.BUCKETS}
    for entry in entries:
        bucket = entry["bucket"] if entry["bucket"] in normalized else "unclassified"
        normalized[bucket].append(dict(entry, bucket=bucket))
    scope = research_scope or os.environ.get("INTELLIGENCE_RESEARCH_SCOPE", "all").strip().lower()
    if scope not in ("all", "daily"):
        raise ValueError("INTELLIGENCE_RESEARCH_SCOPE must be all or daily")
    coverage = {"scope": "NSE equity trading lists", "listed": len(entries),
                "daily_attempted": 0, "daily_available": 0, "analyzed": 0,
                "research_scope": scope, "research_attempted": 0, "enriched": 0,
                "partial_research": 0, "failed": 0, "missing_prices": 0,
                "debate_selected": 0, "phase": "daily_download",
                "universe": universe_metadata or {},
                "note": "All entries receive stored rule analysis. LLM debate is a separate selection. "
                        "Unavailable provider fields remain explicit gaps; coverage is not predictive accuracy."}
    run_id = store.start(coverage, entries)
    coverage["run_id"] = run_id

    def publish(status="running"):
        ds.LAST_COVERAGE = dict(coverage, status=status)
        store.update_coverage(run_id, coverage, status)
        if progress:
            progress(dict(ds.LAST_COVERAGE))

    publish()
    bundles, records = {}, {}
    try:
        quotes, benchmark = ds.fetch_quotes(normalized, log=say)
        ds.LAST_QUOTES = quotes
        flat = [q for rows in quotes.values() for q in rows]
        coverage["daily_attempted"] = len(entries)
        coverage["daily_available"] = sum(q.get("frame") is not None for q in flat)
        regime = market.regime(log=say)
        for quote in flat:
            quote.update(benchmark=benchmark, regime=regime, history_frame=quote.get("frame"))

        def record(quote, profile, enriched=False):
            prepared = dict(quote, company_data=profile, evidence_scope="enriched" if enriched else "daily")
            try:
                bundle = ds.build_evidence_live(prepared, log=say)
                if bundle.get("sector"):
                    quote["sector"] = bundle["sector"]
                bundle["strategies"] = ds.fired_strategies(prepared)
                result = scoring.evaluate(bundle)
                error = None
                has_price = (bundle.get("price") or {}).get("live") is not None
                if not has_price:
                    status = "missing_data"
                elif enriched:
                    meta = profile.get("metadata") or {}
                    status = "partial" if meta.get("errors") or meta.get("stale") else "enriched"
                else:
                    status = "analyzed"
            except Exception as exc:
                error = f"{type(exc).__name__}: analysis could not be computed"
                bundle = {"ticker": quote["ticker"], "symbol": quote["ticker"].rsplit(".", 1)[0],
                          "name": quote.get("name"), "data_gaps": ["analysis"], "evidence_scope": "unavailable"}
                result, status = {}, "failed"
                say(f"{quote['ticker']}: {error}")
            rank = ds.screen_score(quote)
            store.save(run_id, bundle, result, status, rank, error)
            bundles[quote["ticker"]] = bundle
            records[quote["ticker"]] = status

        coverage["phase"] = "daily_analysis"
        publish()
        # Persist baseline analysis for everyone before slow provider enrichment begins.
        for index, quote in enumerate(flat, 1):
            record(quote, {})
            coverage["analyzed"] = index
            if index % 25 == 0 or index == len(flat):
                publish()

        session = market.describe()
        if session.get("live_session"):
            coverage["phase"] = "intraday_download"
            publish()
            frames = ds.fetch_intraday([q["ticker"] for q in flat], log=say)
            for quote in flat:
                quote["intraday_frame"] = frames.get(quote["ticker"])
            coverage["intraday_available"] = sum(f is not None for f in frames.values())
        else:
            coverage["intraday_available"] = 0
            coverage["intraday_note"] = "No live session; intraday analysis unavailable."

        if scope == "all":
            coverage["phase"] = "company_research"
            publish()
            workers = ds._setting_int("INTELLIGENCE_WORKERS", 4, high=8)
            say(f"studying company metrics, analyst evidence and news for ALL {len(flat)} stocks ({workers} workers)")
            # Per-symbol caches make subsequent runs and interrupted-run restarts inexpensive.
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="company-research") as pool:
                pending = {pool.submit(company_data.fetch_company_data, q["ticker"], log=say): q for q in flat}
                for future in as_completed(pending):
                    quote = pending[future]
                    try:
                        profile = future.result()
                    except Exception as exc:
                        profile = {"metadata": {"errors": [f"company research failed ({type(exc).__name__})"]}}
                    record(quote, profile, enriched=True)
                    coverage["research_attempted"] += 1
                    status = records[quote["ticker"]]
                    coverage["enriched"] += status == "enriched"
                    coverage["partial_research"] += status == "partial"
                    if coverage["research_attempted"] % 10 == 0 or coverage["research_attempted"] == len(flat):
                        say(f"company research: {coverage['research_attempted']}/{len(flat)}; "
                            f"{coverage['enriched']} complete, {coverage['partial_research']} partial")
                        publish()
        elif session.get("live_session"):
            for quote in flat:
                record(quote, {})

        coverage.update(_refresh_sector_context(flat, bundles, records, store, run_id, say))
        selected = []
        for bucket, rows in quotes.items():
            eligible = [q for q in rows if (bundles.get(q["ticker"], {}).get("price") or {}).get("live") is not None]
            for quote in ds.screen_bucket(eligible, max(0, shortlist_per_bucket)):
                selected.append(bundles[quote["ticker"]])
        coverage.update(phase="complete", missing_prices=sum(s == "missing_data" for s in records.values()),
                        failed=sum(s == "failed" for s in records.values()), debate_selected=len(selected))
        incomplete = coverage["failed"] or coverage["missing_prices"] or coverage["partial_research"]
        incomplete = incomplete or coverage["universe"].get("degraded", False)
        publish("partial" if incomplete else "done")
        say(f"whole-universe analysis stored: {len(records)}/{len(entries)}; {len(selected)} selected for LLM debate")
        return len(entries), selected
    except Exception as exc:
        coverage.update(phase="error", error=f"{type(exc).__name__}: scan interrupted; completed records retained")
        publish("error")
        raise


_CSV_FIELDS = (
    "run_id", "ticker", "name", "sector", "cap_segment", "status", "analyzed_at", "evidence_scope",
    "price", "quote_as_of", "quote_source", "day_change_pct", "volume", "rvol", "price_vs_sma_pct",
    "atr_pct", "window_return_pct", "rel_day_change_pct", "sector_rel_pct", "sector_peer_count",
    "trailing_pe", "forward_pe", "price_to_book", "roe_pct", "profit_margin_pct", "operating_margin_pct",
    "revenue_growth_pct", "earnings_growth_pct", "debt_to_equity_pct", "debt_to_equity_ratio",
    "market_cap", "currency", "fundamentals_as_of", "fundamentals_stale",
    "positional_verdict", "positional_confidence", "positional_actionable_now",
    "intraday_verdict", "intraday_confidence", "intraday_actionable_now", "data_gaps",
)


def write_csv(store, output_file, run_id=None):
    """Write every stored record to a text stream; return exported row count.

    The selected run is frozen before paging so a concurrent scan cannot mix
    observations from two runs into one export. Verdicts retain their analysis
    timestamp; read-time actionability is explicitly separate.
    """
    writer = csv.DictWriter(output_file, fieldnames=_CSV_FIELDS)
    writer.writeheader()
    selected_run = store.coverage(run_id).get("run_id")
    if selected_run is None:
        return 0
    count = 0
    while True:
        page = store.query(run_id=selected_run, limit=500, offset=count, include_evidence=True)
        for item in page["items"]:
            ev, result = item.get("evidence") or {}, item.get("result") or {}
            price, technicals = ev.get("price") or {}, ev.get("technicals") or {}
            financials, relative = ev.get("fundamentals") or {}, ev.get("relative") or {}
            actionable = (item.get("current_evidence_quality") or {}).get("actionable") or {}
            row = {"run_id": selected_run, "ticker": item["ticker"], "name": item["name"],
                   "sector": ev.get("sector"), "cap_segment": ev.get("cap_segment"),
                   "status": item["status"], "analyzed_at": item["updated_at"],
                   "evidence_scope": ev.get("evidence_scope"), "price": price.get("live"),
                   "quote_as_of": price.get("as_of"), "quote_source": price.get("source"),
                   "day_change_pct": price.get("day_change_pct"), "volume": price.get("volume"),
                   "fundamentals_as_of": financials.get("as_of"), "fundamentals_stale": financials.get("stale"),
                   "data_gaps": "; ".join(str(gap) for gap in ev.get("data_gaps") or [])}
            for key in ("rvol", "price_vs_sma_pct", "atr_pct", "window_return_pct"):
                row[key] = technicals.get(key)
            for key in ("rel_day_change_pct", "sector_rel_pct", "sector_peer_count"):
                row[key] = relative.get(key)
            for key in _CSV_FIELDS:
                if key in financials and key not in row:
                    row[key] = financials[key]
            for track in ("positional", "intraday"):
                verdict = (result.get("tracks") or {}).get(track) or {}
                row[f"{track}_verdict"] = verdict.get("verdict")
                row[f"{track}_confidence"] = verdict.get("confidence")
                row[f"{track}_actionable_now"] = (
                    bool(actionable.get(track) and verdict.get("verdict") == "BUY"
                         and verdict.get("actionable") is True) if verdict else None)
            # Spreadsheet software must treat third-party strings as text.
            row = {key: ("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value)
                   for key, value in row.items()}
            writer.writerow(row)
            count += 1
        if not page["items"] or count >= page["total"]:
            break
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description="Research all NSE equities without sending signals")
    parser.add_argument("--curated", action="store_true", help="explicitly restrict to universe.json")
    parser.add_argument("--daily-only", action="store_true", help="skip company profiles/news, preserve all-stock price analysis")
    parser.add_argument("--coverage", action="store_true", help="print last scan coverage without fetching data")
    parser.add_argument("--export", metavar="PATH", help="export all stored stocks to CSV; combine with --coverage to skip a new scan")
    args = parser.parse_args(argv)
    store = IntelligenceStore()
    if not args.coverage:
        universe = ds.load_universe() if args.curated else ds.load_full_exchange(log=print)
        analyze_universe(universe, log=print, store=store, research_scope="daily" if args.daily_only else "all",
                         universe_metadata={"source": "curated", "degraded": False} if args.curated else ds.LAST_UNIVERSE_METADATA)
    if args.export:
        with open(args.export, "w", encoding="utf-8-sig", newline="") as output:
            count = write_csv(store, output)
        print(f"Exported {count} stock records to {args.export}")
    print(json.dumps(store.coverage(), indent=2))


if __name__ == "__main__":
    main()
