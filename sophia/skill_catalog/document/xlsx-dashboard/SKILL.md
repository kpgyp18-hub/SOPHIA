---
skill_id: skill.document.xlsx-dashboard
name: xlsx-dashboard
version: 1.0.0
description: Build an auditable Excel dashboard from validated metrics.
category: document
capability_tags: [xlsx, dashboard, kpi]
allowed_tools: [filesystem.read, filesystem.write]
requires_review: true
status: active
---
# XLSX Dashboard

1. Validate input data and KPI definitions.
2. Resolve data-quality, KPI, and chart-selection contexts.
3. Build calculations and visuals through the workbook adapter.
4. Run formula and integrity validation before registration.
