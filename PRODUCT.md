# Dalal Desk

<!-- impeccable:product-schema 1 -->

## Platform

web

## Product Purpose

A research dashboard for Indian equities, with an eight-agent evidence panel, intraday strategies, a fundamental quality screen, and an IPO research desk. Product facts below come from README.md and the existing API implementation. The current task authorizes a complete UI/UX redesign; it does not change analytical rules.

## Users

The existing workflow serves people researching Indian stocks before and during the NSE session. Research is the inferred primary job; this is not a broker or execution platform.

## Operating Context

Four self-contained HTML pages are served by Flask or generated into web/ for Vercel. The backend may run separately. Demo bundles are illustrative. Live requests depend on external data providers and may be slow or unavailable.

## Capabilities and Constraints

- Start a demo or live analysis with optional paper capital; inspect agents, verdicts, risk gates, track record, sectors and logs.
- Inspect strategy results, quality-screen matches and IPO evidence through three dedicated desks.
- Telegram delivery is an existing backend side effect when configured. The redesign must not start a run automatically.
- No real orders are placed. Paper-account state, scoring rules, regulatory copy and missing-data semantics are preserved.
- Missing values are shown as a dash and explained. Never invent a live price, success rate or validated edge.

## Brand Commitments

Dalal Desk. Plain English, Indian currency formatting and Asia/Kolkata timestamps. The user requests a modern, smoother, faster experience across the site.

## Evidence on Hand

README.md, app.py, demo_data/, intraday_desk.py, quality_screen.py, ipo.py and the four existing pages. QA fixtures are explicitly illustrative and never shipped as live API data.
