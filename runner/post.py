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
"""
import argparse, json, os, pathlib, subprocess, sys


def gh_api(method, endpoint, fields=None, body_file=None):
    cmd = ["gh", "api", "-X", method, endpoint]
    if body_file:
        cmd += ["-F", f"body=@{body_file}"]  # @file -> read body from the rendered markdown
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    r = subprocess.run(cmd, text=True, capture_output=True)
    if r.returncode != 0:
        print(f"gh api {method} {endpoint} FAILED: {r.stderr[-600:]}", file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}


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

    # 1) Scoreboard: edit in place if we know its id, else create and remember it.
    sb_id = pr_state.get("scoreboard_comment_id") or plan.get("scoreboard_comment_id")
    if sb_id:
        gh_api("PATCH", f"/repos/{a.repo}/issues/comments/{sb_id}", body_file=plan["scoreboard_body"])
    else:
        resp = gh_api("POST", f"/repos/{a.repo}/issues/{a.pr}/comments",
                      body_file=plan["scoreboard_body"])
        if resp and resp.get("id"):
            pr_state["scoreboard_comment_id"] = resp["id"]
        else:
            print("post.py: scoreboard create failed", file=sys.stderr)

    # 2) Per-rubric threads (only rubrics that ran this round appear in the plan).
    for t in plan.get("threads", []):
        rubric = t["rubric"]
        cf = pr_state["state"].setdefault(rubric, {})
        cid = (cf.get("thread") or {}).get("comment_id") or t.get("comment_id")
        if cid:  # edit the existing thread root (blocking update, or 'now passing' note)
            gh_api("PATCH", f"/repos/{a.repo}/pulls/comments/{cid}", body_file=t["body"])
            if t["action"] == "close":  # finding cleared: collapse the thread
                resolve_thread(a.repo, a.pr, cid)
        elif t["action"] == "upsert":  # first time blocking: open a file-level review thread
            resp = gh_api("POST", f"/repos/{a.repo}/pulls/{a.pr}/comments",
                          fields={"commit_id": head, "path": t["path"], "subject_type": "file"},
                          body_file=t["body"])
            if resp and resp.get("id"):
                cf["thread"] = {"comment_id": resp["id"], "node_id": resp.get("node_id"),
                                "path": t["path"]}
            else:
                print(f"post.py: thread create failed for {rubric}", file=sys.stderr)
        # action == "close" with no recorded thread: nothing was ever posted, so nothing to do.

    ledger_path.write_text(json.dumps(ledger, indent=2))
    print(f"post.py: scoreboard id={pr_state.get('scoreboard_comment_id')}; "
          f"{len(plan.get('threads', []))} thread action(s); ledger updated.")


if __name__ == "__main__":
    main()
