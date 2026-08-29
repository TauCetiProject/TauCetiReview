#!/usr/bin/env python3
"""Trusted-phase poster for the Tau Ceti review runner.

Runs AFTER the tokenless reviewer phase, with a scoped GitHub App token in `$GH_TOKEN`. It reads
the post plan the runner wrote and:

  * publishes every required blocking **review thread** first, adopting an identical root left by
    a crash when necessary, then
  * upserts the in-place **scoreboard** issue comment as the publication commit marker, and finally
  * performs close notes and contest replies as best-effort UI cleanup.

then writes the comment ids back into the store ledger so the next round edits in place. It runs
no model and trusts only the structured plan plus the runner's rendered bodies (which are the
review output we intend to publish anyway); a prompt-injected reviewer never reaches this step's
token.

Every API action's outcome is tracked: only CONFIRMED comment ids reach the ledger and the archive
sidecar (records/posts/). A required thread failure withholds the scoreboard and exits nonzero; a
scoreboard failure leaves the write-ahead markers pending and also exits nonzero. Thus a visible
current-head scoreboard never names a blocker that lacks its contestable inline thread.
"""
import argparse, datetime, hashlib, json, os, pathlib, re, subprocess, sys

import archive

REPLY_MARKER_RE = re.compile(r"<!--tauceti-reply:([a-z][a-z-]*):through:(\d+)-->")
RUBRIC_MARKER_RE = re.compile(r"<!--tauceti-rubric:([a-z][a-z-]*)-->")
META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)


def atomic_write_json(path, value):
    """Replace a JSON file atomically, so a killed poster leaves either ledger version intact."""
    path = pathlib.Path(path)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2))
    os.replace(tmp, path)


def _comment_json_lines(stdout):
    """Decode `gh --jq '.[] | @json'` output (and tolerate an extra JSON string layer)."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
            if isinstance(value, str):
                value = json.loads(value)
            if isinstance(value, dict):
                yield value
        except Exception:
            continue


def find_review_roots(repo, pr):
    """Return GitHub's review-thread roots, including rubric/run provenance, or None on failure.

    This is the remote half of crash idempotence. A POST can succeed immediately before the process
    dies and before its id reaches the ledger; the next invocation recognizes its exact rubric,
    head, and run id and adopts it instead of creating a duplicate.
    """
    r = subprocess.run(
        ["gh", "api", "--paginate", "--jq", ".[] | @json",
         f"/repos/{repo}/pulls/{pr}/comments?per_page=100"],
        text=True, capture_output=True)
    if r.returncode != 0:
        print(f"review-root lookup failed: {r.stderr[-300:]}", file=sys.stderr)
        return None
    roots = []
    for c in _comment_json_lines(r.stdout):
        if c.get("in_reply_to_id") is not None:
            continue
        body = c.get("body") or ""
        rubric_match = RUBRIC_MARKER_RE.search(body)
        meta = {}
        meta_match = META_RE.search(body)
        if meta_match:
            try:
                meta = json.loads(meta_match.group(1))
            except Exception:
                meta = {}
        roots.append({
            "id": c.get("id"), "node_id": c.get("node_id"), "path": c.get("path"),
            "login": (c.get("user") or {}).get("login"),
            "rubric": rubric_match.group(1) if rubric_match else None,
            "head_sha": meta.get("head_sha") or c.get("commit_id"),
            "run_ids": [run.get("id") for run in (meta.get("runs") or []) if run.get("id")],
            "created_at": c.get("created_at") or "",
        })
    return roots


def _newest(items):
    return max(items, key=lambda c: (c.get("created_at") or "", c.get("id") or 0), default=None)


def publish_required_upsert(repo, pr, head, action, cf, roots, me, failures):
    """Land one required blocker root and update `cf`; return its confirmed id or None.

    Exact roots authored by this identity are adopted after a crash. Known roots authored by this
    identity are edited in place. A root owned by another identity is never PATCHed: this identity
    appends its own root on the current head instead.
    """
    rubric, run_id = action["rubric"], action.get("run_id")
    exact = _newest(c for c in roots
                    if c.get("login") == me and c.get("rubric") == rubric
                    and c.get("head_sha") == head and run_id in c.get("run_ids", []))
    if exact:
        chosen, response = exact, exact
    else:
        known_id = (cf.get("thread") or {}).get("comment_id") or action.get("comment_id")
        known = next((c for c in roots if c.get("id") == known_id), None) if known_id else None
        foreign = bool(known and known.get("login") != me)
        if known and not foreign:
            response = gh_api(
                "PATCH", f"/repos/{repo}/pulls/comments/{known_id}",
                body_file=action["body"], failures=failures,
                action=f"required thread PATCH {rubric}")
            chosen = known if response is not None else None
        else:
            # If the ledger lost its id, reuse our newest rubric root. A known foreign root is the
            # identity-migration case and deliberately forces a new current-head root instead.
            ours = None if foreign else _newest(
                c for c in roots if c.get("login") == me and c.get("rubric") == rubric)
            if ours:
                response = gh_api(
                    "PATCH", f"/repos/{repo}/pulls/comments/{ours['id']}",
                    body_file=action["body"], failures=failures,
                    action=f"required thread PATCH {rubric}")
                chosen = ours if response is not None else None
            else:
                response = gh_api(
                    "POST", f"/repos/{repo}/pulls/{pr}/comments",
                    fields={"commit_id": head, "path": action["path"],
                            "subject_type": "file"},
                    body_file=action["body"], failures=failures,
                    action=f"required thread POST {rubric}")
                chosen = ({"id": response.get("id"), "node_id": response.get("node_id"),
                           "path": action["path"]}
                          if response and response.get("id") else None)
    if not chosen or not chosen.get("id"):
        if not any(f.get("action", "").endswith(rubric) for f in failures):
            failures.append({"action": f"required thread upsert {rubric}",
                             "error": "GitHub response had no confirmed comment id"})
        print(f"post.py: required thread publication failed for {rubric}", file=sys.stderr)
        return None
    cf["thread"] = {
        **(cf.get("thread") or {}),
        "comment_id": chosen["id"],
        "node_id": chosen.get("node_id"),
        "path": chosen.get("path") or action.get("path"),
        "published_run_id": run_id,
    }
    return chosen["id"]


def already_replied(repo, pr, rubric, through_id, me):
    """True iff we already posted a contest answer for `rubric` covering `through_id` or newer.

    Durable, marker-based dedupe across machines (the engine's per-rubric `last_reply_seen` is the
    local fast path; this is the cross-machine authority). The scan-then-POST is not atomic, so two
    workers posting at the exact same instant could still double-reply — that is an accepted, rare,
    cosmetic duplicate (normal operation is a single worker), not a correctness problem: the
    expensive model-run dedupe is guaranteed by the case-file watermark, never this scan. On a fetch
    failure, return False (post — a missed answer is worse than a rare duplicate)."""
    if through_id is None:
        return False
    out = subprocess.run(["gh", "api", "--paginate", "--jq", ".[]",
                          f"/repos/{repo}/pulls/{pr}/comments?per_page=100"],
                         text=True, capture_output=True)
    if out.returncode != 0:
        return False
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except Exception:
            continue
        if (c.get("user") or {}).get("login") != me:
            continue
        m = REPLY_MARKER_RE.search(c.get("body") or "")
        if m and m.group(1) == rubric and int(m.group(2)) >= int(through_id):
            return True
    return False


def gh_api(method, endpoint, fields=None, body_file=None, failures=None, action=""):
    cmd = ["gh", "api", "-X", method, endpoint]
    if body_file:
        cmd += ["-F", f"body=@{body_file}"]  # @file -> read body from the rendered markdown
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    r = subprocess.run(cmd, text=True, capture_output=True)
    if r.returncode != 0:
        print(f"gh api {method} {endpoint} FAILED: {r.stderr[-600:]}", file=sys.stderr)
        if failures is not None:
            failures.append({"action": action or f"{method} {endpoint}",
                             "error": r.stderr[-300:]})
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}


SCOREBOARD_MARKER = "<!--tauceti-scoreboard-->"
TRUSTED_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}
REVIEW_BOT = "tauceti-review-bot[bot]"


def current_login():
    """Who this token acts as: the operator for a user token, or the review bot for an installation
    token (which cannot read /user). We only ever edit/delete comments authored by this login, so a
    write-scoped token never overwrites or removes a comment belonging to someone else."""
    r = subprocess.run(["gh", "api", "user", "--jq", ".login"], text=True, capture_output=True)
    login = (r.stdout or "").strip()
    return login if (r.returncode == 0 and login) else REVIEW_BOT


def find_scoreboard_comments(repo, pr):
    """Scoreboard comments eligible for cross-machine reuse as {id, login, ...}, newest first.

    So a review run whose local store does not know the scoreboard's comment id (the PR was last
    scored by CI or another machine) can edit the existing comment in place instead of posting a
    duplicate. Reuse is deliberately narrower than the merge consumer: discovery returns only
    scoreboards from a repo-associated author or the review bot, and `upsert_scoreboard` then selects
    only comments authored by the current login before editing or deleting. Consequently an outside
    contributor's own scoreboard is not discoverable on another machine and a later run may post a
    duplicate. The merge gate separately reads the newest marked scoreboard from any author. `@json`
    forces one compact object per line so parsing is robust. Best-effort: returns [] on any API
    error."""
    r = subprocess.run(
        ["gh", "api", "--paginate", f"/repos/{repo}/issues/{pr}/comments", "--jq",
         '.[] | select((.body // "") | contains("' + SCOREBOARD_MARKER + '")) '
         '| {id, login: (.user.login // ""), assoc: (.author_association // ""), '
         'updated_at: (.updated_at // "")} | @json'],
        text=True, capture_output=True)
    if r.returncode != 0:
        print(f"scoreboard lookup failed: {r.stderr[-300:]}", file=sys.stderr)
        return []
    out = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        try:
            c = json.loads(line)
        except Exception:
            continue
        if c.get("assoc") in TRUSTED_ASSOC or c.get("login") == REVIEW_BOT:
            out.append(c)
    out.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return out


def upsert_scoreboard(repo, pr, body_file, plan_sb_id, pr_state, failures, mine=None):
    """Publish the PR's single scoreboard comment, editing OUR existing one in place rather than
    duplicating, and collapsing OUR older duplicates.

    The comment to edit is our store/plan id, or — when the store does not know it (the PR was last
    scored by CI or another machine) — the newest scoreboard WE authored, discovered on GitHub. We
    only ever PATCH/DELETE our own comments (a write-scoped token could technically remove another
    account's comment; we must not). A known scoreboard id owned by another identity is never
    PATCHed: this identity posts a fresh `kind:scoreboard` instead (cross-identity PATCH 404s;
    see TauCetiReview#98 / the same guard as `publish_required_upsert`). If the only scoreboard
    present belongs to someone else, we likewise post our own and let the consumer's newest-wins
    read pick it. A failed edit of our own comment is a real error, never silently re-posted.
    Returns (sb_id, ok). `mine` overrides the actor login."""
    me = mine if mine is not None else current_login()
    existing = find_scoreboard_comments(repo, pr)
    sb_id = pr_state.get("scoreboard_comment_id") or plan_sb_id
    mine_dupes = []                      # older scoreboards WE authored, to collapse
    if sb_id:
        known = next((c for c in existing if c.get("id") == sb_id), None)
        # A known foreign scoreboard is the identity-migration case: never PATCH it.
        if known and known.get("login") != me:
            sb_id = None
    else:
        ours = [c for c in existing if c.get("login") == me]
        if ours:
            sb_id = ours[0]["id"]
            mine_dupes = [c["id"] for c in ours[1:]]
    ok = False
    if sb_id:
        if gh_api("PATCH", f"/repos/{repo}/issues/comments/{sb_id}", body_file=body_file,
                  failures=failures, action="scoreboard PATCH") is not None:
            pr_state["scoreboard_comment_id"] = sb_id
            ok = True
    else:
        resp = gh_api("POST", f"/repos/{repo}/issues/{pr}/comments",
                      body_file=body_file, failures=failures, action="scoreboard POST")
        if resp and resp.get("id"):
            pr_state["scoreboard_comment_id"] = sb_id = resp["id"]
            ok = True
        else:
            print("post.py: scoreboard create failed", file=sys.stderr)
            sb_id = None
    for dup in mine_dupes:               # collapse our own older duplicates (best-effort)
        if dup and dup != sb_id:
            gh_api("DELETE", f"/repos/{repo}/issues/comments/{dup}",
                   action=f"scoreboard collapse {dup}")
    return sb_id, ok


def resolve_thread(repo, pr, comment_id):
    """Resolve (collapse) the review thread whose root comment is `comment_id`, so a finding the
    author has cleared stops cluttering the conversation. Best-effort; failures are logged."""
    owner, name = repo.split("/")
    q = ("query($owner:String!,$name:String!,$pr:Int!){repository(owner:$owner,name:$name){"
         "pullRequest(number:$pr){reviewThreads(first:100){nodes{id isResolved "
         "comments(first:1){nodes{databaseId}}}}}}}")
    r = subprocess.run(["gh", "api", "graphql", "-f", f"query={q}", "-F", f"owner={owner}",
                        "-F", f"name={name}", "-F", f"pr={int(pr)}"], text=True, capture_output=True)
    if r.returncode != 0:
        print(f"resolve query failed: {r.stderr[-300:]}", file=sys.stderr)
        return
    try:
        nodes = json.loads(r.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except Exception:
        return
    tid = next((t["id"] for t in nodes if not t["isResolved"]
                and t["comments"]["nodes"] and t["comments"]["nodes"][0]["databaseId"] == comment_id),
               None)
    if not tid:
        return
    mut = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
    subprocess.run(["gh", "api", "graphql", "-f", f"query={mut}", "-F", f"id={tid}"],
                   text=True, capture_output=True)


def archive_outcome(archive_dir, repo, pr, plan, sb_id, scoreboard_ok, posted_threads, failures):
    """Archive only confirmed remote ids, including partial/failed publication attempts."""
    if not archive_dir:
        return
    sb_body = pathlib.Path(plan["scoreboard_body"]).read_text()
    rec = {"schema": "tauceti.post/v1", "repo": repo, "pr": int(pr),
           "round": plan.get("round"), "head_sha": plan.get("head_sha") or None,
           "posted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "scoreboard_comment_id": sb_id if scoreboard_ok else None,
           "scoreboard_body_sha256": hashlib.sha256(sb_body.encode()).hexdigest(),
           "threads": posted_threads or None, "failures": failures or None}
    try:
        archive.archive_post(archive_dir, {k: v for k, v in rec.items() if v is not None})
    except Exception as e:
        print(f"WARNING: post archive write failed: {e}", file=sys.stderr)


def execute_post(repo, pr, plan, ledger_path, archive_dir=""):
    """Execute one trusted plan. Return a process-style status code (0 success, 1 incomplete)."""
    head = plan.get("head_sha", "")
    ledger_path = pathlib.Path(ledger_path)
    ledger = json.loads(ledger_path.read_text())
    pr_state = ledger["prs"].setdefault(str(pr), {})
    pr_state.setdefault("state", {})
    failures, posted_threads = [], {}
    sb_id, scoreboard_ok = None, False
    actions = plan.get("threads", [])
    required = [t for t in actions
                if t.get("required", t.get("action") == "upsert")]
    optional = [t for t in actions if t not in required]
    me = current_login()

    # Phase 1: every genuine blocker root is required. Remote discovery is itself required because
    # it proves ownership before PATCH and makes a POST-crash retry idempotent.
    roots = find_review_roots(repo, pr)
    if required and roots is None:
        failures.append({"action": "required thread discovery",
                         "error": "could not list review roots"})
    else:
        for t in required:
            rubric = t["rubric"]
            cf = pr_state["state"].setdefault(rubric, {})
            cid = publish_required_upsert(repo, pr, head, t, cf, roots, me, failures)
            if cid:
                posted_threads[rubric] = cid
                # Persist the confirmed root immediately. Keep its pending marker until the
                # scoreboard succeeds; a crash or scoreboard failure must schedule another repair.
                atomic_write_json(ledger_path, ledger)

    required_failed = any(t["rubric"] not in posted_threads for t in required)
    if required_failed:
        archive_outcome(archive_dir, repo, pr, plan, None, False, posted_threads, failures)
        print(f"post.py: withheld scoreboard; {len(required)} required thread(s), "
              f"{len(failures)} failure(s); confirmed ids saved.")
        return 1

    # Phase 2: the current-head scoreboard is the publication commit marker. Only after it lands do
    # we clear the PR/thread write-ahead records.
    sb_id, scoreboard_ok = upsert_scoreboard(
        repo, pr, plan["scoreboard_body"], plan.get("scoreboard_comment_id"), pr_state, failures,
        mine=me)
    atomic_write_json(ledger_path, ledger)
    if not scoreboard_ok:
        archive_outcome(archive_dir, repo, pr, plan, sb_id, False, posted_threads, failures)
        print(f"post.py: scoreboard failed after {len(required)} required thread(s); "
              f"{len(failures)} failure(s); publication remains pending.")
        return 1

    pr_state["published_head_sha"] = head
    if pr_state.get("pending_publication_head_sha") == head:
        pr_state.pop("pending_publication_head_sha", None)
    for t in required:
        cf = pr_state["state"].setdefault(t["rubric"], {})
        if (not t.get("run_id")
                or cf.get("pending_thread_run_id") == t.get("run_id")):
            cf.pop("pending_thread_run_id", None)
    atomic_write_json(ledger_path, ledger)

    # Phase 3: closes and direct contest answers improve the UI but do not affect whether the
    # review verdict was published. Optional failures are archived and reported, never rolled back.
    roots_by_id = {c.get("id"): c for c in (roots or []) if c.get("id")}
    for t in optional:
        rubric = t["rubric"]
        cf = pr_state["state"].setdefault(rubric, {})
        if t["action"] == "reply":
            parent = t.get("in_reply_to") or (cf.get("thread") or {}).get("comment_id")
            if not parent or already_replied(repo, pr, rubric, t.get("reply_dedupe"), me):
                continue
            resp = gh_api("POST", f"/repos/{repo}/pulls/{pr}/comments/{parent}/replies",
                          body_file=t["body"], failures=failures,
                          action=f"thread reply {rubric}")
            if resp and resp.get("id"):
                posted_threads[f"{rubric}:reply"] = resp["id"]
            continue
        if t["action"] != "close":
            continue
        cid = (cf.get("thread") or {}).get("comment_id") or t.get("comment_id")
        root = roots_by_id.get(cid)
        if not cid or not root or root.get("login") != me:
            continue  # nothing of ours to edit; never cross-edit another identity's thread
        if gh_api("PATCH", f"/repos/{repo}/pulls/comments/{cid}", body_file=t["body"],
                  failures=failures, action=f"thread close {rubric}") is not None:
            posted_threads[rubric] = cid
            resolve_thread(repo, pr, cid)

    atomic_write_json(ledger_path, ledger)
    archive_outcome(archive_dir, repo, pr, plan, sb_id, True, posted_threads, failures)
    print(f"post.py: scoreboard id={pr_state.get('scoreboard_comment_id')}; "
          f"{len(actions)} thread action(s); {len(failures)} failure(s); ledger updated.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--archive-dir", default="",
                    help="outbox directory: record which comment ids actually landed "
                         "(records/posts/) for the durable archive")
    a = ap.parse_args()
    if not os.environ.get("GH_TOKEN"):
        print("post.py: GH_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    plan = json.loads(pathlib.Path(a.plan).read_text())
    status = execute_post(a.repo, a.pr, plan, pathlib.Path(a.store) / "ledger.json",
                          a.archive_dir)
    if status:
        sys.exit(status)


if __name__ == "__main__":
    main()
