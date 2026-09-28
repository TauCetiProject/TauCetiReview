#!/usr/bin/env python3
"""A reviewer taking over a PR recycles another reviewer's approvals as stale (♻️).

Each reviewer keeps its own store, so before this a reviewer picking up a PR someone else had been
reviewing had no case files for it and re-ran every rubric. The CLI now hands the engine the PR's
newest completed scoreboard, and commit/reply mode seeds its approvals as stale into rubrics the store
has no verdict for: they are deferred while other rubrics block, and re-run before a green verdict.
Dependency-free: run with `python tests/test_seed_approvals.py`.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "runner"))
sys.path.insert(0, str(HERE))
import casefile  # noqa: E402
import cli  # noqa: E402
import render  # noqa: E402
import test_billing as tb  # noqa: E402
from verdict import state_of  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    fails += not ok


def board_comment(cid, login, head, states, ts, mode="commit"):
    meta = json.dumps({"kind": "scoreboard", "head_sha": head, "mode": mode, "states": states})
    return {"id": cid, "user": {"login": login}, "updated_at": ts,
            "body": f"<!--tauceti-scoreboard-->\n## AI review\n\n<!--tauceti-meta:v1 {meta}-->"}


print("latest_scoreboard")
H1, H2, H3 = "1" * 40, "2" * 40, "3" * 40
comments = [
    board_comment(1, "alice", H1, {"reuse": "green"}, "2026-09-01T00:00:00Z"),
    board_comment(2, "bob", H2, {"reuse": "stale"}, "2026-09-02T00:00:00Z"),
    board_comment(3, "carol", H3, {}, "2026-09-03T00:00:00Z", mode="init"),
    board_comment(5, "eve", 1, {"reuse": "green"}, "2026-09-05T00:00:00Z"),
    {"id": 4, "user": {"login": "dave"}, "updated_at": "2026-09-04T00:00:00Z", "body": "lgtm"},
]
check("newest completed board wins, init and plain comments skipped", cli.latest_scoreboard(comments),
      {"comment_id": 2, "by": "bob", "head_sha": H2, "states": {"reuse": "stale"}})
check("no board", cli.latest_scoreboard(comments[3:]), None)

print("seed_stale_approvals")
HEAD = tb.HEAD
board = {"comment_id": 7, "by": "alice", "head_sha": HEAD,
         "states": {"a": "green", "b": "stale", "c": "blocking_request", "d": "green", "e": "green"}}
sm = {"d": {"verdict": "request_changes", "reviewed_sha": "old"},
      "e": {"author_replies": [{"id": 1, "body": "hm"}]}}
check("seeds approvals only where this store has no verdict",
      casefile.seed_stale_approvals(sm, board, ["a", "b", "c", "d", "e", "f"]), ["a", "b", "e"])
check("seeded at the board's own head is still stale", state_of(sm["a"], HEAD), "stale")
check("never overwrites this store's verdict", sm["d"]["verdict"], "request_changes")
check("keeps replies already folded in", sm["e"]["author_replies"], [{"id": 1, "body": "hm"}])
check("blocker on the board is not seeded", "c" in sm, False)
check("carry_forward never promotes a seeded approval",
      casefile.carry_forward(sm, HEAD, casefile.patch_digest("diff --git a/x b/x\n+x\n")), [])
check("idempotent", casefile.seed_stale_approvals(sm, board, ["a", "b"]), [])
leftover = {"a": {"approved_digest": "D", "approved_rubrics_version": "V", "carried_from_sha": "x"}}
casefile.seed_stale_approvals(leftover, board, ["a"])
check("leftover carry metadata cannot promote a seeded approval",
      casefile.carry_forward(leftover, HEAD, "D", "V"), [])
forged = {}
casefile.seed_stale_approvals(forged, {"by": "x|y", "head_sha": 1, "comment_id": "7",
                                       "states": {"a": "green"}}, ["a"])
check("malformed origin fields are dropped", forged["a"]["imported_from"], {})
check("board without states seeds nothing", casefile.seed_stale_approvals({}, {"by": "x"}, ["a"]), [])
row = [ln for ln in render.render_scoreboard(["a"], sm, "new", "x", "").splitlines() if "| a |" in ln]
check("scoreboard names the origin", "stale (re-run pending) (approved in @alice's review of `aaaaaaa`)"
      in row[0], True)
casefile.update_case_file(sm, "a", {"verdict_obj": {"verdict": "approve"}}, "new")
check("a fresh run drops the provenance", "imported_from" in sm["a"], False)

print("engine: commit mode defers seeded approvals while a rubric blocks")
tb._install_stubs()
rubrics = ["correctness", "reuse"]


def run_seeded(mode, verdicts):
    tb._VERDICTS.clear(); tb._VERDICTS.update(verdicts)
    d, rd, store = tb._workspace(rubrics)
    prior = d / "prior.json"
    prior.write_text(json.dumps({"comment_id": 9, "by": "alice", "head_sha": "b" * 40,
                                 "states": {"correctness": "blocking_request", "reuse": "green"}}))
    led = tb._run(store, rd, d / "diff.txt", mode=mode, rubrics=rubrics,
                  extra=["--prior-scoreboard-json", str(prior)])
    return led["prs"]["1"]["rounds"][-1]


r = run_seeded("commit", {"correctness": "request_changes"})
check("only the unjudged rubric ran", r["ran"], ["correctness"])
check("seeded approval shown stale", r["states"]["reuse"], "stale")
r = run_seeded("commit", {"correctness": "approve", "reuse": "approve"})
check("sweep re-runs the seeded approval once clean", r["ran"], ["correctness", "reuse"])
check("all green only after the re-run", r["states"], {"correctness": "green", "reuse": "green"})
r = run_seeded("manual", {"correctness": "request_changes"})
check("manual mode ignores the board", r["ran"], ["correctness", "reuse"])

sys.exit(1 if fails else 0)
