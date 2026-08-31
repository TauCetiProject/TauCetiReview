#!/usr/bin/env python3
"""The review-in-progress marker must de-contend concurrent reviewers so a commit is reviewed once.

Guards the fleet-dedup contract: a run yields the WHOLE head if any other reviewer already holds it,
regardless of model (one review per (pr, head)); only a new push — a fresh head — is a fresh unit.
Posting needs only comment access, so an independent reviewer with no repo write still coordinates.
Dependency-free — run with `python tests/test_coordinate.py` or under pytest.
"""
import contextlib
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import cli  # noqa: E402

H = "abcdef1234567890abcdef1234567890abcdef12"  # full-length head; matching is exact


def marker(cid, nonce, providers, head=H, exp=9_999_999_999):  # far future unless a test overrides
    body = ('x <!--tauceti-review-in-progress '
            + cli.json.dumps({"nonce": nonce, "providers": providers, "head": head,
                              "expires_at": exp})
            + '--> y')
    return {"id": cid, "body": body}


def test_covered_providers():
    now = 1000
    comments = [
        marker(10, "A", ["codex"]),
        marker(11, "B", ["claude"]),
        marker(12, "C", ["codex"], exp=500),                 # expired → ignored
        marker(13, "A", ["codex"], head="f" * 40),           # other head → ignored (exact match)
        marker(14, "D", ["codex"], head=H[:12]),             # truncated head → ignored (not exact)
        {"id": 15, "body": "a plain human comment, no marker"},
    ]
    assert cli.covered_providers(comments, H, now, exclude_nonce="Z") == {"codex", "claude"}
    # our own marker (nonce A) is never a conflict with ourselves
    assert cli.covered_providers(comments, H, now, exclude_nonce="A") == {"claude"}
    # tiebreak: only markers with a lower comment id count
    assert cli.covered_providers(comments, H, now, exclude_nonce="Z", max_id=11) == {"codex"}


class FakeGH:
    """Replaces cli.issue_comments / post_marker / delete_marker. `before` is the foreign markers on the
    first (pre-post) read; `after` on every later read; a post id makes our own comment visible on later
    reads. `after_sequence` models delayed replication. Reads listed in `fail_reads` return None."""
    def __init__(self, before=None, after=None, after_sequence=None, post_ids=(999,), post_fails=False,
                 own_visible=True, fail_reads=()):
        self.before = before or []
        self.after = after if after is not None else (before or [])
        self.after_sequence = list(after_sequence or [])
        self.post_ids = list(post_ids)
        self.post_fails = post_fails
        self.own_visible = own_visible
        self.fail_reads = set(fail_reads)
        self.reads = 0
        self.posted = []
        self.deleted = []
        self.last_post_id = None

    def issue_comments(self, repo, pr):
        self.reads += 1
        if self.reads in self.fail_reads:
            return None
        if self.reads == 1:
            base = list(self.before)
        elif self.after_sequence:
            base = list(self.after_sequence.pop(0))
        else:
            base = list(self.after)
        if self.last_post_id is not None and self.own_visible:
            base.append({"id": self.last_post_id, "body": "our own marker (visible)"})
        return base

    def post_marker(self, repo, pr, head, providers, nonce, submitted_by):
        if self.post_fails:
            return None
        self.posted.append(list(providers))
        self.last_post_id = self.post_ids.pop(0) if self.post_ids else 999
        return self.last_post_id

    def delete_marker(self, repo, cid):
        self.deleted.append(cid)


_ORIG = {}


def _wire(g):
    for name in ("issue_comments", "post_marker", "delete_marker", "_install_marker_cleanup",
                 "COORD_SETTLE_S"):
        _ORIG.setdefault(name, getattr(cli, name))
    cli.issue_comments = g.issue_comments
    cli.post_marker = g.post_marker
    cli.delete_marker = g.delete_marker
    cli._install_marker_cleanup = lambda: None   # never touch real signal/atexit state in a test
    cli.COORD_SETTLE_S = 0.0                     # ordinary unit cases need only one reconciliation read
    cli._ACTIVE_MARKERS = []
    cli._CLEANUP_INSTALLED = False


def _unwire():
    for name, fn in _ORIG.items():
        setattr(cli, name, fn)
    cli._ACTIVE_MARKERS = []
    cli._CLEANUP_INSTALLED = False


def test_no_markers_runs_all():
    g = FakeGH()
    _wire(g)
    try:
        out = cli.coordinate("r", 1, H, ["claude", "codex"], "me")
        assert out == ["claude", "codex"]
        assert g.posted == [["claude", "codex"]]      # one marker for the whole set
        assert cli._ACTIVE_MARKERS == [("r", 999)]    # cleanup armed
        assert g.deleted == []
    finally:
        _unwire()


def test_all_covered_skips_without_posting():
    g = FakeGH(before=[marker(5, "other", ["claude", "codex"])])
    _wire(g)
    try:
        out = cli.coordinate("r", 1, H, ["claude", "codex"], "me")
        assert out == []
        assert g.posted == [] and cli._ACTIVE_MARKERS == []   # spent nothing, posted nothing
    finally:
        _unwire()


def test_any_foreign_marker_skips_whole_head():
    # A foreign marker for a DIFFERENT model still owns the head: we yield entirely (one review per head).
    g = FakeGH(before=[marker(5, "other", ["codex"])])
    _wire(g)
    try:
        out = cli.coordinate("r", 1, H, ["claude"], "me")
        assert out == []                              # claude does NOT proceed — the head is taken
        assert g.posted == [] and cli._ACTIVE_MARKERS == []   # spent nothing, posted nothing
    finally:
        _unwire()


def test_lost_post_race_yields():
    # No foreign marker pre-post; on the recheck a LOWER-id racer for our provider has appeared.
    g = FakeGH(before=[], after=[marker(1, "racer", ["claude"])], post_ids=(999,))
    _wire(g)
    try:
        out = cli.coordinate("r", 1, H, ["claude"], "me")
        assert out == []
        assert g.deleted == [999]                     # deleted our own marker on losing
        assert cli._ACTIVE_MARKERS == []
    finally:
        _unwire()


def test_lost_race_to_other_model_yields():
    # Post [claude]; recheck shows a lower-id racer running a DIFFERENT model (codex) → we yield the
    # head entirely (head-level de-contention: the lowest-id claimer wins regardless of model).
    g = FakeGH(before=[], after=[marker(1, "racer", ["codex"])], post_ids=(999,))
    _wire(g)
    try:
        out = cli.coordinate("r", 1, H, ["claude"], "me")
        assert out == []
        assert g.deleted == [999]                             # deleted our own marker on losing
        assert cli._ACTIVE_MARKERS == []
    finally:
        _unwire()


def test_delayed_lower_id_marker_still_wins_after_own_is_visible():
    """Regression: seeing our own write is not proof an earlier peer's write reached this replica."""
    lower = marker(1, "racer", ["codex"])
    g = FakeGH(before=[], after_sequence=[[], [], [lower]], post_ids=(999,))
    g.last_post_id = 999  # our marker is visible immediately; the lower-id peer is replication-delayed
    _wire(g)
    try:
        ticks = iter((0.0, 0.0, 0.5, 1.0, 1.5))
        lost = cli._recheck_lost(
            "r", 1, H, "ours", 999,
            settle_s=2.0,
            poll_s=0.5,
            monotonic=lambda: next(ticks),
            sleeper=lambda _delay: None,
        )
        assert lost == {"codex"}
    finally:
        _unwire()


def test_post_failure_proceeds_unclaimed():
    g = FakeGH(post_fails=True)
    _wire(g)
    try:
        out = cli.coordinate("r", 1, H, ["claude", "codex"], "me")
        assert out == ["claude", "codex"]             # no comment access → review anyway, no cleanup
        assert cli._ACTIVE_MARKERS == []
    finally:
        _unwire()


def test_list_failure_proceeds_and_posts():
    # The initial list read fails (None, not empty): proceed, but still post a marker so we're visible.
    g = FakeGH(fail_reads={1})
    _wire(g)
    try:
        out = cli.coordinate("r", 1, H, ["claude"], "me")
        assert out == ["claude"]
        assert g.posted == [["claude"]]               # distinguishing None from [] still posts
        assert cli._ACTIVE_MARKERS == [("r", 999)]
    finally:
        _unwire()


def test_settlement_read_failure_warns_and_proceeds():
    g = FakeGH(fail_reads={2})
    _wire(g)
    try:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            out = cli.coordinate("r", 1, H, ["claude"], "me")
        assert out == ["claude"]
        assert "concurrent duplicate review is possible" in stderr.getvalue()
        assert cli._ACTIVE_MARKERS == [("r", 999)]
    finally:
        _unwire()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all coordinate tests passed")
