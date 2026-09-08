# Dalal Desk interaction contract

The visual system is documented in DESIGN.md. Existing backend research, paper-account and Telegram behavior remains authoritative.

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Select/Listbox | Native HTML select, shared_styles.css | Existing two-mode API; DESIGN.md | Platform-owned menu for data mode and result sort | Browser selection, keyboard and narrow viewport |
| Form | Dashboard start controls | POST /start API | Optional positive finite paper capital, blank uses backend default | tests/test_dashboard_behavior.cjs |
| Scrollbar | shared_styles.css | DESIGN.md | Horizontal scrolling within strategy tables; natural document scrolling | Computed browser styles, narrow viewport |
| Toast | shared deskLoad and dashboard banner | shared_desk.js; dashboard.html | Inline status, error with retry, critical caveat | tests/shared_request.test.cjs and browser failure checks |
| Search | deskSearch; dashboard-specific combined verdict filters | URL q, buys, sort | Local immediate filtering; URL-restorable state | Browser and dashboard behavior tests |
| Navigation | shared_shell.html and shared_desk.js | Existing four server routes | Sidebar becomes a four-item navigation bar below 900px | Browser all routes and narrow viewport |

## State and data behavior

- Read-only requests check HTTP status and JSON validity. Stale responses never overwrite newer requests. Loading and errors keep existing research visible.
- Configuration/status/start calls have a 20-second deadline. IPO and intraday research allow 180 seconds to accommodate existing filing parsing and live scans. Shared retries show their pending state.
- The overview polls after completion, every second while running and every ten seconds while idle, with bounded error backoff. Hidden tabs do not schedule new polls; visibility or browser reconnection requests fresh state.
- The intraday desk uses the `/intraday` scan lifecycle: the initial GET checks or starts the cached scan; Refresh data requests `/intraday?refresh=1`. It polls running scans every three seconds without overlapping requests, pauses when hidden, and rechecks on visibility or reconnection. Its page-owned polling controller uses the shared `deskRequest` transport and shared banner/control styles. Errors retain the last research snapshot, withdraw entry-ready presentation, and offer Retry loading; they do not restart scans automatically.
- Starting analysis is explicit. Block duplicate submissions, disable configuration while running, validate paper capital in text and keep user input on failure. Never automatically repeat POST /start after an ambiguous response.
- Demo/live semantics, monetary calculations, statistical caveats, blockers and Telegram delivery remain server-owned. Visual QA only uses the isolated fixture server.
- Missing numbers display a dash. Missing evidence is not converted to a zero or a validated result. Every verdict uses a word as well as a color.

## Lists and discovery

The overview shows all verdicts by default, with the existing saved buy-only preference respected. Search matches stock symbols/company names; sort is newest, confidence, or symbol. Rendering is bounded to 12 matches and an explicit load-more action. Clearing resets filters immediately and returns focus to search. Composing text is never committed mid-IME. URL parameters preserve API routing and unrelated query state.

Quality and IPO data are already server-bounded research snapshots. Their lists use immediate local search with an explicit clear button, a live result count, and separate no-match vs empty-dataset messages. Supporting methodology is a native disclosure; consequential caveats remain visible. Strategy tables use native table semantics, horizontal scrolling with an accessible name, and no clipped columns.

The intraday desk separates server-qualified entry-ready picks from research candidates and session history. BUY/SELL direction, signal and confirmation times, price observation time, expiry, entry, stop, target and remaining reward:risk are visible together. Expired picks leave the ready list locally even between server requests. Candidate and history lists initially render twelve entries with an explicit Load more action; new entries receive focus after expansion. These transient snapshot limits do not persist in the URL. Evidence records distinguish modeled net results from unverified legacy gross results; `enough` or `trusted` flags alone never qualify a pick. Qualification and trading eligibility remain owned by the intraday backend and its validation record.

## Accessibility and environment

English interface, Indian monetary grouping, Asia/Kolkata display dates. Native buttons, links, inputs, selects and details retain platform keyboard behavior. Focus indicators are visible. Loading/error feedback has live-region semantics. The remembered theme is applied before first paint, defaulting to light for a new visit. Reduced motion removes animation; forced-colors keeps native scrollbars and selected-state outlines.

The app owns no authentication, money movement, destructive CRUD, uploads, or authored popup geometry. Those capabilities are not introduced by this redesign.
