#!/usr/bin/env python3
"""The claude reviewer has no shell, and no way to reach the network.

This is the property the rest of the module is built on and it was not true. `run_claude` passed
`--allowedTools Read Grep Glob`, which grants permission WITHIN whatever tool set exists; it does
not restrict the set. The reviewer therefore still had Bash, and used it: of 427 traced tool calls
across 20 rubric runs, 295 were Bash and 278 of those succeeded. Reproduced directly, in the shape
reviewer_env builds — `claude -p --disable-slash-commands --allowedTools Read Grep Glob` answering
"SHELL_IS_AVAILABLE".

Why it matters, in this module's own words. reviewer_env: the isolation is load-bearing "with
public transcripts and no redaction gate, a prompt-injected reviewer must have nothing worth
leaking", with the residual being that "a reviewer can still read its OWN key via
/proc/self/environ". A shell, plus the egress a reviewer needs to reach its provider, turns that
residual into a direct exfiltration path. run_pi already states the intended property for the
OpenRouter reviewers: "a read-only tool set (PI_TOOLS, no bash) means it has no shell to leak it
with."

The assertions below are about the SET, not about a flag's spelling, so a future rewrite that keeps
the tools read-only passes and one that quietly readmits a shell does not.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import reviewers  # noqa: E402

fails = 0

# Anything that runs a command, writes a file, or leaves the machine. A reviewer needs none of them:
# the rubrics ask it to read the diff and grep the vendored sources, both of which are file reads.
FORBIDDEN = {
    "Bash", "BashOutput", "KillShell", "Shell", "Execute",
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Create",
    "WebFetch", "WebSearch", "Fetch",
    "Task", "Agent", "ToolSearch",
}


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def captured_argv():
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "", ""

    orig = reviewers.sh
    reviewers.sh = lambda argv, **kw: (seen.update(argv=list(argv)), R)[1]
    try:
        reviewers.run_claude("prompt", ".", "claude-opus-5", {})
    finally:
        reviewers.sh = orig
    return seen["argv"]


def flag_values(argv, flag):
    """The values following `flag`, up to the next flag."""
    if flag not in argv:
        return None
    out = []
    for tok in argv[argv.index(flag) + 1:]:
        if tok.startswith("--"):
            break
        out.append(tok)
    return out


def main():
    argv = captured_argv()

    # 1) The tool SET is restricted, not merely permitted. Without this the reviewer has a shell.
    tools = flag_values(argv, "--tools")
    check("the built-in tool set is restricted", tools is not None)
    check(f"...to read-only tools ({tools})", set(tools or []) == set(reviewers._REVIEW_TOOLS))
    check("no shell is in the set", not (set(tools or []) & FORBIDDEN))

    # 2) And the remaining tools still do not prompt, or a headless round stalls on the first read.
    allowed = flag_values(argv, "--allowedTools")
    check("the remaining tools are permitted", set(allowed or []) == set(tools or []))

    # 3) The declared set itself carries nothing that runs, writes, or fetches.
    check("the declared review tools are read-only", not (set(reviewers._REVIEW_TOOLS) & FORBIDDEN))
    check("...and are exactly read/search/list", set(reviewers._REVIEW_TOOLS) == {"Read", "Grep", "Glob"})

    # 4) Nothing else in the launch quietly re-grants what the set removed.
    for danger in ("--dangerously-skip-permissions", "--permission-mode"):
        check(f"the launch does not pass {danger}", danger not in argv)
    check("slash commands stay disabled", "--disable-slash-commands" in argv)

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
