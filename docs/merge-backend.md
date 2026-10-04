# Merge backend admission

The reusable merge workflows read TauCetiProject/TauCeti's `MERGE_BACKEND` live with the Review App. An unset variable defaults to GitHub queue only after a successful authenticated variables listing. The older merge_backend workflow input remains accepted for callers but does not choose admission.

Both App registration and installation require Variables read. The pinned create-github-app-token action discovers permission inputs from environment keys, so the merge jobs set `INPUT_PERMISSION-ACTIONS-VARIABLES: read` in addition to their existing scoped write permissions.

`runner/backend.py` is shared by event reconciliation and sweep. New GitHub enqueues require queue selected and all main bors batches drained. New bors approvals require bors selected and GitHub main queue empty. The receiver guards delayed commands independently. Observation errors defer admissions; unsafe reviews still attempt revocation in both queues. Existing native reservations and eviction recovery remain native-only. A sweep under bors invokes the same exact-head CI/scope/review policy, with immutable bors membership and terminal outcomes for deduplication.

The public bors observation endpoint must be deployed before repinning TauCeti. It preserves existing batch_ids and adds the tauceti-bors.observation/v1 contract. Unknown/legacy heads cannot prove a fresh approval. An old r+ comment cannot prove admission; a deferred/lost command can be retried. Terminal failure at the same head requires an explicit bors retry or a new head. A live review marker does not revoke a prior green standing approval.

The trusted sweep accepts repository_dispatch tauceti-merge-reconcile in TauCeti. It runs no reviewer and stages no provider keys. Heartbeat labels merely request reconciliation and never authorize merging. The bors deployment runbook documents rollout, manual switches, and experiment measurements.
