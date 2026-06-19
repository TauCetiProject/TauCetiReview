#!/usr/bin/env python3
"""The review-in-progress marker must de-contend concurrent reviewers without blocking distinct work.

Guards the fleet-dedup contract: a reviewer skips a provider another reviewer is already running on
the same head (no duplicate spend), but a different model — or the same model after a new push — is a
distinct unit that still runs and reaches the backend DB. Posting needs only comment access, so an
independent reviewer with no repo write still coordinates. Dependency-free — run with
`python tests/test_coordinate.py` or under pytest.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import cli  # noqa: E402

H = "abcdef1234567890"


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
        marker(12, "C", ["codex"], exp=500),        # expired → ignored
        marker(13, "A", ["codex"], head="deadbeef"),  # other head → ignored
        {"id": 14, "body": "a plain human comment, no marker"},
    ]
    assert cli.covered_providers(comments, H, now, exclude_nonce="Z") == {"codex", "claude"}
    # our own marker (nonce A) is never a conflict with ourselves
    assert cli.covered_providers(comments, H, now, exclude_nonce="A") == {"claude"}
    # tiebreak: only markers with a lower comment id count
    assert cli.covered_providers(comments, H, now, exclude_nonce="Z", max_id=11) == {"codex"}


class FakeGH:
    """Replaces cli.issue_comments / post_marker / delete_marker. issue_comments returns the next
    queued snapshot each call (so a test can show a racer appearing only on the recheck)."""
    def __init__(self, snapshots, post_id=999, post_fails=False):
        self.snapshots = list(snapshots)
        self.post_id = post_id
        self.post_fails = post_fails
        self.posted = []
        self.deleted = []
        self.registered = []

    def issue_comments(self, repo, pr):
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def post_marker(self, repo, pr, head, providers, nonce, submitted_by):
        if self.post_fails:
            return None
        self.posted.append(list(providers))
        return self.post_id

    def delete_marker(self, repo, cid):
        self.deleted.append(cid)

    def register(self, fn, *a):
        self.registered.append((fn, a))


def _wire(monkey):
    cli.issue_comments = monkey.issue_comments
    cli.post_marker = monkey.post_marker
    cli.delete_marker = monkey.delete_marker
    cli.atexit.register = monkey.register


def test_no_markers_runs_all():
    g = FakeGH(snapshots=[[]])
    _wire(g)
    out = cli.coordinate("r", 1, H, ["claude", "codex"], "me")
    assert out == ["claude", "codex"]
    assert g.posted == [["claude", "codex"]]      # posted a marker for the whole set
    assert g.registered and not g.deleted          # cleanup armed, nothing deleted


def test_all_covered_skips_without_posting():
    g = FakeGH(snapshots=[[marker(5, "other", ["claude", "codex"])]])
    _wire(g)
    out = cli.coordinate("r", 1, H, ["claude", "codex"], "me")
    assert out == []
    assert g.posted == [] and g.registered == []   # spent nothing, posted nothing


def test_partial_cover_runs_remainder():
    g = FakeGH(snapshots=[[marker(5, "other", ["codex"])]])
    _wire(g)
    out = cli.coordinate("r", 1, H, ["claude", "codex"], "me")
    assert out == ["claude"]                        # codex deferred, claude proceeds → reaches the DB
    assert g.posted == [["claude"]]


def test_lost_post_race_yields():
    # No marker at first; on the recheck a LOWER-id racer for our provider has appeared → we yield.
    g = FakeGH(snapshots=[[], [marker(1, "racer", ["claude"])]], post_id=999)
    _wire(g)
    out = cli.coordinate("r", 1, H, ["claude"], "me")
    assert out == []
    assert g.deleted == [999]                       # we deleted our own marker on losing


def test_post_failure_proceeds_unclaimed():
    g = FakeGH(snapshots=[[]], post_fails=True)
    _wire(g)
    out = cli.coordinate("r", 1, H, ["claude", "codex"], "me")
    assert out == ["claude", "codex"]               # no comment access → review anyway, no cleanup
    assert g.registered == []


if __name__ == "__main__":
    import atexit as _ax
    _real_register = _ax.register
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            cli.atexit.register = _real_register     # restore between cases
            print(f"ok  {name}")
    print("all coordinate tests passed")
