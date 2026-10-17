"""Pinned consumer catalog-sheet schema, parse and render (gen-mcp-server 80b33e41).

GEN-10539 requires the retry comparison to use the consumer's own catalog-sheet
parse/render semantics, never a second TSV parser in agent-infra. These are the
relevant functions copied from ``gen-mcp-server`` at 80b33e41
(``src/gen_mcp_server/catalog_sheet.py``), so the fixture regeneration command
renders bytes the future product must read with the same schema. ``derive`` is
not copied: it needs the full MCP runtime, and the fixture below supplies rows
already in the pinned COLUMNS shape, exactly as the vendored TSV does.

# intent: a test fixture that parsed the sheet with its own ad-hoc TSV reader
# could accept a shape the consumer would reject, hiding a real contract diff.
"""

from __future__ import annotations

import csv
import io

# ``catalog_sheet.KEYS`` at 80b33e41.
KEYS = {
    "Tool catalog": ("tool_name",),
    "Branches": ("tool_name", "branch_key"),
    "Discover views": ("domain", "view"),
    "Migration": ("legacy_tool",),
    "Provider routing": ("capability",),
}

# ``catalog_sheet.COLUMNS`` at 80b33e41; Provider routing is
# ``provider_catalog.COLUMNS`` + served_sha.
COLUMNS = {
    "Tool catalog": (
        "tool_name", "kind", "replaces_legacy_count", "readOnlyHint",
        "destructiveHint", "idempotentHint", "openWorldHint", "served_sha",
    ),
    "Branches": (
        "tool_name", "branch_kind", "branch_key", "required_fields", "enum_fields",
        "served_idempotency_key", "served_confirm_token", "source_fn",
        "lifecycle", "scheduled_task_ok", "served_sha",
    ),
    "Discover views": (
        "domain", "view", "required_args", "maps_legacy_tools",
        "scheduled_task_ok", "served_sha",
    ),
    "Migration": ("legacy_tool", "fate", "new_tool", "new_branch_key", "served_now", "served_sha"),
    "Provider routing": (
        "capability", "mcp_capability", "platform", "provider", "fallback_provider",
        "fallback_trigger", "unit_cost", "limits", "evidence", "served_sha",
    ),
}


# Verbatim ``catalog_sheet.render`` at 80b33e41.
def render(sections):
    out = io.StringIO()
    writer = csv.writer(out, delimiter="\t", lineterminator="\n")
    for tab, rows in sections.items():
        out.write(f"# {tab}\n")
        writer.writerow(COLUMNS[tab])
        writer.writerows(["" if v is None else v for v in row.values()] for row in rows)
        out.write("\n")
    return out.getvalue()


# Verbatim ``catalog_sheet.parse`` at 80b33e41.
def parse(text):
    sections = {}
    for block in text.strip().split("\n\n"):
        title, body = block.split("\n", 1)
        sections[title[2:]] = list(csv.DictReader(io.StringIO(body), delimiter="\t"))
    return sections
