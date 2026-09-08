"""Causal intraday research with costs, an auditable ledger and chronological OOS.

Default coverage is the discovered NSE exchange universe. Yahoo's 60 calendar-day
window is a limited research feed. Use --data-dir for a longer timestamped archive;
see INTRADAY-HISTORY.md. This command does not certify a strategy as proven.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sys

import data_sources
import intraday_validation as validation
import strategies

HERE = Path(__file__).resolve().parent
OUT_FILE = str(HERE / "backtest_intraday_v2.json")
PERIOD = "60d"
INTERVAL = "5m"
MIN_PRIOR_SESSIONS = 20
HOLDOUT_FRACTION = .30


def _import_yf():
    import yfinance as yf
    return yf


def _ticker(symbol):
    name = str(symbol).strip().upper()
    return name if "." in name else name + ".NS"


def _archive_files(data_dir):
    """One ticker per CSV/JSON file; never guess between duplicate sources."""
    found = {}
    for path in sorted(Path(data_dir).iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".csv", ".json") or path.stem == "metadata":
            continue
        ticker = _ticker(path.stem)
        if ticker in found:
            raise ValueError(f"Duplicate archive files for {ticker}; retain one authoritative source")
        found[ticker] = path
    return found


def _archive_bars(path):
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        with path.open(encoding="utf-8-sig") as handle:
            content = json.load(handle)
        rows = content.get("bars") if isinstance(content, dict) else content
    if not isinstance(rows, list):
        raise ValueError("Archive must contain a list of OHLCV bars")
    bars = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each archive bar must be an object")
        normal = {str(key).lower(): value for key, value in row.items()}
        if any(key not in normal for key in ("timestamp", "open", "high", "low", "close", "volume")):
            raise ValueError("Every bar needs timestamp, open, high, low, close and volume")
        bars.append({key: normal[key] for key in ("timestamp", "open", "high", "low", "close", "volume")})
    return bars


def _frame_bars(frame, ticker):
    if hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1:
        frame = frame.xs(ticker, axis=1, level=1)
    return [{"timestamp": stamp.isoformat(), "open": row.get("Open"),
             "high": row.get("High"), "low": row.get("Low"), "close": row.get("Close"),
             "volume": row.get("Volume")} for stamp, row in frame.iterrows()]


def prepare_sessions(bars, diagnostics=None, now=None):
    """Only earlier full sessions feed the volume baseline used by the live desk."""
    info = diagnostics if diagnostics is not None else {}
    groups = defaultdict(list)
    for bar in bars:
        timestamp = validation.parse_time(bar.get("timestamp"))
        if timestamp is None:
            raise ValueError("Bar timestamps must be ISO 8601 with an explicit UTC offset")
        groups[timestamp.date().isoformat()].append(dict(bar, timestamp=timestamp))
    info.update(raw_dates=len(groups), complete_sessions=0, warmup_sessions=0,
                invalid_or_partial_dates=[], first_date=min(groups) if groups else None,
                last_date=max(groups) if groups else None)
    prior_volumes, previous_close, prepared = [], None, []
    for day, records in sorted(groups.items()):
        records.sort(key=lambda row: row["timestamp"])
        average = (sum(prior_volumes[-MIN_PRIOR_SESSIONS:]) / MIN_PRIOR_SESSIONS
                   if len(prior_volumes) >= MIN_PRIOR_SESSIONS else None)
        session = strategies.session(records, prev_close=previous_close, avg_volume=average, now=now)
        if not session or not session.get("complete"):
            info["invalid_or_partial_dates"].append(day)
            previous_close = None
            continue
        info["complete_sessions"] += 1
        if average is not None:
            prepared.append((day, session))
        else:
            info["warmup_sessions"] += 1
        previous_close = session["close"][-1]
        prior_volumes.append(session["day_volume"])
    info["evaluated_sessions"] = len(prepared)
    return prepared


def sessions_for(ticker, log=print, data_dir=None, diagnostics=None, now=None):
    info = diagnostics if diagnostics is not None else {}
    info.update(ticker=ticker, status="data_unavailable", source="local_archive" if data_dir else "Yahoo Finance / yfinance")
    try:
        if data_dir:
            path = _archive_files(data_dir).get(ticker)
            if path is None:
                raise ValueError("No archive file for requested ticker")
            bars = _archive_bars(path)
            info.update(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            frame = _import_yf().download(ticker, period=PERIOD, interval=INTERVAL,
                                          progress=False, auto_adjust=False, threads=False)
            if frame is None or getattr(frame, "empty", True):
                raise ValueError("No intraday history returned")
            bars = _frame_bars(frame, ticker)
        days = prepare_sessions(bars, info, now=now)
        info["status"] = "ok" if days else "insufficient_complete_history"
        return days
    except Exception as exc:  # one unavailable stock must not abort exchange research
        info["error"] = f"{type(exc).__name__}: {exc}"
        log(f"  {ticker}: {info['error']}")
        return []


def chronological_split(dates, holdout_fraction=HOLDOUT_FRACTION):
    """A single split for all stocks; no same-day cross-sectional leakage."""
    ordered = sorted(set(dates))
    if not 0 < holdout_fraction < 1:
        raise ValueError("Holdout fraction must be between zero and one")
    split = max(1, min(len(ordered) - 2, math.floor(len(ordered) * (1 - holdout_fraction))))
    development = ordered[:split] if len(ordered) >= 3 else ordered
    embargo = ordered[split:split + 1] if len(ordered) >= 3 else []
    out_of_sample = ordered[split + 1:] if len(ordered) >= 3 else []
    membership = {day: "development" for day in development}
    membership.update({day: "embargo" for day in embargo})
    membership.update({day: "oos" for day in out_of_sample})
    return membership, {
        "method": "chronological_global_dates", "fraction_requested": holdout_fraction,
        "development_start": development[0] if development else None,
        "development_end": development[-1] if development else None,
        "embargo_dates": embargo, "start": out_of_sample[0] if out_of_sample else None,
        "end": out_of_sample[-1] if out_of_sample else None,
        "development_dates": len(development), "oos_dates": len(out_of_sample),
        "untouched": False, "preregistered_at": None, "protocol_evidence": None,
        "note": "Automatic date separation is research. It does not establish an untouched, predeclared evaluation.",
    }


def _selection_diagnostics(candidates):
    """Shared ranking at confirmation time; never claim portfolio returns here."""
    import intraday_policy
    by_time = defaultdict(list)
    for candidate in candidates:
        by_time[candidate["confirmed_at"]].append(candidate)
    selected, rejected_count = [], 0
    for timestamp, rows in sorted(by_time.items()):
        result = intraday_policy.select_candidates(rows, max_picks=5, max_per_sector=2)
        for row in result["selected"]:
            selected.append({"signal_id": row["signal_id"], "confirmed_at": timestamp,
                             "ticker": row["ticker"], "strategy": row["strategy"], "direction": row["direction"]})
        rejected_count += len(result["rejected"])
    return {"snapshot_count": len(by_time), "candidates": len(candidates), "selected_count": len(selected),
            "rejected_count": rejected_count, "selected_signals": selected,
            "scope": "Independent confirmation-time ranking diagnostics; not portfolio performance"}


def run(symbols=None, log=print, data_dir=None, output=OUT_FILE, now=None):
    current = validation.parse_time(now) if now is not None else datetime.now(strategies.IST)
    if current is None:
        raise ValueError("now must have a timezone")
    if symbols:
        entries = [{"ticker": ticker, "sector": None} for ticker in sorted({_ticker(s) for s in symbols if str(s).strip()})]
        universe_metadata = {"scope": "explicit_subset", "point_in_time": False}
    elif data_dir:
        entries = [{"ticker": ticker, "sector": None} for ticker in _archive_files(data_dir)]
        universe_metadata = {"scope": "all_supplied_archive_files", "point_in_time": False}
    else:
        universe = data_sources.load_full_exchange(log=log)
        unique = {entry["ticker"]: dict(entry, bucket=bucket)
                  for bucket, rows in universe.items() for entry in rows if entry.get("ticker")}
        entries = [unique[ticker] for ticker in sorted(unique)]
        universe_metadata = dict(data_sources.LAST_UNIVERSE_METADATA, scope="full_current_exchange", point_in_time=False)
    if not entries:
        log("No symbols to test")
        return None
    log(f"Intraday research: {len(entries)} requested symbols, 20 prior complete sessions required")
    ledger, candidates, signal_exclusions, coverage, evaluation_dates = [], [], [], [], set()
    stock_sessions = 0
    for entry in entries:
        ticker, info = entry["ticker"], {}
        days = sessions_for(ticker, log=log, data_dir=data_dir, diagnostics=info, now=current)
        coverage.append(info)
        stock_sessions += len(days)
        evaluation_dates.update(day for day, session in days)
        before = len(ledger)
        for day, session in days:
            for name, setup in strategies.signals(session).items():
                signal_id = f"{ticker}|{name}|{setup['direction']}|{setup['confirmed_at']}"
                risk = float(setup["risk_per_share"])
                estimated_cost_r = ((strategies.ROUND_TRIP_COST_BPS + 2 * strategies.SLIPPAGE_BPS)
                                    * setup["entry"] / 10000 / risk)
                candidates.append(dict(setup, ticker=ticker, sector=entry.get("sector"), signal_id=signal_id,
                                       current_reward_risk=(setup["reward_risk"] - estimated_cost_r) / (1 + estimated_cost_r),
                                       entry_drift_r=0.0, state="entry_ready"))
                outcome = strategies.simulate(setup, session)
                if not outcome or validation.finite(outcome.get("net_r")) is None:
                    signal_exclusions.append({"signal_id": signal_id, "date": day, "ticker": ticker,
                                              "strategy": name, "direction": setup["direction"],
                                              "reason": "No valid completed executable fill"})
                    continue
                stressed = strategies.simulate(setup, session, cost_bps=2 * strategies.ROUND_TRIP_COST_BPS,
                                               slippage_bps=2 * strategies.SLIPPAGE_BPS)
                ledger.append(dict(outcome, signal_id=signal_id, ticker=ticker, symbol=ticker.split(".")[0],
                                   date=day, strategy=name, direction=setup["direction"],
                                   signal_at=setup["signal_at"], confirmed_at=setup["confirmed_at"],
                                   planned_entry=setup["entry"], stop=setup["stop"], target=setup["target"],
                                   reason=setup["why"], decision_rvol=setup.get("rvol"),
                                   stress_net_r=stressed.get("net_r") if stressed else None))
        log(f"  {ticker:<18} {len(days):>4} evaluated sessions, {len(ledger)-before:>5} completed trades")
    samples, holdout = chronological_split(evaluation_dates)
    for row in ledger:
        row["sample"] = samples[row["date"]]
    ledger.sort(key=lambda row: (row["date"], row["confirmed_at"], row["ticker"], row["strategy"]))
    summaries = {}
    for name in strategies.STRATEGIES:
        direction = strategies.STRATEGY_DIRECTIONS[name]
        rows = [row for row in ledger if row["strategy"] == name and row["direction"] == direction]
        by_sample = {sample: validation.net_summary([row for row in rows if row["sample"] == sample])
                     for sample in ("development", "oos")}
        by_sample["all"] = validation.net_summary(rows)
        stressed = [dict(row, net_r=row["stress_net_r"]) for row in rows if row["sample"] == "oos"]
        by_sample["oos_double_friction"] = validation.net_summary(stressed)
        summaries[name] = {"directions": {direction: by_sample}}
    observed = sorted({day for item in coverage for day in (item.get("first_date"), item.get("last_date")) if day})
    blob = {
        "schema_version": validation.SCHEMA_VERSION, "generated_at": current.isoformat(),
        "generated": current.isoformat(), "strategy_version": strategies.strategy_version(),
        "validation_policy_hash": validation.validation_policy_hash(),
        "cost_model": {"version": validation.COST_MODEL_VERSION,
                       "round_trip_cost_bps": strategies.ROUND_TRIP_COST_BPS,
                       "slippage_bps_per_fill": strategies.SLIPPAGE_BPS,
                       "assumption": "Conservative research allowances, not an exact broker tariff; stress case doubles both"},
        "window": "local timestamped archive" if data_dir else f"{PERIOD} calendar lookback of {INTERVAL} bars",
        "symbols": len(entries), "stock_sessions": stock_sessions,
        "data": {"source": "local_archive" if data_dir else "Yahoo Finance / yfinance",
                 "first_date": observed[0] if observed else None, "last_date": observed[-1] if observed else None,
                 "point_in_time_universe": False, "universe_evidence": None,
                 "universe": universe_metadata, "minimum_prior_complete_sessions": MIN_PRIOR_SESSIONS,
                 "rvol_baseline": "prior_20_complete_day_average_scaled_by_elapsed_regular_bars"},
        "coverage": {"requested": len(entries), "successful": sum(info.get("status") == "ok" for info in coverage),
                     "failed_or_insufficient": sum(info.get("status") != "ok" for info in coverage), "symbols": coverage},
        "holdout": holdout, "strategies": summaries, "ledger": ledger, "signal_exclusions": signal_exclusions,
        "ranking": {"policy_version": validation.RANKING_POLICY_VERSION, "tested": False,
                    "method": "confirmation_time_diagnostics_only", "execution_eligibility_evidence": None,
                    "diagnostics": _selection_diagnostics(candidates),
                    "limitations": ["No historical broker eligibility or spreads", "No live publication/capital replay",
                                    "Repeated day entries and cross-snapshot exposures are not a tested portfolio"]},
        "limitations": ["Current-universe or supplied-file sample is not point-in-time membership",
                        "Chronological split is not a predeclared untouched holdout",
                        "OHLCV bars cannot establish executable depth or market impact",
                        "Historical results do not establish future profitability"],
    }
    for name, record in summaries.items():
        for direction, metrics in record["directions"].items():
            metrics["validation"] = validation.assess_record(blob, name, direction, now=current)
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(blob, handle, indent=1, allow_nan=False)
        os.replace(temporary, destination)
    return blob


def report(blob, log=print):
    if not blob:
        return
    log(f"\nResearch only: {blob['coverage']['successful']}/{blob['symbols']} symbols, "
        f"{blob['stock_sessions']} evaluated stock-sessions")
    log(f"Observed dates: {blob['data']['first_date']} to {blob['data']['last_date']}")
    log("Rule / side                 OOS trades  OOS dates  Net mean R   95% day-cluster interval")
    for name, record in blob["strategies"].items():
        for direction, result in record["directions"].items():
            metrics = result["oos"]
            mean = metrics["net_expectancy_r"]
            shown = f"{mean:+.3f}" if mean is not None else "n/a"
            log(f"{name + ' / ' + direction:<29} {metrics['trades']:>8}  {metrics['distinct_dates']:>8}  "
                f"{shown:>10}   {metrics['net_expectancy_ci95']}")
    log("No rule is promoted automatically. Holdout provenance, point-in-time membership, "
        "daily selector execution and forward paper evidence remain required.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="", help="Explicit comma-separated subset; default discovers all NSE stocks")
    parser.add_argument("--data-dir", help="Offline CSV/JSON archive directory; default tests every supplied file")
    parser.add_argument("--output", default=OUT_FILE, help="Version 2 destination; legacy gross record is preserved by default")
    args = parser.parse_args(argv)
    symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()] or None
    blob = run(symbols=symbols, data_dir=args.data_dir, output=args.output)
    report(blob)
    if blob:
        print(f"Written to {args.output}")
    return 0 if blob else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
