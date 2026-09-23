---
name: troubleshooting
description: Diagnose and fix software failures through reproducible evidence, minimal safe changes, and regression validation.
---

# Troubleshooting

Move from symptom to verified root cause with the smallest safe intervention.

## Workflow

1. Capture symptom, expected behavior, environment, exact command/request, error output, and reproducibility rate.
2. Triage severity and data/security impact before changing state. Preserve logs and avoid destructive recovery steps.
3. Form explicit hypotheses, inspect the narrowest relevant code and configuration, and test with discriminating evidence.
4. Implement only the change needed for the confirmed cause. Keep unrelated user modifications intact.
5. Reproduce the failure, verify the fix, run relevant regression checks, and check adjacent error paths.

## Handoff

Report root cause, changed files, validation performed, residual risk, and a rollback path for material changes. If unconfirmed, state what was ruled out and the next highest-value observation.
