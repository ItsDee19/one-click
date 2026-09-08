"""Quality-screen scope and cache regressions; all data providers are mocked."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import quality_screen as qs
import stock_universe


class QualityUniverseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "quality.json"
        self.progress = Path(self.tmp.name) / "progress.json"
        self.entries = [{"ticker": "MAIN.NS", "symbol": "MAIN", "name": "Main", "series": "BE"},
                        {"ticker": "SME.NS", "symbol": "SME", "name": "SME", "series": "SM"}]
        self.metadata = {"source": "nse_live", "scope": "NSE main board and SME",
                         "listed_count": 2, "degraded": False,
                         "exclusions": ["BSE-only listings"]}
        self.addCleanup(patch.stopall)
        patch.object(qs, "CACHE_FILE", str(self.cache)).start()
        patch.object(qs, "PROGRESS_FILE", str(self.progress)).start()
        patch.object(qs, "_now", return_value=datetime(2026, 9, 8, tzinfo=timezone.utc)).start()
        self.discovery = patch.object(qs, "fetch_nse_list", return_value=(self.entries, self.metadata)).start()
        self.liquidity = patch.object(qs, "_liquidity_stage", side_effect=lambda rows, _log: list(rows)).start()
        self.evaluate = patch.object(qs, "evaluate", return_value={"clears": False}).start()

    def test_completed_result_and_cache_preserve_discovery_scope_and_filters(self):
        self.metadata.update(source="curated_fallback", degraded=True)
        blob = qs.run(force=True)
        cached = qs.read_cache()
        self.assertEqual(blob["universe"], self.metadata)
        self.assertEqual(cached["universe"], self.metadata)
        self.assertEqual(blob["discovered"], 2)
        self.assertEqual(blob["listed"], 2)
        self.assertEqual(blob["examined"], 2)
        self.assertEqual(blob["screen_filters"]["minimum_average_daily_turnover_cr"], 0.5)
        self.assertTrue(any("degraded" in item for item in blob["caveats"]))

    def test_limit_cannot_poison_subsequent_full_screen_cache(self):
        limited = qs.run(limit=1)
        self.assertEqual(limited["listed"], 1)
        self.assertEqual(limited["discovered"], 2)
        self.assertTrue(limited["screen_filters"]["limited"])
        full = qs.run()
        self.assertEqual(full["listed"], 2)
        self.assertEqual(full["examined"], 2)
        self.assertEqual(self.discovery.call_count, 2)

    def test_full_cached_screen_is_reused_without_discovery(self):
        first = qs.run()
        self.discovery.reset_mock()
        second = qs.run()
        self.discovery.assert_not_called()
        self.assertEqual(first["universe"], second["universe"])

    def test_legacy_cache_is_labelled_and_cannot_skip_expanded_scan(self):
        qs.write_cache({"criteria": qs.DEFAULTS, "listed": 1, "examined": 1, "matches": []})
        visible = qs.read_cache()
        self.assertTrue(visible["universe"]["degraded"])
        self.assertIn("EQ-only", visible["universe"]["scope"])
        actual = qs.run()
        self.discovery.assert_called_once()
        self.assertEqual(actual["listed"], 2)
        self.assertEqual(actual["screen_version"], qs.SCREEN_VERSION)

    def test_legacy_eq_progress_does_not_exclude_sme_from_expanded_run(self):
        self.progress.write_text(json.dumps({"criteria": qs.DEFAULTS,
            "survivors": self.entries[:1], "done": ["MAIN"], "matches": [], "errors": 0}))
        blob = qs.run()
        self.liquidity.assert_called_once()
        self.assertEqual(self.evaluate.call_count, 2)
        self.assertEqual(blob["examined"], 2)

    def test_matching_scope_resumes_without_repeating_completed_work(self):
        qs._prepare_progress(qs.DEFAULTS, self.entries, lambda message: None)
        qs._save_survivors(qs.DEFAULTS, self.entries)
        qs._save_progress(qs.DEFAULTS, {"MAIN"}, [], 0)
        blob = qs.run()
        self.liquidity.assert_not_called()
        self.evaluate.assert_called_once()
        self.assertEqual(self.evaluate.call_args.args[0], "SME.NS")
        self.assertEqual(blob["examined"], 2)

    def test_limit_scope_change_discards_old_survivors(self):
        qs._prepare_progress(qs.DEFAULTS, self.entries[:1], lambda message: None)
        qs._save_survivors(qs.DEFAULTS, self.entries[:1])
        qs._save_progress(qs.DEFAULTS, {"MAIN"}, [], 0)
        blob = qs.run()
        self.liquidity.assert_called_once()
        self.assertEqual(blob["examined"], 2)

    def test_invalid_limit_rejected(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(limit=value):
                with self.assertRaises(ValueError):
                    qs.run(limit=value)


class DiscoveryWrapperTests(unittest.TestCase):
    def test_wrapper_includes_all_buckets_and_metadata(self):
        universe = {"large": [{"ticker": "Z.NS", "series": "EQ"}],
                    "unclassified": [{"ticker": "SME.NS", "series": "ST"},
                                     {"ticker": "MAIN.NS", "series": "BE"}]}
        metadata = {"source": "nse_mixed", "degraded": True}
        with patch.object(stock_universe, "load_exchange_universe", return_value=(universe, metadata)):
            rows, actual_meta = qs.fetch_nse_list(include_metadata=True)
            plain = qs.fetch_nse_list()
        self.assertEqual([row["ticker"] for row in rows], ["MAIN.NS", "SME.NS", "Z.NS"])
        self.assertEqual(actual_meta, metadata)
        self.assertEqual(plain, rows)


if __name__ == "__main__":
    unittest.main()
