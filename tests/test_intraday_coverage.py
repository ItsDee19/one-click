"""Intraday snapshots are dated and rechecked at the end of long scans."""

from datetime import date, datetime
import unittest
from unittest import mock

import pandas as pd

import intraday_desk as desk
import market


def daily_frame(dates, closes):
    return pd.DataFrame({"Close": closes}, index=pd.to_datetime(dates))


class PreviousCloseTests(unittest.TestCase):
    def test_daily_response_ending_yesterday_uses_last_close(self):
        quote = {"frame": daily_frame(["2026-09-04", "2026-09-07"], [90, 100])}
        self.assertEqual(100, desk._prev_close(quote, date(2026, 9, 8)))

    def test_forming_today_candle_and_future_rows_are_excluded(self):
        quote = {"frame": daily_frame(["2026-09-07", "2026-09-08", "2026-09-04", "2026-09-09"],
                                       [100, 110, 90, 120])}
        self.assertEqual(100, desk._prev_close(quote, date(2026, 9, 8)))

    def test_single_prior_close_is_valid_and_default_is_current_session_date(self):
        quote = {"frame": daily_frame(["2026-09-07"], [100])}
        with mock.patch.object(market, "now_ist", return_value=datetime(2026, 9, 8, 10, tzinfo=market.IST)):
            self.assertEqual(100, desk._prev_close(quote))
        self.assertIsNone(desk._prev_close({"frame": daily_frame(["2026-09-08"], [110])}, date(2026, 9, 8)))


class IntradayPublicationTests(unittest.TestCase):
    def scan_between(self, observed, processed, completed):
        index = pd.date_range(end=observed, periods=4, freq="5min")
        frame = pd.DataFrame({"Open": [101] * 4, "High": [103] * 4, "Low": [100] * 4,
                              "Close": [102] * 4, "Volume": [1000] * 4}, index=index)
        universe = {"large": [{"ticker": "TEST.NS", "name": "Test", "sector": "Technology"}]}
        quotes = {"large": [{"ticker": "TEST.NS", "rvol": 2,
                              "frame": daily_frame(["2026-09-04", "2026-09-07"], [90, 100])}]}
        setup = {"why": "fixture breakout", "entry": 102, "stop": 100, "target": 106,
                 "reward_risk": 2, "risk_per_share": 2}
        with mock.patch.object(market, "is_trading_day", return_value={"trading": True}), \
                mock.patch.object(market, "now_ist", side_effect=[processed, completed]), \
                mock.patch.object(desk.data_sources, "fetch_quotes", return_value=(quotes, {})), \
                mock.patch.object(desk.data_sources, "fetch_intraday", return_value={"TEST.NS": frame}), \
                mock.patch.object(desk, "load_record", return_value=({}, {})), \
                mock.patch.object(desk, "_swing_payload", return_value={}), \
                mock.patch.object(desk.strategies, "evaluate", return_value={"fixture": {"setup": setup}}):
            return desk.scan(universe=universe)

    def test_scan_crossing_market_close_uses_final_phase(self):
        result = self.scan_between(datetime(2026, 9, 8, 15, 25, tzinfo=market.IST),
                                   datetime(2026, 9, 8, 15, 29, tzinfo=market.IST),
                                   datetime(2026, 9, 8, 15, 32, tzinfo=market.IST))
        self.assertEqual(market.POST, result["phase"])
        self.assertFalse(result["tradeable"])
        self.assertEqual(1, len(result["picks"]))
        self.assertEqual(1, result["picks"][0]["gap_pct"])
        self.assertIn("15:25:00", result["picks"][0]["as_of"])

    def test_snapshot_that_expires_during_scan_is_not_published(self):
        result = self.scan_between(datetime(2026, 9, 8, 11, 0, tzinfo=market.IST),
                                   datetime(2026, 9, 8, 11, 5, tzinfo=market.IST),
                                   datetime(2026, 9, 8, 11, 21, tzinfo=market.IST))
        self.assertEqual([], result["picks"])
        self.assertEqual(0, result["coverage"]["usable_sessions"])
        self.assertEqual(1, result["coverage"]["stale_sessions"])
        self.assertEqual(0, result["coverage"]["missing_sessions"])


if __name__ == "__main__":
    unittest.main()
