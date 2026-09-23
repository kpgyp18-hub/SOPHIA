---
name: minimal-implementation
description: Design, implement, or review a coding change for the smallest solution that satisfies the confirmed requirement; use when avoiding unnecessary dependencies, abstraction, or scope is a primary concern.
---

# Minimal Implementation

Deliver the smallest maintainable change that satisfies the confirmed behavior. This is a decision framework, not permission to omit tests, error handling, security, or requested functionality.

## Before changing code

1. Restate the observable requirement and identify the narrowest relevant entry point. Check whether existing behavior, a platform feature, or a standard-library facility already meets it.
2. Identify what must remain unchanged: public interfaces, data shape, error behavior, performance constraint, and compatibility boundary.
3. Reject work with no demonstrated requirement. If two approaches satisfy the requirement, prefer the one with fewer new concepts, dependencies, files, and long-term state.

## Implementation

1. Modify the existing path rather than creating a parallel framework unless the existing path cannot meet the requirement.
2. Add an abstraction only when it removes an observed duplication or protects a real compatibility boundary; name the concrete present need it serves.
3. Keep the diff focused. Do not fold refactoring, formatting churn, dependency upgrades, or speculative options into the same change.
4. Preserve required validation. A smaller patch still needs a test or direct observable check appropriate to its risk.

## Review gate

Before completion, state why each new file, dependency, configuration option, or abstraction is necessary. Remove anything whose justification is hypothetical. Report the behavior delivered, validation evidence, and intentionally deferred work.

## Reference implementations

Use these as methodology references, not as bundled runtime code:

- Ponytail minimal-solution skill: <https://github.com/DietrichGebert/ponytail>
- Superpowers development methodology: <https://github.com/obra/superpowers>
