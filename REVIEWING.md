# Reviewing a PR yourself

Tau Ceti has a CI path that reviews with the metered Anthropic and OpenAI **APIs**, but production
review generation is currently disabled there to conserve that budget. Reviews instead come from
the trusted operator-run worker and from ad hoc command-line runs. `tauceti-review` lets a trusted
person run the same review on their **own Claude / Codex / Kiro subscription**: the inference runs
through the locally logged-in provider CLI, so there is no per-token bill. It is the same engine,
same rubrics, same scoreboard and per-rubric threads — only the inference auth and who posts change.
Claude can also use **Amazon Bedrock**, billed to your AWS account; see below.

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
| `--reviewer claude\|codex\|kiro\|sonnet\|deepseek\|minimax\|grok` | restrict to these reviewers (default: every auto-drawn one you have — `claude` and `codex`). Direct `claude` defaults to exact `claude-opus-5`; override with `--claude-model`. `kiro` is explicit-only and always uses the exact `--kiro-model`. `sonnet` is the `claude` CLI pinned to Sonnet. `deepseek`/`minimax`/`grok` run an OpenRouter model through the [`pi`](https://github.com/badlogic/pi-mono) agent and need `pi` on PATH + `OPENROUTER_API_KEY`. All but `claude`/`codex` are explicit-only (never auto-drawn) |
| `--claude-model MODEL` | exact direct-Claude model ID, also configurable through `TAUCETI_CLAUDE_MODEL` for managed workers. The flag takes precedence. Unset: use the selected engine's default. The model must have an entry in `runner/prices.json`; its exact ID is recorded in review provenance |
| `--kiro-model MODEL` | exact Kiro model ID; defaults to `gpt-5.6-sol`. Use `claude-opus-5` for Kiro's current Opus |
| `--mode commit` | review only rubrics not already passing in the local store (default `manual` = all) |
| `--no-mathlib` | skip fetching pinned Mathlib source; `reuse`/`naming` can't grep Mathlib |
| `--repo owner/name` | review a different repo (default `TauCetiProject/TauCeti`) |
| `--auth api` | use the matching `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `KIRO_API_KEY` instead of a browser login |
| `--auth bedrock` | use AWS credentials for Claude/Sonnet; billed to AWS |
| `--keep` | keep the temporary workspace for inspection |

### Amazon Bedrock

With Claude Code, AWS CLI v2 supporting `configure export-credentials`, and an AWS profile
that can invoke the pinned Opus reviewer:

```bash
export AWS_PROFILE=tauceti
export AWS_REGION=us-east-1
tauceti-review 42 --auth bedrock --reviewer claude --post
```

Set `AWS_REGION` (or `AWS_DEFAULT_REGION`) to a region available to your account. No Claude
subscription login is needed. `--auth bedrock` is explicit and supports only `claude` and
`sonnet`; an ambient `CLAUDE_CODE_USE_BEDROCK`, including `true` or `yes`, cannot change
`--auth api` or `--auth subscription` into a billed AWS run.

The trusted parent uses
[`aws configure export-credentials`](https://docs.aws.amazon.com/cli/latest/reference/configure/export-credentials.html)
to resolve the selected profile before creating the reviewer's throwaway HOME. Profile files,
credential processes, SSO caches, and role credentials remain on the parent side: only the resolved
access key, secret, and optional session token enter the reviewer environment. `AWS_CONFIG_FILE`
and `AWS_SHARED_CREDENTIALS_FILE` are respected by the parent, but never forwarded. An explicit
`AWS_PROFILE` or `AWS_DEFAULT_PROFILE` selects that profile even if ambient access keys exist.
For SSO, log in with `aws sso login --profile tauceti` before starting. Resolution happens before
each attempt, so a long worker run can obtain fresh credentials; credentials do **not** refresh
inside an individual model attempt. An expired SSO login must be renewed by the operator.

Alternatively, set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (plus `AWS_SESSION_TOKEN`
when applicable) with neither profile variable set, or set `AWS_BEARER_TOKEN_BEDROCK`.
Those two routes do not require the AWS CLI. A Bedrock bearer token takes precedence over
other AWS credentials. The reviewer cannot fall back to instance metadata, and receives no
AWS profile paths, source-role settings, or credential-helper commands.

The engine passes its exact `--claude-model` to Claude Code; `ANTHROPIC_DEFAULT_OPUS_MODEL`
does not select this reviewer and is not forwarded. Records retain the requested model,
the CLI's `modelUsage` model IDs when reported, and `auth: bedrock`. Credential-resolution
failures and AWS authentication errors abort through the provider-down path without posting
a blocking scoreboard.

This isolates configuration and limits the credentials deliberately passed to a reviewer;
it is **not** a filesystem sandbox. Read tools can still reach files accessible to the worker's
OS user. Use a dedicated worker identity with narrowly scoped AWS permissions and no unrelated
credentials. See [SECURITY.md](SECURITY.md#residual-risks-knowingly-accepted).

## What it does

1. Reads the PR head SHA, diff, and description via `gh`.
2. Builds the same read-only reviewer workspace CI uses: the PR source at its head, the roadmap
   repo, and (unless `--no-mathlib`) the pinned Mathlib source for `reuse`/`naming` to grep.
3. Runs each rubric through `claude -p`, `codex exec`, exact-model `kiro-cli chat`, or the `pi`
   agent against OpenRouter, **read-only** (`Read`/`Grep`/`Glob`, or pi's `read`/`grep`/`ls` —
   no shell, no writes). In `--auth subscription` mode Claude, Codex, and Kiro use the logged-in
   subscription; the OpenRouter reviewers are pay-per-token and use
   `OPENROUTER_API_KEY`. API and Bedrock reviewers use a throwaway HOME with only their selected
   credential. Subscription reviewers copy the login credential when available, but Claude and
   Codex fall back to personal configuration paths when it cannot be copied; see the isolation
   caveat below. A throwaway HOME does not itself restrict filesystem reads.
4. Reads each verdict from a fresh one-time marker token, so nothing in the PR text can forge an
   `approve` (this anti-forgery channel is kept even though you are trusted).
5. Prints the scoreboard + threads, and with `--post`, publishes them via `gh` as you.

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
  edit the bot's comment in place). The authenticated login is also recorded as `submitted_by` in
  the scoreboard's hidden provenance so cooperating workers can give that reviewer first refusal on
  the next head.
- **Subscription terms.** Driving a personal Claude/ChatGPT/Kiro subscription as an automated reviewer
  is fine for occasional, interactive, human-initiated runs like this. Standing it up as a 24/7
  self-hosted auto-reviewer is closer to API-tier usage and likely outside subscription terms — for
  always-on review, enable Tau Ceti's CI path and use `--auth api` with API keys.
- **Configuration and credential isolation.** A throwaway HOME avoids loading personal
  configuration by default. In subscription mode, if the Claude or Codex credential file cannot
  be copied (for example, a keychain login), the runner falls back to the real HOME or CODEX_HOME.
  That fallback exposes personal configuration and credential files: it loses the intended
  credential boundary as well as reproducibility. API and Bedrock modes do not use that fallback,
  but unrestricted filesystem reads remain a risk in every mode. The repository's own in-tree
  instructions remain visible as part of the code under review.
- **Determinism.** With both CLIs installed the reviewer is random per rubric, so two runs can
  differ on borderline rubrics — the same property the CI review has.
- **Concurrent reviewers.** Before spending inference, a contributing run (one that posts or
  archives) posts a short-lived `review in progress` comment scoped to the `head` alone and checks
  for one already there. If another reviewer already holds the commit, this run skips it entirely —
  so a commit is reviewed once regardless of model, and a fleet never pays twice. A *different* model
  is not a distinct unit (the first claimer wins); only a new push, being a fresh head, is a fresh
  unit. Simultaneous claimers wait five seconds for GitHub's comment replicas to settle, then the
  lowest comment id wins. The marker self-expires (a crashed reviewer never blocks anyone) and is
  deleted when done.
  It needs only the ability to comment, so an independent reviewer with no repo write still
  coordinates. Pass `--no-coordinate` for a private read-only pass that touches the PR not at all
  (at the cost of possible duplicate spend); a `--shadow` arm opts out automatically.

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
