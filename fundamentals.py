"""
fundamentals.py — order book against quarterly sales.

The screen you want is book-to-bill: a company whose contracted, unexecuted
order book exceeds its latest quarterly revenue has visible work ahead. It is a
real metric, and the right one for order-driven businesses — capital goods,
defence, EPC, railways, infrastructure. It is meaningless for a bank or an FMCG
company, which have no order book at all.

WHERE THE TWO NUMBERS COME FROM
-------------------------------
Quarterly sales: fetched automatically. The price feed carries a full quarterly
income statement, so revenue is real, dated and verifiable.

Order book: **not fetchable anywhere**. It is not in the price feed, and it is
not a structured field on NSE or BSE — both of which return 403 to programmatic
requests in any case. Order book is disclosed in investor presentations and
earnings releases, as prose inside PDFs.

Rather than scrape and regex a PDF — where a misparse silently produces a wrong
financial number, which is worse than no number — order book values live in
`orderbook.json`, maintained by hand from company disclosures, with a source and
an as-of date on every entry. A company with no entry is reported as a data gap
and never guessed.

That is a real limitation and it is stated rather than papered over. The ratio
is only as fresh as the file.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
ORDERBOOK_FILE = os.path.join(HERE, "orderbook.json")

CRORE = 1e7                 # 1 crore = 10,000,000
STALE_DAYS = 120            # roughly one reporting quarter

# Sectors where an order book is a meaningful concept at all.
ORDER_DRIVEN_HINTS = (
    "industrial", "capital goods", "defence", "defense", "engineering",
    "construction", "infrastructure", "railway", "power", "electrical",
    "aerospace", "shipbuilding", "utilities",
)


def is_order_driven(sector, industry=None):
    text = f"{sector or ''} {industry or ''}".lower()
    return any(hint in text for hint in ORDER_DRIVEN_HINTS)


# ---------------------------------------------------------------------------
# the hand-maintained side
# ---------------------------------------------------------------------------

def load_orderbook(path=ORDERBOOK_FILE) -> dict:
    """
    {SYMBOL: {value_cr, as_of, source, note}} from orderbook.json.

    Entries with a null value are treated as absent — the file lists the
    companies worth tracking even before anyone has filled the numbers in.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}

    out = {}
    for symbol, entry in (raw.get("companies") or {}).items():
        if not isinstance(entry, dict):
            continue
        value = entry.get("order_book_cr")
        if value in (None, "", 0):
            continue
        try:
            out[symbol.upper()] = {
                "order_book_cr": float(value),
                "as_of": entry.get("as_of"),
                "source": entry.get("source"),
                "note": entry.get("note"),
            }
        except (TypeError, ValueError):
            continue
    return out


def entry_age_days(as_of):
    """How stale is this disclosure? None when the date is missing or unparsable."""
    if not as_of:
        return None
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%b %Y", "%Y-%m"):
        try:
            when = datetime.strptime(str(as_of), fmt).replace(tzinfo=IST)
            return (datetime.now(IST) - when).days
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# the fetched side
# ---------------------------------------------------------------------------

def quarterly_revenue(ticker, log=None) -> dict:
    """Latest quarterly revenue in crore, with the quarter it belongs to."""
    say = log or (lambda _m: None)
    out = {"revenue_cr": None, "quarter": None, "prior_revenue_cr": None,
           "yoy_growth_pct": None, "reason": None}

    try:
        import yfinance as yf
        statement = yf.Ticker(ticker).quarterly_income_stmt
    except Exception as exc:                                       # noqa: BLE001
        out["reason"] = f"income statement unavailable ({type(exc).__name__})"
        return out

    if statement is None or getattr(statement, "empty", True):
        out["reason"] = "feed returned no quarterly income statement"
        return out

    row = None
    for key in ("Total Revenue", "Operating Revenue"):
        if key in statement.index:
            row = statement.loc[key]
            break
    if row is None:
        out["reason"] = "no revenue line in the quarterly statement"
        return out

    values, quarters = [], []
    for column, value in row.items():
        if value == value and value is not None:
            values.append(float(value))
            try:
                quarters.append(column.date().isoformat())
            except AttributeError:
                quarters.append(str(column))

    if not values:
        out["reason"] = "revenue line present but empty"
        return out

    out["revenue_cr"] = round(values[0] / CRORE, 1)
    out["quarter"] = quarters[0]
    if len(values) > 1:
        out["prior_revenue_cr"] = round(values[1] / CRORE, 1)
    if len(values) >= 5 and values[4]:
        out["yoy_growth_pct"] = round((values[0] - values[4]) / values[4] * 100, 1)
    return out


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------

def assess(symbol, ticker, sector, industry=None, book=None, log=None) -> dict:
    """
    Book-to-sales for one company.

    `qualifies` is True only when there is a real order book value AND real
    revenue AND the ratio clears 1.0. Everything else reports why not.
    """
    say = log or (lambda _m: None)
    book = book if book is not None else load_orderbook().get(symbol.upper())

    result = {
        "symbol": symbol,
        "sector": sector,
        "order_driven": is_order_driven(sector, industry),
        "order_book_cr": None,
        "order_book_as_of": None,
        "order_book_source": None,
        "order_book_age_days": None,
        "stale": None,
        "quarterly_revenue_cr": None,
        "quarter": None,
        "yoy_growth_pct": None,
        "book_to_sales": None,
        "quarters_of_revenue": None,
        "qualifies": False,
        "reason": None,
    }

    revenue = quarterly_revenue(ticker, log=say)
    result["quarterly_revenue_cr"] = revenue["revenue_cr"]
    result["quarter"] = revenue["quarter"]
    result["yoy_growth_pct"] = revenue["yoy_growth_pct"]

    if not book:
        result["reason"] = (
            "no order book on file — add it to orderbook.json from the company's "
            "latest investor presentation" if result["order_driven"]
            else "not an order-driven business — book-to-sales does not apply")
        return result

    result["order_book_cr"] = book["order_book_cr"]
    result["order_book_as_of"] = book.get("as_of")
    result["order_book_source"] = book.get("source")
    age = entry_age_days(book.get("as_of"))
    result["order_book_age_days"] = age
    result["stale"] = bool(age is not None and age > STALE_DAYS)

    if not revenue["revenue_cr"]:
        result["reason"] = revenue.get("reason") or "quarterly revenue unavailable"
        return result

    ratio = book["order_book_cr"] / revenue["revenue_cr"]
    result["book_to_sales"] = round(ratio, 2)
    result["quarters_of_revenue"] = round(ratio, 1)

    if ratio <= 1.0:
        result["reason"] = (f"order book is {ratio:.2f}x the last quarter's sales — "
                            f"below the 1.0x screen")
        return result

    result["qualifies"] = True
    result["reason"] = (
        f"order book Rs {book['order_book_cr']:,.0f} Cr is {ratio:.2f}x the "
        f"Rs {revenue['revenue_cr']:,.0f} Cr booked in {revenue['quarter']} — "
        f"roughly {ratio:.1f} quarters of revenue already contracted"
        + (f" (disclosure is {age} days old)" if result["stale"] else ""))
    return result


def screen(universe, log=None, only_order_driven=True) -> dict:
    """
    Run the book-to-sales screen across the universe.

    Returns the qualifying list plus an honest account of coverage: how many
    companies could not be assessed, and why.
    """
    say = log or (lambda _m: None)
    book_file = load_orderbook()

    entries = []
    for bucket in ("large", "mid", "small"):
        for entry in (universe.get(bucket) or []):
            entries.append(dict(entry, bucket=bucket))

    if only_order_driven:
        candidates = [e for e in entries
                      if is_order_driven(e.get("sector")) or
                      e["ticker"].split(".")[0].upper() in book_file]
    else:
        candidates = entries

    say(f"book-to-sales screen: {len(candidates)} order-driven candidates, "
        f"{len(book_file)} with an order book on file")

    if not book_file:
        say("orderbook.json holds no values yet — fill it from company investor "
            "presentations and the screen will populate")

    rows, missing = [], []
    for entry in candidates:
        symbol = entry["ticker"].split(".")[0].upper()
        book = book_file.get(symbol)
        if not book:
            missing.append(symbol)
            continue
        rows.append(dict(assess(symbol, entry["ticker"], entry.get("sector"),
                                book=book, log=say),
                         name=entry.get("name"), bucket=entry.get("bucket")))

    qualifying = sorted([r for r in rows if r["qualifies"]],
                        key=lambda r: r["book_to_sales"], reverse=True)
    if qualifying:
        say("book-to-sales qualifiers: " + ", ".join(
            f"{r['symbol']} {r['book_to_sales']}x" for r in qualifying[:6]))

    return {
        "generated": datetime.now(IST).strftime("%d %b %Y, %H:%M IST"),
        "threshold": 1.0,
        "qualifying": qualifying,
        "assessed": rows,
        "candidates": len(candidates),
        "with_orderbook": len(rows),
        "missing_orderbook": missing,
        "coverage_pct": round(len(rows) / len(candidates) * 100, 1) if candidates else 0.0,
        "note": ("Order book values are maintained by hand in orderbook.json from "
                 "company disclosures — they are not machine-readable on NSE, BSE "
                 "or any price feed. Quarterly revenue is fetched live."),
    }
