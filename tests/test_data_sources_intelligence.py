"""Offline ingestion/refresh regressions using synthetic OHLCV observations."""

import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

import data_sources as ds
import evidence_quality
import market


NOW = datetime(2026, 9, 8, 10, 15, tzinfo=ds.IST)
PHASE = market.describe(NOW)


def daily(count=300, end="2026-09-08", base=100):
    index = pd.bdate_range(end=end, periods=count)
    closes = [base + i / 10 for i in range(count)]
    return pd.DataFrame({"Open": closes, "High": [v + 2 for v in closes],
                         "Low": [v - 2 for v in closes], "Close": closes,
                         "Volume": [1000] * count}, index=index)


def intraday(periods=12, start="2026-09-08 09:15:00", tz=ds.IST, base=130):
    index = pd.date_range(start, periods=periods, freq="5min", tz=tz)
    values = [base + i / 10 for i in range(periods)]
    return pd.DataFrame({"Open": values, "High": [v + 1 for v in values],
                         "Low": [v - 1 for v in values], "Close": values,
                         "Volume": [100] * periods}, index=index)


def profile():
    return {"info": {"currentPrice": 999, "regularMarketTime": NOW.timestamp(),
                     "targetMeanPrice": 160, "returnOnEquity": 0.15,
                     "trailingPE": 18, "debtToEquity": 150},
            "recommendations": {}, "news": {"total": 0},
            "metadata": {"source": "fixture", "sections": {"info": {
                "fetched_at": NOW.isoformat(), "stale": True}}}}


def quote(frame=None, session=None):
    return {"ticker": "TEST.NS", "name": "Test", "bucket": "unclassified",
            "sector": None, "frame": frame, "intraday_frame": session,
            "company_data": profile(), "regime": {"state": "risk_on"},
            "benchmark": ds._benchmark_block(frame)}


class DataSourceIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.patchers = [patch.object(market, "now_ist", return_value=NOW),
                         patch.object(market, "describe", return_value=PHASE),
                         patch.object(market, "regime", return_value={"state": "risk_on"})]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_malformed_rows_are_dropped_as_whole_bars(self):
        frame = daily(5)
        frame.iloc[1, frame.columns.get_loc("High")] = float("nan")
        frame["Volume"] = frame["Volume"].astype(object)
        frame.iloc[2, frame.columns.get_loc("Volume")] = "bad"
        valid = ds._valid_frame(frame)
        self.assertEqual(len(valid), 3)
        self.assertNotIn(frame.index[1], valid.index)
        self.assertNotIn(frame.index[2], valid.index)
        self.assertEqual(valid["Close"].tolist(), [100, 100.3, 100.4])

    def test_daily_bars_are_price_authority_over_cached_profile(self):
        frame = daily()
        ev = ds.build_evidence_live(quote(frame))
        self.assertEqual(ev["price"]["live"], 129.9)
        self.assertEqual(ev["price"]["as_of"], "2026-09-08")
        self.assertNotEqual(ev["price"]["live"], 999)
        self.assertEqual(ev["technicals"]["observations"], 300)
        self.assertEqual(ev["technicals"]["window_sessions"], 23)

    def test_technical_and_benchmark_return_windows_are_equal(self):
        frame = daily()
        ev = ds.build_evidence_live(quote(frame))
        expected = round((frame["Close"].iloc[-1] / frame["Close"].iloc[-23] - 1) * 100, 2)
        self.assertEqual(ev["technicals"]["window_return_pct"], expected)
        self.assertEqual(ev["relative"]["rel_window_return_pct"], 0)
        self.assertTrue(ev["relative"]["comparison_aligned"])

    def test_new_listing_shorter_window_is_not_compared_to_full_benchmark_window(self):
        item = quote(daily(8))
        item["benchmark"] = ds._benchmark_block(daily())
        ev = ds.build_evidence_live(item)
        self.assertIsNone(ev["relative"]["rel_window_return_pct"])
        self.assertIsNone(ev["technicals"]["price_vs_sma_pct"])
        self.assertFalse(ev["relative"]["comparison_aligned"])

    def test_stale_stock_session_is_not_compared_with_todays_index(self):
        item = quote(daily(end="2026-09-07"))
        item["benchmark"] = ds._benchmark_block(daily())
        ev = ds.build_evidence_live(item)
        self.assertIsNone(ev["relative"]["rel_day_change_pct"])
        self.assertIsNone(ev["relative"]["outperforming"])

    def test_utc_session_bars_are_converted_to_ist(self):
        frame = intraday()
        frame.index = frame.index.tz_convert("UTC")
        block = ds.build_intraday(frame, 129, 131.1, PHASE)
        self.assertTrue(block["available"])
        self.assertTrue(block["opening_range_complete"])
        self.assertIn("+05:30", block["last_bar"])
        self.assertEqual(block["session_volume"], 1200)

    def test_yesterdays_bars_do_not_become_today_intraday(self):
        frame = intraday(start="2026-09-07 09:15:00")
        block = ds.build_intraday(frame, 129, 131, PHASE)
        self.assertFalse(block["available"])
        ev = ds.build_evidence_live(quote(daily(), frame))
        self.assertEqual(ev["price"]["live"], 129.9)
        self.assertFalse(ev["intraday"]["available"])

    def test_late_bars_do_not_invent_opening_range(self):
        frame = intraday(periods=3, start="2026-09-08 10:00:00")
        block = ds.build_intraday(frame, 129, 131, PHASE)
        self.assertTrue(block["available"])
        self.assertFalse(block["opening_range_complete"])
        self.assertFalse(block["coverage_complete"])
        self.assertIsNone(block["above_opening_range"])

    def test_missing_session_bar_marks_vwap_partial(self):
        frame = intraday().drop(intraday().index[5])
        block = ds.build_intraday(frame, 129, 131, PHASE)
        self.assertFalse(block["coverage_complete"])
        self.assertTrue(block["opening_range_complete"])
        ev = ds.build_evidence_live(quote(daily(), frame))
        quality = evidence_quality.assess_evidence(ev, now=NOW)
        self.assertIn("session bars are incomplete; VWAP coverage is partial", quality["blockers"]["intraday"])

    def test_forming_opening_range_waits_until_fifteen_minutes_complete(self):
        with patch.object(market, "now_ist", return_value=NOW.replace(hour=9, minute=27)):
            block = ds.build_intraday(intraday(periods=3), 129, 131, PHASE)
        self.assertFalse(block["opening_range_complete"])
        self.assertIsNone(block["above_opening_range"])

    def test_current_intraday_replaces_current_daily_without_double_counting_volume(self):
        ev = ds.build_evidence_live(quote(daily(), intraday()))
        self.assertEqual(ev["price"]["live"], 131.1)
        self.assertEqual(ev["price"]["volume"], 1200)
        self.assertEqual(ev["technicals"]["observations"], 300)
        self.assertEqual(ev["price"]["prev_close"], 129.8)

    def test_current_session_appends_to_previous_daily_history(self):
        ev = ds.build_evidence_live(quote(daily(end="2026-09-07"), intraday()))
        self.assertEqual(ev["technicals"]["observations"], 301)
        self.assertEqual(ev["technicals"]["last_bar"], "2026-09-08")
        self.assertEqual(ev["price"]["prev_close"], 129.9)
        self.assertEqual(ev["price"]["volume"], 1200)

    def test_profile_fallback_keeps_original_provider_timestamp(self):
        item = quote()
        item["company_data"]["info"]["regularMarketTime"] = (NOW - timedelta(days=10)).timestamp()
        ev = ds.build_evidence_live(item)
        self.assertEqual(ev["price"]["live"], 999)
        self.assertIn("2026-08-29", ev["price"]["as_of"])
        self.assertIsNone(ev["technicals"]["price_vs_sma_pct"])
        self.assertFalse(evidence_quality.assess_evidence(ev, NOW)["actionable"]["positional"])

    def test_fundamental_metadata_units_and_staleness_survive_build(self):
        ev = ds.build_evidence_live(quote(daily()))
        self.assertEqual(ev["fundamentals"]["roe_pct"], 15)
        self.assertEqual(ev["fundamentals"]["debt_to_equity_ratio"], 1.5)
        self.assertEqual(ev["fundamentals"]["source"], "fixture")
        self.assertTrue(ev["fundamentals"]["stale"])
        self.assertIn("not a filing date", ev["fundamentals"]["as_of_basis"])

    def test_precomputed_regime_avoids_per_stock_regime_request(self):
        market.regime.reset_mock()
        ds.build_evidence_live(quote(daily()))
        market.regime.assert_not_called()

    def test_demo_loader_is_offline(self):
        market.regime.reset_mock()
        self.assertTrue(ds.load_demo_bundles())
        market.regime.assert_not_called()

    def test_refresh_preserves_order_and_rebuilds_prices(self):
        old = [{"ticker": "B.NS", "symbol": "B", "name": "B", "price": {"live": 999}},
               {"ticker": "A.NS", "symbol": "A", "name": "A", "price": {"live": 999}}]
        original = copy.deepcopy(old)
        frames = {"B.NS": daily(), "A.NS": daily(base=200), ds.BENCHMARK: daily()}
        with patch.object(ds, "download_frames", return_value=frames), \
             patch.object(ds, "fetch_intraday", return_value={}), \
             patch("company_data.fetch_company_data", return_value=profile()), \
             patch.object(ds, "fired_strategies", return_value={"intraday": {}, "swing": {}}):
            new = ds.refresh_evidence(old)
        self.assertEqual([bundle["symbol"] for bundle in new], ["B", "A"])
        self.assertEqual([bundle["price"]["live"] for bundle in new], [129.9, 229.9])
        self.assertEqual(old, original)

    def test_refresh_failure_does_not_resurrect_cached_price(self):
        old = [{"ticker": "TEST.NS", "symbol": "TEST", "price": {"live": 123}}]
        with patch.object(ds, "download_frames", return_value={}), \
             patch.object(ds, "fetch_intraday", return_value={}), \
             patch("company_data.fetch_company_data", return_value=profile()):
            new = ds.refresh_evidence(old)
        self.assertEqual(new[0]["refresh_status"], "unavailable")
        self.assertIsNone(new[0]["price"]["live"])
        self.assertIsNone(new[0]["price"]["as_of"])
        self.assertIn("price.live", new[0]["data_gaps"])

    def test_quote_screen_compares_matching_sessions_and_true_sector_median(self):
        first, second = daily(), daily()
        first.iloc[-1, first.columns.get_loc("Close")] = first["Close"].iloc[-2] * 1.01
        second.iloc[-1, second.columns.get_loc("Close")] = second["Close"].iloc[-2] * 1.03
        universe = {"unclassified": [{"ticker": "A.NS", "name": "A", "sector": "IT"},
                                     {"ticker": "B.NS", "name": "B", "sector": "IT"},
                                     {"ticker": "C.NS", "name": "C", "sector": "IT"}]}
        with patch.object(ds, "download_frames", return_value={"A.NS": first, "B.NS": second,
                         "C.NS": daily(end="2026-09-07"), ds.BENCHMARK: daily()}):
            quotes, _ = ds.fetch_quotes(universe)
        rows = quotes["unclassified"]
        self.assertEqual(rows[0]["sector_median_pct"], 2)
        self.assertIsNone(rows[2]["sector_rel_pct"])
        self.assertIsNone(rows[2]["rel_day_change_pct"])


if __name__ == "__main__":
    unittest.main()
