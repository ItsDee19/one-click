"""Offline regressions for exchange discovery, scope, and cache integrity."""

from datetime import datetime, timedelta, timezone
import builtins
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import stock_universe as su


HEADER = "SYMBOL, NAME OF COMPANY, SERIES, ISIN NUMBER\n"


class StockUniverseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "universe-cache.json"
        self.curated = Path(self.tmp.name) / "curated.json"
        self.curated.write_text(json.dumps({
            "large": [{"ticker": "KNOWN.NS", "name": "Old name", "sector": "Industrials"}],
            "small": ["OLD.NS"],
        }), encoding="utf-8")
        self.now = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
        self.clock = patch.object(su, "_now", return_value=self.now).start()
        self.addCleanup(patch.stopall)
        patch.object(su, "MIN_SOURCE_ROWS", {"main_board": 1, "sme": 1}).start()

    def load(self, **kwargs):
        return su.load_exchange_universe(cache_path=self.cache, curated_path=self.curated, **kwargs)

    @staticmethod
    def feed(segment, _log):
        content = (HEADER + "KNOWN,Known current company,EQ,INE001A01010\n"
                   + "ILLIQUID,Unclassified company,BE,INE002A01011\n"
                   if segment == "main_board" else
                   HEADER + "STARTUP,SME Company,SM,INE003A01012\n")
        return su._parse_equity_csv(content, segment)

    def seed_cache(self):
        with patch.object(su, "_fetch_segment", side_effect=self.feed):
            return self.load()

    def test_all_series_retained_without_liquidity_or_cap_inference(self):
        universe, meta = self.seed_cache()
        self.assertEqual([row["ticker"] for row in universe["large"]], ["KNOWN.NS"])
        self.assertEqual(universe["large"][0]["name"], "Known current company")
        self.assertEqual(universe["large"][0]["sector"], "Industrials")
        self.assertEqual([row["ticker"] for row in universe["unclassified"]],
                         ["ILLIQUID.NS", "STARTUP.NS"])
        self.assertFalse(universe["small"])
        self.assertEqual(meta["listed_count"], 3)
        self.assertEqual(meta["filters_applied"], [])
        self.assertTrue(meta["complete_for_declared_scope"])
        self.assertFalse(meta["degraded"])
        self.assertTrue(any("BSE-only" in exclusion for exclusion in meta["exclusions"]))

    def test_import_has_no_http_or_quote_dependency(self):
        original_import = builtins.__import__
        def offline_import(name, *args, **kwargs):
            if name in {"requests", "yfinance"}:
                raise AssertionError("network library imported at module load")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=offline_import):
            source = (ROOT / "stock_universe.py").read_text(encoding="utf-8")
            exec(compile(source, str(ROOT / "stock_universe.py"), "exec"),
                 {"__file__": str(ROOT / "stock_universe.py"), "__name__": "offline_universe_test"})

    def test_fresh_cache_makes_no_network_calls_and_retains_timestamps(self):
        original, first_meta = self.seed_cache()
        self.clock.return_value = self.now + timedelta(hours=2)
        with patch.object(su, "_fetch_segment", side_effect=AssertionError("network called")) as fetch:
            universe, meta = self.load()
        fetch.assert_not_called()
        self.assertEqual(universe, original)
        self.assertEqual(meta["source"], "nse_cache")
        self.assertEqual(meta["fetched_at"], first_meta["fetched_at"])
        self.assertEqual(meta["cache_age_hours"], 2)
        self.assertFalse(meta["stale"])

    def test_default_cache_resolves_db_dir_at_call_time(self):
        data_dir = Path(self.tmp.name) / "persistent-data"
        with patch.dict(su.os.environ, {"DB_DIR": str(data_dir)}), \
                patch.object(su, "_fetch_segment", side_effect=self.feed):
            su.load_exchange_universe(curated_path=self.curated)
        self.assertTrue((data_dir / ".stock_universe_cache.json").exists())
        with patch.dict(su.os.environ, {"DB_DIR": str(data_dir)}), \
                patch.object(su, "_fetch_segment", side_effect=AssertionError("network called")) as fetch:
            _, meta = su.load_exchange_universe(curated_path=self.curated)
        fetch.assert_not_called()
        self.assertEqual(meta["source"], "nse_cache")

    def test_failed_refresh_retains_complete_stale_cache(self):
        original, first = self.seed_cache()
        cache_before = self.cache.read_bytes()
        self.clock.return_value = self.now + timedelta(hours=49)
        logs = []
        with patch.object(su, "_fetch_segment", side_effect=TimeoutError):
            universe, meta = self.load(log=logs.append)
        self.assertEqual(universe, original)
        self.assertEqual(meta["fetched_at"], first["fetched_at"])
        self.assertEqual(meta["cache_age_hours"], 49)
        self.assertTrue(meta["stale"])
        self.assertTrue(meta["degraded"])
        self.assertFalse(meta["complete_for_declared_scope"])
        self.assertEqual(self.cache.read_bytes(), cache_before)
        self.assertTrue(any("DEGRADED" in item for item in logs))

    def test_missing_sme_is_explicit_and_does_not_erase_main_board(self):
        def main_only(segment, log):
            if segment == "sme":
                raise TimeoutError()
            return self.feed(segment, log)
        with patch.object(su, "_fetch_segment", side_effect=main_only):
            universe, meta = self.load()
        self.assertEqual(meta["listed_count"], 2)
        self.assertTrue(universe["unclassified"])
        self.assertTrue(meta["degraded"])
        self.assertEqual(meta["segment_coverage"]["sme"]["source"], "unavailable")
        self.assertTrue(any("sme" in item for item in meta["exclusions"]))

    def test_segment_failure_keeps_its_old_timestamp_while_other_refreshes(self):
        self.seed_cache()
        self.clock.return_value = self.now + timedelta(days=2)
        def partial(segment, log):
            if segment == "sme":
                raise TimeoutError()
            return self.feed(segment, log)
        with patch.object(su, "_fetch_segment", side_effect=partial):
            _, meta = self.load()
        self.assertEqual(meta["segment_coverage"]["main_board"]["source"], "live")
        self.assertEqual(meta["segment_coverage"]["sme"]["source"], "stale_cache")
        self.assertEqual(meta["fetched_at"], self.now.isoformat())
        cached = json.loads(self.cache.read_text())
        self.assertEqual(cached["segments"]["sme"]["fetched_at"], self.now.isoformat())

    def test_no_exchange_data_uses_truthful_curated_fallback(self):
        with patch.object(su, "_fetch_segment", side_effect=ConnectionError):
            universe, meta = self.load()
        self.assertEqual(meta["source"], "curated_fallback")
        self.assertEqual(meta["listed_count"], 2)
        self.assertTrue(meta["degraded"])
        self.assertFalse(meta["complete_for_declared_scope"])
        self.assertIsNone(meta["fetched_at"])
        self.assertEqual(universe["small"][0]["ticker"], "OLD.NS")
        self.assertFalse(self.cache.exists())

    def test_corrupt_cache_is_not_trusted(self):
        self.cache.write_text("{not-json", encoding="utf-8")
        with patch.object(su, "_fetch_segment", side_effect=ConnectionError):
            _, meta = self.load()
        self.assertEqual(meta["source"], "curated_fallback")
        self.assertTrue(any("cache unavailable" in error for error in meta["errors"]))

    def test_malformed_cache_rows_and_future_timestamp_trigger_refresh(self):
        for mutation in ("ticker", "future", "rows", "validation"):
            with self.subTest(mutation=mutation):
                self.cache.unlink(missing_ok=True)
                self.seed_cache()
                raw = json.loads(self.cache.read_text())
                block = raw["segments"]["main_board"]
                if mutation == "ticker":
                    block["rows"][0]["ticker"] = "WRONG.NS"
                elif mutation == "future":
                    block["fetched_at"] = (self.now + timedelta(days=1)).isoformat()
                elif mutation == "rows":
                    block["rows"] = "broken"
                else:
                    block["validation"] = {"rejected_rows": "zero"}
                self.cache.write_text(json.dumps(raw))
                with patch.object(su, "_fetch_segment", side_effect=self.feed) as fetch:
                    _, meta = self.load()
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(fetch.call_args.args[0], "main_board")
                self.assertEqual(meta["listed_count"], 3)

    def test_suspicious_list_shrink_keeps_prior_segment(self):
        self.seed_cache()
        self.clock.return_value = self.now + timedelta(days=2)
        def truncated(segment, log):
            rows, validation = self.feed(segment, log)
            return (rows[:1], validation) if segment == "main_board" else (rows, validation)
        with patch.object(su, "_fetch_segment", side_effect=truncated):
            _, meta = self.load()
        self.assertEqual(meta["listed_count"], 3)
        self.assertEqual(meta["segment_coverage"]["main_board"]["source"], "stale_cache")
        self.assertTrue(any("shrank" in error for error in meta["errors"]))

    def test_duplicate_symbols_have_deterministic_identity_and_series(self):
        records = ["M&M,Mahindra main,EQ,INE001A01010\n",
                   "M&M,Mahindra alternate,BE,INE001A01010\n",
                   "M&M,Mahindra main,EQ,INE001A01010\n",
                   "360ONE,Numeric symbol,BZ,INE002A01011\n"]
        first, stats = su._parse_equity_csv("\ufeff" + HEADER + "".join(records), "main_board")
        second, _ = su._parse_equity_csv(HEADER + "".join(reversed(records)), "main_board")
        self.assertEqual(first, second)
        self.assertEqual(stats["duplicate_rows"], 2)
        self.assertEqual(first[1]["name"], "Mahindra main")
        self.assertEqual(first[1]["series_codes"], ["BE", "EQ"])
        self.assertEqual(first[0]["ticker"], "360ONE.NS")

    def test_cross_segment_duplicates_do_not_inflate_coverage(self):
        def both(segment, _log):
            return su._parse_equity_csv(HEADER + "SAME,Same issuer,EQ,INE001A01010\n", segment)
        with patch.object(su, "_fetch_segment", side_effect=both):
            universe, meta = self.load()
        self.assertEqual(meta["listed_count"], 1)
        self.assertEqual(meta["cross_segment_duplicates"], 1)
        self.assertEqual(universe["unclassified"][0]["segment"], "main_board")

    def test_malformed_csv_never_overwrites_valid_source(self):
        for content in ("<html>Access denied</html>", "SYMBOL,NAME OF COMPANY\nA,Acme\n",
                        HEADER, HEADER + "INVALID SYMBOL,Acme,EQ,INE001A01010\n",
                        HEADER + "A,Acme,EQ,broken\n",
                        "SYMBOL,NAME OF COMPANY,SERIES,SERIES\nA,Acme,EQ,EQ\n"):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    su._parse_equity_csv(content, "main_board")

    def test_rejected_rows_mark_partial_coverage(self):
        def partly_malformed(segment, log):
            if segment == "sme":
                return self.feed(segment, log)
            return su._parse_equity_csv(HEADER + "KNOWN,Acme,EQ,INE001A01010\n"
                                       + "BAD SYMBOL,Invalid,EQ,INE002A01011\n", segment)
        with patch.object(su, "_fetch_segment", side_effect=partly_malformed):
            _, meta = self.load()
        self.assertTrue(meta["degraded"])
        self.assertFalse(meta["complete_for_declared_scope"])
        self.assertEqual(meta["segment_coverage"]["main_board"]["validation"]["rejected_rows"], 1)

    def test_unwritable_cache_does_not_discard_live_discovery(self):
        with patch.object(su, "_fetch_segment", side_effect=self.feed), \
                patch.object(su, "_write_cache", side_effect=PermissionError):
            _, meta = self.load()
        self.assertEqual(meta["listed_count"], 3)
        self.assertTrue(meta["complete_for_declared_scope"])
        self.assertTrue(any("could not be saved" in error for error in meta["errors"]))


if __name__ == "__main__":
    unittest.main()
