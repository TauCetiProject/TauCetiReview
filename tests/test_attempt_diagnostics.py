#!/usr/bin/env python3
"""A failed attempt keeps its diagnosis locally and publishes none of it.

Regression cover for the second half of TauCetiReview#105: every artifact a failed rubric produced
recorded `returncode`, `secs` and `usage` at $0.00 and no error text, so a total auth failure was
indistinguishable from a model that answered nothing. The stderr line now rides in each attempt,
and must still never reach the public archive record.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import review  # noqa: E402


NOT_LOGGED_IN = "Not logged in · Please run /login"


def test_stderr_summary_takes_the_last_operative_line():
    res = {"raw_stderr": f"warming up\nfetching config\n{NOT_LOGGED_IN}\n\n"}
    assert review.stderr_summary(res) == NOT_LOGGED_IN


def test_stderr_summary_is_bounded_and_empty_when_silent():
    assert review.stderr_summary({"raw_stderr": "x" * 5000}) == "x" * 200
    assert review.stderr_summary({"raw_stderr": "   \n\n  "}) == ""
    assert review.stderr_summary({}) == ""


def test_archive_projection_drops_private_attempt_fields():
    # The exact comprehension review.py applies when building the durable record.
    attempts = [{"returncode": 1, "secs": 0.9, "usage": {"input_tokens": 0}, "model": "opus",
                 "session_id": "s-abc", "raw_stderr": NOT_LOGGED_IN}]
    published = [{k: v for k, v in at.items() if k not in review.ATTEMPT_PRIVATE_KEYS}
                 for at in attempts]

    assert "raw_stderr" not in published[0], "the archive repo is public"
    assert "session_id" not in published[0]
    assert published[0] == {"returncode": 1, "secs": 0.9, "usage": {"input_tokens": 0},
                            "model": "opus"}
    # ...and the local copy the operator reads still has it.
    assert attempts[0]["raw_stderr"] == NOT_LOGGED_IN


def test_private_keys_cover_both_secrets():
    assert set(review.ATTEMPT_PRIVATE_KEYS) == {"session_id", "raw_stderr"}


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} attempt diagnostic checks passed")
