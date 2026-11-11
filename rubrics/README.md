# Review rubrics

Each file here is the prompt for one review agent. A PR is reviewed by several independent
agents, each judging a single angle. An agent's full prompt is `_common.md` (the shared
protocol), then the pull request, then its angle file. The angle comes last so that every
rubric of a round shares the same prompt prefix, which the prompt cache reuses.

Agents run only after CI is green, so the mechanical layer (build, axiom audit, the Mathlib
linter set, the import boundary) is already satisfied and no agent re-checks it.

## The angles

The table is in review order: rubrics run one at a time and a `block` halts the round, so the
block-capable integrity angles run first (cheapest, most-likely-to-block earliest).

| Rubric | Question | Can block? |
| --- | --- | --- |
| [`correctness`](correctness.md) | Do the statements and definitions say what they should? | yes |
| [`reuse`](reuse.md) | Does it reuse Mathlib / TauCeti instead of reinventing? | yes (outright duplication) |
| [`scope`](scope.md) | Is this on the roadmap, and a single topic? | yes |
| [`attribution`](attribution.md) | Does it credit its formal and informal sources? | yes (clear missing credit) |
| [`api-design`](api-design.md) | Minimal public surface, complete characteristic API? | no |
| [`generality`](generality.md) | Weakest assumptions; natural level? | no |
| [`placement`](placement.md) | Canonical home; direct, minimal imports? | no |
| [`naming`](naming.md) | Conclusion-describing names; conventional notation? | no |
| [`documentation`](documentation.md) | Accurate module and declaration docstrings? | no |
| [`proof-quality`](proof-quality.md) | Automation-first, robust proofs? | no |
| [`quality-pass`](quality-pass.md) | Did the author complete the PR template's quality pass? | no |

Blocking angles are integrity checks. The rest use `request_changes` for fixable issues.

`quality-pass` judges the PR description rather than the code. The checklist itself lives in Tau
Ceti's PR template (`.github/pull_request_template.md`), its source of truth: authors run it over
their own diff before review and report each check in the description. The angle checks that the
report is complete, kept current as the PR changes, and not contradicted by the diff, so a PR that
arrives with it done passes without a round trip. It adds to the other angles rather than replacing
them: in a pre-launch test on 16 merged PRs, the checklist and a plain review each caught defects
the other missed. It is run on, and required of, only PRs opened after the checklist reached the
template (TauCetiProject/TauCeti#33936), so PRs already open keep the scoreboards they have:
`PHASED_RUBRICS` in `runner/admission_policy.py` holds the cutoff PR number, and until that is set
the angle does not run.
