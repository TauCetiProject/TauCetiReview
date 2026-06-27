#!/usr/bin/env python3
"""The merge sweep's decision must re-drive evicted-but-green PRs without thrashing on broken ones.

Covers decide_action (the pure policy) and that the merge gate it relies on (decide_from_comments) is
the SAME one the merge-only path uses, so the sweep can never enqueue something the normal gate refuses.
Dependency-free — run with `python tests/test_sweep.py` or under pytest.
"""
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


def _scoreboard(head, states):
    meta = "<!--tauceti-meta:v1 " + json.dumps({"head_sha": head, "states": states}) + "-->"
    return [{"body": "<!--tauceti-scoreboard-->\n" + meta, "updated_at": "2026-06-26T00:00:00Z"}]


def test_gate_is_shared_with_merge_only():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    diff = "diff --git a/TauCeti/Foo.lean b/TauCeti/Foo.lean\n+x\n"
    # green + TauCeti-only + build green -> mergeable
    assert mfs.decide_from_comments(_scoreboard(head, green), head, required, diff, "SUCCESS", "")["merge"]
    # a stale scoreboard (different head) is refused — the sweep must never enqueue an unreviewed commit
    assert not mfs.decide_from_comments(_scoreboard(head, green), "other99", required, diff,
                                        "SUCCESS", "")["merge"]
    # a path outside TauCeti/ is refused
    diff2 = "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n+y\n"
    assert not mfs.decide_from_comments(_scoreboard(head, green), head, required, diff2, "SUCCESS", "")["merge"]


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
