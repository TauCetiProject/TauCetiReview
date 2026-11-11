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

Every git call retains its transport timeout and process-group cleanup. Diff-only workflow
callers retain their existing byte-limit arguments; CLI source preparation streams without an
application byte ceiling and fetches the two complete endpoint trees once. Lazy fetching is off.

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
import shutil
import subprocess
import sys

from tauceti_tools import git_inputs
from tauceti_tools.git_inputs import (INDEX_ABBREV, MAX_BYTES, MAX_FETCH_BYTES, FETCH_TIMEOUT, DIFF_TIMEOUT,
                                     PREPARATION_WAIT_EXIT as PREPARATION_WAIT_EXIT, PreparationUnavailable, transport_unavailable, _git_env)


def _git(*arguments, **options):
    return git_inputs._git(*arguments, **options)


_changed_blobs = git_inputs._changed_blobs


def git_diff(remote, merge_base, head, diff_out=None, limit=MAX_BYTES,
             fetch_limit=MAX_FETCH_BYTES, source_dir=None, cache_dir=None, environment=None):
    """Diff `merge_base..head` from a throwaway bare clone of `remote`. Only the two commits'
    trees (blobless, depth 1) are fetched, then, when `diff_out` is given, exactly the changed
    files' blobs, all under `fetch_limit` bytes in total. git never fetches lazily after that
    (GIT_NO_LAZY_FETCH), so nothing else can be downloaded. Writes the raw diff to the binary file
    `diff_out` if given and returns the changed paths (str, surrogate-escaped if not UTF-8).
    If source_dir is given, fetch both endpoint trees with their blobs once,
    generate the identical patch, then check out the head there without Git
    administration. None limits stream without an application byte ceiling.
    Raises RuntimeError on failure, or PreparationUnavailable for transport/host
    failures that a fleet caller should retry before inference."""
    if not (merge_base and head):
        raise RuntimeError("need both the merge base and the head SHA")
    source_dir = os.path.abspath(source_dir) if source_dir is not None else None
    env = _git_env() if environment is None else environment
    local = {**env, "GIT_NO_LAZY_FETCH": "1"}
    parent = os.path.dirname(os.fspath(source_dir)) if source_dir is not None else None
    with git_inputs.snapshot(remote, merge_base, head, run_git=_git, environment=env,
                             cache_dir=cache_dir, parent=parent, complete=source_dir is not None,
                             fetch_limit=fetch_limit) as (d, env):
        names = _git(d, ["diff", "--name-only", "-z", "--no-renames", merge_base, head, "--"],
                     local, DIFF_TIMEOUT, limit=limit, disk_cap=fetch_limit)
        if diff_out is not None and source_dir is None:
            git_inputs.fetch_changed_blobs(d, merge_base, head, env, run_git=_git, limit=limit,
                                           fetch_limit=fetch_limit)
        if diff_out is not None:
            _git(d, ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M",
                     f"--abbrev={INDEX_ABBREV}", "--src-prefix=a/", "--dst-prefix=b/",
                     merge_base, head, "--"], local, DIFF_TIMEOUT, sink=diff_out, limit=limit,
                 disk_cap=fetch_limit)
        if source_dir is not None:
            # Diff while HEAD is unborn and there is no worktree: PR attributes
            # must not change the canonical patch. Only then materialize source.
            os.mkdir(source_dir)
            try:
                _git(d, ["--work-tree", os.fspath(source_dir), "checkout", "--force", head, "--", "."],
                     local, DIFF_TIMEOUT, limit=limit)
            except BaseException:
                shutil.rmtree(source_dir, ignore_errors=True)
                raise
    return [p.decode("utf-8", "surrogateescape") for p in names.split(b"\0") if p]


def _gh_api(path, jq):
    try:
        r = subprocess.run(["gh", "api", path, "--jq", jq], capture_output=True, text=True,
                           timeout=FETCH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PreparationUnavailable(f"gh api {path} failed: {e}")
    if r.returncode != 0 or not r.stdout.strip():
        error = PreparationUnavailable if transport_unavailable(r.stderr) else RuntimeError
        raise error(f"gh api {path} failed ({r.returncode}): {r.stderr.strip()}")
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
