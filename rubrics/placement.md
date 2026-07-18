# Placement and imports

Where does the new material live, and what does it import? Uses `request_changes`. File
length is linter-enforced; do not re-report it. `shake` is not yet enforced, so report only
imports whose wrongness is evident from the diff or the dependency topic.

## Placement

- Each declaration belongs in its canonical home: the file whose topic, level, and
  dependencies fit it, near the definition or result it elaborates. If it belongs in an
  earlier `TauCeti/` file, or depends on no later theory and is broadly useful, ask to move it
  there.
- Reject generic placement for declarations whose hypotheses or names are roadmap-specific:
  do not let roadmap-specific lemmas masquerade as reusable by living in a generic file.
- New files join the tree the way the surrounding tree already does: into an existing topic
  subdirectory when one fits. When a PR adds a file to an already-large flat filename
  family (several siblings sharing a leading CamelCase component), the expected shape is
  restructure-as-you-add: the same PR moves the family into its subdirectory (mechanical
  `git mv` plus import/header updates, any anchor `Foo.lean` kept in place, no invented
  `Basic.lean`, zero declaration renames) and places the new file there.
  `request_changes` when such a PR instead extends the flat family or starts a second
  layout beside it, unless the PR documents an open PR still importing the old module
  names, in which case the flat addition is correct and the family belongs on the
  structure tracking issue. Treat the prefix count as evidence, not arithmetic: the
  reviewer judges the topic boundary, and a restructure bundled with the file that
  triggered it is one topic, not opportunistic bundling.

## Imports

- Flag only an evidently wrong import: unused, or a broad `import Mathlib` where
  specific modules would do. Do not request a direct import for something already
  available transitively; that is redundant and `shake` removes it.

## Verdict

- `request_changes` for a declaration in the wrong home, material that belongs in an earlier
  file, roadmap-specific material hidden in a generic file, or an evidently wrong import.
- `approve` when each declaration is in its natural place and no import is unused or
  unnecessarily broad.
