"""Offline checks for single-flight scans, stale cache and immutable observations."""
from datetime import datetime, timedelta
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import market
from intraday_service import IntradayService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 8, 10, 0, tzinfo=market.IST)

    def payload(self):
        item = {"ticker": "T.NS", "strategy": "ORB breakout", "direction": "long",
                "confirmed_at": self.now.isoformat(), "as_of": self.now.isoformat(),
                "expires_at": (self.now + timedelta(minutes=10)).isoformat(), "state": "entry_ready",
                "validation": {"qualified": True}}
        return {"generated": self.now.isoformat(), "picks": [item], "candidates": [], "history": []}

    def test_poll_is_nonblocking_and_single_flight(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def scan(**kwargs):
            calls.append(kwargs["universe"])
            entered.set()
            self.assertTrue(release.wait(3))
            return self.payload()
        service = IntradayService(scan, lambda: self.now)
        try:
            first = service.get(lambda: {"full": []})
            self.assertEqual("running", first["status"])
            self.assertTrue(entered.wait(2))
            for _ in range(5):
                self.assertEqual("running", service.get(lambda: self.fail("duplicate loader"), refresh=True)["status"])
            self.assertEqual(1, len(calls))
        finally:
            release.set()
            service.worker.join(3)
        self.assertEqual("done", service.get(lambda: self.fail("poll restarted scan"))["status"])

    def test_cached_pick_expires_without_new_download(self):
        service = IntradayService(lambda **kwargs: self.payload(), lambda: self.now)
        service.get(lambda: {})
        service.worker.join(3)
        self.now += timedelta(minutes=11)
        result = service.get(lambda: self.fail("poll must not download"))
        self.assertEqual([], result["picks"])
        self.assertEqual("stale", result["history"][0]["state"])

    def test_failure_clears_actionable_items_and_explicit_retry_works(self):
        def fail(**kwargs):
            raise RuntimeError("secret token must not appear")
        service = IntradayService(fail, lambda: self.now)
        service.get(lambda: {})
        service.worker.join(3)
        result = service.get(lambda: self.fail("implicit retry"))
        self.assertEqual("error", result["status"])
        self.assertNotIn("secret", result["error"])
        self.assertEqual([], result["picks"])
        service.scanner = lambda **kwargs: self.payload()
        service.get(lambda: {}, refresh=True)
        service.worker.join(3)
        self.assertEqual("done", service.get(lambda: {})["status"])

    def test_universe_change_does_not_publish_previous_universe(self):
        service = IntradayService(lambda **kwargs: self.payload(), lambda: self.now)
        service.get(lambda: {}, key="full")
        service.worker.join(3)
        changed = service.get(lambda: {}, key="curated")
        self.assertEqual([], changed["picks"])
        service.worker.join(3)

    def test_journal_retains_first_observation_not_retroactive_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.db"
            service = IntradayService(clock=lambda: self.now, journal_path=path)
            first = self.payload()
            service._journal(first)
            newer = self.payload()
            newer["generated"] = (self.now + timedelta(minutes=5)).isoformat()
            newer["picks"][0]["state"] = "target_hit"
            service._journal(newer)
            with closing(sqlite3.connect(path)) as db:
                rows = db.execute("SELECT first_seen, latest_seen, first_payload, latest_payload FROM observations").fetchall()
            self.assertEqual(1, len(rows))
            self.assertEqual(first["generated"], rows[0][0])
            self.assertEqual(newer["generated"], rows[0][1])
            self.assertEqual("entry_ready", json.loads(rows[0][2])["state"])
            self.assertEqual("target_hit", json.loads(rows[0][3])["state"])


if __name__ == "__main__":
    unittest.main()
