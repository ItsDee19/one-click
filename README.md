# Dalal Desk

A local, one-click dashboard that runs a panel of named agents over Indian
stocks, debates every pick with an LLM, and pushes BUY signals to Telegram.

Everything runs on your machine. There is no cloud backend, no account, no
telemetry. **No order is ever placed — this is analysis only, and it is not
investment advice.**

---

## Run it

```bash
pip install -r requirements.txt
```

```bash
python app.py
```

Open <http://127.0.0.1:5000> (it opens by itself), pick a mode, press
**Start agents**.

Start with **Demo** — it runs fully offline. Use **Live** during market hours
(NSE: Mon–Fri, 09:15–15:30 IST).

---

## The LLM, without an API key

The debate engine auto-detects a provider in this order:

| # | Provider | What it needs | Cost |
|---|---|---|---|
| 1 | `claude_code` | the `claude` CLI on your PATH, logged in | your existing Claude Pro/Max plan — no API key, no per-call billing |
| 2 | `anthropic` | `ANTHROPIC_API_KEY` in `.env` | per-token API billing |
| 3 | `openai` | `OPENAI_API_KEY` in `.env` | per-token API billing |
| — | `deterministic` | nothing at all | free, always works |

**The no-API-key route:** install Claude Code, run `claude` in a terminal,
`/login` with your Claude plan. The app finds the CLI on PATH and shells out to
it (`claude -p "<prompt>" --output-format json --model haiku`) — one call per
stock. Force a specific provider with `LLM_PROVIDER=claude_code|anthropic|openai`.

If no provider is available — no login, no key, no network, a timeout, a
malformed reply — the app silently falls back to the **deterministic panel** in
`scoring.py` and says so in the footer, the log and the audit database. A run
never fails because the LLM was unreachable.

> Note: the `claude` CLI refuses to launch inside an existing Claude Code
> session. Run `python app.py` from a normal terminal.

---

## Telegram

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Message [@userinfobot](https://t.me/userinfobot) → copy your numeric chat id.
3. Put both in `.env` (copy `.env.example` first), then restart the app.

When a run finishes, the app posts **one message per fired BUY** plus **one
daily summary**. A stock fires when the verdict is `BUY` **and** confidence is
at least `CONFIDENCE_THRESHOLD` (default 7/10).

The bot token is scrubbed out of every log line, error message and API response
before it can reach the console, the dashboard or the database.

Without Telegram configured everything else still works — signals appear on the
dashboard and in SQLite, they are just not delivered, and the dashboard says so.

---

## The panel

Agents run in pipeline order, `offline → working → done`:

| Agent | Role | Stat 1 | Stat 2 |
|---|---|---|---|
| **Scout** | screens the stock universe for movers | Scanned | Shortlisted |
| **Technician** | reads price action, RVOL & trend | Analyzed | Avg RVOL |
| **Fundamentalist** | weighs valuation & analyst targets | Covered | Avg upside |
| **Newsdesk** | pulls live news & scores sentiment | Headlines | Net tone |
| **Bull** | argues the case to buy | Cases | Avg score |
| **Bear** | argues the case against | Cases | Avg score |
| **Judge** | weighs the debate, issues verdict + confidence | Verdicts | Buy |
| **Messenger** | sends signals to Telegram | Sent | Engine |

One combined LLM call per stock seats all six (Bull, Bear, Fundamentals,
Technicals, News + the Judge). The board then reveals them in pipeline order so
the hand-off reads left to right.

---

## Data

**Demo** loads pre-built evidence bundles from `demo_data/*.json` — real NSE
tickers with plausible, internally consistent figures. Each shipped bundle says
so in its own `notes` field: they are illustrative, not a market capture. To
turn them into genuine snapshots, run this during market hours:

```bash
python capture_demo.py
```

That rewrites every bundle through the exact same yfinance path live mode uses,
and relabels them as real captures.

**Live** reads `universe.json` (editable; tickers must end in `.NS`), pulls ~1
month of daily OHLC for the whole universe in one batched call, screens each
cap bucket by day change, keeps the top `SHORTLIST_PER_BUCKET` (default 4), and
only then pays for `.info` / `.news` / analyst recommendations on the survivors.

### The evidence bundle

Every stock becomes one normalised bundle, and both engines see only this:

- `price` — live, day open/high/low, prev close, day change %, volume
- `range_52w` — high, low, % from high, position in range
- `technicals` — RVOL (time-adjusted, see above), % vs the 20-day SMA, window
  return, swing high/low, day-range position, ATR %, trend
  (`up`/`down`/`sideways`)
- `intraday` — gap %, opening range high/low, VWAP, price vs VWAP, session
  high/low/volume, or `available: false` with a stated reason
- `market` — trading phase, % of session elapsed, minutes to close
- `analyst` — consensus, analyst count, buy/hold/sell %, target mean/low/high,
  upside %
- `news` — total, positive/negative/neutral counts, net tone, recent headlines
- `data_gaps` — **every** field that could not be computed, named

A missing value is `null` **and** listed in `data_gaps`. Nothing is guessed,
interpolated or carried forward.

**This feed carries no raw fundamental ratios** — no P/E, P/B, ROE, margins or
debt. The Fundamentalist works purely from sell-side targets and consensus, and
both engines are instructed to say so rather than fake a valuation view.

---

## Two horizons

Every stock is judged **twice**, because the same evidence answers two
different questions.

### Intraday — closed before the bell

Reads the session tape only: VWAP, the opening range, the gap, time-adjusted
RVOL and position in the day's range, all built from 5-minute bars.

A BUY requires **all** of:

- net score ≥ 25
- price above VWAP
- price above the opening-range high
- time-adjusted RVOL ≥ 1.5
- at least 45 minutes left in the session

Those are enforced in code, for the LLM engine too. If the panel argues a BUY
without them the verdict is held down to WATCH and the row is tagged *held back
by desk rules* with the reason.

Each call carries three levels read from the evidence: **trigger** (opening
range high), **invalidation** (nearest of VWAP / opening-range low below price)
and **objective** (one average daily range higher).

### Positional — held for weeks

Reads trend, the 52-week position, analyst headroom and news. A BUY needs
net ≥ 25 **and** leadership (52-week position ≥ 60 or RVOL ≥ 3).

Each positional call carries a **holding window**:

```
hold 4w–2mo  ·  minimum ~19 trading days
basis: 15.52% to the mean target at an average daily range of 1.43%,
       assuming ~30% of that range accrues as net drift
```

That is arithmetic on two evidence figures — distance to the mean analyst
target, and the stock's own ATR — plus one openly stated assumption. It is
computed in code and handed *to* the model, never invented by it, so the same
number appears whichever engine ran.

**It is not a claim that the position becomes profitable in that time, or at
all.** It is an order-of-magnitude estimate of how long the thesis needs, and
it is capped at twelve months because that is the horizon sell-side targets are
set on.

---

## Running before the open

At 09:00 the session has not traded. There is no open, no range, no VWAP and no
volume — so **an honest intraday signal is impossible**, and the app says so
instead of recycling yesterday's tape:

```
INTRADAY   UNAVAILABLE   same session (once open)
   No intraday call possible: no live session — pre-open. VWAP, the opening
   range and today's RVOL do not exist until the session has traded.
   Watch: previous close, and the recent swing high/low from the daily window.
```

The **positional track works normally at 09:00** — it does not need today's
data. So a pre-open run gives you a real multi-week shortlist plus an intraday
watchlist with levels.

The workflow that actually works:

| Time | Run | What you get |
|---|---|---|
| ~09:00 | pre-open | positional BUYs are real; intraday is a watchlist with levels |
| ~09:45+ | confirmation | the opening range has printed — intraday verdicts become real |
| through the day | re-run | RVOL and VWAP firm up as the session builds |

Phase is taken from the feed's own `marketState` when yfinance provides it, so
market holidays are respected rather than guessed from the calendar.

### RVOL is scaled for time of day

`today_volume ÷ average_full_day_volume` is only a fair comparison at the
closing bell. Two hours into a 6h15m session, a stock trading exactly its
normal volume has printed about a third of it and looks dead.

RVOL is therefore divided by the fraction of the session elapsed, and both
figures are kept:

```json
"rvol": 1.02, "rvol_raw": 0.40,
"rvol_method": "scaled to 39% of session elapsed"
```

---

## The unattended schedule

The app runs itself at the two moments in an NSE day when the answer changes:

| Time (IST) | Purpose |
|---|---|
| **09:00** | pre-open — the positional shortlist is fully computable before the bell; intraday is a watchlist with levels |
| **09:45** | confirmation — the opening range has printed and RVOL has a real sample, so intraday verdicts become live |

Weekdays only. Holidays are not guessed from a hardcoded calendar: the run
starts, the feed reports the market closed, and the intraday track says so.

```bash
SCHEDULE_ENABLED=1
SCHEDULE_TIMES=09:00,09:45
SCHEDULE_MODE=live
```

Add times freely (`09:00,09:45,14:30`). The dashboard shows the next run, and
`GET /scheduler` returns it as JSON. No cron, no APScheduler — a daemon thread
compares the clock every 20 seconds and fires once per slot per day.

The machine has to be awake with `python app.py` running.

---

## Memory: the desk grades its own homework

Every fired signal is followed until it resolves:

- **objective** — the high touched the target first
- **invalidated** — the low broke the stop first
- **expired** — the horizon ran out with neither hit

Resolved against daily OHLC, so an intraday spike through a level counts, not
just the close. When both levels are touched on the same daily bar the order is
unknowable from daily data, so it settles as *invalidated* — assuming the worse
fill is the only honest choice.

### It refuses to quote a rate it cannot support

A hit rate off three signals is noise wearing a decimal point, and the first
version printed exactly that — `66.7% of 3` — on the dashboard *and* into the
LLM prompt, where a meaningless number can anchor the panel. Two gates now
stand in the way:

| Settled | What is reported |
|---|---|
| 0 | "no settled signals yet" |
| 1–7 | counts only — "3 settled (2 reached target, 1 stopped) — too few to read a rate from" |
| 8–19 | the rate, always with its interval, flagged `(indicative only)` |
| 20+ | the rate with its 95% confidence interval |

Intervals use the **Wilson score** method, because the textbook
`p ± z·√(p(1-p)/n)` collapses to ±0 at 0% and 100% — precisely where small
samples land. Two winners out of two would otherwise read "100%, no
uncertainty"; Wilson reports 34–100%, which is the truth.

```
positional: 60.0% of 10, 95% CI 31.3–83.2%, +4.00% avg (indicative only)
```

The raw proportion is still stored for the audit. What changes is that
nothing is allowed to *present* it as a rate until the sample supports one.

### It checks whether confidence means anything

A confidence number nobody audits is decoration. Settled signals are bucketed
by the confidence they were issued with, under the same gating:

```
confidence calibration: 7: 25.0% of 12, 9-10: 75.0% of 12
```

or, far more likely for a while:

```
confidence calibration: 4 settled across 2 buckets — too few to tell whether
higher confidence performs better
```

Both lines go to the dashboard and into the panel's prompt as context:

```
Track record so far: positional: 3 settled (2 reached target, 1 stopped) —
too few to read a rate from.
positional: called BUY 8/10 on 2026-08-20 at 100.0 — that signal resolved as
objective (10.0%)
```

The prompt explicitly tells the panel this is context, not instruction — a
previous BUY that was invalidated is a reason to look harder, not to flip.

**A signal that is already open does not fire again.** Without this the same
position is re-sent to Telegram every morning it still qualifies, which reads
as five signals instead of one:

```
KPITTECH [positional]: BUY not re-sent — positional signal from 25 Aug
still open at 1548.6 (5d cooldown)
```

Intraday signals are capped at one per stock per day. `GET /scoreboard` returns
the full record.

---

## Three more quality gates

**Relative strength.** A stock up 3% on a day the NIFTY is up 3% is not strong.
The index is pulled alongside the universe in the same request, and the screen
now ranks on strength *relative* to it, adjusted for participation:

```
score = relative day change % + 1.5 x (RVOL - 1)
```

Previously the screen ranked on raw day change, so a stock up 5% on dead volume
beat one up 2% on four times its usual volume.

**Risk/reward.** Every BUY is checked against its own levels. Reward under
1.5x risk is held to WATCH no matter how high the conviction, because score
alone cannot see that a setup risks more than it stands to make:

```
Held to WATCH on risk/reward: 4.0% to the objective against 5.0% to the
invalidation is 0.8:1, under the 1.5:1 the desk requires.
```

**Earnings awareness.** A print inside the next seven days is a binary event
the Bear now scores, and the date is named in the evidence.

**Exhaustion.** A 6% day is unremarkable for a stock that swings 4% and
extraordinary for one that swings 1%. Comparing raw percentages across stocks
hides that, so every move is measured in units of the stock's own average
range:

```
today's 8% is 4x its average daily range of 2% — extended, poor place to
start a position
```

The Bear scores it past 2.5x on the positional track and 2x on intraday,
which is what stops a breakout entry from becoming a top tick.

**Sector-relative strength.** The NIFTY comparison cannot see that an IT stock
down 1% on a day its sector is down 3% is quietly strong. Sector medians are
computed from the universe already being downloaded, and both sides score it.

**Concentration.** Five BUYs in one sector is one bet in five envelopes, and no
amount of per-stock analysis can see it — each stock is judged alone. The batch
is checked after the run and flagged on the dashboard and in the Telegram
summary:

```
⚠️ 3 of the fired names are Banking (HDFCBANK, ICICIBANK, SBIN) — these are
correlated, not independent positions
```

---

## Scoring

Both engines implement one interface:

```python
evaluate(evidence) -> {
  "scores": {agent: {"score": 0-100, "reasons": [...]}},
  "tracks": {
    "intraday":   {"verdict", "confidence", "winner", "rationale",
                   "key_catalyst", "horizon", "levels", ...},
    "positional": {..., "horizon_days_min", "horizon_days_max",
                   "horizon_basis"}
  }
}
```

**Deterministic rules** (`scoring.py`) — the Bull scores high RVOL, breakouts
(52-week position ≥ 85), price above a rising SMA, a strong day-range close,
analyst upside ≥ 10%, ≥ 80% buy ratings, positive news and a positive window
return. The Bear scores RVOL < 1, proximity to 52-week lows (< 30), price below
the SMA or a downtrend, no analyst headroom, weak conviction (buy < 55%), being
≥ 20% off the high, high sell %, negative news and a weak close.

The Judge takes `net = bull − bear`:

- **BUY** when `net ≥ 25` **and** there is leadership (52-week position ≥ 60 **or** RVOL ≥ 3)
- **AVOID** when `net ≤ −15`
- **WATCH** otherwise

`confidence = clamp(round(4 + net/15), 1, 10)`, forced ≥ 7 for a BUY and ≤ 6 for
anything else — so nothing can fire a signal without clearing the bar honestly.

### Grounding

Non-negotiable for both engines: every figure an agent cites must exist in the
evidence bundle. Nothing is invented; a missing value is reported as
"data unavailable".

A verifier scans the model's prose, extracts every number, and flags any that
cannot be traced back to the bundle (within rounding tolerance; window labels
like "52-week" and "20-day" are excluded). Flags appear on the verdict row as
`⚠ n unverified figure(s)`, in the log, and in the `ungrounded` column of the
database. The rule engine only ever quotes evidence, so it flags nothing.

---

## Config

Copy `.env.example` to `.env`. Real environment variables always win over the
file. `.env` is never read by the browser and never leaves the machine.

| Key | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | from @BotFather |
| `TELEGRAM_CHAT_ID` | — | from @userinfobot |
| `LLM_PROVIDER` | auto | force `claude_code` / `anthropic` / `openai` |
| `ANTHROPIC_API_KEY` | — | only for the `anthropic` provider |
| `OPENAI_API_KEY` | — | only for the `openai` provider |
| `CLAUDE_CLI_MODEL` | `haiku` | `haiku` or `sonnet` |
| `LLM_TIMEOUT` | `90` | seconds before one debate call gives up |
| `BRAND` | `Dalal Desk` | header name |
| `CONFIDENCE_THRESHOLD` | `7` | minimum confidence for a BUY to fire |
| `AGENT_DELAY` | `0.6` | seconds of visual pacing per agent |
| `SHORTLIST_PER_BUCKET` | `4` | movers per cap bucket sent to debate |
| `PORT` | `5000` | web server port |
| `NO_BROWSER` | — | set to `1` to stop the tab opening itself |
| `SCHEDULE_ENABLED` | `1` | run automatically at the scheduled times |
| `SCHEDULE_TIMES` | `09:00,09:45` | IST times, weekdays only |
| `SCHEDULE_MODE` | `live` | mode used by scheduled runs |
| `SIGNAL_COOLDOWN_DAYS` | `5` | don't re-send a positional BUY still open |
| `LLM_CONCURRENCY` | `3` | stocks debated at once |

---

## Files

```
app.py            server, agent state machine, Telegram, SQLite
market.py         NSE trading phase + session-elapsed maths
scheduler.py      unattended 09:00 / 09:45 runs
history.py        outcome tracking, track record, signal cooldown
scoring.py        deterministic agents + both Judges
llm.py            LLM debate, provider detection, grounding verifier
data_sources.py   demo loader, yfinance adapter, evidence builder
dashboard.html    the whole UI — inline CSS/JS, no build step, no libraries
universe.json     editable tickers per cap bucket
capture_demo.py   rewrite demo_data/ from real live data
demo_data/*.json  offline evidence bundles
signals.db        SQLite audit (created on first run)
```

### Routes

| Route | Purpose |
|---|---|
| `GET /` | the dashboard |
| `POST /start` | `{"mode": "demo"\|"live"}` — starts a run on a background thread |
| `GET /status` | full state as JSON (the page polls this every 500 ms) |
| `GET /config` | brand, agents, engine, thresholds, universe counts, schedule |
| `GET /scheduler` | next scheduled run and the last one fired |
| `GET /scoreboard` | the desk's own record: open signals, settled outcomes, hit rates with confidence intervals, and confidence calibration |

### Audit

Every run is written to `signals.db`:

- `runs` — mode, engine, universe size, shortlist size, BUY count, top pick,
  Telegram messages sent, status
- `verdicts` — **one row per stock per horizon** (`track` = `intraday` or
  `positional`), with scores, rationale, holding window and its basis, the
  trigger/invalidation/objective levels, market phase, whether it fired, the
  engine that produced it, the count of unverified figures, the named data
  gaps, and the **full evidence bundle** it was judged on

```bash
sqlite3 signals.db "select symbol, track, verdict, confidence, horizon, fired from verdicts order by id desc limit 20;"
```

---

## Limits worth knowing

- yfinance is an unofficial, best-effort feed. Fields go missing without
  warning — that is what `data_gaps` is for, and both engines are built to
  argue around holes rather than crash.
- Live mode outside market hours returns the last close, not a live tape.
- News sentiment is a small keyword lexicon over headlines, not a language
  model. It produces a count, and the agents may only cite it as a count.
- The LLM panel is a debate, not a forecast. Confidence is the panel's own
  conviction, and nothing more.
- A holding window says how long a thesis plausibly needs, not that it works.
  Nothing here estimates a probability of profit, because nothing in this feed
  supports one.
- Intraday verdicts go stale in minutes. A BUY read at 10:15 is a statement
  about 10:15.
- The track record is a record, not a forecast. The app now enforces this
  rather than relying on you to remember it: no rate is quoted under eight
  settled signals, and everything up to twenty carries an interval and an
  "indicative" flag.
- Outcomes are resolved on daily bars, so a level touched intraday and
  reversed still counts as touched. That is deliberate but it is not the same
  as a fill.
- No order is ever placed. There is no broker integration and no code path that
  could create one.
