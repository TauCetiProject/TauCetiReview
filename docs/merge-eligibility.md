# Automatic bors admission

The shared merge policy still evaluates the newest completed scoreboard for the
current head and merge base, required rubrics, build/scope checks, allowed paths,
and the pin bump guard. It publishes `merge eligibility` checks using the review
App (3947238), rather than posting command comments. Native queue admission still
uses that same decision directly.

Each completed check carries JSON in `external_id`:

```json
{"schema":"tauceti-merge.eligibility/v1","repo":"TauCetiProject/TauCeti","pr":123,"merge_base_sha":"40-character SHA","eligible":true,"review_safe":true,"single":false}
```

GitHub binds the check to `head_sha` and its issuing App. `success` means eligible,
`neutral` means waiting without withdrawing existing approval, and `failure`
means unsafe review and withdrawal. Each changed decision creates an immutable
check. Identical reconciliation makes no duplicate writes. Bors always fetches
the newest check for that PR/head from the trusted App, regardless of webhook
ordering. It validates the live PR and reviewed merge base and handles `single`
for pin-changing PRs.

Eligibility is independent of the selected engine. Only bors admission consults
the live `MERGE_BACKEND` and requires GitHub's queue to be drained; withdrawals
apply regardless of engine. Bors recovers missed events through its minute
heartbeat, bounded to eight PRs per tick. Human `bors` commands remain available.
A manual cancellation is not undone by repeated observation of the same check,
and terminal failed heads do not automatically retry.

## Deployment

1. Approve Checks: read and write for the review App installation on TauCeti.
2. Deploy the bors consumer and its additive database migration.
3. Merge the publisher and update all TauCeti policy pins together.
4. Run a focused merge sweep; observe checks and bors admission before retiring
   any rollout monitoring. The publisher removes the automatic `bors r+`/`r-`
   comment path. Existing approvals and human commands continue working.

No additional service, secret, or Cloudflare resource is required. The review
App's installation token needs `checks:write` in merge-only and merge-sweep.
