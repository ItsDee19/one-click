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
import fundamentals
import history
import llm
import market
import portfolio
import research
import scheduler as scheduler_mod
import scoring
import sectors

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


def risk_limits(capital=None):
    """
    The risk rules, rebuilt each run so .env edits take effect.

    `capital` overrides the .env default for a single run. The percentages are
    what stay fixed: 1% of Rs 20,000 and 1% of Rs 2,00,000 are different rupee
    amounts but the same discipline, so a different sum each day changes the
    sizes without changing the rules.
    """
    return portfolio.RiskLimits(
        capital=capital if capital else env_float("PAPER_CAPITAL", 100000),
        risk_per_trade_pct=env_float("RISK_PER_TRADE_PCT", 1.0),
        max_position_pct=env_float("MAX_POSITION_PCT", 20.0),
        max_concurrent=env_int("MAX_CONCURRENT_POSITIONS", 5),
        max_daily_loss_pct=env_float("MAX_DAILY_LOSS_PCT", 3.0),
        max_daily_trades=env_int("MAX_DAILY_TRADES", 6),
        max_sector_pct=env_float("MAX_SECTOR_PCT", 40.0),
        cash_reserve_pct=env_float("CASH_RESERVE_PCT", 20.0),
        slippage_pct=env_float("SLIPPAGE_PCT", 0.05),
    )


def paper_enabled(mode=None):
    """
    Paper trading is only meaningful against real prices.

    Demo bundles carry frozen, illustrative figures, so a position "opened" at
    a demo price and then marked against the live tape produces a P&L that
    measures nothing but the gap between the two. Demo mode still produces
    verdicts and sizing previews; it just does not book them.
    """
    if env_str("PAPER_TRADING", "1") in ("0", "false", "no"):
        return False
    return mode != "demo"


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
        "scoreboard": {},
        "scoreboard_line": "",
        "calibration": {},
        "calibration_line": "",
        "concentration": None,
        "portfolio": {},
        "sector_heat": {},
        "orderbook": {},
        "capital": None,
        "agents": fresh_agents(),
        "verdicts": [],
        "log": [],
        "telegram": {"configured": telegram_configured(), "sent": 0, "error": None},
        "error": None,
    }


LOCK = threading.RLock()
STATE = {}
WORKER = None
SCHEDULER = None


def telegram_configured():
    return bool(env_str("TELEGRAM_BOT_TOKEN") and env_str("TELEGRAM_CHAT_ID"))


# --------------------------------------------------------------------------
# state mutation helpers (always under LOCK)
# --------------------------------------------------------------------------

def log(message):
    line = f"[{stamp()}] {scrub(message)}"
    with LOCK:
        # the scheduler logs between runs, when STATE may be bare
        STATE.setdefault("log", []).append(line)
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
        history.init(conn)
        portfolio.init(conn)


def db_start_run(mode, engine):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, mode, engine, status) VALUES (?,?,?,?)",
            (now_ist().isoformat(), mode, engine, "running"),
        )
        return cur.lastrowid


def db_save_verdict(run_id, row):
    """One audit row per stock per horizon, each with the evidence behind it.

    Returns {track: verdict_id} so a fired signal can be followed afterwards.
    """
    saved = {}
    evidence_json = json.dumps(row.get("evidence") or {}, default=str)
    scores_json = json.dumps(row.get("scores") or {})
    gaps_json = json.dumps(row.get("data_gaps") or [])

    with db() as conn:
        for track_name, track in (row.get("tracks") or {}).items():
            cursor = conn.execute(
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
            saved[track_name] = cursor.lastrowid
            if track.get("fired"):
                history.record_signal(conn, cursor.lastrowid, run_id,
                                      row, track_name, track)
    return saved


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

    sizing = track.get("sizing") or {}
    if sizing.get("qty"):
        lines.append(
            f"Size: {sizing['qty']} sh ≈ ₹{sizing['value']:,.0f} "
            f"· risking ₹{sizing['risk_rupees']:,.0f} "
            f"({sizing['risk_pct_of_capital']}% of capital)")
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

    cluster = concentration(intraday + positional)
    if cluster:
        lines += ["", f"⚠️ {esc(cluster)}"]
    lines += ["", f"<i>{esc(DISCLAIMER)}</i>"]
    return "\n".join(lines)


# ==========================================================================
# the cycle
# ==========================================================================

def _avg(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else None


def _bars_since(ticker, since_iso):
    """Daily OHLC for one ticker since a signal fired — used to settle outcomes."""
    import yfinance as yf

    try:
        fired = datetime.fromisoformat(since_iso)
    except (TypeError, ValueError):
        return []

    days = max(2, (now_ist() - fired).days + 2)
    frame = yf.Ticker(ticker).history(period=f"{min(days, 365)}d", interval="1d")
    if frame is None or getattr(frame, "empty", True):
        return []

    bars = []
    for stamp_index, row in frame.iterrows():
        try:
            when = stamp_index.to_pydatetime()
        except AttributeError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=IST)
        if when < fired:
            continue          # only bars *after* the signal count
        bars.append({
            "date": when.isoformat(),
            "high": float(row.get("High")) if row.get("High") == row.get("High") else None,
            "low": float(row.get("Low")) if row.get("Low") == row.get("Low") else None,
            "close": float(row.get("Close")) if row.get("Close") == row.get("Close") else None,
        })
    return bars


def _quotes_for(symbols):
    """Current price/high/low for open paper positions."""
    if not symbols:
        return {}
    try:
        import yfinance as yf
        tickers = [f"{s}.NS" for s in symbols]
        frame = yf.download(" ".join(tickers), period="1d", interval="5m",
                            group_by="ticker", progress=False, threads=True,
                            auto_adjust=False, actions=False)
    except Exception as exc:                                       # noqa: BLE001
        log(f"paper: could not refresh prices ({type(exc).__name__})")
        return {}

    phase = market.describe()
    out = {}
    for symbol, ticker in zip(symbols, tickers):
        try:
            bars = frame[ticker] if len(tickers) > 1 else frame
            closes = [c for c in bars["Close"].tolist() if c == c]
            highs = [h for h in bars["High"].tolist() if h == h]
            lows = [l for l in bars["Low"].tolist() if l == l]
        except Exception:                                          # noqa: BLE001
            continue
        if not closes:
            continue
        out[symbol] = {"price": round(float(closes[-1]), 2),
                       "high": round(float(max(highs)), 2) if highs else None,
                       "low": round(float(min(lows)), 2) if lows else None,
                       "live_session": phase["live_session"]}
    return out


def mark_paper_book(limits, mode=None):
    """Settle stops, targets and session exits before anything new is opened."""
    if not paper_enabled(mode):
        return {}
    try:
        with db() as conn:
            live = portfolio.open_positions(conn)
            quotes = _quotes_for(sorted({p["symbol"] for p in live}))
            result = portfolio.mark_to_market(conn, limits, quotes, log=log)
            summary = portfolio.account_summary(conn, limits, quotes)
        if result.get("closed"):
            log(f"paper: {result['closed']} position(s) closed, "
                f"realised Rs {result['realised']:,.0f} this pass")
        return summary
    except Exception as exc:                                       # noqa: BLE001
        log(f"paper book skipped ({type(exc).__name__}: {scrub(exc)})")
        return {}


def review_outcomes():
    """
    Settle every open signal before a new run judges anything.

    This is what turns the audit trail into a feedback loop: past calls are
    marked as having reached their objective, broken their invalidation, or
    expired, and the resulting hit rate is shown on the board and handed to
    the panel.
    """
    try:
        with db() as conn:
            summary = history.resolve(conn, _bars_since, log=log)
            board = history.scoreboard(conn)
        if summary["checked"]:
            log(f"reviewed {summary['checked']} open signal(s), "
                f"{summary['resolved']} settled · {history.scoreboard_line(board)}")
        return board
    except Exception as exc:                                       # noqa: BLE001
        log(f"outcome review skipped ({type(exc).__name__}: {scrub(exc)})")
        return {}


def _verdict_row(evidence, result, threshold):
    """One analysed stock, carrying both horizons."""
    price = (evidence.get("price") or {}).get("live")
    change = (evidence.get("price") or {}).get("day_change_pct")

    symbol = evidence.get("symbol")
    cooldown_days = env_int("SIGNAL_COOLDOWN_DAYS", 5)

    tracks = {}
    for name in ("intraday", "positional"):
        track = dict((result.get("tracks") or {}).get(name) or {})
        confidence = track.get("confidence")
        qualifies = (track.get("verdict") == "BUY"
                     and confidence is not None
                     and confidence >= threshold)

        # A BUY that is already on does not need firing again. Without this
        # the same position is re-sent to Telegram every morning it still
        # qualifies, which reads as five signals instead of one.
        if qualifies:
            try:
                with db() as conn:
                    held = history.in_cooldown(conn, symbol, name, cooldown_days)
            except Exception:                                      # noqa: BLE001
                held = None
            if held:
                qualifies = False
                track["suppressed"] = held
                log(f"{symbol} [{name}]: BUY not re-sent — {held}")

        track["fired"] = qualifies
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


def concentration(fired):
    """
    Are the fired signals really independent bets?

    Five BUYs in one sector is one bet in five envelopes, and no amount of
    per-stock analysis can see it — each stock is judged alone. This looks at
    the batch and says so when it clusters.
    """
    if len(fired) < 2:
        return None

    sectors = {}
    for row, _track, _data in fired:
        sector = row.get("sector") or "unclassified"
        sectors.setdefault(sector, set()).add(row["symbol"])

    biggest, symbols = max(sectors.items(), key=lambda kv: len(kv[1]))
    share = len(symbols) / len({r["symbol"] for r, _t, _d in fired})

    if len(symbols) >= 2 and share >= 0.6:
        return (f"{len(symbols)} of the fired names are {biggest} "
                f"({', '.join(sorted(symbols))}) — these are correlated, "
                f"not independent positions")
    return None


def _fired(rows, track=None):
    """Every track that cleared the confidence bar, as (row, track_name, track)."""
    out = []
    for row in rows:
        for name, data in row["tracks"].items():
            if data.get("fired") and (track is None or name == track):
                out.append((row, name, data))
    return out


def run_cycle(mode, capital=None):
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
        if capital:
            log(f"sizing this run against Rs {capital:,.0f} — "
                f"Rs {capital * env_float('RISK_PER_TRADE_PCT', 1.0) / 100:,.0f} risked per trade, "
                f"Rs {capital * env_float('MAX_DAILY_LOSS_PCT', 3.0) / 100:,.0f} daily stop")

        # settle yesterday's calls, and the paper book, before making today's
        limits = risk_limits(capital)
        if mode == "demo" and env_str("PAPER_TRADING", "1") not in ("0", "false", "no"):
            log("paper trading idle in demo mode — demo prices are frozen, so a "
                "simulated fill against them would measure nothing. Use live mode.")
        book = mark_paper_book(limits, mode)
        if book:
            with LOCK:
                STATE["portfolio"] = book
            log(f"paper account: equity Rs {book['equity']:,.0f} "
                f"({book['total_return_pct']:+.2f}%), {len(book['open_positions'])} open, "
                f"day P&L Rs {book['day_pnl']:,.0f}"
                + (f" — HALTED: {book['halted']['reason']}" if book.get("halted") else ""))

        board = review_outcomes()
        board_line = history.scoreboard_line(board) if board else ""
        try:
            with db() as conn:
                calib = history.calibration(conn)
            calib_line = history.calibration_line(calib)
        except Exception:                                          # noqa: BLE001
            calib, calib_line = {}, ""
        with LOCK:
            STATE["scoreboard"] = board
            STATE["scoreboard_line"] = board_line
            STATE["calibration"] = calib
            STATE["calibration_line"] = calib_line

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

        # Sector heatmap + book-to-sales screen. Both run off data the scan
        # already fetched, so they cost one extra request between them.
        if mode == "live":
            try:
                heat = sectors.heatmap(data_sources.LAST_QUOTES, log=log)
                with LOCK:
                    STATE["sector_heat"] = heat
            except Exception as exc:                               # noqa: BLE001
                log(f"sector heatmap skipped ({type(exc).__name__})")

        try:
            universe_for_screen = (data_sources.load_universe()
                                   if mode == "live" else {})
            if universe_for_screen:
                book = fundamentals.screen(universe_for_screen, log=log)
                with LOCK:
                    STATE["orderbook"] = book
        except Exception as exc:                                   # noqa: BLE001
            log(f"book-to-sales screen skipped ({type(exc).__name__}: {scrub(exc)})")

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
        web_on = env_str("WEB_RESEARCH", "1") not in ("0", "false", "no")

        if web_on:
            # Public RSS, sanitised. Everything fetched is treated as data:
            # research.gather strips instruction-like text and flags it.
            stripped = 0
            for bundle in shortlist:
                try:
                    web = research.gather(bundle["symbol"], bundle.get("name") or bundle["symbol"],
                                          data_sources.score_headline, log=log)
                    bundle["news"] = research.merge_into_news(bundle.get("news") or {}, web)
                    bundle["web_research"] = {
                        "source": web["source"], "trust": web["trust"],
                        "fetched_at": web["fetched_at"], "added": bundle["news"].get("web_added", 0),
                    }
                    stripped += web.get("injection_attempts_stripped", 0)
                except Exception as exc:                           # noqa: BLE001
                    log(f"research: {bundle['symbol']} skipped ({type(exc).__name__})")
            log(f"newsdesk pulled public RSS for {len(shortlist)} names"
                + (f" — {stripped} instruction-like pattern(s) stripped" if stripped else ""))

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
            pending = {}
            for position, bundle in enumerate(shortlist):
                try:
                    with db() as conn:
                        recall = history.memory_for(conn, bundle.get("symbol"))
                except Exception:                                  # noqa: BLE001
                    recall = {}
                pending[pool.submit(llm.evaluate, bundle, provider=provider,
                                    log=log, memory=recall,
                                    scoreboard_line=board_line,
                                    calibration_line=calib_line)] = position
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
            verdict_ids = db_save_verdict(run_id, row)

            # What this signal means for today's amount. Computed for every
            # fired signal regardless of mode, because "how much" is the
            # question a verdict on its own never answers.
            for name, track in row["tracks"].items():
                if not track.get("fired"):
                    continue
                levels = track.get("levels") or {}
                track["sizing"] = portfolio.size_position(
                    limits, row.get("price"), levels.get("invalidation"),
                    cash_available=limits.deployable)

            # A fired signal becomes a simulated position, subject to every
            # risk gate. No real order is placed — see portfolio.py.
            if paper_enabled(mode):
                for name, track in row["tracks"].items():
                    if not track.get("fired"):
                        continue
                    try:
                        with db() as conn:
                            outcome = portfolio.open_paper_position(
                                conn, limits, run_id, verdict_ids.get(name),
                                row, name, track, log=log)
                        track["paper"] = outcome
                    except Exception as exc:                       # noqa: BLE001
                        log(f"paper: {row['symbol']} sizing failed "
                            f"({type(exc).__name__}: {scrub(exc)})")

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

        cluster = concentration(fired)
        if cluster:
            log(f"concentration: {cluster}")
        with LOCK:
            STATE["concentration"] = cluster
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
        if paper_enabled(mode):
            try:
                with db() as conn:
                    with LOCK:
                        STATE["portfolio"] = portfolio.account_summary(conn, limits)
            except Exception:                                      # noqa: BLE001
                pass

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
        "suppressed": track.get("suppressed"),
        "risk_reward": track.get("risk_reward") or {},
        "sizing": track.get("sizing") or {},
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
        "rel_day_change_pct": ((row.get("evidence") or {}).get("relative") or {})
                              .get("rel_day_change_pct"),
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
        "scheduler": SCHEDULER.status() if SCHEDULER else {"enabled": False},
        "signal_cooldown_days": env_int("SIGNAL_COOLDOWN_DAYS", 5),
        "paper_trading": paper_enabled(),
        "web_research": env_str("WEB_RESEARCH", "1") not in ("0", "false", "no"),
        "risk": risk_limits().as_dict(),
        "executes_orders": False,
    })


def begin_run(mode, trigger="manual", capital=None):
    """
    Start a cycle. Shared by the button and the scheduler.

    Returns (ok, message) so an unattended trigger can log why it was skipped
    rather than silently doing nothing.
    """
    global WORKER

    if mode not in ("demo", "live"):
        return False, f"unknown mode {mode!r}"

    with LOCK:
        if STATE.get("status") == "running":
            return False, "a run is already in progress"
        STATE.clear()
        STATE.update(fresh_state())
        STATE["mode"] = mode
        STATE["trigger"] = trigger
        STATE["capital"] = capital

    WORKER = threading.Thread(target=run_cycle, args=(mode, capital),
                              name="agent-cycle", daemon=True)
    WORKER.start()
    return True, "started"


@app.post("/start")
def start():
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "demo").strip().lower()

    capital = None
    if payload.get("capital") not in (None, "", 0):
        try:
            capital = float(payload["capital"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "capital must be a number"}), 400
        if capital <= 0:
            return jsonify({"ok": False, "error": "capital must be positive"}), 400

    ok, message = begin_run(mode, trigger="manual", capital=capital)
    if not ok:
        code = 400 if message.startswith("unknown mode") else 409
        return jsonify({"ok": False, "error": message}), code
    return jsonify({"ok": True, "mode": mode, "capital": capital})


@app.get("/scheduler")
def scheduler_status():
    return jsonify(SCHEDULER.status() if SCHEDULER else {"enabled": False})


@app.get("/sectors")
def sectors_route():
    """Sector heatmap: index moves plus breadth across the universe."""
    with LOCK:
        heat = STATE.get("sector_heat") or {}
    if heat:
        return jsonify(heat)
    try:
        quotes, _bench = data_sources.fetch_quotes(data_sources.load_universe(), log=log)
        return jsonify(sectors.heatmap(quotes, log=log))
    except Exception as exc:                                       # noqa: BLE001
        return jsonify({"error": scrub(f"{type(exc).__name__}: {exc}")}), 500


@app.get("/orderbook")
def orderbook_route():
    """Stocks whose order book exceeds their latest quarterly sales."""
    try:
        return jsonify(fundamentals.screen(data_sources.load_universe(), log=log))
    except Exception as exc:                                       # noqa: BLE001
        return jsonify({"error": scrub(f"{type(exc).__name__}: {exc}")}), 500


@app.get("/portfolio")
def portfolio_route():
    """The paper account. No real positions exist; nothing here is executable."""
    limits = risk_limits()
    try:
        with db() as conn:
            live = portfolio.open_positions(conn)
            quotes = _quotes_for(sorted({p["symbol"] for p in live})) if live else {}
            summary = portfolio.account_summary(conn, limits, quotes)
            closed = [dict(r) for r in conn.execute(
                """SELECT symbol, track, qty, fill_price, exit_price, exit_reason,
                          gross_pnl, costs, net_pnl, net_pnl_pct, opened_at, closed_at
                   FROM paper_positions WHERE status='closed'
                   ORDER BY closed_at DESC LIMIT 30""")]
    except Exception as exc:                                       # noqa: BLE001
        return jsonify({"error": scrub(f"{type(exc).__name__}: {exc}")}), 500

    summary["closed"] = closed
    summary["mode"] = ("paper" if env_str("PAPER_TRADING", "1")
                       not in ("0", "false", "no") else "disabled")
    summary["books_trades_in"] = "live mode only"
    summary["disclaimer"] = ("Simulated account. No broker is connected and no "
                             "real order is ever placed.")
    return jsonify(summary)


@app.get("/scoreboard")
def scoreboard_route():
    """The desk's own record: what past signals actually did."""
    try:
        with db() as conn:
            board = history.scoreboard(conn)
            calib = history.calibration(conn)
            open_rows = history.open_signals(conn)
            settled = [dict(r) for r in conn.execute(
                """SELECT symbol, track, status, return_pct, fired_at, resolved_at,
                          entry_price, exit_price
                   FROM outcomes WHERE status != 'open'
                   ORDER BY resolved_at DESC LIMIT 25""")]
    except Exception as exc:                                       # noqa: BLE001
        return jsonify({"error": scrub(f"{type(exc).__name__}: {exc}")}), 500

    return jsonify({
        "scoreboard": board,
        "summary": history.scoreboard_line(board),
        "calibration": calib,
        "calibration_summary": history.calibration_line(calib),
        "open": [{k: r[k] for k in ("symbol", "track", "fired_at", "entry_price",
                                    "objective", "invalidation",
                                    "max_favourable_pct", "max_adverse_pct")}
                 for r in open_rows],
        "settled": settled,
    })


@app.get("/status")
def status():
    with LOCK:
        snapshot = json.loads(json.dumps(STATE, default=str))
    snapshot["scheduler"] = SCHEDULER.status() if SCHEDULER else {"enabled": False}
    snapshot["market"] = market.describe()
    return jsonify(snapshot)


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

    global SCHEDULER
    SCHEDULER = scheduler_mod.Scheduler(
        times=env_str("SCHEDULE_TIMES", scheduler_mod.DEFAULT_TIMES),
        trigger=lambda mode: begin_run(mode, trigger="scheduled"),
        mode=env_str("SCHEDULE_MODE", "live"),
        log=log,
        enabled=env_str("SCHEDULE_ENABLED", "1") not in ("0", "false", "no"),
    )

    provider = llm.detect_provider()
    url = f"http://127.0.0.1:{PORT}"

    print("=" * 68)
    print(f"  {BRAND} · Indian stock analysis · {len(AGENT_SPECS)} agents on duty")
    print("=" * 68)
    print(f"  engine    : {provider['label']}  ({provider['reason']})")
    print(f"  telegram  : {'configured' if telegram_configured() else 'NOT configured — see .env.example'}")
    print(f"  audit db  : {DB_PATH}")
    print(f"  schedule  : "
          f"{SCHEDULER.pretty_times()} IST ({SCHEDULER.mode} mode)"
          if SCHEDULER.enabled else "  schedule  : disabled")
    print(f"  dashboard : {url}")
    print("  analysis only — this app never places an order")
    print("=" * 68, flush=True)

    # NO_BROWSER=1 keeps the tab from opening (handy for headless testing)
    if not os.environ.get("WERKZEUG_RUN_MAIN") and env_str("NO_BROWSER") not in ("1", "true"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    with LOCK:
        STATE.setdefault("log", [])
    SCHEDULER.start()

    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
