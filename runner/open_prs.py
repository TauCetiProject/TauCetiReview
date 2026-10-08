#!/usr/bin/env python3
"""List the other open pull requests a reuse review should know about: those that change a file
this PR changes, or a Lean file in a directory where this PR changes one.

Every rubric reviews a read-only checkout of this PR's head, so it cannot see a sibling PR that is
open at the same time. Two PRs reviewed against the same `main` can then each add the same
declaration and both pass `reuse`, and the merge gate deliberately keeps an approval valid while
`main` merely advances (merge_from_scoreboard.decide_from_comments), so nothing re-checks the
second after the first merges. This gathers, for the reuse prompt, the open PRs whose changes sit
next to this one's and the declarations each adds. Whichever of two overlapping PRs is reviewed
second then sees the other: in this list while it is open, on `main` once it has merged.

Which PRs are open, when they were opened, whether they are drafts and which paths they change are
GitHub's facts. Their titles and declaration names are other authors' text, and reach the prompt
only as fenced data (reviewers.open_prs_block).

    open_prs.py --repo TauCetiProject/TauCeti --pr 123 --paths-file paths.z --out open_prs.json

Bounded: one listing call for the open PRs (GitHub returns at most the first 100 changed paths of
each), then one file-listing call for each of at most MAX_PRS overlapping PRs. Best-effort: on any
failure it writes an empty list and exits 0, and the prompt then says nothing.

Flat imports only (run as a script with runner/ on sys.path).
"""
import argparse
import json
import posixpath
import re
import subprocess
import sys

# Overlapping PRs reported, and declaration names per PR. The block is reuse-prompt text, so it is
# kept to what a reviewer can act on.
MAX_PRS = 12
MAX_DECLS = 30
MAX_TITLE = 160
MAX_NAME = 120
LIST_LIMIT = 1000

# A declaration added by a patch line: optional attributes and modifiers, a declaration keyword,
# then a name. Anonymous instances (`instance : C α`) have no name and are skipped.
DECL = re.compile(
    r"^\+\s*(?:@\[.*?\]\s*)*"
    r"(?:(?:private|protected|noncomputable|partial|unsafe|nonrec|scoped|local)\s+)*"
    r"(?:theorem|lemma|def|abbrev|instance|structure|class(?:\s+inductive)?|inductive)\s+"
    r"(?!:)([^\s:({\[]+)")


def added_declarations(patch, limit=MAX_DECLS):
    """The names of the declarations a unified-diff patch adds, in order, without repeats."""
    names = []
    for line in (patch or "").splitlines():
        m = DECL.match(line)
        if m and m.group(1) not in names:
            names.append(m.group(1)[:MAX_NAME])
            if len(names) >= limit:
                break
    return names


def lean_dirs(paths):
    """The directories holding the `.lean` files among `paths`."""
    return {posixpath.dirname(p) for p in paths if p.endswith(".lean") and posixpath.dirname(p)}


def overlapping(own_paths, prs, own_pr, limit=MAX_PRS):
    """The open PRs (as `gh pr list --json number,title,createdAt,isDraft,files` returns them) that
    change a path this PR changes, or a Lean file in a directory where it changes one; those
    sharing a file first, then the earlier-opened first."""
    own = set(own_paths)
    own_dirs = lean_dirs(own)
    out = []
    for pr in prs:
        if pr.get("number") == own_pr:
            continue
        paths = {f.get("path") for f in pr.get("files") or [] if f.get("path")}
        shared_files = sorted(own & paths)
        shared_dirs = sorted(own_dirs & lean_dirs(paths))
        if not shared_files and not shared_dirs:
            continue
        out.append({"number": pr["number"], "title": (pr.get("title") or "")[:MAX_TITLE],
                    "created_at": pr.get("createdAt") or "", "draft": bool(pr.get("isDraft")),
                    "shared_files": shared_files, "shared_dirs": shared_dirs})
    out.sort(key=lambda e: (not e["shared_files"], e["created_at"], e["number"]))
    return out[:limit]


def gh(args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
    return r.stdout


def gather(repo, pr, own_paths, run=gh):
    """The overlapping open PRs, each with the declarations its Lean files add."""
    prs = json.loads(run(["pr", "list", "--repo", repo, "--state", "open",
                          "--limit", str(LIST_LIMIT),
                          "--json", "number,title,createdAt,isDraft,files"]))
    entries = overlapping(own_paths, prs, pr)
    for e in entries:
        files = json.loads(run(["api", f"repos/{repo}/pulls/{e['number']}/files?per_page=100"]))
        names = []
        for f in files:
            if (f.get("filename") or "").endswith(".lean"):
                for n in added_declarations(f.get("patch"), MAX_DECLS - len(names)):
                    if n not in names:
                        names.append(n)
            if len(names) >= MAX_DECLS:
                break
        e["declarations"] = names
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--paths-file", required=True,
                    help="this PR's changed paths, NUL-terminated (runner/pr_diff.py --paths-out)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    try:
        with open(a.paths_file, "rb") as f:
            own = {p.decode("utf-8", "surrogateescape") for p in f.read().split(b"\0") if p}
        entries = gather(a.repo, a.pr, own)
    except Exception as e:  # best-effort: no list means no block, never a failed review
        print(f"open_prs: {e}", file=sys.stderr)
        entries = []
    with open(a.out, "w") as f:
        json.dump({"prs": entries}, f, indent=1)
    print(f"open_prs: {len(entries)} overlapping open PR(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
