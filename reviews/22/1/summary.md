## AI review (round 1): **blocked**

See the [review rubrics](https://github.com/FormalFrontier/TauCetiReview/tree/main/rubrics).

### ⛔ scope — block  `claude/claude-sonnet-4-6`
This PR edits only `AGENTS.md` and `README.md` with documentation about the auto-merge pipeline. The roadmap contains exclusively mathematical targets (Universal Covers, Jacobian Challenge, Reductive Groups, PDE); there is no path from infrastructure documentation to any of them.
- No roadmap target. The changes describe the review/merge pipeline in prose but advance none of the four mathematical roadmap targets and are not a prerequisite for any of them. _Fix:_ If this documentation is worth landing, a human must add a corresponding node to the roadmap (e.g., an 'infrastructure' or 'meta' section) before this PR can be accepted under the current rubric.

### ✅ correctness — approve  `claude/claude-sonnet-4-6`
PR contains only documentation edits to AGENTS.md and README.md; no Lean definitions, statements, or proofs are introduced or modified. Nothing in scope for the correctness-and-faithfulness angle.

### ✅ reuse — approve  `codex/gpt-5.5`
The diff only changes project documentation and introduces no TauCeti or Lean declarations, so there is no reuse or duplication issue under this rubric.

### ✅ proof-quality — approve  `claude/claude-sonnet-4-6`
PR touches only documentation files; no proofs to evaluate.
