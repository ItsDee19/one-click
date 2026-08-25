"""
market.py — what time it is for the NSE, and how much of the session has run.

Two things depend on this and both were wrong without it:

  * RVOL. today_volume / average_full_day_volume is only a fair comparison at
    the closing bell. Two hours into the session an average stock looks like
    it is trading at a third of its normal volume. Every intraday reading has
    to be scaled by how much of the session has actually elapsed.

  * Whether an intraday call can be made at all. At 09:00 there is no session
    data — no open, no range, no volume. The honest output then is a watchlist
    with levels, not a signal.

Phase is taken from the feed's own `marketState` when yfinance supplies it,
and from the IST clock otherwise. Holidays are never guessed: if the market
says CLOSED, or no bars exist for today, the session is treated as absent.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

OPEN_TIME = dtime(9, 15)
CLOSE_TIME = dtime(15, 30)
PRE_OPEN_TIME = dtime(9, 0)       # NSE pre-open auction starts
POST_TIME = dtime(16, 0)

SESSION_MINUTES = 375              # 09:15 -> 15:30
OPENING_MINUTES = 30               # first half hour: ranges still forming

# Phases, in the order the day runs through them.
PRE_OPEN = "pre_open"      # 09:00-09:15 — auction, no continuous trading yet
OPENING = "opening"        # 09:15-09:45 — session live but ranges immature
REGULAR = "regular"        # 09:45-15:30 — full confidence in intraday reads
POST = "post"              # 15:30-16:00 — session done, figures final
CLOSED = "closed"          # everything else, incl. weekends and holidays

PHASE_LABELS = {
    PRE_OPEN: "pre-open",
    OPENING: "first 30 minutes",
    REGULAR: "regular session",
    POST: "post-close",
    CLOSED: "market closed",
}


def now_ist() -> datetime:
    return datetime.now(IST)


def _minutes_into_session(moment: datetime) -> float:
    """Minutes elapsed since 09:15 IST, clamped to the session length."""
    start = moment.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute,
                           second=0, microsecond=0)
    return max(0.0, min(SESSION_MINUTES, (moment - start).total_seconds() / 60.0))


def is_weekend(moment: datetime) -> bool:
    return moment.weekday() >= 5


def phase_from_clock(moment: datetime = None) -> str:
    moment = moment or now_ist()
    if is_weekend(moment):
        return CLOSED

    clock = moment.time()
    if PRE_OPEN_TIME <= clock < OPEN_TIME:
        return PRE_OPEN
    if OPEN_TIME <= clock < CLOSE_TIME:
        opening_end = (datetime.combine(moment.date(), OPEN_TIME)
                       + timedelta(minutes=OPENING_MINUTES)).time()
        return OPENING if clock < opening_end else REGULAR
    if CLOSE_TIME <= clock < POST_TIME:
        return POST
    return CLOSED


def phase_from_feed(market_state) -> str:
    """
    Map yfinance's `marketState` onto our phases.

    Yahoo reports PRE / REGULAR / POST / CLOSED / PREPRE / POSTPOST. It is
    authoritative about holidays in a way the clock can never be, so we prefer
    it whenever it is present and recognised.
    """
    state = str(market_state or "").strip().upper()
    if state == "REGULAR":
        return REGULAR
    if state in ("PRE", "PREPRE"):
        return PRE_OPEN
    if state in ("POST", "POSTPOST"):
        return POST
    if state == "CLOSED":
        return CLOSED
    return ""


def session_fraction(moment: datetime = None, phase: str = None) -> float:
    """
    How much of the 09:15-15:30 session has run, as 0.0 - 1.0.

    Anything at or after the close is 1.0; anything before the open is 0.0.
    This is the divisor that makes an intraday RVOL comparable to a full-day
    average.
    """
    moment = moment or now_ist()
    phase = phase or phase_from_clock(moment)

    if phase in (POST, CLOSED):
        return 1.0
    if phase == PRE_OPEN:
        return 0.0
    return _minutes_into_session(moment) / SESSION_MINUTES


def describe(moment: datetime = None, market_state=None) -> dict:
    """
    One snapshot of where we are in the trading day.

    `live_session` answers the question the agents actually care about: is
    there real, still-forming session data behind today's numbers?
    """
    moment = moment or now_ist()
    phase = phase_from_feed(market_state) or phase_from_clock(moment)

    # The feed can say REGULAR while our clock disagrees (stale info payload).
    # Trust the feed for the phase, but scale volume by the clock.
    fraction = session_fraction(moment, phase)

    return {
        "phase": phase,
        "label": PHASE_LABELS.get(phase, phase),
        "at": moment.strftime("%d %b %Y, %H:%M:%S IST"),
        "session_pct": round(fraction * 100, 1),
        "session_fraction": round(fraction, 4),
        "live_session": phase in (OPENING, REGULAR),
        "intraday_tradeable": phase in (OPENING, REGULAR),
        "minutes_to_close": (
            round(SESSION_MINUTES - _minutes_into_session(moment))
            if phase in (OPENING, REGULAR) else 0
        ),
        "from_feed": bool(phase_from_feed(market_state)),
    }


def volume_divisor(fraction: float) -> float:
    """
    Scale factor for comparing a partial day's volume with a full-day average.

    Floored at 4% so the first ten minutes cannot divide by ~zero and report a
    stock as trading at fifty times normal volume.
    """
    return max(0.04, min(1.0, fraction))
