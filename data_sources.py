"""
data_sources.py — evidence bundles for the agent panel.

Two modes, one output shape:

  demo : read pre-built bundles from demo_data/*.json (fully offline)
  live : pull NSE data through yfinance for the tickers in universe.json

Everything downstream (scoring.py, llm.py, app.py) only ever sees the
normalised bundle produced here, so the engines cannot tell the two apart.

Rules that matter:
  * a value that could not be computed is None *and* named in data_gaps
  * we never guess, interpolate or carry forward a missing number
  * this feed carries no raw fundamental ratios (P/E, ROE, margins). The only
    "fundamental" view available is the sell-side analyst block, and the
    bundle says so explicitly in `notes`.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

import market

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.path.join(HERE, "demo_data")
UNIVERSE_FILE = os.path.join(HERE, "universe.json")

SMA_PERIOD = 20            # N-day simple moving average used for price_vs_sma_pct
HISTORY_PERIOD = "1mo"     # ~1 month of daily OHLC
BENCHMARK = "^NSEI"        # NIFTY 50 — the yardstick for relative strength
BENCHMARK_NAME = "NIFTY 50"
INTRADAY_INTERVAL = "5m"   # granularity for VWAP / opening range
OPENING_RANGE_BARS = 3     # first 3 x 5m bars = the 09:15-09:30 opening range
ATR_PERIOD = 14
IST = timezone(timedelta(hours=5, minutes=30))

BUCKETS = ("large", "mid", "small")

# The most recent screen's quotes, so the sector heatmap can compute breadth
# without paying for a second download of the same bars.
LAST_QUOTES = {}

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
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _round(value, digits=2):
    value = _clean(value)
    return None if value is None else round(value, digits)


def now_ist_str() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %H:%M:%S IST")


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
        "regime": market.regime(),
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

    for section in ("price", "range_52w", "technicals", "analyst", "news"):
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
        bundle.setdefault("regime", market.regime())
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
    if single:
        return downloaded
    try:
        frame = downloaded[ticker]
    except (KeyError, TypeError):
        return None
    return None if getattr(frame, "empty", True) else frame


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
    yf = _import_yf()
    say = log or (lambda _m: None)

    tickers = [e["ticker"] for bucket in BUCKETS for e in universe.get(bucket, [])]
    if not tickers:
        return {b: [] for b in BUCKETS}, {}

    # The NIFTY rides along in the same request. Without it there is no way to
    # tell a stock that is genuinely strong from one drifting up with the index.
    say(f"downloading {HISTORY_PERIOD} daily OHLC for {len(tickers)} tickers + {BENCHMARK_NAME}")
    downloaded = yf.download(
        tickers=" ".join(tickers + [BENCHMARK]),
        period=HISTORY_PERIOD,
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=True,
    )
    single = False          # always at least the benchmark alongside a ticker

    benchmark = _benchmark_block(_frame_for(downloaded, BENCHMARK, single))
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
            frame = _frame_for(downloaded, entry["ticker"], single)
            closes = _series_values(frame, "Close")
            if len(closes) >= 2 and closes[-2] and entry.get("sector"):
                sector_moves.setdefault(entry["sector"], []).append(
                    (closes[-1] - closes[-2]) / closes[-2] * 100.0)
    sector_median = {
        sector: sorted(values)[len(values) // 2]
        for sector, values in sector_moves.items() if len(values) >= 2
    }

    quotes = {}
    for bucket in BUCKETS:
        rows = []
        for entry in universe.get(bucket, []):
            frame = _frame_for(downloaded, entry["ticker"], single)
            closes = _series_values(frame, "Close")
            volumes = _series_values(frame, "Volume")

            change = None
            if len(closes) >= 2 and closes[-2]:
                change = (closes[-1] - closes[-2]) / closes[-2] * 100.0

            # RVOL at screen time, so the shortlist can weigh participation
            rvol = None
            if len(volumes) >= 3:
                prior = [v for v in volumes[:-1] if v > 0]
                if prior and volumes[-1] > 0:
                    rvol = (volumes[-1] / (sum(prior) / len(prior))
                            / market.volume_divisor(session["session_fraction"]))

            relative = None
            if change is not None and benchmark.get("day_change_pct") is not None:
                relative = change - benchmark["day_change_pct"]

            peer_median = sector_median.get(entry.get("sector"))
            sector_rel = (change - peer_median
                          if change is not None and peer_median is not None else None)

            rows.append({
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
    closes = _series_values(frame, "Close")
    out = {
        "name": BENCHMARK_NAME, "ticker": BENCHMARK,
        "last": None, "day_change_pct": None, "window_return_pct": None,
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
    yf = _import_yf()
    say = log or (lambda _m: None)

    ticker = quote["ticker"]
    symbol = ticker.split(".")[0]
    ev = _empty_evidence(symbol, quote.get("name") or symbol, ticker,
                         quote.get("bucket"), quote.get("sector"))

    frame = quote.get("frame")
    closes = _series_values(frame, "Close")
    highs = _series_values(frame, "High")
    lows = _series_values(frame, "Low")
    opens = _series_values(frame, "Open")
    volumes = _series_values(frame, "Volume")

    info = {}
    news_items = []
    try:
        handle = yf.Ticker(ticker)
        try:
            info = handle.info or {}
        except Exception as exc:                                  # noqa: BLE001
            say(f"{symbol}: .info unavailable ({type(exc).__name__})")
        try:
            news_items = handle.news or []
        except Exception as exc:                                  # noqa: BLE001
            say(f"{symbol}: .news unavailable ({type(exc).__name__})")
        info_reco = _recommendation_split(handle, symbol, say)
    except Exception as exc:                                      # noqa: BLE001
        say(f"{symbol}: yfinance handle failed ({type(exc).__name__})")
        info_reco = {}

    if info.get("longName") or info.get("shortName"):
        ev["name"] = info.get("longName") or info.get("shortName")
    if info.get("sector"):
        ev["sector"] = info["sector"]

    # ---- price -----------------------------------------------------------
    live = _clean(info.get("currentPrice")) or _clean(info.get("regularMarketPrice"))
    if live is None and closes:
        live = closes[-1]
    prev_close = _clean(info.get("regularMarketPreviousClose")) or _clean(info.get("previousClose"))
    if prev_close is None and len(closes) >= 2:
        prev_close = closes[-2]

    ev["price"] = {
        "live": _round(live),
        "day_open": _round(_clean(info.get("regularMarketOpen")) or _clean(info.get("open"))
                           or (opens[-1] if opens else None)),
        "day_high": _round(_clean(info.get("dayHigh")) or (highs[-1] if highs else None)),
        "day_low": _round(_clean(info.get("dayLow")) or (lows[-1] if lows else None)),
        "prev_close": _round(prev_close),
        "day_change_pct": _round(((live - prev_close) / prev_close * 100.0)
                                 if (live is not None and prev_close) else None),
        "volume": int(_clean(info.get("volume")) or _clean(info.get("regularMarketVolume"))
                      or (volumes[-1] if volumes else 0)) or None,
    }

    # ---- 52-week range ---------------------------------------------------
    hi52 = _clean(info.get("fiftyTwoWeekHigh"))
    lo52 = _clean(info.get("fiftyTwoWeekLow"))
    position = pct_from_high = None
    if live is not None and hi52 and lo52 is not None and hi52 > lo52:
        position = (live - lo52) / (hi52 - lo52) * 100.0
        pct_from_high = (live - hi52) / hi52 * 100.0
    ev["range_52w"] = {
        "high": _round(hi52), "low": _round(lo52),
        "pct_from_high": _round(pct_from_high), "position_pct": _round(position),
    }

    # ---- market phase, technicals, intraday -------------------------------
    phase_info = market.describe(market_state=info.get("marketState"))
    ev["market"] = phase_info
    ev["regime"] = market.regime(log=say)
    ev["technicals"].update(_technicals(closes, highs, lows, volumes, live,
                                        ev["price"], phase_info["session_fraction"]))
    ev["intraday"] = build_intraday(quote.get("intraday_frame"),
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
    rel_day = (day_change - benchmark["day_change_pct"]
               if day_change is not None and benchmark.get("day_change_pct") is not None
               else None)
    rel_window = (window - benchmark["window_return_pct"]
                  if window is not None and benchmark.get("window_return_pct") is not None
                  else None)
    ev["relative"] = {
        "benchmark": benchmark.get("name") or BENCHMARK_NAME,
        "benchmark_day_change_pct": benchmark.get("day_change_pct"),
        "benchmark_window_return_pct": benchmark.get("window_return_pct"),
        "rel_day_change_pct": _round(rel_day),
        "rel_window_return_pct": _round(rel_window),
        "outperforming": None if rel_day is None else bool(rel_day > 0),
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
        prior = [v for v in volumes[:-1] if v > 0]
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
        if len(window) >= 5:
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

    if frame is None or getattr(frame, "empty", True):
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
    take = min(OPENING_RANGE_BARS, len(highs), len(lows))
    if take:
        or_high = max(highs[:take])
        or_low = min(lows[:take])
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

    out["session_volume"] = int(sum(volumes)) or None
    out["session_high"] = _round(max(highs)) if highs else None
    out["session_low"] = _round(min(lows)) if lows else None

    try:
        out["last_bar"] = str(frame.index[-1])
    except (AttributeError, IndexError):
        out["last_bar"] = None

    return out


def fetch_intraday(tickers, log=None):
    """One batched 5-minute download for the shortlist. {ticker: frame}."""
    yf = _import_yf()
    say = log or (lambda _m: None)
    if not tickers:
        return {}

    say(f"downloading {INTRADAY_INTERVAL} session bars for {len(tickers)} shortlisted")
    try:
        downloaded = yf.download(
            tickers=" ".join(tickers),
            period="1d",
            interval=INTRADAY_INTERVAL,
            group_by="ticker",
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=True,
        )
    except Exception as exc:                                      # noqa: BLE001
        say(f"intraday download failed ({type(exc).__name__}) — "
            f"intraday track will report unavailable")
        return {}

    single = len(tickers) == 1
    return {t: _frame_for(downloaded, t, single) for t in tickers}


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


def _news_block(news_items) -> dict:
    """Normalise yfinance news (old flat shape and new {'content': ...} shape)."""
    recent, pos, neg, neu = [], 0, 0, 0

    for item in (news_items or [])[:12]:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        title = content.get("title") or item.get("title")
        if not title:
            continue
        publisher = (
            (content.get("provider") or {}).get("displayName")
            if isinstance(content.get("provider"), dict) else None
        ) or item.get("publisher") or None
        published = content.get("pubDate") or item.get("providerPublishTime") or None

        tone = score_headline(title)
        pos += tone == "positive"
        neg += tone == "negative"
        neu += tone == "neutral"
        recent.append({
            "title": title[:180],
            "publisher": publisher,
            "published": str(published) if published is not None else None,
            "sentiment": tone,
        })

    if not recent:
        return {"total": 0, "positive": 0, "negative": 0, "neutral": 0,
                "net_tone": 0, "recent": []}
    return {"total": len(recent), "positive": pos, "negative": neg,
            "neutral": neu, "net_tone": pos - neg, "recent": recent[:6]}


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


def scan_live(universe: dict, shortlist_per_bucket: int, log=None):
    """Return (universe_count, shortlisted_evidence_bundles) for live mode."""
    say = log or (lambda _m: None)
    quotes, benchmark = fetch_quotes(universe, log=say)
    globals()["LAST_QUOTES"] = quotes

    screened = []
    for bucket in BUCKETS:
        picked = screen_bucket(quotes.get(bucket, []), shortlist_per_bucket)
        if picked:
            say(f"{bucket}-cap shortlist: " + ", ".join(
                f"{q['ticker'].split('.')[0]} "
                f"{q['day_change_pct']:+.2f}%"
                + (f" (rel {q['rel_day_change_pct']:+.2f}%)"
                   if q.get("rel_day_change_pct") is not None else "")
                + (f" rvol {q['rvol']:.1f}x" if q.get("rvol") is not None else "")
                if q["day_change_pct"] is not None else q["ticker"].split(".")[0]
                for q in picked))
        screened.extend(picked)

    for quote in screened:
        quote["benchmark"] = benchmark

    # Intraday bars cost one more request, so we only pay for it on survivors.
    frames = fetch_intraday([q["ticker"] for q in screened], log=say)
    for quote in screened:
        quote["intraday_frame"] = frames.get(quote["ticker"])

    bundles = []
    for quote in screened:
        try:
            bundle = build_evidence_live(quote, log=say)
        except Exception as exc:                                  # noqa: BLE001
            say(f"{quote['ticker']}: evidence build failed ({type(exc).__name__}) — skipped")
            continue

        # A ticker Yahoo no longer resolves (renamed, delisted, demerged) comes
        # back fully null. There is nothing to debate, so drop it rather than
        # spend an LLM call arguing about a stock with no price.
        if (bundle.get("price") or {}).get("live") is None:
            say(f"{quote['ticker']}: no price data returned — delisted or renamed? skipped")
            continue
        bundles.append(bundle)

    return universe_size(universe), bundles
