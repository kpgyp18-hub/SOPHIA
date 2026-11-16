---
skill_id: skill.business.monthly-report
name: monthly-report
version: 1.0.0
description: Execute the monthly reporting workflow across analysis and report artifacts.
category: document
capability_tags: [cross-artifact, monthly-reporting]
allowed_tools: [filesystem.read, filesystem.write]
requires_review: true
status: active
---
# Monthly Report

1. Load the approved monthly-report workflow definition.
2. Analyze the source workbook with data-quality and KPI contexts.
3. Build each requested report artifact with its own generation context.
4. Run separate judge contexts and register package provenance.
