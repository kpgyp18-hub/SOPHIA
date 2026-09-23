---
name: xlsx-editing
description: Create, repair, or edit Excel-compatible spreadsheet files (.xlsx, .xlsm, .xltx, .csv, or .tsv) when the requested deliverable is a spreadsheet; preserve formulas, sheets, and formatting.
---

# XLSX Editing

Deliver a spreadsheet that opens cleanly and retains its intended calculation model.

## Preflight

1. Identify the input and output format, target sheets/ranges, macro requirements, and whether formulas, charts, pivot tables, external links, or protected sheets are present.
2. Preserve the original file. For macro-enabled workbooks, retain VBA explicitly and do not save it as `.xlsx`.
3. Use a spreadsheet-aware library for cell-level edits. Confirm that the selected library supports every feature that must survive the edit; use the native application or an approved conversion path when it does not.

## Edit and verify

1. Change only the requested cells, formulas, styles, validation rules, charts, or sheet structure. Preserve formulas as formulas rather than replacing them with computed values.
2. Recalculate formula-bearing workbooks with a compatible spreadsheet engine before delivery; do not treat empty cached values as successful calculation.
3. Reopen the output and verify target values/formulas, sheet names, dimensions, merged cells, hyperlinks, and the absence of introduced formula errors. Visually inspect layouts when charts or print formatting matter.

## Limits

Do not claim full fidelity for unsupported Excel features, external data connections, or VBA unless the validated toolchain and a reopen check confirm them. Report those limits before overwriting a source.
