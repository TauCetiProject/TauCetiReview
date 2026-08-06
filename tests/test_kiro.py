#!/usr/bin/env python3
"""Kiro reviews pin the requested model and expose read-only tools only."""

import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import reviewers  # noqa: E402
import pricing  # noqa: E402


def test_run_kiro_exact_model_and_read_only_tools():
    captured = {}

    def fake_sh(cmd, **kwargs):
        captured.update(cmd=cmd, kwargs=kwargs)
        return types.SimpleNamespace(returncode=0, stdout="verdict", stderr="")

    original = reviewers.sh
    reviewers.sh = fake_sh
    try:
        out = reviewers.run_kiro("PROMPT", "/repo", "claude-opus-4.8", {"HOME": "/tmp/h"})
    finally:
        reviewers.sh = original

    assert out["text"] == "verdict"
    assert captured["kwargs"]["stdin_text"] == "PROMPT"
    assert captured["cmd"][:3] == ["kiro-cli", "chat", "--no-interactive"]
    assert "--trust-tools=read,grep,glob" in captured["cmd"]
    assert "--trust-all-tools" not in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "claude-opus-4.8"
    assert "claude-opus-4.8" not in pricing.dispatch_models()


def test_run_kiro_rejects_auto_router():
    for model in ("", "Auto", "auto-premium"):
        try:
            reviewers.run_kiro("PROMPT", "/repo", model, {"HOME": "/tmp/h"})
            assert False, model
        except ValueError as exc:
            assert "Auto" in str(exc)


if __name__ == "__main__":
    test_run_kiro_exact_model_and_read_only_tools()
    print("ok  test_run_kiro_exact_model_and_read_only_tools")
    test_run_kiro_rejects_auto_router()
    print("ok  test_run_kiro_rejects_auto_router")
