#!/usr/bin/env python3
"""Opt-in real CLI capture against a local fake API; no paid calls.

Run this script against the fixed checkout normally. To reproduce the old failure,
add --expect-limit-error --fork-cache-pin --review-root /path/to/unchanged-main.
Only newly created synthetic private configuration is used for the feature gate.
"""

import argparse
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit


def breakpoints(value):
    if isinstance(value, dict):
        return int("cache_control" in value) + sum(breakpoints(v) for v in value.values())
    if isinstance(value, list):
        return sum(map(breakpoints, value))
    return 0


def require(condition, report):
    if not condition:
        raise RuntimeError(f"native fixture acceptance failed: {report}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", type=pathlib.Path, required=True)
    parser.add_argument("--review-root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1])
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--expect-limit-error", action="store_true",
                        help="expect old transport failure; --review-root must select an unchanged checkout")
    parser.add_argument("--auth-mode", choices=("api", "subscription"), default="api")
    parser.add_argument("--tool-turns", type=int, default=3)
    parser.add_argument("--fork-cache-pin", action="store_true", help="exercise cached CLI feature gate in a fresh synthetic HOME")
    parser.add_argument("--timeout", type=float, default=120, help="seconds allowed for each native CLI process")
    args = parser.parse_args()
    if args.timeout <= 0 or args.tool_turns < 1:
        parser.error("--timeout must be positive and --tool-turns at least 1")
    binary = args.claude.resolve(strict=True)
    sys.path.insert(0, str(args.review_root / "runner"))
    import reviewers

    counts, roles, user_roles, errors = [], [], [], []
    with tempfile.TemporaryDirectory(prefix="review-native-cache-") as temporary:
        root = pathlib.Path(temporary)
        bindir = root / "bin"
        bindir.mkdir()
        (bindir / "claude").symlink_to(binary)
        # run_claude and legacy version probes both find this exact binary, even when
        # the supplied versioned executable is not itself named "claude".
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
        require(pathlib.Path(shutil.which("claude")).resolve() == binary, "wrong CLI binary on PATH")
        def bounded_sh(cmd, cwd=None, env=None, stdin_text=None):
            return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True,
                                  input=stdin_text, stdin=None if stdin_text is not None else subprocess.DEVNULL,
                                  timeout=args.timeout)
        reviewers.sh = bounded_sh
        (root / "fixture.txt").write_text("LOCAL_TOOL_RESULT_SENTINEL\n")

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                path = urlsplit(self.path).path.rstrip("/")
                if path.endswith("/messages/count_tokens"):
                    payload = b'{"input_tokens":2000}'
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if not path.endswith("/messages"):
                    self.send_error(404, "fixture only serves Messages API requests")
                    return
                count = breakpoints(body)
                counts.append(count)
                roles.append("USER_PR_SENTINEL" not in json.dumps(body.get("system", "")))
                user_roles.append(any(message.get("role") == "user" and "USER_PR_SENTINEL" in json.dumps(message.get("content", ""))
                                      for message in body.get("messages", []) if isinstance(message, dict)))
                if count > 4:
                    errors.append(count)
                    payload = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message":
                        f"A maximum of 4 blocks with cache_control may be provided. Found {count}."}}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                tool = len(counts) <= args.tool_turns
                events = [{"type": "message_start", "message": {"id": f"msg_{len(counts)}", "type": "message",
                    "role": "assistant", "model": body["model"], "content": [], "stop_reason": None,
                    "stop_sequence": None, "usage": {"input_tokens": 2000, "output_tokens": 1}}}]
                events.extend([
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "I will inspect the file." if tool else "LOCAL_REVIEW_OK"}},
                    {"type": "content_block_stop", "index": 0},
                ])
                if tool:
                    events.extend([
                        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": f"toolu_local_read_{len(counts)}", "name": "Read", "input": {}}},
                        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"file_path": str(root / "fixture.txt")})}},
                        {"type": "content_block_stop", "index": 1},
                    ])
                events.extend([
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn",
                        "stop_sequence": None}, "usage": {"output_tokens": 10}},
                    {"type": "message_stop"},
                ])
                data = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env, home = reviewers.reviewer_env("claude", {"anthropic": "local-test-only"})
        try:
            env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}", DISABLE_TELEMETRY="1",
                       DISABLE_ERROR_REPORTING="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
            if args.fork_cache_pin:
                config = {"cachedGrowthBookFeatures": {"tengu_basalt_spur": True}}
                for directory in (pathlib.Path(home), pathlib.Path(home) / ".claude"):
                    directory.mkdir(mode=0o700, exist_ok=True)
                    (directory / ".claude.json").write_text(json.dumps(config))
                env["CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF"] = "1"
            if args.auth_mode == "subscription":
                env.pop("ANTHROPIC_API_KEY", None)
                env["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-oat01-local-test-only"
            prompt = reviewers.ReviewPrompt("USER_PR_SENTINEL: untrusted PR context\n", "Read fixture.txt and review this angle.")
            result = reviewers.run_claude(prompt, str(root), "claude-opus-5-5", env)
            version = reviewers.subprocess.run([str(args.claude), "--version"], capture_output=True, text=True, check=True, timeout=args.timeout).stdout.strip()
            report = {"cli": version, "resolved_cli": str(binary), "auth_mode": args.auth_mode, "fork_cache_pin": args.fork_cache_pin, "breakpoints_per_request": counts, "rejected_counts": errors,
                      "pr_absent_from_system": bool(roles) and all(roles),
                      "pr_present_in_user": bool(user_roles) and all(user_roles), "tool_trace": result.get("tool_trace", []),
                      "returncode": result.get("returncode"), "is_error": result.get("is_error"),
                      "local_review_reply": result.get("text") == "LOCAL_REVIEW_OK", "paid_calls": 0}
            args.report.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))
            require(len(counts) >= 2 and all(roles) and all(user_roles), report)
            require(result.get("tool_trace_meta", {}).get("successful_calls", 0) >= 1, report)
            if args.expect_limit_error:
                require(bool(errors) and not report["local_review_reply"], report)
            else:
                require(not errors and max(counts) <= 4 and report["local_review_reply"] and result["returncode"] == 0, report)
        finally:
            reviewers.cleanup_rev_home(home)
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
