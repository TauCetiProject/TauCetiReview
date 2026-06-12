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
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REVIEW_REPO = "FormalFrontier/TauCetiReview"
DEFAULT_CODE_REPO = "FormalFrontier/TauCeti"
DEFAULT_ROADMAP_REPO = "FormalFrontier/TauCetiRoadmap"
CACHE_DIR = pathlib.Path(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))) / "tauceti-review"


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
    `in_reply_to_id` points at such a root."""
    r = run(["gh", "api", f"/repos/{repo}/pulls/{pr}/comments?per_page=100"],
            capture=True, quiet=True, allow_fail=True)
    try:
        comments = json.loads(r.stdout)
    except Exception:
        return {}
    root_rubric = {}
    for c in comments:
        if c.get("in_reply_to_id") is None:
            m = re.search(r"tauceti-rubric:([a-z][a-z-]*?)\s*-->", c.get("body", ""))
            if m:
                root_rubric[c["id"]] = m.group(1)
    replies = {}
    for c in comments:
        rubric = root_rubric.get(c.get("in_reply_to_id"))
        if rubric:
            replies.setdefault(rubric, []).append(
                {"by": (c.get("user") or {}).get("login", "author"), "body": c.get("body", "")})
    return replies


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
                    help="restrict to these reviewers (comma-separated: claude, codex, "
                         "deepseek, minimax). deepseek/minimax run an OpenRouter model through "
                         "the `pi` agent and need `pi` on PATH + OPENROUTER_API_KEY. "
                         "Default: every one you have available")
    ap.add_argument("--no-mathlib", action="store_true",
                    help="skip fetching the pinned Mathlib source (faster; reuse checks weaker)")
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
    a = ap.parse_args()

    if a.shadow and not a.label:
        die("--shadow requires --label <name> (it tags every archived record).")
    if a.shadow and a.post:
        die("--shadow and --post are mutually exclusive: shadow arms are never posted.")
    if a.shadow and a.no_archive:
        die("--shadow without archiving is pure spend; drop --no-archive.")

    need("git", "Install git.")
    need("gh", "Install the GitHub CLI and run `gh auth login`.")
    want = [p.strip() for p in a.reviewer.split(",") if p.strip()] if a.reviewer else []
    if a.auth == "subscription":
        avail = [p for p in ("claude", "codex") if shutil.which(p)]
    else:  # api: draw only from providers whose key is in the environment
        avail = [p for p, k in (("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY"))
                 if os.environ.get(k)]
    # OpenRouter reviewers (DeepSeek/MiniMax, driven by the `pi` agent) are pay-per-token, so they
    # are NEVER drawn by default — they join the pool ONLY when you name them in --reviewer (the
    # budget gate: no auto-dispatch), and then only if `pi` and OPENROUTER_API_KEY are present.
    if shutil.which("pi") and os.environ.get("OPENROUTER_API_KEY"):
        avail += [p for p in ("deepseek", "minimax") if p in want and p not in avail]
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
    run(["git", "clone", "-q", "--depth", "1", f"https://github.com/{a.roadmap_repo}",
         str(work / "roadmap")])
    shutil.rmtree(work / "roadmap" / ".git", ignore_errors=True)

    mathlib_args = []
    if not a.no_mathlib:
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
           "--ci-build", ci_build or "", "--auth", a.auth,
           "--providers", providers, "--daily-budget", "1000000", "--no-post",
           "--scoreboard-file", str(work / "scoreboard.md"),
           "--threads-dir", str(work / "threads"), "--post-plan-file", str(plan),
           "--replies-json", str(replies_path)]
    if a.rubrics:
        cmd += ["--rubrics", a.rubrics]
    print("\n=== running review (this calls claude/codex per rubric; takes a few minutes) ===\n",
          file=sys.stderr)
    run(cmd)

    sb = (work / "scoreboard.md")
    print("\n" + "=" * 72)
    print(sb.read_text() if sb.is_file() else "(no scoreboard produced)")
    threads = sorted((work / "threads").glob("*.md")) if (work / "threads").is_dir() else []
    for t in threads:
        print("\n" + "-" * 72 + f"\n[thread] {t.stem}\n")
        print(t.read_text())
    print("=" * 72 + "\n")

    if a.shadow:
        print(f"shadow arm `{a.label}` complete — archived, nothing posted.", file=sys.stderr)
    elif a.post:
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
    # records in <store>/outbox, and the next run (or `archive.py sync`) lands them.
    if outbox and pathlib.Path(outbox).is_dir():
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
