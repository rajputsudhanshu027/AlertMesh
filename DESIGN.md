---
name: AlertMesh
description: A field folio for reviewing official Alerts around the Places people monitor.
colors:
  forest-ink: "#183b32"
  forest-action: "#285447"
  field-paper: "#e8eeea"
  record-surface: "#f8faf7"
  rule-line: "#cbd6ce"
  body-ink: "#24342e"
  secondary-ink: "#52635b"
  place-green: "#477a67"
  safety-amber: "#875514"
  safety-wash: "#f2e8ce"
  urgent-red: "#8d3c32"
  weather-blue: "#416771"
typography:
  display:
    fontFamily: "Georgia, 'Times New Roman', serif"
    fontSize: "clamp(36px, 4vw, 52px)"
    fontWeight: 500
    lineHeight: 1.02
    letterSpacing: "-0.035em"
  body:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.65
    letterSpacing: "normal"
  label:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "11px"
    fontWeight: 650
    lineHeight: 1.45
    letterSpacing: "0.03em"
rounded:
  xs: "4px"
  sm: "5px"
  md: "8px"
  lg: "12px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  2xl: "32px"
  3xl: "48px"
components:
  button-primary:
    backgroundColor: "{colors.forest-action}"
    textColor: "{colors.record-surface}"
    rounded: "{rounded.sm}"
    padding: "8px 13px"
    height: "39px"
  button-primary-hover:
    backgroundColor: "{colors.forest-ink}"
  alert-record:
    backgroundColor: "{colors.record-surface}"
    textColor: "{colors.body-ink}"
    rounded: "{rounded.md}"
    padding: "15px 10px 14px 0"
---

# Design System: AlertMesh

## Overview

**Creative North Star: “The Field Folio”**

AlertMesh reads like a dependable field reference: people move from the Places they monitor to a concise Inbox, then into a selected Alert record. The visual world uses cool green-gray paper, evergreen ink, and ruled divisions to give urgent information a calm, practical frame.

The same folio language carries through the local demo, account access, Inbox, and Place management. Source freshness remains visible, maps stay functional, and status never relies on color alone.

**Key Characteristics:**

- Place-first orientation with a distinct Alert ledger and detail record.
- Cool paper tones, evergreen ink, and limited hazard colors.
- Serif display headings paired with clear sans-serif interface text.
- Fine rules and restrained elevation separate records without nesting cards.

## Colors

The palette keeps the page quiet while reserving stronger color for primary action, source condition, and hazard meaning.

### Primary
- **Evergreen Ink**: anchors the masthead, headings, and selected states.
- **Forest Action**: marks the primary action and active filters.

### Secondary
- **Place Green**: identifies monitored Places and healthy source signals.
- **Safety Amber**: signals delayed source data and moderate-severity weather.
- **Weather Blue**: distinguishes earthquake information from weather Alerts.
- **Urgent Red**: identifies severe conditions and failed delivery states.

### Neutral
- **Field Paper**: the continuous page ground.
- **Record Surface**: the raised reading surface for source and Alert records.
- **Rule Line**: divisions between folio sections and list entries.
- **Body Ink**: primary reading text; **Secondary Ink** supports metadata and helper text.

**The Status-in-Words Rule.** Pair colored source and hazard cues with labels or shape so status remains clear without color perception.

## Typography

**Display Font:** Georgia (with Times New Roman, serif fallback)
**Body Font:** system UI sans-serif stack
**Label/Mono Font:** system UI sans-serif stack; tabular numerals for times and counts.

**Character:** The serif display voice makes major headings feel like a printed reference. Sans-serif body and label text keeps controls, source status, and Alert metadata direct and familiar.

### Hierarchy
- **Display** (500, responsive clamp, tight leading): the page title and first-level account heading.
- **Headline** (500, 25px, 1.15): the selected Alert title.
- **Title** (500, 18–22px, about 1.2): Inbox entries and account sections.
- **Body** (400, 14px, 1.65, around 58ch): explanatory copy and guidance.
- **Label** (650, 11px, 1.45, light tracking): controls, metadata, and source records.

**The Legible Record Rule.** Keep functional labels and metadata at or above the documented label size; let hierarchy come from spacing and weight instead of shrinking text.

## Layout

The desktop demo uses three reading areas: a narrow Place index, the Inbox ledger, and a selected Alert record. Source freshness spans the workspace above them. Account screens reuse the same reading surfaces and fine rules. At tablet widths the Alert record moves beneath the Place index and Inbox; on phones these regions stack in task order, and filters remain horizontally scrollable.

Use a spacing rhythm built from compact control gaps and broader section breaks. Keep long explanatory copy to a comfortable measure; times and counts use tabular numerals.

## Elevation & Depth

Depth stays restrained. Fine borders and background shifts do most of the separating; soft, low shadows appear on the selected Alert record and account surfaces. Avoid colored glow effects. Focus indicators use a clearly visible safety-amber outline.

## Shapes

Controls use gently squared corners. Record surfaces use a modest radius; small tags and input controls use tighter corners. Reserve pill shapes for compact status tokens only.

## Components

- **Primary action:** a forest-filled button with strong contrast; hover deepens the forest color.
- **Quiet action:** a light surface with a fine border; hover changes the surface, not its position.
- **Alert record:** a ruled ledger row with hazard and state labels, title, matching Places, and time. A narrow severity marker supplements the text labels.
- **Selected Alert record:** use a muted green-gray surface and a visible outline so selection is apparent without relying on the severity marker.
- **Source status:** pair the source name and freshness wording with a distinct status dot; retain the last-checked time.
- **Input:** use a light surface, readable label, and visible focus outline; keep validation and recovery text adjacent to the form.

## Do's and Don'ts

- Do keep the Place index, Inbox, and selected Alert record easy to scan in that order.
- Do preserve full Alert guidance and source freshness in the detail view.
- Do use hazard color sparingly and retain textual state labels.
- Do keep demo and account flows within the same folio system.
- Don't present stale source data as current.
- Don't rely on small text, color alone, decorative grids, or glow effects to carry meaning.
