# Reviewing a PR yourself

CI reviews every Tau Ceti PR by calling the Anthropic and OpenAI **APIs**, which is metered and
adds up fast. `tauceti-review` lets a trusted person run the *same* review on their **own
Claude / Codex subscription** instead: the inference runs through the locally logged-in `claude`
and `codex` CLIs, so there is no per-token bill. It is the same engine, same rubrics, same
scoreboard and per-rubric threads — only the inference auth and who posts change.

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
- Python ≥ 3.10.

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
# one-off, no install:
uvx --from git+https://github.com/FormalFrontier/TauCetiReview tauceti-review 42

# or install the command:
uv tool install git+https://github.com/FormalFrontier/TauCetiReview
tauceti-review 42
```

Or from a checkout (also how to hack on it):

```bash
git clone https://github.com/FormalFrontier/TauCetiReview
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
tauceti-review 42 --no-mathlib          # skip the Mathlib clone (faster; weaker reuse checks)
```

It **defaults to a dry run**: it prints the scoreboard and each rubric's thread and posts nothing.
Add `--post` to publish. Useful flags:

| flag | effect |
|---|---|
| `--post` | post the scoreboard comment + per-rubric review threads to the PR, under your GitHub login |
| `--rubrics a,b,c` | review only these rubrics (default: all of them) |
| `--reviewer claude\|codex` | restrict to one reviewer (default: every one you have) |
| `--mode commit` | review only rubrics not already passing in the local store (default `manual` = all) |
| `--no-mathlib` | skip fetching pinned Mathlib source; `reuse`/`naming` can't grep Mathlib |
| `--repo owner/name` | review a different repo (default `FormalFrontier/TauCeti`) |
| `--auth api` | use `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from the env instead of the subscription (billed) |
| `--keep` | keep the temporary workspace for inspection |

## What it does

1. Reads the PR head SHA, diff, and description via `gh`.
2. Builds the same read-only reviewer workspace CI uses: the PR source at its head, the roadmap
   repo, and (unless `--no-mathlib`) the pinned Mathlib source for `reuse`/`naming` to grep.
3. Runs each rubric through `claude -p` / `codex exec`, **read-only** (`Read`/`Grep`/`Glob` only),
   in `--auth subscription` mode — no API key, so the CLIs use your logged-in subscription.
4. Reads each verdict from a fresh one-time marker token, so nothing in the PR text can forge an
   `approve` (this anti-forgery channel is kept even though you are trusted).
5. Prints the scoreboard + threads, and with `--post`, publishes them via `gh` as you.

## Notes

- **Cost line.** The scoreboard's `Review spend: $X` is a *notional* API-equivalent estimate from
  token usage. On a subscription you are **not** billed that — it is there so you can see what the
  same review would have cost on the API.
- **Who it posts as.** With `--post`, comments are created under your `gh` identity, not the review
  bot's, and as a fresh scoreboard comment (a local run keeps no state shared with CI, so it won't
  edit the bot's comment in place).
- **Subscription terms.** Driving a *personal* Claude/ChatGPT subscription as an automated reviewer
  is fine for occasional, interactive, human-initiated runs like this. Standing it up as a 24/7
  self-hosted auto-reviewer is closer to API-tier usage and likely outside subscription terms — if
  you want always-on review, use the CI path (`--auth api`) with API keys.
- **Determinism.** With both CLIs installed the reviewer is random per rubric, so two runs can
  differ on borderline rubrics — the same property the CI review has.
