"""Daily picks must be fresh, directional, executable candidates with evidence."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from unittest import mock

import pandas as pd

import intraday_desk as desk
import intraday_policy as policy
import market
import strategies


START = datetime(2026, 9, 8, 9, 15, tzinfo=market.IST)
NOW = START + timedelta(minutes=20)


def session(n=4):
    return strategies.session([{"timestamp": START + timedelta(minutes=5 * i),
        "open": 100, "high": 100.5, "low": 99.5, "close": 100, "volume": 1000}
        for i in range(n)], avg_volume=30000)


def signal(direction="long", name=None, **updates):
    name = name or ("ORB breakout" if direction == "long" else "ORB breakdown")
    stop, target = (99, 103) if direction == "long" else (101, 97)
    item = strategies.Setup(name, 3, 100, stop, target, direction, "test setup")
    item.update(signal_at=(NOW - timedelta(minutes=5)).isoformat(),
                confirmed_at=NOW.isoformat(), rvol=2.5, session_validated=True)
    item.update(updates)
    return item


def listing(ticker="TEST.NS", **updates):
    item = {"ticker": ticker, "name": ticker, "sector": "Technology",
            "segment": "main_board", "series": "EQ"}
    item.update(updates)
    return item


def permission(tickers, direction="long"):
    return {"symbols": {ticker: {"as_of": NOW.date().isoformat(), "source": "fixture broker", direction: True}
                         for ticker in tickers}}


def reference():
    return {"prev_close": 100, "avg_volume": 30000, "avg_turnover": 20_000_000,
            "reference_date": "2026-09-07", "reference_sessions": 20}


class LifecycleTests(unittest.TestCase):
    def test_fresh_long_and_short_keep_sensible_current_net_levels(self):
        for direction in ("long", "short"):
            state = desk.setup_lifecycle(signal(direction), session(), NOW)
            self.assertEqual(state["state"], "entry_ready")
            self.assertGreaterEqual(state["current_reward_risk"], policy.MIN_NET_REWARD_RISK)
            self.assertEqual(state["expires_at"], (NOW + timedelta(minutes=desk.MAX_BAR_AGE_MINUTES)).isoformat())
            self.assertEqual(state["as_of"], NOW.isoformat())

    def test_stop_or_target_hit_cannot_reappear_as_current_pick(self):
        for direction, key, level, expected in (("long", "low", 98, "stop_hit"),
              ("long", "high", 104, "target_hit"), ("short", "high", 102, "stop_hit"),
              ("short", "low", 96, "target_hit")):
            with self.subTest(direction=direction, expected=expected):
                s = session(6)
                s[key][4] = level
                result = desk.setup_lifecycle(signal(direction), s, NOW + timedelta(minutes=10))
                self.assertEqual(result["state"], expected)

    def test_ambiguous_both_touch_marks_stop(self):
        s = session(5)
        s["high"][4], s["low"][4] = 104, 98
        self.assertEqual(desk.setup_lifecycle(signal(), s, NOW + timedelta(minutes=5))["state"], "stop_hit")

    def test_latest_price_does_not_renew_original_signal_age(self):
        result = desk.setup_lifecycle(signal(), session(6), NOW + timedelta(minutes=10))
        self.assertEqual(result["state"], "expired")
        self.assertEqual(result["expires_at"], (NOW + timedelta(minutes=10)).isoformat())

    def test_entry_drift_and_cost_adjusted_reward_risk_are_admission_gates(self):
        s = session()
        s["close"][-1] = 100.3
        self.assertEqual(desk.setup_lifecycle(signal(), s, NOW)["state"], "missed")
        result = desk.setup_lifecycle(signal(target=101.6), session(), NOW)
        self.assertEqual(result["state"], "missed")
        self.assertLess(result["current_reward_risk"], 1.5)

    def test_unknown_volume_future_confirmation_and_invalid_direction_are_rejected(self):
        self.assertEqual(desk.setup_lifecycle(signal(rvol=None), session(), NOW)["state"], "restricted")
        future = signal(confirmed_at=(NOW + timedelta(minutes=5)).isoformat())
        self.assertNotEqual(desk.setup_lifecycle(future, session(), NOW)["state"], "entry_ready")
        self.assertEqual(desk.setup_lifecycle(signal(direction="sideways"), session(), NOW)["state"], "invalid")

    def test_unvalidated_research_session_is_invalid_without_crashing(self):
        s = session()
        s.update(validated=False, timestamps=[None] * 4)
        self.assertEqual(desk.setup_lifecycle(signal(), s, NOW)["state"], "invalid")

    def test_cached_publication_respects_1415_entry_cutoff(self):
        stamp = NOW.replace(hour=14, minute=10)
        payload = {"picks": [dict(signal(), as_of=stamp.isoformat(),
            confirmed_at=stamp.isoformat(), expires_at=(stamp + timedelta(minutes=10)).isoformat())],
            "candidates": [], "history": [], "trading_day": True}
        result = desk.refresh_publication(payload, stamp + timedelta(minutes=5))
        self.assertEqual(result["picks"], [])
        self.assertEqual(result["history"][0]["state"], "expired")
        self.assertEqual(len(payload["picks"]), 1, "Publication checks must not mutate the saved snapshot")

    def test_browser_expiry_is_bounded_by_daily_entry_cutoff(self):
        s = session(59)  # final candle closes at 14:10
        stamp = START + timedelta(minutes=59 * 5)
        setup = signal(bar=58, signal_at=(stamp - timedelta(minutes=5)).isoformat(),
                       confirmed_at=stamp.isoformat())
        state = desk.setup_lifecycle(setup, s, stamp)
        self.assertEqual("entry_ready", state["state"])
        self.assertEqual(stamp.replace(hour=14, minute=15).isoformat(), state["expires_at"])


class EligibilityTests(unittest.TestCase):
    def test_requires_correct_listing_turnover_and_current_side_permission(self):
        entry = listing()
        ref = reference()
        allowed = permission([entry["ticker"]])
        self.assertEqual(desk.eligibility_reasons(entry, ref, "long", NOW, allowed), [])
        self.assertTrue(desk.eligibility_reasons(entry, ref, "short", NOW, allowed))
        self.assertTrue(desk.eligibility_reasons(listing(series="BE"), ref, "long", NOW, allowed))
        self.assertTrue(desk.eligibility_reasons(listing(segment="sme"), ref, "long", NOW, allowed))
        self.assertTrue(desk.eligibility_reasons(entry, {"avg_turnover": 0}, "long", NOW, allowed))
        stale_ref = dict(ref, reference_date="2026-08-31")
        self.assertTrue(desk.eligibility_reasons(entry, stale_ref, "long", NOW, allowed))
        same_day_ref = dict(ref, reference_date=NOW.date().isoformat())
        self.assertTrue(desk.eligibility_reasons(entry, same_day_ref, "long", NOW, allowed))
        old = deepcopy(allowed)
        old["symbols"][entry["ticker"]]["as_of"] = "2026-09-07"
        self.assertTrue(desk.eligibility_reasons(entry, ref, "long", NOW, old))

    def test_unknown_and_string_permissions_do_not_grant_eligibility(self):
        entry, ref = listing(), reference()
        self.assertTrue(desk.eligibility_reasons(entry, ref, "long", NOW, {}))
        allowed = permission([entry["ticker"]])
        allowed["symbols"][entry["ticker"]]["long"] = "true"
        self.assertTrue(desk.eligibility_reasons(entry, ref, "long", NOW, allowed))


class RankingTests(unittest.TestCase):
    def candidate(self, ticker, direction="long", sector="Technology", rr=2, rvol=2):
        return dict(signal(direction), ticker=ticker, sector=sector,
                    current_reward_risk=rr, entry_drift_r=0.1, rvol=rvol)

    def test_opposing_signals_block_a_stock_and_same_side_is_deduplicated(self):
        rows = [self.candidate("A.NS"), self.candidate("A.NS", "short"),
                self.candidate("B.NS"), self.candidate("B.NS", rr=3)]
        result = policy.select_candidates(rows)
        self.assertEqual([p["ticker"] for p in result["selected"]], ["B.NS"])
        self.assertEqual(result["selected"][0]["current_reward_risk"], 3)
        self.assertEqual(sum(p["state"] == "conflict" for p in result["rejected"]), 2)
        self.assertEqual(sum(p["state"] == "duplicate" for p in result["rejected"]), 1)

    def test_deterministic_stock_ranking_and_sector_exposure(self):
        rows = [self.candidate("D.NS", sector="Energy"), self.candidate("C.NS"),
                self.candidate("B.NS"), self.candidate("A.NS")]
        expected = ["A.NS", "B.NS", "D.NS"]
        self.assertEqual([p["ticker"] for p in policy.select_candidates(rows)["selected"]], expected)
        self.assertEqual([p["ticker"] for p in policy.select_candidates(list(reversed(rows)))["selected"]], expected)

    def test_finite_zero_score_is_not_treated_as_missing(self):
        zero, missing = self.candidate("ZERO.NS", rr=0), self.candidate("MISSING.NS", rr=None)
        self.assertLess(policy.rank_key(zero), policy.rank_key(missing))


class ScanAdmissionTests(unittest.TestCase):
    def scan(self, entries, setups, permissions, qualified=True):
        index = pd.date_range(start=START, periods=4, freq="5min")
        frame = pd.DataFrame({"Open": [100] * 4, "High": [100.5] * 4,
                              "Low": [99.5] * 4, "Close": [100] * 4, "Volume": [1000] * 4}, index=index)
        prior = reference()
        check = {"status": "historically_validated" if qualified else "research_only",
                 "qualified": qualified, "metrics": {}, "reasons": [] if qualified else ["Unseen validation missing"]}
        with mock.patch.object(market, "now_ist", return_value=NOW), \
             mock.patch.object(market, "is_trading_day", return_value={"trading": True}), \
             mock.patch.object(desk, "load_record", return_value=({}, {})), \
             mock.patch.object(desk, "_swing_payload", return_value={}), \
             mock.patch.object(desk, "load_eligibility", return_value=permissions), \
             mock.patch.object(desk.intraday_data, "load_references", return_value={e["ticker"]: prior for e in entries}), \
             mock.patch.object(desk.data_sources, "fetch_intraday", return_value={e["ticker"]: frame for e in entries}), \
             mock.patch.object(desk.intraday_validation, "assess_record", return_value=check), \
             mock.patch.object(desk.strategies, "signals", side_effect=setups):
            return desk.scan(universe={"large": entries})

    def test_unverified_evidence_yields_research_and_no_qualified_pick(self):
        entries = [listing()]
        result = self.scan(entries, [{"ORB breakout": signal()}], permission(["TEST.NS"]), qualified=False)
        self.assertEqual(result["picks"], [])
        self.assertEqual(result["candidates"][0]["state"], "research_only")

    def test_restricted_research_cannot_consume_qualified_stock_or_sector_slots(self):
        entries = [listing("A.NS"), listing("B.NS"), listing("C.NS")]
        setups = [{"ORB breakout": signal(target=105)}, {"ORB breakout": signal(target=104)},
                  {"ORB breakout": signal(target=103)}]
        result = self.scan(entries, setups, permission(["C.NS"]))
        self.assertEqual([p["ticker"] for p in result["picks"]], ["C.NS"])

    def test_short_pick_is_explicit_sell_and_requires_short_permission(self):
        entries = [listing()]
        result = self.scan(entries, [{"ORB breakdown": signal("short")}], permission(["TEST.NS"], "short"))
        self.assertEqual(result["picks"][0]["direction"], "short")
        self.assertEqual(result["picks"][0]["action"], "SELL")


if __name__ == "__main__":
    unittest.main()
