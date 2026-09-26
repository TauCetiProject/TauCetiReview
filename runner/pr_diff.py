#!/usr/bin/env python3
"""Produce a PR's unified diff with git, the one way every review and merge path computes it.

The diff is the three-dot compare GitHub shows (merge base of base and head, against head). It is
built here rather than taken from `gh pr diff`, which GitHub refuses for a PR touching more than
300 files (`HTTP 406: Sorry, the diff exceeded the maximum number of files (300)`), as a Lake-pin
bump routinely does. Fetch the merge base and the head by SHA into a throwaway bare repository and
run `git diff <merge-base> <head>`.

The bytes matter beyond the prompt: `casefile.patch_digest` hashes them to decide whether an
approval carries to a new head, and `merge.changed_paths` reads the paths the auto-merge rule
checks, so the workflows, the local CLI and the merge sweep must all produce them the same way.
They all call this. The output also matches `gh pr diff` byte for byte on TauCeti's Lean sources
(new, deleted and renamed files, binary files, mode changes, missing trailing newlines); it can
differ in the section label after a hunk's `@@ ... @@` (GitHub applies language-specific function
patterns to e.g. Python and BibTeX files, git's default here) and in the length of the abbreviated
blob ids on `index` lines (GitHub's depends on the repository's size).

Security: nothing from the PR is executed. The fetch is anonymous HTTPS by SHA into a bare
repository with no work tree, run with global and system git config ignored, so no credential
helper, URL rewrite, external diff driver or textconv applies, and the PR's own `.gitattributes`
is never read (no work tree and an unborn HEAD leave no tree to read it from), so a PR cannot hide
a change from the reviewer by marking it `-diff`.

    pr_diff.py --repo TauCetiProject/TauCeti --pr 123 --head-sha <sha> --merge-base-sha <sha> \
        --out diff.txt

Flat imports only (run as a script with runner/ on sys.path, or imported by sweep.py).
"""
import argparse
import os
import subprocess
import sys
import tempfile

# Blob-id abbreviation on `index` lines, as GitHub renders them for TauCeti today. Cosmetic: the
# patch digest normalises these ids away.
INDEX_ABBREV = 11


def _git_env():
    """Environment for git: no user/system config, no prompts, no pager, no inherited repo."""
    env = dict(os.environ)
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS",
              "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        env.pop(k, None)
    return env


def git_diff(remote, merge_base, head):
    """`git diff merge_base head` from a throwaway bare clone of `remote`. Only the two commits
    (blobless, depth 1) are fetched; git then fetches just the blobs the diff reads. Returns raw
    bytes; raises RuntimeError on failure."""
    if not (merge_base and head):
        raise RuntimeError("need both the merge base and the head SHA")
    env = _git_env()
    with tempfile.TemporaryDirectory(prefix="pr-diff-") as d:
        def git(*args):
            r = subprocess.run(["git", "-C", d, *args], capture_output=True, env=env)
            if r.returncode != 0:
                raise RuntimeError(f"git {args[0]} failed ({r.returncode}): "
                                   + r.stderr.decode("utf-8", "replace").strip())
            return r.stdout
        git("init", "-q", "--bare")
        git("remote", "add", "origin", remote)
        git("fetch", "-q", "--no-tags", "--depth", "1", "--filter=blob:none",
            "origin", merge_base, head)
        return git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M",
                   f"--abbrev={INDEX_ABBREV}", "--src-prefix=a/", "--dst-prefix=b/",
                   merge_base, head, "--")


def _gh_api(path, jq):
    r = subprocess.run(["gh", "api", path, "--jq", jq], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"gh api {path} failed ({r.returncode}): {r.stderr.strip()}")
    return r.stdout.strip()


def pr_diff(repo, pr, head_sha="", merge_base_sha="", remote=None):
    """The PR's three-dot diff as raw bytes. `head_sha` and `merge_base_sha` are resolved from the
    API when not given; pass the head the caller resolved so the diff is bound to that commit.
    Raises RuntimeError on failure."""
    if not head_sha:
        head_sha = _gh_api(f"/repos/{repo}/pulls/{pr}", ".head.sha")
    if not merge_base_sha:
        base = _gh_api(f"/repos/{repo}/pulls/{pr}", ".base.sha")
        merge_base_sha = _gh_api(f"/repos/{repo}/compare/{base}...{head_sha}?per_page=1",
                                 ".merge_base_commit.sha")
    return git_diff(remote or f"https://github.com/{repo}", merge_base_sha, head_sha)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--head-sha", default="", help="PR head (resolved from the API if omitted)")
    ap.add_argument("--merge-base-sha", default="",
                    help="merge base of base and head (resolved from the compare API if omitted)")
    ap.add_argument("--out", required=True, help="file to write the raw diff bytes to")
    a = ap.parse_args()
    try:
        diff = pr_diff(a.repo, a.pr, a.head_sha, a.merge_base_sha)
    except RuntimeError as e:
        print(f"pr_diff: {e}", file=sys.stderr)
        sys.exit(1)
    with open(a.out, "wb") as f:
        f.write(diff)


if __name__ == "__main__":
    main()
