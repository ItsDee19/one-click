---
version: alpha
name: Dalal Desk
description: >
  A calm, dense research terminal for Indian equities. Optimised for reading
  numbers under time pressure and for being trusted when it says it does not
  know something.
colors:
  # --- surfaces, lowest to highest --------------------------------------
  surface: "#f5f6f8"
  surface-container-lowest: "#ffffff"
  surface-container-low: "#fcfcfd"
  surface-container: "#fafbfc"
  surface-container-high: "#f1f3f7"
  surface-container-highest: "#eceef2"
  # --- content on surfaces ------------------------------------------------
  on-surface: "#111520"
  on-surface-variant: "#3d4451"
  on-surface-muted: "#5d6573"
  on-surface-faint: "#646c7b"
  outline: "#d6d9e0"
  outline-variant: "#e5e7ec"
  # --- action -------------------------------------------------------------
  primary: "#2b62ee"
  on-primary: "#ffffff"
  primary-container: "#eef3ff"
  on-primary-container: "#1c3f9e"
  # --- verdict semantics --------------------------------------------------
  buy: "#0f7a4d"
  on-buy: "#ffffff"
  buy-container: "#e9f6ef"
  on-buy-container: "#0b5c3a"
  watch: "#94620a"
  on-watch: "#ffffff"
  watch-container: "#fdf3e0"
  on-watch-container: "#7a5108"
  avoid: "#b02a20"
  on-avoid: "#ffffff"
  avoid-container: "#fdeeec"
  on-avoid-container: "#8f231b"
  unavailable: "#646c7b"
  unavailable-container: "#f0f1f4"
typography:
  headline-md:
    fontFamily: Inter
    fontSize: 17px
    fontWeight: "650"
    lineHeight: 1.3
    letterSpacing: -0.01em
  title-sm:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: "640"
    lineHeight: 1.35
  metric-lg:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: "660"
    lineHeight: 1.15
    letterSpacing: -0.02em
    fontFeature: "'tnum' 1, 'zero' 1"
  metric-md:
    fontFamily: Inter
    fontSize: 17px
    fontWeight: "640"
    lineHeight: 1.2
    fontFeature: "'tnum' 1, 'zero' 1"
  body-md:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: "400"
    lineHeight: 1.45
  label-sm:
    fontFamily: Inter
    fontSize: 10.5px
    fontWeight: "650"
    lineHeight: 1.3
    letterSpacing: 0.06em
  mono-sm:
    fontFamily: ui-monospace
    fontSize: 12px
    fontWeight: "400"
    lineHeight: 1.65
    fontFeature: "'tnum' 1"
spacing:
  xs: 6px
  sm: 10px
  md: 14px
  lg: 18px
  xl: 26px
rounded:
  sm: 5px
  md: 10px
  lg: 14px
  full: 999px
components:
  card:
    background: "{colors.surface-container-lowest}"
    border: "{colors.outline-variant}"
    radius: "{rounded.lg}"
    padding: "{spacing.lg}"
  nested-panel:
    background: "{colors.surface-container-low}"
    border: "{colors.outline-variant}"
    radius: "{rounded.md}"
  metric:
    label: "{typography.label-sm}"
    labelColor: "{colors.on-surface-faint}"
    value: "{typography.metric-lg}"
    valueColor: "{colors.on-surface}"
  badge:
    typography: "{typography.label-sm}"
    radius: "{rounded.full}"
  log:
    typography: "{typography.mono-sm}"
    background: "{colors.surface-container-lowest}"
omitted:
  - section: Elevation & Depth
    reason: >
      Depth is carried by a single hairline border and one near-invisible
      shadow. A dashboard read for hours should not have layers competing for
      attention, so there is no elevation scale to define.
---

# Dalal Desk

## Brand & Style

This is a research terminal, not a trading app. Its job is to present evidence
and a verdict, make the reasoning inspectable, and be unambiguous about what it
does not know. Every visual decision follows from that.

The register is a quiet institutional desk: white cards on a soft grey field,
hairline borders, generous internal spacing, almost no colour except where
colour carries meaning. Nothing pulses, glows or animates unless it is
reporting live state.

The emotional target is **calm competence under time pressure**. A user opens
this at 09:00 with fifteen minutes before the bell. They need to find the
number they are looking for immediately and trust it. Excitement is the wrong
feeling for an interface that is often telling you to do nothing.

Two consequences worth stating up front, because they rule out most of what is
fashionable in dashboard design:

- **Legibility outranks atmosphere.** Glass, blur, translucency and gradient
  fills all reduce the contrast of text sitting on them. On a screen whose
  entire purpose is small numbers — ₹1,403.00, 2.6x, −1.87% — that trade is
  never worth making.
- **Density outranks drama.** Oversized display type is a poster technique. A
  verdict row here carries two verdicts, two confidences, six price levels and
  a rationale. Space spent on a 84px headline is space taken from evidence.

## Colors

The palette is deliberately small. Grey does the structural work; colour is
reserved almost entirely for **verdict semantics**, so that a spot of green or
red always means something specific.

- **Surfaces** run from the page field (`surface`) up through white cards
  (`surface-container-lowest`) to the tinted panels used for nested content
  (`surface-container-low`) and table headers (`surface-container`). The
  ordering matters: a track panel sits *inside* a verdict card, and the tiers
  keep that nesting readable without adding a second border weight.
- **Verdict colours** are the only saturated hues in normal use: green for BUY,
  amber for WATCH, red for AVOID, and a deliberately colourless grey for
  UNAVAILABLE. Each has a `-container` pair for the badge background.
- **Primary** is used for exactly two things: the Start button, and the
  working state of an agent. It is not a decorative accent.

Every foreground/background pair in this file meets **WCAG AA (4.5:1)** for
body text. That is a hard requirement, not an aspiration — an audit of the
first palette found `on-surface-faint` at **2.63:1**, and it was being used for
KPI labels, agent stat labels and the trigger/invalidation/objective price
levels. Numbers a user might act on were the least legible text on the page.

## Typography

One family, Inter, with a system fallback stack. Size and weight carry the
hierarchy; there are no decorative faces.

The important rule is **tabular numerals**. Every metric, price and percentage
uses `font-feature-settings: 'tnum' 1`, so that digits occupy identical width.
Without it, a column of prices visibly jitters as it updates on each poll,
which reads as instability in the data rather than in the font.

`label-sm` is uppercase with wide tracking and is used for every field label —
KPI captions, agent stat names, track names, table headers. It should never be
used for a value.

## Layout & Spacing

A single centred column, max 1180px, on a 4px-derived spacing scale.

The page is ordered by decreasing urgency:

1. **Header** — brand, market phase, controls. The phase chip is here because
   everything below it means something different depending on whether the
   session is live.
2. **KPI row** — four numbers that answer "what happened in this run".
3. **Status strip** — track record, calibration, regime, capital, next run.
   These are context, deliberately smaller than the KPIs.
4. **The panel** — eight agent cards, in pipeline order.
5. **Screens and heatmap** — order book, sector heatmap.
6. **Verdict feed** — the actual output, newest first.
7. **Run log** — collapsed by default.

Verdict cards are the densest element and get the most internal air: a header
line, then two side-by-side track panels that collapse to stacked on narrow
screens.

## Shapes

Rounded rectangles throughout, on a 5/10/14px scale. Cards get `lg`, nested
panels `md`, badges `full`. Nothing is a circle except status dots.

Borders are always 1px and always `outline-variant`, except for the controls
row which uses the heavier `outline` to signal interactivity.

## Components

**Card** — white, 1px `outline-variant`, `lg` radius, one very soft shadow.
The shadow exists to lift the card off the grey field, not to suggest height.

**Metric** — a `label-sm` caption above a `metric-lg` value. Values are
tabular. A missing value renders as `—`, never as `0` or a blank.

**Badge** — a pill in a verdict container colour. Verdict is *never* conveyed
by colour alone; the badge always carries the word BUY / WATCH / AVOID /
UNAVAILABLE.

**Track panel** — one per horizon inside a verdict card, tinted by verdict.
Carries the badge, confidence, horizon, rationale, levels, and any gate that
fired.

**Gate chip** — amber, for a verdict held back by a desk rule. This is a
first-class component because "we would have said BUY but the regime filter
stopped it" is one of the most important things the app can tell you.

**Status dot** — grey when offline, primary and pulsing while working, green
when done. The pulse is the only ambient animation in the product and it is
reporting real state.

## Do's and Don'ts

**Do** use `—` for a value that does not exist, and put the reason nearby. A
blank cell is indistinguishable from a bug; a dash plus a stated reason is the
whole trust model of this app.

**Do** keep verdict colour and verdict word together. A red-green colourblind
user must lose nothing.

**Do** use tabular numerals for anything that updates on a poll.

**Do** surface gates and suppressions rather than hiding them. A signal that
was blocked is more informative than one that never appeared.

**Don't** introduce glass, blur, or translucent surfaces. They cost contrast
on the one thing this interface exists to show.

**Don't** add a second accent colour. Every additional hue makes the verdict
palette mean less.

**Don't** animate anything that is not reporting live state. Decorative motion
in a financial interface reads as the data moving.

**Don't** use `on-surface-faint` for numbers a user might act on. It is for
labels. Levels, prices and percentages belong in `on-surface-variant` or
darker.

**Don't** grow the type scale to fill space. If a section looks empty, it
usually means the evidence is thin — which is worth showing, not padding.
