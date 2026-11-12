# Shared PR caching across independent rubrics

Claude and Codex keep the shared protocol and PR context in user messages. A
primer contains no rubric, reactivation text, or verdict marker and must reply
`READY` without tools. Every rubric attempt then receives its own rubric tail and
a fresh verdict marker. No PR content is promoted into a system/developer prompt,
and the runner adds no cache-control breakpoint to Claude Code's requests.

Claude launches every attempt with `--resume <primer> --fork-session`. The original
transcript and minimal CLI metadata are restored at the same private HOME path
between forks; completed forks, debug files, indexes, and memory are removed.
Refreshed authentication is preserved separately. The five-minute cache TTL,
model, cwd, and tool definitions stay stable.

Codex runs one local `codex app-server --listen stdio://` process per round/model.
It retains a single persistent paginated thread and uses `thread/revert` before
each completed rubric's turn to return to the primer. Rubrics are serial within
that thread. The next turn is refused until the reset completes and the retained
history contains only the primer. Native `codex exec fork` creates a different
cache identity and did not reuse the PR prefix in the measured subscription runs.
Connected apps, delegation, automatic memory, and web search are disabled for this
review path; shell tools retain the existing read-only sandbox and credential-free
environment.

Switching providers closes the previous provider's server, removes its HOME, and
clears private evidence before the next reviewer starts, including transitions to
uncached providers. Returning to that provider requires another primer. This
preserves the existing single-provider credential exposure; same-uid processes
remain outside a strong OS isolation boundary.

Session state lives only for the current review round. Changing shared context,
provider, exact model, or cwd creates another primer. A CLI lacking the required
fork/TTL or paginated-revert capabilities produces an actionable compatibility
error. Missing copyable subscription credentials require an isolated login or API
authentication; this path never operates on the user's personal session tree.
Ordinary cache misses do not invalidate a review. Primers add overhead, so a
single-rubric round may cost more than an uncached direct call.

## Results and spending survive a reset

Before resetting provider history, the runner writes a public-safe attempt
receipt under the round's store directory, then durably settles its spending.
Primer requests are settled separately before the first rubric starts. Receipts
record validated verdicts and input fingerprints, allowing a process restart to
recover a completed attempt without another model call. Settlement is idempotent;
a failed ledger update after a receipt write is repaired when the store is next
opened. Completed failed rubric attempts are retired so normal provider recovery
can make new requests on a later invocation.
Dry-run receipts live separately and cannot be settled or replayed by a real run.
The budget reservation includes an additional call allowance when a primer is
needed. As before, call allowances are estimates rather than hard provider limits.

Raw provider errors, session IDs, credentials, and transcripts are excluded from
these receipts. Private evidence stays in an owner-only leased review directory.
Normal cleanup removes reviewer state; abandoned directories are age-gated and
active leases prevent the cleanup sweep from removing a live round. A review
receipt does not itself authorize publication or alter existing exact-head and
verdict validation rules.

Claude's terminal cost can include the primer again on a fork. Incremental costs
therefore come from unique API response IDs and final streamed output counters,
including thinking tokens. Codex uses each turn's response-usage observations;
inherited/resettable thread totals are never treated as new spend or refunds.
First-request counters and initialization usage are retained in public run and
attempt records, so production analysis can establish reuse before any tool call.
Cache reads, writes, and ordinary input remain distinct. Pricing is per response
using the existing dated model table, including TTL and long-context tiers, and
derived prices remain labeled estimates. Round totals include initialization,
retries, and recovered attempt receipts. If response usage is unavailable, the
review is rejected and receipts/round records retain an incomplete-cost flag;
observed spend is a lower bound, not an assertion that the request was free.
Paid primer usage is preserved even when its transcript snapshot or history
validation fails. Stdio writes as well as response waits have bounded deadlines,
so an app-server that stops reading cannot strand a large prompt indefinitely.
Model calls default to a one-hour limit, configurable with
`TAUCETI_REVIEW_CALL_TIMEOUT_S`; control RPCs use 30-second deadlines. Linux
app-servers receive a parent-death signal if the runner is forcibly killed.
Abnormal calls retain available counters and flag spend as incomplete. Private
journaling is best effort; public receipts and ledger settlement remain mandatory.

## Reproducing validation

The ordinary CI workflow runs `tests/test_reviewer_sessions.py` alongside the
existing billing, publication, tool-policy, and review tests. The following tests
are opt-in because they require native CLIs or paid model requests:

```bash
# Actual CLI requests captured by a local fake provider; no paid model calls.
python tests/native_session_input.py --provider claude
python tests/native_session_input.py --provider codex

# Real rubrics, verdict markers, reference material, reactivation context, and tools.
python tests/live_prompt_cache.py --provider claude --auth subscription
python tests/live_prompt_cache.py --provider codex --auth subscription

# Larger-than-argv context, forged PR instructions, and an independent repeated attempt.
python tests/live_prompt_cache.py --provider claude --large --adversarial --retry
python tests/live_prompt_cache.py --provider codex --large --adversarial --retry

# Requires the selected provider's API key in the process environment.
python tests/live_prompt_cache.py --provider claude --auth api
python tests/live_prompt_cache.py --provider codex --auth api
```

The native request capture asserts that the shared PR appears only in user
messages and that the next rubric contains neither the previous rubric's marker
nor its assessment. Live tests inspect the first request before tool use, since a
cache read after a tool call alone cannot prove reuse across rubrics. They require
a valid rubric verdict and successful inspection of the source fixture.

## Validation observations, 2026-11-12

Initial implementation acceptance used Claude Code 2.1.322 and Codex 0.167.2 on
this host. Codex is the host's patched build; deployment must validate its actual
binary. These versions are evidence points, not inferred minimum versions.

| Subscription run | Model | Successful rubric calls | First-request cache reads | Total estimated cost including primer |
| --- | --- | ---: | --- | ---: |
| Claude | `claude-opus-5-5` | 3 | 16,819 tokens for every rubric | $0.225695 |
| Codex, apps/delegation/memory disabled | `gpt-6.1-sol` | 3 | Shared PR reused on each rubric | $0.087327 |

The larger-than-argv adversarial runs also completed four rubric attempts per provider
(including one repeated attempt from the primer): Claude read 95,045 shared-prefix
tokens on every first request, with a $0.797164 total including initialization;
Codex read 92,544 on every first request, with a $0.307295 total. Neither run
followed the forged PR instructions. These fixtures validate behavior, not the
production cost-saving percentage.

Both native input-capture tests passed. Both production-model runs completed tool
calls and parsed verdicts. A direct Haiku check established that final response
counters match incremental native billing while the fork's terminal total also
includes its primer. The source fixture intentionally contains a vacuous theorem;
an adverse review of that fixture is a successful acceptance result.

Deployment-host checks on `tauchonki` used its actual Claude Code 2.1.304 and
Codex 0.163.0 binaries. Both passed native request capture against the local fake
provider, including the final control-loop changes. CI pins these accepted CLI
versions; upgrades require repeating native acceptance. With its enrolled credential kept on that host, Claude API-auth
acceptance passed three production-model rubrics with tool use: every first
request read 16,457 shared-prefix tokens; estimated cost including initialization
was $0.226303. Worker and CI admissions were unchanged.

The user explicitly waived the live OpenAI API-auth acceptance test. It was not
run: local and deployment-host Codex credentials use ChatGPT authentication and
contain no API key. Codex validation covers subscription authentication and native
request capture, not live API-key billing. This is a recorded validation limit,
not a rollout gate. Upstream review, current-head CI, and live deployment checks
still apply. Passing finite prompt-injection fixtures is not a security proof.

Deploy through the normal review-source revision update, with worker and CI
admissions enabled. Verify running revisions, completed useful reviews, first-turn
cache metrics, spend reconciliation, and cleanup of interrupted sessions. Recover
only missing/failed rubrics from the earlier cache-control incident against the
correct current PR head; do not replay published successes.
