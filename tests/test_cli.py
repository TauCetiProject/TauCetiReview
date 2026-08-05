#!/usr/bin/env python3
"""Focused tests for the user-facing tauceti-review CLI."""
import json
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import cli  # noqa: E402


def test_pr_ref_oids_uses_old_gh_compatible_rest_fields():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return types.SimpleNamespace(
            stdout=json.dumps({"head": {"sha": "head-sha"}, "base": {"sha": "base-sha"}})
        )

    original = cli.run
    cli.run = fake_run
    try:
        assert cli.pr_ref_oids("owner/repo", 42) == ("head-sha", "base-sha")
    finally:
        cli.run = original

    assert calls == [
        (["gh", "api", "/repos/owner/repo/pulls/42"], {"capture": True, "quiet": True})
    ]


if __name__ == "__main__":
    test_pr_ref_oids_uses_old_gh_compatible_rest_fields()
    print("ok  test_pr_ref_oids_uses_old_gh_compatible_rest_fields")
