"""Intraday snapshots are dated and rechecked at the end of long scans."""

from datetime import date, datetime, timedelta
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
        index = pd.date_range(start=observed.replace(hour=9, minute=15), end=observed, freq="5min")
        frame = pd.DataFrame({"Open": [101] * len(index), "High": [103] * len(index),
                              "Low": [100] * len(index), "Close": [102] * len(index),
                              "Volume": [1000] * len(index)}, index=index)
        universe = {"large": [{"ticker": "TEST.NS", "name": "Test", "sector": "Technology",
                                "segment": "main_board", "series": "EQ"}]}
        reference = {"prev_close": 100, "avg_volume": 30000, "avg_turnover": 20_000_000,
                     "reference_date": "2026-09-07", "reference_sessions": 20}
        permission = {"symbols": {"TEST.NS": {"as_of": processed.date().isoformat(),
                                               "source": "fixture broker", "long": True}}}
        def signals(session):
            stamp = session["timestamps"][-1]
            return {"ORB breakout": {"strategy": "ORB breakout", "why": "fixture breakout",
                    "entry": 102, "stop": 100, "target": 106, "bar": session["n"] - 1,
                    "direction": "long", "rvol": 2, "session_validated": True,
                    "signal_at": stamp.isoformat(), "confirmed_at": (stamp + timedelta(minutes=5)).isoformat(),
                    "reward_risk": 2, "risk_per_share": 2}}
        with mock.patch.object(market, "is_trading_day", return_value={"trading": True}), \
                mock.patch.object(market, "now_ist", side_effect=[processed, processed, completed]), \
                mock.patch.object(desk.intraday_data, "load_references", return_value={"TEST.NS": reference}), \
                mock.patch.object(desk.data_sources, "fetch_intraday", return_value={"TEST.NS": frame}), \
                mock.patch.object(desk, "load_record", return_value=({}, {})), \
                mock.patch.object(desk, "load_eligibility", return_value=permission), \
                mock.patch.object(desk.intraday_validation, "assess_record", return_value={
                    "status": "historically_validated", "qualified": True, "reasons": [], "metrics": {}}), \
                mock.patch.object(desk, "_swing_payload", return_value={}), \
                mock.patch.object(desk.strategies, "signals", side_effect=signals):
            return desk.scan(universe=universe)

    def test_scan_crossing_market_close_uses_final_phase(self):
        result = self.scan_between(datetime(2026, 9, 8, 15, 25, tzinfo=market.IST),
                                   datetime(2026, 9, 8, 15, 29, tzinfo=market.IST),
                                   datetime(2026, 9, 8, 15, 32, tzinfo=market.IST))
        self.assertEqual(market.POST, result["phase"])
        self.assertFalse(result["tradeable"])
        self.assertEqual([], result["picks"])
        self.assertEqual(1, result["history"][0]["gap_pct"])
        self.assertIn("15:25:00", result["history"][0]["as_of"])
        self.assertEqual("expired", result["history"][0]["state"])

    def test_fresh_confirmed_signal_publishes_direction_and_actual_signal_times(self):
        observed = datetime(2026, 9, 8, 9, 30, tzinfo=market.IST)
        now = observed + timedelta(minutes=5)
        result = self.scan_between(observed, now, now)
        self.assertEqual(1, len(result["picks"]))
        item = result["picks"][0]
        self.assertEqual(item["action"], "BUY")
        self.assertEqual(item["direction"], "long")
        self.assertEqual(item["signal_at"], observed.isoformat())
        self.assertEqual(item["confirmed_at"], now.isoformat())
        self.assertEqual(item["state"], "entry_ready")

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
