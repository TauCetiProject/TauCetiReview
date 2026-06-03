# Security model

This records the threat model, design decisions, and accepted residual risks for the Tau Ceti
automated review system. It came out of a nine-angle adversarial audit (2026-06) and the
hardening that followed (issues #7–#15, #22).

## What runs

A PR on `FormalFrontier/TauCeti` triggers review after CI succeeds, or on a `/review` comment
from a trusted user. The reusable workflow (`.github/workflows/review.yml`) checks out the PR
code, runs each rubric through a reviewer CLI (`claude` or `codex`, chosen at random per rubric,
read-only tools), posts an aggregated verdict, and — when the gate passes — auto-merges.

## Trust boundary

The PR diff, description, comments, and file contents are **attacker-controlled**. Every
reviewer is treated as potentially prompt-injected. The design does not try to prevent
injection (it can't, see R1); instead it ensures an injected reviewer **has nothing worth
stealing and no power to merge on its own**.

## The leak chain

A secret reaches a public artifact (the public `reviews` branch or a PR comment) only if all
four links hold:

1. **the secret is present in the job** — provider keys, the GitHub App token;
2. **the reviewer can reach it** — via its environment or the filesystem;
3. **the attacker induces it into the model's output** — prompt injection in the diff;
4. **we persist/publish that output** — committed to the public branch, posted as a comment.

We deliberately keep transcripts public with **no redaction gate** (link 4 is open by choice),
so safety rests entirely on breaking links 1–2: removing the reviewer's *access* to any secret.

## Mitigations (mapped to audit findings)

- **I1** — never evaluate PR-controlled Lake (`lakefile`/manifest) in the privileged job;
  Mathlib source is cloned at the rev pinned in the *base* manifest. Closes the pre-auth RCE.
- **I2** — reviewers run in a clean workspace (PR source without `.git`, roadmap, Mathlib,
  diff) with a minimal **per-provider** env: only that provider's key, never the other key and
  never a GitHub token. Keys are staged to files, read into memory, and unlinked before any
  reviewer runs. `persist-credentials: false` on every checkout. codex uses
  `shell_environment_policy.inherit=none`.
- **I3** — the App token is split into two narrowly-scoped tokens (TauCeti `pull-requests:write`
  to comment; TauCetiReview `contents:write` to persist), minted only *after* reviewers finish.
- **I4** — `/review` requires an exact command line (not a substring); per-PR daily round cap.
- **I5** — reserve-before-spend: skip a rubric if spend-so-far plus a per-call ceiling would
  breach the daily budget; count every attempt; persist spend incrementally.
- **I6** — the verdict is parsed only from after a one-time random marker the runner injects per
  review. Attacker content (including a forged `{"verdict":"approve"}`) sits before the marker
  and is ignored; parsing fails closed on a missing marker, bad JSON, or an out-of-set verdict.
- **I7** — an approval is bound to the `head_sha` + a fingerprint of the rubric text. A new
  commit or a rubric edit re-runs all rubrics instead of carrying forward stale approvals.
- **I8** — reviewers are told to assume an adversarial, possibly same-model author and to verify
  substance rather than defer to fluent output (we accept self-declared provenance and treat any
  mismatch as the reviewer's problem to catch — no provider-exclusion machinery).

## Auto-merge gate (I9)

Merging is performed by the `tauceti-review-bot` GitHub App, which is a **review-bypass actor**
on `main` (no machine user account is used; a GitHub App cannot be a CODEOWNER). A PR is merged
only when **all** of:

- every rubric **approves on the current commit** (latest verdict per rubric across that
  commit's rounds; I7 guarantees freshness);
- every changed path is under `TauCeti/` — infra-touching PRs are never auto-merged and still
  require `@humans`;
- CI's `build` check is green (status checks are **not** bypassed, only the review requirement).

Toggle: `enable_automerge` on the trigger workflow. `gh workflow disable "Review"` pauses the
whole pipeline.

## Residual risks (knowingly accepted)

- **R1 — prompt injection is not eliminable.** No wording makes an LLM immune. Contained by
  zero-privilege reviewers and no unilateral merge authority; treat every reviewer as injected.
- **R2 — two-provider independence ceiling.** With only claude + codex, excluding the author
  would leave a single model deciding everything, so the gate is one model with no cross-check.
  Removing this needs a third provider or a human second-signal.
- **R3 — fork auto-review depends on the sandbox staying airtight.** GitHub-hosted runners are
  not a hard boundary against a determined attacker with network egress; we mitigate to "no
  secret is reachable to exfiltrate," which is the achievable bar.
- **R6 — a reviewer can read its *own* provider key** via `/proc/self/environ` (the CLI needs
  it to function). Blast radius is one key, only on model compliance with injection. Tracked in
  issue #22; the real fix is uid-separation or a local auth proxy.
- **Admin bypass.** `enforce_admins` is off (human break-glass); the bot's token has no admin
  and cannot bypass status checks.

## If a secret ever leaks

Public transcripts mean a leak is durable in git history. Response order: **revoke the key
first** (rotate the provider key / regenerate the App key), then scrub history if needed. The
mitigations above are designed so that there is no reachable secret to leak in the first place.
