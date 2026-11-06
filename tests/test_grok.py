#!/usr/bin/env python3
"""Native Grok Build reviewer: model id validation."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import reviewers  # noqa: E402


def test_exact_grok_model():
    assert reviewers.exact_grok_model("  grok-4.7 ") == "grok-4.7"
    for bad in ("", "   ", "x-ai/grok-4.3"):
        try:
            reviewers.exact_grok_model(bad)
            assert False, bad
        except ValueError as exc:
            if "/" in bad:
                assert "grok-openrouter" in str(exc), str(exc)


if __name__ == "__main__":
    test_exact_grok_model()
    print("ok  test_exact_grok_model")
