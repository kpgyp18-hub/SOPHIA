---
name: docx-editing
description: Create or edit Microsoft Word documents (.docx, .dotx) when the requested deliverable is a Word document; preserve styles, tracked changes, headers/footers, and layout.
---

# DOCX Editing

Deliver a Word document that opens cleanly and keeps its intended structure and formatting.

## Preflight

1. Identify the target sections/headings, requested output format, and whether styles, tracked changes, comments, headers/footers, tables of contents, or embedded objects are present.
2. Preserve the original file. For template-bound documents (`.dotx`, content controls, mail-merge fields), confirm which bindings must survive.
3. Use a DOCX-aware library or the native application for structural edits. Do not treat DOCX as flat text; it is a packaged XML document with style, numbering, and relationship parts that plain string edits can corrupt.

## Edit and verify

1. Change only the requested text, styles, tables, or sections. Apply edits through existing paragraph/character styles rather than manual formatting overrides, unless the user's document does not use styles.
2. If tracked changes are in use, insert edits as tracked revisions rather than silently rewriting accepted text, unless the user asks for a clean edit.
3. Reopen the output and verify the changed sections, table of contents/field updates, headers/footers, page numbering, and that styles and formatting outside the requested edit remain intact.

## Limits

Treat macros (`.docm`), complex field codes, embedded OLE objects, and content controls as fidelity-sensitive. Do not promise full round-trip preservation of features the selected toolchain cannot confirm; report the validated tool and any unresolved risk before overwriting a source.
