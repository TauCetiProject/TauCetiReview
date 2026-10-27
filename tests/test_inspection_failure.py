"""Broken reviewer tools never become a published verdict (TauCetiReview#189)."""
import contextlib
import io
import json
import pathlib
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "runner")]
import reviewers
import review
import test_billing as tb


def codex_result(items, text="answer"):
    events = [{"type": "item.completed", "item": item} for item in items]
    events.append({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    response = SimpleNamespace(returncode=0, stderr="", stdout="\n".join(map(json.dumps, events)))
    with patch.object(reviewers, "sh", return_value=response):
        return reviewers.run_codex("prompt", ".", review.CODEX_MODEL, {})


def command(ok=True, output=""):
    return {"type": "command_execution", "command": "PRIVATE_COMMAND",
            "aggregated_output": output, "exit_code": 0 if ok else 1,
            "status": "completed" if ok else "failed"}


def test_codex_records_outcomes_without_commands_or_output():
    result = codex_result([command(False, "error running landlock: Sandbox(LandlockRestrict)")])
    assert result["returncode"] == 0
    assert reviewers.inspection_failure(result, {"verdict": "approve"}) == "sandbox_failed"
    assert result["tool_trace_meta"]["successful_calls"] == 0
    assert "PRIVATE_COMMAND" not in json.dumps(review.public_record(result))
    assert "LandlockRestrict" not in json.dumps(review.public_record(result))
    # A later successful call does not erase a sandbox initialization failure.
    result = codex_result([command(False, "command sandbox failed to start"), command()])
    assert reviewers.inspection_failure(result, {}) == "sandbox_failed"
    result = codex_result([command(False), command(True, "PRIVATE_SOURCE")])
    assert reviewers.inspection_failure(result, {"verdict": "approve"}) is None
    assert "PRIVATE_SOURCE" not in json.dumps(review.public_record(result))
    # A successful grep may read sandbox diagnostics from the project under review.
    result = codex_result([command(True, "command sandbox failed to start")])
    assert reviewers.inspection_failure(result, {}) is None
    # Diff-only answers and failed calls cannot masquerade as completed inspection.
    for items in ([], [command(False)]):
        assert reviewers.inspection_failure(codex_result(items), {}) == "no_successful_inspection"
    # Count beyond the public trace limit; truncation must not turn working tools into an outage.
    result = codex_result([command(False)] * reviewers._MAX_TOOL_TRACE + [command()])
    assert result["tool_trace_meta"]["successful_calls"] == 1
    assert reviewers.inspection_failure(result, {}) is None


def test_claude_counts_successful_reads_beyond_trace_limit():
    events = []
    for i in range(reviewers._MAX_TOOL_TRACE + 1):
        events.extend([
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": str(i),
              "name": "Grep", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": str(i),
              "is_error": i < reviewers._MAX_TOOL_TRACE, "content": "PRIVATE_SOURCE"}]}},
        ])
    trace, meta, _ = reviewers._tool_trace(map(json.dumps, events), ".")
    assert len(trace) == reviewers._MAX_TOOL_TRACE
    assert meta["successful_calls"] == 1
    assert "PRIVATE_SOURCE" not in json.dumps(trace)


def test_reported_gaps_withhold_any_verdict():
    for summary in (
        "Local source inspection was unavailable because the command sandbox failed to start.",
        "Required repository searches could not run because the execution sandbox failed to initialize.",
        "Required comparison against existing APIs remains incomplete; repository searches failed.",
        "Pinned Mathlib was not in the checkout, so it could not be searched.",
    ):
        for verdict in ("approve", "request_changes", "block"):
            assert reviewers.inspection_failure({}, {"verdict": verdict, "summary": summary}), summary
    assert reviewers.inspection_failure({}, {"summary": "The sandbox failure handler is correct."}) is None
    for summary in (
        "The attribution names the central Mathlib declaration it follows; no missing attribution.",
        "Reuses Mathlib's central declarations and has no missing citations.",
        "No missing Mathlib attribution was found.",
        "Mathlib is missing this API, so the project-specific lemma is needed.",
    ):
        assert reviewers.inspection_failure({}, {"verdict": "approve", "summary": summary}) is None
    # Findings can legitimately discuss sandbox code or quote an author's diagnostic.
    assert reviewers.inspection_failure({}, {"summary": "Bug found", "findings": [
        {"issue": "The sandbox failed to start"}]}) is None


def test_engine_aborts_without_plan_and_can_recover():
    for mode in ("commit", "manual", "reply"):
        for failure_kind in ("sandbox_failed", "no_successful_inspection", "reported_gap"):
            d, rd, store = tb._workspace(["correctness", "reuse"])
            plan, sb = d / "plan.json", d / "scoreboard.md"
            # A stale plan (including Actions init) must not be posted by a later always() step.
            plan.write_text('{"stale": true}')
            sb.write_text("stale approval")
            calls = []

            def runner(prompt, cwd, model, env):
                result = tb._fake_runner(prompt, cwd, model, env)
                calls.append(result)
                if failure_kind == "reported_gap":
                    result["text"] = result["text"].replace(
                        "says approve", "inspection was unavailable because the sandbox failed to start")
                elif failure_kind == "no_successful_inspection":
                    result["tool_trace_meta"] = {"total_calls": 3, "successful_calls": 0}
                else:
                    result["inspection_error"] = failure_kind
                return result

            extra = ["--post-plan-file", str(plan), "--scoreboard-file", str(sb)]
            if mode == "reply":
                extra += ["--reply-rubric", "correctness"]
            with contextlib.ExitStack() as stack:
                stack.callback(shutil.rmtree, d)
                stack.enter_context(patch.object(review, "run_claude", runner))
                stack.enter_context(patch.object(review, "reviewer_env", return_value=({}, None)))
                stack.enter_context(patch.object(review, "sweep_rev_homes"))
                stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                try:
                    tb._run(store, rd, d / "diff.txt", mode=mode,
                            rubrics=["correctness", "reuse"], extra=extra)
                except SystemExit as error:
                    assert error.code == 1
                else:
                    raise AssertionError("inspection failure was accepted as a review")
                assert len(calls) == 1, "broken tools must stop spending immediately"
                assert not plan.exists() and not sb.exists()
                ledger = json.loads((store / "ledger.json").read_text())
                pr = ledger["prs"]["1"]
                assert pr["state"]["correctness"]["verdict"] == "error"
                assert "pending_publication_head_sha" not in pr
                assert not pr["rounds"]
                assert ledger["days"][review.today()] == tb.COST
                record = json.loads((store / "reviews/1/1/correctness.json").read_text())
                assert record["verdict_obj"] is None
                assert record["attempts"][0]["error_kind"] in (
                    "sandbox_failed", "no_successful_inspection")
                # Repairing the tools re-runs the errored rubric and can publish normally.
                stack.enter_context(patch.object(review, "run_claude", tb._fake_runner))
                recovered = tb._run(store, rd, d / "diff.txt", mode="commit",
                                    rubrics=["correctness", "reuse"], extra=extra)
                assert recovered["prs"]["1"]["state"]["correctness"]["verdict"] == "approve"
                assert plan.exists() and sb.exists()


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS: {name}")
