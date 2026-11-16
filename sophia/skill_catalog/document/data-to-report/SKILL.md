---
skill_id: skill.business.data-to-report
name: data-to-report
version: 1.0.0
description: Turn validated data findings into a grounded business report package.
category: document
capability_tags: [cross-artifact, reporting]
allowed_tools: [filesystem.read, filesystem.write]
requires_review: true
status: active
---
# Data to Report

1. Analyze source data and retain metric lineage.
2. Resolve artifact-specific contexts for each output step.
3. Build report artifacts through their host-owned adapters.
4. Judge each artifact independently before packaging.
