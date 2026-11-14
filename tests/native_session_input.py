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
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import reviewers
from reviewer_sessions import ClaudeSession, CodexSession


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    provider = parser.parse_args().provider
    captures = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("count_tokens"):
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
            data = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    env, home = reviewers.reviewer_env(provider, {"anthropic": "local-test-only", "openai": "local-test-only"},
                                       round_home=True)
    session = None
    try:
        with tempfile.TemporaryDirectory() as cwd:
            if provider == "claude":
                env.update(ANTHROPIC_BASE_URL=url, DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1")
                cls, model = ClaudeSession, "claude-haiku-4-5"
            else:
                # Use a custom local provider so the captured wire format is the actual CLI's
                # Responses API request without connecting to OpenAI or using a real credential.
                pathlib.Path(env["CODEX_HOME"], "config.toml").write_text(
                    'model_provider="local_capture"\n[model_providers.local_capture]\n'
                    'name="Local capture"\nwire_api="responses"\nrequires_openai_auth=false\n'
                    'supports_websockets=false\nbase_url=' + json.dumps(url + "/v1") + '\n')
                env.pop("OPENAI_API_KEY")
                cls, model = CodexSession, "gpt-6.1-sol"
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
                if i == 2:
                    assert "RUBRIC_SECRET_MARKER_1" not in json.dumps(request)
                    assert "ASSESSMENT_PRIVATE_2" not in json.dumps(request)
                    assert "RUBRIC_SECRET_MARKER_2" in json.dumps(request)
            print(provider, "native model input: PR remains user data; prior rubric and assessment absent")
    finally:
        if session:
            session.close()
        reviewers.cleanup_rev_home(home)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
