from datetime import date
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import intraday_data as data
import data_sources
import strategies
import intraday_desk
import strategy_edge


class ReferenceTests(unittest.TestCase):
    def test_provider_duplicate_candles_reach_strict_session_rejection(self):
        index = pd.date_range("2026-09-08 09:15", periods=4, freq="5min")
        frame = pd.DataFrame({"Open": [100] * 4, "High": [101] * 4, "Low": [99] * 4,
                              "Close": [100] * 4, "Volume": [1000] * 4}, index=index)
        duplicated = pd.concat([frame, frame.tail(1)])
        raw = data_sources._frame_for(duplicated, "T.NS", True, preserve_rows=True)
        self.assertEqual(5, len(raw))
        self.assertIsNone(strategies.session(intraday_desk._bars_from(raw), avg_volume=10000))
        self.assertIsNone(data_sources._session_frame(raw))

    def frame(self):
        days = pd.bdate_range(end="2026-09-08", periods=22)
        return pd.DataFrame({"Close": [100] * 21 + [999], "Volume": [1000] * 21 + [99999999]}, index=days)

    def test_reference_excludes_current_and_future_daily_volume(self):
        result = data.daily_reference(self.frame(), date(2026, 9, 8))
        self.assertEqual(1000, result["avg_volume"])
        self.assertEqual(100, result["prev_close"])
        self.assertEqual("2026-09-07", result["reference_date"])

    def test_short_history_is_not_liquid_by_assumption(self):
        result = data.daily_reference(self.frame().tail(5), date(2026, 9, 8))
        self.assertIsNone(result["avg_volume"])
        self.assertIsNone(result["avg_turnover"])

    def test_daily_cache_reuses_references_and_retries_missing_symbols(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(data.data_sources, "download_frames", return_value={"T.NS": self.frame()}) as fetch:
            data.load_references(["T.NS", "M.NS"], date(2026, 9, 8), cache_dir=directory)
            data.load_references(["T.NS", "M.NS"], date(2026, 9, 8), cache_dir=directory)
            self.assertEqual(["M.NS"], fetch.call_args.args[0])
            self.assertEqual("3mo", fetch.call_args.kwargs["period"])
            data.load_references(["T.NS"], date(2026, 9, 9), cache_dir=directory)
            self.assertEqual(3, fetch.call_count)


class StrategyVoteTests(unittest.TestCase):
    def test_legacy_profitable_record_cannot_vote(self):
        import intraday_desk
        legacy = {"strategies": {"ORB breakout": {"trades": 10000, "enough": True, "expectancy_r": 2}}}
        with patch.object(intraday_desk, "load_record", return_value=(legacy["strategies"], legacy)):
            result = strategy_edge.intraday_edge({"ORB breakout": "old signal"})
        self.assertEqual(0, result["for_points"])
        self.assertEqual(["ORB breakout"], result["untrusted"])

    def test_validated_short_supports_downside(self):
        record = {"VWAP rejection": {"enough": True, "direction": "short", "trades": 100,
                                     "expectancy_r": .5, "win_rate_pct": 55}}
        with patch.object(strategy_edge, "intraday_record", return_value=record):
            result = strategy_edge.intraday_edge({"VWAP rejection": "current short"})
        self.assertEqual(0, result["for_points"])
        self.assertEqual(12, result["against_points"])


if __name__ == "__main__":
    unittest.main()
