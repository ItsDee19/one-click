# Intraday Desk audit — 8 September 2026

Original reviewed implementation: commit `43f2e84`. The findings below describe
that baseline. The follow-up implementation repairs the code paths listed here;
the saved legacy backtest remains frozen and unverified.

Implementation status (8 September 2026): causal per-bar RVOL, strict timestamped
closed sessions, next-open cost-aware simulation, direction-specific evidence
admission, current setup lifecycle, explicit BUY/SELL, conflict/deduplication and
sector limits, dated execution eligibility, asynchronous full-stock scanning,
immutable forward observations, and stale/error UI handling are implemented.
Regression checks cover these paths. See `INTRADAY-HISTORY.md` for operation.

**Evidence still required:** longer authoritative intraday archives, historical
membership and broker eligibility, a preregistered untouched holdout, replay of
the actual selector/execution/exposure policy, and forward paper verification.
These cannot be created by changing code or relabelling the old results. The
repaired application blocks qualified picks until the evidence gates pass.

The desired output is a small ranked set of current BUY/long and SELL/short
opportunities for the day, with executable conditions and credible evidence.
An exit from an existing holding is a separate action from opening a short.

## Finding

The current desk displays triggered patterns, including patterns whose simulated
trade has already ended. Its saved performance is insufficient to call any rule
proven. Several issues affect both live opportunity selection and historical
measurement. Expanding stock coverage does not correct these issues.

## What the saved record actually says

Source: `backtest_intraday.json`, generated 4 September 2026 at 09:12 IST.
It reports 164 requested symbols, 9,676 stock-sessions and a `60d of 5m bars`
window. These are gross simulated results, not independently validated live returns.
R denotes the initial entry-to-stop risk per share.

| Strategy | Direction | Trades | Win rate | Gross mean R | Profit factor | Audit interpretation |
|---|---|---:|---:|---:|---:|---|
| RVOL momentum | Long | 684 | 56.1% | +0.475 | 2.23 | Retest after eliminating future-volume leakage |
| ORB breakout | Long | 1,433 | 57.4% | +0.334 | 2.15 | Retest after eliminating future-volume leakage |
| VWAP rejection | Short | 6,609 | 37.9% | +0.050 | 1.08 | Small gross edge; costs and holdout may erase it |
| VWAP reclaim | Long | 8,608 | 36.2% | 0.000 | 1.00 | No measured gross edge |
| Gap and go | Long | 331 | 44.7% | -0.025 | 0.93 | Negative even before costs |
| VWAP reversion | Long | 177 | 45.8% | -0.040 | 0.80 | Negative even before costs |
| ORB breakdown | Short | 1,145 | 44.5% | -0.047 | 0.89 | Current short rule lacks positive evidence |

No strategy should be promoted on these numbers alone. The artifact lacks actual
observation dates, successful/failed symbols and a trade ledger. The reported
9,676/164 = 59 sessions per stock also needs reconciliation with a 60-calendar-day
request; the metadata cannot substantiate the claimed trading-session coverage.

## Critical gaps

1. **Future volume determines past signals.** `strategies.session()` computes
   RVOL using the entire session before `orb_breakout`, `orb_breakdown` and
   `momentum_rvol` inspect early entry bars. Consequently, activity later in the
   day changes whether an earlier trade supposedly existed. Live RVOL is a
   different calculation and can even come from yesterday's daily candle.
   Missing RVOL currently bypasses the ORB participation condition.

2. **Completed trades remain proposed opportunities.** Each strategy returns its
   first trigger that day. `strategies.evaluate()` simulates subsequent stop and
   target touches, but `intraday_desk.scan()` discards that outcome. The displayed
   timestamp is the latest bar, not the signal time. No expiry, entry-drift or
   remaining reward/risk check distinguishes a current entry from a missed trade.

3. **Direction is lost.** Strategy setups contain `direction=long|short`, but the
   Intraday Desk's pick object and card omit explicit BUY/SELL direction. A short
   setup can be displayed in the same form as a long setup.

4. **Prices do not establish executable fills.** The historical simulator enters
   at the signal candle close and assumes perfect stop/target execution. It omits
   transaction charges, spread, slippage, market impact and losses through gaps.
   Checking the stop first on an ambiguous candle is appropriate, but insufficient.

5. **The opening range can be wrong.** The desk removes timestamps when converting
   a frame to bars. Three surviving bars are treated as the opening fifteen minutes
   even if the download starts late or has gaps. A forming five-minute candle is
   not required to finish before it can trigger a close-based strategy.

6. **The trust label does not mean validation.** `enough` means only twenty trades;
   negative-expectancy rules are also marked trusted. Confidence is a formula based
   on pooled gross expectancy, not a calibrated chance of success. Records have no
   strategy fingerprint, cost-model version, untouched holdout or uncertainty bound.
   The main engine's `strategy_edge.py` also consumes these records.

7. **Ranking is not stock-specific.** Stocks using the same strategy receive the
   same expectancy/confidence. Ties preserve universe order, and one ticker can
   consume multiple slots or carry opposite signals. Zero expectancy incorrectly
   sorts as `-99` because of `value or -99`. Current spread, liquidity, entry drift,
   relative strength and remaining reward/risk are absent from the ranking.

8. **The published picks are not the tested portfolio.** The backtest pools all
   stock/strategy triggers. It does not replay the daily top-N ranking, overlapping
   trades, capital constraints or sector concentration. Thousands of trades on the
   same dates are not thousands of independent market observations.

9. **Coverage and eligibility are different.** Research should continue to cover
   all discovered stocks. Actionable intraday picks additionally need exchange
   series, liquidity and broker eligibility checks, including short availability.
   Unknown eligibility must be explicit; a bearish pattern is not confirmation
   that a particular account can execute a short.

10. **Refresh architecture is too slow for the objective.** A synchronous request
    downloads two years of daily history across the exchange before current-session
    bars. It can lose early opportunities while scanning later names. Static daily
    references should be cached; current-session data and candidate revalidation
    should update separately, with progress and snapshot age visible.

## Reproductions executed during the audit

Two synthetic checks ran against the actual strategy and desk functions, with
provider calls mocked and no orders or messages:

- Kept the first four OHLCV bars identical. At that point RVOL was 0.4 and no ORB
  was emitted. Adding later high-volume bars changed RVOL to 20.4 and retroactively
  created an ORB entry at bar 3. This confirms future-data leakage.
- That ORB entered at 102, stopped at 99 on the following bar, and still appeared
  in the desk's picks with a newer 09:40 timestamp. The pick omitted direction.
  A deliberately negative record (-0.1R, profit factor 0.9) was still `trusted=true`.

An independent replay also confirmed that entry 100/stop 99 followed by a bar
opening 98.50 with high 98.80 is scored as an exit at 99. The simulator understates
that gap loss.

## Recommended output and decision policy

The primary lists should be **Best BUY candidates** and **Best SELL/short
candidates**, with no requirement to fill either list. Three to five qualified,
distinct names in total is a product default to test, not a validated optimum.

Each row needs direction, current price and timestamp, signal time, strategy
version, entry trigger/acceptable range, stop, target, current net reward/risk,
expiry and square-off time, reason for the ranking, liquidity/eligibility status,
and validation evidence. Show only one resolved directional view per stock;
conflicting signals become WATCH until resolved by a tested policy.

Separate states should be `watching`, `entry_ready`, `missed`, `expired`,
`stop_hit`, `target_hit`, `restricted`, and `data_unavailable`. A fresh quote must
not refresh the age of an old signal. Completed and research-only setups can
remain inspectable without entering the actionable picks list.

Admission should require a complete, closed-bar session; causal participation;
acceptable execution conditions; a valid current entry; and strategy evidence
appropriate to the direction and eligible stock universe. Rank admitted stocks
with a documented, historically replayed policy using evidence strength and
current execution quality. Market/sector alignment and abnormal volume are
candidate features to test, not assumed improvements.

## Strategy research priorities

First retest a clearly specified ORB continuation family with causal opening
relative volume and VWAP context. Test long and short independently. Keep RVOL
momentum as a comparison to establish whether the breakout rule adds value beyond
participation. Test VWAP rejection separately as a short-side hypothesis. The
saved negative or flat rules should not enter a qualified list on their current
record, and inverting a losing rule requires an independent test too.

The cited [authors' ORB study](https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/)
examines US stocks and emphasizes stocks with unusually high activity. The
repository's entry, range, stop and exit rules differ. This is a basis for
research; it is not proof of the NSE implementation or its short-side performance.

## What would justify promotion beyond research

Use `unverified`, `research_only`, `historically_validated`, and `forward_confirmed`
labels. Avoid a blanket `proven` label: historical validation cannot establish
future profitability. A proposed admission policy should be fixed before running
the final evaluation:

- Share causal signal code between live and backtest paths. Preserve timestamps,
  exact session checks, prior-only volume history and complete-bar decisions.
- Use the next executable bar's open after confirmation, adverse fill assumptions,
  gap-aware stops, end-of-day exit, and a versioned cost model. Include the applicable
  [NSE levies](https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies)
  and broker-specific costs; stress-test higher friction.
- Acquire an adequately long, point-in-time intraday archive spanning differing
  regimes and relevant liquidity groups. Twelve to twenty-four months is a proposed
  research target, not a sufficient statistical guarantee. The current
  [yfinance intraday history limit](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)
  does not supply that archive.
- Keep chronological development and untouched evaluation periods, account for
  strategies/parameters tried, and record the exact universe and strategy version.
  Never randomly distribute same-day stock trades between training and holdout.
- Report net expectancy, profit factor, drawdown, payoff distribution, cost
  sensitivity and confidence intervals resampled by trading date. Proposed initial
  floors of 100 holdout trades and 60 distinct holdout dates are engineering gates,
  not statistical proof. Require a positive lower uncertainty bound for net edge
  under the declared selection policy, not just a positive pooled average.
- Replay the exact top-N daily selection and exposure limits before reporting
  best-pick performance. Keep per-direction and market-regime breakdowns.
- Retain the trade ledger, including decision, entry and exit times; fills; gross
  and net R; costs; reasons; missing-data exclusions; and requested/successful stock
  counts. Then compare untouched forward-paper outcomes with the backtest over a
  predeclared window, initially 30-60 sessions or longer where signals are sparse.

Short eligibility must follow the current exchange/broker configuration and
[SEBI's short-selling framework](https://www.sebi.gov.in/legal/circulars/jan-2024/framework-for-short-selling_80448.html),
with unknown eligibility kept separate from a bearish research signal.

## Implementation order

1. Correct signal timing, session validation, direction, trade lifecycle, causal
   RVOL and confidence labels. Add regressions reproducing the confirmed failures.
2. Rebuild the cost-aware replay and retained ledger; version and invalidate legacy
   validation records; obtain the missing historical data and retest hypotheses.
3. Add deduplicated BUY/SELL ranking, eligibility/rejection reasons, background
   incremental scans and forward-paper tracking. Validate the selection policy
   itself before presenting a best-picks performance claim.
