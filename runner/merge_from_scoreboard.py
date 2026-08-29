#!/usr/bin/env python3
"""Compute the auto-merge decision from the PR's scoreboard COMMENTS (the live verdict source).

Any reviewer — the worker, or anyone running tauceti-review — posts a scoreboard comment on the PR
carrying a `<!--tauceti-meta:v1 {...}-->` block with `head_sha` and a full per-rubric `states` map. A
PR's review state is safe for the queue when at least one scoreboard is at the current head, every
scoreboard at that head has every required rubric green, and no unexpired review-in-progress marker
claims that head. It is mergeable when that review state is safe and the shared `decide_merge` rule
also holds (build green, TauCeti/-only + allowed root/pins, bump-guard for a pin). This is the "no bar"
model: trust is the posted comment itself, so a contributor with no repo write can still have their
review count. The HARD boundary that a forged scoreboard cannot bypass remains the CI build + scope +
axiom audit + bump-guard checks.

For comments whose meta predates the `states` field, fall back to reading the rendered scoreboard
table (one row per rubric; the 3rd cell is the state word). Writes `merge.json` like before.

    merge_from_scoreboard.py --pr 183 --head-sha <sha> --comments-file comments.json \
        --diff-file diff.txt --ci-build SUCCESS --bump-guard SUCCESS --merge-decision-file merge.json
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys
import time

from review import DEFAULT_RUBRICS, changed_paths, decide_merge

SCOREBOARD_MARKER = "<!--tauceti-scoreboard-->"
META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)
COORD_RE = re.compile(r"<!--tauceti-review-in-progress (.*?)-->", re.S)
# A rendered scoreboard row: | <icon> | [rubric](url) | <state word> | `judge` | summary |
TABLE_ROW_RE = re.compile(r"^\|[^|]*\|\s*\[?([a-z0-9-]+)\]?[^|]*\|\s*([^|]+?)\s*\|", re.M)
WORD_STATE = {"approved": "green", "changes requested": "blocking_request",
              "blocked": "blocking_block", "stale (re-run pending)": "stale",
              "not yet run": "absent", "error": "error"}


def parse_meta(body):
    m = META_RE.findall(body or "")
    if not m:
        return None
    try:
        d = json.loads(m[-1].strip())
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None


def states_from_table(body):
    """Fallback for comments whose meta predates `states`: derive per-rubric state from the table."""
    out = {}
    for rubric, word in TABLE_ROW_RE.findall(body or ""):
        w = word.strip().lower()
        if rubric in DEFAULT_RUBRICS and w in WORD_STATE:
            out[rubric] = WORD_STATE[w]
    return out


def current_scoreboards(comments, head_sha):
    """Valid scoreboard comments for this exact head. There is deliberately no author access bar:
    ordinary contributors' posted reviews count, just as they did in the original merge gate."""
    out = []
    for comment in comments:
        body = comment.get("body") or ""
        if SCOREBOARD_MARKER not in body:
            continue
        meta = parse_meta(body)
        if meta and meta.get("head_sha") == head_sha:
            out.append((comment, meta))
    return out


def has_live_review(comments, head_sha, now=None):
    """Whether an unexpired review-in-progress marker claims this exact head.

    Markers need only normal issue-comment access, so independent contributors can pause the queue
    while reviewing without gaining repository permissions. Invalid, expired, and other-head markers
    are ignored. A crashed reviewer self-clears when its marker's existing TTL expires.
    """
    now = int(time.time()) if now is None else now
    for comment in comments:
        match = COORD_RE.search(comment.get("body") or "")
        if not match:
            continue
        try:
            marker = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(marker, dict):
            continue
        expires = marker.get("expires_at")
        if marker.get("head") == head_sha and isinstance(expires, int) and expires > now:
            return True
    return False


DEFAULT_ALLOW = ["TauCeti.lean", "lake-manifest.json", "lean-toolchain"]


def load_comments(text):
    """Parse a comments file that is either a single JSON array or JSONL (one comment object per line
    — what `gh api --paginate --jq '.[]|...'` emits across pages, so all comments are seen, not just
    page 1). Distinguish by the leading bracket: a single JSONL line is itself valid JSON (a dict), so
    a plain json.loads would silently drop it."""
    if text.lstrip()[:1] == "[":
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def decide_from_comments(comments, head_sha, required, diff_text, ci_build, bump_guard,
                         merge_path_prefix="TauCeti/", merge_allow_file=None, scope="", now=None):
    """The gate shared by merge-only and the sweep.

    `review_safe` is intentionally separate from `merge`: the reconciler should dequeue a PR when
    its review state becomes unsafe, but should leave a human-queued PR alone when only the automatic
    path/build policy refuses it. Returns {"review_safe", "merge", "reason", "head_sha"}.
    """
    allow = DEFAULT_ALLOW if merge_allow_file is None else merge_allow_file
    if not required:
        return {"review_safe": False, "merge": False,
                "reason": "no rubric set; refusing to merge", "head_sha": head_sha}
    boards = current_scoreboards(comments, head_sha)
    if not boards:
        return {"review_safe": False, "merge": False,
                "reason": "no scoreboard comment for the current head; refusing",
                "head_sha": head_sha}
    for board, meta in boards:
        raw = meta.get("states")
        if not isinstance(raw, dict) or not raw:
            raw = states_from_table(board.get("body"))  # old comment: derive from rendered table
        states = {r: (raw.get(r) or "absent") for r in required}
        if not all(states[r] == "green" for r in required):
            return {"review_safe": False, "merge": False,
                    "reason": "a scoreboard for the current head is not all-green; refusing",
                    "head_sha": head_sha}
    if has_live_review(comments, head_sha, now):
        return {"review_safe": False, "merge": False,
                "reason": "a review is in progress for the current head; refusing",
                "head_sha": head_sha}

    states = {r: "green" for r in required}
    candidates = sorted(required)
    paths = changed_paths(diff_text)
    merge_ok, reason = decide_merge(states, candidates, True, paths, head_sha,
                                    merge_path_prefix, allow, bump_guard, ci_build, scope)
    return {"review_safe": True, "merge": merge_ok, "reason": reason, "head_sha": head_sha}


def resolve_commit_status(repo, head_sha, context):
    """Read the newest trusted commit status for `context`; failure returns missing."""
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{head_sha}/statuses", "--jq",
             f'map(select(.context == "{context}")) | sort_by(.id) | last | .state // ""'],
            check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"could not resolve trusted {context} status for {repo}@{head_sha[:12]}: {exc}",
              file=sys.stderr)
        return ""
    return out.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="TauCetiProject/TauCeti")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--comments-file", required=True, help="JSON array of the PR's issue comments")
    ap.add_argument("--rubrics", default=",".join(DEFAULT_RUBRICS),
                    help="comma list of rubrics that must ALL be green to merge")
    ap.add_argument("--diff-file", required=True)
    ap.add_argument("--ci-build", default="")
    ap.add_argument("--bump-guard", default="")
    ap.add_argument("--scope", default="",
                    help="trusted scope status; resolved from HEAD when omitted")
    ap.add_argument("--merge-path-prefix", default="TauCeti/")
    ap.add_argument("--merge-allow-file", action="append", default=list(DEFAULT_ALLOW))
    ap.add_argument("--merge-decision-file", default="")
    a = ap.parse_args()

    required = {r for r in a.rubrics.split(",") if r}
    try:
        text = pathlib.Path(a.comments_file).read_text()
    except OSError:
        text = ""
    try:
        diff_text = pathlib.Path(a.diff_file).read_text()
    except OSError:
        diff_text = ""
    scope = a.scope or resolve_commit_status(a.repo, a.head_sha, "scope")
    out = decide_from_comments(load_comments(text), a.head_sha, required, diff_text,
                               a.ci_build, a.bump_guard, a.merge_path_prefix, a.merge_allow_file,
                               scope)
    print(json.dumps(out))
    if a.merge_decision_file:
        pathlib.Path(a.merge_decision_file).write_text(json.dumps(out))


if __name__ == "__main__":
    main()
