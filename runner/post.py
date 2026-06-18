#!/usr/bin/env python3
"""Trusted-phase poster for the Tau Ceti review runner.

Runs AFTER the tokenless reviewer phase, with a scoped GitHub App token in `$GH_TOKEN`. It reads
the post plan the runner wrote and:

  * upserts the in-place **scoreboard** issue comment (edit if we know its id, else create and
    record it), and
  * upserts the per-rubric **review threads** for blocking rubrics (edit the thread root in place
    if it exists, else create a file-level review comment and record its id),

then writes the comment ids back into the store ledger so the next round edits in place. It runs
no model and trusts only the structured plan plus the runner's rendered bodies (which are the
review output we intend to publish anyway); a prompt-injected reviewer never reaches this step's
token.

Every API action's outcome is tracked: only CONFIRMED comment ids reach the ledger and the
archive sidecar (records/posts/), failures are recorded explicitly, and a failed scoreboard
upsert exits nonzero — a review that silently never landed on the PR is an error, not a success.
"""
import argparse, datetime, hashlib, json, os, pathlib, re, subprocess, sys

import archive

REPLY_MARKER_RE = re.compile(r"<!--tauceti-reply:([a-z][a-z-]*):through:(\d+)-->")


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
    """Trusted scoreboard comments on the PR as {id, login, ...}, newest first.

    So a review run whose local store does not know the scoreboard's comment id (the PR was last
    scored by CI or another machine) can edit the existing comment in place instead of posting a
    duplicate. Trust mirrors the consumer side: the scoreboard marker AND a repo-associated author
    (or the review bot) — a forged scoreboard from an untrusted commenter is ignored. `@json` forces
    one compact object per line so parsing is robust. Best-effort: returns [] on any API error."""
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
    account's comment; we must not). If the only scoreboard present belongs to someone else, we post
    our own and let the consumer's newest-wins read pick it. A failed edit of our own comment is a
    real error, never silently re-posted. Returns (sb_id, ok). `mine` overrides the actor login."""
    sb_id = pr_state.get("scoreboard_comment_id") or plan_sb_id
    mine_dupes = []                      # older scoreboards WE authored, to collapse
    if not sb_id:
        me = mine if mine is not None else current_login()
        ours = [c for c in find_scoreboard_comments(repo, pr) if c.get("login") == me]
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
    head = plan.get("head_sha", "")
    ledger_path = pathlib.Path(a.store) / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    pr_state = ledger["prs"].setdefault(str(a.pr), {})
    pr_state.setdefault("state", {})
    failures, posted_threads = [], {}
    scoreboard_ok = False

    # 1) Scoreboard: one comment per PR, edited in place (discovered from GitHub if our store does
    #    not know its id), with older duplicates collapsed.
    sb_id, scoreboard_ok = upsert_scoreboard(
        a.repo, a.pr, plan["scoreboard_body"], plan.get("scoreboard_comment_id"), pr_state, failures)

    # 2) Per-rubric threads (only rubrics that ran this round appear in the plan).
    me = current_login()
    for t in plan.get("threads", []):
        rubric = t["rubric"]
        cf = pr_state["state"].setdefault(rubric, {})
        if t["action"] == "reply":
            # A DIRECT reply under the thread root answering the author's contest. The root already
            # exists (a contest implies a prior thread). Deduped by marker so the same contest is
            # never answered twice across re-runs.
            parent = t.get("in_reply_to") or (cf.get("thread") or {}).get("comment_id")
            if not parent or already_replied(a.repo, a.pr, rubric, t.get("reply_dedupe"), me):
                continue
            resp = gh_api("POST", f"/repos/{a.repo}/pulls/{a.pr}/comments",
                          fields={"in_reply_to": parent}, body_file=t["body"],
                          failures=failures, action=f"thread reply {rubric}")
            if resp and resp.get("id"):
                posted_threads[f"{rubric}:reply"] = resp["id"]
            continue
        cid = (cf.get("thread") or {}).get("comment_id") or t.get("comment_id")
        if cid:  # edit the existing thread root (blocking update, or 'now passing' note)
            if gh_api("PATCH", f"/repos/{a.repo}/pulls/comments/{cid}", body_file=t["body"],
                      failures=failures, action=f"thread PATCH {rubric}") is not None:
                posted_threads[rubric] = cid
                if t["action"] == "close":  # finding cleared: collapse the thread
                    resolve_thread(a.repo, a.pr, cid)
        elif t["action"] == "upsert":  # first time blocking: open a file-level review thread
            resp = gh_api("POST", f"/repos/{a.repo}/pulls/{a.pr}/comments",
                          fields={"commit_id": head, "path": t["path"], "subject_type": "file"},
                          body_file=t["body"], failures=failures,
                          action=f"thread POST {rubric}")
            if resp and resp.get("id"):
                cf["thread"] = {"comment_id": resp["id"], "node_id": resp.get("node_id"),
                                "path": t["path"]}
                posted_threads[rubric] = resp["id"]
            else:
                print(f"post.py: thread create failed for {rubric}", file=sys.stderr)
        # action == "close" with no recorded thread: nothing was ever posted, so nothing to do.

    ledger_path.write_text(json.dumps(ledger, indent=2))

    # Archive what CONFIRMED landed — never an id we did not get back from the API.
    if a.archive_dir:
        sb_body = pathlib.Path(plan["scoreboard_body"]).read_text()
        rec = {"schema": "tauceti.post/v1", "repo": a.repo, "pr": int(a.pr),
               "round": plan.get("round"), "head_sha": head or None,
               "posted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "scoreboard_comment_id": sb_id if scoreboard_ok else None,
               "scoreboard_body_sha256": hashlib.sha256(sb_body.encode()).hexdigest(),
               "threads": posted_threads or None, "failures": failures or None}
        try:
            archive.archive_post(a.archive_dir, {k: v for k, v in rec.items() if v is not None})
        except Exception as e:
            print(f"WARNING: post archive write failed: {e}", file=sys.stderr)

    print(f"post.py: scoreboard id={pr_state.get('scoreboard_comment_id')}; "
          f"{len(plan.get('threads', []))} thread action(s); "
          f"{len(failures)} failure(s); ledger updated.")
    if not scoreboard_ok:
        sys.exit(1)  # the review never reached the PR: that is a failed post, not a quiet one


if __name__ == "__main__":
    main()
