# Tau Ceti Review

The review rubrics and (soon) the machinery that runs review for
[Tau Ceti](https://github.com/FormalFrontier/TauCeti), an AIs-welcome Lean 4 library
downstream of Mathlib. Humans own these rubrics; AIs author the code; the human roadmaps
live in [TauCetiRoadmap](https://github.com/FormalFrontier/TauCetiRoadmap).

## How review works

Reviewers run only after a PR's CI is green, so the mechanical layer (build, the axiom
allowlist, the Mathlib linter set, the import boundary) is already satisfied. Each PR is then
judged by several independent agents, one per angle, which post `approve` / `request_changes`
/ `block` verdicts with evidence. Only the integrity angles may block.

## Rubrics

Each agent's prompt is [`rubrics/_common.md`](rubrics/_common.md) followed by its angle file;
see [`rubrics/README.md`](rubrics/README.md) for the list and which angles can block.

## Status

- `rubrics/` — the per-angle prompts (live).
- The review runner and its GitHub Actions workflow — in progress.
