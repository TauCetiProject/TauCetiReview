#!/usr/bin/env python3
"""Produce a PR's unified diff and changed paths with git, the one way every review and merge path
computes them.

The diff is the three-dot compare GitHub shows (merge base of base and head, against head). It is
built here rather than taken from `gh pr diff`, which GitHub refuses for a PR touching more than
300 files (`HTTP 406: Sorry, the diff exceeded the maximum number of files (300)`), as a Lake-pin
bump routinely does. Fetch the merge base and the head by SHA into a throwaway bare repository and
run `git diff <merge-base> <head>`.

The bytes matter beyond the prompt: `casefile.patch_digest` hashes them to decide whether an
approval carries to a new head, so the workflows and the local CLI must produce them the same way.
They all call this. The output also matches `gh pr diff` byte for byte on TauCeti's Lean sources
(new, deleted and renamed files, binary files, mode changes, missing trailing newlines); it can
differ in the section label after a hunk's `@@ ... @@` (GitHub applies language-specific function
patterns to e.g. Python and BibTeX files, git's default here) and in the length of the abbreviated
blob ids on `index` lines (GitHub's depends on the repository's size).

The changed paths are machine-read (`git diff --name-only -z`), never parsed back out of the human
patch headers, where git quotes names with control or non-ASCII bytes. They feed the auto-merge path
rule, so the merge paths (merge-only, the sweep, review.py's merge decision) read them from here.
Renames are not detected for the list: a rename then lists as a deletion plus an addition, which is
exactly the old and the new path the rename would contribute, and git needs only the trees for it.

Bounded: every git call has a timeout, and the diff and the path list are streamed with a hard
byte limit, so a hostile or pathological PR fails clearly instead of hanging a job or filling a
disk.

Security: nothing from the PR is executed. The fetch is anonymous HTTPS by SHA into a bare
repository with no work tree. git runs with an allowlisted environment (no tokens, provider keys,
askpass or ssh helpers) and with global and system config ignored, so no credential helper, URL
rewrite, external diff driver or textconv applies. The PR's own `.gitattributes` is never read
(no work tree and an unborn HEAD leave no tree to read it from), so a PR cannot hide a change from
the reviewer by marking it `-diff`.

    pr_diff.py --repo TauCetiProject/TauCeti --pr 123 --head-sha <sha> --merge-base-sha <sha> \
        --out diff.txt --paths-out paths.z

Flat imports only (run as a script with runner/ on sys.path, or imported by sweep.py).
"""
import argparse
import os
import signal
import subprocess
import sys
import tempfile
import threading

# Blob-id abbreviation on `index` lines, as GitHub renders them for TauCeti today. Cosmetic: the
# patch digest normalises these ids away.
INDEX_ABBREV = 11
# Hard cap on the diff (and on the NUL-separated path list). Real large PRs are far below it: the
# 909-file mathlib4 bump #3780 is about 6 MB, the 338-file mathlib4 #26077 0.36 MB, TauCeti's
# biggest diffs well under 1 MB.
MAX_BYTES = 64 << 20
FETCH_TIMEOUT = 300   # seconds, per git call
DIFF_TIMEOUT = 300
# The only variables git sees. Everything else (GH_TOKEN, GITHUB_TOKEN, provider API keys,
# GIT_ASKPASS, SSH_ASKPASS, GIT_SSH*, GIT_CONFIG_*, GIT_DIR, ...) is dropped.
ENV_ALLOW = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "LANG",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "no_proxy", "all_proxy",
             "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_EXEC_PATH")


def _git_env():
    """Allowlisted environment for git: no credentials, no user/system config, no prompts."""
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
    return env


def _killpg(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def _git(d, args, env, timeout, sink=None, limit=MAX_BYTES):
    """Run `git -C d args`, streaming stdout into `sink` (a binary file) up to `limit` bytes and
    killing git's whole process group at `timeout`. Returns stdout bytes when `sink` is None.
    Raises RuntimeError on failure, timeout, or overflow."""
    name = args[0]
    with tempfile.TemporaryFile() as err:
        try:
            p = subprocess.Popen(["git", "-C", d, *args], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=err, env=env,
                                 start_new_session=True)
        except OSError as e:
            raise RuntimeError(f"cannot run git: {e}")
        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            _killpg(p)

        timer = threading.Timer(timeout, on_timeout)
        timer.start()
        out, total = [], 0
        try:
            while True:
                chunk = p.stdout.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise RuntimeError(f"git {name} output exceeds the {limit}-byte limit")
                if sink is None:
                    out.append(chunk)
                else:
                    sink.write(chunk)
            p.wait()
        except BaseException:
            _killpg(p)
            p.wait()
            raise
        finally:
            timer.cancel()
            p.stdout.close()
        if timed_out.is_set():
            raise RuntimeError(f"git {name} timed out after {timeout}s")
        if p.returncode != 0:
            err.seek(0)
            msg = err.read()[-2000:].decode("utf-8", "replace").strip()
            raise RuntimeError(f"git {name} failed ({p.returncode}): {msg}")
        return b"".join(out)


def git_diff(remote, merge_base, head, diff_out=None, limit=MAX_BYTES):
    """Diff `merge_base..head` from a throwaway bare clone of `remote`. Only the two commits
    (blobless, depth 1) are fetched; git then fetches just the blobs the diff reads, and none at all
    when `diff_out` is None. Writes the raw diff to the binary file `diff_out` if given and returns
    the changed paths (str, surrogate-escaped if not UTF-8). Raises RuntimeError on failure."""
    if not (merge_base and head):
        raise RuntimeError("need both the merge base and the head SHA")
    env = _git_env()
    with tempfile.TemporaryDirectory(prefix="pr-diff-") as d:
        _git(d, ["init", "-q", "--bare"], env, FETCH_TIMEOUT)
        _git(d, ["remote", "add", "origin", remote], env, FETCH_TIMEOUT)
        _git(d, ["fetch", "-q", "--no-tags", "--depth", "1", "--filter=blob:none",
                 "origin", merge_base, head], env, FETCH_TIMEOUT)
        names = _git(d, ["diff", "--name-only", "-z", "--no-renames", merge_base, head, "--"],
                     env, DIFF_TIMEOUT, limit=limit)
        if diff_out is not None:
            _git(d, ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M",
                     f"--abbrev={INDEX_ABBREV}", "--src-prefix=a/", "--dst-prefix=b/",
                     merge_base, head, "--"], env, DIFF_TIMEOUT, sink=diff_out, limit=limit)
    return [p.decode("utf-8", "surrogateescape") for p in names.split(b"\0") if p]


def _gh_api(path, jq):
    try:
        r = subprocess.run(["gh", "api", path, "--jq", jq], capture_output=True, text=True,
                           timeout=FETCH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"gh api {path} failed: {e}")
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"gh api {path} failed ({r.returncode}): {r.stderr.strip()}")
    return r.stdout.strip()


def resolve_shas(repo, pr, head_sha="", merge_base_sha=""):
    """(head, merge base) of the PR, filling in whichever is missing from the API."""
    if not head_sha:
        head_sha = _gh_api(f"/repos/{repo}/pulls/{pr}", ".head.sha")
    if not merge_base_sha:
        base = _gh_api(f"/repos/{repo}/pulls/{pr}", ".base.sha")
        merge_base_sha = _gh_api(f"/repos/{repo}/compare/{base}...{head_sha}?per_page=1",
                                 ".merge_base_commit.sha")
    return head_sha, merge_base_sha


def pr_diff(repo, pr, head_sha="", merge_base_sha="", diff_out=None, remote=None):
    """The PR's three-dot diff (written to `diff_out` if given) and its changed paths (returned).
    Pass the head the caller resolved so the result is bound to that commit. Raises RuntimeError."""
    head_sha, merge_base_sha = resolve_shas(repo, pr, head_sha, merge_base_sha)
    return git_diff(remote or f"https://github.com/{repo}", merge_base_sha, head_sha, diff_out)


def write_paths(path, paths):
    """NUL-terminated, raw bytes: the exact names, whatever bytes they contain."""
    with open(path, "wb") as f:
        for p in paths:
            f.write(p.encode("utf-8", "surrogateescape") + b"\0")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--head-sha", default="", help="PR head (resolved from the API if omitted)")
    ap.add_argument("--merge-base-sha", default="",
                    help="merge base of base and head (resolved from the compare API if omitted)")
    ap.add_argument("--out", default="", help="file to write the raw diff bytes to")
    ap.add_argument("--paths-out", default="",
                    help="file to write the changed paths to, each NUL-terminated")
    a = ap.parse_args()
    if not (a.out or a.paths_out):
        ap.error("nothing to do: give --out and/or --paths-out")
    tmp = a.out + ".partial" if a.out else ""
    try:
        if tmp:
            with open(tmp, "wb") as f:
                paths = pr_diff(a.repo, a.pr, a.head_sha, a.merge_base_sha, diff_out=f)
            os.replace(tmp, a.out)
        else:
            paths = pr_diff(a.repo, a.pr, a.head_sha, a.merge_base_sha)
        if a.paths_out:
            write_paths(a.paths_out, paths)
    except RuntimeError as e:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
        print(f"pr_diff: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
