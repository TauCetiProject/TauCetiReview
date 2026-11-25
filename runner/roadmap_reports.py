"""The form of the roadmap reports in a PR description, for the correctness rubric's `roadmap-report`
clause (admission_policy.PHASED_CLAUSES).

A Tau Ceti worker that finds its roadmap target's statement wrong leaves, in its PR description, a
`### Roadmap report` section and one marker line per error,

    <!--tauceti-roadmap-report:v1 {"roadmap": ..., "file": ..., "roadmap_sha": ..., "anchor": ...,
                                   "kind": ..., "effect": ..., "refutes": [...], "replaces": [...]}-->

which the roadmap job board harvests once the PR merges. Neither the worker's PR wrapper nor the
board refuses a malformed marker (the one warns, the other skips it), so the review runner checks
the form here and tells the reviewer, who asks for the fix.

A marker runs from the literal prefix to the next `-->` and is one JSON object; at most
MAX_MARKERS count, and unknown fields are ignored. A marker is valid here only when both the
worker's checker (TauCetiWorker, tauceti_worker/roadmap_reports.py) and the board's reader
(TauCetiRoadmapWorker, tauceti_roadmap_worker/reports.py) accept it, so where they differ the
stricter rule is applied: `roadmap` and `roadmap_sha` exactly as written (no surrounding space, a
lowercase SHA), the anchor's length counted before whitespace is normalised, and declaration names
as dot-separated parts with no space, backtick, `<`, `>` or `@`.

What is not checked: that the anchor occurs in the named file at `roadmap_sha`. The reviewer's
roadmap checkout is a shallow copy of main without its history, so that commit is not there; the
board checks the anchor when it harvests.

Every message is fixed text and marker numbers: nothing from the description reaches the trusted
block the reviewer reads, so a description cannot forge or extend it.
"""
import json
import re

PREFIX = "<!--tauceti-roadmap-report:v1 "
END = "-->"
HEADING = "### Roadmap report"
MAX_MARKERS = 5
MAX_ANCHOR = 300
MAX_NAMES = 10
MAX_NAME = 200
FILES = ("README.md", "Suggested.lean")
KINDS = ("false-statement", "missing-hypothesis", "unprovable-pin", "wrong-definition",
         "circular-dependency", "stale-name")
EFFECTS = ("blocked", "worked-around")

_SEGMENT = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_SHA = re.compile(r"[0-9a-f]{40}")
_NAME = re.compile(r"[^\s.`<>@]+(?:\.[^\s.`<>@]+)*")


def field_problems(value):
    """Why a decoded marker is not a valid version 1 report, as fixed messages; empty when it is."""
    if not isinstance(value, dict):
        return ["it is not a JSON object"]
    found = []
    roadmap = value.get("roadmap")
    if not isinstance(roadmap, str) or not all(_SEGMENT.fullmatch(s) for s in roadmap.split("/")):
        found.append("`roadmap` must be the roadmap's folder: `/`-separated segments matching "
                     "`[A-Za-z][A-Za-z0-9]*`")
    if value.get("file") not in FILES:
        found.append("`file` must be `README.md` or `Suggested.lean`")
    sha = value.get("roadmap_sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        found.append("`roadmap_sha` must be the 40-character lowercase hex roadmap commit read")
    anchor = value.get("anchor")
    if not isinstance(anchor, str) or not anchor.strip():
        found.append("`anchor` must be a non-empty string")
    elif len(anchor) > MAX_ANCHOR:
        found.append(f"`anchor` must be at most {MAX_ANCHOR} characters")
    if value.get("kind") not in KINDS:
        found.append("`kind` must be one of " + ", ".join(f"`{k}`" for k in KINDS))
    if value.get("effect") not in EFFECTS:
        found.append("`effect` must be `blocked` or `worked-around`")
    for key in ("refutes", "replaces"):
        names = value.get(key, [])
        if not isinstance(names, list) or len(names) > MAX_NAMES:
            found.append(f"`{key}`, if given, must be a list of at most {MAX_NAMES} names")
        elif not all(isinstance(n, str) and len(n) <= MAX_NAME and _NAME.fullmatch(n) for n in names):
            found.append(f"`{key}` must hold fully qualified declaration names")
    return found


def check(body):
    """(number of valid markers, problems) for a PR description. Each problem is fixed text: a
    marker's number and what is wrong with it, or a missing `### Roadmap report` section."""
    body = body or ""
    valid, problems, start, n = 0, [], 0, 0
    while (begin := body.find(PREFIX, start)) >= 0:
        n += 1
        end = body.find(END, begin + len(PREFIX))
        if end < 0:
            problems.append(f"marker {n}: no closing `-->`")
            break
        start = end + len(END)
        if n > MAX_MARKERS:
            problems.append(f"marker {n}: a description carries at most {MAX_MARKERS} markers")
            continue
        try:
            value = json.loads(body[begin + len(PREFIX):end])
        except (ValueError, RecursionError):  # deep nesting raises the latter
            problems.append(f"marker {n}: not one JSON object (the JSON may not contain `-->`)")
            continue
        found = field_problems(value)
        problems.extend(f"marker {n}: {p}" for p in found)
        valid += not found
    if n and not any(line.strip() == HEADING for line in body.splitlines()):
        problems.append(f"no `{HEADING}` section saying what is wrong, why, and the correction")
    return valid, problems


def description_body(text):
    """The body of a runner-written description file, `# <title>`, a blank line, then the body:
    the board reads markers from the body only."""
    if text.startswith("# "):
        return text.split("\n", 1)[1] if "\n" in text else ""
    return text


def summary(body):
    """The runner's account of the description's markers, for the trusted block. `body` None: the
    runner had no description to read."""
    head = "Roadmap report markers in the PR description, checked by the runner for form only: "
    if body is None:
        return head + "not checked, the runner had no PR description.\n"
    valid, problems = check(body)
    if not problems:
        return head + (f"{valid} found, all valid.\n" if valid else "none found.\n")
    return (head + f"{valid} valid; problems to fix:\n"
            + "".join(f"- {p}\n" for p in problems))
