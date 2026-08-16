"""
app.py — the local server, the agent state machine, Telegram delivery and the
SQLite audit trail.

Everything runs on this machine. There is no cloud backend, no account, no
telemetry. One click starts a cycle on a background thread; the browser polls
/status every ~500ms and re-renders.

No order is ever placed. This is analysis only.
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request

import data_sources
import llm
import scoring

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "signals.db")
DASHBOARD = os.path.join(HERE, "dashboard.html")
IST = timezone(timedelta(hours=5, minutes=30))

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
DISCLAIMER = "— Analysis only. No trade was placed. Not investment advice."


# ==========================================================================
# .env loader — deliberately tiny, no third-party dependency
# ==========================================================================

def load_dotenv(path=os.path.join(HERE, ".env")):
    """Read KEY=VALUE lines into os.environ. Real env vars always win."""
    if not os.path.exists(path):
        return 0
    loaded = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.lower().startswith("export "):
                key = key[7:].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
                loaded += 1
    return loaded


load_dotenv()


def env_str(key, default=""):
    value = os.environ.get(key)
    return default if value is None or value.strip() == "" else value.strip()


def env_int(key, default):
    try:
        return int(float(env_str(key, str(default))))
    except (TypeError, ValueError):
        return default


def env_float(key, default):
    try:
        return float(env_str(key, str(default)))
    except (TypeError, ValueError):
        return default


BRAND = env_str("BRAND", "Dalal Desk")
PORT = env_int("PORT", 5000)


def scrub(text) -> str:
    """Strip the bot token out of anything that could reach a log or the UI."""
    text = "" if text is None else str(text)
    token = env_str("TELEGRAM_BOT_TOKEN")
    if token:
        text = text.replace(token, "<token hidden>")
        head = token.split(":")[0]
        if head and len(head) >= 6:
            text = text.replace(head, "<token hidden>")
    return text


def now_ist():
    return datetime.now(IST)


def stamp():
    return now_ist().strftime("%H:%M:%S")


# ==========================================================================
# the panel
# ==========================================================================

AGENT_SPECS = [
    ("scout", "Scout", "screens the stock universe for movers", "Scanned", "Shortlisted"),
    ("technician", "Technician", "reads price action, RVOL & trend", "Analyzed", "Avg RVOL"),
    ("fundamentalist", "Fundamentalist", "weighs valuation & analyst targets", "Covered", "Avg upside"),
    ("newsdesk", "Newsdesk", "pulls live news & scores sentiment", "Headlines", "Net tone"),
    ("bull", "Bull", "argues the case to buy", "Cases", "Avg score"),
    ("bear", "Bear", "argues the case against", "Cases", "Avg score"),
    ("judge", "Judge", "weighs the debate, issues verdict + confidence", "Verdicts", "Buy"),
    ("messenger", "Messenger", "sends signals to Telegram", "Sent", "Engine"),
]


def fresh_agents():
    return [
        {
            "id": aid, "name": name, "role": role, "status": "offline",
            "stat1": {"label": s1, "value": "—"},
            "stat2": {"label": s2, "value": "—"},
        }
        for aid, name, role, s1, s2 in AGENT_SPECS
    ]


def fresh_state():
    return {
        "status": "idle",                     # idle | running | done | error
        "mode": "demo",
        "engine": llm.detect_provider().get("label", "deterministic"),
        "engine_reason": llm.detect_provider().get("reason", ""),
        "run_id": None,
        "started_at": None,
        "finished_at": None,
        "data_ts": None,
        "kpis": {
            "universe": 0, "in_debate": 0, "buy_signals": 0,
            "top_pick": {"symbol": None, "confidence": None},
        },
        "agents": fresh_agents(),
        "verdicts": [],
        "log": [],
        "telegram": {"configured": telegram_configured(), "sent": 0, "error": None},
        "error": None,
    }


LOCK = threading.RLock()
STATE = {}
WORKER = None


def telegram_configured():
    return bool(env_str("TELEGRAM_BOT_TOKEN") and env_str("TELEGRAM_CHAT_ID"))


# --------------------------------------------------------------------------
# state mutation helpers (always under LOCK)
# --------------------------------------------------------------------------

def log(message):
    line = f"[{stamp()}] {scrub(message)}"
    with LOCK:
        STATE["log"].append(line)
        del STATE["log"][:-160]
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # A legacy Windows console (cp1252) cannot print ₹ or an emoji, and an
        # LLM rationale may contain either. The console is not worth killing a
        # run over — the dashboard and SQLite keep the full text.
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(encoding, "replace").decode(encoding, "replace"), flush=True)


def set_agent(agent_id, status=None, stat1=None, stat2=None):
    with LOCK:
        for agent in STATE["agents"]:
            if agent["id"] != agent_id:
                continue
            if status:
                agent["status"] = status
            if stat1 is not None:
                agent["stat1"]["value"] = stat1
            if stat2 is not None:
                agent["stat2"]["value"] = stat2
            return


def set_kpi(**kwargs):
    with LOCK:
        STATE["kpis"].update(kwargs)


def pace(seconds=None):
    time.sleep(env_float("AGENT_DELAY", 0.6) if seconds is None else seconds)


# ==========================================================================
# SQLite audit
# ==========================================================================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                mode          TEXT NOT NULL,
                engine        TEXT,
                universe      INTEGER DEFAULT 0,
                shortlisted   INTEGER DEFAULT 0,
                buy_signals   INTEGER DEFAULT 0,
                top_symbol    TEXT,
                top_confidence INTEGER,
                telegram_sent INTEGER DEFAULT 0,
                status        TEXT,
                notes         TEXT
            );

            CREATE TABLE IF NOT EXISTS verdicts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        INTEGER NOT NULL REFERENCES runs(id),
                created_at    TEXT NOT NULL,
                symbol        TEXT NOT NULL,
                name          TEXT,
                cap_segment   TEXT,
                sector        TEXT,
                verdict       TEXT,
                confidence    INTEGER,
                winner        TEXT,
                rationale     TEXT,
                key_catalyst  TEXT,
                bull_score    INTEGER,
                bear_score    INTEGER,
                net           INTEGER,
                price         REAL,
                day_change_pct REAL,
                fired         INTEGER DEFAULT 0,
                engine        TEXT,
                ungrounded    INTEGER DEFAULT 0,
                data_gaps     TEXT,
                scores_json   TEXT,
                evidence_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_verdicts_run ON verdicts(run_id);
            CREATE INDEX IF NOT EXISTS idx_verdicts_symbol ON verdicts(symbol);
            """
        )


def db_start_run(mode, engine):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, mode, engine, status) VALUES (?,?,?,?)",
            (now_ist().isoformat(), mode, engine, "running"),
        )
        return cur.lastrowid


def db_save_verdict(run_id, row):
    with db() as conn:
        conn.execute(
            """INSERT INTO verdicts
               (run_id, created_at, symbol, name, cap_segment, sector, verdict,
                confidence, winner, rationale, key_catalyst, bull_score, bear_score,
                net, price, day_change_pct, fired, engine, ungrounded, data_gaps,
                scores_json, evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, now_ist().isoformat(), row["symbol"], row["name"],
                row["cap_segment"], row.get("sector"), row["verdict"], row["confidence"],
                row["winner"], row["rationale"], row["key_catalyst"], row["bull_score"],
                row["bear_score"], row["net"], row.get("price"), row.get("day_change_pct"),
                1 if row.get("fired") else 0, row.get("engine"),
                len(row.get("ungrounded_numbers") or []),
                json.dumps(row.get("data_gaps") or []),
                json.dumps(row.get("scores") or {}),
                json.dumps(row.get("evidence") or {}, default=str),
            ),
        )


def db_finish_run(run_id, **fields):
    if not run_id:
        return
    fields["finished_at"] = now_ist().isoformat()
    columns = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE runs SET {columns} WHERE id = ?",
                     (*fields.values(), run_id))


# ==========================================================================
# Telegram
# ==========================================================================

def send_telegram(text):
    """POST one HTML message. Returns (ok, error_string_or_None)."""
    token = env_str("TELEGRAM_BOT_TOKEN")
    chat_id = env_str("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env"

    try:
        import requests
        response = requests.post(
            TELEGRAM_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if response.status_code >= 400:
            return False, scrub(f"telegram api {response.status_code}: {response.text[:200]}")
        if not (response.json() or {}).get("ok"):
            return False, scrub(f"telegram api rejected the message: {response.text[:200]}")
        return True, None
    except Exception as exc:                                       # noqa: BLE001
        return False, scrub(f"{type(exc).__name__}: {exc}")


def buy_message(row):
    esc = html.escape
    cap = (row.get("cap_segment") or "unknown").capitalize()
    price = row.get("price")
    change = row.get("day_change_pct")
    price_txt = f"₹{price:,.2f}" if isinstance(price, (int, float)) else "data unavailable"
    change_txt = f"{change:+.2f}%" if isinstance(change, (int, float)) else "data unavailable"

    return (
        f"🟢 <b>BUY SIGNAL — {esc(row['symbol'])}</b> ({esc(cap)} cap)\n\n"
        f"Verdict: BUY | Confidence: {int(row['confidence'])}/10\n"
        f"Winner: {esc(row['winner'])}\n"
        f"Why: {esc(row['rationale'])}\n"
        f"Key catalyst: {esc(row['key_catalyst'])}\n"
        f"Live price: {price_txt} | Day change: {change_txt}\n\n"
        f"<i>{esc(DISCLAIMER)}</i>"
    )


def summary_message(fired, analysed, mode, engine, universe):
    esc = html.escape
    lines = [
        f"📊 <b>{esc(BRAND)} — daily summary</b>",
        f"{esc(now_ist().strftime('%d %b %Y, %H:%M IST'))} · mode: {esc(mode)} · engine: {esc(engine)}",
        f"Universe {universe} · debated {analysed} · BUY signals {len(fired)}",
        "",
    ]
    if fired:
        for row in fired:
            price = row.get("price")
            change = row.get("day_change_pct")
            price_txt = f"₹{price:,.2f}" if isinstance(price, (int, float)) else "price n/a"
            change_txt = f"{change:+.2f}%" if isinstance(change, (int, float)) else "n/a"
            lines.append(
                f"🟢 <b>{esc(row['symbol'])}</b> — BUY {int(row['confidence'])}/10 "
                f"· {price_txt} ({change_txt})"
            )
    else:
        lines.append("No BUY signals fired in this run.")
    lines += ["", f"<i>{esc(DISCLAIMER)}</i>"]
    return "\n".join(lines)


# ==========================================================================
# the cycle
# ==========================================================================

def _avg(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else None


def _verdict_row(evidence, result, threshold):
    verdict = result["verdict"]
    price = (evidence.get("price") or {}).get("live")
    change = (evidence.get("price") or {}).get("day_change_pct")
    fired = verdict["verdict"] == "BUY" and verdict["confidence"] >= threshold

    return {
        "symbol": evidence.get("symbol"),
        "name": evidence.get("name"),
        "cap_segment": evidence.get("cap_segment"),
        "sector": evidence.get("sector"),
        "verdict": verdict["verdict"],
        "confidence": verdict["confidence"],
        "winner": verdict["winner"],
        "rationale": verdict["rationale"],
        "key_catalyst": verdict["key_catalyst"],
        "bull_score": verdict["bull_score"],
        "bear_score": verdict["bear_score"],
        "net": verdict["net"],
        "price": price,
        "day_change_pct": change,
        "fired": fired,
        "engine": result.get("engine"),
        "fallback_reason": result.get("fallback_reason"),
        "ungrounded_numbers": result.get("ungrounded_numbers") or [],
        "data_gaps": evidence.get("data_gaps") or [],
        "scores": result.get("scores") or {},
        "evidence": evidence,
        "at": stamp(),
    }


def run_cycle(mode):
    threshold = env_int("CONFIDENCE_THRESHOLD", 7)
    shortlist_per_bucket = env_int("SHORTLIST_PER_BUCKET", 4)
    provider = llm.detect_provider()
    engine_label = provider.get("label", "deterministic")
    run_id = None

    try:
        with LOCK:
            STATE["status"] = "running"
            STATE["mode"] = mode
            STATE["engine"] = engine_label
            STATE["engine_reason"] = provider.get("reason", "")
            STATE["started_at"] = now_ist().isoformat()
            STATE["telegram"] = {"configured": telegram_configured(), "sent": 0, "error": None}

        run_id = db_start_run(mode, engine_label)
        with LOCK:
            STATE["run_id"] = run_id

        log(f"run #{run_id} started · mode={mode} · engine={engine_label} "
            f"({provider.get('reason')}) · BUY threshold {threshold}/10")

        # ---- Scout --------------------------------------------------------
        set_agent("scout", status="working")
        pace()
        if mode == "live":
            universe = data_sources.load_universe()
            log(f"universe.json: {data_sources.universe_size(universe)} tickers "
                f"across {len(universe)} buckets")
            universe_count, shortlist = data_sources.scan_live(
                universe, shortlist_per_bucket, log=log)
        else:
            bundles, shortlist = data_sources.scan_demo(shortlist_per_bucket, log=log)
            universe_count = len(bundles)

        if not shortlist:
            raise RuntimeError(
                "no evidence bundles were produced — "
                + ("check your network / universe.json" if mode == "live"
                   else "demo_data/ is empty")
            )

        with LOCK:
            STATE["data_ts"] = data_sources.now_ist_str()
        set_agent("scout", status="done", stat1=universe_count, stat2=len(shortlist))
        set_kpi(universe=universe_count, in_debate=len(shortlist))
        log(f"scout shortlisted {len(shortlist)} of {universe_count}: "
            + ", ".join(b["symbol"] for b in shortlist))
        pace()

        # ---- Technician ---------------------------------------------------
        set_agent("technician", status="working")
        rvols = []
        for index, bundle in enumerate(shortlist, start=1):
            rvol = (bundle.get("technicals") or {}).get("rvol")
            if rvol is not None:
                rvols.append(rvol)
            avg = _avg(rvols)
            set_agent("technician", stat1=index,
                      stat2=f"{avg:.2f}x" if avg is not None else "n/a")
            pace(0.12)
        set_agent("technician", status="done")
        log(f"technician read {len(shortlist)} charts · "
            f"{len(shortlist) - len(rvols)} without usable RVOL")
        pace()

        # ---- Fundamentalist -----------------------------------------------
        set_agent("fundamentalist", status="working")
        upsides, covered = [], 0
        for bundle in shortlist:
            upside = (bundle.get("analyst") or {}).get("upside_pct")
            if upside is not None:
                upsides.append(upside)
                covered += 1
            avg = _avg(upsides)
            set_agent("fundamentalist", stat1=covered,
                      stat2=f"{avg:+.1f}%" if avg is not None else "n/a")
            pace(0.12)
        set_agent("fundamentalist", status="done")
        log(f"fundamentalist covered {covered}/{len(shortlist)} with analyst targets "
            f"(feed carries no P/E or ROE — target-based view only)")
        pace()

        # ---- Newsdesk ------------------------------------------------------
        set_agent("newsdesk", status="working")
        headlines, tone = 0, 0
        for bundle in shortlist:
            news = bundle.get("news") or {}
            headlines += int(news.get("total") or 0)
            tone += int(news.get("net_tone") or 0)
            set_agent("newsdesk", stat1=headlines, stat2=f"{tone:+d}")
            pace(0.12)
        set_agent("newsdesk", status="done")
        log(f"newsdesk scored {headlines} headlines · net tone {tone:+d}")
        pace()

        # ---- Bull / Bear / Judge -------------------------------------------
        # One combined debate call per stock produces all three seats at once;
        # the panel is then revealed in pipeline order so the board reads like
        # a real hand-off.
        set_agent("bull", status="working")
        results = []
        for index, bundle in enumerate(shortlist, start=1):
            result = llm.evaluate(bundle, provider=provider, log=log)
            results.append((bundle, result))
            if result.get("fallback_reason") and result.get("engine") == scoring.ENGINE_NAME:
                with LOCK:
                    if STATE["engine"] != scoring.ENGINE_NAME:
                        STATE["engine"] = f"{scoring.ENGINE_NAME} (fallback)"
                        engine_label = STATE["engine"]
            avg = _avg([r["scores"]["bull"]["score"] for _b, r in results])
            set_agent("bull", stat1=index, stat2=f"{avg:.0f}" if avg is not None else "n/a")
        set_agent("bull", status="done")
        log(f"bull argued {len(results)} cases")
        pace()

        set_agent("bear", status="working")
        for index, (_bundle, result) in enumerate(results, start=1):
            avg = _avg([r["scores"]["bear"]["score"] for _b, r in results[:index]])
            set_agent("bear", stat1=index, stat2=f"{avg:.0f}" if avg is not None else "n/a")
            pace(0.12)
        set_agent("bear", status="done")
        log(f"bear argued {len(results)} cases")
        pace()

        set_agent("judge", status="working")
        rows, buys = [], 0
        for index, (bundle, result) in enumerate(results, start=1):
            row = _verdict_row(bundle, result, threshold)
            rows.append(row)
            if row["verdict"] == "BUY":
                buys += 1

            with LOCK:
                STATE["verdicts"].insert(0, _public_verdict(row))
            db_save_verdict(run_id, row)

            set_agent("judge", stat1=index, stat2=buys)
            set_kpi(buy_signals=sum(1 for r in rows if r["fired"]),
                    top_pick=_top_pick(rows))
            log(f"judge · {row['symbol']}: {row['verdict']} {row['confidence']}/10 "
                f"(bull {row['bull_score']} vs bear {row['bear_score']}) — {row['rationale']}")
            pace(0.25)
        set_agent("judge", status="done")
        pace()

        # ---- Messenger ------------------------------------------------------
        set_agent("messenger", status="working", stat2=engine_label)
        fired = [r for r in rows if r["fired"]]
        sent, errors = 0, []

        if not telegram_configured():
            errors.append("Telegram not configured — set TELEGRAM_BOT_TOKEN and "
                          "TELEGRAM_CHAT_ID in .env")
            log(errors[-1])
        else:
            for row in fired:
                ok, err = send_telegram(buy_message(row))
                if ok:
                    sent += 1
                    log(f"telegram: BUY signal sent for {row['symbol']}")
                else:
                    errors.append(err)
                    log(f"telegram: failed for {row['symbol']} — {err}")
                set_agent("messenger", stat1=sent)
                pace(0.2)

            ok, err = send_telegram(
                summary_message(fired, len(rows), mode, engine_label, universe_count))
            if ok:
                sent += 1
                log("telegram: daily summary sent")
            else:
                errors.append(err)
                log(f"telegram: summary failed — {err}")

        set_agent("messenger", status="done", stat1=sent, stat2=engine_label)
        with LOCK:
            STATE["telegram"] = {
                "configured": telegram_configured(),
                "sent": sent,
                "error": scrub(errors[0]) if errors else None,
            }

        # ---- close out -------------------------------------------------------
        top = _top_pick(rows)
        set_kpi(buy_signals=len(fired), top_pick=top)
        db_finish_run(
            run_id, universe=universe_count, shortlisted=len(rows),
            buy_signals=len(fired), top_symbol=top.get("symbol"),
            top_confidence=top.get("confidence"), telegram_sent=sent,
            engine=engine_label, status="done",
            notes=scrub("; ".join(errors)[:500]) if errors else None,
        )

        with LOCK:
            STATE["status"] = "done"
            STATE["engine"] = engine_label
            STATE["finished_at"] = now_ist().isoformat()
        log(f"run #{run_id} complete · {len(rows)} verdicts · {len(fired)} BUY signals "
            f"· {sent} Telegram message(s)")

    except Exception as exc:                                       # noqa: BLE001
        message = scrub(f"{type(exc).__name__}: {exc}")
        log(f"run failed — {message}")
        db_finish_run(run_id, status="error", notes=message[:500])
        with LOCK:
            STATE["status"] = "error"
            STATE["error"] = message
            STATE["finished_at"] = now_ist().isoformat()


def _top_pick(rows):
    buys = [r for r in rows if r["verdict"] == "BUY"]
    pool = buys or rows
    if not pool:
        return {"symbol": None, "confidence": None}
    best = max(pool, key=lambda r: (r["confidence"], r["net"]))
    return {"symbol": best["symbol"], "confidence": best["confidence"],
            "verdict": best["verdict"]}


def _public_verdict(row):
    """The slice of a verdict the dashboard is allowed to see."""
    return {
        "symbol": row["symbol"],
        "name": row["name"],
        "cap_segment": row["cap_segment"],
        "sector": row["sector"],
        "verdict": row["verdict"],
        "confidence": row["confidence"],
        "winner": row["winner"],
        "why": row["rationale"],
        "key_catalyst": row["key_catalyst"],
        "bull_score": row["bull_score"],
        "bear_score": row["bear_score"],
        "net": row["net"],
        "price": row["price"],
        "day_change_pct": row["day_change_pct"],
        "fired": row["fired"],
        "engine": row["engine"],
        "data_gaps": len(row["data_gaps"]),
        "ungrounded": len(row["ungrounded_numbers"]),
        "at": row["at"],
    }


# ==========================================================================
# routes
# ==========================================================================

app = Flask(__name__)


@app.after_request
def _no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    with open(DASHBOARD, "r", encoding="utf-8") as fh:
        return Response(fh.read(), mimetype="text/html")


@app.get("/config")
def config():
    provider = llm.detect_provider()
    try:
        universe = data_sources.load_universe()
        counts = {bucket: len(entries) for bucket, entries in universe.items()}
        total = data_sources.universe_size(universe)
    except Exception as exc:                                       # noqa: BLE001
        counts, total = {}, 0
        log(f"universe.json could not be read: {type(exc).__name__}")

    return jsonify({
        "brand": BRAND,
        "agents": [
            {"id": a[0], "name": a[1], "role": a[2], "stat1": a[3], "stat2": a[4]}
            for a in AGENT_SPECS
        ],
        "agent_count": len(AGENT_SPECS),
        "modes": ["demo", "live"],
        "engine": provider.get("label"),
        "engine_provider": provider.get("provider"),
        "engine_reason": provider.get("reason"),
        "confidence_threshold": env_int("CONFIDENCE_THRESHOLD", 7),
        "shortlist_per_bucket": env_int("SHORTLIST_PER_BUCKET", 4),
        "telegram_configured": telegram_configured(),
        "universe": {"total": total, "buckets": counts},
        "demo_bundles": len(data_sources.load_demo_bundles()),
        "db": os.path.basename(DB_PATH),
    })


@app.post("/start")
def start():
    global WORKER

    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "demo").strip().lower()
    if mode not in ("demo", "live"):
        return jsonify({"ok": False, "error": f"unknown mode {mode!r}"}), 400

    with LOCK:
        if STATE.get("status") == "running":
            return jsonify({"ok": False, "error": "a run is already in progress"}), 409
        STATE.clear()
        STATE.update(fresh_state())
        STATE["mode"] = mode

    WORKER = threading.Thread(target=run_cycle, args=(mode,),
                              name="agent-cycle", daemon=True)
    WORKER.start()
    return jsonify({"ok": True, "mode": mode})


@app.get("/status")
def status():
    with LOCK:
        return jsonify(json.loads(json.dumps(STATE, default=str)))


# ==========================================================================
# entry point
# ==========================================================================

def main():
    # Windows terminals still default to a legacy code page; the banner and the
    # log carry ₹ and ·, so ask for UTF-8 where the runtime supports it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    init_db()
    with LOCK:
        STATE.clear()
        STATE.update(fresh_state())

    provider = llm.detect_provider()
    url = f"http://127.0.0.1:{PORT}"

    print("=" * 68)
    print(f"  {BRAND} · Indian stock analysis · {len(AGENT_SPECS)} agents on duty")
    print("=" * 68)
    print(f"  engine    : {provider['label']}  ({provider['reason']})")
    print(f"  telegram  : {'configured' if telegram_configured() else 'NOT configured — see .env.example'}")
    print(f"  audit db  : {DB_PATH}")
    print(f"  dashboard : {url}")
    print("  analysis only — this app never places an order")
    print("=" * 68, flush=True)

    # NO_BROWSER=1 keeps the tab from opening (handy for headless testing)
    if not os.environ.get("WERKZEUG_RUN_MAIN") and env_str("NO_BROWSER") not in ("1", "true"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
