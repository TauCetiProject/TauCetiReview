# Quality pass

Did the author run Tau Ceti's quality pass over its own diff, and report what it did? This angle
does not judge the code: the other angles do. It checks that the author attempted each step below
and said what changed. Uses `request_changes`; never `block`.

The checklist exists because these are the problems Tau Ceti's red team keeps finding on `main`
after review: proofs over 50 lines (the most common finding by far), restatements and rebuilds
of Mathlib or Tau Ceti declarations, hypotheses and typeclasses stronger than the proof needs,
`variable` instances carried by theorems that never use them, missing characteristic API that
callers then re-derive, slow declarations, and stale or inaccurate docstrings. Each is cheap for
the author to check while the code is fresh, and expensive to find later.

## For authors: the quality pass

Before you ask for review (and again after a large revision), work through every item below over
each declaration your PR adds or changes. Each item is an attempt, not a guarantee: do what is
reasonable, then say what you did. Then add a `## Quality pass` section to the PR description
with one line per item:

```text
## Quality pass
Q1: changed — deleted `foo_bar`, a restatement of Mathlib `Foo.bar`; uses repointed
Q2: checked — no change (searched TauCeti/ and the open PRs for the defining formula of `baz`)
Q3: changed — dropped `[NeZero n]` from `qux` (derived from `hn : 1 < n`)
Q4: n/a — no `variable` instances
...
```

Each line starts `changed —`, `checked — no change` or `n/a —`, followed by a short note: what
changed, or what you checked. List every item, Q1–Q9.

**Q1. Deduplicate against Mathlib.** For each new public declaration, search Mathlib for the
same statement or a more general one: by name pattern, and by statement shape (grep the main
constants of the conclusion, try `exact?`/`apply?`). If one exists, delete yours and use it at
every call site. A new name for a Mathlib lemma, or a one-line wrapper around one, is a duplicate.
A lemma about a Tau Ceti definition, or an `instance` registering a Mathlib theorem, is not.

**Q2. Deduplicate against Tau Ceti, on `main` and in its open pull requests.** Search `TauCeti/`
for the same object or lemma: grep the defining formula and the key constants, not only the name.
Look for a second construction of something another file already builds, a block copied from a
sibling file, and a lemma re-derived locally that is already imported. Reuse or generalise the
existing one instead.

Then do the same against the open pull requests to Tau Ceti, the work not on `main` yet: search
their titles and descriptions for your declarations' names and the key constants of your
statements, list the open pull requests that touch the same files or directories, and read the
diff of any that might add the same thing (for instance `gh pr list --repo TauCetiProject/TauCeti
--state open --search "<name> in:title,body"`, then `gh pr diff` on each candidate). If one adds
it, do not add a second copy. When it came first or is the better one, drop yours, or build on it
once it has merged (never stack on it). When yours is the better one, say so on both pull
requests, each linking the other, so that a person or the reviewers can choose. The note on Q2
says what you searched, on `main` and in the open pull requests.

**Q3. Weaken hypotheses.** For every hypothesis and instance binder of every theorem, including
those pulled in from `variable` lines:

- Delete it and rebuild. If the proof still works, leave it deleted.
- If it follows from the other hypotheses (`[NeZero n]` from `1 < n`, `[Finite K]` from a
  cardinality bound, `[Module.Free k M]` from a basis, `[T2Space G]` from a Lie group structure),
  derive it inside the proof instead of asking every caller for it.
- If the proof breaks only in a degenerate case (an empty type, `0`, `⊥`, the zero ring), handle
  that case and drop the hypothesis when the statement holds there for a real reason (an empty
  sum, an a.e. statement for the zero measure, contradictory hypotheses). Keep it when the
  statement would hold there only through a junk value (`x / 0 = 0`, `(0 : ℝ)⁻¹ = 0`,
  `ENNReal.toReal ⊤ = 0`, the `finrank` or `Nat.card` of something infinite being `0`).
- Try weaker typeclasses: `Field` → `CommRing` or `IsDomain`, `CommSemiring` → `Semiring`,
  `NontriviallyNormedField` → `NormedField`, `IsArtinianRing` → what the proof actually uses.
- A hypothesis a definition in the statement takes is needed to state it: leave it.
- Before you drop one, check the statement is the intended one. A hypothesis that looks unused can
  be the symptom of a conclusion weaker than the docstring promises; compare them.

Update every caller.

**Q4. Section variables.** No theorem should carry an instance from a `variable` line that it
does not use, and no definition should take a binder its body ignores. Use `omit [...] in`, or
restructure the `variable` blocks.

**Q5. Proofs of at most 50 lines.** Count each proof's code lines (not blank lines or comments).
For every proof over 50 lines, first look for a shorter route: a Mathlib lemma, `grind`,
`positivity`, `fun_prop`, a better induction principle. Otherwise split it at its natural stages
into lemmas of at most 50 lines each: a step with mathematical meaning becomes a public,
documented lemma in the right namespace and file; a proof-specific step becomes a private lemma
with a docstring. Keep the main statement unchanged. Do the same for long `def` and `instance`
bodies (move the proof obligations into lemmas), and turn any block repeated within or across
proofs into one lemma.

**Q6. Elaboration speed.** Time every new or changed declaration, for instance with the
`profiler` option set locally (never commit it) or by timing the file. For anything over about a
second, find the cause and fix it, keeping the statement unchanged and never raising
`maxHeartbeats`. Common causes: a broad `simp` or `simp_all` (use `simp only [...]` or a sharper
lemma), `decide` or `norm_num` on large terms, instance search through long chains (supply the
instance), `rfl` or `exact` forcing definitional unfolding (state an intermediate `have` with its
type, or prove an equation lemma), and `convert` on large terms. In a `module` file, check that
`#check @f.eq_1` from a non-module file returns at once for each new definition.

**Q7. API for new definitions.** Give each new definition its characteristic lemmas (`_apply`,
`_def`, `ext`, `@[simp]` lemmas whose left-hand side is in simp normal form so they actually
fire), so callers do not re-derive them. Inline a definition with no API that is used once.
Evaluate each new definition on degenerate inputs (empty, zero, the trivial ring): if it returns a
junk value there, make sure no theorem holds only because of it.

**Q8. Tidy each changed file.**

- The module docstring lists the main definitions and results actually in the file.
- Each public declaration's docstring is accurate: it states every hypothesis, does not
  overclaim, and points to the right reference.
- Imports are minimal. No `public import` is used only in proofs. No unused `open`, `classical`,
  `noncomputable`, or local instance.
- No commented-out code or leftovers (`#check`, `#eval`, trace options), and no double blank
  lines.
- `show`/`change` appear only with a comment explaining why, and nothing relies on accidental
  definitional equality across wrappers or coercions.
- No `by exact t` (write `t`), no single-use `have` that inlines cleanly, no non-terminal `simp`
  without `only`.
- Names follow Mathlib's conventions: they describe the conclusion and sit in the namespace dot
  notation wants. General helpers are public in the file where they belong, not `private` in a
  specific one.

**Q9. Re-read the statements.** Read each new statement as a mathematician would. Check it says
what the docstring and the source say, with nothing moved into the hypotheses. Check the
hypotheses are not contradictory, and the conclusion is not trivially true.

## Verdict

- `approve` when the PR description has a `## Quality pass` section with a line for each item
  Q1–Q9, each `changed`, `checked` or `n/a` with a short note. You do not judge whether the
  changes are good: other angles do. A PR that changes no Lean declaration (documentation,
  toolchain bumps) may answer every item `n/a`.
- `request_changes` when:
  - the section is missing;
  - an item is missing or has no note, or Q2's note does not say the open pull requests were
    searched;
  - or a line is plainly contradicted by the diff, which you can see without judgement. For
    example, Q5 says no proof is over 50 lines but the diff adds one (count it), or Q8 says no
    leftovers but the diff adds a `#check`.

  Quote the item and the contradicting line.
- Never `block`.
