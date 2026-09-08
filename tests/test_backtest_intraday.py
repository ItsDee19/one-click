"""Causal preparation, archive provenance and net replay integration, fully offline."""

import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import backtest_intraday as replay
import intraday_validation as validation
import strategies

NOW = datetime(2026, 9, 8, 18, tzinfo=strategies.IST)


def archive(days=28):
    records, dates = [], []
    day = datetime(2026, 6, 1, 9, 15, tzinfo=strategies.IST)
    while len(dates) < days:
        if day.weekday() < 5:
            index = len(dates)
            dates.append(day.date().isoformat())
            for bar in range(75):
                price, high, low, close, volume = 100, 101, 99, 100, 1000
                if index >= 20:
                    volume = 2000
                    if bar == 3:
                        price, high, low, close = 100, 103, 100, 102
                    elif bar == 4:
                        price, high, low, close = 102, 108, 101, 107
                    elif bar > 4:
                        price, high, low, close = 107, 108, 106, 107
                records.append({"timestamp": (day + timedelta(minutes=bar * 5)).isoformat(),
                                "open": price, "high": high, "low": low, "close": close, "volume": volume})
        day += timedelta(days=1)
    return records, dates


class PreparationTests(unittest.TestCase):
    def test_twenty_prior_complete_sessions_and_prior_only_volume(self):
        bars, dates = archive(22)
        diagnostics = {}
        sessions = replay.prepare_sessions(bars, diagnostics, now=NOW)
        self.assertEqual(20, diagnostics["warmup_sessions"])
        self.assertEqual([dates[20], dates[21]], [day for day, session in sessions])
        self.assertAlmostEqual(2, sessions[0][1]["rvol_by_bar"][3])
        # Current session's high final volume cannot enter its own reference mean.
        changed = [dict(row, volume=1_000_000) if row["timestamp"][:10] == dates[-1] else row for row in bars]
        earlier = replay.prepare_sessions(changed, now=NOW)[0][1]
        self.assertEqual(sessions[0][1]["rvol_by_bar"], earlier["rvol_by_bar"])

    def test_partial_and_gapped_days_never_fill_warmup(self):
        bars, dates = archive(22)
        bars = [row for row in bars if row["timestamp"] != dates[0] + "T09:20:00+05:30"]
        info = {}
        sessions = replay.prepare_sessions(bars, info, now=NOW)
        self.assertEqual([dates[-1]], [day for day, session in sessions])
        self.assertIn(dates[0], info["invalid_or_partial_dates"])

    def test_naive_archive_timestamp_is_rejected_instead_of_guessing_zone(self):
        bars, _ = archive(1)
        bars[0]["timestamp"] = bars[0]["timestamp"][:-6]
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            replay.prepare_sessions(bars, now=NOW)

    def test_global_split_is_chronological_and_embargoes_an_entire_date(self):
        _, dates = archive(30)
        sample, holdout = replay.chronological_split(dates + dates[10:])
        self.assertLess(holdout["development_end"], holdout["embargo_dates"][0])
        self.assertLess(holdout["embargo_dates"][0], holdout["start"])
        self.assertEqual(30, len(sample))
        self.assertFalse(holdout["untouched"])


class ReplayIntegrationTests(unittest.TestCase):
    def test_json_archive_run_retains_net_ledger_failures_and_research_status(self):
        bars, dates = archive()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TEST.NS.json").write_text(json.dumps({"bars": bars}), encoding="utf-8")
            output = root / "output.txt"
            with mock.patch.object(replay.data_sources, "load_full_exchange", side_effect=AssertionError("offline only")):
                blob = replay.run(symbols=["TEST", "MISSING"], data_dir=root, output=output, now=NOW, log=lambda text: None)
            self.assertEqual(2, blob["coverage"]["requested"])
            self.assertEqual(1, blob["coverage"]["successful"])
            self.assertEqual(1, blob["coverage"]["failed_or_insufficient"])
            self.assertEqual(8, blob["stock_sessions"])
            self.assertEqual(dates[0], blob["data"]["first_date"])
            self.assertTrue(blob["ledger"])
            self.assertTrue(output.exists())
            json.loads(output.read_text(encoding="utf-8"))
            for row in blob["ledger"]:
                self.assertIn(row["direction"], ("long", "short"))
                self.assertGreaterEqual(row["entry_at"], row["confirmed_at"])
                self.assertGreater(row["costs_r"], 0)
                self.assertAlmostEqual(row["net_r"], row["gross_r"] - row["costs_r"], places=5)
                self.assertLess(row["stress_net_r"], row["net_r"])
            result = validation.assess_record(blob, "ORB breakout", "long", now=NOW)
            self.assertEqual("research_only", result["status"])
            self.assertFalse(result["qualified"])
            self.assertFalse(blob["ranking"]["tested"])
            self.assertGreater(blob["ranking"]["diagnostics"]["snapshot_count"], 0)
            self.assertNotIn("portfolio_expectancy", blob["ranking"])

    def test_csv_and_json_schema_are_equivalent(self):
        bars, _ = archive(21)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TEST.NS.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(bars[0]))
                writer.writeheader()
                writer.writerows(bars)
            info = {}
            sessions = replay.sessions_for("TEST.NS", data_dir=directory, diagnostics=info, now=NOW)
            self.assertEqual(1, len(sessions))
            self.assertEqual(64, len(info["sha256"]))
            self.assertEqual("ok", info["status"])

    def test_default_requests_full_discovered_exchange_and_retains_missing_symbols(self):
        universe = {"large": [{"ticker": "A.NS"}], "unclassified": [{"ticker": "SME.NS"}]}
        def unavailable(ticker, diagnostics=None, **kwargs):
            diagnostics.update(ticker=ticker, status="data_unavailable")
            return []
        with mock.patch.object(replay.data_sources, "load_full_exchange", return_value=universe) as discover, \
                mock.patch.object(replay, "sessions_for", side_effect=unavailable):
            blob = replay.run(output=None, now=NOW, log=lambda text: None)
        discover.assert_called_once()
        self.assertEqual(2, blob["symbols"])
        self.assertEqual({"A.NS", "SME.NS"}, {item["ticker"] for item in blob["coverage"]["symbols"]})
        self.assertEqual(2, blob["coverage"]["failed_or_insufficient"])

    def test_duplicate_archive_ticker_files_are_not_silently_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("TEST.json", "TEST.NS.csv"):
                (Path(directory) / name).write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                replay._archive_files(directory)


if __name__ == "__main__":
    unittest.main()
