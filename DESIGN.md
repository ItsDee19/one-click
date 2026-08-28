---
version: alpha
name: Dalal Desk
description: >
  A dark, precise research terminal for Indian equities. Optimised for reading
  numbers under time pressure, for long sessions, and for being trusted when it
  says it does not know something.
colors:
  # Dark is the default theme. A light set ships alongside and is one click
  # away; both are audited to the same 4.5:1 requirement.
  # --- surfaces, deepest to highest --------------------------------------
  surface: "#080b12"
  surface-container-lowest: "#0b0f16"
  surface-container-low: "#0b0f16"
  surface-container: "#0e1219"
  surface-container-high: "#141924"
  surface-container-highest: "#1b2130"
  # --- content on surfaces ------------------------------------------------
  on-surface: "#e9eef8"
  on-surface-variant: "#aab6cc"
  on-surface-muted: "#8896ae"
  on-surface-faint: "#78859c"
  outline: "#28303f"
  outline-variant: "#1b2130"
  # --- action -------------------------------------------------------------
  primary: "#4cc9f0"
  on-primary: "#03121a"
  primary-container: "#0c2230"
  on-primary-container: "#4cc9f0"
  # --- verdict semantics --------------------------------------------------
  buy: "#3ddc97"
  on-buy: "#03121a"
  buy-container: "#0d2a1f"
  on-buy-container: "#3ddc97"
  watch: "#f5b544"
  on-watch: "#1a1204"
  watch-container: "#2b2110"
  on-watch-container: "#f5b544"
  avoid: "#ff6b6b"
  on-avoid: "#1a0709"
  avoid-container: "#2c1417"
  on-avoid-container: "#ff6b6b"
  unavailable: "#78859c"
  unavailable-container: "#161b24"
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

The register is a dark instrument panel: deep near-black surfaces, hairline
borders, a faint engineering grid, and a single cyan accent. Colour is almost
absent except where it carries meaning. Nothing pulses, glows or animates
unless it is reporting live state.

Dark is the default because this is read for hours, often before sunrise, and
because a dark ground gives *more* contrast headroom than a light one — every
token here clears 4.5:1 with room to spare, where the light palette had to be
tuned to reach it. The light theme ships alongside, one click away, and the
choice is remembered.

The emotional target is **calm competence under time pressure**. A user opens
this at 09:00 with fifteen minutes before the bell. They need to find the
number they are looking for immediately and trust it. Excitement is the wrong
feeling for an interface that is often telling you to do nothing.

Two consequences worth stating up front, because they rule out most of what is
fashionable in dashboard design:

- **Legibility outranks atmosphere.** Glass, blur and translucency all reduce
  the contrast of text sitting on them. On a screen whose entire purpose is
  small numbers — ₹1,403.00, 2.6x, −1.87% — that trade is never worth making.
  Gradients appear in exactly three places (the logo mark, the primary button,
  and one fixed wash behind the header) and never behind text.
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

Labels are small, uppercase and widely tracked (0.09-0.13em). At 9.5-10.5px
that tracking is what keeps them legible rather than decorative, and it is what
makes the interface read as an instrument rather than a web page.

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

## Motion

Motion answers three questions and nothing else: *what changed*, *what is
working*, and *what just arrived*.

| Animation | What it reports |
|---|---|
| staggered rise on load | page structure, one pass, never on re-render |
| hairline draw across KPI cards | the board has initialised |
| **scan line** across an agent card | that agent is working right now |
| pulsing status dot | live state: working (accent) or done (green) |
| equaliser bars | the working agent is active, not hung |
| **flash on a value** | that number just changed |
| slide-in on a verdict row | a new verdict arrived |
| sweep across the Start button | a run is in flight and the button is locked |
| sheen on the logo mark | idle brand detail, 6s cycle, the one exception |
| spinner | a request is pending |

Two rules govern all of it:

- **A flash only fires on a genuine change.** The first render seeds the value
  silently. Flashing on every poll would train the eye to ignore the flash,
  which destroys the only thing it is for.
- **`prefers-reduced-motion` disables everything.** All of it is polish over a
  fully legible static page, so honouring the OS setting costs nothing.

Theme switching is deliberately *not* animated. Beyond reading as lag, a
transition on a property fed by a custom property that just changed leaves the
computed value stuck at the old one — the page stayed dark while every other
token flipped to light.

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
in a financial interface reads as the data moving. Every animation in the
product is listed under Motion; if a new one does not fit that list, it does
not belong.

**Don't** use `on-surface-faint` for numbers a user might act on. It is for
labels. Levels, prices and percentages belong in `on-surface-variant` or
darker.

**Don't** grow the type scale to fill space. If a section looks empty, it
usually means the evidence is thin — which is worth showing, not padding.
