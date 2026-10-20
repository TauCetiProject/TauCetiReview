"""Merge an eligible, reviewed head through GitHub without a queue or a rebase."""

import json
import os
from pathlib import Path

import backend

KEEP_LABELS = {"keep", "hold", "wip", "human", "do-not-close"}


def merge(repo, pr, decision, merge_base, *, dry_run=False):
    if decision.get("merge") is not True or decision.get("review_safe") is not True:
        return
    head = decision["head_sha"]
    # Re-read the selected mode and PR immediately before merging. Advancing main is
    # fine: the merge base pins the reviewed diff, not the current base-branch tip.
    if backend.selected(repo)["backend"] != "direct":
        return
    current = backend.gh_json(["api", f"repos/{repo}/pulls/{pr}"])
    if (current["state"] != "open" or current["draft"]
            or current["head"]["sha"] != head
            or current["base"]["ref"] != backend.BASE_BRANCH
            or {label["name"].lower() for label in current["labels"]} & KEEP_LABELS):
        return
    comparison = backend.gh_json([
        "api", f"repos/{repo}/compare/{current['base']['sha']}...{head}?per_page=1"])
    if not merge_base or comparison["merge_base_commit"]["sha"] != merge_base:
        return
    if dry_run:
        backend.log(backend="direct", pr=pr, head=head, dry_run=True)
        return
    try:
        result = backend.gh_json([
            "api", "--method", "PUT", f"repos/{repo}/pulls/{pr}/merge", "--input", "-"],
            {"sha": head, "merge_method": "squash"})
    except RuntimeError as error:
        # Head races, conflicts and GitHub's pending mergeability are retried by
        # the next event/sweep. Permission and branch-policy errors remain loud.
        message = str(error)
        if "HTTP 409" in message or ("HTTP 405" in message and "Pull Request is not mergeable" in message):
            backend.log(backend="direct", pr=pr, head=head, reason="retry_later", merged=False)
            return
        raise
    if result.get("merged") is not True:
        raise RuntimeError(f"GitHub refused direct merge of #{pr}: {result.get('message', 'unknown')}")
    backend.log(backend="direct", pr=pr, head=head, merged=True)


if __name__ == "__main__":
    merge(os.environ["REPO"], int(os.environ["PR"]),
          json.loads(Path("merge.json").read_text()), os.environ["MERGE_BASE"])
