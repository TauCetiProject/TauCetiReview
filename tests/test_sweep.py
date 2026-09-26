#!/usr/bin/env python3
"""The merge sweep's decision must re-drive evicted-but-green PRs without thrashing on broken ones.

Covers decide_action (the pure policy) and that the merge gate it relies on (decide_from_comments) is
the SAME one the merge-only path uses, so the sweep can never enqueue something the normal gate refuses.
Dependency-free — run with `python tests/test_sweep.py` or under pytest.
"""
import contextlib
import datetime
import io
import json
import os
import sys
import tempfile
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import sweep  # noqa: E402
import merge_from_scoreboard as mfs  # noqa: E402
import merge as mfs_merge  # noqa: E402
import pr_diff  # noqa: E402


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


MB = "f" * 40   # the merge base every scoreboard below was reviewed against, unless noted


def _scoreboard(head, states, updated="2026-06-26T00:00:00Z", mode="commit", merge_base=MB):
    payload = {"head_sha": head, "states": states}
    if merge_base is not None:
        payload["merge_base_sha"] = merge_base
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
    diff = {"TauCeti/Foo.lean"}
    # green + TauCeti-only + build/scope green -> mergeable
    assert mfs.decide_from_comments(
        _scoreboard(head, green), head, required, diff, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)["merge"]
    # The trusted base-side scope status is a hard gate for every automatic merge. This also
    # prevents unusual quoted diff paths from bypassing path parsing.
    assert not mfs.decide_from_comments(
        _scoreboard(head, green), head, required, diff, "SUCCESS", "", scope="", merge_base_sha=MB)["merge"]
    # a stale scoreboard (different head) is refused — the sweep must never enqueue an unreviewed commit
    assert not mfs.decide_from_comments(_scoreboard(head, green), "other99", required, diff,
                                        "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)["merge"]
    # a path outside TauCeti/ is refused
    diff2 = {".github/workflows/x.yml"}
    assert not mfs.decide_from_comments(
        _scoreboard(head, green), head, required, diff2, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)["merge"]
    # an init (in-progress) board is refused even when it renders every rubric green: no verdict
    # for this head has completed, and a carried-forward approval must not enqueue before the run
    init_green = mfs.decide_from_comments(
        _scoreboard(head, green, mode="init"), head, required, diff, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)
    assert not init_green["merge"] and not init_green["review_safe"]


def test_newest_completed_current_head_scoreboard_wins():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    blocking = {"correctness": "green", "reuse": "blocking_request"}
    diff = {"TauCeti/Foo.lean"}

    comments = (_scoreboard(head, blocking, "2026-06-26T00:00:00Z")
                + _scoreboard(head, green, "2026-06-26T01:00:00Z"))
    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)
    assert decision["review_safe"] and decision["merge"]

    # Conversely, a later completed blocker supersedes an earlier approval. Shuffle the API input to
    # ensure the GitHub timestamps, not incidental list order, choose the verdict.
    comments = (_scoreboard(head, blocking, "2026-06-26T02:00:00Z")
                + _scoreboard(head, green, "2026-06-26T01:00:00Z"))
    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)
    assert not decision["review_safe"] and not decision["merge"]

    # A blocking scoreboard for an old head says nothing about the current commit.
    comments = _scoreboard("oldbeef", blocking) + _scoreboard(head, green)
    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)
    assert decision["review_safe"] and decision["merge"]


def test_in_progress_scoreboard_does_not_supersede_a_completed_verdict():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    pending = {"correctness": "absent", "reuse": "absent"}
    blocking = {"correctness": "green", "reuse": "blocking_request"}
    diff = {"TauCeti/Foo.lean"}
    comments = (_scoreboard(head, green, "2026-06-26T00:00:00Z")
                + _scoreboard(head, pending, "2026-06-26T01:00:00Z", mode="init"))

    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)
    assert decision["review_safe"] and decision["merge"]

    # Once that review publishes a completed blocking verdict, it becomes authoritative.
    comments += _scoreboard(head, blocking, "2026-06-26T02:00:00Z")
    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)
    assert not decision["review_safe"] and not decision["merge"]

    # With no completed scoreboard to preserve, an init-only review still fails closed.
    decision = mfs.decide_from_comments(
        _scoreboard(head, pending, mode="init"), head, required, diff,
        "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)
    assert not decision["review_safe"] and not decision["merge"]


def test_live_review_marker_holds_enqueue_without_revoking_green_review():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    diff = {"TauCeti/Foo.lean"}
    comments = _scoreboard(head, green) + [_marker(head, 2000)]

    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB, now=1000)
    assert decision["review_safe"] and not decision["merge"]

    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB, now=2000)
    assert decision["review_safe"] and decision["merge"]

    # Malformed marker payloads fail harmlessly rather than crashing the gate.
    comments.append({"body": "<!--tauceti-review-in-progress []-->"})
    decision = mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB, now=2000)
    assert decision["review_safe"] and decision["merge"]


def test_review_safe_is_separate_from_automatic_path_policy():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    human_owned = {".github/workflows/x.yml"}
    decision = mfs.decide_from_comments(
        _scoreboard(head, green), head, required, human_owned, "SUCCESS", "", scope="SUCCESS", merge_base_sha=MB)
    assert decision["review_safe"] and not decision["merge"]



def test_verdict_is_bound_to_the_merge_base_it_reviewed():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    paths = {"TauCeti/Foo.lean"}
    ok = mfs.decide_from_comments(_scoreboard(head, green), head, required, paths, "SUCCESS", "",
                                  scope="SUCCESS", merge_base_sha=MB)
    assert ok["review_safe"] and ok["merge"]
    # Same head, different diff: the PR was retargeted or its base history rewritten. The green
    # verdict is for another diff, so it is unsafe (dequeue), not merely unmergeable.
    moved = mfs.decide_from_comments(_scoreboard(head, green), head, required, paths, "SUCCESS",
                                     "", scope="SUCCESS", merge_base_sha="e" * 40)
    assert not moved["review_safe"] and not moved["merge"], moved
    # A scoreboard that recorded no merge base, or an unknown merge base now, fails closed.
    for board, mb in ((_scoreboard(head, green, merge_base=None), MB),
                      (_scoreboard(head, green, merge_base=""), MB),
                      (_scoreboard(head, green), ""),
                      (_scoreboard(head, green), None)):
        d = mfs.decide_from_comments(board, head, required, paths, "SUCCESS", "",
                                     scope="SUCCESS", merge_base_sha=mb)
        assert not d["review_safe"] and not d["merge"], (board, mb)
    # Absent the keyword (an old caller), nothing merges.
    d = mfs.decide_from_comments(_scoreboard(head, green), head, required, paths, "SUCCESS", "",
                                 scope="SUCCESS")
    assert not d["merge"]


def test_merge_reads_machine_paths_including_names_git_quotes():
    head = "deadbee"
    required = {"correctness", "reuse"}
    green = {"correctness": "green", "reuse": "green"}
    # A patch header git quotes (non-ASCII or control bytes) is invisible to the diff-text parser,
    # so the merge gate reads the NUL-separated list pr_diff writes instead.
    odd = [".github/w\u00f6rkflows/x.yml", "scripts/new\nline.py", "TauCeti/Foo.lean"]
    quoted = ('diff --git "a/.github/w\\303\\266rkflows/x.yml" "b/.github/w\\303\\266rkflows/x.yml"\n'
              "diff --git a/TauCeti/Foo.lean b/TauCeti/Foo.lean\n")
    assert mfs_merge.changed_paths(quoted) == {"TauCeti/Foo.lean"}   # why the parser is not used
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "paths.z"
        pr_diff.write_paths(f, odd)
        paths = mfs_merge.read_paths(f)
    assert paths == set(odd)
    for bad in odd[:2]:
        d = mfs.decide_from_comments(_scoreboard(head, green), head, required,
                                     {bad, "TauCeti/Foo.lean"}, "SUCCESS", "", scope="SUCCESS",
                                     merge_base_sha=MB)
        assert d["review_safe"] and not d["merge"], (bad, d)


def test_review_merge_decision_reads_machine_paths_and_fails_closed_without():
    import types
    import review
    states = {"correctness": "green"}
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "paths.z"
        args = dict(merge_path_prefix="TauCeti/", merge_allow_file=[], bump_guard="",
                    ci_build="SUCCESS", scope="SUCCESS")
        a = types.SimpleNamespace(paths_file="", **args)
        ok, reason = review.merge_decision(a, states, ["correctness"], True, "h" * 40)
        assert not ok and "--paths-file" in reason
        a.paths_file = str(f)
        pr_diff.write_paths(f, ["TauCeti/Foo.lean"])
        assert review.merge_decision(a, states, ["correctness"], True, "h" * 40)[0]
        pr_diff.write_paths(f, ["TauCeti/Foo.lean", ".github/w\u00f6rkflows/x.yml"])
        assert not review.merge_decision(a, states, ["correctness"], True, "h" * 40)[0]
        # Thread anchors come from the same list, so a finding on a file whose name git quotes in
        # the patch header still anchors to it.
        odd = "TauCeti/\u00c9tale.lean"
        pr_diff.write_paths(f, ["TauCeti/Foo.lean", odd])
        quoted = ('diff --git "a/TauCeti/\\303\\211tale.lean" "b/TauCeti/\\303\\211tale.lean"\n'
                  "diff --git a/TauCeti/Foo.lean b/TauCeti/Foo.lean\n")
        paths = review.changed_file_paths(a, quoted)
        assert paths == {"TauCeti/Foo.lean", odd}
        assert review.pick_anchor({"findings": [{"file": odd}]}, "TauCeti/Foo.lean", paths) == odd
        a.paths_file = ""
        assert review.changed_file_paths(a, quoted) == {"TauCeti/Foo.lean"}   # the old parser

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
    assert '"headRefOid,baseRefName,baseRefOid,id,labels,statusCheckRollup,"' in sweep_source
    # merge-only re-checks the merge base as the last step before the enqueue mutation, and again
    # straight after it (test_merge_only_merge_base_check runs the check itself).
    mutation = merge_only.index("mutation($prId: ID!, $headOid: GitObjectID!)")
    assert merge_only.index('check_merge_base "since the decision"') < mutation
    assert merge_only.index('check_merge_base "while enqueuing"') > mutation
    assert 'MERGE_PREFIX, scope=scope, merge_base_sha=merge_base' in sweep_source
    # The merge paths read machine paths and bind to the merge base; nothing parses `gh pr diff`.
    assert '--merge-base-sha "$MERGE_BASE" --paths-file paths.z' in merge_only
    assert '--paths-out paths.z' in merge_only and '--paths-file paths.z' in review
    shadow = (root / ".github/workflows/shadow-review.yml").read_text()
    assert 'python3 .diff-helper/runner/pr_diff.py' in shadow
    for workflow in (merge_only, review, shadow):
        assert 'gh pr diff "$PR"' not in workflow



def _merge_only_functions():
    """merge-only.yml's dequeue / merge_base_now / check_merge_base, as a bash script."""
    text = (pathlib.Path(__file__).resolve().parent.parent
            / ".github/workflows/merge-only.yml").read_text()
    start = text.index("          dequeue() {")
    end = text.index("          # (end of the functions tests/test_sweep.py runs)")
    return "\n".join(line[10:] for line in text[start:end].splitlines())


def test_merge_only_merge_base_check():
    """Run the workflow's own bash against a fake `gh`: a moved merge base dequeues and succeeds; a
    failed or empty read dequeues AND fails the step; a match does nothing."""
    import subprocess
    fake_gh = """#!/usr/bin/env bash
echo "$*" >> "$LOG"
case "$*" in
  "pr view"*) [ "$VIEW" = fail ] && exit 1; echo "$VIEW" ;;
  "api /repos/"*compare*) [ "$MB_NOW" = fail ] && { echo "HTTP 502" >&2; exit 1; }
                          echo "$MB_NOW" ;;
  *isInMergeQueue*) echo true ;;
  *dequeuePullRequest*) echo '{}' ;;
  *) echo "unexpected: $*" >&2; exit 3 ;;
esac
"""
    with tempfile.TemporaryDirectory() as d:
        bindir = pathlib.Path(d) / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(fake_gh)
        gh.chmod(0o755)
        script = "set -euo pipefail\n" + _merge_only_functions() + '\ncheck_merge_base "now"\n'

        def run(view, mb_now):
            log = pathlib.Path(d) / "log"
            log.write_text("")
            env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "LOG": str(log),
                   "VIEW": view, "MB_NOW": mb_now, "PR": "7", "PRID": "PR_7",
                   "HEAD_SHA": "h" * 40, "MERGE_BASE": MB}
            r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
            return r.returncode, "dequeuePullRequest" in log.read_text()

        assert run("c" * 40, MB) == (0, False)          # unchanged: nothing to do
        assert run("c" * 40, "e" * 40) == (0, True)     # moved: dequeued
        assert run("c" * 40, "") == (1, True)           # empty: dequeued, and the step fails
        assert run("c" * 40, "fail") == (1, True)       # compare failed: same
        assert run("fail", MB) == (1, True)             # base read failed: same
        assert run("", MB) == (1, True)

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
    diff = {"lakefile.toml", "lake-manifest.json"}
    comments = _scoreboard(head, green)

    assert not mfs.decide_from_comments(
        comments, head, required, diff, "SUCCESS", "SUCCESS", scope="SUCCESS", merge_base_sha=MB)["merge"]

    # Bot authorship does not grant a general infrastructure bypass.
    workflow_diff = {".github/workflows/x.yml"}
    assert not mfs.decide_from_comments(
        comments, head, required, workflow_diff, "SUCCESS", "SUCCESS", scope="SUCCESS", merge_base_sha=MB)["merge"]


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


def _request(head, author="tauceti-review-bot[bot]"):
    return {"author": author, "body": f"Merge-queue recovery for head `{head[:7]}`.\n\n<!--tauceti-rebase:v1 {head}-->"}


def test_handoff_trust_and_head_binding():
    head = "a" * 40
    assert sweep.rebase_request_heads([_request(head)]) == {head}
    assert sweep.rebase_request_heads([_request(head, "contributor"), _request("bad")]) == set()


def test_fork_handoff_retries_label_without_reposting_and_stops_at_head():
    from types import SimpleNamespace
    from unittest.mock import patch
    head = "a" * 40
    comments, calls = [], []
    label_fails = True

    def gh(args):
        calls.append(args)
        if args[:2] == ["pr", "comment"]:
            comments.append({"author": "tauceti-review-bot[bot]", "body": args[-1]})
        fail = label_fails and args[:2] == ["pr", "edit"]
        return SimpleNamespace(returncode=int(fail), stdout="", stderr="label error" if fail else "")

    with patch.object(sweep, "DRY_RUN", False), patch.object(sweep, "gh", gh), \
            patch.object(sweep, "update_branch", side_effect=AssertionError("fork update attempted")):
        assert not sweep.recover_branch(1, head, True, comments)
        assert len(comments) == 1
        label_fails = False
        assert sweep.reconcile_rebase_request(1, head, [], comments) == "waiting"
        assert len(comments) == 1
        calls.clear()
        assert sweep.reconcile_rebase_request(1, head, [{"name": "needs-rebase"}], comments) == "waiting"
        assert calls == []


def test_handoff_new_head_cleanup_and_races():
    from types import SimpleNamespace
    from unittest.mock import patch
    old, head = "a" * 40, "b" * 40
    labels = [{"name": "needs-rebase"}]
    with patch.object(sweep, "DRY_RUN", False), patch.object(sweep, "current_head", return_value=head) as current, \
            patch.object(sweep, "gh", return_value=SimpleNamespace(returncode=0)) as gh:
        assert sweep.reconcile_rebase_request(1, head, labels, []) == "ready"
        gh.assert_not_called()  # preserve a human/legacy label
        assert sweep.reconcile_rebase_request(1, head, labels, [_request(old)]) == "ready"
        assert "--remove-label" in gh.call_args.args[0]
        gh.reset_mock()
        current.return_value = "c" * 40
        assert sweep.reconcile_rebase_request(1, head, labels, [_request(old)]) == "waiting"
        gh.assert_not_called()
        with patch.object(sweep, "DRY_RUN", True):
            assert sweep.reconcile_rebase_request(1, head, labels, [_request(old)]) == "ready"
            assert sweep.recover_branch(1, head, True, [])
            gh.assert_not_called()


def test_main_hands_off_once_then_waits_until_push():
    from types import SimpleNamespace
    from unittest.mock import patch
    head = "a" * 40
    labels, comments, mutations = [], [], []
    view = {"headRefOid": head, "baseRefName": "main", "baseRefOid": "c" * 40, "id": "PR_1",
            "labels": labels,
            "statusCheckRollup": [], "isCrossRepository": True}

    def gh_json(args):
        if args[:2] == ["pr", "list"]:
            return [{"number": 1, "isDraft": False, "labels": labels}]
        if args[:2] == ["pr", "view"]:
            return view
        if "/compare/" in args[1]:
            return {"behind_by": 10, "merge_base_commit": {"sha": MB}}
        if "/commits/" in args[1]:
            return {"commit": {"committer": {"date": "2026-09-01T00:00:00Z"}}}
        raise AssertionError(args)

    def gh_jsonl(args):
        if "/comments?" in args[2]:
            return comments
        return [{"event": "removed_from_merge_queue", "created_at": "2026-09-02T00:00:00Z"}] * 2

    def gh(args):
        mutations.append(args[:2])
        if args[:2] == ["pr", "comment"]:
            comments.append({"author": "tauceti-review-bot[bot]", "body": args[-1]})
        elif "--add-label" in args:
            labels.append({"name": "needs-rebase"})
        elif "--remove-label" in args:
            labels.clear()
        else:
            raise AssertionError(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(sweep, "REPO", "owner/repo"), patch.object(sweep, "DRY_RUN", False), \
            patch.object(sweep, "queue_entries", return_value=[]), \
            patch.object(sweep, "gh_json", gh_json), patch.object(sweep, "gh_jsonl", gh_jsonl), \
            patch.object(sweep, "gh", gh), patch.object(sweep, "pr_diff", return_value=["TauCeti/X.lean"]), \
            patch.object(sweep, "decide_from_comments", return_value={"merge": True}) as gate:
        assert sweep.main() == 0
        assert mutations == [["pr", "comment"], ["pr", "edit"]]
        assert len(comments) == 1
        mutations.clear()
        gate.reset_mock()
        assert sweep.main() == 0
        assert mutations == []
        gate.assert_not_called()  # do not retry the same green, evicted head
        view["headRefOid"] = "b" * 40
        gate.return_value = {"merge": False}  # new head is awaiting CI/review
        assert sweep.main() == 0
        assert not labels
        assert mutations == [["pr", "edit"]]
        gate.assert_called_once()



def test_merge_base_is_rechecked_right_before_enqueue():
    from types import SimpleNamespace
    from unittest.mock import patch
    head = "a" * 40
    view = {"headRefOid": head, "baseRefName": "main", "baseRefOid": "c" * 40, "id": "PR_1",
            "labels": [], "statusCheckRollup": [], "isCrossRepository": False}
    merge_bases = []

    def gh_json(args):
        if args[:2] == ["pr", "list"]:
            return [{"number": 1, "isDraft": False, "labels": []}]
        if args[:2] == ["pr", "view"]:
            return view
        if "/compare/" in args[1]:
            return {"behind_by": 0, "merge_base_commit": {"sha": merge_bases.pop(0) or None}}
        if "/commits/" in args[1]:
            return {"commit": {"committer": {"date": "2026-09-01T00:00:00Z"}}}
        raise AssertionError(args)

    def run(decision_mb, recheck_mb, after_mb=MB, base_name="main"):
        merge_bases[:] = [decision_mb, recheck_mb, after_mb]
        with patch.object(sweep, "REPO", "owner/repo"), patch.object(sweep, "DRY_RUN", False), \
                patch.object(sweep, "queue_entries", return_value=[]), \
                patch.object(sweep, "gh_json", gh_json), \
                patch.object(sweep, "gh_jsonl", return_value=[]), \
                patch.object(sweep, "current_head", return_value=head), \
                patch.object(sweep, "pr_diff", return_value=["TauCeti/X.lean"]), \
                patch.object(sweep, "decide_from_comments", return_value={"merge": True}), \
                patch.object(sweep, "enqueue", return_value=True) as enq, \
                patch.object(sweep, "dequeue", return_value=True) as deq, \
                patch.dict(view, {"baseRefName": "main"}):
            orig = sweep.merge_base_now

            def merge_base_now(pr, h):
                view["baseRefName"] = base_name   # e.g. retargeted between decision and recheck
                return orig(pr, h)
            with patch.object(sweep, "merge_base_now", merge_base_now), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                assert sweep.main() == 0
            return enq.call_count, deq.call_count

    assert run(MB, MB) == (1, 0)                       # unchanged: enqueued
    assert run(MB, "e" * 40) == (0, 0)                 # base rewritten under the same head: skipped
    assert run(MB, MB, base_name="release") == (0, 0)  # retargeted away from main: skipped
    assert run(MB, "") == (0, 0)                       # unreadable before enqueue: skipped
    # Moved, or unreadable, between the pre-enqueue check and the mutation: dequeued at once.
    assert run(MB, MB, after_mb="e" * 40) == (1, 1)
    assert run(MB, MB, after_mb="") == (1, 1)

def test_up_to_date_eviction_waits_for_human_without_worker_request():
    from types import SimpleNamespace
    from unittest.mock import patch
    head, comments = "a" * 40, []

    def gh(args):
        if args[:2] == ["pr", "comment"]:
            comments.append({"author": "tauceti-review-bot[bot]", "body": args[-1]})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(sweep, "DRY_RUN", False), patch.object(sweep, "gh", gh):
        assert sweep.flag(1, head, comments, worker=False)
        assert not sweep.rebase_request_heads(comments)
        assert sweep.rebase_request_heads(comments, stalled=True) == {head}
        assert sweep.reconcile_rebase_request(1, head, [], comments) == "waiting"
        assert len(comments) == 1
        assert not sweep.rebase_request_heads(comments)  # label repair must not upgrade to worker work


def test_same_repo_updates_keep_real_errors_visible():
    from unittest.mock import patch
    with patch.object(sweep, "update_branch", return_value="updated") as update, \
            patch.object(sweep, "flag", return_value=True) as flag:
        assert sweep.recover_branch(1, "a" * 40, False, [])
        update.assert_called_once()
        flag.assert_not_called()
        update.return_value = "error"
        assert not sweep.recover_branch(1, "a" * 40, False, [])
        flag.assert_not_called()
        update.return_value = "conflict"
        assert sweep.recover_branch(1, "a" * 40, False, [])
        flag.assert_called_once()
        update.reset_mock()
        assert not sweep.recover_branch(1, "a" * 40, None, [])
        update.assert_not_called()


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
