---
skill_id: skill.design.figma-review
name: figma-review
version: 1.0.0
description: Review Figma structure, consistency, accessibility, and handoff readiness.
category: document
capability_tags: [figma, design-review]
allowed_tools: [filesystem.read]
requires_review: false
status: active
---
# Figma Review

1. Resolve Figma review and accessibility contexts.
2. Inspect supplied design data through a host-owned read adapter.
3. Evaluate components, tokens, constraints, states, and handoff evidence.
4. Return a bounded review; do not mutate the design.
