#!/usr/bin/env python3
"""Characterization test for the run_one billing/persistence loop in review.py.

The CI suite covers pricing, posting, and coordination, but nothing exercised the per-rubric
spend loop: cost accumulation, the per-call daily-budget reservation, the block halt, incremental
ledger persistence, and the appended round record. This pins that observable behaviour (with the
reviewer subprocesses stubbed, so no spend and no network) so the run_one -> run_rubric/Ledger
refactor can be verified behaviour-preserving.

Run: python3 tests/test_billing.py   (exit 0 = pass, 1 = fail)
"""
import contextlib
import io
import json
import pathlib
import re
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "runner"))
import review  # noqa: E402  (runner/ on path; same import the engine uses)

HEAD = "a" * 40
COST = 0.01
_VERDICTS = {}   # rubric -> "approve" | "request_changes" | "block"; set per scenario


def _fake_runner(prompt, cwd, model, env):
    """Stand in for run_claude/run_codex/run_pi: find which rubric this prompt is for (each rubric
    file carries an ANGLE:<name> sentinel), echo the one-time marker, and emit the desired verdict."""
    marker = re.search(r"TAUCETI-VERDICT-\w+", prompt).group(0)
    rubric = re.search(r"ANGLE:(\S+)", prompt).group(1)
    verdict = _VERDICTS.get(rubric, "approve")
    vo = {"verdict": verdict, "summary": f"{rubric} says {verdict}",
          "findings": ([{"file": f"code/{rubric}.lean", "what": "x"}]
                       if verdict != "approve" else [])}
    text = f"analysis for {rubric}\n{marker}\n{json.dumps(vo)}"
    return {"returncode": 0, "text": text, "cost_usd": COST,
            "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50}}


def _install_stubs():
    review.run_claude = _fake_runner
    review.run_codex = _fake_runner
    review.run_pi = _fake_runner
    review.reviewer_env = lambda provider, keys, subscription=False: ({}, None)
    review.cleanup_rev_home = lambda home: None
    review.sweep_rev_homes = lambda *a, **k: None


def _workspace(rubrics):
    d = pathlib.Path(tempfile.mkdtemp(prefix="tauceti-billing-"))
    rd = d / "rubrics"; rd.mkdir()
    (rd / "_common.md").write_text("common rubric preamble\n")
    for r in rubrics:
        (rd / f"{r}.md").write_text(f"ANGLE:{r}\nreview angle for {r}\n")
    (d / "diff.txt").write_text("diff --git a/code/x.lean b/code/x.lean\n+x\n")
    (d / "code").mkdir()
    store = d / "store"; store.mkdir()
    return d, rd, store


def _seed(store, state, rounds=None, **pr_fields):
    pr = {"rounds": rounds or [], "state": state, "scoreboard_comment_id": 500, **pr_fields}
    (store / "ledger.json").write_text(json.dumps({"days": {}, "prs": {"1": pr}}, indent=2))


def _run(store, rd, diff, *, mode, rubrics, daily_budget=5.0, max_call_cost=1.0, extra=None):
    argv = ["review", "--pr", "1", "--repo", "Owner/Repo", "--rubrics", ",".join(rubrics),
            "--rubrics-dir", str(rd), "--tool-cwd", str(store.parent / "code"),
            "--diff-file", str(diff), "--store", str(store), "--head-sha", HEAD,
            "--mode", mode, "--auth", "subscription", "--no-post", "--providers", "claude",
            "--daily-budget", str(daily_budget), "--max-call-cost", str(max_call_cost)]
    if extra:
        argv += extra
    old = sys.argv
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            review.main()
    finally:
        sys.argv = old
    return json.loads((store / "ledger.json").read_text())


fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        fails += 1


def today():
    return review.today()


def main():
    _install_stubs()

    # --- A: manual, both approve → both run, all green, spend = 2*COST, one round ---
    print("A. manual, all approve")
    _VERDICTS.clear(); _VERDICTS.update(correctness="approve", reuse="approve")
    d, rd, store = _workspace(["correctness", "reuse"])
    led = _run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness", "reuse"])
    pr = led["prs"]["1"]
    check("spend = 2*COST", round(led["days"][today()], 6), round(2 * COST, 6))
    check("one round recorded", len(pr["rounds"]), 1)
    check("round ran both", pr["rounds"][0]["ran"], ["correctness", "reuse"])
    check("round cost", round(pr["rounds"][0]["cost"], 6), round(2 * COST, 6))
    check("not halted", pr["rounds"][0]["halted_at"], None)
    check("both green", pr["rounds"][0]["states"], {"correctness": "green", "reuse": "green"})
    check("case file verdict", pr["state"]["correctness"]["verdict"], "approve")
    check("scoreboard written", (store / "reviews/1/1/scoreboard.md").exists(), True)

    # --- B: commit, correctness blocks → halt after it; reuse never runs; spend = 1*COST ---
    print("B. commit, first rubric blocks → halt")
    _VERDICTS.clear(); _VERDICTS.update(correctness="block", reuse="approve")
    d, rd, store = _workspace(["correctness", "reuse"])
    led = _run(store, rd, d / "diff.txt", mode="commit", rubrics=["correctness", "reuse"])
    pr = led["prs"]["1"]
    check("only correctness ran", pr["rounds"][0]["ran"], ["correctness"])
    check("halted at correctness", pr["rounds"][0]["halted_at"], "correctness")
    check("spend = 1*COST", round(led["days"][today()], 6), round(COST, 6))
    check("correctness blocking", pr["rounds"][0]["states"]["correctness"], "blocking_block")

    # --- C: #1943 shape: a blocker persisted before the crash, another rubric resumes. The final
    # plan must include BOTH the rubric that ran and the carried blocker whose thread never landed.
    print("C. recovered round publishes a carried blocker that did not re-run")
    _VERDICTS.clear(); _VERDICTS.update(reuse="approve")
    d, rd, store = _workspace(["correctness", "reuse"])
    _seed(store, {"correctness": {
        "rubric": "correctness", "provider": "claude", "model": "m",
        "verdict": "request_changes", "summary": "persisted before SIGTERM",
        "findings": [{"file": "code/x.lean", "issue": "x"}],
        "reviewed_sha": HEAD, "run_id": "r-crashed", "thread": None,
        "author_replies": []}})
    plan_path, threads_dir = d / "plan.json", d / "threads"
    led = _run(store, rd, d / "diff.txt", mode="commit",
               rubrics=["correctness", "reuse"],
               extra=["--post-plan-file", str(plan_path), "--threads-dir", str(threads_dir)])
    plan = json.loads(plan_path.read_text())
    check("only the unfinished rubric ran", led["prs"]["1"]["rounds"][0]["ran"], ["reuse"])
    check("carried blocker is a required upsert",
          [(t["rubric"], t["action"], t["required"]) for t in plan["threads"]],
          [("correctness", "upsert", True)])

    # --- D: a pending update to an existing root is a model-free publication repair. It produces a
    # required PATCH plan but consumes neither a model call nor a review round.
    print("D. pending thread update repairs without a review round")
    _VERDICTS.clear()
    d, rd, store = _workspace(["correctness"])
    _seed(store, {"correctness": {
        "rubric": "correctness", "provider": "claude", "model": "m",
        "verdict": "request_changes", "summary": "new finding body",
        "findings": [{"file": "code/x.lean", "issue": "x"}],
        "reviewed_sha": HEAD, "run_id": "r-new", "pending_thread_run_id": "r-new",
        "thread": {"comment_id": 41, "path": "code/x.lean"}, "author_replies": []}},
        rounds=[{"round": 1, "mode": "commit", "ts": "2026-01-01T00:00:00Z", "cost": 1}])
    plan_path = d / "plan.json"
    led = _run(store, rd, d / "diff.txt", mode="commit", rubrics=["correctness"],
               extra=["--post-plan-file", str(plan_path)])
    plan = json.loads(plan_path.read_text())
    check("repair records no new round", len(led["prs"]["1"]["rounds"]), 1)
    check("repair plans current run", (plan["threads"][0]["run_id"],
                                        plan["threads"][0]["comment_id"]), ("r-new", 41))

    # --- E: the daily cap prevents spend, not publication repair.
    print("E. daily cap still publishes persisted blockers")
    d, rd, store = _workspace(["correctness"])
    _seed(store, {"correctness": {
        "rubric": "correctness", "provider": "claude", "model": "m",
        "verdict": "block", "summary": "must be contestable", "findings": [],
        "reviewed_sha": HEAD, "run_id": "r-cap", "thread": None,
        "author_replies": []}},
        rounds=[{"round": 1, "mode": "commit", "ts": today() + "T00:00:00Z", "cost": 1}])
    plan_path = d / "plan.json"
    led = _run(store, rd, d / "diff.txt", mode="commit", rubrics=["correctness"],
               extra=["--max-rounds-per-day", "1", "--post-plan-file", str(plan_path)])
    plan = json.loads(plan_path.read_text())
    check("cap adds no round", len(led["prs"]["1"]["rounds"]), 1)
    check("cap plan carries blocker", [t["rubric"] for t in plan["threads"]], ["correctness"])

    # --- F: all-green scoreboard recovery uses the PR marker and errors never create threads.
    print("F. scoreboard marker repairs green state; errors stay threadless")
    d, rd, store = _workspace(["correctness"])
    _seed(store, {"correctness": {
        "rubric": "correctness", "provider": "claude", "model": "m",
        "verdict": "approve", "summary": "green", "findings": [],
        "reviewed_sha": HEAD, "approved_sha": HEAD, "run_id": "r-green",
        "thread": None, "author_replies": []}},
        rounds=[{"round": 1, "mode": "commit", "ts": "2026-01-01T00:00:00Z", "cost": 1}],
        pending_publication_head_sha=HEAD)
    plan_path = d / "plan.json"
    led = _run(store, rd, d / "diff.txt", mode="commit", rubrics=["correctness"],
               extra=["--post-plan-file", str(plan_path)])
    check("green scoreboard retry adds no round", len(led["prs"]["1"]["rounds"]), 1)
    error_actions = review.thread_action_rubrics(
        ["correctness"], [], {"correctness": {
            "verdict": "error", "reviewed_sha": HEAD,
            "pending_thread_run_id": "r-error", "thread": None}}, HEAD)
    check("infra error excluded from thread repair", error_actions, [])

    # --- G: manual, daily budget allows only one call (reservation) → second is deferred ---
    print("G. daily-budget reservation stops the second rubric")
    _VERDICTS.clear(); _VERDICTS.update(correctness="approve", reuse="approve")
    d, rd, store = _workspace(["correctness", "reuse"])
    led = _run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness", "reuse"],
               daily_budget=1.5 * COST, max_call_cost=COST)
    pr = led["prs"]["1"]
    check("only one rubric ran (reserved)", pr["rounds"][0]["ran"], ["correctness"])
    check("spend = 1*COST", round(led["days"][today()], 6), round(COST, 6))

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
