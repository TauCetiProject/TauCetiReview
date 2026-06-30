# Scope: roadmap fit and single topic

One question: does this PR belong in Tau Ceti now, as a single coherent unit? This angle may
`block`, and should fairly readily.

## Roadmap fit

**A refactor of already-merged code is in scope a priori.** Everything on `main` was reviewed
for roadmap fit when it was merged, so reworking it needs no fresh roadmap claim. If the PR only
refactors, relocates, renames, simplifies or re-proves, modestly generalises, or documents
material that — up to those changes — already exists on `main`, roadmap fit is automatically
satisfied: do not `request_changes` for a missing or unstated roadmap target. Judge by whether
the mathematics already existed, not by whether identifiers or file paths moved. The test below
applies only to genuinely *new* mathematical content: a definition, theorem, instance, or file
that adds a capability `main` did not have.

Tau Ceti implements the roadmaps in the `TauCetiProject/TauCetiRoadmap` repo, checked out for
you in the workspace. New material is in scope only if it advances a specific roadmap target, or
supplies a prerequisite a specific target needs. A valid claim identifies a roadmap file and
node or heading; read it (in the roadmap checkout) to confirm.

- The dependency must be real and proximate: you can see the path from this material to the
  named target. "Might be useful for", or a long speculative chain, is not a prerequisite.
- Building what is missing is the point, so do not reject genuine prerequisite
  infrastructure. Reject material on no path to any target, or justified only as interesting;
  if it is off-roadmap but plausibly worthwhile, `block` and say a human must add it to the
  roadmap first.
- Judge the path, not its mathematical adequacy. If scope turns on whether a prerequisite is
  strong enough or non-vacuous, leave that to correctness.

## Single topic

`block` and ask for a split when the PR is more than one topic: an opportunistic refactor of
prerequisite material bundled with new work, or several unrelated targets at once. A single
refactor that is itself the topic is fine.

## Verdict

- `block` when new material has no real path to a roadmap target, or the PR is not a single
  topic.
- `request_changes` when new material's path is genuine but the description fails to state it.
- `approve` when the PR reworks already-merged material as a single topic, or advances one
  target or one target's genuine prerequisite, as one unit.
