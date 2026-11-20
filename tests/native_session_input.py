#!/usr/bin/env python3
"""Opt-in native CLI wire test against a local fake provider; no paid API calls.

Captures actual model input after fork/revert, rather than inferring isolation
from the history listing. Run with --provider claude|codex on a supported CLI.
"""
import argparse
import http.server
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import reviewers
from reviewer_sessions import ClaudeSession, CodexSession, RoundReviewers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    parser.add_argument("--cli", type=pathlib.Path, help="exact native executable; a temporary PATH alias forces its use")
    parser.add_argument("--model", help="exact model used in captured native request")
    parser.add_argument("--reasoning-output", action="store_true", help="emit primer thinking/opaque reasoning and verify independent fallback")
    parser.add_argument("--empty-reasoning-summary", action="store_true", help="Codex opaque reasoning without a visible summary")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    provider = args.provider
    if args.empty_reasoning_summary and (provider != "codex" or not args.reasoning_output):
        parser.error("--empty-reasoning-summary requires --provider codex --reasoning-output")
    captures = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if urlsplit(self.path).path.endswith("count_tokens"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"input_tokens":2000}')
                return
            captures.append(body)
            number = len(captures)
            text = "READY" if number == 1 else f"ASSESSMENT_PRIVATE_{number}"
            if provider == "codex":
                rid = f"resp_{number}"
                events = [
                    {"type": "response.created", "response": {"id": rid}},
                    {"type": "response.output_item.done", "item": {
                        "type": "message", "role": "assistant", "id": f"msg_{number}",
                        "content": [{"type": "output_text", "text": text}]}},
                    {"type": "response.completed", "response": {"id": rid, "usage": {
                        "input_tokens": 2000, "input_tokens_details": {"cached_tokens": 1000},
                        "output_tokens": 5, "output_tokens_details": None, "total_tokens": 2005}}},
                ]
                if args.reasoning_output and number == 1:
                    events.insert(1, {"type": "response.output_item.done", "item": {
                        "type": "reasoning", "id": "reasoning_primer", "summary": [] if args.empty_reasoning_summary else [{"type": "summary_text", "text": "PRIMER_JUDGMENT_SENTINEL"}],
                        "encrypted_content": "OPAQUE_PRIMER_SENTINEL"}})
            else:
                events = [
                    {"type": "message_start", "message": {"id": f"msg_{number}", "type": "message",
                        "role": "assistant", "model": body["model"], "content": [],
                        "stop_reason": None, "stop_sequence": None, "usage": {
                            "input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 1000,
                            "cache_creation_input_tokens": 900}}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 5}},
                    {"type": "message_stop"},
                ]
            if provider == "claude" and args.reasoning_output and number == 1:
                # Real Anthropic stream block shapes, with a synthetic signature; no real model.
                for event in events:
                    if "index" in event:
                        event["index"] += 1
                events[1:1] = [
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "PRIMER_JUDGMENT_SENTINEL"}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SYNTHETIC_SIGNATURE"}},
                    {"type": "content_block_stop", "index": 0},
                ]
            data = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    original_env = reviewers.reviewer_env
    homes = []
    def native_env(name, keys, subscription=False, **kwargs):
        env, home = original_env(name, keys, subscription, **kwargs)
        homes.append(home)
        if provider == "claude":
            env.update(ANTHROPIC_BASE_URL=url, DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1",
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        else:
            pathlib.Path(env["CODEX_HOME"], "config.toml").write_text(
                'model_provider="local_capture"\n[model_providers.local_capture]\n'
                'name="Local capture"\nwire_api="responses"\nrequires_openai_auth=false\n'
                'supports_websockets=false\nbase_url=' + json.dumps(url + "/v1") + '\n')
            env.pop("OPENAI_API_KEY", None)
        return env, home
    session = None
    try:
        with tempfile.TemporaryDirectory() as cwd:
            if args.cli:
                bindir = pathlib.Path(cwd) / "bin";bindir.mkdir()
                (bindir / provider).symlink_to(args.cli.resolve(strict=True))
                os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
                assert pathlib.Path(shutil.which(provider)).resolve() == args.cli.resolve()
            model = args.model or ("claude-opus-5-5" if provider == "claude" else "gpt-6.1-sol")
            if args.reasoning_output:
                with patch.object(reviewers, "reviewer_env", side_effect=native_env):
                    with RoundReviewers({"anthropic": "local-test-only", "openai": "local-test-only"}, False) as rounds:
                        primer_receipts = []
                        prompt = reviewers.ReviewPrompt("SHARED_PR_USER_DATA_ONLY\n", "RUBRIC_SECRET_MARKER_1: review this angle only")
                        first = rounds.run(provider, prompt, cwd, model, on_primer=primer_receipts.append)
                        assert first.get("primer_rejected") and provider in rounds.uncached, first
                        rounds.finish()
                        for i in (1, 2):
                            prompt = reviewers.ReviewPrompt("SHARED_PR_USER_DATA_ONLY\n", f"RUBRIC_SECRET_MARKER_{i}: review this angle only")
                            assert not rounds.needs_primer(provider, prompt, cwd, model)
                            result = rounds.run(provider, prompt, cwd, model, on_primer=primer_receipts.append)
                            assert result.get("returncode") == 0, result
                            rounds.finish()
                        assert len(primer_receipts) == 1
            else:
                env, home = native_env(provider, {"anthropic": "local-test-only", "openai": "local-test-only"}, round_home=True)
                cls = ClaudeSession if provider == "claude" else CodexSession
                session = cls(env, home, cwd, model, "SHARED_PR_USER_DATA_ONLY\n")
                first = session.prime()
                assert session.ready, first.get("error_message", first)
                for i in (1, 2):
                    result = session.run(f"RUBRIC_SECRET_MARKER_{i}: review this angle only")
                    assert result.get("returncode") == 0, result.get("error_message", result)
                    session.reset()
            assert len(captures) == 3, len(captures)
            for i, request in enumerate(captures):
                trusted = json.dumps(request.get("system", request.get("instructions", "")))
                inputs = request.get("messages", request.get("input", []))
                trusted += json.dumps([v for v in inputs if v.get("role") in ("developer", "system")])
                assert "SHARED_PR_USER_DATA_ONLY" not in trusted
                assert "SHARED_PR_USER_DATA_ONLY" in json.dumps([v for v in inputs if v.get("role") == "user"])
                if args.reasoning_output and i > 0:
                    assert "PRIMER_JUDGMENT_SENTINEL" not in json.dumps(request)
                    assert "OPAQUE_PRIMER_SENTINEL" not in json.dumps(request)
                if i == 2:
                    assert "RUBRIC_SECRET_MARKER_1" not in json.dumps(request)
                    assert "ASSESSMENT_PRIVATE_2" not in json.dumps(request)
                    assert "RUBRIC_SECRET_MARKER_2" in json.dumps(request)
            report = {"provider": provider, "model": model, "reasoning_output": args.reasoning_output,
                      "model_requests": len(captures), "empty_reasoning_summary": args.empty_reasoning_summary, "primer_rejected": bool(first.get("primer_rejected")),
                      "thinking_request": captures[0].get("thinking"), "reasoning_request": captures[0].get("reasoning"),
                      "independent_inputs_verified": True, "paid_calls": 0}
            if args.report:
                args.report.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))
    finally:
        if session:
            session.close()
        for home in homes:
            reviewers.cleanup_rev_home(home)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
