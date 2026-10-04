"""Observe queue drainage immediately before admission. MERGE_BACKEND is the only switch.

An authenticated, successful variable listing with no setting means queue. Errors,
invalid values and incomplete observations mean defer. Ordinary PR CI is irrelevant.
"""
import argparse
import datetime
import json
import os
import subprocess
import urllib.request

BORS_URL = "https://bors.taucetiproject.org/repositories/1/active-batches?base=main"
ACTIVE = {"waiting", "running"}


def gh_json(args):
    r = subprocess.run(["gh", *args], text=True, capture_output=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"GitHub observation/mutation failed: {r.stderr.strip()}")
    data = json.loads(r.stdout or "null")
    if isinstance(data, dict) and data.get("errors"):
        raise RuntimeError("GitHub returned GraphQL errors")
    return data


def selected(repo):
    pages = gh_json(["api", "--paginate", "--slurp", f"repos/{repo}/actions/variables?per_page=100"])
    if not isinstance(pages, list) or not pages:
        raise RuntimeError("invalid variables response")
    variables = []
    for page in pages:
        if (not isinstance(page, dict) or not isinstance(page.get("variables"), list)
                or type(page.get("total_count")) is not int):
            raise RuntimeError("invalid variables page")
        variables.extend(page["variables"])
    if any(not isinstance(v, dict) or not isinstance(v.get("name"), str) or not isinstance(v.get("value"), str) for v in variables):
        raise RuntimeError("invalid variable entry")
    if len(variables) != pages[0]["total_count"]:
        raise RuntimeError("truncated variable listing")
    found = [v for v in variables if v.get("name") == "MERGE_BACKEND"]
    if not found:
        return {"backend": "queue", "updated_at": None}
    if len(found) != 1 or found[0].get("value") not in ("queue", "bors"):
        raise RuntimeError("invalid MERGE_BACKEND")
    return {"backend": found[0]["value"], "updated_at": found[0].get("updated_at")}


def github_entries(repo):
    owner, name = repo.split("/")
    query = '''query($owner:String!,$name:String!,$cursor:String){
      repository(owner:$owner,name:$name){mergeQueue(branch:"main"){
        entries(first:100,after:$cursor){pageInfo{hasNextPage endCursor}
          nodes{enqueuedAt pullRequest{number id headRefOid}}}}}}'''
    result, cursor, seen = [], None, set()
    while True:
        args = ["api", "graphql", "-f", "query=" + query,
                "-f", "owner=" + owner, "-f", "name=" + name]
        if cursor:
            args += ["-f", "cursor=" + cursor]
        data = gh_json(args)
        try:
            conn = data["data"]["repository"]["mergeQueue"]["entries"]
            nodes, page = conn["nodes"], conn["pageInfo"]
            if not isinstance(nodes, list) or type(page["hasNextPage"]) is not bool:
                raise ValueError()
            for node in nodes:
                pr = node["pullRequest"]
                result.append({"number": pr["number"], "node_id": pr["id"],
                               "head_sha": pr["headRefOid"], "enqueued_at": node["enqueuedAt"]})
            if not page["hasNextPage"]:
                return result
            cursor = page["endCursor"]
            if not cursor or cursor in seen:
                raise ValueError()
            seen.add(cursor)
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("incomplete merge queue response")


def bors_observation():
    req = urllib.request.Request(BORS_URL, headers={
        "Cache-Control": "no-cache", "Accept": "application/json",
        "User-Agent": "TauCetiReview/1.0",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    if (not isinstance(data, dict) or data.get("schema") != "tauceti-bors.observation/v1"
            or data.get("repo") != "TauCetiProject/TauCeti" or data.get("base") != "main"):
        raise RuntimeError("incomplete or incorrectly scoped bors observation")
    for key in ("batch_ids", "batches", "held", "outcomes"):
        if not isinstance(data.get(key), list):
            raise RuntimeError("incomplete bors observation: " + key)
    if data["batch_ids"] != [b["id"] for b in data["batches"]]:
        raise RuntimeError("inconsistent active bors batches")
    for b in data["batches"]:
        if b.get("base") != "main" or b.get("state") not in ACTIVE or not isinstance(b.get("members"), list):
            raise RuntimeError("invalid active bors batch")
    return data


def log(**fields):
    print(json.dumps({"schema": "tauceti-merge.admission/v1",
                      "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), **fields},
                     sort_keys=True))


def allow(repo, expected):
    try:
        setting = selected(repo)
        if setting["backend"] != expected:
            log(**setting, expected=expected, reason="backend_changed", admitted=False)
            return False
        other = bors_observation()["batch_ids"] if expected == "queue" else github_entries(repo)
        ok = not other
        log(**setting, expected=expected, outgoing_count=len(other), admitted=ok,
            reason="drained" if ok else "outgoing_not_drained")
        return ok
    except Exception as e:
        log(expected=expected, admitted=False, reason="observation_unavailable", error=str(e))
        return False


def bors_head_state(data, pr, head):
    active = any(m.get("pr") == pr and m.get("head_sha") == head
                 for b in data["batches"] for m in b["members"])
    held = any(m.get("pr") == pr and m.get("head_sha") == head for m in data["held"])
    if active or held:
        return "approved"
    outcome = next((o for o in data["outcomes"] if o.get("pr") == pr and o.get("head_sha") == head), None)
    if outcome and outcome.get("state") in ("error", "conflict", "ok"):
        return "terminal"
    # An old batch without a snapshot cannot be attributed to this head.
    if any(o.get("pr") == pr and not o.get("head_sha") for o in data["outcomes"]):
        return "legacy_unknown"
    return "absent"


def bors_command(repo, pr, head, approve, single=False, dry_run=False, merge_base=None):
    live = gh_json(["api", f"repos/{repo}/pulls/{pr}"])
    if live.get("state") != "open" or live.get("base", {}).get("ref") != "main" or (approve and live.get("head", {}).get("sha") != head):
        log(pr=pr, head_sha=head, reason="head_or_base_moved", admitted=False)
        return
    if approve:
        if live.get("draft") or not allow(repo, "bors"):
            return
        state = bors_head_state(bors_observation(), pr, head)
        if state != "absent":
            log(pr=pr, head_sha=head, reason="bors_" + state, admitted=False)
            return
        if merge_base:
            cmp = gh_json(["api", f"repos/{repo}/compare/{live['base']['sha']}...{head}?per_page=1"])
            if cmp.get("merge_base_commit", {}).get("sha") != merge_base:
                log(pr=pr, head_sha=head, reason="merge_base_moved", admitted=False)
                return
    else:
        head = live["head"]["sha"]
        approved = None
        try:
            data = bors_observation()
            approved = (any(m.get("pr") == pr for b in data["batches"] for m in b["members"])
                        or any(m.get("pr") == pr for m in data["held"]))
        except Exception:
            pass
        # Revoke even when the other queue is selected or observations fail.
        # A lost r- delivery is retried while real approval is still present.
        if approved is not True:
            pages = gh_json(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{pr}/comments?per_page=100"])
            commands = [c.get("body") for page in pages for c in page
                        if (c.get("body") or "").startswith("bors r")]
            if commands and commands[-1] == f"bors r- sha={head}":
                return
            if not commands and approved is False:
                return
    body = f"bors {'r+ single' if single else 'r+'} sha={head}" if approve else f"bors r- sha={head}"
    if dry_run:
        log(pr=pr, head_sha=head, dry_run=True, command=body)
        return
    # Read again after all potentially slow observations.
    if approve and not allow(repo, "bors"):
        return
    if approve and merge_base:
        live = gh_json(["api", f"repos/{repo}/pulls/{pr}"])
        if live.get("head", {}).get("sha") != head or live.get("base", {}).get("ref") != "main":
            return
        cmp = gh_json(["api", f"repos/{repo}/compare/{live['base']['sha']}...{head}?per_page=1"])
        if cmp.get("merge_base_commit", {}).get("sha") != merge_base:
            return
    gh_json(["api", "-X", "POST", f"repos/{repo}/issues/{pr}/comments", "-f", "body=" + body])
    log(pr=pr, head_sha=head, command=body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("selected", "allow", "bors"))
    ap.add_argument("--expected", choices=("queue", "bors"))
    ap.add_argument("--decision", default="merge.json")
    args = ap.parse_args()
    repo = os.environ["REPO"]
    if args.command == "allow":
        return 0 if allow(repo, args.expected) else 3
    if args.command == "selected":
        try:
            setting = selected(repo)
            log(**setting)
            mode = setting["backend"]
        except Exception as e:
            log(reason="observation_unavailable", error=str(e))
            mode = "unknown"
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("backend=" + mode + "\n")
        return 0
    with open(args.decision) as f:
        decision = json.load(f)
    if decision.get("review_safe") is not True:
        bors_command(repo, int(os.environ["PR"]), os.environ["HEAD_SHA"], False)
    elif decision.get("merge") is True:
        with open("paths.z", "rb") as f:
            single = bool({b"lake-manifest.json", b"lean-toolchain"} & set(f.read().split(b"\0")))
        bors_command(repo, int(os.environ["PR"]), os.environ["HEAD_SHA"], True, single,
                     merge_base=os.environ["MERGE_BASE"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
