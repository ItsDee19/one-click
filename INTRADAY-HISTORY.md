# Intraday research records and historical archives

The repaired replay produces **research evidence**, not a promise of profitable
daily picks. The old `backtest_intraday.json` is retained as a frozen, invalidated
gross-only artifact. New output defaults to `backtest_intraday_v2.json`.

```sh
python backtest_intraday.py
python backtest_intraday.py --symbols RELIANCE,TCS
python backtest_intraday.py --data-dir /path/to/archive --output backtest_intraday_v2.json
```

The first command discovers the current full NSE main-board/SME universe. A
discovery fallback or provider failure is recorded; a requested symbol with no
history is counted as unavailable, never as successfully tested. A live default
run makes one history request per discovered ticker and can take a long time.
`--symbols` is an explicit subset. With `--data-dir`, every supplied ticker file
is studied without network discovery, unless `--symbols` narrows that archive.

## Archive contract

Use one file per ticker: `RELIANCE.NS.csv`, `TCS.NS.json`, and so on. Bare NSE
symbols such as `RELIANCE.csv` are normalized to `.NS`. Duplicate JSON/CSV sources
for the same ticker are rejected. Keep replay outputs outside the archive folder.
An optional `metadata.json` is reserved and is not treated as a ticker.

CSV columns (case-insensitive):

```csv
timestamp,open,high,low,close,volume
2026-06-01T09:15:00+05:30,1400,1405,1398,1403,120000
2026-06-01T09:20:00+05:30,1403,1406,1400,1401,100000
```

JSON contains either a list of those same objects or `{"bars": [...]}`. Prices
are INR per share; volume is traded shares, not turnover or cumulative daily
volume. Timestamps denote each five-minute candle's **opening time** and must
include an explicit UTC offset. UTC timestamps are converted to IST. Naive
timestamps are rejected by the archive loader.

A regular NSE session must contain all 75 contiguous candles opening at 09:15
through 15:25 IST. Out-of-order records are sorted, but duplicates, missing
candles, invalid OHLC values, negative volume and incomplete sessions cause that
date to be excluded. No candles are interpolated. Special shorter sessions need
an explicit calendar/session extension; they are not silently accepted as regular
sessions. Use exchange-authorized or broker-provided historical exports with
source documentation, adjustment policy and historical instrument identities.
Unadjusted executable OHLCV is expected; inconsistent corporate-action adjustment
can corrupt gaps and price levels and needs a separate archive-quality review.

The first twenty **complete prior sessions** warm up each stock. They cannot
generate scored trades. Current and future session volume never enter the
reference average. The default relative-volume denominator is the prior twenty
complete-day average, scaled by elapsed five-minute bars, matching the live
desk's causal approximation. It is not a measured time-of-day volume profile.

## What is retained

Version 2 records include:

- Requested, successfully evaluated and unavailable symbol counts, actual date
  bounds, warmup and incomplete-date counts, errors and source identity. Local
  source files include SHA-256 hashes.
- Strategy/execution fingerprint, validation/selection policy hash, explicit
  round-trip fee allowance and adverse slippage per fill.
- A full trade ledger with BUY/long or SELL/short direction, signal and
  confirmation timestamps, planned levels, next-open entry and exit fills,
  exit-time precision, gross R, costs R, net R and doubled-friction net R.
- One global chronological development/evaluation split across all stocks, with
  one entire trading date embargoed between the two periods. Warmup uses earlier
  sessions; later prices never influence historical decisions.
- Separate long/short rule summaries, distinct evaluation dates, net expectancy,
  profit factor, chronological drawdown in R and a deterministic 95% confidence
  interval resampled by trading date. Thousands of same-day stock trades are
  not treated as thousands of independent market dates.
- Shared-rule ranking diagnostics at each actual signal confirmation timestamp.
  These diagnostics never report selected-portfolio returns: current-entry
  lifecycle, historical broker eligibility, spreads, capital and cross-snapshot
  exposures have not been replayed from an OHLCV-only file.

Fees default to an assumed total 10 bps on the mean entry/exit notional, plus
5 bps adverse slippage on each fill. These are explicit research allowances,
not an exact brokerage/tax calculation or a guarantee of conservative real-world
fills. The stress case doubles both. Stops through gaps use the worse opening
price. Ambiguous candles touching both stop and target take the stop. Missing or
invalid next-bar fills are retained as signal exclusions, not winning trades.

## Promotion gates

`intraday_validation.assess_record()` is the common admission boundary. Legacy
gross records, mismatched strategy/policy/cost versions, missing direction and
stale records cannot qualify. Even a schema-correct positive result remains
`research_only` until the following evidence is substantiated:

- At least 100 out-of-sample trades and 60 distinct out-of-sample dates for the
  particular rule and direction, with positive net expectancy and a positive
  lower day-cluster confidence bound. These floors are engineering gates, not
  proof; multiple trials and regime sensitivity still require research review.
- A protocol fixed before the holdout, evidence it remained untouched, and
  nonoverlapping chronological dates. Merely generating a 70/30 split does not
  establish that the evaluation was untouched.
- Historical point-in-time universe membership and its evidence. Discovering
  today's stocks or loading today's supplied files cannot establish membership
  and eligibility at historical decision times.
- The exact current selector, execution eligibility and exposure policy replayed
  at event time, with its own direction-specific net out-of-sample ledger and
  uncertainty gates. Ranking snapshots are not that portfolio replay.

This CLI deliberately sets those unsubstantiated attestations to false. There is
no switch that turns research into validation. Longer authoritative archives,
historical membership/eligibility evidence, a frozen protocol and a full event
replay must be added and reviewed before promotion. Forward paper results should
then be compared with the historical assumptions before relying on daily picks.
Historical validation never establishes future profitability.

## Live desk operation

`GET /intraday` starts one background scan on first use and returns a status
snapshot immediately. Polls reuse it. `GET /intraday?refresh=1` requests a new
scan; simultaneous requests share the running scan. A new date or universe
configuration starts a new scan. Daily references use three months of daily
data, cache only prior-session statistics per date/universe, and retry missing
symbols. Every discovered stock is requested for session research. Coverage
separately counts missing, invalid, stale and usable sessions.

Qualified cards require verified main-board EQ series, twenty prior sessions
with average turnover of at least INR 1 crore, a fresh prior daily reference,
current dated broker eligibility and the validation gates above. Eligibility
is optional input; its absence produces research candidates, never assumed
permissions. Set `INTRADAY_ELIGIBILITY_FILE` to a reviewed export in this format:

```json
{"symbols":{"EXAMPLE.NS":{"as_of":"2026-09-08","source":"Your broker export reference","long":true,"short":false}}}
```

Use actual permissions and the current session date. This file is not a broker
connection and does not place an order. SELL means a proposed short entry; it
does not mean an instruction to exit an existing holding.

Signals retain their original confirmation time. Entries expire after ten
minutes or at 14:15 IST. Stop/target touches, stale completed-bar observations,
entry drift above 0.25R and estimated net reward/risk below 1.5 remove a setup
from current candidates. Current entry is an estimate from the last completed
five-minute close with adverse slippage; a live executable quote, spread and
depth are still necessary for actual execution. Opposing signals for one stock
produce no directional pick. Duplicate stocks are removed and each snapshot
allows at most five qualified picks and two per sector; research candidates
have separate display limits and cannot consume qualified slots. These limits
are snapshot selection rules, not a capital-aware trading portfolio.

Every response and the browser recheck expiry. Refresh failures clear qualified
cards and retain dated context. The background scan writes an observation
journal to `DB_DIR/intraday_observations.db`, retaining the immutable first-seen
payload and latest observed state for each signal. These are observations, not
executed trades, fills, settled paper returns or proof. Cache files live in
`DB_DIR/.intraday_cache`; without `DB_DIR`, both use the application directory.

Deployment does not automatically run a history replay, validate strategies,
connect a broker, or send notifications. With the existing legacy record the
expected result is **no qualified picks**, with clearly labelled research where
current data supports it.
