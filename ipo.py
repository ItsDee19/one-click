"""
ipo.py — the IPO desk: what is open, how it is being bid, and whether to apply.

WHERE EACH NUMBER COMES FROM
----------------------------
This matters more here than anywhere else in the project, because IPO
commentary is full of numbers nobody can source.

  calendar + price band + dates   NSE, official, fetched live
  subscription by category        NSE, official, fetched live
  news                            public RSS, sanitised (see research.py)
  financials from the DRHP        NOT machine-readable — hand-entered
  GMP (grey market premium)       NOT official — hand-entered, see below

The NSE IPO endpoints are open, unlike its quote API which returns 403. So the
calendar and the live bid data are real, dated and verifiable.

ON GMP
------
Grey market premium is quoted by unofficial dealers in an unregulated market.
No exchange publishes it, SEBI has repeatedly cautioned against relying on it,
and the quotes circulating on aggregator sites are trivially moved by the
operators who benefit from them. Scraping a number like that and printing it
next to official exchange data would give it a credibility it has not earned.

So GMP is supported as an optional hand-entered field, always labelled
unofficial, and it can never on its own carry a verdict — the deterministic
judge caps its contribution and requires official demand or financials to
agree before APPLY is possible.

Subscription is the better signal anyway, and it is free: it is the same
demand, measured by the exchange instead of by a rumour.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
NOTES_FILE = os.path.join(HERE, "ipo_notes.json")

NSE_BASE = "https://www.nseindia.com"
UPCOMING_URL = NSE_BASE + "/api/all-upcoming-issues?category=ipo"
CURRENT_URL = NSE_BASE + "/api/ipo-current-issue"
TIMEOUT = 15

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_BASE + "/market-data/all-upcoming-issues-ipo",
}

# --- verdicts ---------------------------------------------------------------
APPLY = "APPLY"
NEUTRAL = "NEUTRAL"
AVOID = "AVOID"
UNKNOWN = "UNKNOWN"

# --- deterministic thresholds ----------------------------------------------
STRONG_SUBSCRIPTION = 3.0      # comfortably spoken for
WEAK_SUBSCRIPTION = 1.0        # below this the book is not even full
APPLY_NET = 25
AVOID_NET = -15
# GMP is unofficial, so it is capped hard and can never decide a verdict alone.
MAX_GMP_POINTS = 8


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def _session():
    """
    NSE hands out cookies on the HTML pages and checks them on the JSON ones.

    The IPO endpoints often answer without priming, but not always — a cold
    call can return an empty body. Fetching the landing page first costs one
    request and makes the whole thing reliable.
    """
    import requests
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(NSE_BASE + "/market-data/all-upcoming-issues-ipo", timeout=TIMEOUT)
    except Exception:                                              # noqa: BLE001
        pass
    return s


def _get_json(session, url, log=None):
    say = log or (lambda _m: None)
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code >= 400:
            say(f"ipo feed {url.rsplit('/', 1)[-1]} returned {r.status_code}")
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as exc:                                       # noqa: BLE001
        say(f"ipo feed unavailable ({type(exc).__name__})")
        return []


def _num(value):
    """NSE sends floats as strings in scientific notation ('6.32E7')."""
    if value in (None, "", "-"):
        return None
    try:
        out = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _parse_band(text):
    """'Rs.408 to Rs.429' -> (408.0, 429.0). A fixed price gives (x, x)."""
    if not text:
        return (None, None)
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(text))]
    if not nums:
        return (None, None)
    return (min(nums), max(nums)) if len(nums) > 1 else (nums[0], nums[0])


def _parse_date(text):
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(text).strip(), fmt).replace(tzinfo=IST)
        except (TypeError, ValueError):
            continue
    return None


def fetch_calendar(log=None) -> list:
    """
    Every IPO the exchange currently lists, with live subscription where open.

    The two endpoints overlap: `upcoming` carries the calendar, `current`
    carries the bid book for issues that are open. They are merged on symbol.
    """
    say = log or (lambda _m: None)
    session = _session()

    upcoming = _get_json(session, UPCOMING_URL, log=say)
    current = _get_json(session, CURRENT_URL, log=say)
    say(f"NSE IPO feed: {len(upcoming)} listed, {len(current)} currently open")

    # subscription rows are per category; keep them all, plus the Total
    subs = {}
    for row in current:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        slot = subs.setdefault(symbol, {"categories": {}, "total": None})
        category = (row.get("category") or "Total").strip()
        times = _num(row.get("noOfTime"))
        entry = {
            "times_subscribed": round(times, 2) if times is not None else None,
            "shares_offered": _num(row.get("noOfSharesOffered")),
            "shares_bid": _num(row.get("noOfsharesBid")),
        }
        slot["categories"][category] = entry
        if category.lower() == "total":
            slot["total"] = entry["times_subscribed"]

    merged, seen = [], set()
    for row in upcoming + current:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        merged.append(_normalise(row, subs.get(symbol)))
    return merged


def _normalise(row, subscription):
    low, high = _parse_band(row.get("issuePrice"))
    opens = _parse_date(row.get("issueStartDate"))
    closes = _parse_date(row.get("issueEndDate"))
    now = datetime.now(IST)

    days_to_close = (closes.date() - now.date()).days if closes else None
    if opens and now.date() < opens.date():
        window = "upcoming"
    elif closes and now.date() > closes.date():
        window = "closed"
    else:
        window = "open"

    shares = _num(row.get("issueSize"))
    issue_value_cr = None
    if shares and high:
        issue_value_cr = round(shares * high / 1e7, 1)      # 1 crore = 1e7

    return {
        "symbol": (row.get("symbol") or "").strip().upper(),
        "name": (row.get("companyName") or "").strip(),
        "series": row.get("series"),
        "status": row.get("status"),
        "window": window,
        "opens": opens.strftime("%d %b %Y") if opens else None,
        "closes": closes.strftime("%d %b %Y") if closes else None,
        "days_to_close": days_to_close,
        "price_band": {"low": low, "high": high,
                       "spread_pct": (round((high - low) / low * 100, 1)
                                      if low and high and high > low else 0.0),
                       "raw": row.get("issuePrice")},
        "issue": {"shares_offered": shares, "value_cr": issue_value_cr},
        "subscription": subscription or {"categories": {}, "total": None},
        "source": "NSE (official)",
        "fetched_at": now.strftime("%d %b %Y, %H:%M IST"),
    }


# ---------------------------------------------------------------------------
# the hand-maintained supplement
# ---------------------------------------------------------------------------

def load_notes(path=NOTES_FILE) -> dict:
    """
    DRHP financials and, optionally, a GMP quote — both entered by hand.

    Neither is machine-readable: financials live in a DRHP PDF, and GMP has no
    official publisher at all. A symbol with no entry is a declared gap, never
    a guess.
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
        if isinstance(entry, dict):
            out[symbol.strip().upper()] = entry
    return out


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------

def build_evidence(item, notes=None, news=None) -> dict:
    """One IPO as an evidence bundle, with every missing field named."""
    notes = notes or {}
    fin = notes.get("financials") or {}
    gmp = notes.get("gmp") or {}

    band_high = item["price_band"]["high"]
    gmp_value = _num(gmp.get("premium_rs"))
    gmp_pct = (round(gmp_value / band_high * 100, 1)
               if gmp_value is not None and band_high else None)

    evidence = dict(item)
    evidence["financials"] = {
        "revenue_cr": _num(fin.get("revenue_cr")),
        "revenue_growth_pct": _num(fin.get("revenue_growth_pct")),
        "profit_cr": _num(fin.get("profit_cr")),
        "profit_growth_pct": _num(fin.get("profit_growth_pct")),
        "pe_post_issue": _num(fin.get("pe_post_issue")),
        "peer_pe": _num(fin.get("peer_pe")),
        "roe_pct": _num(fin.get("roe_pct")),
        "debt_to_equity": _num(fin.get("debt_to_equity")),
        "source": fin.get("source"),
        "as_of": fin.get("as_of"),
    }
    evidence["gmp"] = {
        "premium_rs": gmp_value,
        "premium_pct": gmp_pct,
        "as_of": gmp.get("as_of"),
        "source": gmp.get("source"),
        "trust": ("UNOFFICIAL — grey market quote, no exchange publishes this, "
                  "easily moved by operators; treated as weak corroboration only"),
    }
    evidence["news"] = news or {"total": 0, "positive": 0, "negative": 0,
                                "neutral": 0, "net_tone": 0, "recent": []}

    gaps = []
    if evidence["subscription"].get("total") is None:
        gaps.append("subscription.total (issue not open yet)")
    for key in ("revenue_cr", "profit_cr", "pe_post_issue"):
        if evidence["financials"][key] is None:
            gaps.append(f"financials.{key}")
    if gmp_value is None:
        gaps.append("gmp.premium_rs (unofficial, optional)")
    if not evidence["news"]["total"]:
        gaps.append("news (no headlines found)")
    evidence["data_gaps"] = gaps
    evidence["notes"] = [
        "Calendar, price band and subscription are official NSE data.",
        "Financials come from the DRHP and are entered by hand in ipo_notes.json; "
        "they are not machine-readable.",
        "GMP is unofficial and is never sufficient on its own to justify APPLY.",
    ]
    return evidence


# ---------------------------------------------------------------------------
# the deterministic judge
# ---------------------------------------------------------------------------

class _Tally:
    def __init__(self):
        self.points = 0.0
        self.reasons = []

    def add(self, points, reason):
        if points <= 0:
            return
        self.points += points
        self.reasons.append(reason)

    def note(self, reason):
        self.reasons.append(reason)

    def score(self):
        return int(round(max(0.0, min(100.0, self.points))))


def _for_case(ev) -> _Tally:
    t = _Tally()
    sub = ev["subscription"].get("total")
    cats = ev["subscription"].get("categories") or {}
    fin = ev["financials"]

    if sub is not None:
        if sub >= STRONG_SUBSCRIPTION:
            t.add(min(28.0, 8.0 + sub * 3.0),
                  f"book subscribed {sub:.2f}x overall — demand is well ahead of supply")
        elif sub >= 1.5:
            t.add(12.0, f"book subscribed {sub:.2f}x overall")
        elif sub >= WEAK_SUBSCRIPTION:
            t.add(5.0, f"book just covered at {sub:.2f}x")

    qib = (cats.get("Qualified Institutional Buyers(QIBs)")
           or cats.get("QIB") or {}).get("times_subscribed")
    if qib is not None and qib >= 2:
        t.add(min(15.0, qib * 2.0),
              f"institutions have taken {qib:.2f}x their reserved portion")

    growth = fin.get("revenue_growth_pct")
    if growth is not None and growth >= 15:
        t.add(min(15.0, growth / 3.0), f"revenue growing {growth:g}%")

    profit = fin.get("profit_cr")
    if profit is not None and profit > 0:
        t.add(10.0, f"profitable at the last reported period ({profit:g} Cr)")

    pe, peer = fin.get("pe_post_issue"), fin.get("peer_pe")
    if pe is not None and peer is not None and pe < peer:
        t.add(min(15.0, (peer - pe) / peer * 40.0),
              f"asking {pe:g}x earnings against a peer set at {peer:g}x — priced below the group")

    roe = fin.get("roe_pct")
    if roe is not None and roe >= 15:
        t.add(8.0, f"return on equity {roe:g}%")

    tone = ev["news"].get("net_tone")
    if tone is not None and tone > 0:
        t.add(min(8.0, tone * 3.0),
              f"news tone net +{int(tone)} across {ev['news'].get('total', 0)} headlines")

    # capped deliberately: an unofficial number must never carry the verdict
    gmp_pct = ev["gmp"].get("premium_pct")
    if gmp_pct is not None and gmp_pct > 0:
        t.add(min(MAX_GMP_POINTS, gmp_pct / 4.0),
              f"grey market quoted {gmp_pct:g}% above the band — unofficial, "
              f"corroboration only")

    if not t.reasons:
        t.note("nothing in the evidence argues for applying")
    return t


def _against_case(ev) -> _Tally:
    t = _Tally()
    sub = ev["subscription"].get("total")
    fin = ev["financials"]
    days = ev.get("days_to_close")

    if sub is not None and sub < WEAK_SUBSCRIPTION:
        # undersubscription matters far more on the last day than the first
        late = days is not None and days <= 1
        t.add(28.0 if late else 14.0,
              f"book only {sub:.2f}x covered"
              + (" on the final day — the issue is struggling" if late
                 else " so far"))

    profit = fin.get("profit_cr")
    if profit is not None and profit <= 0:
        t.add(22.0, f"loss-making at the last reported period ({profit:g} Cr)")

    growth = fin.get("revenue_growth_pct")
    if growth is not None and growth < 0:
        t.add(15.0, f"revenue shrinking {growth:g}%")

    pe, peer = fin.get("pe_post_issue"), fin.get("peer_pe")
    if pe is not None and peer is not None and pe > peer * 1.2:
        t.add(min(20.0, (pe - peer) / peer * 30.0),
              f"asking {pe:g}x earnings against a peer set at {peer:g}x — a premium to the group")

    dte = fin.get("debt_to_equity")
    if dte is not None and dte > 1.5:
        t.add(10.0, f"debt-to-equity {dte:g}")

    tone = ev["news"].get("net_tone")
    if tone is not None and tone < 0:
        t.add(min(10.0, -tone * 3.0), f"news tone net {int(tone)}")

    gmp_pct = ev["gmp"].get("premium_pct")
    if gmp_pct is not None and gmp_pct < 0:
        t.add(min(MAX_GMP_POINTS, -gmp_pct / 4.0),
              f"grey market quoted {gmp_pct:g}% below the band — unofficial")

    # An IPO with no financials on file cannot be assessed as a business.
    missing_fin = sum(1 for k in ("revenue_cr", "profit_cr", "pe_post_issue")
                      if fin.get(k) is None)
    if missing_fin == 3:
        t.add(12.0, "no financials on file — the business cannot be assessed, "
                    "only the demand for its paper")

    if not t.reasons:
        t.note("nothing in the evidence argues against applying")
    return t


def judge(ev, for_score, against_score, for_reasons, against_reasons) -> dict:
    """
    APPLY needs real evidence, not just enthusiasm.

    Two hard requirements beyond the score: the exchange must actually be
    showing demand, and something other than GMP must support the case. An
    unofficial grey-market quote cannot be the reason to put money in.
    """
    net = for_score - against_score
    sub = ev["subscription"].get("total")
    fin = ev["financials"]
    has_financials = any(fin.get(k) is not None
                         for k in ("revenue_cr", "profit_cr", "pe_post_issue"))
    demand_confirmed = sub is not None and sub >= 1.5

    blockers = []
    if not demand_confirmed:
        blockers.append("the book is not yet showing clear demand"
                        if sub is not None else
                        "the issue has not opened, so there is no demand data")
    if not has_financials:
        blockers.append("no DRHP financials on file")

    if net >= APPLY_NET and demand_confirmed and has_financials:
        verdict = APPLY
    elif net <= AVOID_NET:
        verdict = AVOID
    elif ev["window"] == "closed":
        verdict = UNKNOWN
    else:
        verdict = NEUTRAL

    confidence = int(max(1, min(10, round(4 + net / 15.0))))
    confidence = max(7, confidence) if verdict == APPLY else min(6, confidence)

    winner = "For" if for_score >= against_score else "Against"
    lead = for_reasons if winner == "For" else against_reasons
    headline = lead[0] if lead else None

    if verdict == APPLY:
        rationale = (f"For {for_score} vs Against {against_score} (net +{net}), with "
                     f"demand and financials both confirming. {headline or ''}").strip()
    elif verdict == AVOID:
        rationale = (f"Against {against_score} outweighs For {for_score} (net {net}). "
                     f"{headline or ''}").strip()
    elif verdict == UNKNOWN:
        rationale = "The issue has closed — no application decision left to make."
    elif net >= APPLY_NET and blockers:
        rationale = (f"The case is there (net +{net}) but held at NEUTRAL: "
                     f"{'; '.join(blockers)}.")
    else:
        rationale = (f"For {for_score} vs Against {against_score} (net {net}) — "
                     f"nothing decisive either way.")

    return {
        "verdict": verdict,
        "confidence": confidence,
        "winner": winner,
        "rationale": rationale,
        "key_factor": lead[0] if lead else "no single dominant factor",
        "for_score": for_score,
        "against_score": against_score,
        "net": net,
        "blockers": blockers,
        "demand_confirmed": demand_confirmed,
        "has_financials": has_financials,
    }


def evaluate(evidence: dict) -> dict:
    """evidence -> {scores, verdict}. Same contract shape as scoring.evaluate."""
    for_case = _for_case(evidence)
    against_case = _against_case(evidence)
    return {
        "scores": {
            "for": {"score": for_case.score(), "reasons": for_case.reasons},
            "against": {"score": against_case.score(), "reasons": against_case.reasons},
        },
        "verdict": judge(evidence, for_case.score(), against_case.score(),
                         for_case.reasons, against_case.reasons),
        "engine": "deterministic",
    }


# ---------------------------------------------------------------------------
# the whole desk
# ---------------------------------------------------------------------------

def review(log=None, scorer=None, news_fn=None, evaluate_fn=None) -> dict:
    """
    Fetch the calendar, gather evidence, judge every live issue.

    `news_fn(symbol, name)` and `evaluate_fn(evidence)` are injected so the app
    can supply RSS research and the LLM panel without this module importing
    either.
    """
    say = log or (lambda _m: None)
    try:
        calendar = fetch_calendar(log=say)
    except Exception as exc:                                       # noqa: BLE001
        say(f"IPO desk unavailable ({type(exc).__name__}: {exc})")
        return {"generated": datetime.now(IST).strftime("%d %b %Y, %H:%M IST"),
                "error": str(exc)[:200], "ipos": [], "open": 0, "upcoming": 0}

    notes = load_notes()
    rows = []
    for item in calendar:
        if item["window"] == "closed":
            continue
        news = None
        if news_fn:
            try:
                news = news_fn(item["symbol"], item["name"])
            except Exception:                                      # noqa: BLE001
                news = None
        evidence = build_evidence(item, notes.get(item["symbol"]), news)
        result = (evaluate_fn or evaluate)(evidence)
        rows.append({**item,
                     "verdict": result["verdict"],
                     "scores": result["scores"],
                     "financials": evidence["financials"],
                     "gmp": evidence["gmp"],
                     "news_tone": evidence["news"].get("net_tone"),
                     "news_total": evidence["news"].get("total"),
                     "data_gaps": evidence["data_gaps"],
                     "engine": result.get("engine")})

    order = {"open": 0, "upcoming": 1, "closed": 2}
    rows.sort(key=lambda r: (order.get(r["window"], 3), r.get("days_to_close") or 99))

    applies = [r for r in rows if r["verdict"]["verdict"] == APPLY]
    if rows:
        say(f"IPO desk: {len(rows)} live issue(s), {len(applies)} rated APPLY"
            + (f" ({', '.join(r['symbol'] for r in applies)})" if applies else ""))
    missing = [r["symbol"] for r in rows if not r["verdict"]["has_financials"]]
    if missing:
        say(f"IPO desk: no DRHP financials on file for {', '.join(missing)} — "
            f"add them to ipo_notes.json to let the panel assess the business")

    return {
        "generated": datetime.now(IST).strftime("%d %b %Y, %H:%M IST"),
        "source": "NSE official calendar and subscription data",
        "ipos": rows,
        "open": sum(1 for r in rows if r["window"] == "open"),
        "upcoming": sum(1 for r in rows if r["window"] == "upcoming"),
        "apply_count": len(applies),
        "note": ("Financials are hand-entered from the DRHP; GMP is unofficial and "
                 "never sufficient on its own. Applying to an IPO is a decision "
                 "only you can make — this is analysis, not advice."),
    }
