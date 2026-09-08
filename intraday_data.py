"""Prior-day references cached independently from frequently refreshed session bars."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import data_sources
import evidence_quality


def daily_reference(frame, day):
    prior = {}
    if frame is None or getattr(frame, "empty", True):
        return {}
    for stamp, row in frame.iterrows():
        observed = evidence_quality.parse_timestamp(stamp)
        if observed is None or observed.date() >= day:
            continue
        close = evidence_quality.finite_number(row.get("Close"))
        volume = evidence_quality.finite_number(row.get("Volume"))
        if close and close > 0:
            prior[observed.date()] = (close, volume)
    days = sorted(prior)
    if not days:
        return {}
    window = [prior[d] for d in days[-20:]]
    enough = len(window) == 20 and all(v is not None and v > 0 for _, v in window)
    return {"prev_close": prior[days[-1]][0], "reference_date": days[-1].isoformat(),
            "avg_volume": sum(v for _, v in window) / 20 if enough else None,
            "avg_turnover": sum(c * v for c, v in window) / 20 if enough else None,
            "reference_sessions": len(window)}


def load_references(tickers, day, log=None, cache_dir=None):
    """Only prior data is cached. A new session date forces fresh references."""
    tickers = sorted(set(tickers))
    root = Path(cache_dir or os.environ.get("DB_DIR") or Path(__file__).parent) / ".intraday_cache"
    digest = hashlib.sha256("\n".join(tickers).encode()).hexdigest()[:16]
    path = root / f"references-{day.isoformat()}-{digest}.json"
    try:
        refs = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(refs, dict):
            refs = {}
    except (OSError, ValueError):
        refs = {}
    missing = [ticker for ticker in tickers if not refs.get(ticker)]
    if missing:
        frames = data_sources.download_frames(missing, period="3mo", interval="1d", log=log)
        for ticker in missing:
            ref = daily_reference(frames.get(ticker), day)
            if ref:
                refs[ticker] = ref
        try:
            root.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(refs, allow_nan=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            if log:
                log("intraday reference cache unavailable; using this scan's downloaded references")
    return {ticker: refs.get(ticker, {}) for ticker in tickers}
