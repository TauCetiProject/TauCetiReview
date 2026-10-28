# Reviewing a PR yourself

Tau Ceti has a CI path that reviews with the metered Anthropic and OpenAI **APIs**, but production
review generation is currently disabled there to conserve that budget. Reviews instead come from
the trusted operator-run worker and from ad hoc command-line runs. `tauceti-review` lets a trusted
person run the same review on their **own Claude / Codex / Kiro subscription**: the inference runs
through the locally logged-in provider CLI, so there is no per-token bill. It is the same engine,
same rubrics, same scoreboard and per-rubric threads — only the inference auth and who posts change.

This is for people the project already trusts (maintainers, regular contributors). The tool is
read-only and posts under *your* GitHub identity, but nothing stops a reviewer from rubber-stamping
— the safeguard is social, not technical. See [SECURITY.md](SECURITY.md) for the threat model the
CI harness defends and which parts a local run deliberately drops.

## Prerequisites

On your `PATH`, all logged in:

- `git`
- [`gh`](https://cli.github.com/) — run `gh auth login` (this identity posts the review and reads
  the PR).
- `claude` ([Claude Code](https://www.npmjs.com/package/@anthropic-ai/claude-code)) signed into a
  Claude subscription, and/or `codex` ([Codex](https://www.npmjs.com/package/@openai/codex)) signed
  into a ChatGPT subscription. You need **at least one**; each rubric is judged by whichever you
  have. With both, the reviewer is drawn per rubric, like CI.
- For explicit Kiro reviews, `kiro-cli` signed in with `kiro-cli login` (or a
  headless `KIRO_API_KEY`). Kiro is never auto-drawn.
- Python ≥ 3.10.

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
# one-off, no install:
uvx --from git+https://github.com/TauCetiProject/TauCetiReview tauceti-review 42

# or install the command:
uv tool install git+https://github.com/TauCetiProject/TauCetiReview
tauceti-review 42
```

An installed command stays at the version you installed (`uv tool upgrade tauceti-review` to update).
Scoreboards you post show its commit as `CLI @ abc1234` in the footer, so you can check it. This
needs a `git+https` install or a git checkout (where uncommitted changes to tracked files add
`(modified)`); a `pip install .` copy has no commit to show.

Before each review the CLI also compares the rubrics it will use with the published `main`. If they
differ (a stale `--repo-dir` or `$TAUCETI_REVIEW_DIR` checkout, a `--rubrics-sha` pin, or local
edits) it warns, and the posted scoreboard and review threads say so, so PR authors know a finding
may rest on rules that have since changed. That is expected while you test a rubric change;
otherwise, update the checkout.

Or from a checkout (also how to hack on it):

```bash
git clone https://github.com/TauCetiProject/TauCetiReview
cd TauCetiReview
uv run tauceti-review 42          # or: pipx install . / pip install .
```

The rubrics and the review engine always come from a TauCetiReview checkout — the one you ran from
if it is one, otherwise a cached shallow clone under `~/.cache/tauceti-review` that refreshes each
run — so the rubrics never drift from the engine.

## Use

```bash
tauceti-review 42                       # review PR #42, PRINT the verdicts — posts nothing
tauceti-review 42 --post                # also post the scoreboard + threads, as you
tauceti-review 42 --rubrics scope,correctness,reuse
tauceti-review 42 --reviewer claude     # use only Claude even if both are installed
tauceti-review 42 --reviewer claude --claude-model claude-fable-5-1
tauceti-review 42 --reviewer kiro --kiro-model gpt-5.6-sol
tauceti-review 42 --reviewer kiro --kiro-model claude-opus-5
tauceti-review 42 --reviewer deepseek   # use DeepSeek via OpenRouter + the `pi` agent
tauceti-review 42 --no-mathlib          # skip the Mathlib clone (faster; weaker reuse checks)
```

It **defaults to a dry run**: it prints the scoreboard and each rubric's thread and posts nothing.
Add `--post` to publish. Useful flags:

| flag | effect |
|---|---|
| `--post` | post the scoreboard comment + per-rubric review threads to the PR, under your GitHub login |
| `--rubrics a,b,c` | review only these rubrics (default: all of them) |
| `--reviewer claude\|codex\|kiro\|sonnet\|deepseek\|minimax\|grok` | restrict to these reviewers (default: every auto-drawn one you have — `claude` and `codex`). Direct `claude` defaults to exact `claude-opus-5-5`; override with `--claude-model`. `kiro` is explicit-only and always uses the exact `--kiro-model`. `sonnet` is the `claude` CLI pinned to Sonnet. `deepseek`/`minimax`/`grok` run an OpenRouter model through the [`pi`](https://github.com/badlogic/pi-mono) agent and need `pi` on PATH + `OPENROUTER_API_KEY`. All but `claude`/`codex` are explicit-only (never auto-drawn) |
| `--claude-model MODEL` | exact direct-Claude model ID, also configurable through `TAUCETI_CLAUDE_MODEL` for managed workers. The flag takes precedence. Unset: use the selected engine's default. A model with no entry in `runner/prices.json` still runs, billed at the newest rate on its own model line; its exact ID is recorded in review provenance |
| `--kiro-model MODEL` | exact Kiro model ID; defaults to `gpt-5.6-sol`. Use `claude-opus-5` for Kiro's current Opus |
| `--mode manual` | re-run every rubric. The default, `commit`, reuses a completed published review of the current head across workers. A new unanswered author contest reopens its rubric while retaining the other published decisions. Missing history still permits ordinary investigation. Incomplete or different-head reviews retain the existing stale-approval and review rules |
| `--no-mathlib` | skip fetching pinned Mathlib source; `reuse`/`naming` can't grep Mathlib |
| `--repo owner/name` | review a different repo (default `TauCetiProject/TauCeti`) |
| `--auth api` | use the matching `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `KIRO_API_KEY` instead of a browser login |
| `--keep` | keep the temporary workspace for inspection |

## What it does

1. Reads the PR head SHA, diff, and description via `gh`.
2. Builds the same read-only reviewer workspace CI uses: the PR source at its head, the roadmap
   repo, and (unless `--no-mathlib`) the pinned Mathlib source for `reuse`/`naming` to grep.
3. Runs each rubric through `claude -p`, `codex exec`, exact-model `kiro-cli chat`, or the `pi`
   agent against OpenRouter, **read-only** (`Read`/`Grep`/`Glob`, or pi's `read`/`grep`/`ls` —
   no shell, no writes). In `--auth subscription` mode Claude, Codex, and Kiro use the logged-in
   subscription; the OpenRouter reviewers are pay-per-token and use
   `OPENROUTER_API_KEY`. Each reviewer runs in a **clean room**: a throwaway HOME seeded with only
   its own credential, so it ignores your personal `CLAUDE.md` / `AGENTS.md`, skills, plugins, and
   settings (and those are disabled outright). The review depends on the rubrics and the PR, not
   on who runs it.
4. Reads each verdict from a fresh one-time marker token, so nothing in the PR text can forge an
   `approve` (this anti-forgery channel is kept even though you are trusted).
5. Prints the scoreboard + threads, and with `--post`, publishes them via `gh` as you.

If a reviewer's sandbox fails or its verdict summary reports incomplete source inspection,
the round aborts before publication, even if
the model says `approve`. No review scoreboard or finding threads are posted. Spend and an
errored case file remain recorded; repair the local toolchain and retry. Claude and Codex
tool outcomes are checked independently of the model's verdict.
The prompt includes the PR diff, so a review may need no tool calls. Tool counts are recorded
for diagnostics and do not determine whether a verdict is accepted.

Codex tool commands inherit no provider credentials or personal environment. The runner
restores `PATH`, which the Linux sandbox helper also uses to find the system `bwrap`.
Without it, Codex always falls back to its bundled helper: an installation without that
resource fails even if `bwrap` is installed, and the bundled executable may not have the
system executable's AppArmor permissions. The runner also creates a private per-attempt
`TMPDIR` for the launcher's writable bookkeeping; a read-only system `/tmp` otherwise
prevents it from starting. These are the only forwarded runtime variables. The model's
tools remain read-only, including inside that temporary directory.

To diagnose a failing Codex host, run the following **inside the same container or worker
environment** that runs reviews, from a writable review workspace:

```sh
uvx --refresh --from git+https://github.com/TauCetiProject/TauCetiReview tauceti-review-check-sandbox
```

This calls the native Codex sandbox directly in the reviewer's clean environment, without
credentials, model inference, or GitHub access. It checks that a known file can be read and
workspace writes are denied, then removes its scratch files. The JSON report includes the
CLI version, relevant Linux kernel settings, and the actual launcher/sandbox error. A failed
report does not relax the sandbox. Linux namespace or AppArmor restrictions, missing runtime
helpers, and macOS Seatbelt errors must be investigated on the affected host; a successful
probe on another host does not diagnose them.

Add `--compare-path` to run an empty-PATH probe as well. If the current probe succeeds
and `without_path` fails, the report sets `path_restoration_fixed_sandbox: true`. Both probes
check read access and write denial, and neither calls a model or falls back to unrestricted
execution. Add `--compare-tmpdir` to test the system temporary directory as well; the report
sets `private_tmpdir_fixed_sandbox: true` if only the private temporary directory works.
A system-level denial of namespace creation can still require a host-specific fix;
compare the actual stderr before changing container or AppArmor policy. Nested sandboxing
can work when the outer policy permits it; launching from another agent alone does not
establish the cause.

Comment publication keeps two-second spacing. GitHub's specific HTTP 422
`was submitted too quickly` rejection retries the same comment after 60, 120, and 240 seconds,
with up to five seconds of jitter and any longer `Retry-After` delay. Other validation errors
are not retried. If publication still fails, the saved plan and confirmed comment IDs remain
available for automatic publication repair without running the models again.

The posted scoreboard is the live verdict consumed by auto-merge: both merge paths read the newest
marked scoreboard and require it to name the current head. The engine also writes detailed records
for the TauCetiData analytics/provenance archive, but archive publication is not part of the merge
gate and a contributor needs no TauCetiData write access for a posted review to count.

## Notes

- **Cost line.** For Claude/Codex, the scoreboard's `Review spend: $X` is a *notional*
  API-equivalent estimate from token usage. Kiro 2.x exposes no per-turn token telemetry, so Kiro
  subscription runs are recorded at $0 rather than assigned a fictional API price.
- **Who it posts as.** With `--post`, comments are created under your `gh` identity, not the review
  bot's, and as a fresh scoreboard comment (a local run keeps no state shared with CI, so it won't
  edit the bot's comment in place). It does read the PR's newest scoreboard, to show the approvals
  there as ♻️ rather than re-run them while other rubrics are still blocking. The authenticated login is also recorded as `submitted_by` in
  the scoreboard's hidden provenance so cooperating workers can give that reviewer first refusal on
  the next head.
- **Subscription terms.** Driving a personal Claude/ChatGPT/Kiro subscription as an automated reviewer
  is fine for occasional, interactive, human-initiated runs like this. Standing it up as a 24/7
  self-hosted auto-reviewer is closer to API-tier usage and likely outside subscription terms — for
  always-on review, enable Tau Ceti's CI path and use `--auth api` with API keys.
- **Reproducibility.** The clean room means your personal `~/.claude/CLAUDE.md`, `~/.codex/`
  config/`AGENTS.md`, skills, and MCP servers do **not** influence the review — two people running
  the same rubrics on the same PR get reviews that differ only by the model, not by their local
  setup. (The repo's own in-tree `CLAUDE.md` is still visible, as part of the code under review.)
  On macOS, where the login lives in the keychain rather than a credential file, it falls back to
  your real HOME and prints a note; pass `--auth api` with a key for a guaranteed clean room there.
- **Determinism.** With both CLIs installed the reviewer is random per rubric, so two runs can
  differ on borderline rubrics — the same property the CI review has.
- **Concurrent reviewers.** Before spending inference, a contributing run acquires a renewable
  Git lease for the repository, PR and exact head. Another holder makes the run yield without
  inference (exit status 81), regardless of model. Leases release on exit and expire after crashes.
  Set `CLAIM_REPO` to an accessible claims repository; otherwise existing shared access or a direct
  personal fork is used. No repository is created automatically. Git outages allow work to proceed
  with a warning about possible duplicates. `--no-coordinate` opts out; shadow runs opt out automatically.
  Old review-in-progress comments are ignored and no longer published or deleted.

## Shadow reviews (A/B arms)

A shadow review runs the same PR through alternative rubrics and/or models, archives the
results to [TauCetiData](https://github.com/TauCetiProject/TauCetiData), and posts **nothing**
— the PR thread and the production review state are untouched. This is how review variants are
evaluated against each other before being adopted.

    tauceti-review 139 --shadow --label deepseek-arm --reviewer deepseek
    tauceti-review 139 --shadow --label rubrics-v2 --rubrics-sha <TauCetiReview commit>

`--rubrics-sha` pins the rubrics *and* the engine to that commit (a cached per-SHA checkout),
so an arm reruns exactly the code that existed then. Arms always run every requested rubric
fresh (`--mode manual` semantics, scratch store, no carried-forward case files) so that two
arms over the same `(PR, head, rubric)` are comparable; records land with `arm: shadow:<label>`
and pair up with the production run in TauCetiData's `ab_pairs` view. In CI, the
`shadow-review` workflow (manual dispatch) does the same with API keys and a per-run budget.

### Publication traffic

All rubric results, findings, contest answers and scoreboard metadata remain.
For example, a publication repair that already set a finding's closed body used
to PATCH that identical body again; it now skips that PATCH, while still resolving
the thread if needed. Existing exact-root adoption, reply deduplication and
scoreboard reuse remain in place. No per-rubric detail is removed.

Standalone clients space comment sends and retry the specific captured
422 anti-abuse responses with bounded exponential delay. Under an updated Worker
transport, shared account-wide pacing and retry handling replace that local loop,
so delays do not multiply. Newly created findings are still published individually;
batched reviews are deferred because partial-response reconciliation would add a
second publication protocol. Blocker roots still land before the scoreboard.

The shared lease implementation is packaged in TauCetiTools. Operators using an
explicit claim repository keep using `CLAIM_REPO`; otherwise the helper checks
existing shared access and an existing direct fork. The worker passes its already
resolved namespace, avoiding repeated access discovery in review subprocesses.
