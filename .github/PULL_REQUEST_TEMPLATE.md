## Summary

<!-- What does this change and why? Link the issue it closes. -->

## Type of change

- [ ] Bug fix
- [ ] New tool or capability
- [ ] Refactor / internal
- [ ] Packaging, CI or docs

## Checklist

- [ ] `pytest` is green in **both** environments: mcp 1.x (`.venv`) and mcp 2.x (`.venv-mcp2`) - see CONTRIBUTING.md
- [ ] New or changed tool: registered through the `tool(...)` decorator with the right `read=` / `write=` flag (and `destructive=` where it deletes), in a `tools_*.py` module where possible
- [ ] Writes go through `core/io_safe` (atomic write + backup), keep each file's line endings and BOM, and TMDL edits stay surgical
- [ ] PBIR output is validated against the vendored Fabric schemas before it is written
- [ ] Tests added for the new behaviour, including the dry-run and undo path for writes
- [ ] Goldens changed only deliberately (`PBI_MCP_REGEN_GOLDENS=1`) and the diff reviewed
- [ ] README tool tables/counts and `CHANGELOG.md` (`[Unreleased]`) updated
- [ ] No real `.pbip` data, credentials or personal paths committed
- [ ] If a TMDL/PBIR writer changed: the result was reopened in Power BI Desktop (manual gate)

## Notes for the reviewer

<!-- Risky areas, follow-ups, screenshots of the reopened report, ... -->
