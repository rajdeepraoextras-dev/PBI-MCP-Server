"""Machine-readable capability descriptor (E1 D5).

`pbi_capabilities()` returns everything an LLM client needs to build a report
without trial-and-error: the visual types + their buckets, filter kinds,
formatting targets, binding grammar, and a worked example. Generated from the
live spec tables so it never drifts from the code.
"""

from __future__ import annotations

from core.visual_specs import VISUAL_SPECS

#: Shown on connect (server instructions) and on the first pbi_set_project call.
GREETING = (
    "Hi, I'm Rajdeep — a Power BI dev with 2 YoE, and I built this. "
    "Have fun with PBI now! LinkedIn: "
    "https://www.linkedin.com/in/rajdeep-rao-14bab1320/"
)


def capabilities() -> dict:
    return {
        "binding_grammar": {
            "shape": '{"bucket_name": ["Table.Field", ...]}',
            "note": "Measures vs columns are resolved automatically. Field "
                    "names must exist in the semantic model. Buckets are "
                    "per-visual-type — see visual_types below.",
            "aggregation": "Wrap a column to aggregate it: 'Sum(Table.Col)', "
                           "'Average(Table.Col)', 'Count(Table.Col)', etc.",
        },
        "interactivity": {
            "pbi_sort_visual": "sort a visual by a field (pairs with TopN)",
            "pbi_add_nav_button": "page-navigation button (build a nav bar)",
            "pbi_set_page_role": "make a page drillthrough or tooltip",
            "pbi_set_visual_interactions": "cross-filter behavior per pair",
            "pbi_create_bookmark": "capture current page + filter state",
        },
        "visual_types": {
            vt: {"required_buckets": spec["required"],
                 "optional_buckets": spec["optional"]}
            for vt, spec in sorted(VISUAL_SPECS.items())
        },
        "bucket_notes": {
            "Series": "This is the 'Legend' role in the Desktop UI.",
            "Y2": "Secondary axis for combo charts (the line series).",
            "donutChart/pieChart": "Slice by 'Series'; there is no 'Category'.",
        },
        "design_elements": {
            "text": "pbi_add_text — titles, headers, commentary",
            "image": "pbi_add_image — logos, icons, backgrounds",
            "shape": "pbi_add_shape — rectangle/line/oval backplates & dividers",
            "page_style": "pbi_style_page — background color/image/wallpaper",
            "group": "pbi_group_visuals — move/style a block as one",
        },
        "filters": {
            "scopes": ["report", "page", "visual"],
            "types": {
                "Categorical": "values=[...] (keep-these)",
                "Advanced": "comparison in eq|gt|ge|lt|le + comparison_value",
                "TopN": "top_n=N (ranks by the visual's value field)",
                "RelativeDate": "last_n=N + relative_unit in day|week|month|year",
                "Passthrough": "raw_condition for anything else",
            },
        },
        "formatting": {
            "pbi_format_visual": {
                "targets": {
                    "container": "title, background, border (chrome)",
                    "visual": "labels, legend, axes (content)",
                },
                "value_encoding": "plain str/int/float/bool and '#hex' colors "
                                  "are auto-encoded to PBIR literals",
            },
            "pbi_set_report_theme": "standard Power BI theme JSON (needs 'name')",
            "pbi_generate_theme": "brand color -> full validated theme",
        },
        "workflow": [
            "1. pbi_set_project(path)",
            "2. inspect: pbi_get_model / pbi_list_pages / pbi_model_usage",
            "3. build: pbi_build_page(name, visuals[]) or per-tool",
            "4. style: pbi_generate_theme, pbi_format_visual, design elements",
            "5. verify: pbi_validate_project (schema) + pbi_lint_page (design)",
        ],
        "example": {
            "tool": "pbi_build_page",
            "args": {
                "name": "Overview",
                "visuals": [
                    {"visual_type": "card",
                     "bindings": {"Values": ["Sales.Net Revenue"]},
                     "title": "Revenue"},
                    {"visual_type": "clusteredBarChart",
                     "bindings": {"Category": ["Date.Year"],
                                  "Y": ["Sales.Net Revenue"]},
                     "title": "By Year"},
                ],
            },
        },
        "safety": {
            "every_write": "atomic + backup + pre-flight schema validation",
            "deletes": "guarded by lineage + report usage; force/dry_run available",
            "recovery": "pbi_list_backups / pbi_restore_backup",
        },
    }
