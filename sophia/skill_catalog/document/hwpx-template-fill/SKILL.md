---
skill_id: skill.document.hwpx-template-fill
name: hwpx-template-fill
version: 1.0.0
description: Fill an approved HWPX template without changing its required structure.
category: document
capability_tags: [hwpx, template]
allowed_tools: [filesystem.read, filesystem.write]
requires_review: true
status: active
---
# HWPX Template Fill

1. Checkpoint and validate the approved template.
2. Map grounded inputs only to declared fields and sections.
3. Preserve required styles, tables, and approval fields.
4. Validate the completed package and retain template provenance.
