#!/usr/bin/env python3
"""Paid results survive workspace/round changes; only genuinely new calls reserve budget."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_billing as billing
import review
from ledger import Ledger
from review_attempts import Attempts, source_identity


def workspace(root, name):
    work = root / name
    (work / "code").mkdir(parents=True)
    (work / "roadmap").mkdir()
    (work / "code/A.lean").write_text("def a := 1\n")
    (work / "roadmap/topic.md").write_text("same roadmap\n")
    return work


class Crashed(billing.DirectSessions):
    calls = []
    def run(self, provider, prompt, cwd, model, **kwargs):
        self.calls.append((provider, model))
        return super().run(provider, prompt, cwd, model, **kwargs)
    def finish(self):
        raise RuntimeError("crash after durable paid receipt")


class NoNewCalls(billing.DirectSessions):
    def run(self, *args, **kwargs):
        raise AssertionError("a paid completed result must replay without another model call")


def crash(store, rd, diff, work, **kwargs):
    with patch.object(review, "RoundReviewers", Crashed):
        try:
            billing._run(store, rd, diff, mode="manual", rubrics=["correctness"],
                         extra=["--tool-cwd", str(work)], **kwargs)
        except RuntimeError as error:
            assert "durable paid receipt" in str(error)
        else:
            raise AssertionError("missing crash")


def test_fresh_workspace_replays_at_exhausted_budget_and_zero_allowance():
    billing._install_stubs()
    billing._VERDICTS.clear()
    Crashed.calls = []
    d, rd, store = billing._workspace(["correctness"])
    try:
        a, b = workspace(d, "fresh-a"), workspace(d, "fresh-b")
        crash(store, rd, d / "diff.txt", a)
        assert len(Crashed.calls) == 1
        # Simulate a preceding invocation that already appended a stopped round.
        ledger = json.loads((store / "ledger.json").read_text())
        ledger["prs"]["1"]["rounds"].append({"round": 1, "mode": "manual", "cost": 0})
        (store / "ledger.json").write_text(json.dumps(ledger))
        (b / "review-deferred.json").write_text(json.dumps({"head_sha": "stale"}))
        with patch.object(review, "RoundReviewers", NoNewCalls):
            recovered = billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                daily_budget=billing.COST, max_call_cost=0,
                extra=["--tool-cwd", str(b)])
        assert recovered["prs"]["1"]["state"]["correctness"]["verdict"] == "approve"
        assert sum(recovered["days"].values()) == billing.COST
        assert recovered["prs"]["1"]["rounds"][-1]["cost"] == billing.COST
        assert not (b / "review-deferred.json").exists(), "paid replay is a completed review, not a deferral"
    finally:
        shutil.rmtree(d)


def test_replay_precedes_positive_call_reservation_and_does_not_append_empty_round():
    billing._install_stubs()
    billing._VERDICTS.clear()
    d, rd, store = billing._workspace(["correctness"])
    try:
        a, b = workspace(d, "first"), workspace(d, "retry")
        crash(store, rd, d / "diff.txt", a)
        with patch.object(review, "RoundReviewers", NoNewCalls):
            recovered = billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                daily_budget=billing.COST, max_call_cost=billing.COST,
                extra=["--tool-cwd", str(b)])
        assert len(recovered["prs"]["1"]["rounds"]) == 1
        # Fresh unrelated work cannot be funded; it records no round and no plan.
        other = d / "other-store"; other.mkdir()
        plan = d / "unfunded-plan.json"
        with patch.object(review, "RoundReviewers", NoNewCalls):
            stopped = billing._run(other, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                daily_budget=0, max_call_cost=billing.COST,
                extra=["--tool-cwd", str(b), "--post-plan-file", str(plan)])
        assert not stopped["prs"]["1"]["rounds"] and not plan.exists()
        assert json.loads((b / "review-deferred.json").read_text()) == {
            "schema": "tauceti.review-deferred/v1", "head_sha": billing.HEAD, "reason": "daily-budget"}
    finally:
        shutil.rmtree(d)


def test_changed_visible_sources_or_config_invalidate_paid_recovery():
    billing._install_stubs()
    billing._VERDICTS.clear()
    d, rd, store = billing._workspace(["correctness"])
    try:
        a, b = workspace(d, "first"), workspace(d, "retry")
        crash(store, rd, d / "diff.txt", a)
        (b / "code/A.lean").write_text("def a := 2\n")
        with patch.object(review, "RoundReviewers", NoNewCalls):
            stopped = billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                daily_budget=billing.COST, max_call_cost=billing.COST,
                extra=["--tool-cwd", str(b)])
        assert not stopped["prs"]["1"]["state"] and not stopped["prs"]["1"]["rounds"]
        (b / "code/A.lean").write_text("def a := 1\n")
        for extra in (["--claude-model", "claude-fable-5-1"], ["--head-sha", "b" * 40], ["--auth", "api"]):
            with patch.object(review, "RoundReviewers", NoNewCalls):
                stopped = billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                    daily_budget=billing.COST, max_call_cost=billing.COST,
                    extra=["--tool-cwd", str(b), *extra])
            assert not stopped["prs"]["1"]["state"]
        (rd / "correctness.md").write_text("ANGLE:correctness\nchanged rubric\n")
        with patch.object(review, "RoundReviewers", NoNewCalls):
            stopped = billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                daily_budget=billing.COST, max_call_cost=billing.COST, extra=["--tool-cwd", str(b)])
        assert not stopped["prs"]["1"]["state"]
    finally:
        shutil.rmtree(d)


def test_replayed_error_still_reserves_before_new_retry():
    billing._install_stubs()
    billing._VERDICTS.clear(); billing._VERDICTS["correctness"] = "error"
    d, rd, store = billing._workspace(["correctness"])
    try:
        a, b = workspace(d, "first"), workspace(d, "retry")
        crash(store, rd, d / "diff.txt", a)
        with patch.object(review, "RoundReviewers", NoNewCalls):
            stopped = billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                daily_budget=billing.COST, max_call_cost=billing.COST, extra=["--tool-cwd", str(b)])
        assert not stopped["prs"]["1"]["state"] and not stopped["prs"]["1"]["rounds"]
        assert sum(stopped["days"].values()) == billing.COST
        assert not stopped.get("completed_review_attempts")
    finally:
        shutil.rmtree(d)


def test_round_totals_recovered_and_new_same_provider_without_double_reporting():
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        ledger = Ledger(root / "ledger.json")
        old = Attempts(root / "reviews/1/1", "correctness", "context", ledger, "day", head_sha="head")
        receipt = old.record("claude", "model", {"cost_usd": .02}, {"verdict": "approve"}, "sha", None, False)
        new = Attempts(root / "reviews/1/2", "reuse", "other", ledger, "day", head_sha="head")
        newer = new.record("claude", "model", {"cost_usd": .03}, {"verdict": "approve"}, "sha", None, False)
        results = [{"provider": "claude", "cost_usd": .02, "attempt_receipt_ids": [receipt["id"]]},
                   {"provider": "claude", "cost_usd": .03, "attempt_receipt_ids": [newer["id"]]}]
        totals = Attempts.round_totals(root / "reviews/1/2", "head", results, [receipt])
        assert totals["cost_usd"] == .05
        assert Attempts.round_totals(root / "reviews/1/2", "head", results, [receipt], [receipt["id"]])["cost_usd"] == .03
        real = Attempts(root / "reviews/1/3", "correctness", "context", ledger, "day")
        assert real.replay("claude", "model")["id"] == receipt["id"]
        real.complete()
        assert not Attempts(root / "reviews/1/4", "correctness", "context", ledger, "day").saved
        dry = Attempts(root / "reviews/1/5", "correctness", "dry", ledger, "day", dry_run=True)
        dry.record("claude", "model", {"cost_usd": 1}, {}, "sha", None, False)
        assert not Attempts(root / "reviews/1/6", "correctness", "dry", ledger, "day").saved


def test_input_identity_covers_full_artifacts_and_all_declared_sources_once():
    from types import SimpleNamespace
    from review_attempts import context_identity
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        work = workspace(root, "work")
        (work / "mathlib").mkdir(); (work / "mathlib/M.lean").write_text("mathlib source")
        diff, desc = root / "diff", root / "description"
        diff.write_text("full diff"); desc.write_text("full description")
        args = SimpleNamespace(tool_cwd=str(work), code_path="code", roadmap_path="roadmap", mathlib_path="mathlib",
                               lean_src="", diff_file=str(diff), pr_desc_file=str(desc))
        first = context_identity(args)
        for path in (diff, desc, work / "roadmap/topic.md", work / "mathlib/M.lean"):
            previous = path.read_bytes(); path.write_bytes(previous + b"changed")
            assert context_identity(args) != first
            path.write_bytes(previous)
            assert context_identity(args) == first
    billing._install_stubs(); billing._VERDICTS.clear()
    d, rd, store = billing._workspace(["correctness", "reuse"])
    try:
        with patch.object(review, "context_identity", wraps=review.context_identity) as identity:
            billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness", "reuse"])
        assert identity.call_count == 1
    finally:
        shutil.rmtree(d)


def test_partial_round_retry_reports_only_new_receipt_spend():
    billing._install_stubs()
    billing._VERDICTS.clear(); billing._VERDICTS["reuse"] = "error"
    d, rd, store = billing._workspace(["correctness", "reuse"])
    try:
        work = workspace(d, "work")
        first = billing._run(store, rd, d / "diff.txt", mode="commit", rubrics=["correctness", "reuse"],
                            daily_budget=.02, max_call_cost=.01, extra=["--tool-cwd", str(work)])
        assert first["prs"]["1"]["rounds"][0]["cost"] == .02
        assert "reuse" not in first["prs"]["1"]["state"]
        plan = d / "repair-plan.json"
        with patch.object(review, "RoundReviewers", NoNewCalls):
            repair = billing._run(store, rd, d / "diff.txt", mode="commit", rubrics=["correctness", "reuse"],
                daily_budget=.02, max_call_cost=.01,
                extra=["--tool-cwd", str(work), "--post-plan-file", str(plan)])
        assert plan.exists(), "budget must not strand a completed case-file publication"
        assert len(repair["prs"]["1"]["rounds"]) == 1
        billing._VERDICTS["reuse"] = "approve"
        second = billing._run(store, rd, d / "diff.txt", mode="commit", rubrics=["correctness", "reuse"],
                             daily_budget=.03, max_call_cost=.01, extra=["--tool-cwd", str(work)])
        assert second["prs"]["1"]["state"]["reuse"]["verdict"] == "approve"
        assert round(sum(second["days"].values()), 6) == .03
        assert [r["cost"] for r in second["prs"]["1"]["rounds"]] == [.02, .01]
    finally:
        shutil.rmtree(d)


def test_source_identity_git_clean_dirty_untracked_and_exported_equivalence():
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        for name in ("a", "b"):
            p = root / name; p.mkdir()
            subprocess.run(["git", "init", "-q", str(p)], check=True)
            (p / "A.lean").write_text("same source\n")
            subprocess.run(["git", "-C", str(p), "add", "."], check=True)
            subprocess.run(["git", "-C", str(p), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "source"], check=True)
        assert source_identity(root / "a") == source_identity(root / "b")
        subprocess.run(["git", "-C", str(root / "b"), "update-index", "--assume-unchanged", "A.lean"], check=True)
        flagged_before = source_identity(root / "b")
        (root / "b/A.lean").write_text("hidden changed source\n")
        assert source_identity(root / "b") != flagged_before
        subprocess.run(["git", "-C", str(root / "b"), "update-index", "--no-assume-unchanged", "A.lean"], check=True)
        (root / "b/A.lean").write_text("changed source\n")
        assert source_identity(root / "a") != source_identity(root / "b")
        (root / "b/A.lean").write_text("same source\n")
        (root / "b/untracked").write_text("visible\n")
        assert source_identity(root / "a") != source_identity(root / "b")
        shutil.rmtree(root / "a/.git"); shutil.rmtree(root / "b/.git")
        (root / "b/untracked").unlink()
        assert source_identity(root / "a") == source_identity(root / "b")


def test_git_dangling_cyclic_and_external_links_disable_reuse_without_scanning_host():
    from types import SimpleNamespace
    from review_attempts import context_identity
    billing._install_stubs(); billing._VERDICTS.clear()
    for kind in ("dangling", "cycle", "external"):
        d, rd, store = billing._workspace(["correctness"])
        try:
            work = workspace(d, "work")
            code = work / "code"
            subprocess.run(["git", "init", "-q", str(code)], check=True)
            link = code / "link"
            if kind == "dangling":
                link.symlink_to("missing")
            elif kind == "cycle":
                link.symlink_to("other"); (code / "other").symlink_to("link")
            else:
                link.symlink_to("/")
            subprocess.run(["git", "-C", str(code), "add", "."], check=True)
            subprocess.run(["git", "-C", str(code), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "source"], check=True)
            args = SimpleNamespace(tool_cwd=str(work), code_path="code")
            original_iterdir = pathlib.Path.iterdir
            def inside_only(path):
                assert path.resolve().is_relative_to(work), "identity scanned outside source workspace"
                return original_iterdir(path)
            with patch.object(pathlib.Path, "iterdir", inside_only):
                first, second = context_identity(args), context_identity(args)
                assert first.startswith("nonreusable:") and first != second
                reviewed = billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                                        extra=["--tool-cwd", str(work)])
            assert reviewed["prs"]["1"]["state"]["correctness"]["verdict"] == "approve"
        finally:
            shutil.rmtree(d)


def test_unreadable_source_disables_receipt_reuse_without_aborting_review():
    from types import SimpleNamespace
    from review_attempts import context_identity
    billing._install_stubs(); billing._VERDICTS.clear()
    d, rd, store = billing._workspace(["correctness"])
    try:
        work = workspace(d, "work")
        blocked = work / "code/A.lean"
        original_open = pathlib.Path.open
        def unreadable(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError("fixture unreadable input")
            return original_open(path, *args, **kwargs)
        args = SimpleNamespace(tool_cwd=str(work), code_path="code")
        with patch.object(pathlib.Path, "open", unreadable):
            first, second = context_identity(args), context_identity(args)
            assert first.startswith("nonreusable:") and first != second
            reviewed = billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                                    extra=["--tool-cwd", str(work)])
        assert reviewed["prs"]["1"]["state"]["correctness"]["verdict"] == "approve"
    finally:
        shutil.rmtree(d)


def test_replayed_rejected_primer_uses_one_funded_direct_retry_without_repriming():
    billing._install_stubs(); billing._VERDICTS.clear()
    d, rd, store = billing._workspace(["correctness"])
    calls = []
    class RejectedCrash(billing.DirectSessions):
        def run(self, *args, **kwargs):
            return {"returncode": 1, "text": "", "cost_usd": .01, "primer_rejected": True}
        def finish(self):
            raise RuntimeError("crash after rejected primer receipt")
    class DirectAfterReplay(billing.DirectSessions):
        rejected = False
        def reject(self, provider):
            self.rejected = True
        def needs_primer(self, *args):
            return not self.rejected
        def run(self, provider, prompt, cwd, model, **kwargs):
            assert self.rejected, "replay did not restore uncached-provider decision"
            calls.append(provider)
            return billing._fake_runner(prompt, cwd, model, {})
    try:
        with patch.object(review, "RoundReviewers", RejectedCrash):
            try:
                billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"])
            except RuntimeError as error:
                assert "rejected primer receipt" in str(error)
        with patch.object(review, "RoundReviewers", DirectAfterReplay):
            recovered = billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                                     daily_budget=.02, max_call_cost=.01)
        assert calls == ["claude"]
        assert recovered["prs"]["1"]["state"]["correctness"]["verdict"] == "approve"
        assert sum(recovered["days"].values()) == .02
    finally:
        shutil.rmtree(d)


def test_paid_uncached_failure_is_retained_when_retry_cannot_be_funded():
    for provider in ("kiro", "deepseek"):
        billing._install_stubs(); billing._VERDICTS.clear()
        calls = []
        def failed_once(prompt, cwd, model, env):
            calls.append(model)
            assert len(calls) == 1, "unfunded retry reached the provider"
            return {"returncode": 0, "text": "no verdict", "cost_usd": .01,
                    "usage": {"input_tokens": 123, "output_tokens": 45}}
        d, rd, store = billing._workspace(["correctness"])
        try:
            function = "run_kiro" if provider == "kiro" else "run_pi"
            with patch.object(review, function, failed_once):
                try:
                    billing._run(store, rd, d / "diff.txt", mode="manual", rubrics=["correctness"],
                                 daily_budget=.01, max_call_cost=.01, extra=["--providers", provider])
                except SystemExit as error:
                    assert error.code == review.REVIEWER_CRASHED_EXIT
                else:
                    raise AssertionError("a paid failed review must retain its withheld outcome")
            ledger = json.loads((store / "ledger.json").read_text())
            recorded = ledger["prs"]["1"]["rounds"][0]
            assert len(calls) == 1 and sum(ledger["days"].values()) == .01
            assert recorded["cost"] == .01 and recorded["mode"] == "withheld"
            result = json.loads((store / "reviews/1/1/correctness.json").read_text())
            assert result["retry_deferred_for_budget"] and result["cost_usd"] == .01
            assert result["usage"] == {"input_tokens": 123, "output_tokens": 45}
            assert recorded["tokens"] == review.sum_usage([result])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test(); print("ok", test.__name__)
    print(len(tests), "passed")
