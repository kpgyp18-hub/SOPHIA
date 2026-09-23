---
name: hwpx-editing
description: Edit Korean HWPX documents while preserving their XML package structure, styles, tables, and layout; use for .hwpx revisions, not legacy binary .hwp files.
---

# HWPX Editing

Handle HWPX as a structured, XML-based document package and preserve the source unless the user authorizes replacement.

## Preflight

1. Confirm the file is `.hwpx`, not legacy `.hwp`; identify requested sections, tables, images, headers/footers, numbering, and signature or protection constraints.
2. Make an output copy before editing. Inspect the package structure and use a HWPX-capable editor or library that can preserve the features in scope.
3. Do not make blind ZIP/XML text substitutions. They can invalidate relationships, styles, indexes, or section markup.

## Edit and verify

1. Change only the requested text, tables, styles, or embedded assets while preserving OWPML package relationships and unaffected XML parts.
2. Validate the resulting package can be opened by a HWPX-compatible application. Inspect the changed pages visually whenever layout, tables, Korean line breaking, or pagination matters.
3. Compare the output against the original to confirm that attachments, images, styles, numbering, and metadata outside the requested edit remain intact.

## Limits

HWPX is XML-based and standardized as OWPML, but fidelity depends on the specific editor/library. Do not promise round-trip preservation of unsupported controls, signatures, or proprietary extensions; report the validated tool and any unresolved compatibility risk.
