"""Offline evidence-admission regressions; none of these fixtures are market results."""

import copy
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest import mock

import intraday_validation as validation
import strategies

NOW = datetime(2026, 9, 8, 18, tzinfo=strategies.IST)


def qualified_fixture():
    """A deliberately complete evidence contract for testing each fail-closed gate."""
    days, day = [], datetime(2026, 6, 1, tzinfo=strategies.IST)
    while len(days) < 60:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    ledger = []
    for day in days:
        for number in range(2):
            ledger.append({"ticker": f"TEST{number}.NS", "date": day.date().isoformat(),
                           "strategy": "ORB breakout", "direction": "long", "sample": "oos",
                           "confirmed_at": day.replace(hour=9, minute=35).isoformat(),
                           "entry_at": day.replace(hour=9, minute=35).isoformat(),
                           "exit_at": day.replace(hour=10).isoformat(),
                           "gross_r": .6, "costs_r": .1, "net_r": .5})
    return {
        "schema_version": validation.SCHEMA_VERSION, "strategy_version": strategies.strategy_version(),
        "validation_policy_hash": validation.validation_policy_hash(), "generated_at": NOW.isoformat(),
        "cost_model": {"version": validation.COST_MODEL_VERSION, "round_trip_cost_bps": 10, "slippage_bps_per_fill": 5},
        "data": {"point_in_time_universe": True, "universe_evidence": "synthetic membership fixture"},
        "holdout": {"method": "chronological_global_dates", "development_end": "2026-05-28",
                    "embargo_dates": ["2026-05-29"], "start": days[0].date().isoformat(),
                    "end": days[-1].date().isoformat(), "untouched": True,
                    "preregistered_at": "2026-05-01T12:00:00+05:30", "protocol_evidence": "synthetic fixed protocol"},
        "strategies": {"ORB breakout": {"directions": {"long": {}}}}, "ledger": ledger,
        "ranking": {"policy_version": validation.RANKING_POLICY_VERSION, "tested": True,
                    "method": "event_time_full_policy_replay", "execution_eligibility_evidence": "synthetic broker archive",
                    "ledger": copy.deepcopy(ledger)},
    }


class EvidenceGateTests(unittest.TestCase):
    def assess(self, blob, direction="long"):
        return validation.assess_record(blob, "ORB breakout", direction, now=NOW)

    def test_complete_contract_uses_ledger_not_untrusted_summary(self):
        blob = qualified_fixture()
        blob["strategies"]["ORB breakout"]["directions"]["long"]["oos"] = {"net_expectancy_r": 900}
        result = self.assess(blob)
        self.assertTrue(result["qualified"], result["reasons"])
        self.assertEqual("historically_validated", result["status"])
        self.assertEqual(.5, result["metrics"]["net_expectancy_r"])

    def test_legacy_enough_gross_record_never_qualifies(self):
        result = self.assess({"strategies": {"ORB breakout": {"trades": 9000, "enough": True, "expectancy_r": 1}}})
        self.assertEqual("unverified", result["status"])
        self.assertFalse(result["qualified"])

    def test_code_policy_cost_or_ranking_changes_invalidate(self):
        for field in ("strategy_version", "validation_policy_hash"):
            with self.subTest(field=field):
                blob = qualified_fixture()
                blob[field] = "obsolete"
                self.assertEqual("unverified", self.assess(blob)["status"])
        blob = qualified_fixture()
        blob["cost_model"]["round_trip_cost_bps"] = 0
        self.assertEqual("unverified", self.assess(blob)["status"])
        blob = qualified_fixture()
        blob["ranking"]["policy_version"] = "old"
        self.assertFalse(self.assess(blob)["qualified"])

    def test_live_admission_and_replay_source_changes_invalidate_but_line_endings_do_not(self):
        baseline = validation.validation_policy_hash()
        original_read = Path.read_text
        for filename in ("intraday_policy.py", "intraday_desk.py", "intraday_data.py", "backtest_intraday.py", "strategy_edge.py"):
            def edited(path, *args, **kwargs):
                text = original_read(path, *args, **kwargs)
                return text + "\n# changed execution behavior\n" if path.name == filename else text
            with self.subTest(filename=filename), mock.patch.object(Path, "read_text", edited):
                self.assertNotEqual(baseline, validation.validation_policy_hash())
        def windows_lines(path, *args, **kwargs):
            return original_read(path, *args, **kwargs).replace("\n", "\r\n")
        with mock.patch.object(Path, "read_text", windows_lines):
            self.assertEqual(baseline, validation.validation_policy_hash())

    def test_missing_and_wrong_direction_cannot_borrow_long_evidence(self):
        for direction in (None, "short"):
            self.assertFalse(self.assess(qualified_fixture(), direction)["qualified"])
        blob = qualified_fixture()
        del blob["ledger"][0]["direction"]
        self.assertFalse(self.assess(blob)["qualified"])

    def test_negative_net_even_with_enough_flag_is_rejected(self):
        blob = qualified_fixture()
        for row in blob["ledger"]:
            row.update(net_r=-.1, gross_r=0)
        blob["strategies"]["ORB breakout"]["enough"] = True
        result = self.assess(blob)
        self.assertFalse(result["qualified"])
        self.assertTrue(any("positive net" in reason for reason in result["reasons"]))

    def test_profitable_pooled_trades_do_not_establish_selector_validation(self):
        blob = qualified_fixture()
        blob["ranking"]["tested"] = False
        result = self.assess(blob)
        self.assertEqual("research_only", result["status"])
        self.assertFalse(result["qualified"])

    def test_synthetic_split_does_not_establish_untouched_holdout(self):
        for field, value in (("untouched", False), ("preregistered_at", "2026-08-01T00:00:00+05:30"),
                             ("embargo_dates", []), ("development_end", "2026-06-02")):
            with self.subTest(field=field):
                blob = qualified_fixture()
                blob["holdout"][field] = value
                self.assertFalse(self.assess(blob)["qualified"])

    def test_current_universe_only_remains_research(self):
        blob = qualified_fixture()
        blob["data"]["point_in_time_universe"] = False
        self.assertFalse(self.assess(blob)["qualified"])

    def test_stale_generation_and_refreshed_old_observations_are_rejected(self):
        blob = qualified_fixture()
        blob["generated_at"] = (NOW - timedelta(days=91)).isoformat()
        self.assertEqual("unverified", self.assess(blob)["status"])
        blob = qualified_fixture()
        blob["holdout"]["end"] = "2026-06-01"
        self.assertFalse(self.assess(blob)["qualified"])

    def test_ledger_must_reconcile_costs_and_follow_confirmation(self):
        for key, value in (("net_r", 100), ("costs_r", 0), ("entry_at", "2026-06-01T09:30:00+05:30"),
                           ("exit_at", "2026-06-02T10:00:00+05:30")):
            with self.subTest(key=key):
                blob = qualified_fixture()
                blob["ledger"][0][key] = value
                self.assertFalse(self.assess(blob)["qualified"])

    def test_duplicate_ledger_cannot_inflate_sample_size(self):
        blob = qualified_fixture()
        blob["ledger"].append(copy.deepcopy(blob["ledger"][0]))
        self.assertFalse(self.assess(blob)["qualified"])

    def test_same_day_correlation_and_minimum_distinct_dates_are_respected(self):
        blob = qualified_fixture()
        blob["ledger"] = blob["ledger"][:118]
        self.assertFalse(self.assess(blob)["qualified"])
        summary = validation.net_summary([{"date": "2026-06-01", "net_r": 1}] * 1000)
        self.assertEqual(1, summary["distinct_dates"])
        self.assertEqual([None, None], summary["net_expectancy_ci95"])

    def test_day_cluster_interval_is_deterministic_and_can_cross_zero(self):
        rows = [{"date": f"2026-06-{number:02}", "net_r": 1 if number % 2 else -1}
                for number in range(1, 21) for _ in range(10)]
        first = validation.net_summary(rows)
        self.assertEqual(first, validation.net_summary(list(reversed(rows))))
        self.assertLess(first["net_expectancy_ci95"][0], 0)
        self.assertGreater(first["net_expectancy_ci95"][1], 0)

    def test_malformed_record_fails_closed_without_raising(self):
        for field in ("cost_model", "strategies", "holdout", "ranking", "data"):
            for malformed in ([], "bad", 1):
                with self.subTest(field=field, malformed=malformed):
                    blob = qualified_fixture()
                    blob[field] = malformed
                    self.assertFalse(self.assess(blob)["qualified"])


if __name__ == "__main__":
    unittest.main()
