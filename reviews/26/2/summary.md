## AI review (round 2): **changes requested**

See the [review rubrics](https://github.com/FormalFrontier/TauCetiReview/tree/main/rubrics).

### ✅ scope — approve  `codex/gpt-5.5`
The PR is a single coherent unit adding `Deck p` and its immediate action support, and it directly matches the stated UniversalCovers Stage 1 prerequisite for the deck transformation group of the universal cover.

### ✅ attribution — approve  `claude/claude-sonnet-4-6`
All central sources are credited in the code: the copyright header names Kim Morrison, the module docstring links Mathlib draft #40135 by name and URL, and the vendored instance credits its predecessor `Equiv.Perm.applyMulAction`. No laundering or missing attribution.

### 🟡 api-design — request_changes  `codex/gpt-5.5`
The `Deck` interface exposes more than the named downstream target needs and leaves its main action lemma out of simp normal form.
- `TauCeti/AlgebraicTopology/UniversalCover/Deck.lean:39` — The PR publicly exposes a general `MulAction (Y ≃ₜ Y) Y` plus faithful and continuity instances for all homeomorphism groups, although the stated target only needs the action of `Deck p` on `E`. _Fix:_ Define the `Deck p` action/faithfulness/continuity instances directly, or keep any ambient homeomorphism-action helper private/internal until TauCeti actually needs that public API.
- `TauCeti/AlgebraicTopology/UniversalCover/Deck.lean:74` — `Deck.proj_smul` is the characteristic normal-form lemma for the public action, but it is not marked `[simp]`, so users cannot simplify `p (h • e)` without naming the lemma or unfolding the subgroup action. _Fix:_ Add `@[simp]` to `Deck.proj_smul` unless there is a concrete loop risk, and keep `Deck.comp_eq` as the composition-level simp lemma.

### ✅ generality — approve  `codex/gpt-5.5`
The declarations are at the natural level for this stage: `Deck p` avoids unnecessary covering-map, continuity, or topology-on-base assumptions, while the homeomorphism action instances are exactly as general as the acting type permits.

### 🟡 placement — request_changes  `codex/gpt-5.5`
The deck group file is mostly scoped to deck transformations, but it also vendors a completely generic `Homeomorph` action in the UniversalCover namespace layer. That generic API should live in an earlier topology/homeomorph file and be imported here.
- `TauCeti/AlgebraicTopology/UniversalCover/Deck.lean:39` — `Homeomorph.applyMulAction`, `Homeomorph.smul_def`, `Homeomorph.applyFaithfulSMul`, and `Homeomorph.continuousConstSMul` are generic API for all self-homeomorphisms, not universal-cover or deck-transformation material. _Fix:_ Move these declarations to an earlier generic topology/homeomorph file, then import that file from `Deck.lean`.

### 🟡 naming — request_changes  `claude/claude-sonnet-4-6`
All Deck-namespace names and most vendored Homeomorph names follow Mathlib conventions correctly. One instance name deviates from the established Mathlib pattern: `Homeomorph.continuousConstSMul` should carry the `_apply` suffix to match `ContinuousLinearMap.continuousConstSMul_apply`, the only Mathlib analogue for a tautological-action continuity instance.
- `TauCeti/AlgebraicTopology/UniversalCover/Deck.lean:57` — `Homeomorph.continuousConstSMul` omits the `_apply` suffix used by the closest Mathlib analogue (`ContinuousLinearMap.continuousConstSMul_apply`, `Basic.lean:684`) for the same pattern: a `ContinuousConstSMul` instance arising from the tautological apply-action. _Fix:_ Rename to `Homeomorph.continuousConstSMul_apply` and update the docstring accordingly.

### 🟡 documentation — request_changes  `codex/gpt-5.5`
The new file has a useful module docstring, but its advertised main theorem is left undocumented at the declaration site.
- `TauCeti/AlgebraicTopology/UniversalCover/Deck.lean:79` — `Deck.proj_smul` is listed as a main result in the module docstring but has no declaration docstring explaining the statement. _Fix:_ Add a docstring to `Deck.proj_smul` saying that deck transformations preserve the projection, e.g. `p (h • e) = p e`.

### ✅ deprecation — approve  `codex/gpt-5.5`
No public TauCeti API is renamed, removed, or weakened by this PR; it only adds the new Deck API and imports it from the aggregate file.
