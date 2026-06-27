#!/usr/bin/env python3
"""Merge sweep: re-drive green PRs the merge queue evicted, so an eviction is never terminal.

auto-merge.yml enqueues a green PR exactly once per triggering event (a push, a `/review` comment, or a
fresh pr-build on that PR). GitHub's merge queue then retests the PR against the CURRENT main and, if
that rebuild fails or the merge group cannot form, EVICTS it (`removed_from_merge_queue`). Nothing then
re-drives an otherwise-idle green PR: no new push/comment/build fires, so it sits enqueued-once,
evicted-once, and stranded — green on every rubric yet never merged. housekeeping.py only CLOSES PRs
(and only blocking ones), so a wedged-but-green PR is closed by nothing; without this sweep it would
strand forever. This runs on a schedule and re-drives them:

  enqueue        The default. For a green-at-head, TauCeti/-only, mergeable PR that targets main and is
                 not already in the queue, re-enqueue it. Cheap: it reuses the EXISTING head-pinned green
                 review (no re-review, no API spend), and the queue's own rebuild-against-main is the
                 real test. This rescues the common case — a transient eviction (a merge group that could
                 not form, or a PR that has fallen behind a moving main but still builds on it).

  update-branch  The escalation. If the queue has already evicted THIS head >= EVICT_ESCALATE times
                 (counted from the PR's removed_from_merge_queue timeline events after the head became
                 current), the queue has proven this commit cannot merge onto main — re-enqueuing only
                 burns another merge_group build. Instead, update the branch onto main: that re-runs the
                 build + review against current main, turning a wedged-green PR into a normal one. Only
                 when the PR is behind main (there is something to update); update-branch merges base into
                 head (the queue squash-merges, so the extra merge commit never reaches main).

  flag           If update-branch conflicts (e.g. two PRs added the same declaration), the PR needs
                 human/worker reconciliation, not a retry: label it `needs-rebase`, comment once, and
                 stop. Also used if a PR is evicted past the threshold yet is NOT behind main.

A `keep` label (also hold/wip/human/do-not-close) opts a PR out. Drafts, non-main bases, and PRs
touching paths outside TauCeti/ are skipped. Every mutation is gated on DRY_RUN and bound to the
reviewed head, and a real execution failure exits nonzero; benign races (already queued, head moved) do
not. The merge decision is the SAME decide_from_comments the merge-only path uses, so the sweep can
never enqueue something the normal gate would refuse.

Env: GH_TOKEN (contents+pull-requests write), REPO (owner/name), optional DRY_RUN=1, EVICT_ESCALATE,
MERGE_PREFIX.
"""
import datetime
import json
import os
import subprocess
import sys

from merge_from_scoreboard import decide_from_comments
from review import DEFAULT_RUBRICS

REPO = os.environ.get("REPO", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# How many times the queue may evict the SAME head before the sweep stops re-enqueuing and escalates to
# update-branch. 0 prior evictions => first re-enqueue; the queue rebuild is the real test. The default
# (2) gives a transient eviction one more cheap retry before paying for an update-branch + re-review.
EVICT_ESCALATE = int(os.environ.get("EVICT_ESCALATE", "2"))
MERGE_PREFIX = os.environ.get("MERGE_PREFIX", "TauCeti/")
KEEP_LABELS = {"keep", "hold", "wip", "human", "do-not-close"}
NEEDS_REBASE_LABEL = "needs-rebase"
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
REBASE_COMMENT = (
    "Re-driving automatically: this PR was reviewed all-green but the merge queue evicted head `{sha}` "
    "because it no longer merges cleanly onto `main` (another PR has moved main underneath it), and "
    "updating the branch onto `main` hits a conflict. It needs a rebase / reconciliation against current "
    "`main` before it can merge — the green review cannot be applied to a commit that does not build on "
    "main. Once the branch is updated the normal review + merge flow takes over again. Add the `keep` "
    "label to silence this.")


def gh(args):
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def gh_json(args):
    r = gh(args)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {r.stderr.strip()}")
    try:
        return json.loads(r.stdout or "null")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh {' '.join(args)} returned non-JSON: {e}")


def gh_jsonl(args):
    """Run a `gh api` call whose --jq emits ONE JSON object per line (JSONL) and parse it line by line.
    Paginated array endpoints must be read this way: a bare `gh api --paginate` concatenates each page's
    array (`]` then `[`) into a single invalid-JSON stream, so json.loads would fail once a PR has more
    than one page of comments or timeline events."""
    r = gh(args)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {r.stderr.strip()}")
    out = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def parse_ts(s):
    if not s:
        return EPOCH
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return EPOCH


def has_keep_label(pr):
    return bool({(l.get("name") or "").lower() for l in (pr.get("labels") or [])} & KEEP_LABELS)


# ---- pure decision helpers (unit-tested in tests/test_sweep.py) ----

def eviction_cutoff(head_dt, force_push_dts):
    """Evictions of the CURRENT head are those after the head became current: the later of the head
    commit's own date and the most recent force-push of the head ref. Using force-push events too means
    a force-push back to an older-dated commit still resets the count, instead of inheriting a prior
    head's evictions."""
    return max([head_dt, *force_push_dts])


def count_evictions(events, cutoff):
    """removed_from_merge_queue timeline events after `cutoff` — how many times the queue kicked this
    head. The timeline event carries no SHA, so the cutoff is how we scope it to the current head."""
    return sum(1 for e in events
               if e.get("event") == "removed_from_merge_queue" and parse_ts(e.get("created_at")) > cutoff)


def classify_update(returncode, output):
    """Classify an update-branch result into updated / skip (benign race) / conflict / error. A racing
    push and a real merge conflict are BOTH HTTP 422, so check the head-moved messages BEFORE conflict;
    only an explicit conflict message means the PR needs a rebase. Unknown validation 422s are benign
    (retried next sweep); only auth/permission/server faults are real errors."""
    low = output.lower()
    if returncode == 0:
        return "updated"
    if ("expected_head_sha" in low or "head branch was modified" in low or "does not match" in low
            or "head has been modified" in low):
        return "skip"
    if "conflict" in low:
        return "conflict"
    if "422" in low or "unprocessable" in low:
        return "skip"
    return "error"


# Enqueue outcomes that are NOT failures: the PR is already in the queue (a concurrent auto-merge
# enqueued it first), the head moved, or GitHub does not currently consider it mergeable — all retried
# on a later sweep. GitHub phrases "already queued" as "Pull request is already in the queue", which
# matches neither "already queued" nor "already in the merge queue", so it is listed explicitly.
ENQUEUE_BENIGN = ("already queued", "already in the merge queue", "already in the queue",
                  "expected head", "head has been modified", "not mergeable", "mergeable state",
                  "is not mergeable")


def enqueue_is_benign(output):
    """True if an enqueue's failure output is a benign race (already queued / head moved / not yet
    mergeable) rather than a real fault — those are no-ops a later sweep retries, not failures."""
    low = (output or "").lower()
    return any(b in low for b in ENQUEUE_BENIGN)


def decide_action(*, merge_ok, in_queue, evictions_at_head, behind, escalate=EVICT_ESCALATE):
    """Pure policy: given the merge gate's verdict and the PR's queue history, what should the sweep do?
    Returns (action, reason) with action in {skip, enqueue, update_branch, flag}.

      not green / not TauCeti-only ....... skip (decide_from_comments already refused it)
      already in the merge queue ......... skip (it is progressing)
      evicted < escalate times ........... enqueue (re-enqueue; the queue rebuild is the real test)
      evicted >= escalate, behind main ... update_branch (queue proved this head cannot merge; re-test
                                           + re-review against current main)
      evicted >= escalate, not behind .... flag (an eviction the sweep cannot resolve by updating)
    """
    if not merge_ok:
        return "skip", "not mergeable by the gate"
    if in_queue:
        return "skip", "already in the merge queue"
    if evictions_at_head >= escalate:
        if behind > 0:
            return "update_branch", (f"evicted {evictions_at_head}x at this head and {behind} commit(s) "
                                     "behind main; updating onto main to re-test and re-review")
        return "flag", (f"evicted {evictions_at_head}x at this head but not behind main; "
                        "needs a human look")
    return "enqueue", (f"green and not queued ({evictions_at_head} prior eviction(s) at head); "
                       "re-enqueuing")


# ---- gh-backed gather + execute ----

def queue_numbers():
    """PR numbers currently in the merge queue for main (skip these — they are already progressing)."""
    q = gh_json(["api", "graphql", "-f", "query="
                 '{repository(owner:"%s",name:"%s"){mergeQueue(branch:"main"){'
                 'entries(first:100){nodes{pullRequest{number}}}}}}'
                 % tuple(REPO.split("/", 1))])
    mq = (((q or {}).get("data") or {}).get("repository") or {}).get("mergeQueue") or {}
    nodes = (mq.get("entries") or {}).get("nodes") or []
    return {n["pullRequest"]["number"] for n in nodes if n.get("pullRequest")}


def status_states(rollup):
    """Read the authoritative `build` and `bump-guard` commit STATUSES (posted by trusted base CI) from
    a statusCheckRollup. Missing -> '' -> the gate refuses (decide_merge)."""
    def state(ctx):
        for s in rollup or []:
            if s.get("__typename") == "StatusContext" and s.get("context") == ctx:
                return s.get("state") or ""
        return ""
    return state("build"), state("bump-guard")


def current_head(pr):
    """The PR's head right now — re-read just before acting so a push mid-sweep is never acted on."""
    return (gh_json(["pr", "view", str(pr), "--repo", REPO, "--json", "headRefOid"]) or {}).get("headRefOid", "")


def enqueue(pr, node_id, head):
    """Hand the PR to the merge queue, bound to the reviewed head (expectedHeadOid rejects a racing
    push). Benign outcomes (already queued, head moved, not yet mergeable) are not failures."""
    if DRY_RUN:
        print(f"[dry-run] would enqueue #{pr} ({head[:7]})")
        return True
    r = gh(["api", "graphql", "-f", "query="
            "mutation($prId:ID!,$headOid:GitObjectID!){enqueuePullRequest("
            "input:{pullRequestId:$prId,expectedHeadOid:$headOid}){mergeQueueEntry{position state}}}",
            "-f", f"prId={node_id}", "-f", f"headOid={head}"])
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0 and "errors" not in out.lower():
        print(f"enqueued #{pr}: {out.strip()}")
        return True
    if enqueue_is_benign(out):
        print(f"#{pr}: enqueue not applied (benign — a later sweep retries): {out.strip()}")
        return True
    print(f"#{pr}: unexpected enqueue failure: {out.strip()}", file=sys.stderr)
    return False


def update_branch(pr, head):
    """Update the PR branch onto current main (merge base into head), bound to the reviewed head. Returns
    classify_update's verdict: updated / skip (benign race) / conflict / error."""
    if DRY_RUN:
        print(f"[dry-run] would update-branch #{pr} onto main")
        return "updated"
    r = gh(["api", "--method", "PUT", f"/repos/{REPO}/pulls/{pr}/update-branch",
            "-f", f"expected_head_sha={head}"])
    verdict = classify_update(r.returncode, (r.stdout or "") + (r.stderr or ""))
    msg = ((r.stdout or "") + (r.stderr or "")).strip()
    if verdict == "error":
        print(f"#{pr}: unexpected update-branch failure: {msg}", file=sys.stderr)
    else:
        print(f"#{pr}: update-branch -> {verdict}: {msg}")
    return verdict


def flag(pr, head, labels):
    """Mark a PR that cannot be auto-merged (conflict / unresolved eviction) so it is surfaced, not
    looped on. Idempotent: only labels + comments once, when the label is not already present."""
    if NEEDS_REBASE_LABEL in {(l.get("name") or "").lower() for l in labels}:
        print(f"#{pr}: already flagged {NEEDS_REBASE_LABEL}; leaving it")
        return True
    if DRY_RUN:
        print(f"[dry-run] would label #{pr} {NEEDS_REBASE_LABEL} and comment")
        return True
    r = gh(["pr", "edit", str(pr), "--repo", REPO, "--add-label", NEEDS_REBASE_LABEL])
    if r.returncode != 0:
        # A missing label in the repo is the likely cause; create it once, then retry.
        gh(["label", "create", NEEDS_REBASE_LABEL, "--repo", REPO, "--force", "--color", "D93F0B",
            "--description", "Behind main; needs a rebase to merge"])
        r = gh(["pr", "edit", str(pr), "--repo", REPO, "--add-label", NEEDS_REBASE_LABEL])
    c = gh(["pr", "comment", str(pr), "--repo", REPO, "--body", REBASE_COMMENT.format(sha=head[:7])])
    if r.returncode != 0 or c.returncode != 0:
        print(f"#{pr}: flag failed: {r.stderr.strip()} {c.stderr.strip()}", file=sys.stderr)
        return False
    print(f"flagged #{pr} {NEEDS_REBASE_LABEL}")
    return True


def main():
    if not REPO:
        print("merge-sweep: REPO env is required", file=sys.stderr)
        return 1
    required = set(DEFAULT_RUBRICS)
    failures = 0
    suffix = " [dry-run]" if DRY_RUN else ""
    try:
        in_queue = queue_numbers()
    except RuntimeError as e:
        print(f"merge-sweep: cannot read the merge queue ({e}); aborting", file=sys.stderr)
        return 1
    prs = gh_json(["pr", "list", "--repo", REPO, "--state", "open", "--limit", "1000",
                   "--json", "number,isDraft,labels"]) or []
    cand = [p for p in prs if p.get("isDraft") is False and not has_keep_label(p)]
    print(f"merge-sweep: {len(cand)} candidate PR(s); {len(in_queue)} already queued{suffix}")
    for p in cand:
        n = p["number"]
        if n in in_queue:
            continue
        try:
            v = gh_json(["pr", "view", str(n), "--repo", REPO, "--json",
                         "headRefOid,baseRefName,id,labels,statusCheckRollup"])
            head = v["headRefOid"]
            if (v.get("baseRefName") or "") != "main":
                continue   # the sweep only drives PRs targeting main (the merge queue is main's)
            comments = gh_jsonl(["api", "--paginate", f"/repos/{REPO}/issues/{n}/comments?per_page=100",
                                 "--jq", ".[] | {body, updated_at, created_at}"])
            diff = gh(["pr", "diff", str(n), "--repo", REPO]).stdout or ""
            ci_build, bump_guard = status_states(v.get("statusCheckRollup"))
            decision = decide_from_comments(comments, head, required, diff, ci_build, bump_guard,
                                            MERGE_PREFIX)
            if not decision["merge"]:
                continue   # not green at head / not TauCeti-only — the normal gate would not merge it
            cmp = gh_json(["api", f"/repos/{REPO}/compare/main...{head}"])
            behind = int((cmp or {}).get("behind_by") or 0)
            head_dt = parse_ts((gh_json(["api", f"/repos/{REPO}/commits/{head}"]) or {})
                               .get("commit", {}).get("committer", {}).get("date"))
            tl = gh_jsonl(["api", "--paginate", f"/repos/{REPO}/issues/{n}/timeline?per_page=100",
                           "--jq", ".[] | {event, created_at}"])
            force_pushes = [parse_ts(e.get("created_at")) for e in tl
                            if e.get("event") == "head_ref_force_pushed"]
            evicted = count_evictions(tl, eviction_cutoff(head_dt, force_pushes))
        except (RuntimeError, KeyError, IndexError) as e:
            print(f"#{n}: state fetch failed ({e}); skipping this round", file=sys.stderr)
            continue
        action, reason = decide_action(merge_ok=True, in_queue=False, evictions_at_head=evicted,
                                       behind=behind)
        # Re-read the head right before acting: if a push landed during the sweep, the decision (and the
        # green review) is for a commit that is no longer current — skip rather than act on a stale head.
        if action != "skip" and not DRY_RUN:
            try:
                if current_head(n) != head:
                    print(f"#{n}: head moved during the sweep; skipping")
                    continue
            except RuntimeError as e:
                print(f"#{n}: head re-check failed ({e}); skipping", file=sys.stderr)
                continue
        print(f"#{n} ({head[:7]}): {action} — {reason}")
        if action == "enqueue":
            failures += not enqueue(n, v["id"], head)
        elif action == "update_branch":
            res = update_branch(n, head)
            if res == "conflict":
                failures += not flag(n, head, v.get("labels") or [])
            elif res == "error":
                failures += 1
        elif action == "flag":
            failures += not flag(n, head, v.get("labels") or [])

    if failures:
        print(f"merge-sweep: {failures} action(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"merge-sweep: {e}", file=sys.stderr)
        sys.exit(1)
