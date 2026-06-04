<!--tauceti-scoreboard-->
## AI review — blocked

Each rubric is judged independently by Opus or Codex; only integrity angles can block. See the [rubrics](https://github.com/FormalFrontier/TauCetiReview/tree/main/rubrics).

| | rubric | state | judge | summary |
|---|---|---|---|---|
| ⛔ | scope | blocked | `codex/gpt-5.5` | The PR is both explicitly off-roadmap and not a single coherent topic. It should not land without a roadmap target and substantial splitting. |
| ⛔ | correctness | blocked | `claude/claude-opus-4-8` | Two of the three declarations are logically defective: curvature_gauge_transformation's conclusion is its own hypothesis verbatim (pure tautology, the law is assumed), and IsCompactOrientedManifold encodes orientation as the placeholder field `oriented : True`. The third (uniform_boundedness_principle) is a faithful restatement of Mathlib's banach_steinhaus. |
| ⛔ | reuse | blocked | `claude/claude-opus-4-8` | uniform_boundedness_principle is a thin ℝ-specialized wrapper whose proof is literally `banach_steinhaus h`, directly replaceable by Mathlib's `banach_steinhaus`. IsCompactOrientedManifold adds nothing over `CompactSpace` since its `oriented` field is `True`. |
| 🟡 | attribution | changes requested | `claude/claude-opus-4-8` | The Atlas source is credited in the module docstring and description, satisfying the core attribution requirement. But the file header stamps these adapted formalizations with 'Copyright (c) 2026 Lean FRO, LLC' under Apache 2.0, which misattributes copyright for material the PR itself says is adapted from Meta's facebookresearch/atlas-lean and may violate that project's own license. |
| 🟡 | api-design | changes requested | `codex/gpt-5.5` | The PR exposes public declarations that are either tautological, misleading, or duplicate existing Mathlib API. None has the characteristic public interface required for a downstream TauCeti target. |
| 🟡 | generality | changes requested | `codex/gpt-5.5` | The added declarations are not at a natural Mathlib API level: they include unused or over-strong assumptions, an over-broad fake manifold class, and a special-case wrapper around existing Mathlib API. |
| 🟡 | placement | changes requested | `codex/gpt-5.5` | The new material is placed in a generic catch-all file rather than canonical topic homes, and it mixes unrelated algebraic geometry/gauge, topology/manifold, and functional analysis declarations under one import context. |
| 🟡 | naming | changes requested | `claude/claude-opus-4-8` | All three names advertise mathematical content their statements lack: a curvature/gauge law that only restates a hypothesis, an "oriented manifold" class over a bare topological space with a vacuous orientation field, and a rename of Mathlib's banach_steinhaus. |
| 🟡 | documentation | changes requested | `codex/gpt-5.5` | The added file has module and declaration docstrings, but two of them materially overclaim what the Lean declarations state. |
| 🟡 | proof-quality | changes requested | `claude/claude-opus-4-8` | Proofs are all single robust steps with no brittle chains, change/show, or defeq reliance. Only proof-quality defect: a redundant hypothesis in curvature_gauge_transformation. |
| ✅ | deprecation | approved | `claude/claude-opus-4-8` | Purely additive PR introducing one new file; no existing public declaration is renamed, removed, or weakened, and there is no Mathlib bump. Nothing to flag on the deprecation/backward-compatibility angle. |

♻️ = approved on an earlier commit, re-run before merge. Spent today: $1.11/$10.