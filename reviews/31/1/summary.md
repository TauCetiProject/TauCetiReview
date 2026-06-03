## AI review (round 1): **blocked (daily budget reached; skipped naming and after)**

See the [review rubrics](https://github.com/FormalFrontier/TauCetiReview/tree/main/rubrics).

### ⛔ scope — block  `codex/gpt-5.5`
This PR is explicitly a throwaway CI/go-live test and does not advance a Tau Ceti roadmap target or a proximate prerequisite. It should not be merged as library content.
- `TauCeti/Basic.lean:16` — `TauCeti.goliveCheck` is a throwaway numeric sanity theorem, not material on a path to a roadmap target. _Fix:_ Close this PR or replace it with content tied to a specific roadmap file and node/heading.

### ✅ correctness — approve  `codex/gpt-5.5`
From the provided diff, the only new declaration is `TauCeti.goliveCheck : 0 < 31`, which faithfully states a concrete arithmetic fact and is neither vacuous nor a misplaced assumption. No correctness or faithfulness issue is present under this rubric.

### 🟡 reuse — request_changes  `codex/gpt-5.5`
The new theorem is a named one-off numeric fact rather than reusable API; it should not be added as a parallel declaration for an instance of existing positivity lemmas/tactics.
- `TauCeti/Basic.lean:16` — `TauCeti.goliveCheck : 0 < 31` is a special-case named theorem for a concrete numeral, duplicating the role of existing general positivity facts/procedures rather than adding reusable library material. _Fix:_ Remove `goliveCheck`; where this fact is needed locally, use `norm_num`, `decide`, or a general positivity lemma inline instead of adding a named declaration.

### ✅ attribution — approve  `codex/gpt-5.5`
No attribution issue: the added throwaway numeric sanity theorem does not closely follow an identifiable external formalization or informal source requiring credit.

### 🟡 api-design — request_changes  `codex/gpt-5.5`
The PR adds a throwaway public theorem to the root TauCeti API with no roadmap-backed downstream use. That is an over-exposed surface for a go-live test and should not ship as library API.
- `TauCeti/Basic.lean:17` — `TauCeti.goliveCheck` is a new public theorem whose only stated purpose is a live-chain test, so it exposes meaningless API unrelated to a named downstream target. _Fix:_ Remove `goliveCheck`; if a CI smoke test is needed, keep it outside the exported library surface rather than adding a public declaration.

### 🟡 generality — request_changes  `claude/claude-sonnet-4-6`
`goliveCheck : 0 < 31` is a fully instantiated numeric fact with no free variables, adding no reusable API. The natural Mathlib level for this kind of discharge is `norm_num` inline, not a named theorem.
- `TauCeti/Basic.lean:17` — `goliveCheck : 0 < 31` is an extreme special case with no library value: no parameters, no roadmap target, and `norm_num` already discharges it at every call site without a named lemma. The docstring itself flags it as throwaway. _Fix:_ Remove the theorem entirely. If a positivity lemma is needed for a roadmap target, state it at the natural level (e.g., `(n : ℕ) → 0 < n + 1`) and derive the numeric instance, or use `norm_num` inline.

### ✅ placement — approve  `claude/claude-sonnet-4-6`
Single throwaway theorem added to the only file in the repo; placement and imports are unaffected.
