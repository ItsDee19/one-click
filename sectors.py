"""
sectors.py — which parts of the market are actually working today.

Two independent readings, because either alone misleads:

  * index move — the NSE sector indices themselves, pulled in one batched
    request. This is the official measure of a sector's day.
  * breadth — how many names in *our* universe advanced, and by how much.
    An index can be dragged up by one heavyweight while most of its
    constituents fall; breadth catches that and the index never will.

A sector rated "leading" has both: the index up, and most of its stocks
participating. That distinction is the whole point of computing both.

This deliberately does not scrape NSE or BSE. Both return 403 to
programmatic requests and would need browser-session emulation to defeat,
which is fragile and against their terms. The index values here come through
the same feed as every other price in the project, and breadth is computed
from data already downloaded for the screen — so it costs one extra request
for the entire heatmap.
"""

from __future__ import annotations

import statistics

# NSE sector indices that resolve reliably through the price feed.
SECTOR_INDICES = {
    "^CNXIT": "IT",
    "^NSEBANK": "Bank",
    "^CNXAUTO": "Auto",
    "^CNXPHARMA": "Pharma",
    "^CNXFMCG": "FMCG",
    "^CNXMETAL": "Metal",
    "^CNXENERGY": "Energy",
    "^CNXREALTY": "Realty",
    "^CNXPSUBANK": "PSU Bank",
    "^CNXINFRA": "Infrastructure",
}

BROAD_INDICES = {"^NSEI": "NIFTY 50", "^CRSLDX": "NIFTY 500"}

LEADING = "leading"
LAGGING = "lagging"
MIXED = "mixed"
NEUTRAL = "neutral"


def fetch_indices(log=None) -> dict:
    """Day change and 20-session return for every sector index."""
    say = log or (lambda _m: None)
    try:
        import yfinance as yf
    except ImportError:
        return {}

    tickers = list(SECTOR_INDICES) + list(BROAD_INDICES)
    say(f"pulling {len(tickers)} sector and broad indices")
    try:
        frame = yf.download(" ".join(tickers), period="1mo", interval="1d",
                            group_by="ticker", auto_adjust=False, actions=False,
                            progress=False, threads=True)
    except Exception as exc:                                       # noqa: BLE001
        say(f"sector indices unavailable ({type(exc).__name__})")
        return {}

    def block(ticker, closes):
        label = SECTOR_INDICES.get(ticker) or BROAD_INDICES.get(ticker)
        day = ((closes[-1] - closes[-2]) / closes[-2] * 100.0) if closes[-2] else None
        month = ((closes[-1] - closes[0]) / closes[0] * 100.0) if closes[0] else None
        return label, {
            "index": ticker,
            "last": round(closes[-1], 2),
            "day_change_pct": round(day, 2) if day is not None else None,
            "month_return_pct": round(month, 2) if month is not None else None,
            "broad": ticker in BROAD_INDICES,
        }

    out = {}
    missing = []
    for ticker in tickers:
        try:
            closes = [c for c in frame[ticker]["Close"].tolist() if c == c]
        except Exception:                                          # noqa: BLE001
            closes = []
        if len(closes) >= 2:
            label, data = block(ticker, closes)
            out[label] = data
        else:
            missing.append(ticker)

    # The batched index download drops symbols intermittently — a partial
    # result would silently blank half the heatmap, so anything missing is
    # retried on its own before we accept it as unavailable.
    for ticker in missing:
        try:
            bars = yf.Ticker(ticker).history(period="1mo", interval="1d")
            closes = [c for c in bars["Close"].tolist() if c == c]
        except Exception:                                          # noqa: BLE001
            closes = []
        if len(closes) >= 2:
            label, data = block(ticker, closes)
            out[label] = data

    still_missing = [SECTOR_INDICES.get(t) or BROAD_INDICES.get(t)
                     for t in tickers if (SECTOR_INDICES.get(t) or BROAD_INDICES.get(t)) not in out]
    if still_missing:
        say(f"index data unavailable for {', '.join(still_missing)} — "
            f"those rows fall back to breadth alone")
    return out


def breadth_from_quotes(quotes, universe_meta) -> dict:
    """
    Advancing/declining counts per sector, from the universe already fetched.

    `quotes` is the {bucket: [rows]} structure fetch_quotes returns.
    """
    by_sector = {}
    for rows in (quotes or {}).values():
        for row in rows:
            change = row.get("day_change_pct")
            if change is None:
                continue
            sector = row.get("sector") or "Unclassified"
            slot = by_sector.setdefault(
                sector, {"advancing": 0, "declining": 0, "moves": [], "names": []})
            if change > 0:
                slot["advancing"] += 1
            elif change < 0:
                slot["declining"] += 1
            slot["moves"].append(change)
            slot["names"].append((row["ticker"].split(".")[0], change))

    out = {}
    for sector, slot in by_sector.items():
        total = slot["advancing"] + slot["declining"]
        if not slot["moves"]:
            continue
        leaders = sorted(slot["names"], key=lambda p: p[1], reverse=True)
        out[sector] = {
            "count": len(slot["moves"]),
            "advancing": slot["advancing"],
            "declining": slot["declining"],
            "advance_pct": round(slot["advancing"] / total * 100, 1) if total else None,
            "median_move_pct": round(statistics.median(slot["moves"]), 2),
            "best": {"symbol": leaders[0][0], "change": round(leaders[0][1], 2)},
            "worst": {"symbol": leaders[-1][0], "change": round(leaders[-1][1], 2)},
        }
    return out


def _classify(index_move, advance_pct):
    """
    Leading needs the index up AND the constituents participating.

    One heavyweight can carry an index while most of the sector falls. Calling
    that "leading" is how a heatmap lies, so both have to agree.
    """
    if index_move is None and advance_pct is None:
        return NEUTRAL
    if index_move is None:
        return LEADING if advance_pct >= 60 else (LAGGING if advance_pct <= 40 else NEUTRAL)
    if advance_pct is None:
        return LEADING if index_move > 0.3 else (LAGGING if index_move < -0.3 else NEUTRAL)

    strong = index_move > 0.3 and advance_pct >= 60
    weak = index_move < -0.3 and advance_pct <= 40
    if strong:
        return LEADING
    if weak:
        return LAGGING
    if (index_move > 0.3) != (advance_pct >= 60) and abs(index_move) > 0.3:
        return MIXED       # index and breadth disagree — worth knowing
    return NEUTRAL


def heatmap(quotes, log=None) -> dict:
    """
    The full picture: indices, breadth, and where the two disagree.

    Sector names in universe.json come from the exchange feed ("Technology",
    "Financial Services") while the indices use market shorthand ("IT",
    "Bank"), so the two are matched by keyword rather than exact string.
    """
    say = log or (lambda _m: None)
    indices = fetch_indices(log=say)
    breadth = breadth_from_quotes(quotes, None)

    # feed sector name -> index label
    alias = {
        "technology": "IT", "financial services": "Bank", "consumer cyclical": "Auto",
        "healthcare": "Pharma", "consumer defensive": "FMCG", "basic materials": "Metal",
        "energy": "Energy", "real estate": "Realty", "industrials": "Infrastructure",
        "utilities": "Energy", "communication services": "IT",
    }

    rows = []
    for sector, stats in sorted(breadth.items(),
                                key=lambda kv: kv[1]["median_move_pct"], reverse=True):
        label = alias.get(sector.lower())
        index = indices.get(label) if label else None
        index_move = index["day_change_pct"] if index else None
        rows.append({
            "sector": sector,
            "index": label,
            "index_change_pct": index_move,
            "index_month_pct": index["month_return_pct"] if index else None,
            "stocks": stats["count"],
            "advancing": stats["advancing"],
            "declining": stats["declining"],
            "advance_pct": stats["advance_pct"],
            "median_move_pct": stats["median_move_pct"],
            "best": stats["best"],
            "worst": stats["worst"],
            "state": _classify(index_move, stats["advance_pct"]),
        })

    leaders = [r["sector"] for r in rows if r["state"] == LEADING]
    laggards = [r["sector"] for r in rows if r["state"] == LAGGING]
    disagree = [r["sector"] for r in rows if r["state"] == MIXED]

    if leaders:
        say(f"sector heatmap: leading — {', '.join(leaders[:4])}"
            + (f" · lagging — {', '.join(laggards[:3])}" if laggards else ""))
    if disagree:
        say(f"sector heatmap: index and breadth disagree on {', '.join(disagree[:3])} "
            f"— the move is not broad-based")

    return {
        "rows": rows,
        "broad": {label: data for label, data in indices.items() if data.get("broad")},
        "leaders": leaders,
        "laggards": laggards,
        "disagreement": disagree,
        "source": "NSE sector indices via the price feed + breadth computed "
                  "across the universe",
    }


def sector_state(heat, sector):
    """The state for one sector, for the evidence bundle."""
    for row in (heat or {}).get("rows", []):
        if row["sector"] == sector:
            return {"state": row["state"], "index": row["index"],
                    "index_change_pct": row["index_change_pct"],
                    "advance_pct": row["advance_pct"],
                    "median_move_pct": row["median_move_pct"]}
    return None
