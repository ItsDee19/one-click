"""Non-blocking, single-flight full-universe scans; GET polling never starts a loop."""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import threading

import intraday_desk
import market


class IntradayService:
    def __init__(self, scanner=None, clock=None, journal_path=None):
        self.scanner = scanner or intraday_desk.scan
        self.clock = clock or market.now_ist
        self.journal_path = journal_path
        self.lock = threading.RLock()
        self.worker = None
        self.key = None
        self.snapshot = {"status": "idle", "picks": [], "candidates": [], "history": [], "strategies": {}}

    def get(self, universe_loader, key="full", refresh=False, log=None):
        now = self.clock()
        wanted = (key, now.date().isoformat())
        with self.lock:
            running = self.worker is not None and self.worker.is_alive()
            if wanted != self.key and running:
                return {"status": "running", "picks": [], "candidates": [], "history": [],
                        "strategies": {}, "tradeable": False,
                        "progress": "Finishing the previous universe scan; this universe will load next."}
            if not running and (wanted != self.key or refresh or self.snapshot["status"] == "idle"):
                if wanted != self.key:
                    self.snapshot = {"picks": [], "candidates": [], "history": [], "strategies": {}}
                self.key = wanted
                self.snapshot.update(status="running", error=None, progress="Loading exchange universe")
                self.worker = threading.Thread(target=self._run, args=(universe_loader, log), daemon=True,
                                               name="intraday-scan")
                self.worker.start()
            response = deepcopy(self.snapshot)
        return intraday_desk.refresh_publication(response, now)

    def _run(self, loader, log):
        def progress(message):
            with self.lock:
                self.snapshot["progress"] = str(message)
            if log:
                log(message)
        try:
            result = self.scanner(universe=loader(), log=progress)
            result = intraday_desk.refresh_publication(result, self.clock())
            result.update(status="done", error=None, progress=None)
            if self.journal_path:
                try:
                    self._journal(result)
                    result["journal_status"] = "recorded"
                except (OSError, sqlite3.Error, TypeError, ValueError):
                    result["journal_status"] = "unavailable"
                    result["note"] = (result.get("note") or "") + " Forward observation journal is unavailable."
            with self.lock:
                self.snapshot = result
        except Exception as exc:
            with self.lock:
                # Retain dated history for diagnosis, never keep actionable cards after failure.
                history = (self.snapshot.get("history", []) + self.snapshot.get("picks", [])
                           + self.snapshot.get("candidates", []))
                self.snapshot.update(status="error", picks=[], candidates=[], history=history[:100],
                                     error=f"Intraday scan failed ({type(exc).__name__}); retry the scan.", progress=None)

    def _journal(self, result):
        """Immutable first observation plus updates, explicitly not broker fills or P&L."""
        path = (Path(os.environ.get("DB_DIR") or Path(__file__).parent) / "intraday_observations.db"
                if self.journal_path is True else Path(self.journal_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, latest_seen TEXT NOT NULL, first_payload TEXT NOT NULL, latest_payload TEXT NOT NULL)")
            for item in result.get("picks", []) + result.get("candidates", []) + result.get("history", []):
                identity = "|".join(str(item.get(k, "")) for k in ("strategy_version", "ticker", "strategy", "direction", "confirmed_at"))
                payload = json.dumps(item, allow_nan=False, sort_keys=True)
                stamp = result.get("generated") or self.clock().isoformat()
                db.execute("INSERT INTO observations VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET latest_seen=excluded.latest_seen,latest_payload=excluded.latest_payload",
                           (identity, stamp, stamp, payload, payload))


SERVICE = IntradayService(journal_path=True)
