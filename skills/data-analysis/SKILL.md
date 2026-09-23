---
name: data-analysis
description: Analyze structured data reproducibly, from schema inspection through validated findings and deliverables.
---

# Data Analysis

Turn data into decision-ready findings without hiding assumptions or damaging source data.

## Workflow

1. Inspect schema, units, time coverage, row count, missingness, duplicates, and identifiers before choosing a method.
2. Define question, population, metric, comparison, and success criterion. Flag ambiguity that changes the result.
3. Keep raw inputs immutable. Place intermediate artifacts in `workspace/` and final deliverables in `output/`.
4. Record filters, joins, imputations, exclusions, and calculations so the result is reproducible.
5. Validate with suitable invariants: totals, ranges, uniqueness, sample spot checks, and sensitivity checks when assumptions affect conclusions.

## Reporting

Lead with the answer, include method and limitations, distinguish correlation from causation, and use tables or charts only when they clarify a decision. Include paths to generated artifacts and enough detail to rerun the analysis.
