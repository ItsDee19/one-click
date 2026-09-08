"""Dated intraday research with explicit evidence, lifecycle and admission gates."""
from __future__ import annotations
from copy import deepcopy
from datetime import timedelta
import json
import os
import data_sources
import evidence_quality
import intraday_data
import intraday_policy as policy
import intraday_validation
import market
import strategies

HERE = os.path.dirname(os.path.abspath(__file__))
RECORD_FILE = os.path.join(HERE, "backtest_intraday_v2.json")
LEGACY_RECORD_FILE = os.path.join(HERE, "backtest_intraday.json")
SWING_RECORD_FILE = os.path.join(HERE, "backtest_swing.json")
MAX_PICKS = policy.MAX_PICKS
MAX_BAR_AGE_MINUTES = 7  # age from the completed candle's close


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            blob = json.load(handle)
        return blob if isinstance(blob, dict) else {}
    except (OSError, ValueError):
        return {}


def load_record():
    blob = _read(RECORD_FILE) or _read(LEGACY_RECORD_FILE)
    record = blob.get("strategies")
    return record if isinstance(record, dict) else {}, blob


def load_swing_record():
    blob = _read(SWING_RECORD_FILE)
    return blob.get("strategies") or {}, blob


def _swing_payload():
    record, blob = load_swing_record()
    return {"strategies": record, "window": blob.get("window"), "symbols": blob.get("symbols")}


def _bars_from(frame):
    if frame is None or getattr(frame, "empty", True):
        return []
    return [{"timestamp": stamp, "open": row.get("Open"), "high": row.get("High"),
             "low": row.get("Low"), "close": row.get("Close"), "volume": row.get("Volume")}
            for stamp, row in frame.iterrows()]


def _prev_close(quote, session_date=None):
    return intraday_data.daily_reference(quote.get("frame"), session_date or market.now_ist().date()).get("prev_close")


def _session_is_fresh(observed, moment):
    return (observed is not None and observed.date() == moment.date()
            and 0 <= (moment - observed).total_seconds() / 60 <= MAX_BAR_AGE_MINUTES)


def _market_open(moment):
    return market.phase_from_clock(moment) in (market.REGULAR, market.OPENING)


def _entry_window_open(moment):
    return _market_open(moment) and (moment.hour, moment.minute) < (14, 15)


def load_eligibility():
    path = os.environ.get("INTRADAY_ELIGIBILITY_FILE", "").strip()
    return _read(path) if path else {}


def eligibility_reasons(entry, reference, direction, moment, eligibility):
    reasons = []
    if entry.get("segment") != "main_board" or entry.get("series") != "EQ":
        reasons.append("Only verified NSE main-board EQ listings can enter the actionable shortlist.")
    if policy.finite(reference.get("avg_turnover"), 0) < policy.MIN_DAILY_TURNOVER_INR:
        reasons.append("Twenty prior sessions do not establish the minimum daily turnover of INR 1 crore.")
    dated = evidence_quality.parse_timestamp(reference.get("reference_date"))
    if dated is None or not 0 < (moment.date() - dated.date()).days <= evidence_quality.MAX_DAILY_AGE_DAYS:
        reasons.append("The prior-session daily reference is missing or stale.")
    symbols = eligibility.get("symbols")
    permission = symbols.get(entry["ticker"], {}) if isinstance(symbols, dict) else {}
    permission = permission if isinstance(permission, dict) else {}
    if (permission.get("as_of") != moment.date().isoformat() or not permission.get("source")
            or permission.get(direction) is not True):
        reasons.append(f"Current broker {direction} eligibility has not been verified for this session.")
    return reasons


def setup_lifecycle(setup, session, moment):
    """Prices are completed-bar estimates; old signals never become fresh entries."""
    if not session.get("validated") or not session.get("timestamps") or session["timestamps"][-1] is None:
        return {"state": "invalid", "reasons": ["A validated timestamped session is required."]}
    confirmed = evidence_quality.parse_timestamp(setup.get("confirmed_at"))
    observed = session["timestamps"][-1] + timedelta(minutes=strategies.BAR_MINUTES)
    expiry = confirmed + timedelta(minutes=policy.SIGNAL_LIFETIME_MINUTES) if confirmed else observed
    expiry = min(expiry, observed + timedelta(minutes=MAX_BAR_AGE_MINUTES),
                 moment.replace(hour=14, minute=15, second=0, microsecond=0))
    direction = setup.get("direction")
    sign = 1 if direction == "long" else -1
    last = session["close"][-1]
    estimate = last * (1 + sign * strategies.SLIPPAGE_BPS / 10000)
    stop, target, original = (policy.finite(setup.get(k)) for k in ("stop", "target", "entry"))
    result = {"state": "entry_ready", "reasons": [], "as_of": observed.isoformat(),
              "expires_at": expiry.isoformat(), "last": last, "current_entry": round(estimate, 4),
              "current_reward_risk": None, "entry_drift_r": None}
    def reject(state, reason):
        result.update(state=state, reasons=[reason])
        return result
    if not session.get("validated") or confirmed is None or direction not in ("long", "short"):
        return reject("invalid", "Timestamped session and signal direction are required.")
    if any(v is None or v <= 0 for v in (stop, target, original)):
        return reject("invalid", "Trade levels are incomplete.")
    for i in range(setup["bar"] + 1, session["n"]):
        stop_hit = session["low"][i] <= stop if sign == 1 else session["high"][i] >= stop
        target_hit = session["high"][i] >= target if sign == 1 else session["low"][i] <= target
        if stop_hit or target_hit:
            return reject("stop_hit" if stop_hit else "target_hit", "The original setup has already touched its stop or target.")
    if not _entry_window_open(moment):
        return reject("expired", "The entry window is closed; these levels are historical research.")
    if not _session_is_fresh(observed, moment):
        return reject("stale", "The latest completed candle is stale or from another session.")
    if not confirmed <= moment < expiry:
        return reject("expired", "The original signal's ten-minute entry window has elapsed.")
    original_risk = sign * (original - stop)
    current_risk = sign * (estimate - stop)
    reward = sign * (target - estimate)
    if min(original_risk, current_risk, reward) <= 0:
        return reject("missed", "Current estimated entry is outside the original stop/target range.")
    drift = abs(estimate - original) / original_risk
    reserve = estimate * (strategies.ROUND_TRIP_COST_BPS + strategies.SLIPPAGE_BPS) / 10000
    rr = (reward - reserve) / (current_risk + reserve)
    result.update(current_reward_risk=round(rr, 4), entry_drift_r=round(drift, 4))
    if drift > policy.MAX_ENTRY_DRIFT_R:
        return reject("missed", "Estimated entry has drifted more than 0.25R from the confirmed signal.")
    if rr < policy.MIN_NET_REWARD_RISK:
        return reject("missed", "Estimated reward/risk after costs is below 1.5.")
    if policy.finite(setup.get("rvol"), 0) <= 0:
        return reject("restricted", "Prior-session volume evidence is unavailable.")
    return result


def refresh_publication(payload, moment=None):
    """Recheck every cached response, including expiry during a long scan."""
    result = deepcopy(payload)
    moment = moment or market.now_ist()
    result["phase"] = market.phase_from_clock(moment)
    result["tradeable"] = _entry_window_open(moment) and result.get("trading_day", True)
    history = result.setdefault("history", [])
    for key in ("picks", "candidates"):
        active = []
        for item in result.get(key, []):
            expiry = evidence_quality.parse_timestamp(item.get("expires_at"))
            observed = evidence_quality.parse_timestamp(item.get("as_of"))
            if not _session_is_fresh(observed, moment):
                history.append(dict(item, state="stale", reasons=["Completed-bar snapshot expired; refresh the scan."]))
            elif not result["tradeable"] or expiry is None or moment >= expiry:
                history.append(dict(item, state="expired", reasons=["The original entry window is closed."]))
            else:
                active.append(item)
        result[key] = active
    result["history"] = history[:100]
    return result


def scan(log=None, universe=None):
    say = log or (lambda _message: None)
    record, blob = load_record()
    started = market.now_ist()
    version = strategies.strategy_version()
    trading = market.is_trading_day(log=say)
    board = {"status": "done", "generated": started.isoformat(), "strategy_version": version,
             "picks": [], "candidates": [],
             "history": [], "trading_day": bool(trading.get("trading")), "strategies": {},
             "record_window": blob.get("window") or (f"{blob['data'].get('first_date')} to {blob['data'].get('last_date')}" if isinstance(blob.get("data"), dict) else None),
             "record_sessions": blob.get("stock_sessions"), "swing": _swing_payload(),
             "note": "Only independently validated strategies can produce qualified picks. Current-bar prices are estimates; broker eligibility is required."}
    validations = {}
    for name in strategies.STRATEGIES:
        check = intraday_validation.assess_record(blob, name, strategies.STRATEGY_DIRECTIONS[name], now=started)
        validations[name] = check
        metrics = check.get("metrics") or record.get(name) or {}
        board["strategies"][name] = dict(metrics, validation_status=check["status"],
                                         is_net=blob.get("schema_version") == 2, enough=False)
    if not trading.get("trading"):
        board["note"] = f"No trading session: {trading.get('reason', 'market closed')}."
        return refresh_publication(board, started)
    universe = data_sources.load_full_exchange(log=say) if universe is None else universe
    entries = list({e["ticker"]: dict(e, bucket=b) for b, rows in universe.items() for e in rows}.values())
    tickers = [entry["ticker"] for entry in entries]
    say(f"Loading prior-session references for {len(tickers)} stocks")
    refs = intraday_data.load_references(tickers, started.date(), log=say)
    say(f"Scanning current-session candles for {len(tickers)} stocks")
    frames = data_sources.fetch_intraday(tickers, log=say)
    permissions = load_eligibility()
    active, observations = [], {}
    coverage = {"listed": len(entries), "eligible": 0, "usable_sessions": 0, "stale_sessions": 0,
                "missing_sessions": 0, "invalid_sessions": 0, "universe": data_sources.LAST_UNIVERSE_METADATA}
    for entry in entries:
        ticker = entry["ticker"]
        now = market.now_ist()
        ref = refs.get(ticker) or {}
        if any(not eligibility_reasons(entry, ref, d, now, permissions) for d in ("long", "short")):
            coverage["eligible"] += 1
        frame = frames.get(ticker)
        if frame is None or getattr(frame, "empty", True):
            coverage["missing_sessions"] += 1
            continue
        s = strategies.session(_bars_from(frame), prev_close=ref.get("prev_close"),
                               avg_volume=ref.get("avg_volume"), now=now)
        if not s:
            coverage["invalid_sessions"] += 1
            continue
        observed = s["timestamps"][-1] + timedelta(minutes=strategies.BAR_MINUTES)
        observations[ticker] = observed
        for name, setup in strategies.signals(s).items():
            state = setup_lifecycle(setup, s, now)
            check = validations[name]
            restrictions = eligibility_reasons(entry, ref, setup["direction"], now, permissions)
            item = dict(setup, **state)
            item.update(ticker=ticker, symbol=ticker.split(".")[0], name=entry.get("name"),
                        strategy_version=version,
                        sector=entry.get("sector"), bucket=entry.get("bucket"),
                        action="BUY" if setup["direction"] == "long" else "SELL",
                        validation=check, record=dict(check.get("metrics") or {}, is_net=blob.get("schema_version") == 2),
                        vwap=round(s["vwap"][-1], 2), rvol_method=s["rvol_method"], gap_pct=s["gap_pct"])
            if item["state"] == "entry_ready":
                item["reasons"] += restrictions + list(check.get("reasons") or [])
                if restrictions:
                    item["state"] = "restricted"
                elif not check["qualified"]:
                    item["state"] = "research_only"
                active.append(item)
            else:
                board["history"].append(item)
    selected = policy.select_candidates(active, separate_admission=True)
    for item in selected["selected"]:
        board["picks" if item["state"] == "entry_ready" and item["validation"]["qualified"] else "candidates"].append(item)
    board["history"].extend(selected["rejected"])
    completed = market.now_ist()
    coverage["usable_sessions"] = sum(_session_is_fresh(stamp, completed) for stamp in observations.values())
    coverage["stale_sessions"] = len(observations) - coverage["usable_sessions"]
    board.update(generated=completed.isoformat(), coverage=coverage, selection_policy=policy.POLICY_VERSION)
    result = refresh_publication(board, completed)
    say(f"Scan finished: {len(result['picks'])} qualified picks, {len(result['candidates'])} research candidates")
    return result
