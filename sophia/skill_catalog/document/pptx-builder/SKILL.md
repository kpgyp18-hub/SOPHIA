---
skill_id: skill.document.pptx-builder
name: pptx-builder
version: 1.0.0
description: Build a presentation from a validated plan, context, and template.
category: document
capability_tags: [pptx, artifact-generation]
allowed_tools: [filesystem.read, filesystem.write]
requires_review: true
status: active
---
# PPTX Builder

1. Resolve generation context and an optional compatible template.
2. Build only through a host-owned PPTX adapter on a checkpointed working copy.
3. Validate package structure, render a preview, and run the PPTX judge profile.
4. Register the artifact with context and template provenance.
