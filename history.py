"""
history.py — the desk's memory.

Every run used to start from zero: the panel wrote verdicts into SQLite and
never read one back. It could fire the same BUY five mornings running and had
no idea whether any previous call had worked.

This module closes that loop:

  * outcomes  — every fired signal is followed until it reaches its objective,
                breaks its invalidation, or runs out of horizon. Resolved
                against daily OHLC, so an intraday spike through the level
                counts, not just the close.
  * scoreboard— the resulting hit rate, per track. Shown on the dashboard and
                fed to the panel so it can see its own record.
  * memory    — the last verdict on a stock, handed to the debate as context.
  * cooldown  — stops the same signal being re-fired (and re-sent to Telegram)
                every single morning while it is still open.

Nothing here predicts anything. It only records what already happened.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# A positional signal is followed for its own horizon; an intraday one dies at
# the bell, so it is judged on the session it was fired in.
DEFAULT_POSITIONAL_DAYS = 30
INTRADAY_DAYS = 1

OPEN = "open"
HIT = "objective"
STOPPED = "invalidated"
EXPIRED = "expired"


def init(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS outcomes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            verdict_id   INTEGER NOT NULL REFERENCES verdicts(id),
            run_id       INTEGER,
            symbol       TEXT NOT NULL,
            ticker       TEXT,
            track        TEXT NOT NULL,
            fired_at     TEXT NOT NULL,
            entry_price  REAL,
            objective    REAL,
            invalidation REAL,
            horizon_days INTEGER,
            deadline     TEXT,
            status       TEXT NOT NULL DEFAULT 'open',
            resolved_at  TEXT,
            exit_price   REAL,
            return_pct   REAL,
            max_favourable_pct REAL,
            max_adverse_pct    REAL,
            bars_checked INTEGER DEFAULT 0,
            note         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_outcomes_status ON outcomes(status);
        CREATE INDEX IF NOT EXISTS idx_outcomes_symbol ON outcomes(symbol);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_outcomes_verdict ON outcomes(verdict_id);
        """
    )


def now_ist():
    return datetime.now(IST)


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def record_signal(conn, verdict_id, run_id, row, track_name, track):
    """Start following a signal that just fired. Idempotent per verdict."""
    levels = track.get("levels") or {}
    horizon = (INTRADAY_DAYS if track_name == "intraday"
               else (track.get("horizon_days_max") or DEFAULT_POSITIONAL_DAYS))
    fired = now_ist()
    # calendar days, generously padded for weekends on the positional side
    deadline = fired + timedelta(days=horizon if track_name == "intraday"
                                 else int(horizon * 1.5) + 5)

    conn.execute(
        """INSERT OR IGNORE INTO outcomes
           (verdict_id, run_id, symbol, ticker, track, fired_at, entry_price,
            objective, invalidation, horizon_days, deadline, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            verdict_id, run_id, row["symbol"],
            (row.get("evidence") or {}).get("ticker") or f"{row['symbol']}.NS",
            track_name, fired.isoformat(), row.get("price"),
            levels.get("objective"), levels.get("invalidation"),
            horizon, deadline.isoformat(), OPEN,
        ),
    )


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

def open_signals(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM outcomes WHERE status = ? ORDER BY fired_at", (OPEN,))]


def resolve(conn, fetch_bars, log=None):
    """
    Walk every open signal forward and settle the ones that finished.

    `fetch_bars(ticker, since)` returns a list of {date, high, low, close}
    dicts — injected so this module never imports yfinance and stays testable.

    A signal resolves as `objective` if the high touched the target before the
    low broke the stop, `invalidated` the other way round, and `expired` if the
    deadline passed with neither hit. When both happen on the same daily bar
    we cannot tell the order from daily data, so it is settled as invalidated:
    assuming the worse fill is the only honest choice without intraday bars.
    """
    say = log or (lambda _m: None)
    rows = open_signals(conn)
    if not rows:
        return {"checked": 0, "resolved": 0}

    resolved = 0
    for row in rows:
        try:
            bars = fetch_bars(row["ticker"], row["fired_at"])
        except Exception as exc:                                   # noqa: BLE001
            say(f"outcome: {row['symbol']} bars unavailable ({type(exc).__name__})")
            continue
        if not bars:
            continue

        entry = row.get("entry_price")
        objective = row.get("objective")
        invalidation = row.get("invalidation")
        if not entry:
            continue

        best = worst = entry
        status, exit_price, note = OPEN, None, None

        for bar in bars:
            high, low = bar.get("high"), bar.get("low")
            if high is not None:
                best = max(best, high)
            if low is not None:
                worst = min(worst, low)

            hit = objective is not None and high is not None and high >= objective
            stop = invalidation is not None and low is not None and low <= invalidation

            if hit and stop:
                status, exit_price = STOPPED, invalidation
                note = "objective and invalidation both touched on the same daily bar — settled at the stop"
                break
            if stop:
                status, exit_price = STOPPED, invalidation
                break
            if hit:
                status, exit_price = HIT, objective
                break

        if status == OPEN:
            deadline = row.get("deadline")
            past_due = deadline and now_ist().isoformat() > deadline
            if past_due:
                status = EXPIRED
                exit_price = bars[-1].get("close")
                note = "horizon elapsed without reaching either level"

        if status == OPEN:
            conn.execute(
                """UPDATE outcomes SET max_favourable_pct = ?, max_adverse_pct = ?,
                   bars_checked = ? WHERE id = ?""",
                (round((best - entry) / entry * 100, 2),
                 round((worst - entry) / entry * 100, 2), len(bars), row["id"]),
            )
            continue

        return_pct = round(((exit_price or entry) - entry) / entry * 100, 2)
        conn.execute(
            """UPDATE outcomes SET status = ?, resolved_at = ?, exit_price = ?,
               return_pct = ?, max_favourable_pct = ?, max_adverse_pct = ?,
               bars_checked = ?, note = ? WHERE id = ?""",
            (status, now_ist().isoformat(), exit_price, return_pct,
             round((best - entry) / entry * 100, 2),
             round((worst - entry) / entry * 100, 2), len(bars), note, row["id"]),
        )
        resolved += 1
        say(f"outcome: {row['symbol']} [{row['track']}] {status} "
            f"{return_pct:+.2f}% after {len(bars)} session(s)")

    return {"checked": len(rows), "resolved": resolved}


# --------------------------------------------------------------------------
# scoreboard + memory
# --------------------------------------------------------------------------

def scoreboard(conn):
    """Hit rate per track over everything that has actually settled."""
    out = {}
    for track in ("intraday", "positional"):
        row = conn.execute(
            """SELECT
                 COUNT(*) total,
                 SUM(status = ?) hits,
                 SUM(status = ?) stops,
                 SUM(status = ?) expiries,
                 AVG(return_pct) avg_return
               FROM outcomes WHERE track = ? AND status != ?""",
            (HIT, STOPPED, EXPIRED, track, OPEN),
        ).fetchone()
        settled = row["total"] or 0
        still_open = conn.execute(
            "SELECT COUNT(*) c FROM outcomes WHERE track = ? AND status = ?",
            (track, OPEN)).fetchone()["c"]

        out[track] = {
            "settled": settled,
            "open": still_open,
            "objective": row["hits"] or 0,
            "invalidated": row["stops"] or 0,
            "expired": row["expiries"] or 0,
            "hit_rate": round((row["hits"] or 0) / settled * 100, 1) if settled else None,
            "avg_return_pct": round(row["avg_return"], 2) if row["avg_return"] is not None else None,
        }
    return out


def scoreboard_line(stats):
    """One human sentence, or an honest admission that there is no record yet."""
    parts = []
    for track, data in stats.items():
        if not data["settled"]:
            continue
        parts.append(f"{track} {data['hit_rate']}% of {data['settled']} "
                     f"({data['avg_return_pct']:+.2f}% avg)")
    if not parts:
        return "no settled signals yet — the desk has no track record to show"
    return " · ".join(parts)


def last_verdict(conn, symbol, track):
    """The previous call on this stock, for the panel to be reminded of."""
    row = conn.execute(
        """SELECT v.created_at, v.verdict, v.confidence, v.price, v.rationale,
                  o.status, o.return_pct
           FROM verdicts v
           LEFT JOIN outcomes o ON o.verdict_id = v.id
           WHERE v.symbol = ? AND v.track = ?
           ORDER BY v.id DESC LIMIT 1""",
        (symbol, track),
    ).fetchone()
    return dict(row) if row else None


def memory_for(conn, symbol):
    """Both tracks' previous calls, plus any still-open position."""
    memory = {}
    for track in ("intraday", "positional"):
        previous = last_verdict(conn, symbol, track)
        if previous:
            memory[track] = previous
    return memory


def open_signal_for(conn, symbol, track):
    row = conn.execute(
        """SELECT fired_at, entry_price, objective, invalidation
           FROM outcomes WHERE symbol = ? AND track = ? AND status = ?
           ORDER BY fired_at DESC LIMIT 1""",
        (symbol, track, OPEN),
    ).fetchone()
    return dict(row) if row else None


def in_cooldown(conn, symbol, track, cooldown_days):
    """
    Should this signal be suppressed as a repeat?

    An intraday signal already fired today, or a positional signal still open
    and inside its cooldown, should not be re-sent — the position is already
    on. Returns a reason string, or None if it is free to fire.
    """
    still_open = open_signal_for(conn, symbol, track)
    if not still_open:
        return None

    try:
        fired = datetime.fromisoformat(still_open["fired_at"])
    except (TypeError, ValueError):
        return None

    age_days = (now_ist() - fired).total_seconds() / 86400.0

    if track == "intraday":
        if fired.date() == now_ist().date():
            return f"already fired intraday today at {still_open['entry_price']}"
        return None

    if age_days < cooldown_days:
        return (f"positional signal from {fired.strftime('%d %b')} still open "
                f"at {still_open['entry_price']} ({cooldown_days}d cooldown)")
    return None
