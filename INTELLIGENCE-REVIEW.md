# Intelligence engine review

The engine previously studied a curated universe and researched only a few
shortlisted stocks. Its optional exchange scan filtered by turnover and called
turnover groups large/mid/small caps. Company accounting ratios were absent from
the main debate, and some missing evidence or unsupported LLM figures could
still accompany a BUY.

The new pipeline separates exchange discovery, evidence collection, deterministic
analysis, and LLM explanation. Every discovered stock receives an auditable
record; no-price failures and incomplete research remain searchable. NSE
main-board and SME discovery are default, with real provenance, timestamps,
deduplication and cache/fallback state. Bounded downloads and per-section company
caches support thousands of symbols without unlimited parallel provider calls.

Company ratios, growth, profitability and debt fields augment analyst opinions.
Two-year daily histories support the existing long-window strategy rules across
the universe. Short technical windows remain labelled separately. The pipeline
checks aligned OHLCV rows, explicit missing values and actual observation times.
Freshness, valid risk levels and deterministic confirmation constrain model
BUYs. Unsupported numerical claims and malformed model confidence cannot silently
become actionable signals. All-stock rule analyses are available separately from
the overview's bounded LLM debate.

The highest-value next improvements require additional evidence, rather than
more aggressive scoring:

- Add a reliable BSE security master and symbol/ISIN mappings for BSE-only stocks.
  Current exchange scope is explicitly limited to NSE equity trading lists.
- Add filing-level financial statements, announcement dates and corporate-action
  validation from a supported exchange or licensed provider. Current company
  ratios are secondary-provider observations, not independently audited filings.
- Validate candidate scoring changes with walk-forward, point-in-time datasets,
  costs, liquidity and delisted securities. Existing backtests do not establish
  improved profitability for this expanded universe. Do not reinterpret heuristic
  conviction or data completeness as a probability of success.
- Measure news relevance, event time and source reliability beyond the existing
  sanitised keyword tone counts. Numeric grounding cannot prove the correctness
  of every semantic claim in a model explanation.

The scope comes from [NSE's official trading-list page](https://www.nseindia.com/static/market-data/securities-available-for-trading).
Download batching and timeouts use the documented [yfinance download interface](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html).
Provider coverage and availability still determine how many discovered stocks
have usable evidence; the implementation records that distinction explicitly.

Validation on 8 September 2026: offline regression tests cover discovery outages,
all-stock persistence, profile-cache failures, timestamp checks, malformed model
output, pagination and CSV export. A live two-stock smoke run retrieved daily and
intraday bars, company profiles and news, and stored both analyses successfully
in a temporary database. NSE's main-board CSV responded; the SME endpoint timed
out. A complete live exchange-wide research run has not been verified here.
