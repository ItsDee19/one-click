"""Offline safety/correctness regressions: no prices, network or paid model calls."""

import copy
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import evidence_quality
import llm
import scoring


def strong_evidence():
    now = datetime.now(evidence_quality.IST)
    stamp = now.isoformat()
    return {
        "source": "live", "symbol": "TEST", "as_of": stamp,
        "price": {"live": 100, "volume": 100000, "day_change_pct": 2,
                  "prev_close": 98, "as_of": stamp},
        "range_52w": {"position_pct": 90, "pct_from_high": -5},
        "technicals": {"rvol": 3, "rvol_method": "full session", "price_vs_sma_pct": 5,
                       "trend": "up", "sma_period": 20, "window_return_pct": 10,
                       "day_range_position_pct": 90, "atr_pct": 5, "swing_high": 105,
                       "swing_low": 92, "last_bar": stamp, "observations": 40},
        "analyst": {"target_mean": 120, "upside_pct": 20, "buy_pct": 90,
                    "num_analysts": 12, "consensus": "buy"},
        "intraday": {"available": True, "bars": 12, "vwap": 99,
                     "price_vs_vwap_pct": 1.01, "above_opening_range": True,
                     "opening_range_high": 99.5, "opening_range_low": 98,
                     "gap_pct": 2, "last_bar": stamp},
        "market": {"live_session": True, "minutes_to_close": 120},
        "relative": {"rel_window_return_pct": 10},
        "regime": {"state": "risk_on"},
        "news": {"total": 3, "positive": 2, "negative": 0, "net_tone": 2},
    }


def model_buy():
    payload = {seat: {"conviction": 80 if seat != "bear" else 10,
                      "point": "Observed evidence supports this assessment"}
               for seat in scoring.AGENT_KEYS}
    for track in scoring.TRACKS:
        payload[f"judge_{track}"] = {
            "verdict": "BUY", "confidence": 10, "winner": "Bull",
            "rationale": "Momentum and participation support continuation",
            "key_catalyst": "Observed momentum",
        }
    return payload


class EvidenceIntegrityTests(unittest.TestCase):
    def test_complete_positive_fixture_can_pass_both_tracks(self):
        result = scoring.evaluate(strong_evidence())
        for track in result["tracks"].values():
            self.assertEqual(track["verdict"], "BUY")
            self.assertTrue(track["actionable"])
            self.assertIn("not a calibrated", track["confidence_basis"])

    def test_missing_or_invalid_risk_levels_never_pass_buy(self):
        for target in (None, 90, 100, float("nan"), float("inf")):
            with self.subTest(target=target):
                ev = strong_evidence()
                ev["analyst"]["target_mean"] = target
                block = scoring.evaluate(ev)["tracks"]["positional"]
                self.assertEqual(block["verdict"], "WATCH")
                self.assertFalse(block["actionable"])
                self.assertIsNone(block["risk_reward"]["ratio"])

    def test_risk_reward_rejects_nonpositive_and_nonfinite_inputs(self):
        for price, stop, target in ((0, 90, 120), (-1, 90, 120),
                                    (100, 0, 120), (100, -5, 120),
                                    (100, 100, 120), (float("inf"), 90, 120),
                                    (100, 90, float("nan"))):
            with self.subTest(price=price, stop=stop, target=target):
                rr = scoring.risk_reward(price, {"invalidation": stop, "objective": target})
                self.assertIsNone(rr["ratio"])

    def test_stale_quote_cannot_be_refreshed_by_bundle_timestamp(self):
        ev = strong_evidence()
        ev["price"]["as_of"] = (datetime.now(evidence_quality.IST) - timedelta(days=8)).isoformat()
        out = scoring.evaluate(ev)
        for track in out["tracks"].values():
            self.assertEqual(track["verdict"], "WATCH")
            self.assertIn("quote timestamp is stale", track["evidence_blockers"])

    def test_unknown_quote_timestamp_is_explicit_and_nonactionable(self):
        ev = strong_evidence()
        del ev["price"]["as_of"]
        quality = evidence_quality.assess_evidence(ev)
        self.assertEqual(quality["freshness"]["quote"]["status"], "unknown")
        self.assertFalse(any(quality["actionable"].values()))

    def test_intraday_bars_from_previous_session_do_not_pass(self):
        ev = strong_evidence()
        ev["intraday"]["last_bar"] = (datetime.now(evidence_quality.IST) - timedelta(days=1)).isoformat()
        out = scoring.evaluate(ev)
        self.assertEqual(out["tracks"]["intraday"]["verdict"], "WATCH")
        self.assertEqual(out["tracks"]["positional"]["verdict"], "BUY")

    def test_incomplete_opening_range_is_not_confirmed(self):
        ev = strong_evidence()
        ev["intraday"]["bars"] = 1
        block = scoring.evaluate(ev)["tracks"]["intraday"]
        self.assertEqual(block["verdict"], "WATCH")
        self.assertIn("the opening range has fewer than three bars", block["evidence_blockers"])

    def test_closed_market_does_not_create_actionable_intraday_buy(self):
        ev = strong_evidence()
        ev["market"]["live_session"] = False
        self.assertEqual(scoring.evaluate(ev)["tracks"]["intraday"]["verdict"], "WATCH")

    def test_nonfinite_evidence_does_not_crash_or_leak_nonfinite_json(self):
        ev = strong_evidence()
        ev["technicals"].update(rvol=float("nan"), atr_pct=float("inf"))
        out = scoring.evaluate(ev)
        json.dumps(out, allow_nan=False)
        self.assertEqual(out["tracks"]["positional"]["verdict"], "WATCH")
        self.assertIsNone(out["tracks"]["positional"]["horizon_days_min"])

    def test_extreme_sma_and_atr_do_not_raise_zero_division(self):
        for sma in (-100, -101):
            ev = strong_evidence()
            ev["technicals"].update(price_vs_sma_pct=sma, atr_pct=5e-324)
            json.dumps(scoring.evaluate(ev), allow_nan=False)

    def test_unknown_news_counts_are_not_imputed_as_zero(self):
        ev = strong_evidence()
        ev["news"].update(net_tone=None, positive=None, negative=None)
        reasons = scoring.evaluate(ev)["scores"]["news"]["reasons"]
        self.assertIn("sentiment data unavailable", " ".join(reasons))
        self.assertNotIn("0 negative", " ".join(reasons))

    def test_missing_opening_range_is_not_described_as_a_failed_breakout(self):
        ev = strong_evidence()
        ev["intraday"]["above_opening_range"] = None
        rationale = scoring.evaluate(ev)["tracks"]["intraday"]["rationale"]
        self.assertIn("confirmation unavailable", rationale)
        self.assertNotIn("high is not cleared", rationale)

    def test_ratio_metrics_are_reported_without_sector_blind_thresholds(self):
        ev = strong_evidence()
        ev["fundamentals"] = {"trailing_pe": 12.3, "debt_to_equity_ratio": 7.8,
                              "roe_pct": None, "stale": True}
        reasons = " ".join(scoring.evaluate(ev)["scores"]["fundamentals"]["reasons"])
        self.assertIn("trailing P/E 12.3x", reasons)
        self.assertIn("statements are stale", reasons)
        self.assertNotIn("return on equity 0", reasons)

    def test_demo_verdicts_remain_illustrative_and_nonactionable(self):
        ev = strong_evidence()
        ev["source"] = "demo"
        ev["price"]["as_of"] = "2001-01-01"
        out = scoring.evaluate(ev)
        self.assertEqual(out["tracks"]["positional"]["verdict"], "BUY")
        self.assertFalse(out["tracks"]["positional"]["actionable"])
        self.assertEqual(out["evidence_quality"]["status"], "demo")
        for path in Path("demo_data").glob("*.json"):
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            result = scoring.evaluate(snapshot)
            self.assertEqual(set(result["tracks"]), set(scoring.TRACKS))
            json.dumps(result, allow_nan=False)

    def test_sanitizing_never_mutates_caller_evidence(self):
        ev = strong_evidence()
        ev["technicals"]["rvol"] = "3.0"
        original = copy.deepcopy(ev)
        scoring.evaluate(ev)
        self.assertEqual(ev, original)


class ModelIntegrityTests(unittest.TestCase):
    def test_model_with_supported_buy_can_pass(self):
        out = llm.normalise(model_buy(), strong_evidence(), "mock")
        self.assertFalse(out["ungrounded_numbers"])
        self.assertEqual(out["tracks"]["positional"]["verdict"], "BUY")

    def test_ungrounded_small_integer_is_flagged_and_blocks_buy(self):
        ev = strong_evidence()
        payload = model_buy()
        payload["bull"]["point"] = "Revenue will grow 7%"
        out = llm.normalise(payload, ev, "mock")
        self.assertTrue(out["ungrounded_numbers"])
        for track in out["tracks"].values():
            self.assertEqual(track["verdict"], "WATCH")
            self.assertTrue(track["model_gated"])

    def test_headline_dates_do_not_whitelist_unsupported_numeric_claims(self):
        ev = strong_evidence()
        ev["news"]["recent"] = [{"title": "Company update in 2031"}]
        payload = model_buy()
        payload["bull"]["point"] = "The price target is 2031"
        self.assertTrue(llm.verify_grounding(payload, ev))

    def test_number_grouping_is_read_as_one_number(self):
        payload = model_buy()
        payload["bull"]["point"] = "The price target is 9,999"
        flags = llm.verify_grounding(payload, strong_evidence())
        self.assertEqual(flags[0]["value"], "9,999")

    def test_model_cannot_buy_with_unsupported_momentum(self):
        ev = strong_evidence()
        ev["technicals"].update(rvol=1, price_vs_sma_pct=2, trend="down", window_return_pct=-5)
        ev["range_52w"]["position_pct"] = 35
        ev["relative"]["rel_window_return_pct"] = -5
        out = llm.normalise(model_buy(), ev, "mock")
        self.assertEqual(out["tracks"]["positional"]["verdict"], "WATCH")
        self.assertTrue(out["tracks"]["positional"]["model_gated"])

    def test_nonfinite_model_scores_or_confidence_are_not_trusted(self):
        for value in (float("nan"), float("inf"), "NaN", None, True, 500):
            with self.subTest(value=value):
                payload = model_buy()
                payload["bull"]["conviction"] = value
                payload["judge_positional"]["confidence"] = value
                out = llm.normalise(payload, strong_evidence(), "mock")
                self.assertEqual(out["tracks"]["positional"]["verdict"], "WATCH")
                self.assertTrue(out["tracks"]["positional"]["model_validation_issues"])
                json.dumps(out, allow_nan=False)

    def test_missing_model_seat_blocks_model_buy(self):
        payload = model_buy()
        del payload["news"]
        self.assertEqual(llm.normalise(payload, strong_evidence(), "mock")["tracks"]["positional"]["verdict"], "WATCH")

    def test_malformed_model_payload_falls_back_without_provider_calls(self):
        with patch.object(llm, "call_openai", return_value="[]"):
            out = llm.evaluate(strong_evidence(), provider={"provider": "openai", "model": "mock", "label": "mock"}, env={})
        self.assertEqual(out["engine"], "deterministic")
        self.assertIn("not an object", out["fallback_reason"])


if __name__ == "__main__":
    unittest.main()
