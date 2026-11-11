"""Pure admission policy shared by the review CLI and the queue service.

Importing this module reads no configuration, opens no files and runs no subprocesses.
Callers supply the trusted project configuration and fresh, head-bound source evidence.
"""
import json
import re


SCOREBOARD_MARKER = "<!--tauceti-scoreboard-->"
META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)
REBASE_MARKER_RE = re.compile(r"^<!--tauceti-rebase:v1 ([0-9a-f]{40})-->$", re.M)
STALLED_MARKER_RE = re.compile(r"^<!--tauceti-merge-stalled:v1 ([0-9a-f]{40})-->$", re.M)
TABLE_ROW_RE = re.compile(r"^\|[^|]*\|\s*\[?([a-z0-9-]+)\]?[^|]*\|\s*([^|]+?)\s*\|", re.M)
WORD_STATE = {"approved": "green", "changes requested": "blocking_request",
              "blocked": "blocking_block", "stale (re-run pending)": "stale",
              "not yet run": "absent", "error": "error"}

# Rubrics being phased in: for each repository, the number of the last PR opened before the rubric
# went live there, or None while it is not live. The rubric is run on, and required of, only PRs
# numbered above that, after the others. A PR opened earlier keeps the scoreboard it has. Were the
# rubric required of it, the missing row would hold the merge and nothing would add it: the worker
# counts a head as reviewed once every row its scoreboard has is complete. The table lives in this
# module so that the engine, the merge gate and the queue service read the same one.
#
# quality-pass checks the author's report of the checklist in the PR template, which only PRs
# opened after TauCetiProject/TauCeti#33936 carry. Set its cutoff ahead of the newest PR, so that
# the commit setting it reaches every reviewer and the queue service before a PR above it is
# reviewed.
PHASED_RUBRICS = {"quality-pass": {"TauCetiProject/TauCeti": None}}


def with_phased_rubrics(rubrics, repo, pr):
    """`rubrics`, then each rubric being phased in that PR number `pr` of `repo` is judged against."""
    rubrics = list(rubrics)
    try:
        number = int(pr)
    except (TypeError, ValueError):
        return rubrics
    for rubric, cutoffs in PHASED_RUBRICS.items():
        cutoff = {name.lower(): after for name, after in cutoffs.items()}.get(str(repo).lower())
        if cutoff is not None and number > cutoff and rubric not in rubrics:
            rubrics.append(rubric)
    return rubrics


def rebase_request_heads(comments, *, stalled=False):
    """Existing trusted, head-bound recovery holds, shared by sweep and service intake.

    These are recovery instructions, independently of the intentionally unrestricted authorship
    of review scoreboards. Accept the sweep's compact comment shape or raw REST issue comments.
    """
    marker = STALLED_MARKER_RE if stalled else REBASE_MARKER_RE
    return {head for c in comments
            if (c.get("author") or (c.get("user") or {}).get("login"))
               in {"tauceti-review-bot[bot]", "tau-ceti-bors[bot]"}
            and (c.get("body") or "").startswith("Merge-queue recovery for head `")
            for head in marker.findall(c.get("body") or "")}

def default_allow_files(lean_root):
    # The bibliography is read only by the pages build. TauCeti's trusted scope guard
    # allows it too; both gates must agree. Lakefiles are deliberately excluded.
    return [lean_root.replace(".", "/") + ".lean", "lake-manifest.json", "lean-toolchain",
            "docbuild/docs/references.bib"]


def decide_merge(states, candidates, all_green, paths, head, prefix, allow, bump_guard, ci_build,
                 scope="", self_tested=False):
    """The single auto-merge rule, shared by the post-review path and the no-dispatch merge mode.

    Require a head commit and either green CI or trusted local tests. Trusted local tests replace
    the build only for paths under `prefix` plus the two Lake pins, and a pin change also needs a
    green bump-guard; lakefiles and other root files always need the build. The merge path is
    self-sufficient on comment events. Every
    blocking rubric is green on it (fresh, not stale), and every changed path is under `prefix` or an
    allowed root file (`allow`), and CI's trusted scope status is green. A PR touching either Lake
    pin additionally requires the bump-guard check to be green (a CI-validated forward-only bump).
    Returns (merge_ok, reason)."""
    allow = set(allow or [])
    # Lake pins may auto-merge only as a CI-validated forward bump. Lakefiles are never allowed.
    pin_files = {"lake-manifest.json", "lean-toolchain"}
    touches_pin = bool(paths & pin_files)
    code_only = bool(paths) and all(
        p.startswith(prefix) or p in allow for p in paths)
    if not head:
        return False, "no head_sha; refusing to merge"
    # A self-tested Mathlib bump is admitted only with the trusted bump-guard validation.
    trusted_local = (self_tested and bool(paths)
                     and all(p.startswith(prefix) or p in pin_files for p in paths)
                     and (not touches_pin or (bump_guard or "").upper() == "SUCCESS"))
    if (ci_build or "").lower() != "success" and not trusted_local:
        return False, f"build is not green on HEAD (={ci_build or 'missing'}); refusing to merge"
    if not all_green:
        return False, f"not all rubrics green on HEAD: {[r for r in candidates if states[r] != 'green']}"
    if (scope or "").upper() != "SUCCESS":
        return False, (f"trusted CI scope status is not green on HEAD (={scope or 'missing'}); "
                       f"refusing to merge")
    if not code_only:
        return False, (f"PR touches paths outside {prefix} "
                       f"(allowed extras: {sorted(allow)}); needs human merge")
    if touches_pin and (bump_guard or "").upper() != "SUCCESS":
        return False, (f"PR changes a Lake pin but bump-guard is not green "
                       f"(={bump_guard or 'missing'}); needs human merge")
    return True, f"all rubrics green on {head[:7]}; {prefix}+root only"

def parse_meta(body):
    m = META_RE.findall(body or "")
    if not m:
        return None
    try:
        d = json.loads(m[-1].strip())
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None

def states_from_table(body, rubrics):
    """Fallback for comments whose meta predates `states`: derive per-rubric state from the table."""
    out = {}
    for rubric, word in TABLE_ROW_RE.findall(body or ""):
        w = word.strip().lower()
        if rubric in rubrics and w in WORD_STATE:
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

def latest_current_scoreboard(comments, head_sha):
    """The newest completed scoreboard for this exact head.

    Review init posts an in-progress scoreboard before running models. Ignore those while any
    completed scoreboard exists: starting a second review must not temporarily replace a green
    completed verdict with an empty in-progress one. If init is the only current-head scoreboard,
    retain it and fail closed from its states. Comments normally arrive in creation order; use their
    update timestamp first and their input position as a deterministic fallback/tie-break.
    """
    boards = current_scoreboards(comments, head_sha)
    if not boards:
        return None
    completed = [b for b in boards if b[1].get("mode") != "init"]
    candidates = completed or boards

    def order(item):
        index, (comment, meta) = item
        timestamp = (comment.get("updated_at") or comment.get("created_at")
                     or meta.get("ts") or "")
        return timestamp, index

    return max(enumerate(candidates), key=order)[1]

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

def decide_from_comments(comments, head_sha, required, paths, ci_build, bump_guard, *,
                         merge_path_prefix, merge_allow_file, table_rubrics, scope="", now=None,
                         merge_base_sha="", self_tested=False):
    """The gate shared by merge-only and the sweep.

    A verdict is bound to the diff it judged, which `head_sha` alone does not pin: retargeting the
    PR or rewriting the base branch changes the diff under the same head. The diff is fully
    determined by (merge base, head), and every scoreboard records the merge base it reviewed
    against (`merge_base_sha`, from the same compare API the merge job asks), so the newest
    completed scoreboard counts only if that equals `merge_base_sha`, the merge base now. It is
    stable while the base branch merely advances. A scoreboard without it, or an unknown current
    merge base, fails closed. `paths` are the PR's changed paths, machine-read
    (runner/pr_diff.py), never parsed from the patch text.

    `review_safe` is intentionally separate from `merge`: the reconciler should dequeue a PR when
    its review state becomes unsafe, but should leave a human-queued PR alone when only the automatic
    path/build policy refuses it. Returns {"review_safe", "merge", "reason", "head_sha"}.
    """
    allow = merge_allow_file
    if not required:
        return {"review_safe": False, "merge": False,
                "reason": "no rubric set; refusing to merge", "head_sha": head_sha}
    latest = latest_current_scoreboard(comments, head_sha)
    if latest is None:
        return {"review_safe": False, "merge": False,
                "reason": "no scoreboard comment for the current head; refusing",
                "head_sha": head_sha}
    board, meta = latest
    if meta.get("mode") == "init":
        # Posted before any model ran this round; whatever states it renders, no verdict for this
        # head has been completed. Fail closed regardless of the states.
        return {"review_safe": False, "merge": False,
                "reason": "no completed scoreboard for the current head yet; waiting",
                "head_sha": head_sha}
    if not merge_base_sha or meta.get("merge_base_sha") != merge_base_sha:
        return {"review_safe": False, "merge": False,
                "reason": (f"the newest completed scoreboard reviewed the diff from merge base "
                           f"{(meta.get('merge_base_sha') or 'unrecorded')[:12]}, but the merge "
                           f"base is now {(merge_base_sha or 'unknown')[:12]}; refusing"),
                "head_sha": head_sha}
    raw = meta.get("states")
    if not isinstance(raw, dict) or not raw:
        # The CLI historically filters legacy tables by configured rubrics, which can
        # differ from its explicit --rubrics selection. Preserve that distinction.
        raw = states_from_table(board.get("body"), table_rubrics)
    states = {r: (raw.get(r) or "absent") for r in required}
    if not all(states[r] == "green" for r in required):
        return {"review_safe": False, "merge": False,
                "reason": "the newest completed scoreboard for the current head is not all-green; "
                          "refusing",
                "head_sha": head_sha}
    states = {r: "green" for r in required}
    candidates = sorted(required)
    merge_ok, reason = decide_merge(states, candidates, True, set(paths), head_sha,
                                    merge_path_prefix, allow, bump_guard, ci_build, scope, self_tested)
    out = {"review_safe": True, "merge": merge_ok, "reason": reason, "head_sha": head_sha}
    if self_tested and merge_ok and (ci_build or "").lower() != "success":
        out["self_tested"] = True
    return out
