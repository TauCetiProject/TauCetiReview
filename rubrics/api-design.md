# Public API

You judge the public interface the PR exposes. Uses `request_changes`.

- Expose what later stages of the roadmap need, the explicit products of the roadmap, and genuinely reusable general results. Keep an implementation helper `private` when it has no use outside the proof or file it serves. The roadmap's named targets are not the whole allowed surface, so do not ask for something to be `private` merely because it is not named there. Do not expose bodies to compensate for missing
  lemmas: keep bodies unexposed (no `@[expose]`) where possible unless a consumer must unfold or compute,
  and ask for the missing lemma instead. Recall that we can avoid making lemmas rely on defeq downstream by using `:= (rfl)` instead of `:= rfl`.
- A definition needs the API that characterizes it: introduction and elimination, the
  `*_def` and `mem_*_iff` restatements, interaction with the operations in scope, and the
  universal property where there is one. Try to use the new API without unfolding and demand any missing characteristic lemmas.
- A bundled definition must be **extensional on the object it denotes**: it exposes no data its
  laws leave unconstrained. If a structure field or indexed family is left free on inputs no
  operation or law actually uses, two terms that agree everywhere meaningful can still differ,
  so no `@[ext]` holds and equality and uniqueness reasoning are blocked for every consumer — a
  user-visible risk, not taste. Constrain or drop the free data: carry only what the laws use,
  and recover any wider view as a derived, canonically-determined accessor. Test: if `@[ext]`
  cannot be derived from agreement on the inputs the operations and laws actually use, the
  definition carries free data; require its removal.
- Require symmetric, dual, or parallel forms only when the file already develops both sides or
  the roadmap needs them.
- Annotate `@[simp]` the normal-form lemmas and `@[grind]` the lemmas that should drive
  `grind`. Flag a characteristic lemma that should carry one and does not, and an annotation
  that would loop or fire wrongly.

## Verdict

- `request_changes` for an over-exposed surface, a body exposed for want of API, an
  incomplete characteristic API, free data that defeats extensionality, or missing or wrong
  automation annotations.
- `approve` when the surface is minimal, bodies are hidden, and the characteristic API is
  complete and annotated.
