#!/usr/bin/env python3
"""Tests for runner/pr_diff.py, the one way every review and merge path builds a PR's diff.

`gh pr diff` refuses PRs touching more than 300 files, so the diff is built with git from the merge
base and the head. These run the real git against a local repository served over file:// (no
network): a >300-file PR with a rename, a deletion, a mode change, a binary change and a file
without a trailing newline, on a base branch that has moved on since the PR forked.

Run: python3 tests/test_pr_diff.py   (exit 0 = pass)
"""
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import casefile  # noqa: E402
import merge  # noqa: E402
import pr_diff  # noqa: E402

N_FILES = 320


def _git(repo, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          env=env).stdout.decode().strip()


def _commit(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _fixture(root):
    """A served repository: main forks at `fork`, the PR branch changes N_FILES+ files, and main
    then moves on with a change the PR's three-dot diff must NOT show. Returns (url, merge_base,
    head, base_tip)."""
    src = root / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "uploadpack.allowFilter", "true")
    _git(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    lib = src / "TauCeti"
    lib.mkdir()
    for i in range(N_FILES):
        (lib / f"F{i:03}.lean").write_text(f"theorem t{i} : True := trivial\n")
    (lib / "Moved.lean").write_text("".join(f"-- line {k}\n" for k in range(20)))
    (lib / "Gone.lean").write_text("theorem gone : True := trivial\n")
    (src / "script.sh").write_text("#!/bin/sh\necho hi\n")
    (src / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)))
    (src / "NoEol.lean").write_text("theorem noeol : True := trivial")
    fork = _commit(src, "base")

    _git(src, "checkout", "-q", "-b", "pr")
    for i in range(N_FILES):
        (lib / f"F{i:03}.lean").write_text(f"theorem t{i} : True := by trivial\n")
    (lib / "Moved.lean").rename(lib / "Renamed.lean")
    (lib / "Gone.lean").unlink()
    (src / "script.sh").chmod(0o755)
    (src / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(255, -1, -1)))
    (src / "NoEol.lean").write_text("theorem noeol : True := by trivial")
    # A PR cannot hide a change from the reviewer by marking it binary: its own attributes are not
    # read.
    (src / ".gitattributes").write_text("*.lean -diff\n")
    head = _commit(src, "pr")

    _git(src, "checkout", "-q", "main")
    (src / "MainOnly.lean").write_text("theorem mainOnly : True := trivial\n")
    base_tip = _commit(src, "main moves on")
    return src.resolve().as_uri(), fork, head, base_tip


def test_large_pr_diff_is_three_dot_and_complete():
    with tempfile.TemporaryDirectory() as d:
        url, mb, head, base_tip = _fixture(pathlib.Path(d))
        diff = pr_diff.git_diff(url, mb, head)
        text = diff.decode()
        paths = merge.changed_paths(text)
        assert text.count("\ndiff --git ") + text.startswith("diff --git ") == N_FILES + 6, text[:400]
        assert len(paths) > 300
        assert {"TauCeti/F000.lean", f"TauCeti/F{N_FILES - 1:03}.lean", "TauCeti/Moved.lean",
                "TauCeti/Renamed.lean", "TauCeti/Gone.lean", "script.sh", "logo.png",
                "NoEol.lean", ".gitattributes"} <= paths
        assert "MainOnly.lean" not in paths          # merge-base semantics, not base tip vs head
        assert "rename from TauCeti/Moved.lean\nrename to TauCeti/Renamed.lean\n" in text
        assert "deleted file mode 100644" in text
        assert "old mode 100644\nnew mode 100755\n" in text
        assert "Binary files a/logo.png and b/logo.png differ\n" in text
        assert "+theorem noeol : True := by trivial\n\\ No newline at end of file\n" in text
        assert "+theorem t0 : True := by trivial\n" in text   # .gitattributes `-diff` ignored
        # Same bytes as a plain git diff in the source repo, with GitHub's a/ b/ prefixes and
        # abbreviation: nothing in the fetch changes the output.
        expect = _git(pathlib.Path(d) / "src", "diff", "--no-color", "-M",
                      f"--abbrev={pr_diff.INDEX_ABBREV}", mb, head)
        assert text.rstrip("\n") == expect.rstrip("\n")
        assert casefile.patch_digest(diff) is None   # a binary change is never carried forward


def test_output_ignores_user_git_config():
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        url, mb, head, _ = _fixture(root)
        clean = pr_diff.git_diff(url, mb, head)
        cfg = root / "gitconfig"
        cfg.write_text("[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n\trenames = false\n"
                       "\texternal = false\n[color]\n\tui = always\n[core]\n\tabbrev = 20\n")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg),
                                     "GIT_EXTERNAL_DIFF": "false"}):
            assert pr_diff.git_diff(url, mb, head) == clean


def test_is_deterministic_across_runs():
    with tempfile.TemporaryDirectory() as d:
        url, mb, head, _ = _fixture(pathlib.Path(d))
        assert pr_diff.git_diff(url, mb, head) == pr_diff.git_diff(url, mb, head)


def test_pr_diff_resolves_missing_shas_from_the_api():
    calls = []

    def fake_api(path, jq):
        calls.append((path, jq))
        return {".head.sha": "h" * 40, ".base.sha": "b" * 40,
                ".merge_base_commit.sha": "m" * 40}[jq]

    with patch.object(pr_diff, "_gh_api", fake_api), \
            patch.object(pr_diff, "git_diff", return_value=b"d") as gd:
        assert pr_diff.pr_diff("o/r", 7) == b"d"
        gd.assert_called_once_with("https://github.com/o/r", "m" * 40, "h" * 40)
        assert calls[-1][0] == f"/repos/o/r/compare/{'b' * 40}...{'h' * 40}?per_page=1"
        calls.clear()
        gd.reset_mock()
        pr_diff.pr_diff("o/r", 7, "1" * 40, "2" * 40)
        assert calls == []                                   # given SHAs are used as is
        gd.assert_called_once_with("https://github.com/o/r", "2" * 40, "1" * 40)


def test_failures_raise():
    with tempfile.TemporaryDirectory() as d:
        url, mb, head, _ = _fixture(pathlib.Path(d))
        for args in ((url, mb, "0" * 40), (url, "", head), (url + "-missing", mb, head)):
            try:
                pr_diff.git_diff(*args)
            except RuntimeError:
                continue
            raise AssertionError(f"no error for {args}")


def test_cli_writes_raw_bytes():
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        out = root / "diff.txt"
        with patch.object(pr_diff, "pr_diff", return_value=b"a\r\nb\xff\n"), \
                patch.object(sys, "argv", ["pr_diff.py", "--repo", "o/r", "--pr", "1",
                                           "--out", str(out)]):
            pr_diff.main()
        assert out.read_bytes() == b"a\r\nb\xff\n"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} pr_diff checks passed")
