---
skill_id: skill.business.artifact-package-review
name: artifact-package-review
version: 1.0.0
description: Review a multi-artifact package without mutating its source artifacts.
category: document
capability_tags: [cross-artifact, quality-review]
allowed_tools: [filesystem.read]
requires_review: false
status: active
---
# Artifact Package Review

1. Load the workflow outputs and their recorded provenance.
2. Resolve a separate judge context for each artifact type.
3. Apply the matching quality profile without changing source artifacts.
4. Return PASS, FAIL, or UNCERTAIN with evidence for every artifact.
