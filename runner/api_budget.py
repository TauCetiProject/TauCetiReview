"""Bound sweep work and preserve installation quota for event-driven reviews.

Counts gh invocations, not HTTP pages or GraphQL points. Live quota checks and
rate-limit detection complement the per-run bound. Finish a PR before deferring
the next one so post-admission integrity checks retain their working allowance.
"""
import json
import re
import subprocess


class Exhausted(Exception):
    pass


calls = 0
limit = None
rate_limited = False
failures = 0


def _rate_reason(message):
    message = message.lower()
    for reason, phrases in (
        ("secondary", ("secondary rate limit",)),
        ("graphql", ("rate_limited",)),
        ("primary_or_unspecified", ("api rate limit exceeded", "api rate limit already exceeded")),
        ("http_429", ("(http 429)",)),
    ):
        if any(phrase in message for phrase in phrases):
            return reason
    return None


def _graphql_error_types(stdout):
    """Read GraphQL error types, never search ordinary PR/comment data.

    gh copies API error messages to stderr. REST response objects can also
    contain user-written top-level messages, so do not inspect those at all.
    """
    try:
        data = json.loads(stdout or "null")
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    errors = data.get("errors")
    return [e.get("type") for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []


def _request_label(args):
    """Only emit the API path or CLI verb; omit fields, queries and bodies."""
    if len(args) > 1 and args[1] == "api":
        # These are the endpoint forms used by the sweep. Flags and their
        # arbitrary values are deliberately excluded from diagnostic output.
        values = iter(args[2:])
        for arg in values:
            if arg.startswith("-"):
                if "=" not in arg and arg not in ("--paginate", "--slurp", "--silent", "--include", "-i", "--verbose"):
                    next(values, None)
                continue
            path = arg.split("?", 1)[0].lstrip("/")
            if path in ("graphql", "rate_limit") or re.fullmatch(
                    r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)*", path):
                return "api " + path[:240]
        return "api"
    return " ".join(a for a in args[1:3] if re.fullmatch(r"[a-z-]+", a)) or "gh"


def run(args, **kwargs):
    global calls, rate_limited
    calls += 1
    try:
        result = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("GitHub request timed out") from e
    stderr = result.stderr or ""
    graphql = _request_label(args) == "api graphql"
    error_types = _graphql_error_types(result.stdout) if graphql else []
    status = re.search(r"\bHTTP (\d{3})\b", stderr, re.IGNORECASE)
    reason = _rate_reason(stderr) if result.returncode else None
    if "RATE_LIMITED" in error_types and reason != "secondary":
        reason = "graphql"
    if result.returncode and not reason and status and status[1] == "429":
        reason = "http_429"
    if result.returncode or reason:
        # Fixed labels retain the useful evidence without logging arbitrary
        # response text, PR bodies, query variables, headers or credentials.
        diagnostic = next((label for label in (
            "Resource not accessible by integration", "Bad credentials", "Not Found",
            "Forbidden", "Bad Gateway", "Internal Server Error", "Service Unavailable"
        ) if label.lower() in stderr.lower()), None)
        print(json.dumps({"schema": "tauceti-merge.api-error/v1",
                          "request": _request_label(args), "gh_invocation": calls,
                          "returncode": result.returncode,
                          "http_status": int(status[1]) if status else None,
                          "rate_limit_kind": reason, "diagnostic": diagnostic}), flush=True)
    if reason:
        rate_limited = True
        raise Exhausted("GitHub installation rate limit reached; stopping this sweep")
    return result


def configure(focused):
    global calls, limit, rate_limited, failures
    calls, rate_limited, failures = 0, False, 0
    # Leave capacity for concurrent merge-only and review jobs. The read itself
    # does not consume primary quota. A failed read cannot authorize more work.
    result = run(["gh", "api", "rate_limit"], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Cannot read installation API quota")
    try:
        resources = json.loads(result.stdout)["resources"]
        rest = resources["core"]["remaining"]
        graphql = resources["graphql"]["remaining"]
        if type(rest) is not int or type(graphql) is not int:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError("Invalid installation API quota") from e
    limit = min(120 if focused else 800, max(0, rest - 1000), max(0, graphql - 100))
    print(json.dumps({"schema": "tauceti-merge.api-budget/v1", "focused": focused,
                      "rest_remaining": rest, "graphql_remaining": graphql,
                      "gh_invocation_limit": limit}))
    begin_pr()


def begin_pr():
    if rate_limited or (limit is not None and calls + 30 > limit):
        raise Exhausted(f"Sweep work budget reached ({calls} gh invocations); remaining PRs deferred")


def check_result(result):
    global failures
    if not result:
        failures += 1
    return result


def defer(error):
    print(f"merge-sweep: deferred: {error}; prior failures: {failures}")
    return 1 if failures or rate_limited else 0
