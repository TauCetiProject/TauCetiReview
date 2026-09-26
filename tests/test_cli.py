#!/usr/bin/env python3
"""Focused tests for the user-facing tauceti-review CLI."""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import types
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import cli  # noqa: E402
import reviewers  # noqa: E402


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


def test_claude_model_reaches_engine_and_flag_overrides_worker_environment():
    class EngineCalled(Exception):
        def __init__(self, cmd):
            self.cmd = cmd

    def fake_run(cmd, **kwargs):
        if len(cmd) > 1 and pathlib.Path(cmd[1]).name == "review.py":
            raise EngineCalled(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    cases = [
        ("", [], None),
        # Whitespace-only is truthy and easy to produce from a CI variable; it must not reach
        # the engine and fail there as an unpriced model.
        ("   ", [], None),
        ("claude-fable-5-1", [], "claude-fable-5-1"),
        ("", ["--claude-model", "  claude-fable-5-1  "], "claude-fable-5-1"),
        ("", ["--claude-model", "claude-fable-5-1"], "claude-fable-5-1"),
        ("claude-fable-5-1", ["--claude-model", "claude-opus-5"], "claude-opus-5"),
    ]
    for env_model, flags, expected in cases:
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            argv = ["tauceti-review", "1", "--reviewer", "claude", "--no-archive",
                    "--no-mathlib", "--workdir", tmp, "--store", tmp + "/store",
                    "--submitted-by", "test-reviewer", *flags]
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(patch.dict(os.environ, {"TAUCETI_CLAUDE_MODEL": env_model}))
            stack.enter_context(patch.object(cli, "run", fake_run))
            stack.enter_context(patch.object(cli, "need"))
            stack.enter_context(patch.object(cli.shutil, "which",
                                            side_effect=lambda name: "/bin/claude" if name == "claude" else None))
            stack.enter_context(patch.object(cli.subprocess, "run", return_value=types.SimpleNamespace(
                returncode=0, stdout=b"diff --git a/x.lean b/x.lean\n+x\n", stderr=b"")))
            stack.enter_context(patch.object(cli, "resolve_repo_dir",
                                            return_value=pathlib.Path(cli.__file__).resolve().parent.parent))
            stack.enter_context(patch.object(cli, "pr_ref_oids", return_value=("a" * 40, "b" * 40)))
            stack.enter_context(patch.object(cli, "gh_json", return_value={}))
            stack.enter_context(patch.object(cli, "fetch_thread_replies", return_value={}))
            stack.enter_context(patch.object(cli, "rubrics_repo_sha", return_value=("c" * 40, False)))
            stack.enter_context(patch.object(cli, "merge_base_sha", return_value="b" * 40))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            try:
                cli.main()
            except EngineCalled as call:
                cmd = call.cmd
            else:
                raise AssertionError("CLI did not invoke the review engine")
            actual = cmd[cmd.index("--claude-model") + 1] if "--claude-model" in cmd else None
            assert actual == expected, (env_model, flags, cmd)



def test_unresolved_merge_base_stops_before_the_diff_or_review():
    # A scoreboard without a merge base can never pass the merge gate, so nothing may be spent.
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        argv = ["tauceti-review", "1", "--reviewer", "claude", "--no-archive", "--no-mathlib",
                "--workdir", tmp, "--store", tmp + "/store", "--submitted-by", "test-reviewer"]
        stack.enter_context(patch.object(sys, "argv", argv))
        stack.enter_context(patch.object(cli, "run", fake_run))
        stack.enter_context(patch.object(cli, "need"))
        stack.enter_context(patch.object(cli.shutil, "which",
                                         side_effect=lambda n: "/bin/claude" if n == "claude" else None))
        sub = stack.enter_context(patch.object(cli.subprocess, "run"))
        stack.enter_context(patch.object(cli, "resolve_repo_dir",
                                         return_value=pathlib.Path(cli.__file__).resolve().parent.parent))
        stack.enter_context(patch.object(cli, "pr_ref_oids", return_value=("a" * 40, "b" * 40)))
        stack.enter_context(patch.object(cli, "merge_base_sha", return_value=""))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        try:
            cli.main()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("CLI went on without a merge base")
    sub.assert_not_called()                     # no pr_diff run
    assert not any(len(c) > 1 and str(c[1]).endswith("review.py") for c in calls)

def test_fable_review_preserves_exact_model_and_reported_cost():
    # Exercise the actual engine dispatch and ledger, with only model I/O stubbed.
    import test_billing as billing
    import pricing

    model = "claude-fable-5-1"
    seen = []

    def fake_claude(prompt, cwd, requested_model, env):
        seen.append(requested_model)
        return billing._fake_runner(prompt, cwd, requested_model, env)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(billing.review, "run_claude", fake_claude))
        stack.enter_context(patch.object(billing.review, "reviewer_env", return_value=({}, None)))
        stack.enter_context(patch.object(billing.review, "cleanup_rev_home"))
        stack.enter_context(patch.object(billing.review, "sweep_rev_homes"))
        d, rd, store = billing._workspace(["correctness"])
        stack.callback(cli.shutil.rmtree, d)
        ledger = billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                              extra=["--claude-model", model])
    assert seen == [model], seen
    assert ledger["prs"]["1"]["state"]["correctness"]["model"] == model
    assert ledger["prs"]["1"]["rounds"][0]["cost"] == billing.COST
    assert pricing.CACHE_READ[model] == 0.25


def test_build_status_context_reaches_the_trusted_prompt():
    # The actual shape returned for TauCeti PR #8073, whose sandboxed build and
    # workflow-pinned audits passed. There is no CheckRun.name/conclusion here.
    meta = {
        "headRefOid": "reviewed-head",
        "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "build", "state": "SUCCESS"},
        ],
    }
    status = cli.ci_build_status(meta, "reviewed-head")
    assert status == "success"
    assert "passed `lake build` and the axiom audit" in reviewers.ci_status_block(
        status, "reviewed-head"
    )


def test_check_run_build_is_still_recognized():
    meta = {
        "headRefOid": "reviewed-head",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "build", "conclusion": "SUCCESS"},
        ],
    }
    assert cli.ci_build_status(meta, "reviewed-head") == "success"


def test_node_types_are_discriminated_by_typename():
    # The two vocabularies must never cross-read: a StatusContext carries `state`, a
    # CheckRun carries `conclusion`. Keying on `__typename` keeps that structural.
    meta = {
        "headRefOid": "reviewed-head",
        "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "build", "state": "SUCCESS"},
            {"__typename": "CheckRun", "name": "build", "conclusion": "SUCCESS"},
        ],
    }
    assert cli.ci_build_status(meta, "reviewed-head") == "success"
    # A CheckRun that has not concluded must not be read through the StatusContext branch.
    meta["statusCheckRollup"][1] = {
        "__typename": "CheckRun", "name": "build", "conclusion": None, "state": "SUCCESS",
    }
    assert cli.ci_build_status(meta, "reviewed-head") == ""
    # StatusContext-only states that are not SUCCESS stay untrusted.
    for state in ("ERROR", "EXPECTED", "PENDING"):
        assert cli.ci_build_status({
            "headRefOid": "reviewed-head",
            "statusCheckRollup": [
                {"__typename": "StatusContext", "context": "build", "state": state}],
        }, "reviewed-head") == "", state


def test_build_hint_never_uses_another_head_or_unverified_success():
    success = {"context": "build", "state": "SUCCESS"}
    for meta in (
        {"headRefOid": "newer-head", "statusCheckRollup": [success]},
        {"statusCheckRollup": [success]},
        {"headRefOid": "reviewed-head", "statusCheckRollup": None},
        {"headRefOid": "reviewed-head", "statusCheckRollup": [
            {"context": "scope", "state": "SUCCESS"}]},
        {"headRefOid": "reviewed-head", "statusCheckRollup": [
            {"context": "build", "state": "PENDING"}]},
        {"headRefOid": "reviewed-head", "statusCheckRollup": [
            {"context": "build", "state": "FAILURE"}]},
        {"headRefOid": "reviewed-head", "statusCheckRollup": [
            {"name": "build", "conclusion": "SKIPPED"}]},
        {"headRefOid": "reviewed-head", "statusCheckRollup": [
            success, {"name": "build", "conclusion": None, "status": "IN_PROGRESS"}]},
        {"headRefOid": "reviewed-head", "statusCheckRollup": [
            success, {"name": "build", "conclusion": "FAILURE"}]},
    ):
        status = cli.ci_build_status(meta, "reviewed-head")
        assert status == "", meta
        assert reviewers.ci_status_block(status, "reviewed-head") == "", meta


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} CLI checks passed")
