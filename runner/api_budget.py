"""Bound sweep work and preserve installation quota for event-driven reviews.

Counts gh invocations, not HTTP pages or GraphQL points. Live quota checks and
rate-limit detection complement the per-run bound. Finish a PR before deferring
the next one so post-admission integrity checks retain their working allowance.
"""
import json
import subprocess


class Exhausted(Exception):
    pass


calls = 0
limit = None
rate_limited = False
failures = 0


def run(args, **kwargs):
    global calls, rate_limited
    calls += 1
    try:
        result = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("GitHub request timed out") from e
    message = ((result.stdout or "") + (result.stderr or "")).lower()
    if result.returncode and any(s in message for s in (
            "api rate limit exceeded", "api rate limit already exceeded",
            "secondary rate limit", "rate_limited", "(http 429)")):
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
