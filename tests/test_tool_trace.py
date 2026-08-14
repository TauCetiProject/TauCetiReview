#!/usr/bin/env python3
"""A claude review records which tools it used, not just what it concluded.

`run_claude` kept only the final answer: `--output-format json` returns one document whose `result`
is the review, and the session file lands in the throwaway HOME that `cleanup_rev_home` deletes. So
nothing anywhere said whether a finding came from reading the code or from the model's recollection
of it. That matters because "verify before you assert: name the declaration and show the `grep` hit"
is the central instruction of `rubrics/_common.md`, and it was unfalsifiable.

`--output-format stream-json --verbose` emits the same terminal `result` event plus the events
leading to it. Pinned here:

  1. The trace names each tool call and the thing it was ABOUT (a path, a pattern), in order.
  2. It never carries what a call RETURNED. The store and the archive are both public, so file
     contents there would republish the PR under review.
  3. It is bounded, so one grep-happy rubric cannot bloat a persisted record.
  4. Parsing is tolerant: interleaved non-JSON, a malformed line, and a stream that never produced a
     result event all behave, the last falling through to the existing parse_error path.
  5. Every field the old json format supplied still arrives from the terminal event.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import reviewers  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def tool_use(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


RESULT = {
    "type": "result", "subtype": "success", "is_error": False,
    "result": "TAUCETI-VERDICT-abc\n{\"verdict\": \"approve\"}",
    "total_cost_usd": 1.25, "usage": {"input_tokens": 7}, "session_id": "s-1",
}


def fake_sh(stdout, rc=0, stderr=""):
    class R:
        returncode, stdout, stderr = rc, "", ""
    R.stdout, R.returncode, R.stderr = stdout, rc, stderr
    return lambda *a, **k: R


def main():
    events = [
        {"type": "system", "subtype": "init"},
        assistant({"type": "text", "text": "checking"}, tool_use("Grep", pattern="index_comp", path="./mathlib")),
        assistant(tool_use("Read", file_path="./code/TauCeti/Fredholm/Index.lean")),
        assistant(tool_use("Glob", pattern="**/Index.lean")),
        RESULT,
    ]
    stdout = "\n".join(json.dumps(e) for e in events)

    orig = reviewers.sh
    try:
        # 1/2/5) a normal run
        reviewers.sh = fake_sh(stdout)
        out = reviewers.run_claude("prompt", ".", "claude-opus-5", {})
        check("the verdict text still arrives", out["text"].startswith("TAUCETI-VERDICT-"))
        for k, v in (("cost_usd", 1.25), ("session_id", "s-1"), ("is_error", False)):
            check(f"{k} still arrives from the terminal event", out.get(k) == v)
        trace = out.get("tool_trace")
        check("a trace is recorded", trace == [
            "Grep index_comp",
            "Read ./code/TauCeti/Fredholm/Index.lean",
            "Glob **/Index.lean",
        ])
        blob = json.dumps(out)
        check("the trace carries no tool OUTPUT", "checking" not in json.dumps(trace))
        check("no file contents leak into the result", "TauCeti/Fredholm/Index.lean" in blob)

        # 3) bounded
        many = [assistant(tool_use("Grep", pattern=f"p{i}")) for i in range(200)]
        reviewers.sh = fake_sh("\n".join(json.dumps(e) for e in many + [RESULT]))
        out = reviewers.run_claude("p", ".", "m", {})
        check("the trace is bounded", len(out["tool_trace"]) == reviewers._MAX_TOOL_TRACE)

        # an over-long argument is clipped rather than persisted whole
        reviewers.sh = fake_sh("\n".join(json.dumps(e) for e in
                                         [assistant(tool_use("Grep", pattern="x" * 5000)), RESULT]))
        out = reviewers.run_claude("p", ".", "m", {})
        check("a huge argument is clipped", len(out["tool_trace"][0]) < 200)

        # 4) tolerant parsing
        noisy = "warming up\n" + json.dumps(events[1]) + "\n{ not json\n" + json.dumps(RESULT) + "\n"
        reviewers.sh = fake_sh(noisy)
        out = reviewers.run_claude("p", ".", "m", {})
        check("interleaved noise and a bad line are skipped", out["text"].startswith("TAUCETI-VERDICT-"))
        check("the trace survives the noise", out["tool_trace"] == ["Grep index_comp"])

        reviewers.sh = fake_sh(json.dumps(events[1]), rc=1, stderr="boom")
        out = reviewers.run_claude("p", ".", "m", {})
        check("a stream with no result event is a parse_error", bool(out.get("parse_error")))
        check("...and reports no text", out["text"] == "")
        check("...and keeps the stderr for diagnosis", out["raw_stderr"] == "boom")

        # the flags actually asked for
        seen = {}
        def capture(argv, **kw):
            seen["argv"] = argv
            return fake_sh(stdout)(argv, **kw)
        reviewers.sh = capture
        reviewers.run_claude("p", ".", "m", {})
        check("stream-json is requested", "stream-json" in seen["argv"] and "--verbose" in seen["argv"])
        check("the tool set is still read-only",
              set(seen["argv"][seen["argv"].index("--allowedTools") + 1:]) == {"Read", "Grep", "Glob"})
    finally:
        reviewers.sh = orig

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
