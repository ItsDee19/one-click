# UI redesign verification

## Automated checks

`python -B -m unittest discover -s tests -p 'test_*.py' -v` passes all 11 checks after `python build_web.py`. The suite runs the two Node VM tests when Node is available. No external requests or application imports are involved.

Coverage includes all four generated pages, shared-component expansion, runtime/static parity, fresh committed output, one main/h1, unique IDs and valid label/ARIA targets, JavaScript syntax, inline-script escaping, HTTP/JSON failures, timeout/cancellation cleanup, safe error text, stale-request suppression and retry. Dashboard tests cover pagination, filters and URL state, same-length data updates, unchanged-data rendering, offline retention/recovery, valid paper capital, duplicate starts, polling frequency and hidden tabs.

## Browser checks

Verified with the isolated preview server and illustrative fixtures, in the Chromium-based Codex browser:

- Desktop and 390px phone layouts across Overview, Intraday, Quality and IPO; no horizontal document overflow. Strategy tables retain horizontal scrolling inside their own accessible regions.
- Remembered light/dark theme, selected desk navigation, native data-mode and sort controls, and visible keyboard focus.
- Overview stock search, no-match message, clear/reset, symbol sort, buy-only filtering and result counts.
- Quality/IPO local search, clear, counts, and Indian currency grouping.
- Invalid capital error, simulated start, disabled configuration while running, and completion with entered paper capital. No real backend or Telegram calls.
- Research request failure, retry and recovery; overview connection recovery; initial loading and empty-result presentation.
- No browser console errors in the final loaded overview. Static checks verify reduced-motion and no remote font/script requirements; OS-level reduced-motion and Safari/Firefox were not exercised.

## Static design tools

The official `designmd lint DESIGN.md` completed with zero errors and warnings.

The premium static audit was run in strict mode. Its remaining actionless-button findings are false positives: this auditor only recognizes inline `onclick`/Vue attributes and does not trace `addEventListener` bindings. The buttons are wired in page scripts/shared_desk.js and exercised by the runtime tests and browser checks. The machine-specific JSON is kept locally as `tests/premium-audit.json`; no clean strict-audit claim is made.

The Impeccable detector ran in degraded regex mode because its optional HTML parser modules were unavailable. It reported advisory type/radius values outside the abbreviated frontmatter scale. The component and responsive variants are documented in DESIGN.md; rendered layout and color use were checked in the browser. The detector was not used as proof of accessibility compliance.

## Performance and limits

Idle request frequency changes from 500ms to 10s (95% fewer scheduled idle status requests), with no new polls while hidden. Active polls run once per second after the previous response. Unchanged verdicts, sectors and order-book tables are not rebuilt. Rendering the overview is bounded to twelve matches until Load more. Font loads, decorative grain and perpetual visual effects were removed; the frontend still has no framework or remote script dependency.

Self-contained HTML remains intentional: the overview is approximately 21KB gzip and each secondary desk approximately 12KB gzip. The new shared shell and recovery code increase HTML bytes; improvements concern network polling, redundant rendering and dependency requests. No Lighthouse score or live backend speed improvement is claimed.

The existing same-origin API build default is preserved. Separate frontend deployments still require the established API configuration. Live market data, Telegram delivery and backend research/scoring were not exercised or changed by this UI validation.
