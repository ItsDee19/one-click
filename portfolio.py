"""
portfolio.py — position sizing, risk limits and a paper account.

This module answers the question "given this much capital, what would the desk
actually do?" — and then does it, on a simulated account marked against real
market data.

It places NO real orders. There is no broker integration here and no code path
that could create one. What it does provide is everything that has to be right
*before* money is ever involved:

  * risk-based sizing — how many shares follow from the capital, the entry and
    the invalidation level, rather than from a round number or a hunch
  * the safety nets — per-trade risk, position cap, concurrent positions,
    sector exposure, cash reserve, daily loss circuit breaker, trade count
  * honest costs — brokerage, STT, exchange charges, GST, stamp duty and
    slippage, because a paper account that ignores them prints profits that do
    not exist
  * a ledger — every simulated fill, exit and rupee of P&L, so the strategy can
    be judged on results instead of vibes

The whole point is to find out whether the panel's signals are worth anything
while the answer is still free.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

OPEN = "open"
CLOSED = "closed"

# --- exit reasons -----------------------------------------------------------
TARGET = "target"
STOP = "stop"
SESSION_END = "session_end"
HORIZON_END = "horizon_end"
HALTED = "risk_halt"


# ---------------------------------------------------------------------------
# costs — Indian equity intraday, approximate retail discount-broker rates
# ---------------------------------------------------------------------------
# These are deliberately included. A simulator that ignores costs will show a
# strategy edging out a small profit that vanishes the moment it is real: on a
# tight intraday scalp the round trip can eat most of the move.

BROKERAGE_PCT = 0.03          # 0.03% per leg
BROKERAGE_CAP = 20.0          # or Rs 20 per order, whichever is lower
STT_SELL_PCT = 0.025          # intraday STT, sell side only
EXCHANGE_PCT = 0.00297        # NSE transaction charge
GST_PCT = 18.0                # on brokerage + exchange charges
STAMP_BUY_PCT = 0.003         # buy side only
SEBI_PCT = 0.0001


def _brokerage(turnover):
    return min(BROKERAGE_CAP, turnover * BROKERAGE_PCT / 100.0)


def round_trip_costs(entry, exit_price, qty) -> dict:
    """Every charge on one simulated intraday round trip, itemised."""
    buy_value = entry * qty
    sell_value = exit_price * qty

    brokerage = _brokerage(buy_value) + _brokerage(sell_value)
    stt = sell_value * STT_SELL_PCT / 100.0
    exchange = (buy_value + sell_value) * EXCHANGE_PCT / 100.0
    sebi = (buy_value + sell_value) * SEBI_PCT / 100.0
    stamp = buy_value * STAMP_BUY_PCT / 100.0
    gst = (brokerage + exchange) * GST_PCT / 100.0
    total = brokerage + stt + exchange + sebi + stamp + gst

    return {
        "brokerage": round(brokerage, 2), "stt": round(stt, 2),
        "exchange": round(exchange, 2), "sebi": round(sebi, 2),
        "stamp": round(stamp, 2), "gst": round(gst, 2),
        "total": round(total, 2),
    }


# ---------------------------------------------------------------------------
# risk configuration
# ---------------------------------------------------------------------------

class RiskLimits:
    """
    The rules that decide whether a trade happens at all.

    Every one of these is a refusal condition, not a preference. They are
    checked before sizing, and a breach returns a reason rather than a
    smaller trade — the point of a limit is that it stops you.
    """

    def __init__(self, capital,
                 risk_per_trade_pct=1.0,
                 max_position_pct=20.0,
                 max_concurrent=5,
                 max_daily_loss_pct=3.0,
                 max_daily_trades=6,
                 max_sector_pct=40.0,
                 cash_reserve_pct=20.0,
                 slippage_pct=0.05,
                 min_rupees=1000.0):
        self.capital = float(capital)
        self.risk_per_trade_pct = float(risk_per_trade_pct)
        self.max_position_pct = float(max_position_pct)
        self.max_concurrent = int(max_concurrent)
        self.max_daily_loss_pct = float(max_daily_loss_pct)
        self.max_daily_trades = int(max_daily_trades)
        self.max_sector_pct = float(max_sector_pct)
        self.cash_reserve_pct = float(cash_reserve_pct)
        self.slippage_pct = float(slippage_pct)
        self.min_rupees = float(min_rupees)

    @property
    def risk_per_trade(self):
        return self.capital * self.risk_per_trade_pct / 100.0

    @property
    def max_position_value(self):
        return self.capital * self.max_position_pct / 100.0

    @property
    def daily_loss_limit(self):
        return self.capital * self.max_daily_loss_pct / 100.0

    @property
    def deployable(self):
        return self.capital * (100.0 - self.cash_reserve_pct) / 100.0

    def as_dict(self):
        return {
            "capital": round(self.capital, 2),
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "risk_per_trade": round(self.risk_per_trade, 2),
            "max_position_pct": self.max_position_pct,
            "max_position_value": round(self.max_position_value, 2),
            "max_concurrent": self.max_concurrent,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "daily_loss_limit": round(self.daily_loss_limit, 2),
            "max_daily_trades": self.max_daily_trades,
            "max_sector_pct": self.max_sector_pct,
            "cash_reserve_pct": self.cash_reserve_pct,
            "deployable": round(self.deployable, 2),
            "slippage_pct": self.slippage_pct,
        }


def size_position(limits: RiskLimits, entry, stop, cash_available,
                  sector_exposure=0.0) -> dict:
    """
    How many shares, and why that many.

    Size follows from the distance to the invalidation level, not from the
    capital alone: risking a fixed 1% of the account means a wide stop buys
    fewer shares and a tight stop buys more, so every position carries the
    same rupee risk regardless of how volatile the stock is. That is the whole
    idea, and it is what stops one bad trade mattering more than another.

    Returns a decision with `qty: 0` and a stated reason when the trade cannot
    be taken.
    """
    out = {
        "qty": 0, "entry": entry, "stop": stop, "value": 0.0,
        "risk_rupees": 0.0, "risk_pct_of_capital": 0.0,
        "reason": None, "capped_by": None,
    }

    if entry is None or stop is None or entry <= 0:
        out["reason"] = "entry or invalidation level unavailable — cannot size a position"
        return out
    if stop >= entry:
        out["reason"] = f"invalidation {stop} is not below entry {entry} — not a long setup"
        return out

    per_share_risk = entry - stop
    if per_share_risk <= 0:
        out["reason"] = "zero distance to the stop — cannot size"
        return out

    # 1. the risk-first quantity
    qty = math.floor(limits.risk_per_trade / per_share_risk)
    capped_by = "risk budget"

    # 2. never more than the single-position cap
    by_position_cap = math.floor(limits.max_position_value / entry)
    if by_position_cap < qty:
        qty, capped_by = by_position_cap, "max position size"

    # 3. never more than the cash actually free
    by_cash = math.floor(max(0.0, cash_available) / entry)
    if by_cash < qty:
        qty, capped_by = by_cash, "available cash"

    # 4. never past the sector exposure ceiling
    sector_headroom = limits.capital * limits.max_sector_pct / 100.0 - sector_exposure
    by_sector = math.floor(max(0.0, sector_headroom) / entry)
    if by_sector < qty:
        qty, capped_by = by_sector, "sector exposure cap"

    if qty <= 0:
        out["reason"] = f"position sizes to zero shares ({capped_by})"
        return out

    value = qty * entry
    if value < limits.min_rupees:
        out["reason"] = (f"position would be only Rs {value:,.0f}, below the "
                         f"Rs {limits.min_rupees:,.0f} minimum — costs would dominate")
        return out

    out.update({
        "qty": int(qty),
        "value": round(value, 2),
        "risk_rupees": round(qty * per_share_risk, 2),
        "risk_pct_of_capital": round(qty * per_share_risk / limits.capital * 100, 2),
        "per_share_risk": round(per_share_risk, 2),
        "capped_by": capped_by,
    })
    return out


# ---------------------------------------------------------------------------
# the paper account
# ---------------------------------------------------------------------------

def init(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_positions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       INTEGER,
            verdict_id   INTEGER,
            symbol       TEXT NOT NULL,
            ticker       TEXT,
            sector       TEXT,
            track        TEXT NOT NULL,
            qty          INTEGER NOT NULL,
            entry_price  REAL NOT NULL,
            fill_price   REAL NOT NULL,
            stop         REAL,
            target       REAL,
            risk_rupees  REAL,
            opened_at    TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'open',
            exit_price   REAL,
            exit_reason  TEXT,
            closed_at    TEXT,
            gross_pnl    REAL,
            costs        REAL,
            net_pnl      REAL,
            net_pnl_pct  REAL,
            sizing_json  TEXT,
            note         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_positions(status);
        CREATE INDEX IF NOT EXISTS idx_paper_symbol ON paper_positions(symbol);

        CREATE TABLE IF NOT EXISTS paper_halts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            day        TEXT NOT NULL,
            halted_at  TEXT NOT NULL,
            reason     TEXT NOT NULL,
            day_pnl    REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_halt_day ON paper_halts(day);
        """
    )


def now_ist():
    return datetime.now(IST)


def today_key():
    return now_ist().date().isoformat()


def open_positions(conn, track=None):
    query = "SELECT * FROM paper_positions WHERE status = 'open'"
    params = []
    if track:
        query += " AND track = ?"
        params.append(track)
    return [dict(r) for r in conn.execute(query + " ORDER BY opened_at", params)]


def day_realised_pnl(conn, day=None):
    day = day or today_key()
    row = conn.execute(
        "SELECT COALESCE(SUM(net_pnl), 0) p FROM paper_positions "
        "WHERE status = 'closed' AND substr(closed_at, 1, 10) = ?", (day,)).fetchone()
    return float(row["p"] or 0.0)


def day_trade_count(conn, day=None):
    day = day or today_key()
    row = conn.execute(
        "SELECT COUNT(*) c FROM paper_positions WHERE substr(opened_at, 1, 10) = ?",
        (day,)).fetchone()
    return int(row["c"] or 0)


def deployed_value(conn):
    row = conn.execute(
        "SELECT COALESCE(SUM(qty * fill_price), 0) v FROM paper_positions "
        "WHERE status = 'open'").fetchone()
    return float(row["v"] or 0.0)


def sector_exposure(conn, sector):
    if not sector:
        return 0.0
    row = conn.execute(
        "SELECT COALESCE(SUM(qty * fill_price), 0) v FROM paper_positions "
        "WHERE status = 'open' AND sector = ?", (sector,)).fetchone()
    return float(row["v"] or 0.0)


def is_halted(conn, day=None):
    day = day or today_key()
    row = conn.execute("SELECT reason, day_pnl FROM paper_halts WHERE day = ?",
                       (day,)).fetchone()
    return dict(row) if row else None


def halt(conn, reason, day_pnl, day=None):
    """Stop opening anything else today. Persisted, so a restart cannot undo it."""
    day = day or today_key()
    conn.execute(
        "INSERT OR IGNORE INTO paper_halts (day, halted_at, reason, day_pnl) "
        "VALUES (?,?,?,?)", (day, now_ist().isoformat(), reason, round(day_pnl, 2)))


def check_gates(conn, limits: RiskLimits, symbol, sector) -> str | None:
    """
    Every reason this trade must not be taken, checked before sizing.

    Returns the blocking reason, or None if the trade may proceed.
    """
    halted = is_halted(conn)
    if halted:
        return f"trading halted for today — {halted['reason']}"

    # circuit breaker: the day's realised losses against the limit
    realised = day_realised_pnl(conn)
    if realised <= -limits.daily_loss_limit:
        reason = (f"daily loss limit hit: Rs {realised:,.0f} against a "
                  f"Rs {limits.daily_loss_limit:,.0f} cap "
                  f"({limits.max_daily_loss_pct}% of capital)")
        halt(conn, reason, realised)
        return f"trading halted for today — {reason}"

    if day_trade_count(conn) >= limits.max_daily_trades:
        return (f"daily trade cap reached "
                f"({limits.max_daily_trades} positions opened today)")

    live = open_positions(conn)
    if len(live) >= limits.max_concurrent:
        return f"already holding {len(live)} positions (cap {limits.max_concurrent})"

    if any(p["symbol"] == symbol for p in live):
        return f"already holding {symbol} — not adding to an open position"

    if deployed_value(conn) >= limits.deployable:
        return (f"cash reserve reached — Rs {deployed_value(conn):,.0f} deployed "
                f"of Rs {limits.deployable:,.0f} deployable "
                f"({limits.cash_reserve_pct}% held back)")

    return None


def open_paper_position(conn, limits: RiskLimits, run_id, verdict_id,
                        row, track_name, track, log=None) -> dict:
    """
    Simulate taking the trade this signal implies.

    Returns {"opened": bool, "reason": str, ...}. The reason is always
    populated, because "why did it not trade" is the more useful answer most
    days.
    """
    say = log or (lambda _m: None)
    symbol = row["symbol"]
    sector = row.get("sector")
    levels = track.get("levels") or {}

    blocked = check_gates(conn, limits, symbol, sector)
    if blocked:
        say(f"paper: {symbol} [{track_name}] not taken — {blocked}")
        return {"opened": False, "reason": blocked}

    entry = row.get("price")
    stop = levels.get("invalidation")
    target = levels.get("objective")

    cash = max(0.0, limits.deployable - deployed_value(conn))
    sizing = size_position(limits, entry, stop, cash, sector_exposure(conn, sector))
    if sizing["qty"] <= 0:
        say(f"paper: {symbol} [{track_name}] not taken — {sizing['reason']}")
        return {"opened": False, "reason": sizing["reason"], "sizing": sizing}

    # Assume the fill is worse than the quote. It always is.
    fill = round(entry * (1 + limits.slippage_pct / 100.0), 2)

    cursor = conn.execute(
        """INSERT INTO paper_positions
           (run_id, verdict_id, symbol, ticker, sector, track, qty, entry_price,
            fill_price, stop, target, risk_rupees, opened_at, status, sizing_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)""",
        (run_id, verdict_id, symbol,
         (row.get("evidence") or {}).get("ticker") or f"{symbol}.NS",
         sector, track_name, sizing["qty"], entry, fill, stop, target,
         sizing["risk_rupees"], now_ist().isoformat(), json.dumps(sizing)),
    )

    say(f"paper: {symbol} [{track_name}] {sizing['qty']} @ Rs {fill:,.2f} "
        f"= Rs {sizing['value']:,.0f}, risking Rs {sizing['risk_rupees']:,.0f} "
        f"({sizing['risk_pct_of_capital']}% of capital), sized by {sizing['capped_by']}")

    return {"opened": True, "reason": "position opened", "id": cursor.lastrowid,
            "sizing": sizing, "fill": fill}


def close_paper_position(conn, position, exit_price, reason, log=None):
    """Book a simulated exit, net of every charge."""
    say = log or (lambda _m: None)
    qty = position["qty"]
    fill = position["fill_price"]

    gross = (exit_price - fill) * qty
    costs = round_trip_costs(fill, exit_price, qty)["total"]
    net = gross - costs
    invested = fill * qty

    conn.execute(
        """UPDATE paper_positions SET status='closed', exit_price=?, exit_reason=?,
           closed_at=?, gross_pnl=?, costs=?, net_pnl=?, net_pnl_pct=? WHERE id=?""",
        (round(exit_price, 2), reason, now_ist().isoformat(), round(gross, 2),
         round(costs, 2), round(net, 2),
         round(net / invested * 100, 2) if invested else None, position["id"]),
    )
    say(f"paper: closed {position['symbol']} on {reason} — "
        f"gross Rs {gross:,.0f}, costs Rs {costs:,.0f}, net Rs {net:,.0f}")
    return net


def mark_to_market(conn, limits: RiskLimits, quotes, log=None) -> dict:
    """
    Walk open positions against fresh prices: stops, targets, session end.

    `quotes` is {symbol: {"price", "high", "low", "phase"}}. Stops are checked
    before targets — when a bar touched both we cannot know the order, so the
    worse outcome is assumed.
    """
    say = log or (lambda _m: None)
    closed, realised = 0, 0.0

    for position in open_positions(conn):
        quote = quotes.get(position["symbol"])
        if not quote:
            continue

        price = quote.get("price")
        high = quote.get("high") if quote.get("high") is not None else price
        low = quote.get("low") if quote.get("low") is not None else price
        if price is None:
            continue

        stop, target = position["stop"], position["target"]
        exit_price, reason = None, None

        if stop is not None and low is not None and low <= stop:
            exit_price, reason = stop, STOP
        elif target is not None and high is not None and high >= target:
            exit_price, reason = target, TARGET
        elif position["track"] == "intraday" and not quote.get("live_session", True):
            exit_price, reason = price, SESSION_END

        if exit_price is None:
            continue

        realised += close_paper_position(conn, position, exit_price, reason, log=say)
        closed += 1

    if closed:
        day_pnl = day_realised_pnl(conn)
        if day_pnl <= -limits.daily_loss_limit and not is_halted(conn):
            reason = (f"daily loss limit hit: Rs {day_pnl:,.0f} against a "
                      f"Rs {limits.daily_loss_limit:,.0f} cap")
            halt(conn, reason, day_pnl)
            say(f"paper: TRADING HALTED — {reason}")

    return {"closed": closed, "realised": round(realised, 2)}


def account_summary(conn, limits: RiskLimits, quotes=None) -> dict:
    """Where the paper account stands right now."""
    quotes = quotes or {}
    live = open_positions(conn)

    unrealised = 0.0
    holdings = []
    for position in live:
        quote = quotes.get(position["symbol"]) or {}
        price = quote.get("price") or position["fill_price"]
        move = (price - position["fill_price"]) * position["qty"]
        unrealised += move
        holdings.append({
            "symbol": position["symbol"], "track": position["track"],
            "qty": position["qty"], "fill": position["fill_price"],
            "price": price, "stop": position["stop"], "target": position["target"],
            "risk_rupees": position["risk_rupees"],
            "unrealised": round(move, 2),
            "unrealised_pct": round(move / (position["fill_price"] * position["qty"]) * 100, 2)
            if position["fill_price"] else None,
        })

    row = conn.execute(
        "SELECT COALESCE(SUM(net_pnl),0) net, COUNT(*) n, "
        "COALESCE(SUM(net_pnl > 0),0) wins, COALESCE(SUM(costs),0) costs "
        "FROM paper_positions WHERE status='closed'").fetchone()

    realised_all = float(row["net"] or 0.0)
    trades = int(row["n"] or 0)
    deployed = deployed_value(conn)
    equity = limits.capital + realised_all + unrealised

    return {
        "capital": round(limits.capital, 2),
        "equity": round(equity, 2),
        "cash": round(limits.capital + realised_all - deployed, 2),
        "deployed": round(deployed, 2),
        "realised_pnl": round(realised_all, 2),
        "unrealised_pnl": round(unrealised, 2),
        "total_return_pct": round((equity - limits.capital) / limits.capital * 100, 2)
        if limits.capital else None,
        "day_pnl": round(day_realised_pnl(conn), 2),
        "day_trades": day_trade_count(conn),
        "closed_trades": trades,
        "wins": int(row["wins"] or 0),
        "total_costs": round(float(row["costs"] or 0.0), 2),
        "open_positions": holdings,
        "halted": is_halted(conn),
        "limits": limits.as_dict(),
        "headroom": {
            "positions": max(0, limits.max_concurrent - len(live)),
            "trades_today": max(0, limits.max_daily_trades - day_trade_count(conn)),
            "loss_budget": round(limits.daily_loss_limit + day_realised_pnl(conn), 2),
            "deployable": round(max(0.0, limits.deployable - deployed), 2),
        },
    }
