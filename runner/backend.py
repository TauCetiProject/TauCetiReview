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

import api_budget
import eligibility

BORS_URL = "https://bors.taucetiproject.org/repositories/1/active-batches?base=main"
ACTIVE = {"waiting", "running"}


def gh_json(args, payload=None):
    try:
        r = api_budget.run(["gh", *args], text=True, capture_output=True, timeout=30,
                           **({"input": json.dumps(payload)} if payload is not None else {}))
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("GitHub observation/mutation timed out") from e
    if r.returncode:
        raise RuntimeError(f"GitHub observation/mutation failed: {r.stderr.strip()}")
    try:
        data = json.loads(r.stdout or "null")
    except json.JSONDecodeError as e:
        raise RuntimeError("GitHub returned invalid JSON") from e
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


def _complete_paths(pr):
    """The PR's changed paths when one GraphQL page holds all of them, else None."""
    try:
        files = pr["files"]
        if files["pageInfo"]["hasNextPage"] is not False or len(files["nodes"]) != pr["changedFiles"]:
            return None
        return [f["path"] for f in files["nodes"]]
    except (KeyError, TypeError):
        return None


def github_entries(repo, paths=False):
    """With `paths`, each entry also carries `paths`: the PR's changed paths, or None when one page
    of the `files` connection does not hold them all. Callers read those from REST instead."""
    owner, name = repo.split("/")
    files = " changedFiles files(first:100){pageInfo{hasNextPage} nodes{path}}" if paths else ""
    query = '''query($owner:String!,$name:String!,$cursor:String){
      repository(owner:$owner,name:$name){mergeQueue(branch:"main"){
        entries(first:100,after:$cursor){pageInfo{hasNextPage endCursor}
          nodes{enqueuedAt pullRequest{number id headRefOid''' + files + '''}}}}}}'''
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
                entry = {"number": pr["number"], "node_id": pr["id"],
                         "head_sha": pr["headRefOid"], "enqueued_at": node["enqueuedAt"]}
                if paths:
                    entry["paths"] = _complete_paths(pr)
                result.append(entry)
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
    except api_budget.Exhausted:
        raise
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


def publish_eligibility(repo, pr, head, approve, single=False, dry_run=False, merge_base=None, reason=""):
    """Deliver the existing policy result without a command comment.

    True = eligible, None = wait without withdrawing, False = unsafe review.
    Admission is enforced by bors against the live backend and outgoing queue.
    """
    return eligibility.publish(gh_json, log, repo, pr, head, approve, single,
                               dry_run, merge_base, reason)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("selected", "allow", "eligibility"))
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
        publish_eligibility(repo, int(os.environ["PR"]), os.environ["HEAD_SHA"], False,
                            reason=decision.get("reason", ""))
    else:
        with open("paths.z", "rb") as f:
            single = bool({b"lake-manifest.json", b"lean-toolchain"} & set(f.read().split(b"\0")))
        publish_eligibility(repo, int(os.environ["PR"]), os.environ["HEAD_SHA"],
                     True if decision.get("merge") is True else None, single,
                     merge_base=os.environ["MERGE_BASE"], reason=decision.get("reason", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
