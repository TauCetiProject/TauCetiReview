"""tauceti-review merge — split from review.py (behaviour-preserving).

Run as a script (runner/ on sys.path), so imports are flat siblings, not package-relative."""

import re


def changed_paths(diff_text):
    """Repo-relative paths touched by a unified diff (both sides, to catch renames/deletes).

    Parsed from the human patch headers, which git quotes for names with control or non-ASCII
    bytes (and this skips): display only. A merge decision reads `read_paths` instead."""
    paths = set()
    for m in re.finditer(r"^diff --git a/(.+?) b/(.+)$", diff_text, flags=re.M):
        paths.add(m.group(1)); paths.add(m.group(2))
    return paths


def read_paths(path):
    """The changed paths runner/pr_diff.py wrote (`--paths-out`): NUL-terminated raw names, exact
    whatever bytes they contain."""
    with open(path, "rb") as f:
        return {p.decode("utf-8", "surrogateescape") for p in f.read().split(b"\0") if p}



if __package__:
    from .admission_policy import decide_merge
else:
    from admission_policy import decide_merge
