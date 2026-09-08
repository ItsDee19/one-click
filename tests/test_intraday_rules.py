"""Causal/session/fill regressions for the shared intraday rule engine."""

import copy
from datetime import datetime, timedelta
import unittest
from unittest import mock

import strategies as rules


START = datetime(2026, 9, 7, 9, 15, tzinfo=rules.IST)


def bars(n=6, volume=100):
    return [{"timestamp": (START + timedelta(minutes=5 * i)).isoformat(),
             "open": 100, "high": 100.5, "low": 99.5,
             "close": 100, "volume": volume} for i in range(n)]


def breakout_bars(n=6, volume=100):
    rows = bars(n, volume)
    for i, close in enumerate((100, 100.2, 100.4, 102)):
        rows[i].update(close=close, high=max(101, close + 0.1))
    for row in rows[4:]:
        row.update(open=102, close=102.1, high=102.5, low=101.5)
    return rows


def setup(direction="long", bar=3):
    if direction == "long":
        return rules.Setup("fixture", bar, 100, 99, 102, direction, "fixture")
    return rules.Setup("fixture", bar, 100, 101, 98, direction, "fixture")


class SessionValidationTests(unittest.TestCase):
    def test_missing_session_container_is_invalid(self):
        self.assertIsNone(rules.session(None))

    def test_timestamp_free_requires_explicit_research_mode(self):
        rows = bars()
        for row in rows:
            row.pop("timestamp")
        self.assertIsNone(rules.session(rows))
        session = rules.session(rows, research_compat=True)
        self.assertFalse(session["validated"])
        self.assertFalse(session["complete"])

    def test_rejects_missing_duplicate_unsorted_late_and_gapped_timestamps(self):
        for change in ("missing", "duplicate", "unsorted", "late", "gap"):
            with self.subTest(change=change):
                rows = bars()
                if change == "missing":
                    rows[2].pop("timestamp")
                elif change == "duplicate":
                    rows[2]["timestamp"] = rows[1]["timestamp"]
                elif change == "unsorted":
                    rows[1], rows[2] = rows[2], rows[1]
                elif change == "late":
                    rows = rows[1:]
                else:
                    rows.pop(2)
                self.assertIsNone(rules.session(rows))

    def test_rejects_bad_ohlcv_instead_of_compressing_bars(self):
        for key, value in (("open", 0), ("high", float("inf")),
                           ("close", float("nan")), ("volume", -1),
                           ("volume", None), ("volume", True), ("volume", 10 ** 1000),
                           ("low", 101), ("high", 99)):
            with self.subTest(key=key, value=value):
                rows = bars()
                rows[2][key] = value
                self.assertIsNone(rules.session(rows))

    def test_zero_volume_is_valid_but_missing_volume_is_not(self):
        session = rules.session(bars(volume=0), avg_volume=7500)
        self.assertEqual(session["rvol_by_bar"], [0] * 6)

    def test_finite_inputs_that_overflow_aggregates_are_invalid(self):
        self.assertIsNone(rules.session(bars(volume=1e308)))

    def test_forming_bar_is_excluded(self):
        rows = breakout_bars(5)
        self.assertIsNone(rules.session(rows, now=START + timedelta(minutes=19)))
        session = rules.session(rows, now=START + timedelta(minutes=20), avg_volume=3000)
        self.assertEqual(session["n"], 4)
        signal = rules.signals(session)["ORB breakout"]
        self.assertEqual(signal["signal_at"], (START + timedelta(minutes=15)).isoformat())
        self.assertEqual(signal["confirmed_at"], (START + timedelta(minutes=20)).isoformat())
        self.assertTrue(signal["session_validated"])

    def test_complete_requires_all_75_closed_bars(self):
        rows = bars(75)
        self.assertFalse(rules.session(rows, now=START + timedelta(minutes=374))["complete"])
        self.assertTrue(rules.session(rows, now=START + timedelta(minutes=375))["complete"])
        self.assertIsNone(rules.session(bars(76)))

    def test_utc_and_naive_exchange_timestamps_are_explicitly_normalized(self):
        rows = bars()
        rows[0]["timestamp"] = "2026-09-07T03:45:00Z"
        rows[1]["timestamp"] = datetime(2026, 9, 7, 9, 20)
        self.assertEqual(rules.session(rows)["timestamps"][0], START)


class CausalityTests(unittest.TestCase):
    def test_future_volume_cannot_create_an_earlier_orb_or_momentum(self):
        baseline = breakout_bars()
        future_changed = copy.deepcopy(baseline)
        for row in future_changed[4:]:
            row["volume"] = 100000
        before = rules.session(baseline, avg_volume=7500)
        after = rules.session(future_changed, avg_volume=7500)
        self.assertEqual(before["rvol_by_bar"][:4], after["rvol_by_bar"][:4])
        for name in ("ORB breakout", "RVOL momentum"):
            self.assertNotIn(name, rules.signals(rules.session(baseline[:4], avg_volume=7500)))
            result = rules.signals(after).get(name)
            self.assertTrue(result is None or result["bar"] >= 4)

    def test_earlier_signal_and_own_rvol_survive_later_volume_changes(self):
        first = breakout_bars()
        changed = copy.deepcopy(first)
        changed[-1]["volume"] = 10000000
        for name in ("ORB breakout", "RVOL momentum"):
            a = rules.signals(rules.session(first, avg_volume=3000))[name]
            b = rules.signals(rules.session(changed, avg_volume=3000))[name]
            self.assertEqual(a, b)
            self.assertEqual(a["bar"], 3)
            self.assertEqual(a["rvol"], 2.5)

    def test_missing_rvol_and_legacy_scalar_cannot_bypass_volume_gate(self):
        for rvol in (None, 9999):
            result = rules.signals(rules.session(breakout_bars(), rvol=rvol))
            self.assertNotIn("ORB breakout", result)
            self.assertNotIn("RVOL momentum", result)
        rows = breakout_bars()
        for row in rows:
            row["open"], row["close"] = 200 - row["open"], 200 - row["close"]
            row["low"], row["high"] = 200 - row["high"], 200 - row["low"]
        self.assertNotIn("ORB breakdown", rules.signals(rules.session(rows, rvol=9999)))
        short = rules.signals(rules.session(rows, avg_volume=3000))["ORB breakdown"]
        self.assertEqual(short["direction"], "short")

    def test_prior_volume_profile_is_causal_and_explicit(self):
        profile = [200 * (i + 1) for i in range(75)]
        session = rules.session(bars(), volume_profile=profile)
        self.assertEqual(session["rvol_by_bar"], [0.5] * 6)
        self.assertEqual(session["rvol_method"], "prior_session_cumulative_profile")
        self.assertIsNone(rules.session(bars(), volume_profile=[100] * 5))


class ExecutionTests(unittest.TestCase):
    def test_enters_next_bar_open_after_confirmation(self):
        rows = bars()
        rows[4].update(open=100.4, close=100.4, high=100.6)
        rows[5].update(open=100.4, close=102, high=102.2)
        result = rules.simulate(setup(), rules.session(rows), cost_bps=0, slippage_bps=0)
        self.assertEqual(result["entry_price"], 100.4)
        self.assertEqual(result["entry_at"], rows[4]["timestamp"])
        self.assertAlmostEqual(result["gross_r"], 1.6 / 1.4, places=5)

    def test_gap_stop_uses_worse_open_and_adverse_fills_for_both_directions(self):
        for direction, op, hi, lo, close in (("long", 98.5, 98.8, 98, 98.4),
                                             ("short", 103, 103.5, 102.5, 103)):
            with self.subTest(direction=direction):
                rows = bars()
                rows[5].update(open=op, high=hi, low=lo, close=close)
                result = rules.simulate(setup(direction), rules.session(rows))
                self.assertEqual(result["outcome"], "stop")
                self.assertEqual(result["raw_exit_price"], op)
                self.assertEqual(result["exit_at"], rows[5]["timestamp"])
                self.assertEqual(result["exit_time_precision"], "bar_open")
                self.assertLess(result["net_r"], result["gross_r"])
                self.assertLess(result["net_r"], -1)
                self.assertGreater(result["slippage_r"], 0)
                self.assertEqual(result["direction"], direction)

    def test_both_touch_is_conservative_stop(self):
        rows = bars(5)
        rows[4].update(low=98, high=103)
        for direction in ("long", "short"):
            result = rules.simulate(setup(direction), rules.session(rows), cost_bps=0, slippage_bps=0)
            self.assertEqual(result["outcome"], "stop")
            self.assertEqual(result["r"], -1)

    def test_favorable_target_gap_does_not_assume_better_fill(self):
        rows = bars()
        rows[5].update(open=103, close=103, high=104, low=102.5)
        result = rules.simulate(setup(), rules.session(rows), cost_bps=0, slippage_bps=0)
        self.assertEqual(result["raw_exit_price"], 102)

    def test_costs_reconcile_and_are_positive_on_a_flat_trade(self):
        result = rules.simulate(setup(), rules.session(bars(75)))
        self.assertEqual(result["gross_r"], 0)
        self.assertGreater(result["costs_r"], 0)
        self.assertLess(result["net_r"], 0)
        self.assertAlmostEqual(result["gross_r"] - result["costs_r"], result["net_r"], places=5)

    def test_partial_day_is_open_and_never_a_completed_win(self):
        result = rules.simulate(setup(), rules.session(bars()))
        self.assertEqual(result["outcome"], "open")
        self.assertIsNone(result["r"])
        self.assertIsNone(result["net_r"])
        self.assertIsNone(result["exit_at"])
        self.assertIsNotNone(result["unrealized_net_r"])
        self.assertEqual(rules.summarise([result])["trades"], 0)
        self.assertEqual(rules.summarise([result, {"outcome": "target", "r": 2}])["trades"], 1)

    def test_complete_day_closes_at_1530(self):
        result = rules.simulate(setup(), rules.session(bars(75)))
        self.assertEqual(result["outcome"], "close")
        self.assertEqual(result["exit_at"], "2026-09-07T15:30:00+05:30")

    def test_no_next_bar_invalid_levels_and_gapped_entry_produce_no_trade(self):
        self.assertIsNone(rules.simulate(setup(), rules.session(bars(4))))
        bad = setup()
        bad["target"] = 98
        self.assertIsNone(rules.simulate(bad, rules.session(bars())))
        rows = bars()
        rows[4].update(open=98, close=98, high=98.5, low=97.5)
        self.assertIsNone(rules.simulate(setup(), rules.session(rows)))
        rows[4].update(open=103, close=103, high=103.5, low=102.5)
        self.assertIsNone(rules.simulate(setup(), rules.session(rows)))

    def test_future_and_malformed_forming_ohlc_cannot_trigger_signal(self):
        rows = breakout_bars(6)
        rows[4]["close"] = float("inf")
        asof = START + timedelta(minutes=20)
        session = rules.session(rows, now=asof, avg_volume=3000)
        self.assertEqual(session["n"], 4)
        self.assertEqual(rules.signals(session)["ORB breakout"]["bar"], 3)
        self.assertIsNone(rules.session(rows, now=asof + timedelta(minutes=5)))

    def test_fingerprint_covers_cost_configuration(self):
        original = rules.strategy_version()
        self.assertEqual(len(original), 64)
        with mock.patch.object(rules, "SLIPPAGE_BPS", 25):
            self.assertNotEqual(original, rules.strategy_version())


if __name__ == "__main__":
    unittest.main()
