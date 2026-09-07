---
version: alpha
name: Dalal Desk
description: A calm research workspace for Indian equities, with clear evidence, useful context and deliberate action.
colors:
  surface: "#f6f7fb"
  surface-container: "#ffffff"
  surface-container-low: "#f8f9fc"
  surface-container-high: "#f0f2f8"
  on-surface: "#222538"
  on-surface-variant: "#4a5066"
  on-surface-muted: "#626a80"
  on-surface-faint: "#697187"
  outline: "#cdd1df"
  outline-variant: "#e5e7ef"
  primary: "#635bce"
  on-primary: "#ffffff"
  primary-container: "#eeedfb"
  buy: "#18704f"
  buy-container: "#e8f5ee"
  watch: "#8a5a12"
  watch-container: "#fcf3e3"
  avoid: "#b33f49"
  avoid-container: "#fcecef"
typography:
  headline-md:
    fontFamily: "Segoe UI Variable Display, Segoe UI, sans-serif"
    fontSize: 30px
    fontWeight: "650"
    lineHeight: 1.2
    letterSpacing: -0.025em
  title-sm:
    fontFamily: "Segoe UI Variable Display, Segoe UI, sans-serif"
    fontSize: 17px
    fontWeight: "650"
  body-md:
    fontFamily: "Segoe UI, -apple-system, BlinkMacSystemFont, Helvetica Neue, sans-serif"
    fontSize: 14px
    fontWeight: "400"
    lineHeight: 1.55
  label-sm:
    fontFamily: "Segoe UI, sans-serif"
    fontSize: 12px
    fontWeight: "550"
  metric-lg:
    fontFamily: "Segoe UI Variable Display, Segoe UI, sans-serif"
    fontSize: 29px
    fontWeight: "650"
    lineHeight: 1.2
  mono-sm:
    fontFamily: "ui-monospace, Consolas, monospace"
    fontSize: 12px
    lineHeight: 1.7
spacing:
  sm: 8px
  md: 16px
  lg: 24px
rounded:
  control: 8px
  card: 14px
components:
  card:
    backgroundColor: "{colors.surface-container}"
    textColor: "{colors.on-surface}"
    rounded: "{rounded.card}"
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.control}"
  badge:
    typography: "{typography.label-sm}"
    rounded: 6px
  badge-buy:
    backgroundColor: "{colors.buy-container}"
    textColor: "{colors.buy}"
  badge-watch:
    backgroundColor: "{colors.watch-container}"
    textColor: "{colors.watch}"
  badge-avoid:
    backgroundColor: "{colors.avoid-container}"
    textColor: "{colors.avoid}"
  label:
    textColor: "{colors.on-surface-muted}"
    typography: "{typography.label-sm}"
  caption:
    textColor: "{colors.on-surface-faint}"
---

# Dalal Desk

## Overview

This is an operating workspace for equity research. The user-authorized full redesign replaces the previous dark cyan terminal with a quiet, light research notebook: persistent navigation, a clear analysis toolbar, readable numbers and an evidence-first page order. The signature is the relationship between verdicts and their research conditions: conclusions occupy the main column while the track record, confidence, paper capital and the agent panel remain beside them.

The product must never imply execution, guaranteed returns or validated research where evidence is missing. Read PRODUCT.md for product facts and UX-CONTRACT.md for observable behavior.

## Colors

Runtime CSS is canonical (Model B). shared_styles.css defines all shared tokens and both themes. This file mirrors the accepted light values; page-specific CSS consumes variables instead of maintaining separate palettes. page_render.py inlines shared styles into both Flask responses and build_web.py outputs.

| Documentation role | Runtime variable | Consumers |
|---|---|---|
| surface / containers | --bg, --card, --card-low, --card-high | App canvas, cards, tables |
| on-surface / variant / muted / faint | --ink, --ink-2, --muted, --faint | Headings, evidence, supporting text |
| primary / on-primary / container | --accent, --on-accent, --accent-soft | Actions, active desk, top pick |
| buy / watch / avoid | --green, --amber, --red and -soft variants | Written verdicts, critical caveats |
| outline / outline-variant | --border-strong / --border | Inputs / structural borders |
| spacing sm / md / lg | --space-sm / --space-md / --space-lg | Shared rhythm: 8 / 16 / 24 px |
| rounded control / card | --radius-control / --radius | Controls / panels |

Light is the new-visit default for the daytime research workflow. Previously saved light/dark preferences are respected. Dark mode uses slate surfaces (#141720 canvas, #1e2330 cards), lavender action (#b2a9ff), pale content (#f0f1fa), and corresponding readable verdict colors. The semantic hierarchy does not change between themes. The theme applies inline before first paint.

No gradients, glass, grain or ambient effects. Color means action, navigation, a verdict or a genuine warning. Missing Telegram delivery is neutral context; data failure and concentration warnings remain prominent.

## Typography

The display and body roles use the platform Segoe/system families deliberately: an offline-capable research tool should not fetch a font before it can render its data. Display uses the variable display face where installed and equivalent native fallbacks elsewhere. Metrics use tabular numerals. Logs alone use monospace. There are no remote font requests or font-swap layout shifts.

Page headings are 30px, section headings 17px, body/evidence 12–14px, and supporting labels 10–12px. Narrow headings become 27px. Avoid decorative uppercase tracking. All important prices retain their currency and Indian grouping; unknown values remain a dash.

## Layout

A 220px sidebar (194px at compact laptop sizes), a quiet 78px context bar, and a naturally scrolling content area. The content has 36px desktop margins, 24px tablet margins and 16px phone margins. At 900px the sidebar becomes four visible navigation items above the content, avoiding a hidden menu for four destinations.

Overview order: page identity and session; explicit start controls; analysis summary; verdict search/results plus research context; optional run details; additional sector and order-book evidence. There is one primary action, Start analysis. The result feed renders at most twelve matches before Load more.

The main split uses a flexible verdict column and a 304px context column. It becomes a single column at 1000px. Two horizons sit side by side when readable and stack at compact widths. Mobile metrics retain a two-column scan pattern.

Secondary desks use the same navigation, titles, controls and surface language. They promote current setups, screening criteria/matches, and the issue calendar respectively. Methodology uses native disclosures, while critical evidence limitations remain visible. Strategy tables scroll within their own region; the page does not hide overflow or trap document scrolling.

## Elevation & Depth

Panels use a single subtle border, not shadows. The app canvas and white/slate cards provide separation. The active desk and top research pick use the accent container. Focus uses a visible 3px outline; it is not decorative elevation.

## Shapes

14px cards, 8px controls, 6px verdict badges, and small circular status dots. Icons share a 1.7–1.8px outline vocabulary. No emoji or pictorial SVG illustrations.

## Components

Shared shell: shared_shell.html. Tokens, controls, panels, status, search and responsive rules: shared_styles.css. Theme, checked requests, shared loading/retry, local list search and money formatting: shared_desk.js. Inline page scripts own only the research-specific renderers and the overview's polling/combined filter state.

Native select popups are explicitly platform-owned. Dates are formatted using en-IN and Asia/Kolkata. No authored menu, calendar or modal is required by these workflows.

Animations communicate actual work: a small pending spinner and a brief changed-value color. No entrance choreography, hover lift or pulsing idle decoration. Reduced motion removes animations and transitions. The global scrollbar baseline applies to all owned scroll regions, has visible hover/active colors, and falls back under forced colors.

## Do's and Don'ts

- Keep evidence, risk gates, unavailable data and all original analytical safeguards intact.
- Use text and color together for verdicts. Never style an unavailable metric as zero.
- Keep results visible on a failed refresh, show retry in place and prevent duplicate starts.
- Preserve navigation and main controls before any backend is available.
- Do not add remote assets, a framework or a build dependency for visual effects.
- Do not move backend setup details into the main user flow; keep them with engine/run details and documentation.

## Redesign reconciliation

| Previous system | Authorized change | Implementation |
|---|---|---|
| Dark cyan terminal and duplicated tokens | Light-first lavender research workspace with remembered dark mode | One shared_styles.css |
| Desks and start controls mixed in the header | Persistent desk navigation plus explicit scan toolbar | Shared shell and overview scan panel |
| Agent board before research results | Verdicts lead, context and agents alongside | Overview workspace grid |
| Repeated animation and 500ms continuous polling | Pending-only motion; 1s running / 10s idle; hidden-tab pause | Shared CSS and dashboard polling |
| Repeated styles and no durable behavioral contract | Shared primitives with documented owners | UX-CONTRACT.md and page_render.py |
