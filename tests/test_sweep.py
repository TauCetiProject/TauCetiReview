#!/usr/bin/env python3
"""The merge sweep's decision must re-drive evicted-but-green PRs without thrashing on broken ones.

Covers decide_action (the pure policy) and that the merge gate it relies on (decide_from_comments) is
the SAME one the merge-only path uses, so the sweep can never enqueue something the normal gate refuses.
Dependency-free — run with `python tests/test_sweep.py` or under pytest.
"""
import datetime
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import sweep  # noqa: E402
import merge_from_scoreboard as mfs  # noqa: E402


def test_not_green_skips():
    action, _ = sweep.decide_action(merge_ok=False, in_queue=False, evictions_at_head=5, behind=9)
    assert action == "skip", action


def test_in_queue_skips():
    action, _ = sweep.decide_action(merge_ok=True, in_queue=True, evictions_at_head=0, behind=0)
    assert action == "skip", action


def test_green_not_queued_reenqueues():
    # The #311 case: green, evicted once transiently, not yet at the escalation threshold -> re-enqueue.
    action, _ = sweep.decide_action(merge_ok=True, in_queue=False, evictions_at_head=1, behind=4,
                                    escalate=2)
    assert action == "enqueue", action


def test_first_pass_reenqueues_even_when_behind():
    # Re-enqueue is the default even for a behind PR: the queue's rebuild is the cheap real test, and it
    # preserves the head-pinned green review (no re-review spend).
    action, _ = sweep.decide_action(merge_ok=True, in_queue=False, evictions_at_head=0, behind=20,
                                    escalate=2)
    assert action == "enqueue", action


def test_repeated_eviction_behind_updates_branch():
    # The #391 case: the queue keeps evicting this head and the PR is behind main -> stop re-enqueuing,
    # update the branch onto main (re-test + re-review against current main).
    action, _ = sweep.decide_action(merge_ok=True, in_queue=False, evictions_at_head=2, behind=7,
                                    escalate=2)
    assert action == "update_branch", action


def test_repeated_eviction_not_behind_flags():
    # Evicted past the threshold but already up to date with main: nothing to update, so surface it
    # rather than loop.
    action, _ = sweep.decide_action(merge_ok=True, in_queue=False, evictions_at_head=3, behind=0,
                                    escalate=2)
    assert action == "flag", action


def test_eviction_cutoff_uses_latest_of_commit_and_force_push():
    import datetime as dt
    t = lambda s: dt.datetime.fromisoformat(s)  # noqa: E731
    head = t("2026-06-20T00:00:00+00:00")
    fp = [t("2026-06-22T00:00:00+00:00"), t("2026-06-21T00:00:00+00:00")]
    # A force-push after the commit date moves the cutoff forward, so a prior head's evictions are not
    # counted against the current head.
    assert sweep.eviction_cutoff(head, fp) == t("2026-06-22T00:00:00+00:00")
    assert sweep.eviction_cutoff(head, []) == head


def test_count_evictions_scoped_by_cutoff():
    import datetime as dt
    cutoff = dt.datetime.fromisoformat("2026-06-21T00:00:00+00:00")
    events = [
        {"event": "removed_from_merge_queue", "created_at": "2026-06-20T00:00:00Z"},  # before cutoff
        {"event": "removed_from_merge_queue", "created_at": "2026-06-22T00:00:00Z"},  # after  cutoff
        {"event": "removed_from_merge_queue", "created_at": "2026-06-23T00:00:00Z"},  # after  cutoff
        {"event": "added_to_merge_queue", "created_at": "2026-06-24T00:00:00Z"},      # wrong  event
    ]
    assert sweep.count_evictions(events, cutoff) == 2


def test_classify_update_distinguishes_race_from_conflict():
    # success
    assert sweep.classify_update(0, "") == "updated"
    # a racing push is a 422 too — must be read as a benign skip, NOT a conflict/needs-rebase
    assert sweep.classify_update(1, "HTTP 422: expected_head_sha 'abc' does not match") == "skip"
    assert sweep.classify_update(1, "the head branch was modified; please review") == "skip"
    # a genuine conflict
    assert sweep.classify_update(1, "HTTP 422: merge conflict between base and head") == "conflict"
    # an unknown validation 422 is benign (retried), not a hard error
    assert sweep.classify_update(1, "HTTP 422: Unprocessable Entity") == "skip"
    # auth / server faults are real errors
    assert sweep.classify_update(1, "HTTP 403: Resource not accessible by integration") == "error"


def test_enqueue_already_in_queue_is_benign():
    # The exact GraphQL error when auto-merge enqueued the PR first, mid-sweep. It must be read as a
    # benign race (a no-op a later sweep retries), NOT a failure that turns the sweep job red.
    msg = ('{"data":{"enqueuePullRequest":null},"errors":[{"type":"UNPROCESSABLE",'
           '"message":"Pull request is already in the queue"}]}')
    assert sweep.enqueue_is_benign(msg)
    # other benign races
    assert sweep.enqueue_is_benign("expected head oid does not match")
    assert sweep.enqueue_is_benign("Pull request is not mergeable")
    # a real fault is NOT benign
    assert not sweep.enqueue_is_benign("HTTP 403: Resource not accessible by integration")
    assert not sweep.enqueue_is_benign("")


def test_pull_is_queued_reads_membership_and_fails_closed():
    original = sweep.gh_json
    try:
        sweep.gh_json = lambda _args: {"data": {"node": {"isInMergeQueue": True}}}
        assert sweep.pull_is_queued("PR_1")
        sweep.gh_json = lambda _args: {"data": {"node": {"isInMergeQueue": False}}}
        assert not sweep.pull_is_queued("PR_1")
        sweep.gh_json = lambda _args: {"errors": [{"message": "permission denied"}]}
        try:
            sweep.pull_is_queued("PR_1")
        except RuntimeError:
            pass
        else:
            raise AssertionError("GraphQL errors must fail closed")
    finally:
        sweep.gh_json = original


def _scoreboard(head, states, updated="2026-06-26T00:00:00Z", mode="commit"):
    payload = {"head_sha": head, "states": states}
    if mode is not None:
        payload["mode"] = mode
    meta = "<!--tauceti-meta:v1 " + json.dumps(payload) + "-->"
    return [{"body": "<!--tauceti-scoreboard-->\n" + meta, "updated_at": updated}]


def _marker(head, expires_at):
    meta = json.dumps({"head": head, "expires_at": expires_at, "providers": ["codex"]})
    return {"body": "<!--tauceti-review-in-progress " + meta + "-->"}


def test_gate_is_shared_with_merge_only():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    diff = "diff --git a/TauCeti/Foo.lean b/TauCeti/Foo.lean\n+x\n"
    # green + TauCeti-only + build/scope green -> mergeable
    assert mfs.decide_from_comments(
        _scoreboard(head, green), head, required, diff, "SUCCESS", "", scope="SUCCESS")["merge"]
    # The trusted base-side scope status is a hard gate for every automatic merge. This also
    # prevents unusual quoted diff paths from bypassing path parsing.
    assert not mfs.decide_from_comments(
        _scoreboard(head, green), head, required, diff, "SUCCESS", "", scope="")["merge"]
    # a stale scoreboard (different head) is refused — the sweep must never enqueue an unreviewed commit
    assert not mfs.decide_from_comments(_scoreboard(head, green), "other99", required, diff,
                                        "SUCCESS", "", scope="SUCCESS")["merge"]
    # a path outside TauCeti/ is refused
    diff2 = "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n+y\n"
    assert not mfs.decide_from_comments(
        _scoreboard(head, green), head, required, diff2, "SUCCESS", "", scope="SUCCESS")["merge"]


def test_newest_completed_current_head_scoreboard_wins():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    blocking = {"correctness": "green", "reuse": "blocking_request"}
    diff = "diff --git a/TauCeti/Foo.lean b/TauCeti/Foo.lean\n+x\n"

    comments = (_scoreboard(head, blocking, "2026-06-26T00:00:00Z")
                + _scoreboard(head, green, "2026-06-26T01:00:00Z"))
    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS")
    assert decision["review_safe"] and decision["merge"]

    # Conversely, a later completed blocker supersedes an earlier approval. Shuffle the API input to
    # ensure the GitHub timestamps, not incidental list order, choose the verdict.
    comments = (_scoreboard(head, blocking, "2026-06-26T02:00:00Z")
                + _scoreboard(head, green, "2026-06-26T01:00:00Z"))
    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS")
    assert not decision["review_safe"] and not decision["merge"]

    # A blocking scoreboard for an old head says nothing about the current commit.
    comments = _scoreboard("oldbeef", blocking) + _scoreboard(head, green)
    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS")
    assert decision["review_safe"] and decision["merge"]


def test_in_progress_scoreboard_does_not_supersede_a_completed_verdict():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    pending = {"correctness": "absent", "reuse": "absent"}
    blocking = {"correctness": "green", "reuse": "blocking_request"}
    diff = "diff --git a/TauCeti/Foo.lean b/TauCeti/Foo.lean\n+x\n"
    comments = (_scoreboard(head, green, "2026-06-26T00:00:00Z")
                + _scoreboard(head, pending, "2026-06-26T01:00:00Z", mode="init"))

    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS")
    assert decision["review_safe"] and decision["merge"]

    # Once that review publishes a completed blocking verdict, it becomes authoritative.
    comments += _scoreboard(head, blocking, "2026-06-26T02:00:00Z")
    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS")
    assert not decision["review_safe"] and not decision["merge"]

    # With no completed scoreboard to preserve, an init-only review still fails closed.
    decision = mfs.decide_from_comments(
        _scoreboard(head, pending, mode="init"), head, required, diff,
        "SUCCESS", "", scope="SUCCESS")
    assert not decision["review_safe"] and not decision["merge"]


def test_live_review_marker_holds_enqueue_without_revoking_green_review():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    diff = "diff --git a/TauCeti/Foo.lean b/TauCeti/Foo.lean\n+x\n"
    comments = _scoreboard(head, green) + [_marker(head, 2000)]

    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", now=1000)
    assert decision["review_safe"] and not decision["merge"]

    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", now=2000)
    assert decision["review_safe"] and decision["merge"]

    # Malformed marker payloads fail harmlessly rather than crashing the gate.
    comments.append({"body": "<!--tauceti-review-in-progress []-->"})
    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", now=2000)
    assert decision["review_safe"] and decision["merge"]


def test_review_safe_is_separate_from_automatic_path_policy():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    human_owned = "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n+y\n"
    decision = mfs.decide_from_comments(
        _scoreboard(head, green), head, required, human_owned, "SUCCESS", "", scope="SUCCESS")
    assert decision["review_safe"] and not decision["merge"]


def test_workflows_pass_status_contexts():
    root = pathlib.Path(__file__).resolve().parent.parent
    merge_only = (root / ".github/workflows/merge-only.yml").read_text()
    review = (root / ".github/workflows/review.yml").read_text()
    sweep_source = (root / "runner/sweep.py").read_text()

    for workflow in (merge_only, review):
        assert '--scope "$SCOPE"' in workflow
    for context in ("build", "bump-guard", "scope"):
        query = f'.__typename=="StatusContext" and .context=="{context}"'
        assert query in merge_only
        assert query in review
    assert 'ref: ${{ inputs.review_ref }}' in merge_only
    assert 'dequeuePullRequest' in merge_only
    assert 'isInMergeQueue' in merge_only
    assert 'jq -r .review_safe merge.json' in merge_only
    assert 'already in the queue' in merge_only
    merge_sweep = (root / ".github/workflows/merge-sweep.yml").read_text()
    assert 'ref: ${{ inputs.review_ref }}' in merge_sweep
    assert '"headRefOid,baseRefName,id,labels,statusCheckRollup"' in sweep_source
    assert 'MERGE_PREFIX, scope=scope' in sweep_source


def test_status_states_reads_trusted_contexts_and_fails_closed():
    rollup = [
        {"__typename": "StatusContext", "context": "build", "state": "SUCCESS"},
        {"__typename": "CheckRun", "name": "bump-guard", "conclusion": "SUCCESS"},
        {"__typename": "StatusContext", "context": "scope", "state": "FAILURE"},
    ]
    assert sweep.status_states(rollup) == ("SUCCESS", "", "FAILURE")
    assert sweep.status_states([]) == ("", "", "")


def test_lakefile_never_auto_merges():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    diff = (
        "diff --git a/lakefile.toml b/lakefile.toml\n"
        "+rev = \"deadbeef\"\n"
        "diff --git a/lake-manifest.json b/lake-manifest.json\n"
        "+{}\n"
    )
    comments = _scoreboard(head, green)

    assert not mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "SUCCESS", scope="SUCCESS")["merge"]

    # Bot authorship does not grant a general infrastructure bypass.
    workflow_diff = "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n+y\n"
    assert not mfs.decide_from_comments(
        comments, head, required, workflow_diff, "SUCCESS", "SUCCESS", scope="SUCCESS")["merge"]


# ---- merge-queue reservation ----

_T0 = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.timezone.utc)


def _entry(number, minutes_ago, paths):
    return {"number": number, "node_id": f"PR_{number}", "paths": paths,
            "enqueued_at": (_T0 - datetime.timedelta(minutes=minutes_ago))
            .isoformat().replace("+00:00", "Z")}


def test_is_pin_moving_only_for_lake_pins():
    assert sweep.is_pin_moving(["lake-manifest.json"])
    assert sweep.is_pin_moving(["TauCeti/Foo.lean", "lean-toolchain"])
    assert not sweep.is_pin_moving(["TauCeti/Foo.lean"])
    assert not sweep.is_pin_moving([])


def test_reservation_holder_none_without_a_pin_pr():
    entries = [_entry(1, 5, ["TauCeti/A.lean"]), _entry(2, 3, ["TauCeti/B.lean"])]
    assert sweep.reservation_holder(entries, _T0) is None
    assert sweep.reservation_holder([], _T0) is None


def test_reservation_holder_elects_the_earliest_pin_pr():
    # Two bumps open at once must not dequeue each other in a loop: exactly one holds the queue,
    # and it is the one that got there first.
    entries = [_entry(10, 5, ["TauCeti/A.lean"]),
               _entry(20, 30, ["lake-manifest.json"]),
               _entry(30, 60, ["lean-toolchain"])]
    assert sweep.reservation_holder(entries, _T0) == 30


def test_reservation_holder_lapses_a_stale_holder():
    # A build takes 83-95 min; an entry far older than MAX_HOLD is stuck, and holding every other
    # merge behind it is worse than releasing it.
    entries = [_entry(30, 60, ["lean-toolchain"])]
    assert sweep.reservation_holder(entries, _T0, datetime.timedelta(hours=3)) == 30
    assert sweep.reservation_holder(entries, _T0, datetime.timedelta(minutes=30)) is None


def test_reservation_holder_respects_the_budget():
    entries = [_entry(30, 10, ["lean-toolchain"])]
    assert sweep.reservation_holder(entries, _T0, exhausted=()) == 30
    assert sweep.reservation_holder(entries, _T0, exhausted=(30,)) is None


def test_count_evictions_ignores_our_own_reservation_removals():
    # Booting a PR to clear the way for a bump emits the same removed_from_merge_queue event as a
    # real eviction. Counting ours would escalate an innocent PR to update_branch and needs-rebase.
    cutoff = _T0 - datetime.timedelta(hours=1)
    at = (_T0 - datetime.timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    events = [{"event": "removed_from_merge_queue", "created_at": at, "actor": "tauceti-review-bot"},
              {"event": "removed_from_merge_queue", "created_at": at, "actor": "github-merge-queue"}]
    assert sweep.count_evictions(events, cutoff, app_login="tauceti-review-bot") == 1
    assert sweep.is_reservation_removal("Tauceti-Review-Bot", "tauceti-review-bot")
    assert not sweep.is_reservation_removal(None, "tauceti-review-bot")


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
