#!/usr/bin/env python3
"""Native Grok Build reviewer: model id validation, command shape, stream parsing, fail-closed tools."""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import types

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


def _stream(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


_READ_TOOLS_EVENT = {"type": "available_commands", "tools": ["read_file", "list_dir", "grep"],
                     "commands": ["compact", "context"]}

_END = {"type": "end", "stopReason": "end_turn", "sessionId": "s1",
        "usage": {"input_tokens": 1000, "cache_read_input_tokens": 2000,
                  "cache_creation_input_tokens": 0, "output_tokens": 100,
                  "reasoning_tokens": 50, "total_tokens": 3100},
        "num_turns": 2, "total_cost_usd": 0.00136,
        "modelUsage": {"grok-4.7-build": {"inputTokens": 1000, "outputTokens": 100,
                                          "cacheReadInputTokens": 2000,
                                          "cacheCreationInputTokens": 0,
                                          "modelCalls": 2, "costUSD": 0.00136}}}


def _run(stdout, returncode=0, stderr="", prompt="PROMPT"):
    """Run run_grok against a faked `sh`, returning (out, captured)."""
    captured = {}
    home = tempfile.mkdtemp()

    def fake_sh(cmd, **kwargs):
        captured.update(cmd=list(cmd), kwargs=kwargs)
        path = cmd[cmd.index("--prompt-file") + 1]
        captured["prompt_path"] = path
        with open(path) as f:
            captured["prompt_at_call"] = f.read()
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    original = reviewers.sh
    reviewers.sh = fake_sh
    try:
        out = reviewers.run_grok(prompt, "/repo", "grok-4.7", {"HOME": home})
    finally:
        reviewers.sh = original
        shutil.rmtree(home, ignore_errors=True)
    captured["home"] = home
    return out, captured


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_run_grok_command_shape():
    out, cap = _run(_stream(_READ_TOOLS_EVENT, {"type": "text", "data": "ok"}, _END))
    cmd = cap["cmd"]
    assert cmd[:2] == ["grok", "--prompt-file"], cmd
    assert os.path.commonpath([cap["prompt_path"], cap["home"]]) == cap["home"]
    assert cap["prompt_at_call"] == "PROMPT"
    assert _flag(cmd, "-m") == "grok-4.7"
    assert _flag(cmd, "--output-format") == "streaming-json"
    assert _flag(cmd, "--permission-mode") == "dontAsk"
    for flag in ("--no-subagents", "--no-plan", "--disable-web-search", "--no-memory"):
        assert flag in cmd, flag
    assert int(_flag(cmd, "--max-turns")) > 0
    assert "stdin_text" not in cap["kwargs"]
    assert cap["kwargs"].get("cwd") == "/repo"
    assert "error_message" not in out and not out.get("is_error")


def test_run_grok_rejects_openrouter_ids():
    try:
        reviewers.run_grok("PROMPT", "/repo", "x-ai/grok-4.3", {"HOME": "/nonexistent"})
        assert False
    except ValueError as exc:
        assert "grok-openrouter" in str(exc)


def test_run_grok_parses_stream():
    stream = _stream(
        _READ_TOOLS_EVENT,
        {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}},
        {"type": "tool_call", "toolCallId": "c1", "title": "read_file", "kind": "read",
         "status": "pending", "toolName": "read_file", "rawInput": {"target_file": "/repo/x.py"}},
        {"type": "tool_call_update", "toolCallId": "c1", "status": None, "content": []},
        {"type": "tool_call_update", "toolCallId": "c1", "status": "completed",
         "content": [{"type": "content", "content": {"type": "text", "text": "SECRET FILE"}}]},
        {"type": "thought", "data": "THINKING"},
        {"type": "text", "data": "a"},
        {"type": "text", "data": "b"},
        _END,
    )
    out, _ = _run(stream)
    assert out["returncode"] == 0
    assert out["text"] == "ab", out["text"]
    assert out["session_id"] == "s1"
    assert out["tool_trace"] == [{"tool": "read_file", "ok": True}], out["tool_trace"]
    assert out["tool_trace_meta"] == {"total_calls": 1, "successful_calls": 1,
                                      "trace_truncated": False}, out["tool_trace_meta"]
    assert out["cost_usd"] == round((1000 * 2 + 2000 * 0.5 + 100 * 6) / 1e6, 6) == 0.0036
    assert out["cost_estimated"] is True
    assert out["provider_cost_usd"] == 0.00136
    assert out["usage"]["cache_read_input_tokens"] == 2000
    assert "price_inherited" not in out
    assert "error_message" not in out and "raw_stdout" not in out
    assert not out.get("is_error")
    assert "raw_stderr" in out


def test_run_grok_failed_tool_call_is_not_ok():
    stream = _stream(
        _READ_TOOLS_EVENT,
        {"type": "tool_call", "toolCallId": "c1", "toolName": "grep", "status": "pending"},
        {"type": "tool_call_update", "toolCallId": "c1", "status": "failed"},
        {"type": "text", "data": "v"},
        _END,
    )
    out, _ = _run(stream)
    assert out["tool_trace"] == [{"tool": "grep", "ok": False}], out["tool_trace"]
    assert out["tool_trace_meta"]["successful_calls"] == 0


def test_run_grok_trace_is_capped():
    events = [_READ_TOOLS_EVENT]
    n = reviewers._MAX_TOOL_TRACE + 5
    for i in range(n):
        events.append({"type": "tool_call", "toolCallId": f"c{i}", "toolName": "list_dir"})
        events.append({"type": "tool_call_update", "toolCallId": f"c{i}", "status": "completed"})
    events += [{"type": "text", "data": "v"}, _END]
    out, _ = _run(_stream(*events))
    assert len(out["tool_trace"]) == reviewers._MAX_TOOL_TRACE
    assert out["tool_trace_meta"] == {"total_calls": n, "successful_calls": n,
                                      "trace_truncated": True}


def test_run_grok_prices_top_level_usage_without_model_usage():
    end = dict(_END)
    del end["modelUsage"]
    out, _ = _run(_stream(_READ_TOOLS_EVENT, {"type": "text", "data": "v"}, end))
    assert out["cost_usd"] == 0.0036, out["cost_usd"]


def test_run_grok_records_inherited_price():
    end = dict(_END, modelUsage={"grok-9-unpriced": {"inputTokens": 10, "outputTokens": 1,
                                                       "cacheReadInputTokens": 0}})
    out, _ = _run(_stream(_READ_TOOLS_EVENT, {"type": "text", "data": "v"}, end))
    assert out.get("price_inherited"), out


def _assert_failed_closed(out, tool):
    assert tool in out.get("error_message", ""), out.get("error_message")
    assert out["is_error"] is True
    assert out["text"] == ""
    assert "raw_stdout" in out and "raw_stderr" in out


def test_run_grok_fails_closed_on_unexpected_tools():
    # use_tool exposed in the effective tool set: a normal-looking run is still a failure.
    widened = dict(_READ_TOOLS_EVENT, tools=["read_file", "list_dir", "grep", "use_tool"])
    verdict = {"type": "text", "data": "TAUCETI-VERDICT {\"ok\": true}"}
    out, _ = _run(_stream(widened, verdict, _END))
    assert out["returncode"] == 0
    _assert_failed_closed(out, "use_tool")
    # A later widening counts too, not only the first event.
    out, _ = _run(_stream(_READ_TOOLS_EVENT, verdict, widened, _END))
    _assert_failed_closed(out, "use_tool")
    # A tool_call naming something outside the read set.
    call = {"type": "tool_call", "toolCallId": "c9", "toolName": "run_terminal_cmd",
            "rawInput": {"command": "env"}}
    out, _ = _run(_stream(_READ_TOOLS_EVENT, call, verdict, _END))
    _assert_failed_closed(out, "run_terminal_cmd")
    # The command is the model's own words and must not be echoed into error_message.
    assert "env" not in out["error_message"].replace("run_terminal_cmd", "")


def test_run_grok_error_event_is_a_failure():
    out, _ = _run(_stream({"type": "text", "data": "partial"}, {"type": "error", "message": "boom"}))
    assert out["error_message"] == "boom", out
    assert "raw_stdout" in out
    assert out["is_error"] is True
    out, _ = _run(_stream(_READ_TOOLS_EVENT, {"type": "text", "data": "partial"}))
    assert out.get("error_message"), out
    assert "raw_stdout" in out
    assert out["is_error"] is True


def test_run_grok_nonzero_exit_keeps_tails():
    out, _ = _run("not json\n", returncode=1, stderr="auth failed")
    assert out["returncode"] == 1
    assert out["raw_stderr"] == "auth failed"
    assert out["raw_stdout"] == "not json\n"
    assert out.get("error_message")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
