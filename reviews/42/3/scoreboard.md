<!--tauceti-scoreboard-->
## AI review — changes requested

Each rubric is judged independently by Opus or Codex; only integrity angles can block. See the [rubrics](https://github.com/FormalFrontier/TauCetiReview/tree/main/rubrics).

| | rubric | state | judge | summary |
|---|---|---|---|---|
| ✅ | scope | approved | `claude/claude-opus-4-8` | Ports Stage 0.1 (#31576, discreteness of homotopy-class fibres / SLSC typeclass) and Stage 0.2 (#38292, BasedPath space and endpoint-preimage path components) named explicitly in the UniversalCovers roadmap, plus a prelude vendoring pending-Mathlib API. The three files are one coherent prerequisite dependency stack toward the single universal-cover target. |
| ✅ | correctness | approved | `codex/gpt-5.5` | No correctness or faithfulness issues found in the new based-path, SLSC, and universal-cover prelude declarations under this rubric. |
| 🟡 | reuse | changes requested | `codex/gpt-5.5` | One public prelude helper creates a parallel path-connectedness API instead of using the existing `JoinedIn` extraction API directly. |
| 🟡 | attribution | changes requested | `claude/claude-opus-4-8` | The files carry source-PR credit, but UniversalCoverPrelude.lean — which vendors the subpathOn/codRestrict API, interval partitions, IsPathConnected.exists_path, and Path.Homotopic.of_trans_symm — credits only #38292, while the PR description attributes exactly those declarations to #31449. The #31449 citation sits instead in SemilocallySimplyConnected.lean, which only consumes that material. |
| 🟡 | api-design | changes requested | `codex/gpt-5.5` | The PR exposes too much implementation detail and relies on unfolded bodies where characteristic API should be provided instead. Several core constructor endpoint lemmas are also missing normal-form simp annotations. |
| 🟡 | generality | changes requested | `codex/gpt-5.5` | Several exported declarations are stated above their natural level: local SLSC data is promoted to a global typeclass, one helper carries an unused global hypothesis, and `TubeData` has irrelevant endpoint parameters. |
| ✅ | placement | approved | `claude/claude-opus-4-8` | The three files sit under AlgebraicTopology/FundamentalGroupoid/, mirroring Mathlib's SimplyConnected home; they are prerequisites correctly placed above the roadmap's reserved UniversalCover/ construction directory. Imports are specific and directly justified (e.g. Topology.Order.Basic for exists_Ioc_subset_of_mem_nhds'). |
| ✅ | naming | approved | `codex/gpt-5.5` | The previously reported names have been corrected in the current diff, and the public declarations now use standard semilocally-simply-connected/path-homotopy terminology without overstating their conclusions. No new notation is introduced. |
| 🟡 | documentation | changes requested | `claude/claude-opus-4-8` | Documentation is otherwise accurate and complete after the prior fixes. One stale cross-reference remains in the proof-strategy comment. |
| 🟡 | proof-quality | changes requested | `claude/claude-opus-4-8` | Automation is generally robust, but several goals are closed by `change` across Subtype-order, uncurry, and structure-projection definitional equality; some have no comment and the rest document what they do rather than why no lemma route exists. |
| ✅ | deprecation | approved | `claude/claude-opus-4-8` | Purely additive PR: three new files in a new directory, no modification/rename/removal/weakening of existing public API and no Mathlib bump. Nothing in the deprecation/backward-compatibility angle is at risk. |

♻️ = approved on an earlier commit, re-run before merge.

<sub>Review spend: $13.03.</sub>