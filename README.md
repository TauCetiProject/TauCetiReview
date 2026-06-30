# Tau Ceti Review

The review rubrics and the machinery that runs review for
[Tau Ceti](https://github.com/TauCetiProject/TauCeti), an AIs-welcome Lean 4 library
downstream of Mathlib. Humans own these rubrics; AIs author the code; the human roadmaps
live in [TauCetiRoadmap](https://github.com/TauCetiProject/TauCetiRoadmap).

Tau Ceti is being incubated by the [Lean FRO](https://lean-lang.org/fro/) in partnership with academic and
industry groups.

## How review works

Reviewers run only after a PR's CI is green, so the mechanical layer (build, the axiom
allowlist, the Mathlib linter set, the import boundary) is already satisfied. Each PR is then
judged by several independent agents, one per angle, which post `approve` / `request_changes`
/ `block` verdicts with evidence. Only the integrity angles may block. Rubrics run one at a
time, and a `block` halts the round: blocked code gets reworked or abandoned, so the remaining
rubrics wait until the block clears rather than reviewing a commit that won't survive. (A
manual `/review` is exempt — it always re-reviews every rubric.)

## Rubrics

Each agent's prompt is [`rubrics/_common.md`](rubrics/_common.md) followed by its angle file;
see [`rubrics/README.md`](rubrics/README.md) for the list and which angles can block.

## Reviewing it yourself

CI runs the review on the metered Anthropic / OpenAI APIs. A trusted contributor can run the
same review on their **own Claude / Codex subscription** with the `tauceti-review` CLI — no API
bill. See [REVIEWING.md](REVIEWING.md):

```bash
uvx --from git+https://github.com/TauCetiProject/TauCetiReview tauceti-review 42
```

## Meta-review

We A/B-test the reviews themselves, to measure and improve review quality. `judge.py` (in
[TauCetiData](https://github.com/TauCetiProject/TauCetiData)) takes two review runs of the
same `(pr, head_sha, rubric)` — production vs a `--shadow` arm with a different model or
rubric version — and has AI judges pick the better one, grounded in the actual checked-out
code (a fluent hallucinated finding should lose to a terse real one), in both presentation
orders, with a cross-family panel to dilute self-preference bias. Hard and audit-sampled
cases escalate to human meta-reviewers via `label.py`. The judgments and decisions feed
win-rates per model and rubric version, and judge–human agreement.

So far: several thousand archived review runs, a few hundred A/B pairs, and over a thousand AI
judgments across five judge models and three judge-prompt versions, plus a first round of human
decisions and preliminary calibration. All of it lives in
[TauCetiData](https://github.com/TauCetiProject/TauCetiData); see its `docs/` for the design.

## Costs

`tauceti-review-costs` reports the engine's review spend — tokens and imputed
dollars, per merged line of code, per day, and split by PR outcome — reading the
durable [TauCetiData](https://github.com/TauCetiProject/TauCetiData) archive
(reproducible by anyone) or the local store. Costs are recomputed from token
counts at the rate in effect on each run's date. See [runner/COSTS.md](runner/COSTS.md).

## Status

- `rubrics/` — the per-angle prompts (live).
- `runner/` — the review engine (`review.py` + `post.py`) and the `tauceti-review` CLI (live).
- `runner/costs.py` — the `tauceti-review-costs` analytics CLI (live).
- `runner/prices.json` — model rates; every dispatchable model must be priced (CI-enforced in `tests/`, and the engine fails fast on an unpriced model). Each archived run is stamped with a `prices_sha` so its cost is auditable.
- The GitHub Actions workflows — review + `tests` (price coverage) — live.
