#!/usr/bin/env python3
"""Compute the auto-merge decision from the PR's scoreboard COMMENT (the live verdict source).

Any reviewer — the worker, or anyone running tauceti-review — posts a scoreboard comment on the PR
carrying a `<!--tauceti-meta:v1 {...}-->` block with `head_sha` and a full per-rubric `states` map. A
PR is mergeable when the newest scoreboard comment is AT THE PR'S CURRENT HEAD and every required
rubric is green there, and the shared `decide_merge` rule holds (build green, TauCeti/-only + allowed
root/pins, bump-guard for a pin). This is the "no bar" model: trust is the posted comment itself, so a
contributor with no repo write can still have their review count. The HARD boundary that a forged
scoreboard cannot bypass remains the CI build + scope + axiom audit + bump-guard checks.

For comments whose meta predates the `states` field, fall back to reading the rendered scoreboard
table (one row per rubric; the 3rd cell is the state word). Writes `merge.json` like before.

    merge_from_scoreboard.py --pr 183 --head-sha <sha> --comments-file comments.json \
        --diff-file diff.txt --ci-build SUCCESS --bump-guard SUCCESS --merge-decision-file merge.json
"""
import argparse
import json
import pathlib
import re

from review import DEFAULT_RUBRICS, changed_paths, decide_merge

SCOREBOARD_MARKER = "<!--tauceti-scoreboard-->"
META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)
# A rendered scoreboard row: | <icon> | [rubric](url) | <state word> | `judge` | summary |
TABLE_ROW_RE = re.compile(r"^\|[^|]*\|\s*\[?([a-z0-9-]+)\]?[^|]*\|\s*([^|]+?)\s*\|", re.M)
WORD_STATE = {"approved": "green", "changes requested": "blocking_request",
              "blocked": "blocking_block", "stale (re-run pending)": "stale",
              "not yet run": "absent", "error": "error"}


def newest_scoreboard(comments):
    """The newest comment carrying the scoreboard marker. No access bar: any author; an updated or
    newer scoreboard supersedes an older one (the engine edits the canonical one in place)."""
    boards = [c for c in comments if SCOREBOARD_MARKER in (c.get("body") or "")]
    if not boards:
        return None
    boards.sort(key=lambda c: c.get("updated_at") or c.get("created_at") or "")
    return boards[-1]


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
                         merge_path_prefix="TauCeti/", merge_allow_file=None):
    """The merge gate, shared by the merge-only CLI and the merge sweep: a PR is mergeable iff the
    newest scoreboard comment is AT `head_sha` with every `required` rubric green there, and the
    `decide_merge` rule holds (build green, TauCeti/-only + allowed root/pins, bump-guard for a pin).
    Returns {"merge", "reason", "head_sha"}."""
    allow = DEFAULT_ALLOW if merge_allow_file is None else merge_allow_file
    board = newest_scoreboard(comments)
    meta = parse_meta(board.get("body")) if board else None
    if not required:
        return {"merge": False, "reason": "no rubric set; refusing to merge", "head_sha": head_sha}
    if not meta:
        return {"merge": False, "reason": "no scoreboard comment at the PR; refusing to merge",
                "head_sha": head_sha}
    if (meta.get("head_sha") or "") != head_sha:
        return {"merge": False, "head_sha": head_sha,
                "reason": (f"scoreboard is for a different head "
                           f"({(meta.get('head_sha') or '')[:7]} != {head_sha[:7]}); refusing")}
    raw = meta.get("states")
    if not isinstance(raw, dict) or not raw:
        raw = states_from_table(board.get("body"))   # old comment: derive from the rendered table
    states = {r: (raw.get(r) or "absent") for r in required}
    candidates = sorted(required)
    all_green = all(states[r] == "green" for r in required)
    paths = changed_paths(diff_text)
    merge_ok, reason = decide_merge(states, candidates, all_green, paths, head_sha,
                                    merge_path_prefix, allow, bump_guard, ci_build)
    return {"merge": merge_ok, "reason": reason, "head_sha": head_sha}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True)
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--comments-file", required=True, help="JSON array of the PR's issue comments")
    ap.add_argument("--rubrics", default=",".join(DEFAULT_RUBRICS),
                    help="comma list of rubrics that must ALL be green to merge")
    ap.add_argument("--diff-file", required=True)
    ap.add_argument("--ci-build", default="")
    ap.add_argument("--bump-guard", default="")
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
    out = decide_from_comments(load_comments(text), a.head_sha, required, diff_text,
                               a.ci_build, a.bump_guard, a.merge_path_prefix, a.merge_allow_file)
    print(json.dumps(out))
    if a.merge_decision_file:
        pathlib.Path(a.merge_decision_file).write_text(json.dumps(out))


if __name__ == "__main__":
    main()
