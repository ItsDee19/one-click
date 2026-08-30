"""
drhp.py — read the offer document, so the IPO desk can assess the business.

Until now the IPO panel could see how an issue was being bid but not what it
was bidding for: DRHP financials were hand-entered, so every live issue read
NEUTRAL with "no DRHP financials on file". This closes that.

THE PIPELINE
------------
1. NSE publishes an offer-documents index with direct PDF links. Unlike its
   quote API (403) that endpoint is open.
2. Match the IPO to a filing by company name, download the PDF.
3. Score every page and keep the few that actually carry the numbers —
   "Basis for Issue Price", the peer-comparison table, the KPI table. A DRHP
   runs 300-700 pages; feeding all of it to a model is neither affordable nor
   accurate.
4. Hand those pages to the LLM to return structured JSON. The tables extract
   as run-together text that regex cannot parse reliably, which is exactly
   what a model is good at and exactly where a silent misparse would be worst.
5. **Verify every number against the source text.** A figure the model
   returns that does not appear in the pages it was given is dropped, not
   printed. This is the whole reason the step exists.
6. Cache the result, because a 20 MB download and a 400-page parse should
   happen once per company, not once per run.

WHAT A DRHP CAN AND CANNOT GIVE
-------------------------------
A DRHP is filed before the price is set, so the price band and post-issue P/E
appear as "[●]" placeholders. It does carry restated revenue, profit, EPS,
RoNW and the peer comparison table.

So the P/E is not read from the document — it is computed from the EPS in the
document and the actual price band from the exchange calendar. That is
arithmetic on two sourced figures rather than a number lifted from a page that
does not contain it.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".drhp_cache")

NSE_BASE = "https://www.nseindia.com"
OFFER_DOCS_URL = NSE_BASE + "/api/corporates/offerdocs?index=equities"
OFFER_DOCS_PAGE = NSE_BASE + "/companies-listing/corporate-filings-offer-documents"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": OFFER_DOCS_PAGE,
}

MAX_PDF_MB = 60             # a few DRHPs are enormous; refuse rather than hang
DOWNLOAD_TIMEOUT = 120
MAX_PAGES_TO_MODEL = 6
MAX_CHARS_TO_MODEL = 22000
CACHE_DAYS = 30             # a filed DRHP does not change


def _now():
    return datetime.now(IST)


def _norm(text):
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


# ---------------------------------------------------------------------------
# locating the document
# ---------------------------------------------------------------------------

def _session():
    import requests
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(OFFER_DOCS_PAGE, timeout=20)      # NSE checks for its own cookies
    except Exception:                                              # noqa: BLE001
        pass
    return s


_INDEX_CACHE = {"at": 0.0, "rows": None}


def offer_index(session=None, log=None, ttl=900):
    """The exchange's filing index, cached briefly — it is ~1 MB."""
    say = log or (lambda _m: None)
    if _INDEX_CACHE["rows"] is not None and time.time() - _INDEX_CACHE["at"] < ttl:
        return _INDEX_CACHE["rows"]
    try:
        s = session or _session()
        rows = s.get(OFFER_DOCS_URL, timeout=40).json()
        rows = rows if isinstance(rows, list) else []
    except Exception as exc:                                       # noqa: BLE001
        say(f"offer-document index unavailable ({type(exc).__name__})")
        return []
    _INDEX_CACHE.update({"at": time.time(), "rows": rows})
    say(f"offer-document index: {len(rows)} filings")
    return rows


def find_document(company_name, symbol=None, rows=None, log=None):
    """
    The best available PDF for one company.

    Preference order is deliberate: the RHP carries the final price band, the
    final prospectus is complete, and the DRHP is the earliest and most
    commonly available. Whichever is found is reported, so the caller knows
    which document the numbers came from.
    """
    say = log or (lambda _m: None)
    rows = rows if rows is not None else offer_index(log=say)
    if not rows:
        return None

    target = _norm(company_name)
    if not target:
        return None

    matches = [r for r in rows if _norm(r.get("company")) == target]
    if not matches:
        stem = target[:16]
        matches = [r for r in rows if stem and stem in _norm(r.get("company"))]
    if not matches and symbol:
        matches = [r for r in rows
                   if (r.get("symbol") or "").strip().upper() == symbol.strip().upper()]
    if not matches:
        return None

    for key, kind in (("rhpAttach", "RHP"), ("fpAttach", "Final Prospectus"),
                      ("drhpAttach", "DRHP")):
        for row in matches:
            url = str(row.get(key) or "").strip()
            if url.lower().endswith(".pdf"):
                return {"url": url, "kind": kind,
                        "company": row.get("company"),
                        "filed": row.get("drhpDate") or row.get("fpDate"),
                        "status": row.get("drhpStatus")}
    return None


# ---------------------------------------------------------------------------
# reading it
# ---------------------------------------------------------------------------

# Pages worth sending to the model. Scored rather than matched, because the
# cover page repeats "Basis for Issue Price" without carrying any numbers.
_PAGE_SIGNALS = [
    (re.compile(r"basis\s+for\s+(the\s+)?(issue|offer)\s+price", re.I), 2),
    (re.compile(r"price\s*/\s*earning|p\s*/\s*e\s*ratio", re.I), 3),
    (re.compile(r"comparison\s+with\s+listed|peer\s+group|industry\s+peer", re.I), 3),
    (re.compile(r"return\s+on\s+net\s*worth|ro\s*nw", re.I), 2),
    (re.compile(r"earnings?\s+per\s+share|diluted\s+eps", re.I), 2),
    (re.compile(r"restated\s+(consolidated\s+)?(financial|statement)", re.I), 1),
    (re.compile(r"revenue\s+from\s+operations", re.I), 1),
    (re.compile(r"key\s+performance\s+indicator", re.I), 1),
]
MIN_PAGE_SCORE = 5


def relevant_pages(reader, log=None):
    """The handful of pages that actually carry the numbers."""
    say = log or (lambda _m: None)
    scored = []
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:                                          # noqa: BLE001
            continue
        if len(text) < 200:
            continue
        score = sum(weight for pattern, weight in _PAGE_SIGNALS if pattern.search(text))
        # a page with no digits is prose, whatever it is titled
        if score >= MIN_PAGE_SCORE and len(re.findall(r"\d", text)) > 40:
            scored.append((score, index, text))

    scored.sort(key=lambda row: (-row[0], row[1]))
    chosen = scored[:MAX_PAGES_TO_MODEL]
    if chosen:
        say(f"DRHP: {len(reader.pages)} pages, keeping "
            f"{', '.join(str(i) for _s, i, _t in sorted(chosen, key=lambda r: r[1]))}")
    return sorted(chosen, key=lambda row: row[1])


def fetch_text(document, session=None, log=None):
    """Download the PDF and return the text of its relevant pages."""
    say = log or (lambda _m: None)
    s = session or _session()

    try:
        response = s.get(document["url"], timeout=DOWNLOAD_TIMEOUT)
    except Exception as exc:                                       # noqa: BLE001
        return None, f"download failed ({type(exc).__name__})"
    if response.status_code >= 400:
        return None, f"download returned {response.status_code}"

    size_mb = len(response.content) / 1e6
    if size_mb > MAX_PDF_MB:
        return None, f"document is {size_mb:.0f} MB, over the {MAX_PDF_MB} MB limit"
    if not response.content[:5].startswith(b"%PDF"):
        return None, "the link did not return a PDF"

    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(response.content))
    except Exception as exc:                                       # noqa: BLE001
        return None, f"could not parse the PDF ({type(exc).__name__})"

    pages = relevant_pages(reader, log=say)
    if not pages:
        return None, "no page carrying the financial tables could be identified"

    parts = []
    for _score, index, text in pages:
        cleaned = re.sub(r"[ \t]+", " ", text)
        parts.append(f"--- page {index + 1} ---\n{cleaned}")
    joined = "\n\n".join(parts)[:MAX_CHARS_TO_MODEL]
    say(f"DRHP: {size_mb:.1f} MB, {len(reader.pages)} pages, "
        f"{len(joined)} chars of financial text extracted")
    return joined, None


# ---------------------------------------------------------------------------
# structured extraction, verified against the source
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """You are reading pages from an Indian IPO offer document
(DRHP/RHP). Extract the issuer's own restated financials and its peer set.

Rules:
1. Every number must appear in the text below. Do not infer, annualise,
   convert or recall anything from elsewhere.
2. A DRHP is filed before pricing, so the price band and post-issue P/E are
   often "[●]" placeholders. If a value is a placeholder or absent, return
   null. Never substitute a peer's number for the issuer's.
3. Amounts are usually in ₹ lakhs or ₹ millions — convert to ₹ CRORE and say
   which unit the document used in `units_seen`.
4. peer_pe is the MEDIAN P/E of the listed peer companies in the comparison
   table, excluding the issuer itself and any "NA" entries.

Return ONLY this JSON, no prose, no markdown fence:
{
  "revenue_cr": number|null,
  "profit_cr": number|null,
  "eps": number|null,
  "ronw_pct": number|null,
  "nav_per_share": number|null,
  "peer_pe": number|null,
  "peer_names": [string],
  "units_seen": string,
  "fiscal_year": string|null,
  "evidence": {"revenue_cr": "quoted snippet", "profit_cr": "quoted snippet",
               "eps": "quoted snippet", "peer_pe": "quoted snippet"}
}

TEXT:
"""

_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def _numbers_in(text):
    """Every number in the source, plus scaled variants for unit conversion."""
    found = set()
    for token in _NUM.findall(text or ""):
        try:
            value = float(token.replace(",", ""))
        except ValueError:
            continue
        found.add(abs(value))
        # lakhs -> crore, millions -> crore: the model is asked to convert, so
        # the converted figure must be traceable to the printed one
        found.add(round(abs(value) / 100.0, 2))
        found.add(round(abs(value) / 10.0, 2))
        found.add(round(abs(value), 2))
    return found


def verify(extracted, source, tolerance=0.02):
    """
    Drop any figure that cannot be traced to the source text.

    A model reading a 400-page document under instruction to convert units is
    exactly where a plausible-looking invention would slip through. Anything
    unverifiable is nulled and named rather than shown.
    """
    pool = _numbers_in(source)
    flagged = []
    checked = dict(extracted)

    for field in ("revenue_cr", "profit_cr", "eps", "ronw_pct",
                  "nav_per_share", "peer_pe"):
        value = checked.get(field)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            checked[field] = None
            flagged.append(f"{field} (not a number)")
            continue
        target = abs(value)
        ok = any(abs(target - candidate) <= max(tolerance, target * tolerance)
                 for candidate in pool)
        if not ok:
            checked[field] = None
            flagged.append(f"{field}={value}")
    return checked, flagged


def extract_financials(text, llm_call, log=None):
    """Run the model over the extracted pages, then verify what it returns."""
    say = log or (lambda _m: None)
    try:
        raw = llm_call(EXTRACT_PROMPT + text)
    except Exception as exc:                                       # noqa: BLE001
        return None, f"extraction call failed ({type(exc).__name__})"

    try:
        import llm as llm_mod
        payload = llm_mod.extract_json(raw)
    except Exception as exc:                                       # noqa: BLE001
        return None, f"model did not return usable JSON ({type(exc).__name__})"

    verified, flagged = verify(payload, text)
    if flagged:
        say(f"DRHP: dropped {len(flagged)} unverifiable figure(s) — {', '.join(flagged)}")
    verified["unverified_dropped"] = flagged
    verified["evidence"] = payload.get("evidence") or {}
    return verified, None


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _cache_path(symbol):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", (symbol or "unknown").upper())
    return os.path.join(CACHE_DIR, f"{safe}.json")


def read_cache(symbol, max_age_days=CACHE_DAYS):
    path = _cache_path(symbol)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        fetched = datetime.fromisoformat(blob.get("fetched_at"))
    except Exception:                                              # noqa: BLE001
        return None
    if (_now() - fetched).days > max_age_days:
        return None
    return blob


def write_cache(symbol, blob):
    os.makedirs(CACHE_DIR, exist_ok=True)
    blob = dict(blob, fetched_at=_now().isoformat())
    try:
        with open(_cache_path(symbol), "w", encoding="utf-8") as fh:
            json.dump(blob, fh, indent=1, ensure_ascii=False)
    except OSError:
        pass
    return blob


# ---------------------------------------------------------------------------
# the whole job
# ---------------------------------------------------------------------------

def analyse(symbol, company_name, price_band_high=None, llm_call=None,
            rows=None, log=None, force=False):
    """
    Everything for one company: locate, download, extract, verify, cache.

    `price_band_high` lets the post-issue P/E be computed from the document's
    EPS and the exchange's actual price band — the DRHP itself carries only a
    placeholder, so reading a P/E out of it would be reading something that is
    not there.
    """
    say = log or (lambda _m: None)

    if not force:
        cached = read_cache(symbol)
        if cached:
            return _with_pe(cached, price_band_high)

    document = find_document(company_name, symbol=symbol, rows=rows, log=say)
    if not document:
        return _with_pe(write_cache(symbol, {
            "symbol": symbol, "available": False,
            "reason": "no offer document listed for this company on the exchange",
        }), price_band_high)

    say(f"DRHP: {symbol} — fetching {document['kind']} for {document['company']}")
    session = _session()
    text, error = fetch_text(document, session=session, log=say)
    if error:
        return _with_pe(write_cache(symbol, {
            "symbol": symbol, "available": False, "document": document,
            "reason": error,
        }), price_band_high)

    if not llm_call:
        return _with_pe(write_cache(symbol, {
            "symbol": symbol, "available": False, "document": document,
            "reason": "no LLM available to read the extracted pages",
        }), price_band_high)

    financials, error = extract_financials(text, llm_call, log=say)
    if error:
        return _with_pe(write_cache(symbol, {
            "symbol": symbol, "available": False, "document": document,
            "reason": error,
        }), price_band_high)

    blob = {
        "symbol": symbol,
        "available": True,
        "document": document,
        "financials": financials,
        "source": f"{document['kind']} via NSE offer documents",
        "reason": None,
    }
    say(f"DRHP: {symbol} — revenue {financials.get('revenue_cr')} Cr, "
        f"profit {financials.get('profit_cr')} Cr, EPS {financials.get('eps')}, "
        f"peer P/E {financials.get('peer_pe')}")
    return _with_pe(write_cache(symbol, blob), price_band_high)


def _with_pe(blob, price_band_high):
    """
    Post-issue P/E, computed rather than read.

    The DRHP prints "[●]" where the P/E will go, because the price is not set
    when it is filed. Dividing the exchange's actual band by the document's
    EPS gives the real figure from two sourced numbers.
    """
    blob = dict(blob)
    fin = dict(blob.get("financials") or {})
    eps = fin.get("eps")
    fin.setdefault("pe_post_issue", None)

    if blob.get("available") and eps and price_band_high:
        try:
            eps_value = float(eps)
            # A P/E on negative earnings is not a valuation, it is an artifact
            # of the arithmetic — and printing "-19.81x" next to a peer's
            # "17.83x" invites exactly the wrong comparison. Loss-making is
            # the finding; the Against case already scores it.
            if eps_value > 0:
                fin["pe_post_issue"] = round(float(price_band_high) / eps_value, 2)
                fin["pe_basis"] = (f"upper band {price_band_high} / EPS {eps} from the "
                                   f"offer document — computed, not quoted")
            else:
                fin["pe_post_issue"] = None
                fin["pe_basis"] = (f"not meaningful: EPS is {eps}, so the company was "
                                   f"loss-making in the reported period")
        except (TypeError, ValueError, ZeroDivisionError):
            fin["pe_post_issue"] = None
    blob["financials"] = fin
    return blob
