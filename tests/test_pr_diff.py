#!/usr/bin/env python3
"""Tests for runner/pr_diff.py, the one way every review and merge path builds a PR's diff.

`gh pr diff` refuses PRs touching more than 300 files, so the diff is built with git from the merge
base and the head. These run the real git against a local repository served over file:// (no
network): a >300-file PR with a rename, a deletion, a mode change, a binary change and a file
without a trailing newline, on a base branch that has moved on since the PR forked.

Run: python3 tests/test_pr_diff.py   (exit 0 = pass)
"""
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import casefile  # noqa: E402
import merge  # noqa: E402
import pr_diff  # noqa: E402

N_FILES = 320
ODD_NAMES = ["TauCeti/\u00c9tale.lean", "outside\nnewline.txt"]


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
    for name in ODD_NAMES:   # names git quotes in patch headers
        (src / name).write_text("x\n")
    head = _commit(src, "pr")

    _git(src, "checkout", "-q", "main")
    (src / "MainOnly.lean").write_text("theorem mainOnly : True := trivial\n")
    base_tip = _commit(src, "main moves on")
    return src.resolve().as_uri(), fork, head, base_tip


def _diff(url, mb, head, **kw):
    out = io.BytesIO()
    paths = pr_diff.git_diff(url, mb, head, out, **kw)
    return out.getvalue(), paths


def _two_commits(root, base_files, head_files):
    """A served repository with one commit of `base_files` and a child with `head_files`
    (name -> bytes) added. Returns (url, base, head)."""
    src = root / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "uploadpack.allowFilter", "true")
    _git(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    shas = []
    for files in (base_files, head_files):
        for name, data in files.items():
            (src / name).write_bytes(data)
        shas.append(_commit(src, "c"))
    return (src.resolve().as_uri(), *shas)


def test_large_pr_diff_is_three_dot_and_complete():
    with tempfile.TemporaryDirectory() as d:
        url, mb, head, base_tip = _fixture(pathlib.Path(d))
        diff, paths = _diff(url, mb, head)
        text = diff.decode()
        assert text.count("\ndiff --git ") + text.startswith("diff --git ") == N_FILES + 8, text[:400]
        assert len(paths) == len(set(paths)) == N_FILES + 9          # a rename lists both sides
        assert {"TauCeti/F000.lean", f"TauCeti/F{N_FILES - 1:03}.lean", "TauCeti/Moved.lean",
                "TauCeti/Renamed.lean", "TauCeti/Gone.lean", "script.sh", "logo.png",
                "NoEol.lean", ".gitattributes", *ODD_NAMES} == set(paths) - {
                    f"TauCeti/F{i:03}.lean" for i in range(1, N_FILES - 1)}
        # The machine-read list agrees with the patch headers wherever git does not quote a name,
        # and has the quoted ones the header parser cannot see.
        assert set(paths) - set(ODD_NAMES) == merge.changed_paths(text)
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
        # Paths alone need no diff and no file contents.
        assert pr_diff.git_diff(url, mb, head) == paths


def test_output_ignores_user_git_config():
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        url, mb, head, _ = _fixture(root)
        clean = _diff(url, mb, head)
        cfg = root / "gitconfig"
        cfg.write_text("[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n\trenames = false\n"
                       "\texternal = false\n[color]\n\tui = always\n[core]\n\tabbrev = 20\n"
                       "\tquotePath = false\n")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg),
                                     "GIT_EXTERNAL_DIFF": "false",
                                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "diff.noprefix",
                                     "GIT_CONFIG_VALUE_0": "true"}):
            assert _diff(url, mb, head) == clean


def test_git_sees_no_credentials_or_helpers():
    secrets = {"GH_TOKEN": "s1", "GITHUB_TOKEN": "s2", "ANTHROPIC_API_KEY": "s3",
               "OPENAI_API_KEY": "s4", "OPENROUTER_API_KEY": "s5", "KIRO_API_KEY": "s6",
               "GIT_ASKPASS": "/bin/s7", "SSH_ASKPASS": "/bin/s8", "GIT_SSH": "s9",
               "GIT_SSH_COMMAND": "s10", "GIT_DIR": "/s11"}
    with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, secrets):
        env = pr_diff._git_env()
        assert not set(secrets) & set(env), env
        # And what git itself (and anything it spawns) actually receives.
        out = pr_diff._git(d, ["-c", "alias.showenv=!env", "showenv"], env, 30).decode()
    for k, v in secrets.items():
        assert f"{k}=" not in out and v not in out.split("=")[1:], k


def test_timeout_kills_git_and_its_children():
    with tempfile.TemporaryDirectory() as d:
        t0 = time.monotonic()
        try:
            pr_diff._git(d, ["-c", "alias.slow=!sleep 30", "slow"], pr_diff._git_env(), 0.5)
        except RuntimeError as e:
            assert "timed out" in str(e), e
        else:
            raise AssertionError("no timeout")
        assert time.monotonic() - t0 < 10



def test_disk_cap_kills_git_while_it_is_still_running():
    # git (here a stand-in that writes 3 MB into the repository, then idles) is killed as soon as
    # the directory passes the cap, well before its timeout, and the error says why.
    with tempfile.TemporaryDirectory() as d:
        grow = f"!head -c 3000000 /dev/zero > '{d}/grown' && sleep 30"
        t0 = time.monotonic()
        try:
            pr_diff._git(d, ["-c", f"alias.grow={grow}", "grow"], pr_diff._git_env(), 20,
                         disk_cap=1 << 20)
        except RuntimeError as e:
            assert "exceed the 1048576-byte limit" in str(e), e
        else:
            raise AssertionError("no size error")
        assert time.monotonic() - t0 < 10


def test_disk_cap_is_checked_after_a_fast_exit():
    with tempfile.TemporaryDirectory() as d:
        grow = f"!head -c 3000000 /dev/zero > '{d}/grown'"
        try:
            pr_diff._git(d, ["-c", f"alias.grow={grow}", "grow"], pr_diff._git_env(), 20,
                         disk_cap=1 << 20)
        except RuntimeError as e:
            assert "exceed" in str(e), e
        else:
            raise AssertionError("no size error")

def test_oversized_text_diff_fails_clearly():
    big = "".join(f"line {i}\n" for i in range(200_000)).encode()    # ~2.3 MB
    with tempfile.TemporaryDirectory() as d:
        url, base, head = _two_commits(pathlib.Path(d), {"a.lean": b"x\n"}, {"big.lean": big})
        try:
            _diff(url, base, head, limit=1 << 20)
        except RuntimeError as e:
            assert "exceeds the 1048576-byte limit" in str(e), e
        else:
            raise AssertionError("no size error")
        diff, paths = _diff(url, base, head)          # well within the real limit
        assert len(diff) > len(big) and paths == ["big.lean"]


def test_binary_file_is_summarised_not_streamed():
    blob = os.urandom(3 << 20)                                    # 3 MB, NUL bytes throughout
    with tempfile.TemporaryDirectory() as d:
        url, base, head = _two_commits(pathlib.Path(d), {"a.lean": b"x\n"}, {"blob.bin": blob})
        diff, paths = _diff(url, base, head, limit=1 << 20)
        assert b"Binary files /dev/null and b/blob.bin differ\n" in diff and len(diff) < 1000
        assert paths == ["blob.bin"]



def test_download_cap_refuses_an_oversized_blob():
    blob = os.urandom(3 << 20)
    with tempfile.TemporaryDirectory() as d:
        url, base, head = _two_commits(pathlib.Path(d), {"a.lean": b"x\n"}, {"blob.bin": blob})
        try:
            _diff(url, base, head, fetch_limit=1 << 20)
        except RuntimeError as e:
            assert "objects downloaded exceed the 1048576-byte limit" in str(e), e
        else:
            raise AssertionError("no download-size error")
        # The paths alone need no blob contents, so they stay within the same cap.
        assert pr_diff.git_diff(url, base, head, fetch_limit=1 << 20) == ["blob.bin"]


def test_diff_never_fetches_lazily():
    # Only the prefetched changed blobs are ever downloaded: skip the prefetch and the diff must
    # fail on the missing contents rather than fetch them itself.
    with tempfile.TemporaryDirectory() as d:
        url, base, head = _two_commits(pathlib.Path(d), {"a.lean": b"x\n"},
                                       {"a.lean": b"y\n"})
        assert b"+y\n" in _diff(url, base, head)[0]
        with patch.object(pr_diff, "_changed_blobs", return_value=[]):
            try:
                _diff(url, base, head)
            except RuntimeError as e:
                assert "git diff failed" in str(e), e
            else:
                raise AssertionError("the diff fetched the missing blobs itself")

def test_oversized_path_list_fails_clearly():
    with tempfile.TemporaryDirectory() as d:
        url, mb, head, _ = _fixture(pathlib.Path(d))
        try:
            pr_diff.git_diff(url, mb, head, limit=1000)
        except RuntimeError as e:
            assert "limit" in str(e), e
        else:
            raise AssertionError("no size error")


def test_is_deterministic_across_runs():
    with tempfile.TemporaryDirectory() as d:
        url, mb, head, _ = _fixture(pathlib.Path(d))
        assert _diff(url, mb, head) == _diff(url, mb, head)


def test_pr_diff_resolves_missing_shas_from_the_api():
    calls = []

    def fake_api(path, jq):
        calls.append((path, jq))
        return {".head.sha": "h" * 40, ".base.sha": "b" * 40,
                ".merge_base_commit.sha": "m" * 40}[jq]

    with patch.object(pr_diff, "_gh_api", fake_api), \
            patch.object(pr_diff, "git_diff", return_value=["p"]) as gd:
        assert pr_diff.pr_diff("o/r", 7) == ["p"]
        gd.assert_called_once_with("https://github.com/o/r", "m" * 40, "h" * 40, None)
        assert calls[-1][0] == f"/repos/o/r/compare/{'b' * 40}...{'h' * 40}?per_page=1"
        calls.clear()
        gd.reset_mock()
        pr_diff.pr_diff("o/r", 7, "1" * 40, "2" * 40)
        assert calls == []                                   # given SHAs are used as is
        gd.assert_called_once_with("https://github.com/o/r", "2" * 40, "1" * 40, None)


def test_failures_raise():
    with tempfile.TemporaryDirectory() as d:
        url, mb, head, _ = _fixture(pathlib.Path(d))
        for args in ((url, mb, "0" * 40), (url, "", head), (url + "-missing", mb, head)):
            try:
                pr_diff.git_diff(*args)
            except RuntimeError:
                continue
            raise AssertionError(f"no error for {args}")


def test_cli_writes_raw_bytes_and_paths_and_no_partial_file():
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        url, mb, head, _ = _fixture(root)
        out, paths_out = root / "diff.txt", root / "paths.z"
        argv = ["pr_diff.py", "--repo", "o/r", "--pr", "1", "--head-sha", head,
                "--merge-base-sha", mb, "--out", str(out), "--paths-out", str(paths_out)]
        real = pr_diff.git_diff
        with patch.object(sys, "argv", argv), \
                patch.object(pr_diff, "git_diff", lambda _r, *a, **k: real(url, *a, **k)):
            pr_diff.main()
        assert out.read_bytes() == _diff(url, mb, head)[0]
        assert merge.read_paths(paths_out) == set(pr_diff.git_diff(url, mb, head))
        assert set(ODD_NAMES) <= merge.read_paths(paths_out)
        out.unlink()
        with patch.object(sys, "argv", argv), \
                patch.object(pr_diff, "git_diff",
                             lambda _r, *a, **k: real(url, *a, limit=5000, **k)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                pr_diff.main()
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("no failure exit")
        assert "limit" in err.getvalue()
        assert not out.exists() and not (root / "diff.txt.partial").exists()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} pr_diff checks passed")
