---
name: agent-evaluation
description: Evaluate an agent, skill, prompt, or tool workflow with versioned cases and explicit verifiers; use when comparing behavior changes or validating an automation claim, not for routine unit testing alone.
---

# Agent Evaluation

Measure observable behavior against declared tasks. Do not infer reliability from a generic benchmark score, an anecdote, or a single successful run.

## Evaluation design

1. Declare the behavior under test, the target environment, input distribution, success criteria, and failure criteria before running cases.
2. Use small, representative, versioned cases. Include at least one negative or boundary case when the workflow makes safety, authorization, or data-preservation claims.
3. Define an explicit verifier for every consequential outcome: test command, file/hash comparison, parser, assertion, or independent human review. Separate evaluator judgment from the system being evaluated when practical.

## Execution and interpretation

1. Pin the skill/prompt/configuration revision and record model, tool versions, inputs, outputs, elapsed time, and material environment constraints.
2. Run the same cases for each variant. Do not silently discard failures, retries, timeouts, or partial successes.
3. Report per-case results and failure modes. Distinguish a verified capability from an untested condition; do not convert a pass rate into a guarantee.
4. Turn demonstrated failures into a regression case only when it has a stable verifier and belongs to the workflow's intended scope.

## Safety boundary

Use isolated, reversible test data. Obtain approval before an evaluation can access production systems, make external calls with cost, modify real user data, or exercise destructive behavior.

## Reference implementations

- Agentic Awesome Skills evaluation guidance: <https://github.com/sickn33/agentic-awesome-skills>
- Scientific Agent Skills security and validation guidance: <https://github.com/K-Dense-AI/scientific-agent-skills>
