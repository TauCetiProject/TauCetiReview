#!/usr/bin/env python3
"""A head that skipped PR CI is not reviewed when it fails the PR-level diff guards."""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import cli  # noqa: E402
from tauceti_tools.assets.scripts import ci_git  # noqa: E402


def git(directory, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def repository(tmp, files):
    """A repository with a base commit and a head adding `files` ({path: line count})."""
    directory = pathlib.Path(tmp) / "repo"
    directory.mkdir()
    git(directory, "init", "-q")
    (directory / "TauCeti").mkdir()
    (directory / "TauCeti/Basic.lean").write_text("theorem t : True := trivial\n")
    git(directory, "add", "-A")
    git(directory, "commit", "-qm", "base")
    base = git(directory, "rev-parse", "HEAD")
    for path, lines in files.items():
        (directory / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / path).write_text("".join(f"-- line {i}\n" for i in range(lines)))
    git(directory, "add", "-A")
    git(directory, "commit", "-qm", "head")
    return directory, base, git(directory, "rev-parse", "HEAD")


def test_violations_are_the_shared_measure_admission_applies():
    with tempfile.TemporaryDirectory() as tmp:
        files = {f"TauCeti/Part{i}.lean": 700 for i in range(3)}
        directory, base, head = repository(tmp, files)
        found = cli.diff_guard_violations(directory, base, head)
        with ci_git.repository_reader(directory) as reader:
            expected = tuple(ci_git.pr_guards(reader, base, head, scope=False)["violations"])
        assert found == expected and len(found) == 1, found
        assert found[0].startswith("too large to review: +2100 / -0 lines across 3 files"), found


def test_new_file_cap_and_a_passing_diff():
    with tempfile.TemporaryDirectory() as tmp:
        directory, base, head = repository(tmp, {"TauCeti/Long.lean": 1001})
        (found,) = cli.diff_guard_violations(directory, base, head)
        assert found.startswith("TauCeti/Long.lean: new Lean files must be at most 1000 lines (found 1001)")
    with tempfile.TemporaryDirectory() as tmp:
        directory, base, head = repository(tmp, {"TauCeti/Short.lean": 1000})
        assert cli.diff_guard_violations(directory, base, head) == ()


def test_unreadable_statistics_fail_closed_and_git_failures_retry():
    with tempfile.TemporaryDirectory() as tmp:
        directory, base, head = repository(tmp, {"TauCeti/Short.lean": 10})
        with patch.object(ci_git, "pr_guards", side_effect=ValueError("unreadable Git line statistics")):
            assert cli.diff_guard_violations(directory, base, head) == (cli.DIFF_GUARD_UNMEASURABLE,)
        try:
            cli.diff_guard_violations(directory, base, "f" * 40)
        except cli.repository_diff.PreparationUnavailable:
            pass
        else:
            raise AssertionError("a missing object must be a preparation retry, not a verdict")


class BuildMetadata:
    """The real review source, with this PR's execution metadata stubbed."""

    def __init__(self, source):
        self.source = source

    def __getattr__(self, name):
        return getattr(self.source, name)

    def execution_metadata(self, number):
        return {}


def test_a_tools_release_without_the_shared_measure_reviews_as_before():
    older = types.ModuleType("ci_git")  # ci_git predating pr_guards and repository_reader
    from tauceti_tools.assets import scripts
    with patch.object(scripts, "ci_git", older), patch.dict(sys.modules, {
            "tauceti_tools.assets.scripts.ci_git": older}):
        assert cli.diff_guard_violations("/unused", "b" * 40, "a" * 40) == ()


def run_cli(tmp, guards, build):
    """Run the CLI to the inference boundary; returns (exit code, engine invoked, result file)."""
    head = "a" * 40
    engine = []
    result = pathlib.Path(tmp) / "result.json"

    def fake_run(cmd, **kwargs):
        if len(cmd) > 1 and pathlib.Path(cmd[1]).name == "review.py":
            engine.append(cmd)
            raise SystemExit(99)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with contextlib.ExitStack() as stack:
        argv = ["tauceti-review", "1", "--reviewer", "claude", "--no-archive", "--no-mathlib",
                "--workdir", tmp + "/work", "--store", tmp + "/store", "--submitted-by", "test-reviewer"]
        stack.enter_context(patch.object(sys, "argv", argv))
        stack.enter_context(patch.dict(os.environ, {"TAUCETI_REVIEW_RESULT_FILE": str(result)}))
        stack.enter_context(patch.object(cli, "run", fake_run))
        stack.enter_context(patch.object(cli, "need"))
        stack.enter_context(patch.object(cli.shutil, "which",
                                         side_effect=lambda n: "/bin/claude" if n == "claude" else None))
        stack.enter_context(patch.object(cli.subprocess, "run", return_value=types.SimpleNamespace(
            returncode=0, stdout=b"", stderr=b"")))
        stack.enter_context(patch.object(cli, "resolve_repo_dir",
                                         return_value=pathlib.Path(cli.__file__).resolve().parent.parent))
        stack.enter_context(patch.object(cli, "pr_ref_oids", return_value=(head, "b" * 40)))
        stack.enter_context(patch.object(cli, "gh_json", return_value={}))
        stack.enter_context(patch.object(cli, "fetch_thread_replies", return_value={}))
        stack.enter_context(patch.object(cli, "issue_comments", return_value=[]))
        stack.enter_context(patch.object(cli, "rubrics_repo_sha", return_value=("c" * 40, False)))
        stack.enter_context(patch.object(cli, "merge_base_sha", return_value="b" * 40))
        real = cli.review_source
        stack.enter_context(patch.object(cli, "review_source", side_effect=lambda repo: BuildMetadata(real(repo))))
        stack.enter_context(patch.object(cli, "ci_build_status", return_value=build))
        stack.enter_context(patch.object(cli, "prepare_review_source", return_value=guards))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        try:
            code = cli.main()
        except SystemExit as stop:
            code = stop.code
    return code, bool(engine), json.loads(result.read_text()) if result.exists() else None




def test_a_head_without_a_green_build_that_fails_the_guards_is_not_reviewed():
    sentence = "too large to review: +2992 / -0 lines across 5 files"
    with tempfile.TemporaryDirectory() as tmp:
        code, engine, result = run_cli(tmp, (sentence,), "")
    assert (code, engine) == (0, False)
    assert result == {"head_sha": "a" * 40, "changed": False, "outcome": "diff-guard",
                      "violations": [sentence]}, result


def test_guards_never_stop_a_passing_diff_or_a_green_build():
    for guards, build in (((), ""), (("too large to review",), "success")):
        with tempfile.TemporaryDirectory() as tmp:
            code, engine, result = run_cli(tmp, guards, build)
        assert engine and result is None, (guards, build, code)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} diff-guard review checks passed")
