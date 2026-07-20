#!/usr/bin/env python3
"""Coverage for the seamless codex Sol->Terra downgrade:
  - run_codex parses the terminal failure's structured status/message (not by regex over escaped text),
  - codex_model_unavailable() needs BOTH a model-access message AND a non-transient status,
  - run_codex survives malformed failure payloads without crashing,
  - run_rubric's reconfirm/downgrade state machine takes the right branch, attempts, and final model.
Dependency-free."""
import sys
import types
import pathlib
import argparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import reviewers  # noqa: E402
import pricing    # noqa: E402
import review     # noqa: E402

# Verbatim stdout from `codex exec --json -m <unavailable-model>` on codex-cli 0.144 (ChatGPT auth).
# An unentitled account (Free/Go asking for Sol) returns the same 400 invalid_request_error shape. The
# nested payload is ESCAPED JSON — the exact thing a naive regex misses, so run_codex json.loads() it.
REAL_UNAVAILABLE_STDOUT = (
    '{"type":"thread.started","thread_id":"019f-abc"}\n'
    '{"type":"item.completed","item":{"id":"item_0","type":"error","message":'
    '"Model metadata for `gpt-5.6-sol` not found. Defaulting to fallback metadata."}}\n'
    '{"type":"turn.started"}\n'
    '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":400,\\"error\\":{\\"type\\":'
    '\\"invalid_request_error\\",\\"message\\":\\"The \'gpt-5.6-sol\' model is not supported when using '
    'Codex with a ChatGPT account.\\"}}"}\n'
    '{"type":"turn.failed","error":{"message":"{\\"status\\":400,\\"error\\":{\\"type\\":'
    '\\"invalid_request_error\\",\\"message\\":\\"The \'gpt-5.6-sol\' model is not supported when using '
    'Codex with a ChatGPT account.\\"}}"}}\n'
)


def _run_codex_with_stdout(stdout, returncode=1, stderr=""):
    """Drive run_codex over canned codex output by stubbing sh() — exercises the real event/status parse."""
    orig = reviewers.sh
    reviewers.sh = lambda *a, **k: types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    try:
        return reviewers.run_codex("prompt", "/tmp", "gpt-5.6-sol", {})  # no OPENAI_API_KEY -> no login call
    finally:
        reviewers.sh = orig


# ----- run_codex parsing (status + message, and crash-hardening) -----

def test_run_codex_parses_status_and_message_from_real_output():
    out = _run_codex_with_stdout(REAL_UNAVAILABLE_STDOUT)
    assert out.get("error_status") == 400, f"terminal 400 must be parsed, got {out.get('error_status')!r}"
    assert "not supported when using Codex" in (out.get("error_message") or ""), out.get("error_message")
    assert reviewers.codex_model_unavailable(out), "real unavailable-model output must classify as unavailable"


def test_run_codex_survives_malformed_payloads():
    # turn.failed.error is a string (not an object); error event message is non-JSON. Must not raise,
    # and must fall back to the parseable earlier `error` event rather than the broken terminal one.
    stdout = (
        '{"type":"error","message":"{\\"status\\":403,\\"error\\":{\\"type\\":\\"invalid_request_error\\",'
        '\\"message\\":\\"no access to this model\\"}}"}\n'
        '{"type":"turn.failed","error":"boom - not an object"}\n'
    )
    out = _run_codex_with_stdout(stdout)  # would AttributeError if .get() were called on the string
    assert out.get("error_status") == 403, out.get("error_status")
    assert reviewers.codex_model_unavailable(out)
    # A terminal message that decodes to a non-dict (a JSON list) must be ignored, not crash.
    out2 = _run_codex_with_stdout('{"type":"turn.failed","error":{"message":"[1,2,3]"}}\n')
    assert out2.get("error_status") is None
    assert not reviewers.codex_model_unavailable(out2)  # no status, no message -> not a model problem


# ----- codex_model_unavailable: needs BOTH a model message AND a non-transient status -----

def test_status_alone_is_not_enough():
    # A 400 whose message is NOT about model access (e.g. context length) must NOT be a downgrade.
    assert not reviewers.codex_model_unavailable(
        {"returncode": 1, "error_status": 400, "error_message": "This request exceeds the context window."})
    # A model-access message WITH a 400/403/404 is a downgrade.
    for st in (400, 403, 404):
        assert reviewers.codex_model_unavailable(
            {"returncode": 1, "error_status": st,
             "error_message": "The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account."}), st
    # Even a model-access-looking message is NOT a downgrade under a transient/limit/5xx status.
    for st in (429, 500, 502, 503, 504):
        assert not reviewers.codex_model_unavailable(
            {"returncode": 1, "error_status": st, "error_message": "model not found"}), st


def test_no_status_falls_back_to_message_only():
    assert reviewers.codex_model_unavailable(
        {"returncode": 1, "error_message": "The model does not exist or you do not have access to it."})
    assert reviewers.codex_model_unavailable(
        {"returncode": 1, "raw_stderr": "error: model_not_found for gpt-5.6-sol"})
    assert not reviewers.codex_model_unavailable({"returncode": 0, "text": "APPROVE — looks correct."})
    for msg in ("connection reset by peer", "request timed out after 600s", "stream disconnected"):
        assert not reviewers.codex_model_unavailable({"returncode": 1, "raw_stderr": msg}), msg


# ----- run_rubric reconfirm / downgrade state machine -----

_SOL, _TERRA = pricing.CODEX_MODEL, pricing.CODEX_FALLBACK_MODEL
_OK = {"returncode": 0, "text": "OK"}                                             # has verdict
_TRANSIENT = {"returncode": 1, "text": "", "error_status": 500}                   # not verdict, not model
_UNAVAIL = {"returncode": 1, "text": "", "error_status": 400,
            "error_message": "model is not supported when using Codex with a ChatGPT account"}


def _drive_run_rubric(seq, explicit=False):
    """Run run_rubric with a scripted runner-result sequence; return (models_called, final_model,
    registry_model). Stubs the surrounding I/O so only the downgrade state machine is exercised."""
    called = []

    def fake_runner(prompt, cwd, model, env):
        called.append(model)
        return dict(seq[len(called) - 1])

    runners = {"codex": (fake_runner, _SOL)}
    saved = {n: getattr(review, n) for n in
             ("reviewer_env", "cleanup_rev_home", "build_prompt", "build_reactivation_block",
              "extract_verdict", "update_case_file", "newest_reply_id")}
    review.reviewer_env = lambda *a, **k: ({}, "/tmp/none")
    review.cleanup_rev_home = lambda *a, **k: None
    review.build_prompt = lambda *a, **k: "PROMPT"
    review.build_reactivation_block = lambda *a, **k: ""
    review.extract_verdict = lambda text, marker: {"verdict": "approve"} if text == "OK" else None
    review.update_case_file = lambda *a, **k: {"author_replies": []}
    review.newest_reply_id = lambda *a, **k: None
    import tempfile
    outdir = pathlib.Path(tempfile.mkdtemp())
    a = argparse.Namespace(mode="commit", reply_rubric=None, rubrics_dir="/tmp", tool_cwd="/tmp",
                           code_path="code", repo="r", pr=1, archive_dir=None, dry_run=True, arm="production",
                           auth="subscription", submitted_by=None, base_sha=None, merge_base_sha=None,
                           rubrics_repo="rr", rubrics_sha=None, rubrics_sha_approx=None)
    ledger = types.SimpleNamespace(set_spent=lambda *a, **k: None, persist=lambda: None)
    ctx = review.RunContext(a=a, state_map={}, reply_text="", base_context="", head="h" * 40,
                            providers=["codex"], runners=runners, keys={}, subscription=True,
                            rubrics_version="v1", round_num=1, prov={"round": 1}, diff_full="",
                            outdir=outdir, day="2026-07-20", ledger=ledger, spent_today=0.0,
                            codex_model_explicit=explicit)
    try:
        review.run_rubric(ctx, "correctness")
    finally:
        for n, v in saved.items():
            setattr(review, n, v)
    return called, ctx.run_results[0]["model"], runners["codex"][1]


def test_state_machine_branches():
    # (sequence, explicit) -> (expected models called, expected final model, expected registry model)
    assert _drive_run_rubric([_OK]) == ([_SOL], _SOL, _SOL)
    assert _drive_run_rubric([_TRANSIENT, _OK]) == ([_SOL, _SOL], _SOL, _SOL)
    assert _drive_run_rubric([_UNAVAIL, _OK]) == ([_SOL, _SOL], _SOL, _SOL)          # cleared on reconfirm
    assert _drive_run_rubric([_UNAVAIL, _UNAVAIL, _OK]) == ([_SOL, _SOL, _TERRA], _TERRA, _TERRA)  # persist
    assert _drive_run_rubric([_UNAVAIL, _UNAVAIL, _TRANSIENT]) == ([_SOL, _SOL, _TERRA], _TERRA, _SOL)  # no persist
    # An explicit --codex-model pin opts out of the downgrade: only the ordinary same-model retry runs.
    assert _drive_run_rubric([_UNAVAIL, _OK], explicit=True) == ([_SOL, _SOL], _SOL, _SOL)


def test_fallback_is_priced_distinct_and_dispatchable():
    assert pricing.CODEX_FALLBACK_MODEL != pricing.CODEX_MODEL, "fallback must differ from the default"
    assert pricing.CODEX_FALLBACK_MODEL in pricing.PRICES, "fallback must be priced"
    assert pricing.CODEX_FALLBACK_MODEL in pricing.dispatch_models(), "fallback must be in the priced-coverage set"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nall {len(fns)} codex-fallback checks passed")
