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
from concurrent import futures
import webbrowser
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request

import data_sources
import llm
import market
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
            "intraday_signals": 0, "positional_signals": 0,
            "top_pick": {"symbol": None, "confidence": None},
        },
        "market": market.describe(),
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
                track         TEXT NOT NULL,
                verdict       TEXT,
                confidence    INTEGER,
                winner        TEXT,
                rationale     TEXT,
                key_catalyst  TEXT,
                horizon       TEXT,
                horizon_days_min INTEGER,
                horizon_days_max INTEGER,
                horizon_basis TEXT,
                levels_json   TEXT,
                bull_score    INTEGER,
                bear_score    INTEGER,
                net           INTEGER,
                price         REAL,
                day_change_pct REAL,
                market_phase  TEXT,
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
    """One audit row per stock per horizon, each with the evidence behind it."""
    evidence_json = json.dumps(row.get("evidence") or {}, default=str)
    scores_json = json.dumps(row.get("scores") or {})
    gaps_json = json.dumps(row.get("data_gaps") or [])

    with db() as conn:
        for track_name, track in (row.get("tracks") or {}).items():
            conn.execute(
                """INSERT INTO verdicts
                   (run_id, created_at, symbol, name, cap_segment, sector, track,
                    verdict, confidence, winner, rationale, key_catalyst,
                    horizon, horizon_days_min, horizon_days_max, horizon_basis,
                    levels_json, bull_score, bear_score, net, price, day_change_pct,
                    market_phase, fired, engine, ungrounded, data_gaps,
                    scores_json, evidence_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, now_ist().isoformat(), row["symbol"], row["name"],
                    row["cap_segment"], row.get("sector"), track_name,
                    track.get("verdict"), track.get("confidence"), track.get("winner"),
                    track.get("rationale"), track.get("key_catalyst"),
                    track.get("horizon"), track.get("horizon_days_min"),
                    track.get("horizon_days_max"), track.get("horizon_basis"),
                    json.dumps(track.get("levels") or {}),
                    track.get("bull_score"), track.get("bear_score"), track.get("net"),
                    row.get("price"), row.get("day_change_pct"), row.get("market_phase"),
                    1 if track.get("fired") else 0, row.get("engine"),
                    len(row.get("ungrounded_numbers") or []),
                    gaps_json, scores_json, evidence_json,
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


def _money(value):
    return f"₹{value:,.2f}" if isinstance(value, (int, float)) else "data unavailable"


def _levels_line(track):
    levels = track.get("levels") or {}
    parts = []
    for label, key in (("Trigger", "trigger"), ("Invalidation", "invalidation"),
                       ("Objective", "objective")):
        value = levels.get(key)
        if isinstance(value, (int, float)):
            parts.append(f"{label} ₹{value:,.2f}")
    return " · ".join(parts)


def buy_message(row, track_name, track):
    esc = html.escape
    cap = (row.get("cap_segment") or "unknown").capitalize()
    change = row.get("day_change_pct")
    change_txt = f"{change:+.2f}%" if isinstance(change, (int, float)) else "data unavailable"

    if track_name == "intraday":
        header = f"🟢 <b>INTRADAY BUY — {esc(row['symbol'])}</b> ({esc(cap)} cap)"
        horizon = (f"Horizon: {esc(track.get('horizon') or 'same session')} "
                   f"— close before the bell")
    else:
        header = f"🔵 <b>POSITIONAL BUY — {esc(row['symbol'])}</b> ({esc(cap)} cap)"
        days_min = track.get("horizon_days_min")
        hold = f"Hold: {esc(track.get('horizon') or 'data unavailable')}"
        if days_min:
            hold += f" (minimum ~{int(days_min)} trading days)"
        horizon = f"{hold}\nBasis: {esc(track.get('horizon_basis') or '')}"

    lines = [
        header, "",
        f"Verdict: BUY | Confidence: {int(track['confidence'])}/10",
        f"Winner: {esc(track.get('winner') or '—')}",
        f"Why: {esc(track.get('rationale') or '')}",
        f"Key catalyst: {esc(track.get('key_catalyst') or '')}",
        f"Live price: {_money(row.get('price'))} | Day change: {change_txt}",
    ]
    levels = _levels_line(track)
    if levels:
        lines.append(esc(levels))
    lines += [horizon, "", f"<i>{esc(DISCLAIMER)}</i>"]
    return "\n".join(lines)


def summary_message(rows, mode, engine, universe, phase_label):
    esc = html.escape
    intraday = _fired(rows, "intraday")
    positional = _fired(rows, "positional")

    lines = [
        f"📊 <b>{esc(BRAND)} — daily summary</b>",
        f"{esc(now_ist().strftime('%d %b %Y, %H:%M IST'))} · {esc(phase_label or '')} "
        f"· mode: {esc(mode)} · engine: {esc(engine)}",
        f"Universe {universe} · debated {len(rows)} · "
        f"intraday {len(intraday)} · positional {len(positional)}",
        "",
    ]

    def block(title, fired, icon):
        if not fired:
            return [f"{title}: none fired."]
        out = [f"<b>{title}</b>"]
        for row, _name, track in fired:
            change = row.get("day_change_pct")
            change_txt = f"{change:+.2f}%" if isinstance(change, (int, float)) else "n/a"
            tail = (f" · hold {esc(track.get('horizon') or '')}"
                    if _name == "positional" else "")
            out.append(
                f"{icon} <b>{esc(row['symbol'])}</b> — {int(track['confidence'])}/10 "
                f"· {_money(row.get('price'))} ({change_txt}){tail}"
            )
        return out

    lines += block("Intraday — close before the bell", intraday, "🟢")
    lines.append("")
    lines += block("Positional — multi-week hold", positional, "🔵")

    if not intraday and not positional:
        lines += ["", "No BUY signals fired in this run."]
    lines += ["", f"<i>{esc(DISCLAIMER)}</i>"]
    return "\n".join(lines)


# ==========================================================================
# the cycle
# ==========================================================================

def _avg(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else None


def _verdict_row(evidence, result, threshold):
    """One analysed stock, carrying both horizons."""
    price = (evidence.get("price") or {}).get("live")
    change = (evidence.get("price") or {}).get("day_change_pct")

    tracks = {}
    for name in ("intraday", "positional"):
        track = dict((result.get("tracks") or {}).get(name) or {})
        confidence = track.get("confidence")
        track["fired"] = bool(
            track.get("verdict") == "BUY"
            and confidence is not None
            and confidence >= threshold
        )
        tracks[name] = track

    return {
        "symbol": evidence.get("symbol"),
        "name": evidence.get("name"),
        "cap_segment": evidence.get("cap_segment"),
        "sector": evidence.get("sector"),
        "price": price,
        "day_change_pct": change,
        "tracks": tracks,
        "market_phase": (evidence.get("market") or {}).get("label"),
        "engine": result.get("engine"),
        "fallback_reason": result.get("fallback_reason"),
        "ungrounded_numbers": result.get("ungrounded_numbers") or [],
        "data_gaps": evidence.get("data_gaps") or [],
        "scores": result.get("scores") or {},
        "evidence": evidence,
        "at": stamp(),
    }


def _fired(rows, track=None):
    """Every track that cleared the confidence bar, as (row, track_name, track)."""
    out = []
    for row in rows:
        for name, data in row["tracks"].items():
            if data.get("fired") and (track is None or name == track):
                out.append((row, name, data))
    return out


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
            STATE["market"] = market.describe()
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

        # One LLM call per stock, run a few at a time. Sequentially this is
        # ~1-2 minutes a stock and a live shortlist of twelve would keep the
        # board waiting for half an hour; the CLI spawns its own process per
        # call, so a small pool cuts the wall time without straining anything.
        workers = max(1, min(env_int("LLM_CONCURRENCY", 3), len(shortlist)))
        done_count = 0
        indexed = {}

        log(f"debating {len(shortlist)} stocks, {workers} at a time")
        with futures.ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="debate") as pool:
            pending = {
                pool.submit(llm.evaluate, bundle, provider=provider, log=log): position
                for position, bundle in enumerate(shortlist)
            }
            for future in futures.as_completed(pending):
                position = pending[future]
                bundle = shortlist[position]
                try:
                    result = future.result()
                except Exception as exc:                          # noqa: BLE001
                    log(f"{bundle.get('symbol')}: debate crashed "
                        f"({type(exc).__name__}) — using the rule engine")
                    result = scoring.evaluate(bundle)
                    result["fallback_reason"] = f"{type(exc).__name__}: {exc}"

                indexed[position] = (bundle, result)
                done_count += 1

                if result.get("fallback_reason") and result.get("engine") == scoring.ENGINE_NAME:
                    with LOCK:
                        if not str(STATE["engine"]).startswith(scoring.ENGINE_NAME):
                            STATE["engine"] = f"{engine_label} + rule fallback"
                avg = _avg([r["scores"]["bull"]["score"] for _b, r in indexed.values()])
                set_agent("bull", stat1=done_count,
                          stat2=f"{avg:.0f}" if avg is not None else "n/a")

        # restore the shortlist's own order so the board reads predictably
        results = [indexed[position] for position in sorted(indexed)]
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
            buys = sum(1 for t in row["tracks"].values() if t.get("verdict") == "BUY") + buys

            with LOCK:
                STATE["verdicts"].insert(0, _public_verdict(row))
            db_save_verdict(run_id, row)

            set_agent("judge", stat1=index, stat2=buys)
            set_kpi(buy_signals=len(_fired(rows)),
                    intraday_signals=len(_fired(rows, "intraday")),
                    positional_signals=len(_fired(rows, "positional")),
                    top_pick=_top_pick(rows))

            for name, track in row["tracks"].items():
                extra = (f" hold {track.get('horizon')}" if name == "positional"
                         else f" ({track.get('horizon')})")
                log(f"judge · {row['symbol']} [{name}]: {track.get('verdict')} "
                    f"{track.get('confidence') or '—'}/10 —{extra} {track.get('rationale')}")
            pace(0.25)
        set_agent("judge", status="done")
        pace()

        # ---- Messenger ------------------------------------------------------
        set_agent("messenger", status="working", stat2=engine_label)
        fired = _fired(rows)
        phase_label = rows[0].get("market_phase") if rows else None
        sent, errors = 0, []

        if not telegram_configured():
            errors.append("Telegram not configured — set TELEGRAM_BOT_TOKEN and "
                          "TELEGRAM_CHAT_ID in .env")
            log(errors[-1])
        else:
            for row, track_name, track in fired:
                ok, err = send_telegram(buy_message(row, track_name, track))
                if ok:
                    sent += 1
                    log(f"telegram: {track_name} BUY sent for {row['symbol']}")
                else:
                    errors.append(err)
                    log(f"telegram: failed for {row['symbol']} [{track_name}] — {err}")
                set_agent("messenger", stat1=sent)
                pace(0.2)

            ok, err = send_telegram(
                summary_message(rows, mode, engine_label, universe_count, phase_label))
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
        set_kpi(buy_signals=len(fired), top_pick=top,
                intraday_signals=len(_fired(rows, "intraday")),
                positional_signals=len(_fired(rows, "positional")))
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
    """Best call on the board, across both horizons."""
    candidates = []
    for row in rows:
        for name, track in row["tracks"].items():
            if track.get("confidence") is None:
                continue
            candidates.append((row, name, track))
    if not candidates:
        return {"symbol": None, "confidence": None}

    buys = [c for c in candidates if c[2].get("verdict") == "BUY"]
    pool = buys or candidates
    row, name, track = max(pool, key=lambda c: (c[2].get("confidence") or 0,
                                                c[2].get("net") or 0))
    return {
        "symbol": row["symbol"],
        "confidence": track.get("confidence"),
        "verdict": track.get("verdict"),
        "track": name,
        "horizon": track.get("horizon"),
    }


def _public_track(track):
    return {
        "verdict": track.get("verdict"),
        "confidence": track.get("confidence"),
        "winner": track.get("winner"),
        "why": track.get("rationale"),
        "key_catalyst": track.get("key_catalyst"),
        "horizon": track.get("horizon"),
        "horizon_days_min": track.get("horizon_days_min"),
        "horizon_days_max": track.get("horizon_days_max"),
        "horizon_basis": track.get("horizon_basis"),
        "levels": track.get("levels") or {},
        "bull_score": track.get("bull_score"),
        "bear_score": track.get("bear_score"),
        "net": track.get("net"),
        "fired": bool(track.get("fired")),
        "gated": bool(track.get("gated")),
    }


def _public_verdict(row):
    """The slice of a verdict the dashboard is allowed to see."""
    return {
        "symbol": row["symbol"],
        "name": row["name"],
        "cap_segment": row["cap_segment"],
        "sector": row["sector"],
        "price": row["price"],
        "day_change_pct": row["day_change_pct"],
        "market_phase": row.get("market_phase"),
        "tracks": {name: _public_track(t) for name, t in row["tracks"].items()},
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
        "market": market.describe(),
        "tracks": list(scoring.TRACKS),
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
