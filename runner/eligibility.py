"""Publish the shared merge policy's result as immutable, App-owned GitHub checks.

Checks describe eligibility independently of MERGE_BACKEND. Bors owns admission
and drainage. A neutral check preserves approvals; an unsafe review revokes them.
"""
import json
import re

NAME = "merge eligibility"
APP_ID = 3947238
SCHEMA = "tauceti-merge.eligibility/v1"
KEEP = {"keep", "hold", "wip", "human", "do-not-close"}
SHA = re.compile(r"[0-9a-f]{40}\Z")


def metadata(repo, pr, merge_base, approve, single):
    return {"schema": SCHEMA, "repo": repo, "pr": pr,
            "merge_base_sha": merge_base or None,
            "eligible": approve is True, "review_safe": approve is not False,
            "single": bool(single)}


def record(check, repo, pr):
    if check.get("name") != NAME or (check.get("app") or {}).get("id") != APP_ID:
        return None
    try:
        data = json.loads(check.get("external_id") or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("repo") != repo or data.get("pr") != pr:
        return None
    return data


def publish(gh, log, repo, pr, head, approve, single=False, dry_run=False,
            merge_base=None, reason=""):
    if not SHA.fullmatch(head) or type(pr) is not int or pr <= 0:
        raise ValueError("eligibility requires an exact commit and PR number")
    if approve is True and not SHA.fullmatch(merge_base or ""):
        raise ValueError("eligible decisions require the reviewed merge base")
    live = gh(["api", f"repos/{repo}/pulls/{pr}"])
    if (live.get("state") != "open" or live.get("base", {}).get("ref") != "main"
            or live.get("head", {}).get("sha") != head):
        log(pr=pr, head_sha=head, reason="head_or_base_moved", published=False)
        return
    labels = {(x.get("name") or "").lower() for x in live.get("labels", [])}
    if approve is True and (live.get("draft") or bool(labels & KEEP)):
        approve = None
    if approve is True:
        cmp = gh(["api", f"repos/{repo}/compare/{live['base']['sha']}...{head}?per_page=1"])
        if cmp.get("merge_base_commit", {}).get("sha") != merge_base:
            log(pr=pr, head_sha=head, reason="merge_base_moved", published=False)
            return
    data = metadata(repo, pr, merge_base, approve, single)
    pages = gh(["api", "--paginate", "--slurp",
                f"repos/{repo}/commits/{head}/check-runs?check_name=merge%20eligibility&filter=all&per_page=100"])
    if (not isinstance(pages, list) or not pages
            or any(not isinstance(p, dict) or not isinstance(p.get("check_runs"), list) for p in pages)):
        raise RuntimeError("incomplete eligibility check listing")
    checks = [c for p in pages for c in p["check_runs"] if record(c, repo, pr) is not None]
    latest = max(checks, key=lambda c: c["id"], default=None)
    conclusion = "success" if approve is True else "failure" if approve is False else "neutral"
    if (latest and latest.get("status") == "completed"
            and latest.get("conclusion") == conclusion and record(latest, repo, pr) == data):
        return  # Reconciliation retries delivery by observation, never makes duplicate approvals.
    if dry_run:
        log(pr=pr, head_sha=head, dry_run=True, eligibility=data)
        return
    # Re-read after pagination: never publish an old-head decision on a changed PR.
    live = gh(["api", f"repos/{repo}/pulls/{pr}"])
    if (live.get("state") != "open" or live.get("base", {}).get("ref") != "main"
            or live.get("head", {}).get("sha") != head):
        return
    if approve is True:
        if live.get("draft") or any((l.get("name") or "").lower() in KEEP for l in live.get("labels", [])):
            return
        cmp = gh(["api", f"repos/{repo}/compare/{live['base']['sha']}...{head}?per_page=1"])
        if cmp.get("merge_base_commit", {}).get("sha") != merge_base:
            return
    result = gh(["api", "-X", "POST", f"repos/{repo}/check-runs", "--input", "-"], payload={
        "name": NAME, "head_sha": head, "status": "completed", "conclusion": conclusion,
        "external_id": json.dumps(data, sort_keys=True, separators=(",", ":")),
        "output": {"title": "Eligible for automatic merge" if approve is True else
                   "Review unsafe — approval withdrawn" if approve is False else "Waiting for eligibility",
                   "summary": reason or "Shared TauCeti merge policy; tied to this commit and reviewed diff."},
    })
    log(pr=pr, head_sha=head, eligibility=data, check_id=result.get("id"), published=True)
