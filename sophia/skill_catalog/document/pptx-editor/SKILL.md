---
skill_id: skill.document.pptx-editor
name: pptx-editor
version: 1.0.0
description: Apply bounded edits to an existing presentation while preserving unrelated content.
category: document
capability_tags: [pptx, artifact-editing]
allowed_tools: [filesystem.read, filesystem.write]
requires_review: true
status: active
---
# PPTX Editor

1. Inspect and checkpoint the original presentation.
2. Resolve the requested slide changes and applicable context.
3. Edit through a host-owned adapter and preserve unsupported features.
4. Validate structure and preview before registering a new version.
