---
name: code-review
description: Review code changes for correctness, regressions, security, and test gaps; use for review requests, not routine implementation.
---

# Code Review

Review the change, not the author. Produce actionable findings backed by repository evidence.

## Scope

- Identify requested behavior, changed files, and relevant callers before judging the diff.
- Read local `AGENTS.md` instructions and preserve unrelated user changes.
- Do not rewrite code unless the request also authorizes a fix.

## Review method

1. Inspect the diff and trace changed inputs, outputs, state, error paths, and public interfaces.
2. Check correctness and regressions first; then security, data loss, concurrency, performance, and maintainability where relevant.
3. Run the narrowest meaningful existing tests or static checks. State when validation was not possible.
4. Report only concrete, reproducible, or strongly evidenced issues. Do not invent style nits.

## Findings

Order findings by `critical`, `high`, `medium`, `low`. Include file and line, impact, triggering condition, and remediation direction. Follow with test gaps and a short summary. If there are no findings, say so and list residual risks or unverified paths.
