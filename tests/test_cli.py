#!/usr/bin/env python3
"""Focused tests for the user-facing tauceti-review CLI."""
import json
import pathlib
import sys
import tempfile
import types
from unittest.mock import patch

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


def test_model_overrides_reach_engine_only_when_explicit():
    class EngineReached(Exception):
        pass

    for models in ({}, {"codex": "gpt-5.6-terra"}, {"codex": "gpt-6-astra"},
                   {"claude": "claude-opus-5"},
                   {"claude": "claude-opus-5", "codex": "gpt-5.6-terra"}):
        commands = []

        def fake_run(cmd, **kwargs):
            if len(cmd) > 1 and pathlib.Path(cmd[1]).name == "review.py":
                commands.append(cmd)
                raise EngineReached
            assert cmd[0] == "git", cmd
            return types.SimpleNamespace(returncode=0, stdout="")

        def fake_diff(cmd, **kwargs):
            assert cmd[:3] == ["gh", "pr", "diff"], cmd
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with tempfile.TemporaryDirectory() as tmp:
            argv = ["tauceti-review", "42", "--reviewer", "claude,codex", "--submitted-by", "tester",
                    "--no-archive", "--no-mathlib", "--workdir", tmp, "--fresh"]
            for provider, model in models.items():
                argv += [f"--{provider}-model", model]
            with (patch.object(sys, "argv", argv),
                  patch.object(cli, "run", side_effect=fake_run),
                  patch.object(cli.subprocess, "run", side_effect=fake_diff),
                  patch.object(cli.shutil, "which", return_value="/fake/bin/tool"),
                  patch.multiple(cli,
                      need=lambda *args: None,
                      resolve_repo_dir=lambda _: pathlib.Path(tmp),
                      pr_ref_oids=lambda *args: ("head", "base"),
                      gh_json=lambda *args: {},
                      fetch_thread_replies=lambda *args: {},
                      rubrics_repo_sha=lambda *args: ("rubrics-sha", False),
                      merge_base_sha=lambda *args: "merge-base")):
                try:
                    cli.main()
                except EngineReached:
                    pass
            assert len(commands) == 1, commands
            cmd = commands[0]
            for provider in ("claude", "codex"):
                flag = f"--{provider}-model"
                if provider not in models:
                    assert flag not in cmd, cmd
                else:
                    assert cmd.count(flag) == 1, cmd
                    assert cmd[cmd.index(flag) + 1] == models[provider], cmd


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} CLI checks passed")
