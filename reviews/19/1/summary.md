## AI review (round 1): **blocked**

See the [review rubrics](https://github.com/FormalFrontier/TauCetiReview/tree/main/rubrics).

### ⛔ scope — block  `claude/claude-sonnet-4-6`
This PR modifies `.github/workflows/review.yml`, which CLAUDE.md explicitly marks as human-owned infrastructure. None of the four roadmap targets (JacobianChallenge, PDE, ReductiveGroups, UniversalCovers) cover CI/workflow maintenance, and there is no plausible prerequisite path from a shell-injection fix or exact-command check to any mathematical target.
- `.github/workflows/review.yml` — Change is to human-owned CI infrastructure with no path to any roadmap target. CLAUDE.md states '`TauCeti/` is the only place code goes; everything else (`Scripts/`, `.github/`, the Lake config) is human-owned.' No roadmap file contains a node or heading that this change advances. _Fix:_ If this security fix is worthwhile (and it likely is), a human should apply it directly; it does not belong in an AI-authored PR and need not be added to the mathematics roadmap.

### ✅ correctness — approve  `codex/gpt-5.5`
The supplied diff only changes GitHub Actions review-trigger handling and adds no Lean definitions or theorem statements, so there is no semantic-faithfulness issue in this review angle.

### ✅ reuse — approve  `claude/claude-sonnet-4-6`
This PR modifies only a CI workflow file; it introduces no Lean declarations and has no Mathlib or TauCeti overlap to evaluate.

### ✅ proof-quality — approve  `codex/gpt-5.5`
No proof-quality issues: the diff only changes a GitHub workflow and adds no Lean proofs or proof maintenance surface.
