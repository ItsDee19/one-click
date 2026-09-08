"""Backend integration checks; no credentials, provider calls or messages."""
import importlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class IntelligenceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Importing the application must not load a user's local credentials.
        exists = os.path.exists
        with patch("os.path.exists", side_effect=lambda path: False if Path(path) == ROOT / ".env" else exists(path)):
            cls.app = importlib.import_module("app")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DB_DIR": self.temp.name, "FULL_EXCHANGE": "1",
                                           "LLM_PROVIDER": "deterministic", "TELEGRAM_BOT_TOKEN": ""})
        self.env.start()
        self.client = self.app.app.test_client()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_no_scan_does_not_claim_curated_count_as_full_exchange(self):
        with patch.object(self.app.market, "regime", return_value={}):
            payload = self.client.get("/config").get_json()
        self.assertEqual(payload["universe"]["total"], 0)
        self.assertEqual(payload["universe"]["counts_basis"], "pending first discovery")
        self.assertEqual(self.client.get("/coverage").get_json()["status"], "not_run")

    def test_pagination_validation_and_empty_results(self):
        for query in ("run_id=0", "run_id=-1", "run_id=100000000000000000000", "offset=100000000000000000000", "limit=oops"):
            self.assertEqual(self.client.get("/intelligence?" + query).status_code, 400, query)
        self.assertEqual(self.client.get("/intelligence").get_json()["items"], [])

    def test_all_stock_records_and_csv_are_reachable(self):
        from intelligence_store import IntelligenceStore
        store = IntelligenceStore()
        entries = [{"ticker": "NEW.NS", "name": "New Company"}]
        run = store.start({"listed": 1, "universe": {"source": "fixture"}}, entries)
        store.save(run, dict(entries[0], data_gaps=["price.live"]), {}, "missing_data")
        self.assertEqual(self.client.get("/intelligence?search=NEW&evidence=1").get_json()["total"], 1)
        response = self.client.get("/intelligence.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("NEW.NS", response.get_data(as_text=True))
        self.assertIn("attachment", response.headers["Content-Disposition"])

    def test_nonactionable_buy_never_fires(self):
        result = {"tracks": {"positional": {"verdict": "BUY", "confidence": 10, "actionable": False}}}
        with patch.object(self.app, "db", side_effect=AssertionError("no account access expected")):
            row = self.app._verdict_row({"symbol": "DEMO", "source": "demo"}, result, 7)
        self.assertFalse(row["tracks"]["positional"]["fired"])

    def test_app_default_discovers_full_exchange(self):
        with patch.object(self.app.data_sources, "load_full_exchange", return_value={"unclassified": []}) as fetch:
            self.assertEqual(self.app.active_universe(), {"unclassified": []})
        fetch.assert_called_once()

    def test_intraday_route_delegates_scan_and_explicit_refresh(self):
        with patch.object(self.app.intraday_service.SERVICE, "get", return_value={"status": "running", "picks": []}) as service, \
                patch.object(self.app, "active_universe", side_effect=AssertionError("HTTP request must not download universe")):
            self.assertEqual("running", self.client.get("/intraday").get_json()["status"])
            self.assertFalse(service.call_args.kwargs["refresh"])
            self.assertEqual("1", service.call_args.kwargs["key"])
            self.client.get("/intraday?refresh=1")
            self.assertTrue(service.call_args.kwargs["refresh"])


if __name__ == "__main__":
    unittest.main()
