#!/usr/bin/env python3
"""The scoreboard identifies the community reviewer that published it for downstream affinity."""

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import render  # noqa: E402


META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)


def scoreboard_meta(submitted_by):
    body = render.render_scoreboard(
        [], {}, "a" * 40, "approved", "",
        prov={"repo": "r", "pr": 1, "submitted_by": submitted_by},
    )
    return json.loads(META_RE.findall(body)[-1])


def test_submitted_by_is_machine_readable():
    assert scoreboard_meta("community-reviewer")["submitted_by"] == "community-reviewer"


def test_empty_submitted_by_is_omitted():
    assert "submitted_by" not in scoreboard_meta("")


def test_cli_sha_is_recorded_and_shown():
    body = render.render_scoreboard([], {}, "a" * 40, "approved", "",
                                    prov={"repo": "r", "pr": 1, "cli_sha": "c" * 40})
    assert json.loads(META_RE.findall(body)[-1])["cli_sha"] == "c" * 40
    assert "CLI @ `ccccccc`" in body and "(modified)" not in body


def test_dirty_cli_is_marked_modified():
    body = render.render_scoreboard([], {}, "a" * 40, "approved", "",
                                    prov={"repo": "r", "pr": 1, "cli_sha": "c" * 40,
                                          "cli_dirty": True})
    assert json.loads(META_RE.findall(body)[-1])["cli_dirty"] is True
    assert "CLI @ `ccccccc` (modified)" in body


def test_missing_cli_sha_is_omitted():
    body = render.render_scoreboard([], {}, "a" * 40, "approved", "", prov={"repo": "r", "pr": 1})
    assert "CLI @" not in body and "cli_sha" not in body


def test_round_archive_records_cli_sha():
    import argparse
    import tempfile
    import review
    with tempfile.TemporaryDirectory() as outbox:
        a = argparse.Namespace(archive_dir=outbox, dry_run=False, arm="production", pr=1, repo="r",
                               mode="commit", submitted_by=None, base_sha=None, merge_base_sha=None,
                               rubrics_sha=None)
        for pr, dirty in ((1, True), (2, False)):  # a verified-clean CLI is recorded, not omitted
            a.pr = pr
            prov = {"round": 1, "cli_sha": "c" * 40, "cli_dirty": dirty}
            review.emit_round_archive(a, prov, "a" * 40, [], [], {}, "approved", None, 0, "", "v")
            rec = json.loads(next(pathlib.Path(outbox).rglob(f"{pr}-1.json")).read_text())
            assert rec["cli_sha"] == "c" * 40 and rec["cli_dirty"] is dirty


def test_drifted_rubrics_warn_on_scoreboard_and_threads():
    prov = {"repo": "r", "pr": 1, "rubrics_sha": "f" * 40, "rubrics_drift": True,
            "rubrics_published": True}
    body = render.render_scoreboard(["naming"], {}, "a" * 40, "approved", "", prov=prov)
    assert render.DRIFT_WARNING in body
    assert json.loads(META_RE.findall(body)[-1])["rubrics_drift"] is True
    thread = render.render_thread({"rubric": "naming", "verdict": "request_changes"}, prov)
    assert "rubrics differ from published main" in thread


def test_current_or_unchecked_rubrics_do_not_warn():
    for drift in (False, None):
        prov = {"repo": "r", "pr": 1, "rubrics_sha": "f" * 40, "rubrics_drift": drift}
        body = render.render_scoreboard(["naming"], {}, "a" * 40, "approved", "", prov=prov)
        thread = render.render_thread({"rubric": "naming", "verdict": "request_changes"}, prov)
        assert "⚠️ This review" not in body and "differ from published" not in thread


def test_unpublished_rubrics_commit_is_not_linked():
    sha = "f" * 40
    prov = {"repo": "r", "pr": 1, "rubrics_sha": sha, "rubrics_published": False}
    body = render.render_scoreboard(["naming"], {}, "a" * 40, "approved", "", prov=prov)
    assert f"/tree/{sha}" not in body and f"/blob/{sha}" not in body
    assert "rubrics @ `fffffff` (not on GitHub)" in body
    assert "| naming |" in body  # rubric name shown unlinked
    thread = render.render_thread({"rubric": "naming", "verdict": "request_changes"}, prov)
    assert "/blob/main/rubrics/naming.md" in thread and f"/blob/{sha}/" not in thread
    published = render.render_scoreboard(["naming"], {}, "a" * 40, "approved", "",
                                         prov={**prov, "rubrics_published": True})
    assert f"/blob/{sha}/rubrics/naming.md" in published


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} scoreboard metadata checks passed")
