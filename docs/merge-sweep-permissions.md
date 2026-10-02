# Merge-sweep permissions

The App registration and installation need **Workflows: Read and write** to
incorporate workflow changes from main. Only the merge-sweep token requests that
permission; other tokens list their required permissions explicitly. Use the
pinned create-github-app-token v2.2.1: v1 silently ignores permission inputs and
inherits every App permission.

Callers pin the reusable-workflow reference to a reviewed TauCetiReview commit.
The workflow checks out its runner at that same commit (`job.workflow_sha`), so
callers should not pass `review_ref`; it exists only to test a different policy
commit. The sweep runs trusted pinned code and never executes a PR. Permission
changes do not bypass build or review gates.

## Fork branches

An upstream installation token does not confer write access to contributor
forks. GitHub requires the App to have contents write access to the head
repository for `update-branch`; bringing workflow changes into that fork also
requires workflow access there. Granting Workflows on the upstream installation
alone is insufficient. Fork recovery should be performed by the fork owner's
worker, or a maintainer with existing authorized access. Do not add a personal
access token to CI as an automatic fallback.

A live sweep on 2026-09-17 failed to update #7077 and #6778 in
`sqrt-of-2/TauCeti` despite the upstream Workflows grant. A maintainer's normal
GitHub login successfully requested the #7077 update. These are separate from
#7077's duplicate Lean declarations, which still require a code fix.

## One-time settings

1. Open https://github.com/organizations/TauCetiProject/settings/apps/tauceti-review-bot/permissions
2. Set **Repository permissions → Workflows → Read and write**, then **Save changes**.
3. Open https://github.com/organizations/TauCetiProject/settings/installations/143500674
4. Click **Review request → Accept new permissions**, if pending.

## Verification

Confirm both the App and installation report `workflows: write`. A successful
sweep dry run confirms token creation and planning only. Verify an actual
`update-branch` response and the new head's CI before claiming fork recovery works.

- [GitHub update-branch requirements](https://docs.github.com/en/rest/pulls/pulls#update-a-pull-request-branch)
- [Permission-input bug confirmed upstream](https://github.com/actions/create-github-app-token/issues/248#issuecomment-2855170775)
