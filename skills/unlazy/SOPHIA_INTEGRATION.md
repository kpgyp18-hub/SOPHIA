# SOPHIA integration

Upstream: [Leonxlnx/unlazy](https://github.com/Leonxlnx/unlazy), pinned at
`16671491f6679ad9378f52604d3bc2415b4120c7` (source version 2.1.0, MIT).
Upstream files and LICENSE are preserved. UPSTREAM.json and this note are local additions.

The same pinned skill is installed for the user's Codex CLI and Claude Code.
Use `$unlazy` in Codex or `/unlazy` in Claude Code when explicitly invoking it.
The project copy is a portable skill asset; installation does not activate a new
SOPHIA runtime tool, Worker, model route, scheduler or database registry entry.

## Existing project constraints still apply

- AGENTS.md, CLAUDE.md, AGENT_RULES.md and current user directions take precedence.
- One original execution deadline remains in force. Decomposition does not grant
  a fresh full budget to retries, relays or child work.
- Keep the current whole-suite test pause. Use approved explicit targets through
  scripts/run_tests.py or the bounded scripts/long_tasks.py test surface.
- Core/DB/provider-router changes, Rust conversion and new Workers remain frozen
  unless the user separately lifts that scope restriction.
- The user deferred commits until Claude's quota recovers. Skill installation
  does not authorize commit, push, publication or permanent configuration changes.
- Native agent fan-out requires explicit user or applicable project authorization;
  prefer the solo ledger workflow for the current single-agent task.
- Review CHECK, EXPECT, CWD and called scripts before any gate execution. This
  install does not approve an inherited ledger or run an upstream CHECK.
- The optional Stop hook is not installed. Hook consent and machine-specific
  Claude settings remain separate from adding this skill.

The feature work already in progress predates this installation. Its existing
test evidence is not relabeled as having been run under unlazy gates.
New Codex skill discovery is available on the next turn.
