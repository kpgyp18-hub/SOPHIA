---
skill_id: skill.document.xlsx-quality-check
name: xlsx-quality-check
version: 1.0.0
description: Validate workbook formulas, references, data consistency, and package integrity.
category: document
capability_tags: [xlsx, quality-review]
allowed_tools: [filesystem.read]
requires_review: false
status: active
---
# XLSX Quality Check

1. Inspect formulas, references, types, hidden features, and package structure.
2. Apply the XLSX integrity judge context and profile.
3. Return PASS, FAIL, or UNCERTAIN with concrete evidence.
