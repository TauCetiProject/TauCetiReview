#!/usr/bin/env python3
"""Compute the auto-merge decision from the PR's scoreboard COMMENTS (the live verdict source).

Any reviewer — the worker, or anyone running tauceti-review — posts a scoreboard comment on the PR
carrying a `<!--tauceti-meta:v1 {...}-->` block with `head_sha` and a full per-rubric `states` map. The
newest completed scoreboard at the current head is the verdict: a later completed review supersedes
an earlier one instead of making every disagreement a permanent veto. A later blocking scoreboard
makes the review state unsafe and the reconciler dequeues the PR. It is mergeable when that
completed review state is safe and the shared `decide_merge` rule also holds (build green, TauCeti/-only + allowed
root/pins, bump-guard for a pin). This is the "no bar" model: trust is the posted comment itself, so a
contributor with no repo write can still have their review count. The HARD boundary that a forged
scoreboard cannot bypass remains the CI build + scope + axiom audit + bump-guard checks.

For comments whose meta predates the `states` field, fall back to reading the rendered scoreboard
table (one row per rubric; the 3rd cell is the state word). Writes `merge.json` like before.

    merge_from_scoreboard.py --pr 183 --head-sha <sha> --comments-file comments.json \
        --merge-base-sha <sha> --paths-file paths.z --ci-build SUCCESS --bump-guard SUCCESS \
        --merge-decision-file merge.json
"""
import argparse
import os
import json
import pathlib
import subprocess
import sys

if os.environ.get("TAUCETI_CONF") or any(arg == "--conf" or arg.startswith("--conf=") for arg in sys.argv[1:]):
    from tauceti_tools.project import bootstrap
    bootstrap()

from merge import read_paths
from handoff import DEFAULT_RUBRICS
from admission_policy import (SCOREBOARD_MARKER, META_RE, TABLE_ROW_RE, WORD_STATE,
                              parse_meta, states_from_table as policy_states_from_table, current_scoreboards,
                              latest_current_scoreboard, load_comments, default_allow_files,
                              decide_from_comments as evaluate_admission)

DEFAULT_ALLOW = default_allow_files(os.environ.get("TAUCETI_LEAN_ROOT", "TauCeti"))


def states_from_table(body):
    """Preserve configured rubric names for legacy rendered scoreboards."""
    return policy_states_from_table(body, DEFAULT_RUBRICS)


def decide_from_comments(comments, head_sha, required, paths, ci_build, bump_guard,
                         merge_path_prefix=os.environ.get("TAUCETI_LEAN_ROOT", "TauCeti").replace(".", "/") + "/",
                         merge_allow_file=None, scope="", now=None, merge_base_sha="", self_tested=False):
    """CLI configuration adapter for the shared, pure admission evaluator."""
    return evaluate_admission(comments, head_sha, required, paths, ci_build, bump_guard,
                              merge_path_prefix=merge_path_prefix,
                              merge_allow_file=DEFAULT_ALLOW if merge_allow_file is None else merge_allow_file,
                              table_rubrics=DEFAULT_RUBRICS, scope=scope, now=now,
                              merge_base_sha=merge_base_sha, self_tested=self_tested)


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
    ap.add_argument("--repo", default=os.environ.get("TAUCETI_REPO", "TauCetiProject/TauCeti"))
    ap.add_argument("--pr", required=True)
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--comments-file", required=True, help="JSON array of the PR's issue comments")
    ap.add_argument("--rubrics", default=",".join(DEFAULT_RUBRICS),
                    help="comma list of rubrics that must ALL be green to merge")
    ap.add_argument("--merge-base-sha", required=True,
                    help="merge base of the PR's base and head now; must match the scoreboard's")
    ap.add_argument("--paths-file", required=True,
                    help="the PR's changed paths, NUL-terminated (runner/pr_diff.py --paths-out)")
    ap.add_argument("--ci-build", default="")
    ap.add_argument("--bump-guard", default="")
    ap.add_argument("--scope", default="",
                    help="trusted scope status; resolved from HEAD when omitted")
    ap.add_argument("--merge-path-prefix", default=os.environ.get("TAUCETI_LEAN_ROOT", "TauCeti").replace(".", "/") + "/")
    ap.add_argument("--merge-allow-file", action="append", default=list(DEFAULT_ALLOW))
    ap.add_argument("--merge-decision-file", default="")
    ap.add_argument("--conf")
    a = ap.parse_args()

    required = {r for r in a.rubrics.split(",") if r}
    try:
        text = pathlib.Path(a.comments_file).read_text()
    except OSError:
        text = ""
    try:
        paths = read_paths(a.paths_file)
    except OSError:
        paths = set()   # decide_merge refuses an empty path set
    scope = a.scope or resolve_commit_status(a.repo, a.head_sha, "scope")
    out = decide_from_comments(load_comments(text), a.head_sha, required, paths,
                               a.ci_build, a.bump_guard, a.merge_path_prefix, a.merge_allow_file,
                               scope, merge_base_sha=a.merge_base_sha)
    print(json.dumps(out))
    if a.merge_decision_file:
        pathlib.Path(a.merge_decision_file).write_text(json.dumps(out))


if __name__ == "__main__":
    main()
