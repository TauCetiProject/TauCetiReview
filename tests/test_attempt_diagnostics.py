#!/usr/bin/env python3
"""A failed rubric says why it failed, and says it without publishing provider output.

TauCetiReview#105: a total auth failure rendered as verdict=error, returncode=1, $0.00, on a
scoreboard headed "changes requested". Nothing in any artifact said "Not logged in", so two PRs
carried that scoreboard before anyone looked. The fix is a classified `error_kind` -- a closed
vocabulary, publishable anywhere -- rather than the raw stderr it is derived from.

The raw stderr must not be published, and BOTH persisted sinks are public: the archive record goes
to TauCetiData, and `--store` is a checkout of this repo's `reviews` branch which the review
workflow commits and pushes. Until now the per-rubric store record carried `raw_stderr` and
`session_id` at top level and did leave the machine. These tests pin the stripper that closes that,
including at depth, since attempts are nested inside the record.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import review  # noqa: E402


NOT_LOGGED_IN = "Not logged in · Please run /login"
SECRET = "sk-ant-SENTINEL-must-not-be-published"
SESSION = "019e8fe4-41ce-7cd2-b837-6b641bf22984"


def realistic_result():
    """The shape run_rubric persists, with both private fields at both depths."""
    return {
        "returncode": 1, "cost_usd": 0.0, "provider": "claude", "model": "claude-opus-5",
        "rubric": "correctness", "run_id": "r-2026-1384-correctness-abc123",
        "raw_stderr": f"{SECRET}\n{NOT_LOGGED_IN}\n", "session_id": SESSION,
        "text": "", "verdict_obj": None,
        "attempts": [
            {"returncode": 1, "secs": 0.9, "model": "claude-opus-5", "error_kind": "not_authenticated",
             "session_id": SESSION, "raw_stderr": f"{SECRET}\n{NOT_LOGGED_IN}"},
            {"returncode": 1, "secs": 0.8, "model": "claude-opus-5", "error_kind": "not_authenticated",
             "session_id": SESSION, "raw_stderr": NOT_LOGGED_IN},
        ],
    }


def test_public_record_strips_at_every_depth():
    published = json.dumps(review.public_record(realistic_result()))

    assert SECRET not in published, "provider stderr reached a published record"
    assert SESSION not in published, "a session id reached a published record"
    assert NOT_LOGGED_IN not in published
    assert "raw_stderr" not in published and "session_id" not in published


def test_public_record_keeps_everything_else():
    pub = review.public_record(realistic_result())

    assert pub["returncode"] == 1 and pub["rubric"] == "correctness"
    assert pub["run_id"] == "r-2026-1384-correctness-abc123"
    # The diagnosis survives, which is the whole point: this is what #105 needed and did not have.
    assert [a["error_kind"] for a in pub["attempts"]] == ["not_authenticated", "not_authenticated"]
    assert [a["secs"] for a in pub["attempts"]] == [0.9, 0.8]


def test_public_record_handles_shapes_it_may_meet():
    assert review.public_record([{"raw_stderr": "x"}, {"keep": 1}]) == [{}, {"keep": 1}]
    assert review.public_record({"a": [{"b": {"session_id": "s", "c": 2}}]}) == {"a": [{"b": {"c": 2}}]}
    assert review.public_record("plain") == "plain"
    assert review.public_record(None) is None


def test_error_kind_names_the_105_failure():
    assert review.error_kind({"returncode": 1, "raw_stderr": NOT_LOGGED_IN}) == "not_authenticated"


def test_error_kind_vocabulary_is_closed():
    cases = {
        "API Error: 529 Overloaded": "overloaded",
        "API Error: 429 Too Many Requests": "rate_limited",
        "request timed out after 600s": "timed_out",
        "The 'gpt-5.6-sol' model is not supported when using Codex": "model_unavailable",
        "Error: read ECONNRESET": "transport",
        "error: something nobody has seen before": "unknown_error",
    }
    for stderr, want in cases.items():
        assert review.error_kind({"returncode": 1, "raw_stderr": stderr}) == want, stderr
    # Exited cleanly but emitted nothing parseable: a different failure from a crash.
    assert review.error_kind({"returncode": 0, "raw_stderr": ""}) == "no_verdict"
    every_kind = {k for k, _ in review._ERROR_KINDS} | {"unknown_error", "no_verdict"}
    assert all(review.error_kind({"returncode": 1, "raw_stderr": s}) in every_kind for s in cases)


def test_stderr_summary_takes_the_last_operative_line():
    assert review.stderr_summary({"raw_stderr": f"warming up\n{NOT_LOGGED_IN}\n\n"}) == NOT_LOGGED_IN
    assert review.stderr_summary({"raw_stderr": "x" * 5000}) == "x" * 200
    assert review.stderr_summary({"raw_stderr": "   \n\n  "}) == ""
    assert review.stderr_summary({}) == ""


def test_private_keys_cover_every_raw_provider_field():
    # raw_stdout joined the set when the claude reviewer moved to stream-json: its tail is the last
    # events of the stream, and a tool_result block carries the file the reviewer just read. Both
    # persisted sinks are public, so no raw provider stream may reach either.
    assert set(review.PRIVATE_KEYS) == {"session_id", "raw_stderr", "raw_stdout"}
    published = review.public_record(
        {"raw_stdout": "ANTHROPIC_API_KEY=sk-ant-SENTINEL", "attempts": [{"raw_stdout": "sk-ant-SENTINEL"}]}
    )
    assert "SENTINEL" not in json.dumps(published)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} diagnostic and publication-boundary checks passed")
