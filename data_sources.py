"""
data_sources.py — evidence bundles for the agent panel.

Two modes, one output shape:

  demo : read pre-built bundles from demo_data/*.json (fully offline)
  live : discover NSE main-board/SME equities and collect bounded yfinance data

Everything downstream (scoring.py, llm.py, app.py) only ever sees the
normalised bundle produced here, so the engines cannot tell the two apart.

Rules that matter:
  * a value that could not be computed is None *and* named in data_gaps
  * we never guess, interpolate or carry forward a missing number
  * fundamental metrics retain their provider and freshness metadata; missing
    accounting ratios are never inferred from analyst recommendations.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from statistics import median

import market

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.path.join(HERE, "demo_data")
UNIVERSE_FILE = os.path.join(HERE, "universe.json")

SMA_PERIOD = 20            # N-day simple moving average used for price_vs_sma_pct
HISTORY_PERIOD = "2y"      # enough history for long-trend and swing rules on every stock
BENCHMARK = "^NSEI"        # NIFTY 50 — the yardstick for relative strength
BENCHMARK_NAME = "NIFTY 50"
INTRADAY_INTERVAL = "5m"   # granularity for VWAP / opening range
OPENING_RANGE_BARS = 3     # first 3 x 5m bars = the 09:15-09:30 opening range
ATR_PERIOD = 14
IST = timezone(timedelta(hours=5, minutes=30))

BUCKETS = ("large", "mid", "small", "unclassified")

# The most recent screen's quotes, so the sector heatmap can compute breadth
# without paying for a second download of the same bars.
LAST_QUOTES = {}
LAST_UNIVERSE_METADATA = {}
LAST_COVERAGE = {}

NO_RATIOS_NOTE = (
    "Feed carries no raw fundamental ratios (P/E, P/B, ROE, margins, debt). "
    "The fundamental view is limited to sell-side analyst targets and consensus."
)

# Tiny headline lexicon. Deliberately blunt: it only ever produces a tone
# label that the agents are allowed to cite as a count, never as a number
# dressed up to look like real sentiment analysis.
_POSITIVE_WORDS = {
    "surge", "surges", "surged", "jump", "jumps", "jumped", "rally", "rallies",
    "rallied", "gain", "gains", "gained", "rise", "rises", "rose", "beat",
    "beats", "record", "high", "highs", "upgrade", "upgrades", "upgraded",
    "outperform", "buy", "bullish", "profit", "profits", "growth", "wins",
    "win", "order", "orders", "expansion", "expands", "strong", "boost",
    "boosts", "raises", "raised", "approval", "approved", "launch", "launches",
    "partnership", "deal", "acquire", "acquires", "acquisition", "dividend",
    "bonus", "top", "soar", "soars", "soared", "positive", "optimistic",
}
_NEGATIVE_WORDS = {
    "fall", "falls", "fell", "drop", "drops", "dropped", "slump", "slumps",
    "slumped", "plunge", "plunges", "plunged", "decline", "declines",
    "declined", "loss", "losses", "miss", "misses", "missed", "downgrade",
    "downgrades", "downgraded", "underperform", "sell", "bearish", "weak",
    "weakness", "cut", "cuts", "probe", "raid", "fraud", "penalty", "fine",
    "fined", "lawsuit", "ban", "banned", "recall", "resign", "resigns",
    "resigned", "warning", "warns", "risk", "risks", "crash", "crashes",
    "slide", "slides", "slid", "low", "lows", "negative", "concern",
    "concerns", "delay", "delays", "delayed", "default", "downtrend",
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _clean(value):
    """Return a plain float/int, or None for anything unusable (NaN, inf, '')."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _round(value, digits=2):
    value = _clean(value)
    return None if value is None else round(value, digits)


def now_ist_str() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %H:%M:%S IST")


def load_full_exchange(log=None) -> dict:
    """All equities in the official NSE trading lists; never filter by liquidity."""
    import stock_universe
    universe, metadata = stock_universe.load_exchange_universe(log=log)
    globals()["LAST_UNIVERSE_METADATA"] = metadata
    return universe


def load_universe(path: str = UNIVERSE_FILE) -> dict:
    """Read universe.json -> {"large": [ {ticker,name,sector}, ... ], ...}."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    universe = {}
    for bucket in BUCKETS:
        entries = []
        for item in raw.get(bucket, []) or []:
            if isinstance(item, str):                     # bare "TCS.NS" is fine too
                entries.append({"ticker": item, "name": item.split(".")[0], "sector": None})
            elif isinstance(item, dict) and item.get("ticker"):
                entries.append({
                    "ticker": item["ticker"],
                    "name": item.get("name") or item["ticker"].split(".")[0],
                    "sector": item.get("sector"),
                })
        universe[bucket] = entries
    return universe


def universe_size(universe: dict) -> int:
    return sum(len(v) for v in universe.values())


def score_headline(title: str) -> str:
    """positive | negative | neutral for one headline."""
    words = re.findall(r"[a-zA-Z]+", (title or "").lower())
    pos = sum(1 for w in words if w in _POSITIVE_WORDS)
    neg = sum(1 for w in words if w in _NEGATIVE_WORDS)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _empty_intraday(reason):
    """
    The intraday block when there is no session behind it.

    `available: False` is a first-class state, not a data gap: at 09:00 there
    is genuinely nothing to measure yet, and the agents are told to say so
    rather than reach for yesterday's numbers.
    """
    return {
        "available": False,
        "reason": reason,
        "bars": 0,
        "gap_pct": None,
        "opening_range_high": None,
        "opening_range_low": None,
        "opening_range_pct": None,
        "above_opening_range": None,
        "vwap": None,
        "price_vs_vwap_pct": None,
        "session_volume": None,
        "session_high": None,
        "session_low": None,
        "last_bar": None,
    }


def _empty_evidence(symbol, name, ticker, bucket, sector):
    """Skeleton with every field None, so a partial build is still well-formed."""
    return {
        "symbol": symbol,
        "ticker": ticker,
        "name": name,
        "cap_segment": bucket,
        "sector": sector,
        "as_of": now_ist_str(),
        "source": "live",
        "price": {
            "live": None, "day_open": None, "day_high": None, "day_low": None,
            "prev_close": None, "day_change_pct": None, "volume": None,
        },
        "range_52w": {"high": None, "low": None, "pct_from_high": None, "position_pct": None},
        "technicals": {
            "rvol": None, "rvol_raw": None, "rvol_method": None,
            "price_vs_sma_pct": None, "sma_period": SMA_PERIOD,
            "window_return_pct": None, "swing_high": None, "swing_low": None,
            "day_range_position_pct": None, "trend": None, "atr_pct": None,
            "move_vs_atr": None,
        },
        "intraday": _empty_intraday("not built"),
        "market": market.describe(),
        "regime": {"state": "unknown", "reason": "regime evidence not supplied"},
        "relative": {
            "benchmark": BENCHMARK_NAME, "benchmark_day_change_pct": None,
            "benchmark_window_return_pct": None, "rel_day_change_pct": None,
            "rel_window_return_pct": None, "outperforming": None,
            "sector": None, "sector_median_pct": None, "sector_rel_pct": None,
        },
        "events": {"next_earnings": None, "days_to_earnings": None,
                   "earnings_inside_horizon": None},
        "analyst": {
            "consensus": None, "num_analysts": None, "buy_pct": None, "hold_pct": None,
            "sell_pct": None, "target_mean": None, "target_low": None,
            "target_high": None, "upside_pct": None,
        },
        "news": {"total": None, "positive": None, "negative": None, "neutral": None,
                 "net_tone": None, "recent": []},
        "data_gaps": [],
        "notes": [NO_RATIOS_NOTE],
    }


def _finalise_gaps(evidence: dict) -> dict:
    """Walk the bundle and name every field that ended up None."""
    gaps = list(evidence.get("data_gaps") or [])

    def walk(prefix, node):
        for key, value in node.items():
            if key in ("recent", "sma_period", "rvol_method", "rvol_raw"):
                continue   # metadata about a measurement, not a measurement
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                walk(path, value)
            elif value is None and path not in gaps:
                gaps.append(path)

    for section in ("price", "range_52w", "technicals", "analyst", "news", "fundamentals"):
        walk(section, evidence.get(section) or {})

    if not evidence.get("sector"):
        evidence["sector"] = None
        if "sector" not in gaps:
            gaps.append("sector")

    evidence["data_gaps"] = gaps
    return evidence


# --------------------------------------------------------------------------
# demo mode
# --------------------------------------------------------------------------

def load_demo_bundles(demo_dir: str = DEMO_DIR) -> list:
    """Load every demo_data/*.json evidence bundle, sorted by symbol."""
    bundles = []
    for path in sorted(glob.glob(os.path.join(demo_dir, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                bundle = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(bundle, dict) or not bundle.get("symbol"):
            continue
        bundle.setdefault("source", "demo")
        bundle.setdefault("notes", [NO_RATIOS_NOTE])
        bundle.setdefault("data_gaps", [])
        # A frozen bundle has no live session behind it, so the intraday track
        # is honestly unavailable rather than replayed from a stale snapshot.
        bundle.setdefault("intraday", _empty_intraday(
            "demo bundle — a frozen snapshot has no live session to read"))
        bundle.setdefault("market", market.describe())
        bundle.setdefault("regime", {"state": "unknown", "reason": "offline demo snapshot"})
        # a frozen bundle has no index alongside it and no forward calendar
        bundle.setdefault("relative", {
            "benchmark": BENCHMARK_NAME, "benchmark_day_change_pct": None,
            "benchmark_window_return_pct": None, "rel_day_change_pct": None,
            "rel_window_return_pct": None, "outperforming": None,
        })
        bundle.setdefault("events", {"next_earnings": None, "days_to_earnings": None,
                                     "earnings_inside_horizon": None})
        bundles.append(_finalise_gaps(bundle))
    bundles.sort(key=lambda b: b["symbol"])
    return bundles


# --------------------------------------------------------------------------
# live mode — yfinance
# --------------------------------------------------------------------------

def _import_yf():
    try:
        import yfinance as yf  # noqa: WPS433 (import at call time is deliberate)
    except ImportError as exc:                      # pragma: no cover - env dependent
        raise RuntimeError(
            "yfinance is not installed — run: pip install -r requirements.txt"
        ) from exc
    return yf


def _frame_for(downloaded, ticker, single):
    """Pull one ticker's OHLC frame out of a yf.download result."""
    if downloaded is None or getattr(downloaded, "empty", True):
        return None
    if getattr(downloaded.columns, "nlevels", 1) == 1:
        return _valid_frame(downloaded) if single else None
    try:
        frame = downloaded[ticker]
    except (KeyError, TypeError):
        try:
            frame = downloaded.xs(ticker, axis=1, level=1)
        except (KeyError, TypeError, ValueError):
            return None
    return _valid_frame(frame)


def _valid_frame(frame):
    """Keep OHLCV rows aligned: dropping each column separately corrupts bars."""
    if frame is None or getattr(frame, "empty", True):
        return None
    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(c not in frame.columns for c in required):
        return None
    import pandas as pd
    frame = frame[required].copy().apply(pd.to_numeric, errors="coerce")
    frame = frame.replace([float("inf"), -float("inf")], float("nan"))
    try:
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    except (TypeError, ValueError):
        return None
    frame = frame[~frame.index.isna()]
    frame = frame.dropna(subset=required).sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    frame = frame[(frame[["Open", "High", "Low", "Close"]] > 0).all(axis=1)
                  & (frame["Volume"] >= 0) & (frame["High"] >= frame["Low"])
                  & (frame["High"] >= frame[["Open", "Close"]].max(axis=1))
                  & (frame["Low"] <= frame[["Open", "Close"]].min(axis=1))]
    return None if frame.empty else frame


def _session_frame(frame, moment=None):
    """Aligned bars belonging to today's NSE continuous session, in IST."""
    frame = _valid_frame(frame)
    if frame is None:
        return None
    moment = moment or market.now_ist()
    moment = moment.replace(tzinfo=IST) if moment.tzinfo is None else moment.astimezone(IST)
    try:
        frame.index = (frame.index.tz_localize(IST) if frame.index.tz is None
                       else frame.index.tz_convert(IST))
    except (AttributeError, TypeError, ValueError):
        return None
    start = moment.replace(hour=9, minute=15, second=0, microsecond=0)
    close = moment.replace(hour=15, minute=30, second=0, microsecond=0)
    frame = frame[(frame.index >= start) & (frame.index < close) & (frame.index <= moment)]
    return None if frame.empty else frame


def _setting_int(key, default, low=1, high=500):
    try:
        return max(low, min(high, int(os.environ.get(key, default))))
    except (TypeError, ValueError, OverflowError):
        return default


def download_frames(tickers, period=HISTORY_PERIOD, interval="1d", log=None):
    """Bounded batches and retries; a failing symbol cannot discard its peers."""
    say = log or (lambda _m: None)
    tickers = list(dict.fromkeys(tickers))
    frames = {}
    if not tickers:
        return frames
    yf = _import_yf()
    size = _setting_int("MARKET_BATCH_SIZE", 100, high=200)
    workers = _setting_int("MARKET_DOWNLOAD_THREADS", 4, high=8)
    retries = _setting_int("MARKET_DOWNLOAD_RETRIES", 1, low=0, high=2)
    timeout = _setting_int("MARKET_DATA_TIMEOUT", 15, high=60)
    for start in range(0, len(tickers), size):
        pending = tickers[start:start + size]
        for attempt in range(retries + 1):
            try:
                downloaded = yf.download(tickers=pending, period=period, interval=interval,
                                         group_by="ticker", auto_adjust=False, actions=False,
                                         progress=False, threads=workers, timeout=timeout)
            except Exception as exc:
                say(f"{interval} batch failed ({type(exc).__name__}), attempt {attempt + 1}")
                downloaded = None
            for ticker in pending:
                frame = _frame_for(downloaded, ticker, len(pending) == 1)
                if frame is not None:
                    frames[ticker] = frame
            pending = [ticker for ticker in pending if ticker not in frames]
            if not pending:
                break
            if attempt < retries:
                time.sleep(min(2 ** attempt, 4))
        say(f"{interval} coverage: attempted {min(start + size, len(tickers))}/{len(tickers)}, "
            f"{len(frames)} with usable bars")
    return frames


def _series_values(frame, column):
    """Column of a frame as a list of clean floats (NaN rows dropped)."""
    if frame is None or column not in frame:
        return []
    return [v for v in (_clean(x) for x in frame[column].tolist()) if v is not None]


def screen_score(quote) -> float:
    """
    How interesting is this stock today?

    Raw day change is a poor screen on its own: it ranks a stock up 5% on dead
    volume above one up 2% on four times its usual volume, and it cannot tell
    a real move from the whole index rising together. So the screen ranks on
    strength *relative to the NIFTY*, adjusted for participation:

        score = relative day change % + 1.5 x (RVOL - 1)

    Volume shifts the ranking without being able to dominate it.
    """
    change = quote.get("rel_day_change_pct")
    if change is None:
        change = quote.get("day_change_pct")
    if change is None:
        return -999.0

    rvol = quote.get("rvol")
    boost = 0.0 if rvol is None else 1.5 * (min(max(rvol, 0.0), 4.0) - 1.0)
    return change + boost


def screen_bucket(quotes: list, top_n: int) -> list:
    """Keep the top N of a bucket by relative strength and participation."""
    ranked = sorted(
        [q for q in quotes if q.get("day_change_pct") is not None],
        key=screen_score,
        reverse=True,
    )
    unscored = [q for q in quotes if q.get("day_change_pct") is None]
    return (ranked + unscored)[:top_n]


def fetch_quotes(universe: dict, log=None) -> dict:
    """
    Cheap first pass: one batched history download for the whole universe.

    Returns {bucket: [ {ticker,name,sector,bucket,day_change_pct,frame}, ... ]}
    so the caller can screen before paying for .info / .news per stock.
    """
    say = log or (lambda _m: None)

    tickers = [e["ticker"] for bucket in BUCKETS for e in universe.get(bucket, [])]
    if not tickers:
        return {b: [] for b in BUCKETS}, {}

    # The NIFTY rides along in the same request. Without it there is no way to
    # tell a stock that is genuinely strong from one drifting up with the index.
    say(f"downloading {HISTORY_PERIOD} daily OHLC for {len(tickers)} tickers + {BENCHMARK_NAME}")
    downloaded = download_frames(tickers + [BENCHMARK], log=say)

    benchmark = _benchmark_block(downloaded.get(BENCHMARK))
    if benchmark.get("day_change_pct") is not None:
        say(f"{BENCHMARK_NAME} {benchmark['day_change_pct']:+.2f}% today, "
            f"{benchmark['window_return_pct']:+.2f}% over the window")
    else:
        say(f"{BENCHMARK_NAME} unavailable — relative strength will be reported "
            f"as data unavailable")

    session = market.describe()

    # Sector medians across the whole universe. An IT stock down 1% on a day
    # its sector is down 3% is quietly strong, and neither the raw move nor
    # the NIFTY comparison can show that.
    sector_moves = {}
    for bucket in BUCKETS:
        for entry in universe.get(bucket, []):
            frame = downloaded.get(entry["ticker"])
            closes = _series_values(frame, "Close")
            if (len(closes) >= 2 and closes[-2] and entry.get("sector")
                    and frame.index[-1].date().isoformat() == benchmark.get("last_bar")):
                sector_moves.setdefault(entry["sector"], []).append(
                    (closes[-1] - closes[-2]) / closes[-2] * 100.0)
    sector_median = {
        sector: median(values)
        for sector, values in sector_moves.items() if len(values) >= 2
    }

    quotes = {}
    for bucket in BUCKETS:
        rows = []
        for entry in universe.get(bucket, []):
            frame = downloaded.get(entry["ticker"])
            closes = _series_values(frame, "Close")
            volumes = _series_values(frame, "Volume")

            change = None
            if len(closes) >= 2 and closes[-2]:
                change = (closes[-1] - closes[-2]) / closes[-2] * 100.0

            # RVOL at screen time, so the shortlist can weigh participation
            rvol = None
            if len(volumes) >= 3:
                prior = [v for v in volumes[-21:-1] if v > 0]
                if prior and volumes[-1] > 0:
                    same_session = frame.index[-1].date() == market.now_ist().date()
                    rvol = (volumes[-1] / (sum(prior) / len(prior))
                            / market.volume_divisor(session["session_fraction"] if same_session else 1.0))

            relative = None
            same_day = frame is not None and frame.index[-1].date().isoformat() == benchmark.get("last_bar")
            if same_day and change is not None and benchmark.get("day_change_pct") is not None:
                relative = change - benchmark["day_change_pct"]

            peer_median = sector_median.get(entry.get("sector")) if same_day else None
            sector_rel = (change - peer_median
                          if change is not None and peer_median is not None else None)

            rows.append({
                **entry,
                "ticker": entry["ticker"],
                "name": entry["name"],
                "sector": entry.get("sector"),
                "bucket": bucket,
                "day_change_pct": _round(change),
                "rel_day_change_pct": _round(relative),
                "sector_rel_pct": _round(sector_rel),
                "sector_median_pct": _round(peer_median),
                "rvol": _round(rvol),
                "frame": frame,
            })
        quotes[bucket] = rows
    return quotes, benchmark


def _benchmark_block(frame) -> dict:
    """Today's move and window return for the index, or nulls."""
    closes = _series_values(frame, "Close")[-23:]
    out = {
        "name": BENCHMARK_NAME, "ticker": BENCHMARK,
        "last": None, "day_change_pct": None, "window_return_pct": None,
        "last_bar": frame.index[-1].date().isoformat() if frame is not None and len(frame) else None,
        "window_start": frame.index[-min(23, len(frame))].date().isoformat() if frame is not None and len(frame) else None,
    }
    if len(closes) >= 2:
        out["last"] = _round(closes[-1])
        if closes[-2]:
            out["day_change_pct"] = _round((closes[-1] - closes[-2]) / closes[-2] * 100.0)
        if closes[0]:
            out["window_return_pct"] = _round((closes[-1] - closes[0]) / closes[0] * 100.0)
    return out


def build_evidence_live(quote: dict, log=None) -> dict:
    """Turn one screened quote (with its OHLC frame) into a full evidence bundle."""
    import company_data
    say = log or (lambda _m: None)
    ticker = quote["ticker"]
    symbol = ticker.rsplit(".", 1)[0]
    ev = _empty_evidence(symbol, quote.get("name") or symbol, ticker,
                         quote.get("bucket"), quote.get("sector"))
    profile = quote.get("company_data")
    if profile is None:
        profile = company_data.fetch_company_data(ticker, log=say)
    info = profile.get("info") or {}
    info_reco = profile.get("recommendations") or {}
    ev["company_data"] = profile.get("metadata") or {}
    ev["fundamentals"] = company_data.build_fundamentals(info, profile.get("metadata"))
    ev["news"] = profile.get("news") or ev["news"]
    ev["evidence_scope"] = quote.get("evidence_scope", "enriched")
    ev["notes"] = ["Fundamental ratios are provider-reported; retrieval time is not a filing date. "
                   "Missing metrics are unavailable. Analyst targets are opinions, not fair values."]
    if info.get("longName") or info.get("shortName"):
        ev["name"] = info.get("longName") or info.get("shortName")
    if info.get("sector"):
        ev["sector"] = info["sector"]
    frame = _valid_frame(quote.get("frame"))
    closes = _series_values(frame, "Close")
    highs = _series_values(frame, "High")
    lows = _series_values(frame, "Low")
    opens = _series_values(frame, "Open")
    volumes = _series_values(frame, "Volume")
    daily_date = frame.index[-1].date() if frame is not None else None
    daily_as_of = daily_date.isoformat() if daily_date else None
    intra = _session_frame(quote.get("intraday_frame"))
    today = market.now_ist().date()
    bar_dates = [stamp.date().isoformat() for stamp in frame.index] if frame is not None else []
    live = closes[-1] if closes else None
    prev_close = closes[-2] if len(closes) >= 2 else None
    price_as_of = daily_as_of
    price_source = "yfinance daily bars"
    if intra is not None:
        live = float(intra["Close"].iloc[-1])
        price_as_of = intra.index[-1].isoformat()
        price_source = "yfinance 5m bars"
        prev_close = closes[-2] if daily_date == today and len(closes) >= 2 else (closes[-1] if closes else None)
        session_values = (float(intra["Open"].iloc[0]), float(intra["High"].max()),
                          float(intra["Low"].min()), live, float(intra["Volume"].sum()))
        for values, value in zip((opens, highs, lows, closes, volumes), session_values):
            if daily_date == today and values:
                values[-1] = value
            else:
                values.append(value)
        if daily_date != today:
            bar_dates.append(today.isoformat())
    elif live is None:
        candidates = (_clean(info.get(key)) for key in ("currentPrice", "regularMarketPrice"))
        live = next((value for value in candidates if value is not None and value > 0), None)
        prev_close = _clean(info.get("regularMarketPreviousClose"))
        provider_time = _clean(info.get("regularMarketTime"))
        if provider_time is not None:
            try:
                price_as_of = datetime.fromtimestamp(provider_time, IST).isoformat()
            except (ValueError, OverflowError, OSError):
                price_as_of = None
        price_source = "yfinance profile (fallback)"
    ev["price"] = {
        "live": _round(live), "as_of": price_as_of, "source": price_source,
        "day_open": _round(opens[-1] if opens else None),
        "day_high": _round(highs[-1] if highs else None),
        "day_low": _round(lows[-1] if lows else None),
        "prev_close": _round(prev_close),
        "day_change_pct": _round((live - prev_close) / prev_close * 100.0
                                 if live is not None and prev_close else None),
        "volume": int(volumes[-1]) if volumes else None,
    }

    # ---- 52-week range ---------------------------------------------------
    hi52 = max(highs[-252:]) if len(highs) >= 252 else _clean(info.get("fiftyTwoWeekHigh"))
    lo52 = min(lows[-252:]) if len(lows) >= 252 else _clean(info.get("fiftyTwoWeekLow"))
    position = pct_from_high = None
    if live is not None and hi52 and lo52 is not None and hi52 > lo52:
        position = (live - lo52) / (hi52 - lo52) * 100.0
        pct_from_high = (live - hi52) / hi52 * 100.0
    ev["range_52w"] = {
        "high": _round(hi52), "low": _round(lo52),
        "pct_from_high": _round(pct_from_high), "position_pct": _round(position),
    }

    # ---- market phase, technicals, intraday -------------------------------
    phase_info = market.describe()  # cached profile marketState is not a fresh session signal
    ev["market"] = phase_info
    ev["regime"] = quote.get("regime") or market.regime(log=say)
    fraction = phase_info["session_fraction"] if daily_date == today or intra is not None else 1.0
    ev["technicals"].update(_technicals(closes[-23:], highs[-23:], lows[-23:], volumes[-23:], live,
                                        ev["price"], fraction))
    ev["technicals"]["last_bar"] = bar_dates[-1] if bar_dates else None
    ev["technicals"]["window_start"] = bar_dates[-min(23, len(bar_dates))] if bar_dates else None
    ev["technicals"]["history_bars"] = len(closes)
    ev["technicals"]["observations"] = len(closes)
    ev["technicals"]["window_sessions"] = min(23, len(closes))
    ev["technicals"]["price_vs_sma50_pct"] = _round((live / (sum(closes[-50:]) / 50) - 1) * 100) if len(closes) >= 50 and live else None
    ev["technicals"]["price_vs_sma200_pct"] = _round((live / (sum(closes[-200:]) / 200) - 1) * 100) if len(closes) >= 200 and live else None
    turnover = [c * v for c, v in zip(closes[-21:-1], volumes[-21:-1])]
    ev["liquidity"] = {"average_daily_turnover_inr": _round(sum(turnover) / len(turnover)) if turnover else None,
                       "sample_sessions": len(turnover), "excluded_from_analysis": False}
    ev["intraday"] = build_intraday(intra,
                                    ev["price"]["prev_close"], live, phase_info)

    # ---- analyst ---------------------------------------------------------
    target_mean = _clean(info.get("targetMeanPrice"))
    ev["analyst"] = {
        "consensus": info.get("recommendationKey") or None,
        "num_analysts": int(_clean(info.get("numberOfAnalystOpinions")) or 0) or None,
        "buy_pct": info_reco.get("buy_pct"),
        "hold_pct": info_reco.get("hold_pct"),
        "sell_pct": info_reco.get("sell_pct"),
        "target_mean": _round(target_mean),
        "target_low": _round(_clean(info.get("targetLowPrice"))),
        "target_high": _round(_clean(info.get("targetHighPrice"))),
        "upside_pct": _round(((target_mean - live) / live * 100.0)
                             if (target_mean and live) else None),
    }

    # ---- relative strength vs the index ------------------------------------
    benchmark = quote.get("benchmark") or {}
    day_change = ev["price"]["day_change_pct"]
    window = ev["technicals"]["window_return_pct"]
    same_day = benchmark.get("last_bar") == ev["technicals"].get("last_bar") and benchmark.get("last_bar") is not None
    same_window = same_day and benchmark.get("window_start") == ev["technicals"].get("window_start") and benchmark.get("window_start") is not None
    rel_day = (day_change - benchmark["day_change_pct"]
               if same_day and day_change is not None and benchmark.get("day_change_pct") is not None
               else None)
    rel_window = (window - benchmark["window_return_pct"]
                  if same_window and window is not None and benchmark.get("window_return_pct") is not None
                  else None)
    ev["relative"] = {
        "benchmark": benchmark.get("name") or BENCHMARK_NAME,
        "benchmark_day_change_pct": benchmark.get("day_change_pct"),
        "benchmark_window_return_pct": benchmark.get("window_return_pct"),
        "rel_day_change_pct": _round(rel_day),
        "rel_window_return_pct": _round(rel_window),
        "outperforming": None if rel_day is None else bool(rel_day > 0),
        "comparison_aligned": same_window,
        "sector": quote.get("sector"),
        "sector_median_pct": quote.get("sector_median_pct"),
        "sector_rel_pct": quote.get("sector_rel_pct"),
    }

    # ---- calendar ----------------------------------------------------------
    # Holding through an earnings print is a materially different risk from
    # holding a quiet stock, and the positional horizon is often long enough
    # to span one. Worth naming rather than discovering afterwards.
    ev["events"] = _events_block(info, ev["technicals"].get("atr_pct"))

    ev["source"] = "live"
    ev["as_of"] = now_ist_str()
    return _finalise_gaps(ev)


def _technicals(closes, highs, lows, volumes, live, price_block,
                session_fraction=1.0) -> dict:
    """RVOL, SMA distance, window return, swings, day-range position, trend."""
    out = {
        "rvol": None, "rvol_raw": None, "rvol_method": None,
        "price_vs_sma_pct": None, "sma_period": SMA_PERIOD,
        "window_return_pct": None, "swing_high": None, "swing_low": None,
        "day_range_position_pct": None, "trend": None, "atr_pct": None,
        "move_vs_atr": None,
    }

    ref = live if live is not None else (closes[-1] if closes else None)

    # Relative volume: today against the average of every *prior* day.
    #
    # Mid-session this needs scaling. Two hours into a six-and-a-quarter hour
    # day, a stock trading exactly its normal volume has only printed a third
    # of it — comparing that with a full-day average makes every stock look
    # dead. We divide the benchmark by the fraction of the session elapsed and
    # keep the unscaled figure alongside it so nothing is hidden.
    if len(volumes) >= 3:
        prior = [v for v in volumes[-21:-1] if v > 0]
        if prior and volumes[-1] > 0:
            average = sum(prior) / len(prior)
            raw = volumes[-1] / average
            divisor = market.volume_divisor(session_fraction)
            out["rvol_raw"] = _round(raw)
            out["rvol"] = _round(raw / divisor)
            out["rvol_method"] = (
                "full session" if divisor >= 0.999
                else f"scaled to {round(divisor * 100)}% of session elapsed"
            )

    # Average true range over the daily window, as a % of price — the unit the
    # positional track uses to turn "distance to target" into "how long".
    if len(closes) >= 3 and len(highs) == len(lows) == len(closes):
        trs = []
        for i in range(1, len(closes)):
            trs.append(max(highs[i] - lows[i],
                           abs(highs[i] - closes[i - 1]),
                           abs(lows[i] - closes[i - 1])))
        window = trs[-ATR_PERIOD:]
        if window and ref:
            out["atr_pct"] = _round(sum(window) / len(window) / ref * 100.0)

    # How big is today's move in units of this stock's own normal day? A 6%
    # move is unremarkable for a stock that swings 4% daily and extraordinary
    # for one that swings 1%. Comparing raw percentages across stocks hides
    # exactly that, and buying the third standard deviation of a move is how
    # a breakout entry turns into a top tick.
    day_change = price_block.get("day_change_pct")
    if day_change is not None and out["atr_pct"]:
        out["move_vs_atr"] = _round(abs(day_change) / out["atr_pct"])

    sma = None
    if closes:
        window = closes[-SMA_PERIOD:] if len(closes) >= SMA_PERIOD else closes
        if len(window) >= SMA_PERIOD:
            sma = sum(window) / len(window)
            if ref is not None and sma:
                out["price_vs_sma_pct"] = _round((ref - sma) / sma * 100.0)

    if len(closes) >= 2 and closes[0]:
        out["window_return_pct"] = _round((closes[-1] - closes[0]) / closes[0] * 100.0)

    if highs:
        out["swing_high"] = _round(max(highs))
    if lows:
        out["swing_low"] = _round(min(lows))

    day_high = price_block.get("day_high")
    day_low = price_block.get("day_low")
    if ref is not None and day_high is not None and day_low is not None and day_high > day_low:
        out["day_range_position_pct"] = _round((ref - day_low) / (day_high - day_low) * 100.0)

    # trend: price vs SMA, confirmed by the slope of the SMA itself
    if sma and ref is not None and len(closes) >= SMA_PERIOD + 3:
        older = closes[-(SMA_PERIOD + 3):-3]
        prev_sma = sum(older) / len(older) if older else None
        if prev_sma:
            rising = sma > prev_sma
            falling = sma < prev_sma
            if ref > sma and rising:
                out["trend"] = "up"
            elif ref < sma and falling:
                out["trend"] = "down"
            else:
                out["trend"] = "sideways"
    elif sma and ref is not None:
        out["trend"] = "sideways"

    return out


def build_intraday(frame, prev_close, live, phase_info) -> dict:
    """
    Today's session shape from 5-minute bars: gap, opening range, VWAP.

    Returns `available: False` with a stated reason whenever there is no
    session to read — before the open, on a holiday, or when the feed simply
    did not return bars. Nothing here is ever back-filled from yesterday.
    """
    if not phase_info.get("live_session") and phase_info.get("phase") != market.POST:
        return _empty_intraday(
            f"no live session — {phase_info.get('label', 'market closed')}")

    frame = _session_frame(frame)
    if frame is None:
        return _empty_intraday("feed returned no intraday bars for today")

    opens = _series_values(frame, "Open")
    highs = _series_values(frame, "High")
    lows = _series_values(frame, "Low")
    closes = _series_values(frame, "Close")
    volumes = _series_values(frame, "Volume")

    if not closes or not volumes:
        return _empty_intraday("intraday bars carried no usable prices")

    out = _empty_intraday("")
    out["available"] = True
    out["reason"] = ""
    out["bars"] = len(closes)

    ref = live if live is not None else closes[-1]

    session_open = opens[0] if opens else None
    if session_open is not None and prev_close:
        out["gap_pct"] = _round((session_open - prev_close) / prev_close * 100.0)

    # Opening range: the first 15 minutes. Breaking it is the classic
    # intraday continuation trigger, holding below it the classic failure.
    session_start = market.now_ist().replace(hour=9, minute=15, second=0, microsecond=0)
    expected_open = [session_start + timedelta(minutes=5 * i) for i in range(OPENING_RANGE_BARS)]
    out["opening_range_complete"] = (all(stamp in frame.index for stamp in expected_open)
                                     and market.now_ist() >= session_start + timedelta(minutes=15))
    expected_count = int((frame.index[-1] - session_start).total_seconds() // 300) + 1
    out["coverage_complete"] = frame.index[0] == session_start and len(frame) == expected_count
    if not out["coverage_complete"]:
        out["reason"] = "session bars have gaps; VWAP uses only the available bars"
    if out["opening_range_complete"]:
        opening = frame.loc[expected_open]
        or_high = float(opening["High"].max())
        or_low = float(opening["Low"].min())
        out["opening_range_high"] = _round(or_high)
        out["opening_range_low"] = _round(or_low)
        if or_low:
            out["opening_range_pct"] = _round((or_high - or_low) / or_low * 100.0)
        if ref is not None:
            out["above_opening_range"] = bool(ref > or_high)

    # VWAP from typical price, the reference intraday participants actually use
    if len(closes) == len(volumes) and sum(volumes) > 0:
        n = min(len(closes), len(highs), len(lows), len(volumes))
        turnover = sum(((highs[i] + lows[i] + closes[i]) / 3.0) * volumes[i]
                       for i in range(n))
        traded = sum(volumes[:n])
        if traded > 0:
            vwap = turnover / traded
            out["vwap"] = _round(vwap)
            if ref is not None and vwap:
                out["price_vs_vwap_pct"] = _round((ref - vwap) / vwap * 100.0)

    out["session_volume"] = int(sum(volumes))
    out["session_high"] = _round(max(highs)) if highs else None
    out["session_low"] = _round(min(lows)) if lows else None

    try:
        out["last_bar"] = str(frame.index[-1])
    except (AttributeError, IndexError):
        out["last_bar"] = None

    return out


SWING_HISTORY_PERIOD = "2y"   # 52-week high and 12-1 momentum need a year+


def fetch_swing_history(tickers, log=None):
    """Bounded two-year daily history downloads."""
    return download_frames(tickers, period=SWING_HISTORY_PERIOD, log=log)


def fired_strategies(quote) -> dict:
    """
    The named rules triggering on one stock: {"intraday": {...}, "swing": {...}}.

    Kept separate from the scoring so the two can be reasoned about apart —
    this only observes what fired, and strategy_edge decides what that is
    worth based on how the rule has actually performed.
    """
    import strategies
    import swing_strategies

    out = {"intraday": {}, "swing": {}}

    frame = _session_frame(quote.get("intraday_frame"))
    if frame is not None and not getattr(frame, "empty", True):
        bars = [{"open": o, "high": h, "low": l, "close": c, "volume": v}
                for o, h, l, c, v in zip(_series_values(frame, "Open"),
                                         _series_values(frame, "High"),
                                         _series_values(frame, "Low"),
                                         _series_values(frame, "Close"),
                                         _series_values(frame, "Volume"))]
        daily = _valid_frame(quote.get("frame"))
        prev_close = None
        if daily is not None and not getattr(daily, "empty", True):
            closes = _series_values(daily, "Close")
            prev_close = (closes[-2] if daily.index[-1].date() == market.now_ist().date() and len(closes) >= 2
                          else closes[-1] if closes and daily.index[-1].date() != market.now_ist().date() else None)
        s = strategies.session(bars, prev_close=prev_close, rvol=quote.get("rvol"))
        if s:
            for name, fn in strategies.STRATEGIES.items():
                try:
                    setup = fn(s)
                except Exception:                                  # noqa: BLE001
                    setup = None
                if setup:
                    out["intraday"][name] = setup["why"]

    # the long frame when the shortlist paid for one, else the screener's month
    # (which is too short for these rules and will simply return nothing)
    daily = quote.get("history_frame")
    if daily is None or getattr(daily, "empty", True):
        daily = quote.get("frame")
    if daily is not None and not getattr(daily, "empty", True):
        try:
            signals = swing_strategies.signals_now(
                _series_values(daily, "High"), _series_values(daily, "Low"),
                _series_values(daily, "Close"), _series_values(daily, "Volume"))
        except Exception:                                          # noqa: BLE001
            signals = {}
        for name, sig in signals.items():
            out["swing"][name] = sig["why"]

    return out


def fetch_intraday(tickers, log=None):
    """Bounded downloads of current-session bars, with per-symbol failure isolation."""
    return download_frames(tickers, period="1d", interval=INTRADAY_INTERVAL, log=log)


def refresh_evidence(bundles, log=None):
    """Refresh narrative candidates after an all-stock scan, preserving order.

    Every returned bundle is rebuilt from these downloads and cached company
    evidence. Failed market downloads never reuse the previous candidate price.
    """
    import company_data
    say = log or (lambda _m: None)
    bundles = list(bundles)
    if not bundles:
        return []
    tickers = list(dict.fromkeys(bundle.get("ticker") or f"{bundle.get('symbol')}.NS"
                                for bundle in bundles))
    say(f"refreshing market evidence for {len(tickers)} narrative candidates")
    try:
        daily = download_frames(tickers + [BENCHMARK], log=say)
    except Exception as exc:
        say(f"candidate daily refresh failed ({type(exc).__name__})")
        daily = {}
    try:
        intraday = fetch_intraday(tickers, log=say) if market.describe().get("live_session") else {}
    except Exception as exc:
        say(f"candidate session refresh failed ({type(exc).__name__})")
        intraday = {}
    benchmark = _benchmark_block(daily.get(BENCHMARK))
    try:
        regime = market.regime(log=say)
    except Exception:
        regime = {"state": "unknown", "reason": "regime refresh unavailable"}
    output = []
    for previous in bundles:
        ticker = previous.get("ticker") or f"{previous.get('symbol')}.NS"
        frame = daily.get(ticker)
        session = _session_frame(intraday.get(ticker))
        try:
            profile = company_data.fetch_company_data(ticker, log=say)
            quote = {"ticker": ticker, "name": previous.get("name"),
                     "bucket": previous.get("cap_segment"), "sector": previous.get("sector"),
                     "frame": frame, "history_frame": frame, "intraday_frame": session,
                     "benchmark": benchmark, "regime": regime, "company_data": profile,
                     "evidence_scope": "enriched"}
            bundle = build_evidence_live(quote, log=say)
            if frame is None and session is None:
                # Cached profile prices remain useful provenance, but a failed
                # explicit market refresh cannot certify them as current.
                bundle["price"].update(live=None, as_of=None, source="refresh unavailable")
                bundle["data_gaps"].extend(["price.live", "price.as_of"])
                bundle["notes"].append("market refresh returned no bars; previous price was not reused")
                bundle["refresh_status"] = "unavailable"
            else:
                quote["rvol"] = bundle["technicals"].get("rvol")
                bundle["strategies"] = fired_strategies(quote)
                bundle["refresh_status"] = "updated"
        except Exception as exc:
            bundle = _empty_evidence(previous.get("symbol") or ticker.rsplit(".", 1)[0],
                                     previous.get("name"), ticker,
                                     previous.get("cap_segment"), previous.get("sector"))
            bundle["refresh_status"] = "unavailable"
            bundle["notes"].append(f"candidate refresh failed ({type(exc).__name__}); previous evidence was not reused")
            bundle = _finalise_gaps(bundle)
            say(f"{ticker}: candidate refresh unavailable ({type(exc).__name__})")
        output.append(bundle)
    return output


def _recommendation_split(handle, symbol, say) -> dict:
    """buy/hold/sell percentages from the analyst recommendation table."""
    try:
        table = handle.recommendations
    except Exception as exc:                                      # noqa: BLE001
        say(f"{symbol}: recommendations unavailable ({type(exc).__name__})")
        return {}
    if table is None or getattr(table, "empty", True):
        return {}

    try:
        row = table.iloc[0].to_dict()
        strong_buy = _clean(row.get("strongBuy")) or 0
        buy = _clean(row.get("buy")) or 0
        hold = _clean(row.get("hold")) or 0
        sell = _clean(row.get("sell")) or 0
        strong_sell = _clean(row.get("strongSell")) or 0
    except Exception:                                             # noqa: BLE001
        return {}

    total = strong_buy + buy + hold + sell + strong_sell
    if total <= 0:
        return {}
    return {
        "buy_pct": _round((strong_buy + buy) / total * 100.0, 1),
        "hold_pct": _round(hold / total * 100.0, 1),
        "sell_pct": _round((sell + strong_sell) / total * 100.0, 1),
    }


def _events_block(info, _atr_pct=None) -> dict:
    """Next earnings date from .info, if the feed carries one."""
    out = {"next_earnings": None, "days_to_earnings": None,
           "earnings_inside_horizon": None}

    stamp = (info.get("earningsTimestamp")
             or info.get("earningsTimestampStart")
             or info.get("mostRecentQuarter"))
    value = _clean(stamp)
    if value is None:
        return out

    try:
        when = datetime.fromtimestamp(value, tz=timezone.utc).astimezone(IST)
    except (OverflowError, OSError, ValueError):
        return out

    days = (when.date() - datetime.now(IST).date()).days
    if days < 0:                       # a past print tells us nothing forward
        return out

    out["next_earnings"] = when.strftime("%d %b %Y")
    out["days_to_earnings"] = days
    return out
# --------------------------------------------------------------------------
# public entry points used by app.py
# --------------------------------------------------------------------------

def scan_demo(shortlist_per_bucket: int, log=None):
    """Return (all_bundles, shortlisted_bundles) for demo mode."""
    say = log or (lambda _m: None)
    bundles = load_demo_bundles()
    say(f"loaded {len(bundles)} offline evidence bundles from demo_data/")

    shortlist = []
    for bucket in BUCKETS:
        in_bucket = [b for b in bundles if b.get("cap_segment") == bucket]
        ranked = sorted(
            in_bucket,
            key=lambda b: (b.get("price") or {}).get("day_change_pct") if
            (b.get("price") or {}).get("day_change_pct") is not None else -999,
            reverse=True,
        )
        shortlist.extend(ranked[:shortlist_per_bucket])

    # bundles that declare no bucket still deserve a shot
    shortlist.extend([b for b in bundles if b.get("cap_segment") not in BUCKETS])
    return bundles, shortlist


def scan_live(universe: dict, shortlist_per_bucket: int, log=None, progress=None):
    """Study every stock; return a separate shortlist for the narrative debate."""
    import intelligence
    return intelligence.analyze_universe(universe, shortlist_per_bucket, log=log,
                                         progress=progress, universe_metadata=LAST_UNIVERSE_METADATA)
