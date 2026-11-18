#!/usr/bin/env python3
"""A reviewer crash is withheld, not published, unless a genuine verdict already decides the board.

From 2026-11-15 one group of Claude reviewer workers crashed after its first model call in about
92% of its rubric runs, and each round still posted a scoreboard whose `error` rows made it "changes
requested". The merge queue reads a non-green scoreboard as not approved, so those crashes dropped
merges until someone re-reviewed. The provider-down abort did not apply: these runs were billed for
a real model call and failed locally, not at the provider.

Pinned here:

  1. A round whose crashes would decide the verdict publishes nothing and exits
     REVIEWER_CRASHED_EXIT; the next round re-runs only the crashed rubrics.
  2. A crash beside a genuine adverse verdict at this head is still published, with the crash kind
     in the scoreboard's hidden metadata.
  3. The crash kinds a local setup produces have names of their own.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import contextlib
import io
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "runner"))
sys.path.insert(0, str(HERE))
import casefile  # noqa: E402
import reviewers  # noqa: E402
import review  # noqa: E402
import test_billing as tb  # noqa: E402  (the stubbed-reviewer harness)

META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)
HEAD = tb.HEAD
fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def run(store, rd, d, rubrics, extra=(), **budget):
    """One engine invocation; returns (exit status, stderr, ledger)."""
    err = io.StringIO()
    code = 0
    with contextlib.redirect_stderr(err):
        try:
            tb._run(store, rd, d / "diff.txt", mode="commit", rubrics=rubrics,
                    extra=["--post-plan-file", str(d / "plan.json"), *extra], **budget)
        except SystemExit as e:
            code = e.code
    return code, err.getvalue(), json.loads((store / "ledger.json").read_text())


def last_line(text):
    return [line for line in text.splitlines() if line.strip()][-1]


def main():
    tb._install_stubs()

    # --- 1) crashes alone are withheld, and the retry re-runs only them ------------------------
    tb._VERDICTS.clear(); tb._VERDICTS.update(correctness="error", reuse="approve")
    d, rd, store = tb._workspace(["correctness", "reuse"])
    (d / "plan.json").write_text("{}")  # an earlier plan at the same path, as CI's in-progress board
    code, err, led = run(store, rd, d, ["correctness", "reuse"])
    pr = led["prs"]["1"]
    check("a crash that decides the board exits with the withheld status",
          code == review.REVIEWER_CRASHED_EXIT)
    check("no post plan is left behind", not (d / "plan.json").exists())
    check("no scoreboard is rendered", not (store / "reviews/1/1/scoreboard.md").exists())
    check("the round is recorded as withheld",
          [(r["round"], r["mode"]) for r in pr["rounds"]] == [(1, "withheld")])
    check("with its spend", round(pr["rounds"][0]["cost"], 6) == round(3 * tb.COST, 6))
    check("the publication marker is cleared", "pending_publication_head_sha" not in pr)
    check("the crash stays recorded", pr["state"]["correctness"]["verdict"] == "error")
    check("with its kind", pr["state"]["correctness"]["error_kind"] == "no_verdict")
    check("the genuine verdict is kept", pr["state"]["reuse"]["verdict"] == "approve")
    check("spend is kept", round(led["days"][review.today()], 6) == round(3 * tb.COST, 6))
    check("the engine log names the kind", "[correctness] no verdict: no_verdict" in err)
    check("the last line says what was withheld and why",
          last_line(err) == "review withheld: the reviewer crashed on 1 rubric(s) "
                            "(no_verdict: correctness)")

    tb._VERDICTS.update(correctness="approve")
    code, err, led = run(store, rd, d, ["correctness", "reuse"])
    pr = led["prs"]["1"]
    check("the retry publishes", code == 0 and (d / "plan.json").exists())
    check("the retry re-runs only the crashed rubric", pr["rounds"][1]["ran"] == ["correctness"])
    check("in a new round", pr["rounds"][1]["round"] == 2 and pr["rounds"][1]["mode"] == "commit")
    check("and the board is green", set(pr["rounds"][1]["states"].values()) == {"green"})
    check("the PR's spend includes the withheld round", "Review spend: $0.04" in
          (store / "reviews/1/2/scoreboard.md").read_text())

    # --- 1b) a later round that runs nothing still withholds the recorded crash ----------------
    tb._VERDICTS.clear(); tb._VERDICTS.update(correctness="error", reuse="approve")
    d, rd, store = tb._workspace(["correctness", "reuse"])
    tight = dict(daily_budget=2.5 * tb.COST, max_call_cost=tb.COST)
    code, err, led = run(store, rd, d, ["correctness", "reuse"], **tight)
    check("a crash that spends the budget is withheld", code == review.REVIEWER_CRASHED_EXIT)
    code, err, led = run(store, rd, d, ["correctness", "reuse"], **tight)
    check("the next round, with nothing run, withholds it again",
          code == review.REVIEWER_CRASHED_EXIT and not (d / "plan.json").exists())

    # --- 1c) a withheld round does not swallow a contest answer or a thread close ---------------
    tb._VERDICTS.clear(); tb._VERDICTS.update(correctness="approve", reuse="error")
    d, rd, store = tb._workspace(["correctness", "reuse"])
    contested = {"rubric": "correctness", "provider": "claude", "model": "m",
                 "verdict": "request_changes", "summary": "s", "findings": [],
                 "reviewed_sha": HEAD, "run_id": "r-old", "thread": {"comment_id": 900},
                 "author_replies": [{"id": 1000, "by": "author", "body": "I disagree"}],
                 "last_reply_seen": 0}
    tb._seed(store, {"correctness": contested})
    code, err, led = run(store, rd, d, ["correctness", "reuse"])
    cf = led["prs"]["1"]["state"]["correctness"]
    check("the contest round is withheld", code == review.REVIEWER_CRASHED_EXIT)
    check("the answered rubric is rolled back to its open finding",
          cf["verdict"] == "request_changes" and cf.get("last_reply_seen") == 0)
    tb._VERDICTS.update(reuse="approve")
    code, err, led = run(store, rd, d, ["correctness", "reuse"])
    actions = {(t["rubric"], t["action"]) for t in json.loads((d / "plan.json").read_text())["threads"]}
    check("the retry publishes", code == 0)
    check("and closes the answered thread", ("correctness", "close") in actions)
    check("and answers the contest", ("correctness", "reply") in actions)

    # --- 2) a crash beside a genuine adverse verdict is published, with its kind ----------------
    tb._VERDICTS.clear(); tb._VERDICTS.update(correctness="error", reuse="request_changes")
    d, rd, store = tb._workspace(["correctness", "reuse"])
    code, err, led = run(store, rd, d, ["correctness", "reuse"])
    check("a genuine adverse verdict publishes", code == 0 and (d / "plan.json").exists())
    meta = json.loads(META_RE.findall((store / "reviews/1/1/scoreboard.md").read_text())[-1])
    check("the scoreboard metadata names the crash kind", meta.get("errors") == {"correctness": "no_verdict"})
    runs = {r["rubric"]: r for r in meta["runs"]}
    check("the crashed run carries its kind", runs["correctness"].get("error_kind") == "no_verdict")
    check("a run with a verdict carries none", "error_kind" not in runs["reuse"])

    # A clean scoreboard has no errors key at all.
    clean = review.render_scoreboard(["reuse"], {"reuse": {"verdict": "approve", "approved_sha": HEAD}},
                                     HEAD, "approved", "")
    check("a clean board has no errors key", "errors" not in json.loads(META_RE.findall(clean)[-1]))

    # --- 3) a shadow round publishes nothing anyway, so it keeps its record -------------------
    tb._VERDICTS.clear(); tb._VERDICTS.update(correctness="error")
    d, rd, store = tb._workspace(["correctness"])
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            tb._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                    extra=["--shadow", "--arm", "shadow:x",
                           "--archive-dir", str(d / "outbox")])
        code = 0
    except SystemExit as e:
        code = e.code
    check("a shadow round is not withheld", code == 0)
    check("its scoreboard is still rendered for the archive",
          (store / "reviews/1/1/scoreboard.md").exists())

    # --- 4) the rule ---------------------------------------------------------------------------
    def cf(verdict, sha=HEAD):
        return {"verdict": verdict, "reviewed_sha": sha}

    rule = review.crash_decides_verdict
    check("no crash, nothing to withhold", not rule({"a": "green"}, [], {"a": cf("approve")}, HEAD))
    check("one crash among greens decides the board",
          rule({"a": "error", "b": "green"}, ["a"], {"a": cf("error"), "b": cf("approve")}, HEAD))
    check("a request at this head decides it instead",
          not rule({"a": "error", "b": "blocking_request"}, ["a"],
                   {"a": cf("error"), "b": cf("request_changes")}, HEAD))
    check("a block at this head decides it instead",
          not rule({"a": "error", "b": "blocking_block"}, ["a"], {"a": cf("error"), "b": cf("block")}, HEAD))
    check("a request from an older commit does not",
          rule({"a": "error", "b": "blocking_request"}, ["a"],
               {"a": cf("error"), "b": cf("request_changes", "c" * 40)}, HEAD))
    check("a request on the reused published board of this head does",
          not rule({"a": "error", "b": "blocking_request"}, ["a"], {"a": cf("error")}, HEAD))

    # --- 5) the crash kinds ---------------------------------------------------------------------
    denied = {"returncode": 0, "text": "no marker", "tool_trace_meta": {"denied_calls": 2}}
    check("refused tool calls are tool_denied", review.error_kind(denied) == "tool_denied")
    check("a provider outage still outranks a refusal",
          review.error_kind({**denied, "raw_stderr": "401 OAuth access token has been revoked"})
          == "not_authenticated")
    check("max turns is named",
          review.error_kind({"returncode": 0, "is_error": True, "error_subtype": "error_max_turns"})
          == "max_turns")
    check("an execution error is named",
          review.error_kind({"returncode": 0, "is_error": True, "error_subtype": "error_during_execution"})
          == "execution_error")
    check("an unreadable stream from a clean exit is unparseable_output",
          review.error_kind({"returncode": 0, "parse_error": "no result event in a 0-byte stream"})
          == "unparseable_output")
    check("a failed process is still unknown_error",
          review.error_kind({"returncode": 1, "parse_error": "no result event in a 0-byte stream"})
          == "unknown_error")
    check("an unexplained CLI failure is cli_error",
          review.error_kind({"returncode": 0, "is_error": True, "error_subtype": "success"}) == "cli_error")
    check("a clean answer without a verdict is no_verdict",
          review.error_kind({"returncode": 0, "text": "prose"}) == "no_verdict")
    for kind in ("tool_denied", "max_turns", "execution_error", "unparseable_output", "cli_error"):
        check(f"{kind} is not a provider outage", kind not in review.PROVIDER_DOWN_KINDS)

    # The trace counts refusals from either place the CLI reports them, without their text.
    def ev(**k):
        return json.dumps(k)

    system_denial = ev(type="system", subtype="permission_denied", tool_name="Read",
                       message="Permission to use Read has been denied.")
    result = ev(type="result", result="x", permission_denials=[{"tool_name": "Read"}] * 3)
    trace, meta, _ = reviewers._tool_trace([system_denial, result], ".")
    check("the result event's denials are counted", meta.get("denied_calls") == 3)
    trace, meta, _ = reviewers._tool_trace([system_denial, ev(type="result", result="x")], ".")
    check("a system denial event is counted", meta.get("denied_calls") == 1)
    check("and its message is not kept", "denied" not in json.dumps(trace))
    trace, meta, _ = reviewers._tool_trace([ev(type="result", result="x", permission_denials=[])], ".")
    check("no denials, no key", "denied_calls" not in meta)

    # A later verdict clears the recorded kind.
    state = {}
    casefile.update_case_file(state, "a", {"verdict_obj": None, "error_kind": "tool_denied"}, HEAD)
    check("the case file records the kind", state["a"]["error_kind"] == "tool_denied")
    casefile.update_case_file(state, "a", {"verdict_obj": {"verdict": "approve"}}, HEAD)
    check("a verdict clears it", "error_kind" not in state["a"])

    # --- 6) the CLI agrees on the status and exits on the engine's own line ----------------------
    cli = (HERE.parent / "runner" / "cli.py").read_text()
    m = re.search(r"^REVIEWER_CRASHED_EXIT = (\d+)$", cli, re.M)
    check("cli.py defines the same exit status", bool(m) and int(m.group(1)) == review.REVIEWER_CRASHED_EXIT)
    check("cli.py exits on it", "if r.returncode == REVIEWER_CRASHED_EXIT:" in cli)
    check("distinct from the provider-down status", review.REVIEWER_CRASHED_EXIT != review.PROVIDER_DOWN_EXIT)

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
