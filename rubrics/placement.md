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
  subdirectory when one fits. A directory where several sibling files share a leading
  CamelCase name component is evidence of a subdirectory in the making; treat the prefix
  count as evidence only, never by itself grounds for `request_changes`. Ask for a
  dedicated relocation of the whole family (its own refactor PR) rather than requiring
  this PR to move unrelated files or to start a second layout beside the flat one.

## Imports

- Flag only an evidently wrong import: unused, or a broad `import Mathlib` where
  specific modules would do. Do not request a direct import for something already
  available transitively; that is redundant and `shake` removes it.

## Verdict

- `request_changes` for a declaration in the wrong home, material that belongs in an earlier
  file, roadmap-specific material hidden in a generic file, or an evidently wrong import.
- `approve` when each declaration is in its natural place and no import is unused or
  unnecessarily broad.
