#!/usr/bin/env python3
"""Read-only published-review handoff decision, shared by the engine and its CLI probe."""
import argparse
import json
import os
import pathlib
import sys

_CONFIG_VALID = True
if __name__ == "__main__" and os.environ.get("TAUCETI_CONF"):
    try:
        from tauceti_tools.project import configuration_source, load
        # The CLI has already bootstrapped the project. Reuse its immutable
        # snapshot through the same loader; bootstrap() could fetch/write a new
        # snapshot if it were missing, which this read-only probe must not do.
        snapshot = pathlib.Path(os.environ["TAUCETI_CONF_SNAPSHOT"])
        if configuration_source() != os.environ["TAUCETI_CONF_SOURCE"] or not snapshot.is_file():
            raise ValueError("project snapshot unavailable")
        os.environ.update(load(str(snapshot)).environment())
    except Exception:
        _CONFIG_VALID = False

from ledger import Ledger
from verdict import is_unresolved, posts_review_thread, state_of


# Rubrics run in this order, and a `block` halts the round, so the block-capable integrity
# angles go first, fail-fast style: ordered by observed block rate over cost (ledger data —
# correctness and reuse block as often as scope but cost a third as much; attribution has
# not blocked yet). The non-blocking style angles follow in their README order.
DEFAULT_RUBRICS = ["correctness", "reuse", "scope", "attribution", "api-design",
                   "generality", "placement", "naming", "documentation", "proof-quality"]
DEFAULT_RUBRICS = json.loads(os.environ.get("TAUCETI_RUBRICS", "[]")) or DEFAULT_RUBRICS

def thread_action_rubrics(candidates, ran, state_map, head):
    """Rubrics whose thread state must be reconciled by this invocation.

    `ran` preserves the normal update/close behaviour for results produced now.  The other two
    predicates are the crash-recovery path: an adverse case file without a root is legacy or was
    interrupted before its first post, while `pending_thread_run_id` is the write-ahead marker set
    before every new adverse result.  Infrastructure errors remain excluded by
    posts_review_thread().  Candidate order keeps post plans deterministic.
    """
    ran = set(ran)
    out = []
    for rubric in candidates:
        cf = state_map.get(rubric) or {}
        adverse = posts_review_thread(state_of(cf, head))
        missing_root = not (cf.get("thread") or {}).get("comment_id")
        if rubric in ran or (adverse and (missing_root or cf.get("pending_thread_run_id"))):
            out.append(rubric)
    return out


def can_reuse_completed_review(board, head, candidates, replies, pr_state):
    """Published completion survives an empty worker store; local repairs still run."""
    if (not board or board.get("mode") == "init" or board.get("head_sha") != head
            or not candidates or not isinstance(replies, dict)):
        return False
    states = board.get("states") or {}
    current = [states.get(r) for r in candidates]
    allowed = {"green", "blocking_request", "blocking_block"}
    if any(s in ("blocking_request", "blocking_block") for s in current):
        allowed.add("stale")  # Approvals deferred while an adverse finding remains.
    if "blocking_block" in current:
        allowed.add("absent")  # A block halts the remaining rubric sweep.
    if not all(isinstance(s, str) and s in allowed for s in current):
        return False
    for rubric_replies in replies.values():
        for reply in rubric_replies:
            if not isinstance(reply.get("id"), int):
                return False
    # A completed review at the new head supersedes this worker's older case
    # files. Current-head findings and interrupted publication still need work.
    state_map = {r: cf for r, cf in (pr_state.get("state") or {}).items()
                 if cf.get("reviewed_sha") == head or cf.get("approved_sha") == head}
    if (pr_state.get("pending_publication_head_sha") == head
            or (state_map and pr_state.get("scoreboard_comment_id")
                and not pr_state.get("published_head_sha"))
            or any(is_unresolved(state_of(state_map.get(r), head)) for r in candidates)
            or thread_action_rubrics(candidates, [], state_map, head)):
        return False
    return True



def has_unhandled_contest(board, candidates, replies):
    through = board.get("replies_through")
    through = through if isinstance(through, int) else 0
    return any(reply["id"] > through for rubric in candidates for reply in replies.get(rubric, []))


def completed_before_preparation(board, head, candidates, replies, pr_state):
    # The late engine can carry an older approval to this head after reading
    # the diff. Without that input, a missing publication marker is ambiguous.
    if pr_state.get("scoreboard_comment_id") and not pr_state.get("published_head_sha"):
        return False
    return (can_reuse_completed_review(board, head, candidates, replies, pr_state)
            and not has_unhandled_contest(board, candidates, replies))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--replies", required=True)
    parser.add_argument("--rubrics", default=",".join(DEFAULT_RUBRICS))
    args = parser.parse_args()
    decision = "continue"
    try:
        if not _CONFIG_VALID:
            raise ValueError("project snapshot unavailable")
        path = pathlib.Path(args.store) / "ledger.json"
        # exists() can hide a permission error. A genuinely absent ledger is
        # valid; any other stat/load error declines this optional optimization.
        try:
            path.stat()
        except FileNotFoundError:
            pass
        ledger = Ledger(path).data
        pr_state = ledger["prs"].get(str(args.pr), {})
        board = json.loads(pathlib.Path(args.board).read_text())
        replies = json.loads(pathlib.Path(args.replies).read_text())
        candidates = [r.strip() for r in args.rubrics.split(",") if r.strip()]
        if completed_before_preparation(board, args.head_sha, candidates, replies, pr_state):
            decision = "unchanged"
    except Exception:
        # No inference, network, writes or recovery policy lives in this probe.
        # The ordinary engine retains its existing treatment of bad local state.
        pass
    print(json.dumps({"version": 1, "repo": args.repo, "pr": args.pr,
                      "head_sha": args.head_sha, "decision": decision}))


if __name__ == "__main__":
    main()
