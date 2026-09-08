"""Durable, queryable coverage and evidence for every stock, separate from signals."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class IntelligenceStore:
    def __init__(self, path=None):
        self.path = Path(path or Path(os.environ.get("DB_DIR") or Path(__file__).parent) / "intelligence.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS intelligence_runs (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, coverage TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS stock_analysis (
                    run_id INTEGER NOT NULL, ticker TEXT NOT NULL, name TEXT NOT NULL,
                    status TEXT NOT NULL, rank_score REAL, evidence TEXT, result TEXT, error TEXT,
                    updated_at TEXT NOT NULL, PRIMARY KEY (run_id,ticker));
                CREATE INDEX IF NOT EXISTS analysis_rank ON stock_analysis(run_id, rank_score DESC);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def start(self, coverage, entries):
        with self.connect() as conn:
            stamp = now()
            run_id = conn.execute("INSERT INTO intelligence_runs(started_at,updated_at,status,coverage) VALUES(?,?,?,?)",
                                  (stamp, stamp, "running", json.dumps(coverage, allow_nan=False))).lastrowid
            conn.executemany("INSERT INTO stock_analysis(run_id,ticker,name,status,updated_at) VALUES(?,?,?,?,?)",
                             [(run_id, e["ticker"], e.get("name") or e["ticker"], "pending", stamp) for e in entries])
        return run_id

    def update_coverage(self, run_id, coverage, status="running"):
        with self.connect() as conn:
            conn.execute("UPDATE intelligence_runs SET updated_at=?,status=?,coverage=? WHERE id=?",
                         (now(), status, json.dumps(coverage, allow_nan=False), run_id))

    def save(self, run_id, evidence, result, status, rank_score=None, error=None):
        with self.connect() as conn:
            conn.execute("""UPDATE stock_analysis SET name=?,status=?,rank_score=?,evidence=?,result=?,error=?,updated_at=?
                          WHERE run_id=? AND ticker=?""",
                         (evidence.get("name") or evidence["ticker"], status, rank_score,
                          json.dumps(evidence, allow_nan=False), json.dumps(result, allow_nan=False), error,
                          now(), run_id, evidence["ticker"]))

    def coverage(self, run_id=None):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM intelligence_runs WHERE id=?", (run_id,)).fetchone() if run_id else conn.execute(
                "SELECT * FROM intelligence_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return {"status": "not_run", "scope": "NSE equity trading lists", "listed": 0}
            out = json.loads(row["coverage"])
            out.update(run_id=row["id"], status=row["status"], started_at=row["started_at"], updated_at=row["updated_at"])
            out["records_by_status"] = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status,COUNT(*) AS n FROM stock_analysis WHERE run_id=? GROUP BY status", (row["id"],))}
            return out

    def query(self, run_id=None, search="", status=None, limit=100, offset=0, include_evidence=False):
        import evidence_quality
        import market
        coverage = self.coverage(run_id)
        selected = coverage.get("run_id")
        if not selected:
            return {"coverage": coverage, "total": 0, "items": []}
        clauses, params = ["run_id=?"], [selected]
        if search:
            # Literal substring search; user input never becomes SQL or a wildcard.
            clauses.append("(instr(upper(ticker),upper(?))>0 OR instr(upper(name),upper(?))>0)")
            params.extend([search, search])
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " AND ".join(clauses)
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM stock_analysis WHERE {where}", params).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM stock_analysis WHERE {where} ORDER BY rank_score DESC,ticker LIMIT ? OFFSET ?",
                                params + [max(1, min(int(limit), 500)), max(0, int(offset))]).fetchall()
        items = []
        current_market = market.describe()
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item["result"]) if item["result"] else None
            ev = json.loads(item.pop("evidence") or "null")
            # Stored verdicts describe the analysis time. Report present-day
            # usability separately so an old actionable snapshot cannot pass
            # for current evidence just because it was loaded from the DB.
            item["current_evidence_quality"] = evidence_quality.assess_evidence(
                dict(ev, market=dict(ev.get("market") or {}, **current_market))) if ev else None
            item["actionable_now"] = {
                track: bool(((item.get("result") or {}).get("tracks") or {}).get(track, {}).get("verdict") == "BUY"
                            and ((item.get("result") or {}).get("tracks") or {}).get(track, {}).get("actionable") is True
                            and ((item["current_evidence_quality"] or {}).get("actionable") or {}).get(track))
                for track in ("intraday", "positional")
            }
            item["result_basis"] = "historical analysis snapshot at updated_at"
            if include_evidence:
                item["evidence"] = ev
            elif ev:
                item.update(price=ev.get("price"), fundamentals=ev.get("fundamentals"),
                            data_gaps=ev.get("data_gaps"), evidence_scope=ev.get("evidence_scope"))
            items.append(item)
        return {"coverage": coverage, "total": total, "offset": max(0, int(offset)), "items": items}
