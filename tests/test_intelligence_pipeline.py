"""Whole-universe orchestration and durable storage without any network calls."""

from pathlib import Path
import csv
import io
import tempfile
import unittest
from unittest import mock

import company_data
import intelligence
from intelligence_store import IntelligenceStore


def entry(ticker, name=None):
    return {"ticker": ticker, "name": name or ticker.split(".")[0], "sector": None}


def make_universe():
    return {
        "large": [entry(f"L{index}.NS") for index in range(4)],
        "mid": [entry(f"M{index}.NS") for index in range(4)],
        "small": [entry("S0.NS"), entry("MISSING.NS")],
        "unclassified": [entry("NEW.NS")],
    }


def fixture_quotes(universe, log=None):
    result = {}
    counter = 0
    for bucket, rows in universe.items():
        result[bucket] = []
        for row in rows:
            counter += 1
            missing = row["ticker"] == "MISSING.NS"
            result[bucket].append(dict(row, bucket=bucket, frame=None if missing else object(),
                                       day_change_pct=None if missing else counter / 10,
                                       rel_day_change_pct=None if missing else counter / 10,
                                       rvol=1.2, test_price=None if missing else 100 + counter))
    return result, {"day_change_pct": 0}


def fixture_evidence(quote, log=None):
    price = quote.get("test_price")
    return {"ticker": quote["ticker"], "symbol": quote["ticker"].split(".")[0],
            "name": quote["name"], "cap_segment": quote["bucket"],
            "sector": (quote["company_data"].get("info") or {}).get("sector") or quote.get("sector"),
            "price": {"live": price}, "data_gaps": ["price.live"] if price is None else [],
            "technicals": {"last_bar": quote.get("test_daily_date", "2026-09-08")},
            "evidence_scope": quote["evidence_scope"],
            "fundamentals": company_data.build_fundamentals(
                quote["company_data"].get("info"), quote["company_data"].get("metadata"))}


def fixture_profile(ticker, log=None):
    return {"info": {"shortName": ticker, "returnOnEquity": 0.2, "profitMargins": 0.1},
            "recommendations": {"buy_pct": None, "hold_pct": None, "sell_pct": None},
            "news": {"total": 0, "recent": []},
            "metadata": {"fetched_at": "2026-09-08T01:00:00+00:00", "errors": [], "stale": False}}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = IntelligenceStore(Path(self.directory.name) / "intelligence.db")
        patches = [
            mock.patch.object(intelligence.ds, "fetch_quotes", side_effect=fixture_quotes),
            mock.patch.object(intelligence.ds, "build_evidence_live", side_effect=fixture_evidence),
            mock.patch.object(intelligence.ds, "fired_strategies", return_value={"intraday": {}, "swing": {}}),
            mock.patch.object(intelligence.scoring, "evaluate", return_value={"consensus_score": 6}),
            mock.patch.object(intelligence.market, "regime", return_value={"regime": "neutral"}),
            mock.patch.object(intelligence.market, "describe", return_value={"live_session": False}),
            mock.patch.object(intelligence.ds, "_import_yf", side_effect=AssertionError("network forbidden")),
            mock.patch.dict(intelligence.os.environ, {"INTELLIGENCE_WORKERS": "2"}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def analyze(self, universe=None, **kwargs):
        return intelligence.analyze_universe(universe or make_universe(), shortlist_per_bucket=1,
                                             store=self.store, **kwargs)

    def test_all_stocks_studied_and_stored_beyond_shortlist_including_unclassified(self):
        with mock.patch.object(company_data, "fetch_company_data", side_effect=fixture_profile) as fetch:
            listed, selected = self.analyze(research_scope="all")
        rows = self.store.query(include_evidence=True)
        self.assertEqual(11, listed)
        self.assertEqual(11, rows["total"])
        self.assertEqual(11, fetch.call_count)
        self.assertEqual(4, len(selected))
        by_ticker = {row["ticker"]: row for row in rows["items"]}
        self.assertEqual("enriched", by_ticker["NEW.NS"]["status"])
        self.assertEqual("unclassified", by_ticker["NEW.NS"]["evidence"]["cap_segment"])
        self.assertEqual("missing_data", by_ticker["MISSING.NS"]["status"])
        self.assertIn("price.live", by_ticker["MISSING.NS"]["evidence"]["data_gaps"])
        self.assertNotIn("MISSING.NS", [bundle["ticker"] for bundle in selected])
        self.assertEqual(11, rows["coverage"]["analyzed"])
        self.assertEqual(11, rows["coverage"]["research_attempted"])
        self.assertEqual(1, rows["coverage"]["missing_prices"])
        self.assertEqual("partial", rows["coverage"]["status"])

    def test_duplicate_tickers_are_studied_once_across_buckets(self):
        universe = {"large": [entry("A.NS")], "small": [entry("A.NS"), entry("B.NS")]}
        with mock.patch.object(company_data, "fetch_company_data", side_effect=fixture_profile) as fetch:
            listed, _ = self.analyze(universe, research_scope="all")
        self.assertEqual(2, listed)
        self.assertEqual(2, fetch.call_count)
        self.assertEqual(2, self.store.query()["total"])

    def test_one_failed_company_request_keeps_its_baseline_and_other_stocks(self):
        def profile(ticker, log=None):
            if ticker == "L1.NS":
                raise TimeoutError("failed company provider")
            return fixture_profile(ticker, log)
        with mock.patch.object(company_data, "fetch_company_data", side_effect=profile):
            self.analyze(research_scope="all")
        result = self.store.query(include_evidence=True)
        by_ticker = {row["ticker"]: row for row in result["items"]}
        self.assertEqual(11, result["total"])
        self.assertEqual("partial", by_ticker["L1.NS"]["status"])
        self.assertEqual(102, by_ticker["L1.NS"]["evidence"]["price"]["live"])
        self.assertEqual("enriched", by_ticker["L2.NS"]["status"])
        self.assertEqual(1, result["coverage"]["partial_research"])
        self.assertEqual(0, result["coverage"]["failed"])

    def test_daily_only_research_never_fetches_company_profiles(self):
        with mock.patch.object(company_data, "fetch_company_data", side_effect=AssertionError("profile requests forbidden")) as fetch:
            self.analyze(research_scope="daily")
        fetch.assert_not_called()
        result = self.store.query(include_evidence=True)
        self.assertEqual(11, result["total"])
        self.assertEqual(0, result["coverage"]["research_attempted"])
        self.assertEqual(10, result["coverage"]["records_by_status"]["analyzed"])
        self.assertTrue(all(row["evidence"]["evidence_scope"] == "daily" for row in result["items"]))

    def test_baselines_are_committed_before_any_company_enrichment_begins(self):
        checks = []
        def profile(ticker, log=None):
            snapshot = self.store.query(include_evidence=True)
            checks.append(all(row["evidence"] is not None for row in snapshot["items"]))
            self.assertEqual(11, snapshot["total"])
            raise TimeoutError("every profile request fails")
        with mock.patch.object(company_data, "fetch_company_data", side_effect=profile):
            self.analyze(research_scope="all")
        self.assertEqual([True] * 11, checks)
        result = self.store.query(include_evidence=True)
        self.assertTrue(all(row["evidence"] is not None for row in result["items"]))
        self.assertEqual(10, result["coverage"]["partial_research"])

    def test_fatal_enrichment_setup_error_retains_all_baseline_records(self):
        with mock.patch.object(intelligence, "ThreadPoolExecutor", side_effect=RuntimeError("executor unavailable")):
            with self.assertRaises(RuntimeError):
                self.analyze(research_scope="all")
        result = self.store.query(include_evidence=True)
        self.assertEqual("error", result["coverage"]["status"])
        self.assertEqual(11, result["total"])
        self.assertTrue(all(row["evidence"] is not None for row in result["items"]))
        self.assertEqual(10, result["coverage"]["records_by_status"]["analyzed"])

    def test_daily_download_failure_leaves_discoverable_pending_coverage(self):
        with mock.patch.object(intelligence.ds, "fetch_quotes", side_effect=TimeoutError("download failed")):
            with self.assertRaises(TimeoutError):
                self.analyze(research_scope="daily")
        result = self.store.query()
        self.assertEqual("error", result["coverage"]["status"])
        self.assertEqual(11, result["coverage"]["records_by_status"]["pending"])

    def test_enriched_sector_peers_compare_only_same_date_observations(self):
        universe = {"large": [entry("A.NS"), entry("B.NS"), entry("C.NS")],
                    "unclassified": [entry("D.NS")]}
        def quotes(rows, log=None):
            data, benchmark = fixture_quotes(rows)
            for bucket in data.values():
                for quote in bucket:
                    quote["day_change_pct"] = {"A.NS": 1, "B.NS": 3, "C.NS": 50, "D.NS": 4}[quote["ticker"]]
                    quote["test_daily_date"] = "2026-09-07" if quote["ticker"] == "C.NS" else "2026-09-08"
            return data, benchmark
        def profile(ticker, log=None):
            result = fixture_profile(ticker, log)
            if ticker != "D.NS":
                result["info"]["sector"] = "Technology"
            return result
        with mock.patch.object(intelligence.ds, "fetch_quotes", side_effect=quotes), mock.patch.object(company_data, "fetch_company_data", side_effect=profile):
            self.analyze(universe, research_scope="all")
        rows = {item["ticker"]: item["evidence"] for item in self.store.query(include_evidence=True)["items"]}
        self.assertEqual(2, rows["A.NS"]["relative"]["sector_median_pct"])
        self.assertEqual(-1, rows["A.NS"]["relative"]["sector_rel_pct"])
        self.assertEqual(2, rows["A.NS"]["relative"]["sector_peer_count"])
        self.assertEqual(66.7, rows["A.NS"]["relative"]["sector_coverage_pct"])
        self.assertEqual(75, rows["A.NS"]["relative"]["sector_classification_coverage_pct"])
        self.assertIsNone(rows["C.NS"]["relative"]["sector_median_pct"])
        self.assertEqual("Technology", intelligence.ds.LAST_QUOTES["large"][0]["sector"])


class IntelligenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = IntelligenceStore(Path(self.directory.name) / "coverage.db")

    def start_run(self, entries):
        return self.store.start({"listed": len(entries), "research_scope": "daily"}, entries)

    def save(self, run, ticker, rank, status="analyzed", name=None, price=100):
        self.store.save(run, {"ticker": ticker, "name": name or ticker, "price": {"live": price},
                              "fundamentals": {"roe_pct": 20}, "data_gaps": [],
                              "evidence_scope": "daily"}, {"consensus_score": 6}, status, rank)

    def test_paginated_sorted_search_and_evidence_selection(self):
        run = self.start_run([entry("C.NS"), entry("A.NS"), entry("B.NS")])
        for ticker, rank in (("A.NS", 1), ("B.NS", 3), ("C.NS", 2)):
            self.save(run, ticker, rank)
        first = self.store.query(limit=2)
        second = self.store.query(limit=2, offset=2, include_evidence=True)
        self.assertEqual(3, first["total"])
        self.assertEqual(["B.NS", "C.NS"], [row["ticker"] for row in first["items"]])
        self.assertEqual(["A.NS"], [row["ticker"] for row in second["items"]])
        self.assertNotIn("evidence", first["items"][0])
        self.assertEqual(20, first["items"][0]["fundamentals"]["roe_pct"])
        self.assertEqual(100, second["items"][0]["evidence"]["price"]["live"])

    def test_search_is_literal_case_insensitive_and_never_sql(self):
        run = self.start_run([entry("A.NS", "100% Honest"), entry("B.NS", "UNDER_SCORE"), entry("C.NS", "Other")])
        self.save(run, "A.NS", 1, name="100% Honest")
        self.save(run, "B.NS", 2, name="UNDER_SCORE")
        self.save(run, "C.NS", 3, name="Other")
        self.assertEqual(["A.NS"], [row["ticker"] for row in self.store.query(search="%") ["items"]])
        self.assertEqual(["B.NS"], [row["ticker"] for row in self.store.query(search="_") ["items"]])
        self.assertEqual(1, self.store.query(search="honest")["total"])
        self.assertEqual(0, self.store.query(search="' OR 1=1 --")["total"])
        self.assertEqual(3, self.store.query()["total"])

    def test_runs_are_isolated_and_latest_run_does_not_replay_old_success(self):
        first = self.start_run([entry("A.NS")])
        self.save(first, "A.NS", 5, status="enriched", price=123)
        second = self.start_run([entry("A.NS"), entry("B.NS")])
        latest = self.store.query(include_evidence=True)
        self.assertEqual(second, latest["coverage"]["run_id"])
        self.assertEqual(2, latest["total"])
        self.assertTrue(all(row["evidence"] is None and row["status"] == "pending" for row in latest["items"]))
        old = self.store.query(run_id=first, include_evidence=True)
        self.assertEqual(1, old["total"])
        self.assertEqual(123, old["items"][0]["evidence"]["price"]["live"])
        self.save(second, "B.NS", 2, status="partial")
        self.assertEqual(1, self.store.query(status="partial")["total"])
        self.assertEqual(0, self.store.query(run_id=987654)["total"])
        # A separately opened store sees committed records after a restart.
        reloaded = IntelligenceStore(self.store.path)
        self.assertEqual(second, reloaded.coverage()["run_id"])
        self.assertEqual({"partial": 1, "pending": 1}, reloaded.coverage()["records_by_status"])

    def test_current_evidence_quality_does_not_mutate_historical_verdict(self):
        run = self.start_run([entry("A.NS")])
        old_evidence = {"ticker": "A.NS", "price": {"live": 100, "as_of": "2020-01-01"},
                        "technicals": {"last_bar": "2020-01-01", "rvol": 2, "price_vs_sma_pct": 3},
                        "market": {"live_session": True}}
        old_result = {"tracks": {"positional": {"verdict": "BUY", "confidence": 8}},
                      "evidence_quality": {"actionable": {"positional": True}}}
        self.store.save(run, old_evidence, old_result, "analyzed", 1)
        row = self.store.query(include_evidence=True)["items"][0]
        self.assertTrue(row["result"]["evidence_quality"]["actionable"]["positional"])
        self.assertFalse(row["current_evidence_quality"]["actionable"]["positional"])
        self.assertEqual("stale", row["current_evidence_quality"]["freshness"]["quote"]["status"])
        self.assertIn("historical", row["result_basis"])
        self.assertEqual(old_evidence, row["evidence"])

    def test_csv_exports_beyond_one_page_with_nulls_and_csv_escaping(self):
        names = [entry(f"T{index:04}.NS") for index in range(507)]
        names[0]["name"] = 'A "quoted", company\nwith a second line'
        run = self.start_run(names)
        self.save(run, "T0000.NS", 10, name=names[0]["name"], price=0)
        output = io.StringIO(newline="")
        exported = intelligence.write_csv(self.store, output)
        rows = list(csv.DictReader(io.StringIO(output.getvalue(), newline="")))
        self.assertEqual(507, exported)
        self.assertEqual(507, len(rows))
        self.assertEqual(507, len({row["ticker"] for row in rows}))
        self.assertEqual(names[0]["name"], rows[0]["name"])
        self.assertEqual("0", rows[0]["price"])
        self.assertEqual("", rows[1]["price"])
        self.assertEqual("", rows[1]["roe_pct"])
        self.assertEqual("", rows[1]["positional_confidence"])
        self.assertEqual({str(run)}, {row["run_id"] for row in rows})

    def test_csv_export_pins_run_even_if_new_scan_starts_between_pages(self):
        first = self.start_run([entry(f"T{index:04}.NS") for index in range(503)])
        original = self.store.query
        calls = []
        def query(*args, **kwargs):
            calls.append(kwargs["run_id"])
            if len(calls) == 2:
                self.start_run([entry("OTHER.NS")])
            return original(*args, **kwargs)
        with mock.patch.object(self.store, "query", side_effect=query):
            output = io.StringIO(newline="")
            self.assertEqual(503, intelligence.write_csv(self.store, output))
        self.assertEqual([first, first], calls)
        self.assertNotIn("OTHER.NS", output.getvalue())

    def test_cli_coverage_export_uses_existing_run_without_fetching(self):
        run = self.start_run([entry("A.NS")])
        path = Path(self.directory.name) / "all-stocks.csv"
        with mock.patch.object(intelligence, "IntelligenceStore", return_value=self.store), mock.patch.object(intelligence, "analyze_universe", side_effect=AssertionError("must not analyze")) as analyze, mock.patch("builtins.print"):
            intelligence.main(["--coverage", "--export", str(path)])
        analyze.assert_not_called()
        self.assertEqual(run, self.store.coverage()["run_id"])
        with path.open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual("A.NS", next(csv.DictReader(stream))["ticker"])

    def test_csv_actionability_requires_buy_track_eligibility_and_current_evidence(self):
        run = self.start_run([entry(ticker) for ticker in ("BUY.NS", "WATCH.NS", "BLOCKED.NS")])
        for ticker, verdict, allowed in (("BUY.NS", "BUY", True), ("WATCH.NS", "WATCH", True),
                                          ("BLOCKED.NS", "BUY", False)):
            self.store.save(run, {"ticker": ticker, "price": {"live": 100}},
                            {"tracks": {"positional": {"verdict": verdict, "actionable": allowed,
                                                       "confidence": 7}}}, "analyzed", 1)
        with mock.patch("evidence_quality.assess_evidence", return_value={"actionable": {"positional": True}}):
            output = io.StringIO(newline="")
            intelligence.write_csv(self.store, output)
        rows = {row["ticker"]: row for row in csv.DictReader(io.StringIO(output.getvalue()))}
        self.assertEqual("True", rows["BUY.NS"]["positional_actionable_now"])
        self.assertEqual("False", rows["WATCH.NS"]["positional_actionable_now"])
        self.assertEqual("False", rows["BLOCKED.NS"]["positional_actionable_now"])
        with mock.patch("evidence_quality.assess_evidence", return_value={"actionable": {"positional": False}}):
            output = io.StringIO(newline="")
            intelligence.write_csv(self.store, output)
        self.assertTrue(all(row["positional_actionable_now"] == "False" for row in csv.DictReader(io.StringIO(output.getvalue()))))


if __name__ == "__main__":
    unittest.main()
