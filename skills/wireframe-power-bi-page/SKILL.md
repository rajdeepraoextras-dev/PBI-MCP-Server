---
name: wireframe-power-bi-page
description: Use when planning the LAYOUT of a Power BI page before committing to real visuals — blocking out where the KPIs, charts, slicers, and titles go. Triggers on "wireframe a page", "plan the layout", "sketch a dashboard", "where should things go", "mock up a report page", "block out a page". Produces a fast, low-commitment layout the user can approve before you bind real data.
---

# Wireframe a Power BI Page

When the user is still deciding what a page should look like, don't jump to
final visuals. Block out the layout first, get a yes, then fill it in. Call
`pbi_set_project(path)` first.

## Think in bands and a 12-column grid

Every good page is a vertical stack of bands on a 12-column grid
(margin 16, gutter 12):

1. **Header band** — title + optional subtitle/logo (full width, ~72px tall).
2. **KPI strip** — 3–6 cards across the top, equal columns.
3. **Body** — charts on a 2-column grid; tables/matrices span all 12 columns.
4. **Footer** (optional) — source note / last-updated.

## Two ways to wireframe

### A. Describe it first (cheapest)
Return a short plan the user can approve — band by band, what goes where and
why — before touching the file. Example:

> Header: "Sales Overview". KPI strip: Revenue, Margin %, Orders, AOV.
> Body left: Revenue by Month (line). Body right: Revenue by Region (bar).
> Full-width: Top Products table.

### B. Block it out with placeholders (visual)
Build the skeleton with shapes + text, no data yet, so the user sees the
composition:
- `pbi_create_page(name)` (or size it larger for a tall page).
- `pbi_add_shape(page_id, "rectangle", fill="#ECEFF3", position=..., z=0)` for
  each visual's footprint (a grey placeholder box).
- `pbi_add_text(page_id, "[ Revenue by Month ]", position=..., z=5000)` to
  label each box.
- `pbi_style_page(page_id, background_color="#FFFFFF")`.

Use the 12-column math: a half-width chart is 6 columns, a KPI card is
2 columns (for 6) or 3 (for 4). Keep boxes aligned and evenly spaced — then
`pbi_lint_page(page_id)` to confirm no overlaps or off-canvas boxes.

## From wireframe to real

Once approved, replace placeholders with bound visuals in the same positions
via `pbi_add_visual` (pass the same `position`), or just call
`pbi_build_designed_page` / `pbi_build_page` with the agreed spec and let the
layout engine place them. Don't leave orphan placeholder shapes behind —
delete them with `pbi_delete_visual` (recoverable) as you fill each slot.

## Defaults when the user is vague

- 4 KPIs, 2 charts, 1 table is a safe starter page.
- Put slicers in a narrow left rail (2–3 columns) or across the header.
- One accent color; grey placeholders; real theme applied only once content is
  in (`pbi_generate_theme`).
