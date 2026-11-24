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
import verdict  # noqa: E402
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
same_second = [
    board_comment(11, "alice", H1, {"reuse": "green"}, "2026-09-06T00:00:00Z"),
    board_comment(12, "bob", H2, {"reuse": "blocking_request"}, "2026-09-06T00:00:00Z"),
]
for name, ordered in (("in id order", same_second), ("in reverse order", same_second[::-1])):
    check(f"a same-second tie goes to the larger comment id, {name}",
          cli.latest_scoreboard(ordered)["comment_id"], 12)

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
MB = "m" * 40
same = {"comment_id": 8, "by": "alice", "head_sha": HEAD, "merge_base_sha": MB, "rubrics_version": "rv1",
        "states": {"a": "green", "b": "stale", "c": "green", "d": "green", "e": "blocking_request"}}
cur = {"c": {"verdict": "approve", "reviewed_sha": "old", "approved_sha": "old"},
       "d": {"verdict": "request_changes", "reviewed_sha": "old"}}
check("same-head board: greens and stale rows seed",
      casefile.seed_stale_approvals(cur, same, ["a", "b", "c", "d", "e"], HEAD, MB), ["a", "b", "c"])
check("a green judged on this exact input is current, not re-run", state_of(cur["a"], HEAD), "green")
check("a stale row on the same-head board stays stale", state_of(cur["b"], HEAD), "stale")
check("replaces this store's stale approval from an earlier head", state_of(cur["c"], HEAD), "green")
check("keeps this store's blocking verdict", cur["d"]["verdict"], "request_changes")
check("a current seeded approval still never carries to a new head",
      casefile.carry_forward(cur, "n" * 40, casefile.patch_digest("diff --git a/x b/x\n+x\n")), [])
for name, args in (("another head", ("f" * 40, MB)), ("another merge base", (HEAD, "z" * 40)),
                   ("unknown merge base", (HEAD, ""))):
    other = {}
    casefile.seed_stale_approvals(other, same, ["a"], *args)
    check(f"{name}: the green stays stale", state_of(other["a"], args[0]), "stale")
edited = {}
casefile.seed_stale_approvals(edited, {**same, "rubrics_version": "rv2"}, ["a"], HEAD, MB)
check("a rubric edit does not reopen a same-head approval", state_of(edited["a"], HEAD), "green")
errored = {"a": {"verdict": "error", "reviewed_sha": HEAD, "error_kind": "timeout"}}
casefile.seed_stale_approvals(errored, same, ["a"], HEAD, MB)
check("a same-head green replaces this store's infrastructure error",
      (state_of(errored["a"], HEAD), "error_kind" in errored["a"]), ("green", False))
watched = {"a": {"author_replies": [{"id": 40, "body": "why"}]}}
casefile.seed_stale_approvals(watched, {**same, "replies_through": 50}, ["a"], HEAD, MB)
check("the board's reply watermark covers replies it adjudicated",
      verdict.has_new_contest(watched["a"]), False)
watched["a"]["author_replies"].append({"id": 60, "body": "still"})
check("a newer reply still reopens the rubric", verdict.has_new_contest(watched["a"]), True)
aborted = {"a": {"verdict": "error", "reviewed_sha": HEAD, "last_reply_seen": 60,
                 "author_replies": [{"id": 60, "body": "unjudged"}]}}
casefile.seed_stale_approvals(aborted, {**same, "replies_through": 50}, ["a"], HEAD, MB)
check("a reply an errored run never judged still reopens the imported approval",
      verdict.has_new_contest(aborted["a"]), True)
missing = {}
casefile.seed_stale_approvals(missing, {k: v for k, v in same.items() if k != "merge_base_sha"}, ["a"],
                              HEAD, MB)
check("a board without its merge base stays stale", state_of(missing["a"], HEAD), "stale")
mine = {"a": {"verdict": "approve", "reviewed_sha": HEAD, "approved_sha": HEAD, "summary": "mine"}}
check("this store's own approval at this head is left alone",
      (casefile.seed_stale_approvals(mine, same, ["a"], HEAD, MB), mine["a"]["summary"]), ([], "mine"))
check("latest_scoreboard passes the merge base through",
      cli.latest_scoreboard([{"id": 1, "user": {"login": "a"}, "updated_at": "t", "body":
                              "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(
                                  {"head_sha": H1, "states": {}, "merge_base_sha": MB}) + "-->"}]
                            )["merge_base_sha"], MB)

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


def run_same_head(verdicts, merge_base):
    tb._VERDICTS.clear(); tb._VERDICTS.update(verdicts)
    d, rd, store = tb._workspace(rubrics)
    prior = d / "prior.json"
    prior.write_text(json.dumps({"comment_id": 9, "by": "alice", "head_sha": HEAD,
                                 "merge_base_sha": "m" * 40,
                                 "rubrics_version": render.rubrics_fingerprint(rd),
                                 "states": {"correctness": "error", "reuse": "green"}}))
    led = tb._run(store, rd, d / "diff.txt", mode="commit", rubrics=rubrics,
                  extra=["--prior-scoreboard-json", str(prior), "--merge-base-sha", merge_base])
    return led["prs"]["1"]["rounds"][-1]


r = run_same_head({"correctness": "approve", "reuse": "approve"}, "m" * 40)
check("same-head takeover re-runs only the unfinished rubric", r["ran"], ["correctness"])
check("and posts green from the board's approval", r["states"], {"correctness": "green", "reuse": "green"})
r = run_same_head({"correctness": "approve", "reuse": "approve"}, "z" * 40)
check("a moved merge base re-runs the approval", r["ran"], ["correctness", "reuse"])
r = run_seeded("manual", {"correctness": "request_changes"})
check("manual mode ignores the board", r["ran"], ["correctness", "reuse"])

sys.exit(1 if fails else 0)
