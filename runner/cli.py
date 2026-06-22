#!/usr/bin/env python3
"""`tauceti-review` — run the Tau Ceti AI review on a PR with your own subscription.

This is the user-facing front end to the same review engine CI runs (`runner/review.py` +
`runner/post.py`). Where CI bills the Anthropic / OpenAI APIs, this drives the locally
logged-in `claude` and `codex` CLIs, so the inference runs on *your* subscription at no
metered cost. You stay the trusted party: it reviews read-only, posts under your own GitHub
identity, and defaults to a dry run that prints the verdicts without touching the PR.

    tauceti-review 42                 # review PR #42, print the verdicts (no posting)
    tauceti-review 42 --post          # also post the scoreboard + threads as you
    tauceti-review 42 --rubrics scope,correctness,reuse --no-mathlib

It assembles the same reviewer workspace CI does — the PR source at its head, the roadmap, and
(unless --no-mathlib) the pinned Mathlib source for grep — then invokes the engine in
`--auth subscription` mode. The rubrics and engine come from a checkout of THIS repo
(TauCetiReview): the one you ran from if it is a checkout, else a cached shallow clone, so the
rubrics always match the engine.

Prerequisites on PATH and logged in: `git`, `gh` (`gh auth login`), `claude` (Claude
subscription) and/or `codex` (ChatGPT subscription). Each rubric is judged by whichever of the
two you have available.
"""
import argparse
import atexit
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

REVIEW_REPO = "FormalFrontier/TauCetiReview"
DEFAULT_CODE_REPO = "FormalFrontier/TauCeti"
DEFAULT_ROADMAP_REPO = "FormalFrontier/TauCetiRoadmap"
CACHE_DIR = pathlib.Path(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))) / "tauceti-review"

# A "review in progress" marker comment de-contends concurrent reviewers without any write access
# beyond commenting: an independent reviewer may have repo write NOWHERE, but anyone who can review a
# PR can comment on it. A reviewer posts the marker before spending inference and deletes it when done;
# the embedded TTL self-clears a crashed reviewer. The marker is scoped to (head) ALONE: the first run
# to claim a commit reviews it, and any other run — whatever model it would use — yields, so a commit
# is reviewed exactly once regardless of model (a new push is a fresh head, hence a fresh unit). The
# marker still records which providers are running, but only for display; it is not part of the
# de-contention key. Trust is NOT gated on author association (unlike the scoreboard, which drives
# merges): a forged marker can at worst delay a review by its TTL — no inference runs, no data lands —
# so honoring anyone's marker is what lets a fleet of non-collaborators coordinate at all.
COORD_MARKER = "tauceti-review-in-progress"
COORD_RE = re.compile(r"<!--tauceti-review-in-progress (.*?)-->", re.S)
COORD_TTL = int(os.environ.get("TAUCETI_REVIEW_INPROGRESS_TTL", "1800"))  # 30 min; > a slow review


def die(msg):
    print(f"tauceti-review: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd, capture=False, quiet=False, allow_fail=False, **kw):
    """Run a command; die on failure unless allow_fail. capture=True returns stdout/stderr."""
    if not quiet:
        print(f"$ {' '.join(cmd)}", file=sys.stderr)
    r = subprocess.run(cmd, text=True, capture_output=capture, **kw)
    if r.returncode != 0 and not allow_fail:
        if capture:
            sys.stderr.write(r.stderr or "")
        die(f"command failed ({r.returncode}): {' '.join(cmd)}")
    return r


def need(tool, hint):
    if not shutil.which(tool):
        die(f"`{tool}` not found on PATH. {hint}")


def stage_tree(src, dst, *, ignore_extra=()):
    """Copy a pre-staged input tree (roadmap / Mathlib) into the workspace. symlinks=True so a symlink
    in the staged tree is copied AS a symlink, never dereferenced — a malicious/stray link can't pull
    host files into the reviewer workspace (and transcripts). Skip .git and heavy non-source dirs so a
    plain dev checkout doesn't blow disk/time. Fail loudly instead of tracebacking."""
    src_p, dst_p = pathlib.Path(src).resolve(), pathlib.Path(dst).resolve()
    if not src_p.is_dir():
        die(f"--*-dir source is not a directory: {src}")
    if src_p == dst_p or dst_p.is_relative_to(src_p) or src_p.is_relative_to(dst_p):
        die(f"staged source/dest overlap: {src_p} vs {dst_p}")
    try:
        shutil.copytree(src_p, dst_p, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", *ignore_extra))
    except (OSError, shutil.Error) as e:
        die(f"failed to stage {src_p} -> {dst_p}: {e}")


def resolve_repo_dir(explicit):
    """Locate a TauCetiReview checkout providing rubrics/ and runner/ — engine and rubrics
    together, so they never drift. Order: --repo-dir, $TAUCETI_REVIEW_DIR, this source tree if it
    is a checkout, else a cached shallow clone refreshed each run."""
    def ok(p):
        p = pathlib.Path(p)
        return (p / "rubrics").is_dir() and (p / "runner" / "review.py").is_file()

    for cand in (explicit, os.environ.get("TAUCETI_REVIEW_DIR"),
                 pathlib.Path(__file__).resolve().parent.parent):
        if cand and ok(cand):
            return pathlib.Path(cand).resolve()

    clone = CACHE_DIR / "TauCetiReview"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if (clone / ".git").is_dir():
        run(["git", "-C", str(clone), "fetch", "-q", "--depth", "1", "origin", "main"], quiet=True)
        run(["git", "-C", str(clone), "reset", "-q", "--hard", "origin/main"], quiet=True)
    else:
        run(["git", "clone", "-q", "--depth", "1",
             f"https://github.com/{REVIEW_REPO}", str(clone)])
    if not ok(clone):
        die(f"cached clone at {clone} is missing rubrics/ or runner/review.py")
    return clone


def engine_at(sha):
    """A cached checkout of TauCetiReview pinned at `sha` — rubrics AND engine together, so a
    shadow arm reruns exactly the code+rubrics of that commit, not main's engine on old rubrics."""
    dst = CACHE_DIR / "engines" / sha[:12]
    if not (dst / "rubrics").is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q", str(dst)], quiet=True)
        run(["git", "-C", str(dst), "remote", "add", "origin",
             f"https://github.com/{REVIEW_REPO}"], quiet=True, allow_fail=True)
        run(["git", "-C", str(dst), "fetch", "-q", "--depth", "1", "origin", sha])
        run(["git", "-C", str(dst), "checkout", "-q", sha])
    if not ((dst / "rubrics").is_dir() and (dst / "runner" / "review.py").is_file()):
        die(f"checkout of {REVIEW_REPO}@{sha[:12]} is missing rubrics/ or runner/review.py")
    return dst


def gh_json(repo, pr, fields):
    r = run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", fields],
            capture=True, quiet=True)
    return json.loads(r.stdout)


def merge_base_sha(repo, base, head):
    """The merge base of base...head, from the compare API — the actual left side of the diff
    `gh pr diff` produces (baseRefOid is the branch tip, which may have moved on). Best-effort."""
    if not (base and head):
        return ""
    r = run(["gh", "api", f"/repos/{repo}/compare/{base}...{head}",
             "--jq", ".merge_base_commit.sha"], capture=True, quiet=True, allow_fail=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def rubrics_repo_sha(repo_dir):
    """The commit the rubrics+engine checkout is at, so review comments can link the rubric text
    that actually ran. Falls back to the remote main tip (approximate) when the engine runs from
    an installed package tree rather than a git checkout."""
    r = run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture=True, quiet=True, allow_fail=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip(), False
    r = run(["gh", "api", f"/repos/{REVIEW_REPO}/git/refs/heads/main",
             "--jq", ".object.sha"], capture=True, quiet=True, allow_fail=True)
    return (r.stdout.strip() if r.returncode == 0 else ""), True


def fetch_thread_replies(repo, pr):
    """Gather author replies on the per-rubric review threads from GitHub, keyed by rubric, so a
    re-review audits the author's contest rather than re-judging the diff blind. A thread root
    carries a `<!--tauceti-rubric:NAME-->` marker; a reply is any review comment whose
    `in_reply_to_id` points at such a root.

    Paginated (`--paginate --jq '.[]'` emits one compact comment per line — a bare --paginate would
    concatenate per-page arrays into invalid JSON), so a busy PR cannot hide a root or a reply past
    page one. Each reply keeps its monotonic `id` (the dedupe/watermark key), its `ts`, and the
    thread `root_id` (so a re-review can answer in-thread even with a fresh/cross-machine store that
    has not recorded the root). Our own comments are dropped by MARKER, never by author login: a
    contest answer carries `tauceti-reply:` and a thread root carries `tauceti-rubric:`; filtering by
    a poster login would wrongly drop a contest from someone who happens to share that login."""
    r = run(["gh", "api", "--paginate", "--jq", ".[]",
             f"/repos/{repo}/pulls/{pr}/comments?per_page=100"],
            capture=True, quiet=True, allow_fail=True)
    comments = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line:
            try:
                comments.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    root_rubric = {}
    for c in comments:
        if c.get("in_reply_to_id") is None:
            m = re.search(r"tauceti-rubric:([a-z][a-z-]*?)\s*-->", c.get("body", ""))
            if m:
                root_rubric[c["id"]] = m.group(1)
    replies = {}
    for c in comments:
        root = c.get("in_reply_to_id")
        rubric = root_rubric.get(root)
        if not rubric:
            continue
        body = c.get("body", "") or ""
        if "tauceti-reply:" in body or "tauceti-rubric:" in body:
            continue  # our own contest answer / a nested root — never a fresh contest
        replies.setdefault(rubric, []).append(
            {"id": c.get("id"), "ts": c.get("created_at", ""), "root_id": root,
             "by": (c.get("user") or {}).get("login", "author"), "body": body})
    return replies


def issue_comments(repo, pr):
    """All issue comments on a PR. Returns None on a fetch FAILURE (distinct from an empty list), so the
    caller can tell "no markers" from "couldn't look" and not mistake an API blip for a clear field."""
    r = run(["gh", "api", "--paginate", "--jq", ".[]",
             f"/repos/{repo}/issues/{pr}/comments?per_page=100"],
            capture=True, quiet=True, allow_fail=True)
    if r.returncode != 0:
        return None
    out = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def covered_providers(comments, head, now, *, exclude_nonce, max_id=None):
    """Providers advertised by any UNEXPIRED in-progress marker for this EXACT head — posted by a
    different run (nonce ≠ exclude_nonce). De-contention is on the head alone, so the SET being
    non-empty is what matters (a commit is reviewed once); the provider names are kept only for the
    skip message. With max_id set, only count markers whose comment id is lower (the deterministic
    tiebreak for a simultaneous post: the lowest-id marker wins the head)."""
    cov = set()
    for c in comments:
        m = COORD_RE.search(c.get("body") or "")
        if not m:
            continue
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        exp = d.get("expires_at")
        if not isinstance(exp, int) or exp <= now or d.get("nonce") == exclude_nonce:
            continue
        marker_head = d.get("head")
        if not isinstance(marker_head, str) or marker_head != head:  # exact: a new push is a new unit
            continue
        cid = c.get("id")
        if max_id is not None and not (isinstance(cid, int) and cid < max_id):
            continue
        cov.update(p for p in (d.get("providers") or []) if isinstance(p, str))
    return cov


def post_marker(repo, pr, head, providers, nonce, submitted_by):
    """Post a review-in-progress marker for `providers` on `head`; return its comment id (None on
    failure). Best-effort: an inability to comment (a reviewer with no access) just means no marker."""
    now = int(time.time())
    payload = {"schema": f"{COORD_MARKER}/v1", "nonce": nonce, "providers": list(providers),
               "head": head, "submitted_by": submitted_by or "", "started_at": now,
               "expires_at": now + COORD_TTL}
    body = (f"🔍 Review in progress — `{','.join(providers)}` reviewing `{head[:12]}`."
            f"\n<!--{COORD_MARKER} "
            f"{json.dumps(payload, separators=(',', ':'))}-->")
    r = run(["gh", "api", f"/repos/{repo}/issues/{pr}/comments", "-f", f"body={body}", "--jq", ".id"],
            capture=True, quiet=True, allow_fail=True)
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return int(out) if out.isdigit() else None


def delete_marker(repo, comment_id):
    """Remove a marker by id. Best-effort (a 404 — already gone or expired-and-gc'd — is fine)."""
    run(["gh", "api", "-X", "DELETE", f"/repos/{repo}/issues/comments/{comment_id}"],
        quiet=True, allow_fail=True)


# Markers this process owns and must remove on exit. atexit alone misses signals, so SIGTERM/SIGINT are
# routed through sys.exit (which DOES run atexit); SIGKILL can't be caught, which is what the TTL backs.
_ACTIVE_MARKERS = []
_CLEANUP_INSTALLED = False


def _delete_active_markers():
    while _ACTIVE_MARKERS:
        repo, cid = _ACTIVE_MARKERS.pop()
        delete_marker(repo, cid)


def _install_marker_cleanup():
    """Arm marker deletion on normal exit and on SIGTERM/SIGINT. Idempotent; installed only once we
    actually own a marker, so a non-coordinating run never changes signal disposition."""
    global _CLEANUP_INSTALLED
    if _CLEANUP_INSTALLED:
        return
    _CLEANUP_INSTALLED = True
    atexit.register(_delete_active_markers)
    for sig, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
        try:
            signal.signal(sig, lambda *_a, _c=code: sys.exit(_c))
        except (ValueError, OSError):
            pass  # not the main thread / unsupported — atexit + TTL still apply


def _recheck_lost(repo, pr, head, nonce, cid):
    """After posting, return the providers any LOWER-id foreign marker advertises on this head — a
    non-empty result means a peer claimed the head first and we must yield the whole run. Poll until
    our OWN comment is visible — so the list is current enough for the lowest-id rule to be real, not
    fooled by replication lag where neither poster yet sees the other — or a short deadline passes.
    Time is re-read each scan so an expiring lower-id marker isn't honored past its TTL."""
    lost = set()
    for delay in (0.0, 0.4, 0.8, 1.5):
        if delay:
            time.sleep(delay)
        comments = issue_comments(repo, pr)
        if comments is None:
            continue
        lost = covered_providers(comments, head, int(time.time()), exclude_nonce=nonce, max_id=cid)
        if lost or any(c.get("id") == cid for c in comments):
            break
    return lost


def coordinate(repo, pr, head, avail, submitted_by):
    """[COOP] de-contend concurrent reviewers via a review-in-progress comment, on the head ALONE: a
    commit is reviewed once regardless of model. Returns `avail` (this run owns the head and should
    review it) having posted our own marker and armed its deletion at exit, or [] to skip the whole run
    and spend nothing because another run already holds the head. A different model is NOT a distinct
    unit — the first claimer wins — but a new push is a new head, hence a new claim.

    Not an atomic CAS, so after posting we re-read (waiting until our own comment is visible) and yield
    if any LOWER-id foreign marker is on the head — the lowest-id marker wins, which collapses the
    simultaneous-post window. A read/post failure (e.g. no comment access) proceeds unclaimed: a
    possible duplicate review, never corruption.
    """
    nonce = uuid.uuid4().hex
    comments = issue_comments(repo, pr)
    if comments is None:
        print("note: couldn't list PR comments to check for an in-flight review; proceeding and still "
              "posting our marker (a concurrent duplicate review is possible).", file=sys.stderr)
        comments = []
    cov = covered_providers(comments, head, int(time.time()), exclude_nonce=nonce)
    if cov:
        print(f"PR #{pr} @ {head[:12]} is already being reviewed ({','.join(sorted(cov))}) — "
              f"skipping to avoid duplicate spend.", file=sys.stderr)
        return []
    run_set = list(avail)
    cid = post_marker(repo, pr, head, run_set, nonce, submitted_by)
    if cid is None:
        print("note: couldn't post a review-in-progress marker (no comment access?); proceeding "
              "without de-contention — a concurrent duplicate review is possible.", file=sys.stderr)
        return run_set
    lost = _recheck_lost(repo, pr, head, nonce, cid)
    if lost:
        print(f"PR #{pr} @ {head[:12]}: lost the post race ({','.join(sorted(lost))} claimed it "
              f"first) — skipping.", file=sys.stderr)
        delete_marker(repo, cid)
        return []
    _ACTIVE_MARKERS.append((repo, cid))
    _install_marker_cleanup()
    return run_set


def main():
    ap = argparse.ArgumentParser(
        prog="tauceti-review",
        description="Run the Tau Ceti AI review on a PR using your own claude/codex subscription.")
    ap.add_argument("pr", help="PR number to review")
    ap.add_argument("--repo", default=DEFAULT_CODE_REPO, help="code repo (owner/name)")
    ap.add_argument("--roadmap-repo", default=DEFAULT_ROADMAP_REPO)
    ap.add_argument("--rubrics", default="", help="comma-separated subset (default: all)")
    ap.add_argument("--mode", default="commit", choices=["commit", "manual"],
                    help="commit (default): re-run only unresolved rubrics, carry prior approvals "
                         "forward as ♻️ (stale) until the PR is otherwise clean, then sweep them; "
                         "manual: force a full re-review of every rubric")
    ap.add_argument("--auth", default="subscription", choices=["subscription", "api"],
                    help="subscription (default): use your logged-in claude/codex; api: use "
                         "ANTHROPIC_API_KEY / OPENAI_API_KEY from the environment (billed)")
    ap.add_argument("--post", action="store_true",
                    help="post the scoreboard + per-rubric threads to the PR as you "
                         "(default: dry run — print the review, post nothing)")
    ap.add_argument("--reviewer", default="",
                    help="restrict to these reviewers (comma-separated: claude, codex, sonnet, "
                         "deepseek, minimax, grok). sonnet is the claude CLI pinned to Sonnet; "
                         "deepseek/minimax/grok run an OpenRouter model through the `pi` agent and "
                         "need `pi` on PATH + OPENROUTER_API_KEY. sonnet/deepseek/minimax/grok are "
                         "explicit-only (never auto-drawn). Default: every auto-drawn reviewer you "
                         "have available (claude, codex)")
    ap.add_argument("--no-mathlib", action="store_true",
                    help="skip fetching the pinned Mathlib source (faster; reuse checks weaker)")
    ap.add_argument("--roadmap-dir", default="",
                    help="use a pre-staged roadmap tree (copied into the workspace) instead of cloning "
                         "--roadmap-repo. For sandboxes whose network can't reach a second repo "
                         "(e.g. a repo-scoped review bubble): stage the roadmap on the host and mount it.")
    ap.add_argument("--mathlib-dir", default="",
                    help="use a pre-staged Mathlib source tree (copied into the workspace) instead of "
                         "fetching it. Same motivation as --roadmap-dir; ignored with --no-mathlib.")
    ap.add_argument("--repo-dir", default="",
                    help="path to a TauCetiReview checkout (default: auto-detect / cached clone)")
    ap.add_argument("--workdir", default="", help="workspace dir (default: a fresh temp dir)")
    ap.add_argument("--keep", action="store_true", help="keep the workspace dir after finishing")
    ap.add_argument("--store", default="",
                    help="review store dir holding the ledger (scoreboard/thread comment ids + "
                         "per-rubric verdicts). Default: a persistent per-repo store under the "
                         "cache, so a re-review UPDATES the existing scoreboard and threads in "
                         "place and --mode commit re-runs only unresolved rubrics")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the persistent store and start clean (posts a new scoreboard)")
    ap.add_argument("--expect-head", default="",
                    help="abort unless the PR head matches this commit (a prefix is fine). Use it "
                         "right after a push so a propagation lag can't make the review run against "
                         "a stale head")
    ap.add_argument("--no-archive", action="store_true",
                    help="skip writing durable archive records (and the TauCetiData sync). "
                         "Default: every run is archived to <store>/outbox and synced")
    ap.add_argument("--no-sync", action="store_true",
                    help="archive records to <store>/outbox but do NOT push them to TauCetiData. "
                         "Use when a trusted caller drains the outbox afterwards with --sync-only, "
                         "e.g. a network-restricted review bubble whose host syncs for it.")
    ap.add_argument("--sync-only", action="store_true",
                    help="do not review: just drain <store>/outbox into TauCetiData and exit "
                         "(nonzero exit on push failure). Pairs with a prior --no-sync run.")
    ap.add_argument("--submitted-by", default="",
                    help="GitHub login to stamp on records as the publisher (metadata only — NOT "
                         "part of any record's content identity/hash). Default: the gh-authenticated "
                         "login, or $GITHUB_ACTOR in CI.")
    ap.add_argument("--data-dir", default="",
                    help="TauCetiData checkout the archive sync pushes through "
                         "(default: a cached clone under the cache dir)")
    ap.add_argument("--shadow", action="store_true",
                    help="run an A/B arm: same PR, same diff, but the results are only archived "
                         "to TauCetiData — nothing is posted and the production review state is "
                         "untouched (scratch store). Requires --label; combine with --reviewer "
                         "and/or --rubrics-sha to vary the arm")
    ap.add_argument("--label", default="",
                    help="shadow arm label, recorded as arm=shadow:<label> on every record")
    ap.add_argument("--rubrics-sha", default="",
                    help="run the rubrics AND engine pinned at this TauCetiReview commit "
                         "(a cached per-SHA checkout), instead of the floating main")
    ap.add_argument("--no-coordinate", action="store_true",
                    help="skip the review-in-progress marker. By default a contributing run (one that "
                         "posts or archives) posts a short-lived marker comment and skips a provider "
                         "another reviewer is already running on the same head — so a fleet doesn't "
                         "pay twice for identical work. Pass this to review without touching the PR "
                         "(e.g. a private read-only pass), at the cost of possible duplicate spend.")
    ap.add_argument("--max-rounds-per-day", type=int, default=12,
                    help="per-PR daily round cap (UTC day); past this the run refuses without spending. "
                         "Forwarded to review.py. Exposing it here lets a caller (e.g. the worker, whose "
                         "survey prefilters capped PRs) drive the prefilter and the engine from one value.")
    a = ap.parse_args()

    # --sync-only: no review, just drain an existing store's outbox into TauCetiData and exit. The
    # host runs this after a --no-sync review (e.g. a bubble) to publish with its own creds. Loud:
    # `run` (no allow_fail) exits nonzero if archive.py sync fails after its retries.
    if a.sync_only:
        need("git", "Install git.")
        if not a.store:
            die("--sync-only requires --store <dir>.")
        outbox = pathlib.Path(a.store) / "outbox"
        if not outbox.is_dir() or not any(p.is_file() for p in outbox.rglob("*")):
            print("tauceti-review --sync-only: outbox empty; nothing to sync")
            return
        repo_dir = engine_at(a.rubrics_sha) if a.rubrics_sha else resolve_repo_dir(a.repo_dir)
        data_dir = a.data_dir or str(CACHE_DIR / "data" / "TauCetiData")
        run([sys.executable, str(repo_dir / "runner" / "archive.py"), "sync",
             "--store", str(a.store), "--data-dir", data_dir])
        return

    # Who published this review (metadata only). Auto-detect unless the caller set it (incl. empty).
    # Guard on `gh` existing: run() with allow_fail catches a nonzero exit but not a missing binary.
    if not a.submitted_by and not any(
            arg == "--submitted-by" or arg.startswith("--submitted-by=") for arg in sys.argv):
        a.submitted_by = (os.environ.get("GITHUB_ACTOR")
                          or (run(["gh", "api", "user", "--jq", ".login"],
                                  capture=True, quiet=True, allow_fail=True).stdout.strip()
                              if shutil.which("gh") else ""))

    if a.shadow and not a.label:
        die("--shadow requires --label <name> (it tags every archived record).")
    if a.shadow and a.post:
        die("--shadow and --post are mutually exclusive: shadow arms are never posted.")
    if a.shadow and a.no_archive:
        die("--shadow without archiving is pure spend; drop --no-archive.")
    if a.shadow and a.no_sync:
        die("--shadow with --no-sync archives to the cache, not <store>/outbox, so a host-side "
            "--sync-only could not drain it; a shadow arm syncs itself — drop --no-sync.")

    need("git", "Install git.")
    need("gh", "Install the GitHub CLI and run `gh auth login`.")
    want = [p.strip() for p in a.reviewer.split(",") if p.strip()] if a.reviewer else []
    if a.auth == "subscription":
        avail = [p for p in ("claude", "codex") if shutil.which(p)]
    else:  # api: draw only from providers whose key is in the environment
        avail = [p for p, k in (("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY"))
                 if os.environ.get(k)]
    # sonnet is a cheaper claude-family A/B arm: same `claude` binary / ANTHROPIC_API_KEY as
    # claude, but explicit-only (never auto-drawn) so default reviews stay on Opus.
    claude_ok = shutil.which("claude") if a.auth == "subscription" else os.environ.get("ANTHROPIC_API_KEY")
    if "sonnet" in want and claude_ok and "sonnet" not in avail:
        avail.append("sonnet")
    # OpenRouter reviewers (DeepSeek/MiniMax/Grok, driven by the `pi` agent) are pay-per-token, so
    # they are NEVER drawn by default — they join the pool ONLY when you name them in --reviewer
    # (the budget gate: no auto-dispatch), and then only if `pi` and OPENROUTER_API_KEY are present.
    if shutil.which("pi") and os.environ.get("OPENROUTER_API_KEY"):
        avail += [p for p in ("deepseek", "minimax", "grok") if p in want and p not in avail]
    if not avail:
        if a.auth == "subscription":
            die("need at least one of `claude` / `codex` on PATH (and logged in), or `pi` + "
                "OPENROUTER_API_KEY and `--reviewer deepseek|minimax` for an OpenRouter review. "
                "Install the Claude Code and/or Codex CLI, or pass --auth api.")
        die("--auth api needs ANTHROPIC_API_KEY / OPENAI_API_KEY, or `pi` + OPENROUTER_API_KEY "
            "and `--reviewer deepseek|minimax`, in the environment.")
    if want:
        avail = [p for p in avail if p in want]
        if not avail:
            die(f"--reviewer {a.reviewer} matches none of the available reviewers (a DeepSeek/"
                "MiniMax reviewer needs `pi` on PATH + OPENROUTER_API_KEY).")
    providers = ",".join(avail)
    print(f"reviewers: {providers}", file=sys.stderr)

    repo_dir = engine_at(a.rubrics_sha) if a.rubrics_sha else resolve_repo_dir(a.repo_dir)
    print(f"engine + rubrics: {repo_dir}"
          + (f" (pinned @ {a.rubrics_sha[:12]})" if a.rubrics_sha else ""), file=sys.stderr)

    work = pathlib.Path(a.workdir) if a.workdir else pathlib.Path(tempfile.mkdtemp(
        prefix=f"tauceti-review-{a.pr}-"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"workspace: {work}", file=sys.stderr)

    # PR head, base, diff, and description (author-provided context, no more trusted than the
    # diff). The base/merge-base SHAs are provenance: they pin exactly which diff was reviewed.
    refs = gh_json(a.repo, a.pr, "headRefOid,baseRefOid")
    head, base = refs["headRefOid"], refs.get("baseRefOid", "")
    print(f"PR #{a.pr} head: {head[:12]}", file=sys.stderr)
    if a.expect_head and not (head.startswith(a.expect_head) or a.expect_head.startswith(head)):
        die(f"PR #{a.pr} head is {head[:12]}, expected {a.expect_head[:12]}. The push may not have "
            "propagated to the API yet; re-run in a moment to avoid reviewing a stale commit.")

    # De-contend concurrent reviewers BEFORE the expensive workspace setup + inference. Only for a
    # contributing run (one that posts or archives): a pure read-only dry run (--no-archive, no --post)
    # touches nothing, and a shadow arm deliberately re-reviews the same diff, so both opt out.
    if (a.post or not a.no_archive) and not a.shadow and not a.no_coordinate:
        avail = coordinate(a.repo, a.pr, head, avail, a.submitted_by)
        if not avail:
            if not a.keep and not a.workdir:
                shutil.rmtree(work, ignore_errors=True)
            return
        providers = ",".join(avail)
        print(f"reviewers (after de-contention): {providers}", file=sys.stderr)

    diff = run(["gh", "pr", "diff", str(a.pr), "--repo", a.repo], capture=True, quiet=True).stdout
    (work / "diff.txt").write_text(diff)
    # CI's build-check conclusion for this head — GitHub's own result (trusted, not author input).
    # Passed to the engine so the prompt can assert the code compiles; best-effort (a fetch failure
    # just leaves it blank, and the engine then injects nothing).
    ci_build = ""
    try:
        rollup = gh_json(a.repo, a.pr, "statusCheckRollup").get("statusCheckRollup") or []
        ci_build = next((c.get("conclusion", "") for c in rollup if c.get("name") == "build"), "")
    except Exception:
        ci_build = ""
    meta = gh_json(a.repo, a.pr, "title,body")
    (work / "pr_desc.txt").write_text(
        f"# {meta.get('title','')}\n\n{meta.get('body','') or ''}\n")

    # Reviewer workspace: PR source at head, roadmap, optional Mathlib source. No .git, no creds.
    run(["git", "clone", "-q", "--depth", "1", f"https://github.com/{a.repo}",
         str(work / "code")])
    run(["git", "-C", str(work / "code"), "fetch", "-q", "--depth", "1", "origin", head], quiet=True)
    run(["git", "-C", str(work / "code"), "checkout", "-q", head], quiet=True)
    shutil.rmtree(work / "code" / ".git", ignore_errors=True)
    if a.roadmap_dir:   # pre-staged (e.g. mounted into a network-restricted bubble); copy, don't clone
        stage_tree(a.roadmap_dir, work / "roadmap")
    else:
        run(["git", "clone", "-q", "--depth", "1", f"https://github.com/{a.roadmap_repo}",
             str(work / "roadmap")])
        shutil.rmtree(work / "roadmap" / ".git", ignore_errors=True)

    mathlib_args = []
    if not a.no_mathlib and a.mathlib_dir:   # pre-staged Mathlib source; copy into the workspace
        stage_tree(a.mathlib_dir, work / "mathlib",
                   ignore_extra=(".lake", "build", ".cache", ".elan"))
        mathlib_args = ["--mathlib-path", "mathlib"]
    elif not a.no_mathlib:
        # Mathlib rev comes from the PR head's own manifest here (local trusted use); CI instead
        # pins it from the base repo to avoid evaluating attacker-controlled manifests.
        manifest = work / "code" / "lake-manifest.json"
        rev = ""
        if manifest.is_file():
            for pkg in json.loads(manifest.read_text()).get("packages", []):
                if pkg.get("name") == "mathlib":
                    rev = pkg.get("rev", "")
        if rev:
            ml = work / "mathlib"
            run(["git", "init", "-q", str(ml)], quiet=True)
            run(["git", "-C", str(ml), "remote", "add", "origin",
                 "https://github.com/leanprover-community/mathlib4"], quiet=True)
            print(f"fetching Mathlib source @ {rev[:12]} (for reuse/grep)…", file=sys.stderr)
            run(["git", "-C", str(ml), "fetch", "-q", "--depth", "1", "origin", rev], quiet=True)
            run(["git", "-C", str(ml), "checkout", "-q", "FETCH_HEAD"], quiet=True)
            mathlib_args = ["--mathlib-path", "mathlib"]
        else:
            print("note: no mathlib rev in lake-manifest.json; skipping Mathlib source.",
                  file=sys.stderr)

    # The store (ledger of scoreboard/thread comment ids + per-rubric verdicts) is PERSISTENT and
    # lives outside the throwaway workspace, so a re-review edits the same scoreboard and threads in
    # place instead of posting duplicates, and --mode commit can re-run only unresolved rubrics.
    if a.shadow:
        # Scratch store, always: a shadow arm must never read or write production review state
        # (case files, staleness, comment ids). review.py refuses a production-looking store too.
        store = work / "store"
    elif a.store:
        store = pathlib.Path(a.store)
    elif a.fresh:
        store = work / "store"
    else:
        store = CACHE_DIR / "store" / a.repo.replace("/", "__")
    store.mkdir(parents=True, exist_ok=True)
    print(f"store: {store}{'  (scratch: shadow)' if a.shadow else '  (fresh)' if a.fresh else ''}",
          file=sys.stderr)

    # Read the author's replies on the rubric threads from GitHub and pass them to the engine, so a
    # re-review audits the contest (e.g. a push-back on a finding) instead of re-judging the diff
    # blind. This is the local equivalent of CI's reply-trigger flow.
    replies = fetch_thread_replies(a.repo, a.pr)
    replies_path = work / "replies.json"
    replies_path.write_text(json.dumps(replies))
    if replies:
        print("author replies on threads: "
              + ", ".join(f"{k}×{len(v)}" for k, v in replies.items()), file=sys.stderr)

    rub_sha, rub_approx = rubrics_repo_sha(repo_dir)
    # Shadow outbox lives under the PERSISTENT store, not the throwaway scratch one: if the
    # sync at the end fails, the records must survive the workspace cleanup for a later sync.
    outbox_store = (CACHE_DIR / "store" / a.repo.replace("/", "__")) if a.shadow else store
    outbox = "" if a.no_archive else str(outbox_store / "outbox")
    plan = work / "post_plan.json"
    cmd = [sys.executable, str(repo_dir / "runner" / "review.py"),
           "--repo", a.repo, "--pr", str(a.pr), "--mode", "manual" if a.shadow else a.mode,
           *(["--shadow", "--arm", f"shadow:{a.label}"] if a.shadow else []),
           "--rubrics-dir", str(repo_dir / "rubrics"), "--tool-cwd", str(work),
           "--code-path", "code", "--roadmap-path", "roadmap",
           *mathlib_args,
           "--diff-file", str(work / "diff.txt"), "--pr-desc-file", str(work / "pr_desc.txt"),
           "--store", str(store), "--head-sha", head, "--base-sha", base,
           "--merge-base-sha", merge_base_sha(a.repo, base, head),
           "--rubrics-sha", rub_sha, *(["--rubrics-sha-approx"] if rub_approx else []),
           *(["--archive-dir", outbox] if outbox else []),
           *(["--submitted-by", a.submitted_by] if a.submitted_by else []),
           "--ci-build", ci_build or "", "--auth", a.auth,
           "--providers", providers, "--daily-budget", "1000000", "--no-post",
           "--max-rounds-per-day", str(a.max_rounds_per_day),
           "--scoreboard-file", str(work / "scoreboard.md"),
           "--threads-dir", str(work / "threads"), "--post-plan-file", str(plan),
           "--replies-json", str(replies_path)]
    if a.rubrics:
        cmd += ["--rubrics", a.rubrics]
    print("\n=== running review (this calls claude/codex per rubric; takes a few minutes) ===\n",
          file=sys.stderr)
    run(cmd)

    # The review step exits 0 having written a scoreboard on every path this CLI drives (commit /
    # manual / shadow). A clean exit with no scoreboard means the engine did not actually run: a
    # broken or partial engine (e.g. a missing entry point), not a review verdict. Fail at the phase
    # boundary instead of limping on to post a nonexistent plan with a cryptic downstream error.
    sb = (work / "scoreboard.md")
    if not sb.is_file():
        die(f"review step exited cleanly but produced no scoreboard ({sb}); the engine did not run.")
    print("\n" + "=" * 72)
    print(sb.read_text())
    threads = sorted((work / "threads").glob("*.md")) if (work / "threads").is_dir() else []
    for t in threads:
        print("\n" + "-" * 72 + f"\n[thread] {t.stem}\n")
        print(t.read_text())
    print("=" * 72 + "\n")

    if a.shadow:
        print(f"shadow arm `{a.label}` complete — archived, nothing posted.", file=sys.stderr)
    elif a.post:
        if not plan.is_file():
            die(f"review step exited cleanly but wrote no post plan ({plan}); refusing to post.")
        token = run(["gh", "auth", "token"], capture=True, quiet=True).stdout.strip()
        if not token:
            die("`gh auth token` returned nothing; run `gh auth login` first.")
        print("posting scoreboard + threads to the PR as you…", file=sys.stderr)
        env = {**os.environ, "GH_TOKEN": token}
        run([sys.executable, str(repo_dir / "runner" / "post.py"),
             "--repo", a.repo, "--pr", str(a.pr), "--plan", str(plan), "--store", str(store),
             *(["--archive-dir", outbox] if outbox else [])],
            env=env)
        print("posted.", file=sys.stderr)
    else:
        print("dry run — nothing posted. Re-run with --post to publish this review.",
              file=sys.stderr)

    # Drain the archive outbox into TauCetiData. Best-effort by design: a push outage keeps the
    # records in <store>/outbox, and the next run (or `archive.py sync` / `--sync-only`) lands them.
    # --no-sync skips this push: the records stay in the outbox for a trusted caller (the worker
    # host) to drain with --sync-only, which is how a network-restricted bubble publishes.
    if outbox and not a.no_sync and pathlib.Path(outbox).is_dir():
        data_dir = a.data_dir or str(CACHE_DIR / "data" / "TauCetiData")
        r = run([sys.executable, str(repo_dir / "runner" / "archive.py"), "sync",
                 "--store", str(outbox_store), "--data-dir", data_dir], allow_fail=True)
        if r.returncode != 0:
            print("note: archive sync failed; records remain in the outbox and will sync "
                  "on a later run.", file=sys.stderr)

    if not a.keep and not a.workdir:
        shutil.rmtree(work, ignore_errors=True)
    elif a.keep:
        print(f"workspace kept at {work}", file=sys.stderr)


if __name__ == "__main__":
    main()
