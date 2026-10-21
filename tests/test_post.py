#!/usr/bin/env python3
"""The scoreboard poster must publish exactly one comment per PR, editing OUR own in place.

Guards kim-em/TauCetiWorker#3: a review run whose local store lacks the scoreboard comment id used
to POST a duplicate. It now discovers the scoreboard WE authored from GitHub, edits it, and collapses
our older duplicates — and only ever mutates comments we authored. Dependency-free — run with
`python tests/test_post.py` or under pytest.
"""
import sys
import json
import pathlib
import tempfile
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import post  # noqa: E402


class FakeGH:
    """Records gh_api calls; PATCH succeeds/fails per `patch_ok`, POST returns `post_id`."""
    def __init__(self, patch_ok=True, post_id=None):
        self.calls = []
        self.patch_ok = patch_ok
        self.post_id = post_id

    def __call__(self, method, endpoint, fields=None, body_file=None, failures=None, action=""):
        self.calls.append((method, endpoint))
        if method == "PATCH":
            if self.patch_ok:
                return {}
            if failures is not None:
                failures.append({"action": action})
            return None
        if method == "POST":
            return {"id": self.post_id} if self.post_id else {}
        return {}  # DELETE

    def methods(self):
        return [m for m, _ in self.calls]

    def to(self, method):
        return [e for m, e in self.calls if m == method]


def run(fake, existing, pr_state, plan_sb_id=None, mine="bot"):
    saved_gh, saved_find = post.gh_api, post.find_scoreboard_comments
    post.gh_api = fake
    post.find_scoreboard_comments = lambda repo, pr: None if existing is None else list(existing)
    failures = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            body = pathlib.Path(directory) / "body.md"
            body.write_text("scoreboard body\n")
            sb_id, ok = post.upsert_scoreboard("o/r", 1, body, plan_sb_id, pr_state, failures,
                                               mine=mine)
    finally:
        post.gh_api, post.find_scoreboard_comments = saved_gh, saved_find  # don't leak into other tests
    return sb_id, ok, failures


# --- upsert_scoreboard --------------------------------------------------------------------------

def test_identical_scoreboard_is_success_without_writes():
    fake, state = FakeGH(), {}
    sb_id, ok, failures = run(fake, existing=[{
        "id": 200, "login": "bot", "body": "scoreboard body\n"}], pr_state=state)
    assert ok and sb_id == 200 and state["scoreboard_comment_id"] == 200
    assert not fake.calls and not failures


def test_identical_scoreboard_still_removes_only_our_obsolete_duplicates():
    fake = FakeGH()
    sb_id, ok, failures = run(fake, existing=[
        {"id": 200, "login": "bot", "body": "scoreboard body\n", "updated_at": "2026-10-19T08:00:00Z"},
        {"id": 100, "login": "bot", "body": "old", "updated_at": "2026-10-19T07:00:00Z"},
        {"id": 50, "login": "someone-else", "body": "old", "updated_at": "2026-10-19T06:00:00Z"}], pr_state={})
    assert ok and sb_id == 200 and not failures
    assert fake.calls == [("DELETE", "/repos/o/r/issues/comments/100")]


def test_identical_foreign_scoreboard_does_not_count_as_our_publication():
    fake = FakeGH(post_id=300)
    sb_id, ok, failures = run(fake, existing=[{
        "id": 200, "login": "someone-else", "body": "scoreboard body\n"}], pr_state={})
    assert ok and sb_id == 300 and not failures
    assert fake.calls == [("POST", "/repos/o/r/issues/1/comments")]


def test_identical_board_refreshes_if_another_board_is_newer_or_tied():
    for other_time in ('2026-10-19T08:01:00Z', '2026-10-19T08:00:00Z', None):
        fake = FakeGH()
        existing = [
            {"id": 300, "login": "someone-else", "body": "other", "updated_at": other_time},
            {"id": 200, "login": "bot", "body": "scoreboard body\n", "updated_at": "2026-10-19T08:00:00Z"}]
        sb_id, ok, failures = run(fake, existing, {}, plan_sb_id=200)
        assert ok and sb_id == 200 and not failures
        assert fake.calls == [("PATCH", "/repos/o/r/issues/comments/200")]


def test_known_id_edits_in_place():
    fake = FakeGH(patch_ok=True)
    sb_id, ok, failures = run(fake, existing=[{"id": 100, "login": "bot"}],
                              pr_state={"scoreboard_comment_id": 100})
    assert ok and sb_id == 100, (sb_id, ok)
    assert fake.to("PATCH") == ["/repos/o/r/issues/comments/100"], fake.calls
    assert "POST" not in fake.methods() and "DELETE" not in fake.methods(), fake.calls
    assert not failures


def test_adopts_our_existing_and_collapses_older():
    fake = FakeGH(patch_ok=True)
    pr_state = {}
    existing = [{"id": 200, "login": "bot"}, {"id": 150, "login": "bot"}, {"id": 120, "login": "bot"}]
    sb_id, ok, failures = run(fake, existing, pr_state, mine="bot")
    assert ok and sb_id == 200 and pr_state["scoreboard_comment_id"] == 200
    assert fake.to("PATCH") == ["/repos/o/r/issues/comments/200"]
    assert "POST" not in fake.methods()
    assert sorted(fake.to("DELETE")) == ["/repos/o/r/issues/comments/120",
                                         "/repos/o/r/issues/comments/150"], fake.calls
    assert not failures


def test_collapses_only_our_own_not_other_authors():
    fake = FakeGH(patch_ok=True)
    existing = [{"id": 200, "login": "bot"}, {"id": 150, "login": "alice"}, {"id": 120, "login": "bot"}]
    sb_id, ok, _ = run(fake, existing, pr_state={}, mine="bot")
    assert ok and sb_id == 200
    # Only our own older duplicate (120) is deleted; alice's (150) is left untouched.
    assert fake.to("DELETE") == ["/repos/o/r/issues/comments/120"], fake.calls


def test_only_others_scoreboard_posts_own():
    fake = FakeGH(patch_ok=True, post_id=999)
    sb_id, ok, failures = run(fake, existing=[{"id": 300, "login": "alice"}], pr_state={}, mine="bot")
    assert ok and sb_id == 999, (sb_id, ok)
    assert "PATCH" not in fake.methods(), "must not edit a comment we did not author"
    assert "DELETE" not in fake.methods(), "must not delete a comment we did not author"
    assert fake.to("POST") == ["/repos/o/r/issues/1/comments"]
    assert not failures


def test_no_existing_posts_new():
    fake = FakeGH(patch_ok=True, post_id=888)
    sb_id, ok, _ = run(fake, existing=[], pr_state={}, mine="bot")
    assert ok and sb_id == 888
    assert "PATCH" not in fake.methods()
    assert fake.to("POST") == ["/repos/o/r/issues/1/comments"]


def test_our_failed_edit_is_recorded_not_duplicated():
    fake = FakeGH(patch_ok=False, post_id=777)
    sb_id, ok, failures = run(
        fake, existing=[{"id": 100, "login": "bot"}, {"id": 90, "login": "bot"}],
        pr_state={"scoreboard_comment_id": 100}, mine="bot")
    assert not ok
    assert "POST" not in fake.methods(), "a failed edit of our own comment must not post a duplicate"
    assert "DELETE" not in fake.methods(), "a failed edit must not collapse other scoreboards"
    assert failures, "a failed edit of our own scoreboard must be recorded"


def test_discovery_failure_mutates_nothing():
    fake = FakeGH(patch_ok=True, post_id=777)
    sb_id, ok, failures = run(
        fake, existing=None, pr_state={"scoreboard_comment_id": 100}, mine="bot")
    assert not ok and sb_id is None
    assert not fake.calls, "unknown ownership must fail closed"
    assert failures == [{"action": "scoreboard discovery",
                         "error": "could not verify scoreboard ownership"}]


def test_missing_known_id_posts_fresh():
    fake = FakeGH(patch_ok=True, post_id=777)
    sb_id, ok, failures = run(
        fake, existing=[], pr_state={"scoreboard_comment_id": 100}, mine="bot")
    assert ok and sb_id == 777
    assert fake.to("POST") == ["/repos/o/r/issues/1/comments"]
    assert "PATCH" not in fake.methods()
    assert not failures


def test_known_id_owned_by_someone_else_posts_fresh():
    """The ledger names another identity's scoreboard; post ours instead of cross-editing."""
    fake = FakeGH(patch_ok=True, post_id=777)
    sb_id, ok, failures = run(
        fake, existing=[{"id": 100, "login": "alice"}],
        pr_state={"scoreboard_comment_id": 100}, mine="bot")
    assert ok and sb_id == 777, (sb_id, ok)
    assert "PATCH" not in fake.methods(), "must not PATCH a scoreboard we do not own"
    assert "DELETE" not in fake.methods(), "must not delete a comment we did not author"
    assert fake.to("POST") == ["/repos/o/r/issues/1/comments"]
    assert not failures


def test_plan_id_owned_by_someone_else_posts_fresh():
    fake = FakeGH(patch_ok=True, post_id=666)
    sb_id, ok, failures = run(
        fake, existing=[{"id": 50, "login": "other-bot"}],
        pr_state={}, plan_sb_id=50, mine="bot")
    assert ok and sb_id == 666, (sb_id, ok)
    assert "PATCH" not in fake.methods(), "must not PATCH a scoreboard we do not own"
    assert fake.to("POST") == ["/repos/o/r/issues/1/comments"]
    assert not failures


def test_known_foreign_id_reuses_our_existing_scoreboard():
    """Identity migration does not need a third scoreboard when this actor already has one."""
    fake = FakeGH(patch_ok=True, post_id=888)
    existing = [{"id": 200, "login": "bot"}, {"id": 100, "login": "alice"}]
    sb_id, ok, failures = run(
        fake, existing, pr_state={"scoreboard_comment_id": 100}, mine="bot")
    assert ok and sb_id == 200, (sb_id, ok)
    assert fake.to("PATCH") == ["/repos/o/r/issues/comments/200"], fake.calls
    assert "POST" not in fake.methods()
    assert not failures


# --- find_scoreboard_comments (complete ownership discovery + ordering) --------------------------

def _fake_run(stdout, code=0):
    def run_(args, text=True, capture_output=True):
        return types.SimpleNamespace(returncode=code, stdout=stdout, stderr="")
    return run_


def test_find_parses_all_authors_and_orders(monkeypatch=None):
    lines = "\n".join([
        '{"id":200,"login":"tauceti-review-bot[bot]","updated_at":"2026-06-18T02:00:00Z"}',
        '{"id":150,"login":"alice","updated_at":"2026-06-18T01:00:00Z"}',
        '{"id":120,"login":"mallory","updated_at":"2026-06-18T03:00:00Z"}',
    ])
    orig = post.subprocess.run
    post.subprocess.run = _fake_run(lines)
    try:
        got = post.find_scoreboard_comments("o/r", 1)
    finally:
        post.subprocess.run = orig
    ids = [c["id"] for c in got]
    assert ids == [120, 200, 150], got


def test_find_returns_none_on_api_error():
    orig = post.subprocess.run
    post.subprocess.run = _fake_run("", code=1)
    try:
        assert post.find_scoreboard_comments("o/r", 1) is None
    finally:
        post.subprocess.run = orig


def test_find_returns_none_on_malformed_response():
    orig = post.subprocess.run
    post.subprocess.run = _fake_run('{"id": 100}\nnot-json')
    try:
        assert post.find_scoreboard_comments("o/r", 1) is None
    finally:
        post.subprocess.run = orig


def test_find_review_roots_extracts_identity_head_and_run():
    body = ('<!--tauceti-rubric:api-design-->\ntext\n'
            '<!--tauceti-meta:v1 {"head_sha":"abc","runs":[{"id":"r-1"}]}-->')
    comment = {"id": 7, "node_id": "N7", "path": "x.lean", "body": body,
               "commit_id": "old", "in_reply_to_id": None,
               "user": {"login": "bot"}, "created_at": "2026-08-04T00:00:00Z"}
    orig = post.subprocess.run
    post.subprocess.run = _fake_run(json.dumps(comment))
    try:
        roots = post.find_review_roots("o/r", 1)
    finally:
        post.subprocess.run = orig
    assert roots == [{"id": 7, "node_id": "N7", "path": "x.lean", "login": "bot",
                      "rubric": "api-design", "head_sha": "abc", "run_ids": ["r-1"],
                      "created_at": "2026-08-04T00:00:00Z", "body": body}], roots


# --- transactional publication -----------------------------------------------------------------

def _post_fixture(*, thread_id=None, pending="r-new"):
    root = pathlib.Path(tempfile.mkdtemp(prefix="tauceti-post-"))
    body = root / "thread.md"; body.write_text("thread")
    scoreboard = root / "scoreboard.md"; scoreboard.write_text("scoreboard")
    cf = {"rubric": "api-design", "run_id": "r-new",
          "pending_thread_run_id": pending,
          "thread": ({"comment_id": thread_id, "path": "code/x.lean"}
                     if thread_id else None)}
    ledger_path = root / "ledger.json"
    ledger_path.write_text(json.dumps({"days": {}, "prs": {"1": {
        "rounds": [], "scoreboard_comment_id": 500,
        "pending_publication_head_sha": "h" * 40,
        "state": {"api-design": cf}}}}))
    plan = {"head_sha": "h" * 40, "round": 1, "scoreboard_comment_id": 500,
            "scoreboard_body": str(scoreboard), "threads": [{
                "rubric": "api-design", "action": "upsert", "required": True,
                "run_id": "r-new", "body": str(body), "comment_id": thread_id,
                "path": "code/x.lean"}]}
    return ledger_path, plan


def _execute(fake_gh, roots, *, thread_id=None, scoreboards=...):
    ledger_path, plan = _post_fixture(thread_id=thread_id)
    saved = (post.gh_api, post.find_review_roots, post.current_login,
             post.find_scoreboard_comments)
    post.gh_api = fake_gh
    post.find_review_roots = lambda repo, pr: roots
    post.current_login = lambda: "bot"
    found = ([{"id": 500, "login": "bot"}] if scoreboards is ... else scoreboards)
    post.find_scoreboard_comments = lambda repo, pr: None if found is None else list(found)
    try:
        status = post.execute_post("o/r", 1, plan, ledger_path)
    finally:
        (post.gh_api, post.find_review_roots, post.current_login,
         post.find_scoreboard_comments) = saved
    return status, json.loads(ledger_path.read_text())


class TransactionGH:
    def __init__(self, *, root_ok=True, scoreboard_ok=True):
        self.root_ok = root_ok
        self.scoreboard_ok = scoreboard_ok
        self.calls = []

    def __call__(self, method, endpoint, fields=None, body_file=None, failures=None, action=""):
        self.calls.append((method, endpoint))
        is_scoreboard = "/issues/comments/" in endpoint
        if is_scoreboard:
            if self.scoreboard_ok:
                return {}
        elif self.root_ok:
            return ({"id": 700, "node_id": "N700"} if method == "POST" else {})
        if failures is not None:
            failures.append({"action": action, "error": "injected"})
        return None


def test_required_root_precedes_scoreboard_and_commits_markers():
    fake = TransactionGH()
    status, ledger = _execute(fake, [])
    assert status == 0
    assert fake.calls == [
        ("POST", "/repos/o/r/pulls/1/comments"),
        ("PATCH", "/repos/o/r/issues/comments/500")], fake.calls
    pr = ledger["prs"]["1"]
    assert pr["published_head_sha"] == "h" * 40
    assert "pending_publication_head_sha" not in pr
    cf = pr["state"]["api-design"]
    assert cf["thread"]["comment_id"] == 700
    assert "pending_thread_run_id" not in cf


def test_identical_scoreboard_commits_the_publication_without_a_patch():
    fake = TransactionGH()
    status, ledger = _execute(fake, [], scoreboards=[{
        "id": 500, "login": "bot", "body": "scoreboard"}])
    assert status == 0
    assert fake.calls == [("POST", "/repos/o/r/pulls/1/comments")]
    pr = ledger["prs"]["1"]
    assert pr["published_head_sha"] == "h" * 40
    assert "pending_publication_head_sha" not in pr
    assert "pending_thread_run_id" not in pr["state"]["api-design"]


def test_required_failure_withholds_scoreboard_and_keeps_pending():
    fake = TransactionGH(root_ok=False)
    status, ledger = _execute(fake, [])
    assert status == 1
    assert all("/issues/comments/" not in endpoint for _, endpoint in fake.calls), fake.calls
    pr = ledger["prs"]["1"]
    assert pr["pending_publication_head_sha"] == "h" * 40
    assert pr["state"]["api-design"]["pending_thread_run_id"] == "r-new"


def test_scoreboard_failure_saves_root_but_keeps_transaction_pending():
    fake = TransactionGH(scoreboard_ok=False)
    status, ledger = _execute(fake, [])
    assert status == 1
    cf = ledger["prs"]["1"]["state"]["api-design"]
    assert cf["thread"]["comment_id"] == 700, "confirmed root id must survive the failure"
    assert cf["pending_thread_run_id"] == "r-new"
    assert ledger["prs"]["1"]["pending_publication_head_sha"] == "h" * 40


def test_crash_retry_adopts_exact_remote_root_without_duplicate():
    fake = TransactionGH()
    roots = [{"id": 701, "node_id": "N701", "path": "code/x.lean", "login": "bot",
              "rubric": "api-design", "head_sha": "h" * 40, "run_ids": ["r-new"],
              "created_at": "2026-08-04T00:00:00Z"}]
    status, ledger = _execute(fake, roots)
    assert status == 0
    assert fake.calls == [("PATCH", "/repos/o/r/issues/comments/500")], fake.calls
    assert ledger["prs"]["1"]["state"]["api-design"]["thread"]["comment_id"] == 701


def test_foreign_known_root_is_not_edited():
    fake = TransactionGH()
    roots = [{"id": 41, "node_id": "N41", "path": "code/x.lean", "login": "other-bot",
              "rubric": "api-design", "head_sha": "g" * 40, "run_ids": ["r-old"],
              "created_at": "2026-08-03T00:00:00Z"}]
    status, ledger = _execute(fake, roots, thread_id=41)
    assert status == 0
    assert ("PATCH", "/repos/o/r/pulls/comments/41") not in fake.calls
    assert fake.calls[0] == ("POST", "/repos/o/r/pulls/1/comments"), fake.calls
    assert ledger["prs"]["1"]["state"]["api-design"]["thread"]["comment_id"] == 700


def test_foreign_known_scoreboard_is_not_edited():
    """At execute_post level, a stored foreign scoreboard is never cross-edited."""
    fake = TransactionGH()
    status, ledger = _execute(
        fake, [], scoreboards=[{"id": 500, "login": "other-bot"}])
    assert status == 0
    assert ("PATCH", "/repos/o/r/issues/comments/500") not in fake.calls
    assert ("POST", "/repos/o/r/issues/1/comments") in fake.calls, fake.calls
    assert ledger["prs"]["1"]["scoreboard_comment_id"] == 700


def test_scoreboard_discovery_failure_keeps_publication_pending():
    fake = TransactionGH()
    status, ledger = _execute(fake, [], scoreboards=None)
    assert status == 1
    assert fake.calls == [("POST", "/repos/o/r/pulls/1/comments")], fake.calls
    pr = ledger["prs"]["1"]
    assert pr["pending_publication_head_sha"] == "h" * 40
    assert pr["state"]["api-design"]["pending_thread_run_id"] == "r-new"


def test_optional_close_failure_does_not_roll_back_publication():
    ledger_path, plan = _post_fixture()
    close_body = ledger_path.parent / "close.md"; close_body.write_text("close")
    plan["threads"].append({"rubric": "reuse", "action": "close", "required": False,
                            "body": str(close_body), "comment_id": 80})
    ledger = json.loads(ledger_path.read_text())
    ledger["prs"]["1"]["state"]["reuse"] = {"thread": {"comment_id": 80}}
    ledger_path.write_text(json.dumps(ledger))
    roots = [{"id": 80, "node_id": "N80", "path": "x.lean", "login": "bot",
              "rubric": "reuse", "head_sha": "g" * 40, "run_ids": ["r-old"],
              "created_at": "2026-08-03T00:00:00Z"}]

    class OptionalFailureGH(TransactionGH):
        def __call__(self, method, endpoint, fields=None, body_file=None, failures=None, action=""):
            if endpoint.endswith("/pulls/comments/80"):
                self.calls.append((method, endpoint))
                failures.append({"action": action, "error": "injected optional failure"})
                return None
            return super().__call__(method, endpoint, fields, body_file, failures, action)

    fake = OptionalFailureGH()
    saved = (post.gh_api, post.find_review_roots, post.current_login,
             post.find_scoreboard_comments)
    post.gh_api = fake
    post.find_review_roots = lambda repo, pr: roots
    post.current_login = lambda: "bot"
    post.find_scoreboard_comments = lambda repo, pr: []
    try:
        status = post.execute_post("o/r", 1, plan, ledger_path)
    finally:
        (post.gh_api, post.find_review_roots, post.current_login,
         post.find_scoreboard_comments) = saved
    assert status == 0
    assert fake.calls[-1] == ("PATCH", "/repos/o/r/pulls/comments/80"), fake.calls



def test_identical_closed_body_skips_patch_but_resolves_thread():
    ledger_path, plan = _post_fixture()
    close_body = ledger_path.parent / "close.md"; close_body.write_text("close")
    plan["threads"].append({"rubric": "reuse", "action": "close", "required": False,
                            "body": str(close_body), "comment_id": 80})
    ledger = json.loads(ledger_path.read_text())
    ledger["prs"]["1"]["state"]["reuse"] = {"thread": {"comment_id": 80}}
    ledger_path.write_text(json.dumps(ledger))
    roots = [{"id": 80, "node_id": "N80", "path": "x.lean", "login": "bot",
              "rubric": "reuse", "head_sha": "g" * 40, "run_ids": ["r-old"],
              "created_at": "2026-08-03T00:00:00Z", "body": "close"}]

    class OptionalFailureGH(TransactionGH):
        def __call__(self, method, endpoint, fields=None, body_file=None, failures=None, action=""):
            if endpoint.endswith("/pulls/comments/80"):
                self.calls.append((method, endpoint))
                failures.append({"action": action, "error": "injected optional failure"})
                return None
            return super().__call__(method, endpoint, fields, body_file, failures, action)

    fake = OptionalFailureGH()
    saved = (post.gh_api, post.find_review_roots, post.current_login,
             post.find_scoreboard_comments, post.resolve_thread)
    post.gh_api = fake
    post.find_review_roots = lambda repo, pr: roots
    post.current_login = lambda: "bot"
    post.find_scoreboard_comments = lambda repo, pr: []
    resolved = []
    post.resolve_thread = lambda repo, pr, cid: resolved.append(cid)
    try:
        status = post.execute_post("o/r", 1, plan, ledger_path)
    finally:
        (post.gh_api, post.find_review_roots, post.current_login,
         post.find_scoreboard_comments, post.resolve_thread) = saved
    assert status == 0
    assert ("PATCH", "/repos/o/r/pulls/comments/80") not in fake.calls
    assert resolved == [80]


def test_validation_json_is_logged_and_retained_without_credentials():
    import contextlib, io, json
    from unittest.mock import patch
    payload = {"message": "Validation Failed", "errors": [
        {"resource": "PullRequestReviewComment", "code": "custom",
         "message": "was submitted too quickly: diagnostic-token"}],
        "status": "422", "documentation_url": "https://docs.github.com/rest",
        "request_body": "must not be logged"}
    failures, stderr = [], io.StringIO()
    result = types.SimpleNamespace(returncode=1, stdout=json.dumps(payload),
                                   stderr="gh: Validation Failed (HTTP 422)")
    with (patch.object(post.subprocess, "run", return_value=result),
          patch.dict(post.os.environ, GH_TOKEN="diagnostic-token"),
          contextlib.redirect_stderr(stderr)):
        assert post.gh_api("POST", "/repos/o/r/pulls/1/comments", failures=failures) is None
    detail = failures[0]["response"]
    assert detail["status"] == "422"
    assert detail["errors"][0]["message"] == "was submitted too quickly: [REDACTED]"
    assert "GitHub error JSON:" in stderr.getvalue()
    assert "diagnostic-token" not in stderr.getvalue()
    assert "must not be logged" not in stderr.getvalue()
    assert "request_body" not in detail


def test_non_json_error_response_keeps_original_failure():
    from unittest.mock import patch
    result = types.SimpleNamespace(returncode=1, stdout="<html>gateway failure</html>",
                                   stderr="gh: HTTP 502")
    failures = []
    with patch.object(post.subprocess, "run", return_value=result):
        assert post.gh_api("POST", "/repos/o/r/pulls/1/comments", failures=failures) is None
    assert failures == [{"action": "POST /repos/o/r/pulls/1/comments", "error": "gh: HTTP 502"}]


def _throttled_comment(headers="", message="was submitted too quickly"):
    return types.SimpleNamespace(returncode=1, stderr="gh: Validation Failed (HTTP 422)",
        stdout="HTTP/2.0 422 Unprocessable Entity\n" + headers + "\n" + json.dumps({
            "message": "Validation Failed", "errors": [{
                "resource": "PullRequestReviewComment", "message": message}]}))


def test_throttled_comment_retries_same_request_and_returns_confirmed_id():
    from unittest.mock import patch
    success = types.SimpleNamespace(returncode=0, stderr="",
        stdout='HTTP/2.0 201 Created\nContent-Type: application/json\n\n{"id":123}')
    failures = []
    with (patch.object(post.subprocess, "run", side_effect=[_throttled_comment(), success]) as gh,
          patch.object(post.time, "sleep") as sleep,
          patch.object(post.random, "uniform", return_value=3)):
        assert post.gh_api("POST", "/repos/o/r/pulls/1/comments", body_file="body.md",
                           failures=failures) == {"id": 123}
    assert not failures
    assert [call.args[0] for call in sleep.call_args_list] == [2, 63]
    assert gh.call_count == 2 and gh.call_args_list[0] == gh.call_args_list[1]
    assert "--include" in gh.call_args.args[0]


def test_comment_throttle_doubles_waits_and_exhaustion_retains_failure():
    from unittest.mock import patch
    failures = []
    with (patch.object(post.subprocess, "run", return_value=_throttled_comment()) as gh,
          patch.object(post.time, "sleep") as sleep,
          patch.object(post.random, "uniform", return_value=0)):
        assert post.gh_api("PATCH", "/repos/o/r/pulls/comments/123", body_file="body.md",
                           failures=failures, action="required thread PATCH naming") is None
    assert gh.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [2, 60, 120, 240]
    assert len(failures) == 1
    assert failures[0]["action"] == "required thread PATCH naming"
    assert failures[0]["response"]["errors"][0]["message"] == "was submitted too quickly"


def test_comment_throttle_honors_retry_after():
    from unittest.mock import patch
    success = types.SimpleNamespace(returncode=0, stdout='{"id":123}', stderr="")
    with (patch.object(post.subprocess, "run", side_effect=[
              _throttled_comment("Retry-After: 180\n"), success]),
          patch.object(post.time, "sleep") as sleep,
          patch.object(post.random, "uniform", return_value=0)):
        assert post.gh_api("POST", "/repos/o/r/pulls/1/comments", body_file="body.md") == {"id":123}
    assert [call.args[0] for call in sleep.call_args_list] == [2, 180]


def test_other_validation_errors_are_not_retried():
    from unittest.mock import patch
    failures = []
    with (patch.object(post.subprocess, "run", return_value=_throttled_comment(
              message="path is not part of the diff")) as gh,
          patch.object(post.time, "sleep") as sleep):
        assert post.gh_api("POST", "/repos/o/r/pulls/1/comments", body_file="body.md",
                           failures=failures) is None
    assert gh.call_count == 1 and sleep.call_args.args == (2,)
    assert len(failures) == 1


def test_abuse_response_retries_without_repeating_model():
    from unittest.mock import patch
    abuse = types.SimpleNamespace(returncode=1, stdout='HTTP/2.0 422 Unprocessable Entity\n\n' + json.dumps({"status":"422", "errors":[{"resource":"PullRequestReview", "code":"abuse", "field":"base"}]}), stderr='rejected')
    success = types.SimpleNamespace(returncode=0, stdout='{"id":123}', stderr='')
    with (patch.object(post.subprocess, 'run', side_effect=[abuse, success]) as gh,
          patch.object(post.time, 'sleep') as sleep,
          patch.object(post.random, 'uniform', return_value=0)):
        assert post.gh_api('POST', '/repos/o/r/pulls/1/comments/2/replies', body_file='body.md') == {"id":123}
        assert gh.call_count == 2
        assert [c.args[0] for c in sleep.call_args_list] == [2, 60]


def test_shared_transport_owns_pacing_and_retry_budget():
    from unittest.mock import patch
    with (patch.dict(post.os.environ, {"TAUCETI_GITHUB_SHARED_PACING":"/tmp/shared-gh"}),
          patch.object(post.shutil, "which", return_value="/tmp/shared-gh"),
          patch.object(post.subprocess, 'run', return_value=_throttled_comment()) as gh,
          patch.object(post.time, 'sleep') as sleep):
        assert post.gh_api('POST', '/repos/o/r/pulls/1/comments', body_file='body.md') is None
        assert gh.call_count == 1 and not sleep.called


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nall {len(fns)} scoreboard-poster checks passed")
