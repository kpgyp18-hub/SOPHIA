---
skill_id: skill.document.hwpx-report-builder
name: hwpx-report-builder
version: 1.0.0
description: Build a structured HWPX report from grounded inputs.
category: document
capability_tags: [hwpx, report-generation]
allowed_tools: [filesystem.read, filesystem.write]
requires_review: true
status: active
---
# HWPX Report Builder

1. Resolve report purpose, required sections, context, and template.
2. Build through a host-owned HWPX adapter on a working copy.
3. Validate package XML, tables, formal tone, and grounding.
4. Register the reviewed artifact and provenance.
