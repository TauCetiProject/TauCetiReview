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

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[:2] == ["gh", "api"] and cmd[2].endswith("/pulls/42"), calls
    assert kwargs.get("capture") is True


def test_pr_ref_lookup_does_not_require_new_pr_view_field():
    source = pathlib.Path(cli.__file__).read_text()
    assert '"headRefOid,baseRefOid"' not in source


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} CLI checks passed")
