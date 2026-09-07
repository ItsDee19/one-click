"""Offline browser QA for the four desks; never starts the real application.

Run: python tests/preview_server.py --port 5173
Open /, /intraday-desk, /ipo-desk, or /quality-desk.
Append ?scenario=loaded|empty|idle|error|slow to any page to change the
session fixture. All financial values are illustrative UI fixtures.

Only page_render is imported from the application. No .env is read, no DB or
cache is created, and no scheduler, external API, LLM or Telegram code runs.
POST /start simulates progress in memory. CSP restricts browser connections to
this server, including when a remembered API override exists in localStorage.
"""

from __future__ import annotations

import argparse
import copy
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
import page_render  # noqa: E402

STAMP = "Illustrative offline preview"
PAGES = {
    "/": "dashboard.html", "/index.html": "dashboard.html",
    "/intraday-desk": "intraday_page.html", "/intraday-desk.html": "intraday_page.html",
    "/ipo-desk": "ipo_page.html", "/ipo-desk.html": "ipo_page.html",
    "/quality-desk": "quality_page.html", "/quality-desk.html": "quality_page.html",
}
AGENTS = [
    ("scout", "Scout", "Screens the stock universe for movers", "Scanned", "Shortlisted"),
    ("technician", "Technician", "Reads price action, volume and trend", "Analyzed", "Avg RVOL"),
    ("fundamentalist", "Fundamentalist", "Weighs analyst targets and consensus", "Covered", "Avg upside"),
    ("newsdesk", "Newsdesk", "Checks headlines and news sentiment", "Headlines", "Net tone"),
    ("bull", "Bull", "Builds the case for the opportunity", "Cases", "Avg score"),
    ("bear", "Bear", "Challenges the thesis and downside", "Cases", "Avg score"),
    ("judge", "Judge", "Weighs the debate and issues verdicts", "Verdicts", "Buy"),
    ("messenger", "Messenger", "Keeps the research record up to date", "Sent", "Engine"),
]
MARKET = {"phase": "closed", "label": "Offline preview", "live_session": False,
          "session_pct": 100, "minutes_to_close": 0}
REGIME = {"state": "risk_on", "pct_vs_sma": 4.82, "note": STAMP}
CRITERIA = {"altman_z_min": 3, "piotroski_min": 7, "market_cap_cr_min": 500,
            "operating_margin_min": 15, "sales_growth_min": 15, "profit_growth_min": 15,
            "debt_to_equity_max": 1, "promoter_holding_min": 50}


def fixture_data():
    """Use shipped illustrative bundles for familiar, consistently priced names."""
    bundles = [json.loads((ROOT / "demo_data" / (symbol + ".json")).read_text(encoding="utf-8"))
               for symbol in ("POLYCAB", "KPITTECH", "TCS", "RELIANCE", "CDSL", "DIXON")]
    verdicts = []
    for i, b in enumerate(bundles):
        price = b["price"]["live"]
        verdict = "BUY" if i < 3 else ("WATCH" if i < 5 else "AVOID")
        positional = {
            "verdict": verdict, "confidence": 8 if verdict == "BUY" else 5,
            "horizon": "4–8 weeks", "horizon_basis": "Illustrative UI fixture only",
            "why": ("Trend and participation support the multi-week case. Analyst targets leave room above the current price."
                    if verdict == "BUY" else "Wait for stronger participation and a clearer balance of upside and downside."),
            "levels": {"trigger": round(price * 1.005, 2), "invalidation": round(price * .96, 2),
                       "objective": round(price * 1.1, 2)},
            "risk_reward": {"ratio": 2.5}, "fired": False,
        }
        verdicts.append({"symbol": b["symbol"], "name": b["name"], "cap_segment": b["cap_segment"],
                         "sector": b["sector"], "price": price,
                         "day_change_pct": b["price"]["day_change_pct"], "rel_day_change_pct": 1.2,
                         "data_gaps": 0, "tracks": {"positional": positional,
                         "intraday": {"verdict": "UNAVAILABLE", "confidence": None,
                                      "horizon": "Same session", "why": "Offline preview: there is no live session tape."}}})

    sectors = {"generated": STAMP, "leaders": ["IT", "Electricals"], "rows": []}
    for sector, change, best, worst in (("IT", 1.85, "KPITTECH", "INFY"),
                                       ("Electricals", 1.26, "POLYCAB", "HAVELLS"),
                                       ("Banking", -.42, "ICICIBANK", "SBIN")):
        sectors["rows"].append({"sector": sector, "index": "NIFTY " + sector,
                                "index_change_pct": change, "median_move_pct": change * .8,
                                "advancing": 8 if change > 0 else 3, "declining": 2 if change > 0 else 7,
                                "advance_pct": 80 if change > 0 else 30,
                                "best": {"symbol": best, "change": change + .8},
                                "worst": {"symbol": worst, "change": change - 1},
                                "state": "leading" if change > 0 else "lagging"})
    orderbook = {"generated": STAMP, "with_orderbook": 6, "candidates": 6, "qualifying": [
        {"symbol": "DEMOENG", "sector": "Illustrative engineering company", "book_to_sales": 4.6,
         "order_book_cr": 9200, "quarterly_revenue_cr": 2000, "order_book_as_of": STAMP,
         "stale": False, "reason": "Illustrative fixture to verify the order-book research layout."}]}

    config = {"brand": "Dalal Desk", "agents": [dict(zip(("id", "name", "role", "stat1", "stat2"), a)) for a in AGENTS],
              "agent_count": len(AGENTS), "modes": ["demo", "live"], "engine": "offline preview",
              "engine_provider": "deterministic", "engine_reason": STAMP, "confidence_threshold": 7,
              "shortlist_per_bucket": 4, "telegram_configured": False,
              "universe": {"total": 164, "buckets": {"large": 60, "mid": 54, "small": 50}},
              "demo_bundles": 12, "market": MARKET, "regime": REGIME, "scheduler": {"enabled": False},
              "risk": {"capital": 100000}, "executes_orders": False, "paper_trading": False}

    intraday_record = json.loads((ROOT / "backtest_intraday.json").read_text(encoding="utf-8"))
    swing_record = json.loads((ROOT / "backtest_swing.json").read_text(encoding="utf-8"))
    intraday = {"generated": STAMP, "tradeable": False, "note": STAMP,
                "record_window": intraday_record["window"],
                "record_sessions": intraday_record["stock_sessions"],
                "strategies": intraday_record["strategies"], "swing": swing_record, "picks": []}
    for b, strategy in zip(bundles[:2], ("RVOL momentum", "ORB breakout")):
        price = b["price"]["live"]
        record = copy.deepcopy(intraday_record["strategies"][strategy])
        record["trusted"] = record["enough"]
        intraday["picks"].append({"symbol": b["symbol"], "name": b["name"], "sector": b["sector"],
                                  "strategy": strategy, "entry": price, "stop": round(price * .98, 2),
                                  "target": round(price * 1.04, 2), "last": price, "reward_risk": 2,
                                  "vwap": round(price * .993, 2), "rvol": 2.1, "confidence": 7,
                                  "record": record, "why": "Illustrative setup for UI preview; no live signal has been generated."})
    ipos = {"generated": STAMP, "open": 2, "ipos": []}
    for name, symbol, verdict, window in (("Example Renewables", "EXAMPLEREN", "APPLY", "open"),
                                          ("Sample Digital Services", "SAMPLEDIG", "NEUTRAL", "open"),
                                          ("Illustrative Consumer Brands", "ILLUSBRAND", "AVOID", "upcoming")):
        ipos["ipos"].append({"name": name + " · fictional preview", "symbol": symbol, "window": window,
                              "closes": "11 Sep 2026", "days_to_close": 3, "price_band": {"low": 240, "high": 255},
                              "subscription": {"total": 3.24}, "issue": {"value_cr": 1250},
                              "financials": {"pe_post_issue": 24.5, "peer_pe": 28.2, "profit_cr": 106},
                              "gmp": {}, "verdict": {"verdict": verdict, "confidence": 7 if verdict == "APPLY" else 5,
                              "rationale": "Fictional issue used to verify the IPO research interface. These figures are illustrative.",
                              "blockers": [] if verdict == "APPLY" else ["More evidence needed"]}})
    quality = {"generated": STAMP, "criteria": CRITERIA, "examined": 2184, "listed": 2288,
               "caveats": ["Illustrative UI fixtures, not findings from a current screen.",
                           "Growth is measured over the available filing window. Promoter holding is an estimate."], "matches": []}
    for b in bundles[:2]:
        values = {"altman_z": 5.62, "piotroski": 8, "market_cap_cr": 78520,
                  "operating_margin": 19.6, "sales_growth": 22.4, "profit_growth": 26.1,
                  "debt_to_equity": .18, "promoter_holding": 62.8}
        quality["matches"].append({"symbol": b["symbol"], "name": b["name"], "sector": b["sector"],
                                    "price": b["price"]["live"], "growth_window_years": 4,
                                    "checks": {key: {"value": value, "passed": True} for key, value in values.items()}})
    return config, verdicts, sectors, orderbook, intraday, ipos, quality


CONFIG, VERDICTS, SECTORS, ORDERBOOK, INTRADAY, IPOS, QUALITY = fixture_data()
RUN = {"started": None, "id": 1, "capital": None}
RUN_LOCK = threading.Lock()


def status_fixture(scenario):
    with RUN_LOCK:
        run = dict(RUN)
    elapsed = time.monotonic() - run["started"] if run["started"] is not None else 99
    running = elapsed < 6
    idle = scenario == "idle" and run["started"] is None
    count = 0 if idle else (min(len(AGENTS), int(elapsed / .65)) if running else len(AGENTS))
    rows = [] if idle or scenario == "empty" else VERDICTS[:max(0, count - 2)] if running else VERDICTS
    return {
        "status": "idle" if idle else "running" if running else "done", "mode": "demo",
        "engine": "offline preview", "run_id": run["id"], "started_at": str(run["started"] or "preview"),
        "finished_at": None if running else STAMP, "data_ts": STAMP, "capital": run["capital"],
        "market": MARKET, "regime": REGIME, "scheduler": {"enabled": False},
        "kpis": {"universe": 0 if idle else 12, "in_debate": len(rows),
                 "buy_signals": sum(r["tracks"]["positional"]["verdict"] == "BUY" for r in rows),
                 "intraday_signals": 0, "positional_signals": min(3, len(rows)),
                 "top_pick": {"symbol": "POLYCAB", "confidence": 8, "verdict": "BUY", "track": "positional", "horizon": "4–8 weeks"} if rows else {}},
        "agents": [{"id": a[0], "name": a[1], "role": a[2],
                    "status": "done" if i < count else "working" if i == count and running else "offline",
                    "stat1": {"label": a[3], "value": 12 if i < count else "—"},
                    "stat2": {"label": a[4], "value": ("preview" if i == 7 else 6) if i < count else "—"}}
                   for i, a in enumerate(AGENTS)],
        "verdicts": rows, "sector_heat": {} if idle or scenario == "empty" else SECTORS,
        "orderbook": {} if idle or scenario == "empty" else ORDERBOOK,
        "scoreboard_line": "Illustrative preview · no settled signals", "calibration_line": "",
        "telegram": {"configured": False, "sent": 0, "error": None}, "error": None,
        "log": ["[preview] Offline fixtures loaded. No external requests or messages."] +
               [f"[preview] {a[1]} completed." for a in AGENTS[:count]],
    }


class PreviewHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if self.path != "/status":
            super().log_message(fmt, *args)

    def respond(self, data, code=200, content_type="application/json", scenario=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if content_type == "application/json" else data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' data:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self'; frame-ancestors 'none'")
        if scenario:
            self.send_header("Set-Cookie", f"preview_scenario={scenario}; Path=/; SameSite=Strict")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def scenario(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        item = cookie.get("preview_scenario")
        return item.value if item else "loaded"

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path in PAGES:
            scenario = parse_qs(url.query).get("scenario", [self.scenario()])[0]
            if scenario not in ("loaded", "empty", "idle", "error", "slow"):
                scenario = "loaded"
            if "scenario" in parse_qs(url.query):
                with RUN_LOCK:
                    RUN["started"] = None
            html = page_render.render_file(str(ROOT / PAGES[url.path]))
            # A remembered API override cannot escape the local fixture server.
            guard = "<script>try{localStorage.removeItem('dalal.api');var u=new URL(location.href);u.searchParams.delete('api');history.replaceState(null,'',u);}catch(e){}window.__API_BASE__='';window.__OFFLINE_PREVIEW__=true;</script>"
            html = html.replace("<head>", "<head>" + guard, 1)
            return self.respond(html, content_type="text/html", scenario=scenario)
        if url.path == "/favicon.ico":
            return self.respond("", code=204, content_type="text/plain")
        scenario = self.scenario()
        if url.path == "/health":
            return self.respond({"ok": True, "preview": True, "side_effects": False})
        endpoints = {"/config": CONFIG, "/status": None, "/intraday": INTRADAY, "/ipos": IPOS,
                     "/quality": QUALITY, "/sectors": SECTORS, "/orderbook": ORDERBOOK}
        if url.path not in endpoints:
            return self.respond({"error": "Unknown preview endpoint"}, code=404)
        if scenario == "error":
            return self.respond({"error": "Simulated backend unavailable for UI QA."}, code=503)
        if scenario == "slow" and url.path != "/status":
            time.sleep(4)
        data = status_fixture(scenario) if url.path == "/status" else copy.deepcopy(endpoints[url.path])
        if scenario == "empty":
            if url.path == "/intraday":
                data.update(picks=[], note="Offline fixture: there is no live session to scan.")
            elif url.path == "/ipos":
                data.update(ipos=[], open=0)
            elif url.path == "/quality":
                data.update(matches=[])
        self.respond(data)

    def do_POST(self):
        if urlsplit(self.path).path != "/start":
            return self.respond({"ok": False, "error": "Unknown preview endpoint"}, code=404)
        if self.scenario() == "error":
            return self.respond({"ok": False, "error": "Simulated run failure."}, code=503)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 4096:
                raise ValueError("Request too large")
            payload = json.loads(self.rfile.read(size) or b"{}")
            capital = payload.get("capital")
            if capital is not None and (not isinstance(capital, (int, float)) or capital <= 0):
                raise ValueError("Capital must be positive")
        except (ValueError, TypeError) as exc:
            return self.respond({"ok": False, "error": str(exc)}, code=400)
        with RUN_LOCK:
            if RUN["started"] is not None and time.monotonic() - RUN["started"] < 6:
                conflict = True
            else:
                conflict = False
                RUN.update(started=time.monotonic(), id=RUN["id"] + 1, capital=capital)
        self.respond({"ok": not conflict, "mode": "demo", "preview": True,
                      **({"error": "A preview is already running."} if conflict else {})}, code=409 if conflict else 200)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5173)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print(f"Offline UI preview: http://127.0.0.1:{args.port} (illustrative fixtures; no external side effects)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
