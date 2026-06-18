#!/usr/bin/env python3
"""The scoreboard poster must publish exactly one comment per PR, editing OUR own in place.

Guards kim-em/TauCetiWorker#3: a review run whose local store lacks the scoreboard comment id used
to POST a duplicate. It now discovers the scoreboard WE authored from GitHub, edits it, and collapses
our older duplicates — and only ever mutates comments we authored. Dependency-free — run with
`python tests/test_post.py` or under pytest.
"""
import sys
import pathlib
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
    post.find_scoreboard_comments = lambda repo, pr: list(existing)
    failures = []
    try:
        sb_id, ok = post.upsert_scoreboard("o/r", 1, "body.md", plan_sb_id, pr_state, failures,
                                           mine=mine)
    finally:
        post.gh_api, post.find_scoreboard_comments = saved_gh, saved_find  # don't leak into other tests
    return sb_id, ok, failures


# --- upsert_scoreboard --------------------------------------------------------------------------

def test_known_id_edits_in_place():
    fake = FakeGH(patch_ok=True)
    sb_id, ok, failures = run(fake, existing=[{"id": 999, "login": "bot"}],
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
    sb_id, ok, failures = run(fake, existing=[], pr_state={"scoreboard_comment_id": 100}, mine="bot")
    assert not ok
    assert "POST" not in fake.methods(), "a failed edit of our own comment must not post a duplicate"
    assert failures, "a failed edit of our own scoreboard must be recorded"


# --- find_scoreboard_comments (parsing + trust + ordering) ---------------------------------------

def _fake_run(stdout, code=0):
    def run_(args, text=True, capture_output=True):
        return types.SimpleNamespace(returncode=code, stdout=stdout, stderr="")
    return run_


def test_find_parses_filters_and_orders(monkeypatch=None):
    lines = "\n".join([
        '{"id":200,"login":"tauceti-review-bot[bot]","assoc":"NONE","updated_at":"2026-06-18T02:00:00Z"}',
        '{"id":150,"login":"alice","assoc":"COLLABORATOR","updated_at":"2026-06-18T01:00:00Z"}',
        '{"id":120,"login":"mallory","assoc":"NONE","updated_at":"2026-06-18T03:00:00Z"}',  # untrusted
    ])
    orig = post.subprocess.run
    post.subprocess.run = _fake_run(lines)
    try:
        got = post.find_scoreboard_comments("o/r", 1)
    finally:
        post.subprocess.run = orig
    ids = [c["id"] for c in got]
    # mallory is dropped (assoc NONE, not the bot); bot(02:00) is newer than alice(01:00).
    assert ids == [200, 150], got


def test_find_returns_empty_on_api_error():
    orig = post.subprocess.run
    post.subprocess.run = _fake_run("", code=1)
    try:
        assert post.find_scoreboard_comments("o/r", 1) == []
    finally:
        post.subprocess.run = orig


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nall {len(fns)} scoreboard-poster checks passed")
