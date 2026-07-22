---
name: design-power-bi-report
description: Use when polishing, theming, or styling a Power BI report — brand colors, themes, KPI cards, backplates, headers, logos, shapes, page backgrounds, layout, and formatting taste. Triggers on "theme my report", "make it look professional", "add our brand colors", "add a logo", "style the KPI cards", "clean up the layout", "make this look designed". Turns a bound-but-plain report into a designed one.
---

# Design a Power BI Report

Charts alone read as "generated." Layered backplates, header bands, logos, a
coherent theme, and a real grid read as "designed." Use the pbi-report design
layer to close that gap. Call `pbi_set_project(path)` first.

## Adding to an EXISTING report — match it, don't reinvent

When the report already has pages, a new page must look like it belongs:
- **Page size / orientation is inherited automatically.** `pbi_create_page`,
  `pbi_build_page`, and `pbi_build_designed_page` default to the existing
  pages' dimensions — don't pass a width unless you deliberately want a
  different size.
- **Theme is inherited too.** Omit `accent` on `pbi_build_designed_page` /
  `pbi_scaffold_report` and it uses the report's existing theme color;
  scaffold will NOT overwrite an installed theme. Only call
  `pbi_generate_theme` on a fresh report, or when the user explicitly wants a
  new look.
- Match the existing pages' header/KPI pattern by eye: open one with
  `pbi_get_visual` / `pbi_list_visuals` first if unsure.

## Start with a theme (fresh reports)

**`pbi_generate_theme(brand="#RRGGBB", mode="light"|"dark")`** turns one brand
color into a full, coherent theme — categorical palette, text classes, visual
styles — and installs it. This alone lifts the whole report. If the user gives
a theme JSON instead, use `pbi_set_report_theme(theme)`.

## Design elements (the epic-factor)

- **`pbi_add_text(page_id, runs, z=5000)`** — titles, section headers,
  commentary. `runs` can be a string or rich runs
  `[{text, bold, size, color, align}]`. Keep text on top with a high `z`.
- **`pbi_add_shape(page_id, shape, fill, round_corners, z=0)`** — KPI
  backplates, dividers (`shape="line"`), accent bars. Keep backplates **behind**
  data with a **low `z`**.
- **`pbi_add_image(page_id, image_path, scaling="Fit")`** — logos, icons,
  backgrounds. Uploads the file into the report resources automatically.
- **`pbi_style_page(page_id, background_color, wallpaper_color)`** — canvas +
  outer wallpaper.
- **`pbi_group_visuals(page_id, [ids], name)`** — move/style a block as one.

## Z-order discipline

Backplates/wallpaper: low z (behind). Data visuals: default. Labels/logos/nav:
high z (on top). If a card looks like it's floating, add a rounded rectangle
backplate just behind it (slightly larger, low z).

## Formatting existing visuals

**`pbi_format_visual(page_id, visual_id, target, objects)`**:
- `target="container"` → title, background, border (the chrome)
- `target="visual"` → labels, legend, axes (the content)

`objects` is `{objectName: {prop: value}}`. Plain strings/numbers/bools and
`"#hex"` colors are auto-encoded — e.g.
`{"title": {"text": "Revenue", "fontColor": "#FFFFFF", "background": "#1F3A5F"}}`.

## Layout

`pbi_build_designed_page` and `pbi_build_page` auto-lay-out on a 12-column grid
with a KPI band and auto page-height. To hand-place, pass `position:
{x,y,width,height}` per visual and `pbi_move_visual` to adjust. Always finish
with **`pbi_lint_page(page_id)`** and fix overlaps / off-canvas / misalignment.

## Taste defaults

- One accent color; neutrals for structure. Let the generated theme carry it.
- KPI strip across the top (cards on backplates), charts below on a 2-col grid,
  tables full-width.
- A header band (colored rectangle + white title textbox) frames the page.
- Don't over-format: consistent > decorated.
