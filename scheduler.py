"""
scheduler.py — unattended runs, from the pre-open bell to the close.

The desk runs itself on trading days:

  09:00        pre-open. The positional shortlist is fully computable before
               the bell; the intraday track honestly reports UNAVAILABLE and
               hands over levels to watch.
  09:45        confirmation. The opening range has printed and RVOL has enough
               of a sample to mean something, so intraday verdicts go live.
  every N min  through the session, until the close. Prices move, RVOL firms
               up, and a verdict from 10:00 is a statement about 10:00.

Weekends are ruled out by the clock. Trading holidays are not guessed from a
hardcoded list — those dates move and a stale list fails silently — the
exchange is asked instead (see market.is_trading_day).

No cron, no APScheduler, no third-party dependency: a daemon thread compares
the clock every 20 seconds. Fixed slots fire once per slot per day, tracked by
a (date, hour, minute) key so a restart mid-minute cannot double-fire.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import market

DEFAULT_TIMES = "09:00,09:45"
DEFAULT_INTERVAL_MINUTES = 30
CHECK_SECONDS = 20          # how often the loop wakes to compare the clock

# Stop opening new cycles near the bell. This is not cosmetic: a full cycle
# takes roughly 8-15 minutes on Sonnet, so a sweep started with ten minutes
# left publishes intraday verdicts after the market has already closed. The
# window has to be longer than a run, not longer than a moment.
STOP_BEFORE_CLOSE_MINUTES = 20


def parse_times(raw: str):
    """'09:00, 09:45' -> [(9, 0), (9, 45)], bad entries dropped."""
    out = []
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hour, _, minute = chunk.partition(":")
            hour, minute = int(hour), int(minute or 0)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            out.append((hour, minute))
    return sorted(set(out))


def next_occurrence(times, after=None):
    """The next datetime one of these fixed times comes round, skipping weekends."""
    if not times:
        return None
    moment = after or market.now_ist()

    for offset in range(0, 8):
        day = moment + timedelta(days=offset)
        if market.is_weekend(day):
            continue
        for hour, minute in times:
            candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > moment:
                return candidate
    return None


def describe_slot(hour, minute):
    """What this particular slot is actually for."""
    at = hour * 60 + minute
    open_at = market.OPEN_TIME.hour * 60 + market.OPEN_TIME.minute
    close_at = market.CLOSE_TIME.hour * 60 + market.CLOSE_TIME.minute

    if at < open_at:
        return "pre-open — positional shortlist, intraday watchlist only"
    if at < open_at + market.OPENING_MINUTES:
        return "opening — ranges still forming, intraday reads are provisional"
    if at <= close_at:
        return "confirmation — opening range printed, intraday verdicts are real"
    return "post-close — end-of-day review"


class Scheduler:
    """
    A daemon thread that triggers runs on trading days.

    Two mechanisms, deliberately separate:

      * fixed slots — exact wall-clock times, fired once each per day
      * session interval — a repeating cycle while the market is actually
        open, so the board keeps up with the tape without needing a slot
        listed for every half hour

    Both are gated on the exchange confirming the day is a trading day, which
    is checked once and cached rather than on every twenty-second tick.
    """

    def __init__(self, times, trigger, mode="live", log=print, enabled=True,
                 interval_minutes=DEFAULT_INTERVAL_MINUTES, follow_session=True,
                 is_busy=None):
        self.times = parse_times(times)
        self.trigger = trigger          # callable(mode) -> (ok, message)
        self.mode = mode
        self.log = log
        self.interval_minutes = max(0, int(interval_minutes or 0))
        self.follow_session = bool(follow_session)
        self.is_busy = is_busy or (lambda: False)
        self.enabled = enabled and (bool(self.times) or self._interval_on())
        self._fired = set()
        self._thread = None
        self._stop = threading.Event()
        self.last_fired_at = None
        self.last_result = None
        self.last_interval_run = None
        self.skipped_today = None

    def _interval_on(self):
        return self.follow_session and self.interval_minutes > 0

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if not self.enabled or self._thread:
            return self
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

        plan = []
        if self.times:
            plan.append(f"fixed {self.pretty_times()} IST")
        if self._interval_on():
            plan.append(f"then every {self.interval_minutes} min while the market is open")
        self.log(f"scheduler armed: {' · '.join(plan)} ({self.mode} mode, "
                 f"trading days only) — next {self.next_run_str()}")
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(CHECK_SECONDS):
            try:
                self._tick(market.now_ist())
            except Exception as exc:                               # noqa: BLE001
                self.log(f"scheduler tick failed ({type(exc).__name__}: {exc})")

    # -- the tick ----------------------------------------------------------

    def _tick(self, moment):
        if market.is_weekend(moment):
            return

        # Only ask the exchange once we are somewhere near the trading day,
        # so an idle overnight process makes no network calls at all.
        clock = moment.time()
        if clock < market.PRE_OPEN_TIME or clock > market.POST_TIME:
            return

        day = market.is_trading_day(log=self.log, moment=moment)
        if day["trading"] is False:
            if self.skipped_today != moment.date().isoformat():
                self.skipped_today = moment.date().isoformat()
                self.log(f"standing down today — {day['reason']}")
            return

        self._tick_fixed(moment)
        if self._interval_on():
            self._tick_interval(moment)

    def _tick_fixed(self, moment):
        for hour, minute in self.times:
            key = (moment.date().isoformat(), hour, minute)
            if key in self._fired:
                continue
            # fire when the minute arrives, and for a minute after so a busy
            # machine cannot skip the slot entirely
            due = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= moment < due + timedelta(minutes=1):
                self._fired.add(key)
                self._fire(describe_slot(hour, minute), f"{hour:02d}:{minute:02d}")

    def _tick_interval(self, moment):
        """Repeat through the live session, but never overlap a running cycle."""
        phase = market.describe(moment)
        if not phase["live_session"]:
            return
        if phase["minutes_to_close"] <= STOP_BEFORE_CLOSE_MINUTES:
            return
        if self.is_busy():
            return

        if self.last_interval_run:
            try:
                since = (moment - datetime.fromisoformat(self.last_interval_run))
                if since < timedelta(minutes=self.interval_minutes):
                    return
            except (TypeError, ValueError):
                pass
        else:
            # First interval run of the day waits for the fixed slots to have
            # had their turn, so 09:45's confirmation is not pre-empted.
            if self.times and moment < moment.replace(
                    hour=self.times[-1][0], minute=self.times[-1][1],
                    second=0, microsecond=0):
                return

        self.last_interval_run = moment.isoformat()
        self._fire(f"session sweep — {phase['minutes_to_close']} min to the close",
                   moment.strftime("%H:%M"))

    def _fire(self, purpose, label):
        self.log(f"scheduled run {label} IST — {purpose}")
        self.last_fired_at = market.now_ist().isoformat()
        try:
            ok, message = self.trigger(self.mode)
        except Exception as exc:                                   # noqa: BLE001
            ok, message = False, f"{type(exc).__name__}: {exc}"
        self.last_result = "started" if ok else f"skipped — {message}"
        if not ok:
            self.log(f"scheduled run did not start: {message}")

    # -- introspection -----------------------------------------------------

    def pretty_times(self):
        return ", ".join(f"{h:02d}:{m:02d}" for h, m in self.times) or "—"

    def next_run(self):
        """
        The next time a run is expected.

        During a live session the interval is what fires next, so the fixed
        slots are not the whole answer.
        """
        moment = market.now_ist()
        upcoming = next_occurrence(self.times, moment)

        if self._interval_on():
            phase = market.describe(moment)
            if phase["live_session"] and phase["minutes_to_close"] > STOP_BEFORE_CLOSE_MINUTES:
                base = moment
                if self.last_interval_run:
                    try:
                        base = datetime.fromisoformat(self.last_interval_run)
                    except (TypeError, ValueError):
                        pass
                candidate = base + timedelta(minutes=self.interval_minutes)
                if candidate < moment:
                    candidate = moment
                if upcoming is None or candidate < upcoming:
                    return candidate
        return upcoming

    def next_run_str(self):
        upcoming = self.next_run()
        return upcoming.strftime("%a %d %b %H:%M IST") if upcoming else "—"

    def status(self):
        upcoming = self.next_run()
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "times": self.pretty_times(),
            "interval_minutes": self.interval_minutes if self._interval_on() else 0,
            "follows_session": self._interval_on(),
            "next_run": upcoming.isoformat() if upcoming else None,
            "next_run_label": self.next_run_str(),
            "next_purpose": describe_slot(*self.times[0]) if self.times else None,
            "last_fired_at": self.last_fired_at,
            "last_result": self.last_result,
            "stood_down_today": self.skipped_today,
        }
