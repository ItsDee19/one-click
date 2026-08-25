"""
scheduler.py — unattended runs at the times that matter.

The point is not "run every N minutes". It is to run at the two moments in an
NSE day when the answer changes:

  09:00  pre-open. The positional shortlist is fully computable and worth
         having before the bell. The intraday track honestly reports
         UNAVAILABLE and hands over levels to watch.
  09:45  confirmation. The opening range has printed and RVOL has enough of a
         sample to mean something, so intraday verdicts become real.

Extra times can be added; the defaults are those two.

Weekends are skipped from the clock. Holidays are not guessed — the run starts
and the data layer reports the market closed, which is the honest outcome and
costs one cheap request.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import market

DEFAULT_TIMES = "09:00,09:45"
CHECK_SECONDS = 20          # how often the loop wakes to compare the clock


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
    """The next datetime one of these times comes round, skipping weekends."""
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
    A daemon thread that triggers runs at wall-clock times.

    Deliberately dependency-free: no cron, no APScheduler, nothing to install.
    It compares the clock every 20 seconds and fires once per slot per day,
    tracked by a (date, hour, minute) key so a restart mid-minute cannot
    double-fire and a slow run cannot be re-entered.
    """

    def __init__(self, times, trigger, mode="live", log=print, enabled=True):
        self.times = parse_times(times)
        self.trigger = trigger          # callable(mode) -> (ok, message)
        self.mode = mode
        self.log = log
        self.enabled = enabled and bool(self.times)
        self._fired = set()
        self._thread = None
        self._stop = threading.Event()
        self.last_fired_at = None
        self.last_result = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if not self.enabled or self._thread:
            return self
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        self.log(f"scheduler armed for {self.pretty_times()} IST "
                 f"({self.mode} mode, weekdays only) — next {self.next_run_str()}")
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(CHECK_SECONDS):
            try:
                self._tick(market.now_ist())
            except Exception as exc:                               # noqa: BLE001
                self.log(f"scheduler tick failed ({type(exc).__name__}: {exc})")

    def _tick(self, moment):
        if market.is_weekend(moment):
            return
        for hour, minute in self.times:
            key = (moment.date().isoformat(), hour, minute)
            if key in self._fired:
                continue
            # fire when the minute arrives, and for a minute after so a busy
            # machine cannot skip the slot entirely
            due = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= moment < due + timedelta(minutes=1):
                self._fired.add(key)
                self._fire(hour, minute)

    def _fire(self, hour, minute):
        purpose = describe_slot(hour, minute)
        self.log(f"scheduled run {hour:02d}:{minute:02d} IST — {purpose}")
        self.last_fired_at = market.now_ist().isoformat()
        try:
            ok, message = self.trigger(self.mode)
        except Exception as exc:                                   # noqa: BLE001
            ok, message = False, f"{type(exc).__name__}: {exc}"
        self.last_result = ("started" if ok else f"skipped — {message}")
        if not ok:
            self.log(f"scheduled run did not start: {message}")

    # -- introspection -----------------------------------------------------

    def pretty_times(self):
        return ", ".join(f"{h:02d}:{m:02d}" for h, m in self.times) or "—"

    def next_run(self):
        return next_occurrence(self.times)

    def next_run_str(self):
        upcoming = self.next_run()
        return upcoming.strftime("%a %d %b %H:%M IST") if upcoming else "—"

    def status(self):
        upcoming = self.next_run()
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "times": self.pretty_times(),
            "next_run": upcoming.isoformat() if upcoming else None,
            "next_run_label": self.next_run_str(),
            "next_purpose": describe_slot(*self.times[0]) if self.times else None,
            "last_fired_at": self.last_fired_at,
            "last_result": self.last_result,
        }
