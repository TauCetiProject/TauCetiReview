#!/usr/bin/env python3
"""Trusted-phase poster for the Tau Ceti review runner.

Runs AFTER the tokenless reviewer phase, with a scoped GitHub App token in `$GH_TOKEN`. It reads
the post plan the runner wrote and:

  * publishes every required blocking **review thread** first, adopting an identical root left by
    a crash when necessary (on GitHub, the new roots of one round land as a single batched review), then
  * upserts the in-place **scoreboard** issue comment as the publication commit marker, and finally
  * performs close notes and contest replies as best-effort UI cleanup.

then writes the comment ids back into the store ledger so the next round edits in place. It runs
no model and trusts only the structured plan plus the runner's rendered bodies (which are the
review output we intend to publish anyway); a prompt-injected reviewer never reaches this step's
token.

Every API action's outcome is tracked: only CONFIRMED comment ids reach the ledger and the archive
sidecar (records/posts/). A required thread failure withholds the scoreboard and exits nonzero; a
scoreboard failure leaves the write-ahead markers pending and also exits nonzero. Thus a visible
current-head scoreboard never names a blocker that lacks its contestable inline thread.

GitHub caps an account's content writes (new comments, replies, edits) at about 500 an hour, so the
GitHub transport spends them only on what the scoreboard cannot show: new and changed findings, and
answers to contests that stand. A cleared finding's thread is resolved (a GraphQL mutation, not a
comment write) without a "now passing" edit or a "clears the finding" reply; the scoreboard shows
the rubric green. Every run prints one `post.py: content-writes=N ...` line for accounting.
"""
import argparse, collections, contextlib, datetime, hashlib, json, os, pathlib, random, re, shutil, subprocess, sys, time

from tauceti_tools.forge import ForgeError, Incomplete

try:
    from . import archive
    from .ledger import atomic_write_json
    from .publication_transport import GitHubPublication, PublicationSuperseded, transport_for
except ImportError:
    import archive
    from ledger import atomic_write_json
    from publication_transport import GitHubPublication, PublicationSuperseded, transport_for

REPLY_MARKER_RE = re.compile(r"<!--tauceti-reply:([a-z][a-z-]*):through:(\d+)-->")
RUBRIC_MARKER_RE = re.compile(r"<!--tauceti-rubric:([a-z][a-z-]*)-->")
META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)


def _comment_json_lines(stdout):
    """Decode `gh --jq '.[] | @json'` output (and tolerate an extra JSON string layer)."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
            if isinstance(value, str):
                value = json.loads(value)
            if isinstance(value, dict):
                yield value
        except Exception:
            continue


def find_review_roots(repo, pr):
    """Return GitHub's review-thread roots, including rubric/run provenance, or None on failure.

    This is the remote half of crash idempotence. A POST can succeed immediately before the process
    dies and before its id reaches the ledger; the next invocation recognizes its exact rubric,
    head, and run id and adopts it instead of creating a duplicate.
    """
    r = subprocess.run(
        ["gh", "api", "--paginate", "--jq", ".[] | @json",
         f"/repos/{repo}/pulls/{pr}/comments?per_page=100"],
        text=True, capture_output=True)
    if r.returncode != 0:
        print(f"review-root lookup failed: {r.stderr[-300:]}", file=sys.stderr)
        return None
    return normalize_roots(_comment_json_lines(r.stdout))


def normalize_roots(comments, principal=lambda user: user.get("login"),
                    reviewed_head=lambda row, meta: meta.get("head_sha") or row.get("commit_id")):
    roots = []
    for c in comments:
        if c.get("in_reply_to_id") is not None:
            continue
        body = c.get("body") or ""
        rubric_match = RUBRIC_MARKER_RE.search(body)
        meta = {}
        meta_match = META_RE.search(body)
        if meta_match:
            try:
                meta = json.loads(meta_match.group(1))
            except Exception:
                meta = {}
        roots.append({
            "id": c.get("id"), "node_id": c.get("node_id"), "path": c.get("path"),
            "login": principal(c.get("user") or {}),
            "thread_kind": c.get("thread_kind"), "subject_type": c.get("subject_type"),
            "rubric": rubric_match.group(1) if rubric_match else None,
            "head_sha": reviewed_head(c, meta),
            "run_ids": [run.get("id") for run in (meta.get("runs") or []) if run.get("id")],
            "created_at": c.get("created_at") or "", "body": body,
        })
    return roots


def _newest(items):
    return max(items, key=lambda c: (c.get("created_at") or "", c.get("id") or 0), default=None)


def choose_required_root(head, action, cf, roots, me, transport):
    """How to land one required blocker root: ("adopt", root), ("edit", root) or ("create", None).

    Exact roots authored by this identity are adopted after a crash. Known roots authored by this
    identity are edited in place while the transport still shows them as a live thread. A root
    owned by another identity is never PATCHed: this identity appends its own root on the current
    head instead.
    """
    rubric, run_id = action["rubric"], action.get("run_id")
    exact = _newest(c for c in roots
                    if c.get("login") == me and c.get("rubric") == rubric
                    and c.get("head_sha") == head and run_id in c.get("run_ids", [])
                    and transport.live(c))
    if exact:
        return "adopt", exact
    known_id = (cf.get("thread") or {}).get("comment_id") or action.get("comment_id")
    known = next((c for c in roots if c.get("id") == known_id), None) if known_id else None
    foreign = bool(known and known.get("login") != me)
    if known and not foreign and transport.editable(known, action):
        return "edit", known
    # If the ledger lost its id, reuse our newest rubric root. A known foreign root is the
    # identity-migration case and deliberately forces a new current-head root instead.
    ours = None if foreign else _newest(
        c for c in roots if c.get("login") == me and c.get("rubric") == rubric and transport.editable(c, action))
    if ours:
        return "edit", ours
    return "create", None


def record_required_root(cf, chosen, action, roots=(), me=None):
    """Point the case file at `chosen`. A different root it pointed at before, unless known to
    be another identity's, is kept in `superseded` until its thread is resolved."""
    old = cf.get("thread") or {}
    stale = [i for i in old.get("superseded") or [] if i != chosen["id"]]
    previous = old.get("comment_id")
    owner = next((c.get("login") for c in roots if c.get("id") == previous), me)
    if previous and previous != chosen["id"] and owner == me and previous not in stale:
        stale.append(previous)
    cf["thread"] = {
        **old,
        "comment_id": chosen["id"],
        "node_id": chosen.get("node_id"),
        "path": chosen.get("path") or action.get("path"),
        "published_run_id": action.get("run_id"),
    }
    if stale:
        cf["thread"]["superseded"] = stale
    else:
        cf["thread"].pop("superseded", None)
    return chosen["id"]


def required_root_failed(rubric, failures):
    if not any(f.get("action", "").endswith(rubric) for f in failures):
        failures.append({"action": f"required thread upsert {rubric}",
                         "error": "GitHub response had no confirmed comment id"})
    print(f"post.py: required thread publication failed for {rubric}", file=sys.stderr)


def added_line_anchor(diff_path, path, preferred=None):
    """A RIGHT-side line of `path` that GitHub accepts for a line comment, or None.

    Only added lines qualify: they lie in GitHub's diff hunks whatever context width it uses. The
    finding's own line is preferred when it is one of them, else the file's first added line.
    """
    try:
        text = pathlib.Path(diff_path).read_text(errors="surrogateescape")
    except OSError:
        return None
    selected, header, line, first = False, False, 0, None
    for row in text.splitlines():
        if row.startswith("diff --git "):
            if first is not None:
                break  # the file's section ended without the preferred line
            selected, header, line = False, True, 0
        elif header:
            if row.startswith("+++ "):
                selected = row[4:] == "b/" + path
            elif row.startswith("@@ "):
                header = False
        if not header and row.startswith("@@ "):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", row)
            line = int(match[1]) if match and selected else 0
        elif not header and selected and line:
            if row.startswith("+"):
                if line == preferred:
                    return line
                first = line if first is None else first
                line += 1
            elif row.startswith(" ") or row == "":
                line += 1
    return first


def already_replied(repo, pr, rubric, through_id, me):
    """True iff we already posted a contest answer for `rubric` covering `through_id` or newer.

    Durable, marker-based dedupe across machines (the engine's per-rubric `last_reply_seen` is the
    local fast path; this is the cross-machine authority). The scan-then-POST is not atomic, so two
    workers posting at the exact same instant could still double-reply — that is an accepted, rare,
    cosmetic duplicate (normal operation is a single worker), not a correctness problem: the
    expensive model-run dedupe is guaranteed by the case-file watermark, never this scan. On a fetch
    failure, return False (post — a missed answer is worse than a rare duplicate)."""
    if through_id is None:
        return False
    out = subprocess.run(["gh", "api", "--paginate", "--jq", ".[]",
                          f"/repos/{repo}/pulls/{pr}/comments?per_page=100"],
                         text=True, capture_output=True)
    if out.returncode != 0:
        return False
    return reply_seen(_comment_json_lines(out.stdout), rubric, through_id, me)


def reply_seen(comments, rubric, through_id, me, principal=lambda user: user.get("login")):
    if through_id is None:
        return False
    for c in comments:
        if principal(c.get("user") or {}) != me:
            continue
        match = REPLY_MARKER_RE.search(c.get("body") or "")
        if match and match.group(1) == rubric and int(match.group(2)) >= int(through_id):
            return True
    return False


def github_error_response(stdout):
    """Retain GitHub's validation explanation, without request bodies or credentials."""
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    detail = {k: payload[k] for k in ("message", "errors", "documentation_url", "status")
              if k in payload}
    if not detail:
        return None
    encoded = json.dumps(detail, ensure_ascii=True)
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name)
        if token:
            encoded = encoded.replace(json.dumps(token)[1:-1], "[REDACTED]")
    return json.loads(encoded)


def gh_api(method, endpoint, fields=None, body_file=None, failures=None, action="", input_file=None):
    cmd = ["gh", "api", "-X", method, endpoint]
    if body_file:
        cmd += ["-F", f"body=@{body_file}"]  # @file -> read body from the rendered markdown
    if input_file:
        cmd += ["--input", str(input_file)]  # a JSON request body, e.g. a batched review
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    comment_send = bool((body_file or input_file) and method in ("POST", "PATCH"))
    shared_pacing = bool(os.environ.get("TAUCETI_GITHUB_SHARED_PACING")) and os.environ["TAUCETI_GITHUB_SHARED_PACING"] == shutil.which("gh")
    if comment_send:
        cmd.append("--include")  # Keep Retry-After alongside GitHub's error JSON.
        # Space comment sends to guard against GitHub 422 spam/too-quickly rejections.
        if not shared_pacing:
            time.sleep(2)
    for attempt in range(4):
        r = subprocess.run(cmd, text=True, capture_output=True)
        body, headers, status = r.stdout, {}, None
        # gh --include prefixes the JSON body with HTTP headers. subprocess normalizes newlines.
        while body.startswith("HTTP/"):
            block, separator, body = body.partition("\n\n")
            if not separator:
                break
            status = block.splitlines()[0].split()[1]
            headers = {k.strip().lower(): v.strip() for line in block.splitlines()[1:]
                       if ":" in line for k, v in [line.split(":", 1)]}
        if r.returncode == 0:
            try:
                return json.loads(body)
            except Exception:
                return {}
        print(f"gh api {method} {endpoint} FAILED: {r.stderr[-600:]}", file=sys.stderr)
        detail = github_error_response(body)
        if detail:
            print("GitHub error JSON: " + json.dumps(detail, ensure_ascii=True), file=sys.stderr)
        too_quickly = (comment_send and str(status or (detail or {}).get("status")) == "422"
                       and any(isinstance(e, dict)
                               and (e.get("resource") == "PullRequestReviewComment"
                                    and e.get("message") == "was submitted too quickly"
                                    or e.get("resource") in ("PullRequestReview", "PullRequestReviewComment")
                                    and e.get("code") == "abuse")
                               for e in ((detail or {}).get("errors") or [])))
        if too_quickly and attempt < 3 and not shared_pacing:
            # Retry this rejected publication, never the model. Other 422 validation errors
            # need fixing, not retries. GitHub recommends at least a minute after throttling.
            try:
                retry_after = float(headers.get("retry-after", "0"))
            except ValueError:
                retry_after = 0
            delay = max(60 * 2 ** attempt, retry_after) + random.uniform(0, 5)
            print(f"Comment throttled (HTTP 422); retry {attempt + 1}/3 in {delay:.1f}s",
                  file=sys.stderr)
            time.sleep(delay)
            continue
        if failures is not None:
            # `rejected`: GitHub refused this request as invalid (HTTP 422, not a throttle), so
            # it had no effect and a differently shaped request may succeed.
            invalid = (not too_quickly and (str(status or (detail or {}).get("status")) == "422"
                                            or "(HTTP 422)" in (r.stderr or "")))
            failures.append({"action": action or f"{method} {endpoint}",
                             "error": r.stderr[-300:],
                             **({"response": detail} if detail else {}),
                             **({"rejected": True} if invalid else {})})
        return None


SCOREBOARD_MARKER = "<!--tauceti-scoreboard-->"
# Forgejo scoreboards from the retired receipt publisher. They still count as ordinary posted
# scoreboards, but the native transport refuses to edit or delete them, so a new board is posted.
RECEIPT_SCOREBOARD_MARKER = "<!--tauceti-source:v2 "
REVIEW_BOT = "tauceti-review-bot[bot]"


def current_login():
    """Who this token acts as: the operator for a user token, or the review bot for an installation
    token (which cannot read /user). We only ever edit/delete comments authored by this login, so a
    write-scoped token never overwrites or removes a comment belonging to someone else."""
    r = subprocess.run(["gh", "api", "user", "--jq", ".login"], text=True, capture_output=True)
    login = (r.stdout or "").strip()
    if r.returncode == 0 and login:
        return login
    # Installation tokens cannot read /user. Preserve that known transport
    # behavior, but never turn an outage/rate limit on a user token into a bot
    # identity that will strand its paid publication receipt.
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
    if (token.startswith('ghs_') and '403' in (r.stderr or '')
            and 'resource not accessible by integration' in (r.stderr or '').lower()):
        return REVIEW_BOT
    raise Incomplete('publication identity is unavailable')


def find_scoreboard_comments(repo, pr):
    """Return every marked scoreboard as {id, login, updated_at, body}, newest first.

    This is the ownership proof before `upsert_scoreboard` mutates a known comment, so it deliberately
    includes outside contributors: author association does not change whether a comment is ours.
    Return None, rather than an incomplete list, on an API or parse failure so the caller can fail
    closed instead of PATCHing an id whose owner it could not verify. `@json` forces one compact object
    per line across pages."""
    r = subprocess.run(
        ["gh", "api", "--paginate", f"/repos/{repo}/issues/{pr}/comments?per_page=100", "--jq",
         '.[] | select((.body // "") | contains("' + SCOREBOARD_MARKER + '")) '
         '| {id, login: (.user.login // ""), updated_at: (.updated_at // ""), body} | @json'],
        text=True, capture_output=True)
    if r.returncode != 0:
        print(f"scoreboard lookup failed: {r.stderr[-300:]}", file=sys.stderr)
        return None
    out = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        try:
            c = json.loads(line)
            if isinstance(c, str):
                c = json.loads(c)
            if not isinstance(c, dict) or c.get("id") is None:
                raise ValueError("scoreboard row is not an object with an id")
        except Exception:
            print("scoreboard lookup returned malformed JSON", file=sys.stderr)
            return None
        out.append(c)
    out.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return out


def upsert_scoreboard(repo, pr, body_file, plan_sb_id, pr_state, failures, mine=None, transport=None,
                      writes=None):
    """Publish the PR's single scoreboard comment, editing OUR existing one in place rather than
    duplicating, and collapsing OUR older duplicates.

    Prefer the store/plan id when discovery proves it is ours; otherwise reuse our newest discovered
    scoreboard. A foreign or missing known id is never PATCHed, and a new scoreboard is posted only
    when this identity has none to reuse. A legacy receipt-backed board is never reused: the board
    posted beside it supersedes it once completed, as any newer scoreboard does. Discovery is
    required: on failure, mutate nothing and let the publication retry. A failed edit of our own
    comment is likewise a real error, never silently re-posted. Identical text needs no PATCH only when this
    comment is strictly newest: updated_at determines which scoreboard supersedes others. Our
    obsolete duplicates are still removed. Returns (sb_id, ok). `mine` overrides
    the actor login."""
    transport = transport or GitHubPublication(repo, pr, "", {}, sys.modules[__name__])
    writes = writes if writes is not None else collections.Counter()
    publisher = getattr(transport, 'scoreboard_publisher', lambda: None)()
    if publisher is not None:
        # Immutable receipts own their comment history. The backend confirms
        # publication and returns its actual ID; do not edit/collapse that history.
        result = publisher.publish(transport.body(body_file))
        if result == {'superseded': True}:
            raise PublicationSuperseded
        if not isinstance(result, dict) or type(result.get('id')) is not int or result['id'] <= 0:
            raise Incomplete('scoreboard backend did not confirm publication')
        pr_state['scoreboard_comment_id'] = result['id']
        writes["scoreboard_backend"] += 1
        return result['id'], True
    me = mine if mine is not None else current_login()
    existing = transport.board_comments()
    if existing is None:
        if failures is not None:
            failures.append({"action": "scoreboard discovery",
                             "error": "could not verify scoreboard ownership"})
        return None, False

    known_id = pr_state.get("scoreboard_comment_id") or plan_sb_id
    known = next((c for c in existing if c.get("id") == known_id), None) if known_id else None
    ours = [c for c in existing if c.get("login") == me
            and RECEIPT_SCOREBOARD_MARKER not in (c.get("body") or "")]
    if known and any(c is known for c in ours):
        sb_id = known_id
    else:
        # The stored comment is foreign or gone. Reuse our newest scoreboard if one exists; unlike
        # review-thread roots, scoreboards have no rubric/thread identity that requires a fresh root.
        sb_id = ours[0]["id"] if ours else None
    mine_dupes = [c["id"] for c in ours if c.get("id") != sb_id]

    ok = False
    if sb_id:
        current = next(c for c in ours if c["id"] == sb_id)
        strictly_newest = all(c.get("updated_at") and current.get("updated_at")
                             and c["updated_at"] < current["updated_at"]
                             for c in existing if c["id"] != sb_id)
        if strictly_newest and current.get("body") == transport.body(body_file):
            pr_state["scoreboard_comment_id"] = sb_id
            ok = True
        elif transport.comment("PATCH", sb_id, body_file, failures, "scoreboard PATCH") is not None:
            pr_state["scoreboard_comment_id"] = sb_id
            writes["scoreboard_edit"] += 1
            ok = True
    else:
        resp = transport.comment("POST", None, body_file, failures, "scoreboard POST")
        if resp and resp.get("id"):
            pr_state["scoreboard_comment_id"] = sb_id = resp["id"]
            writes["scoreboard_create"] += 1
            ok = True
        else:
            print("post.py: scoreboard create failed", file=sys.stderr)
            sb_id = None
    if ok:                                # collapse our own older duplicates (best-effort)
        for dup in mine_dupes:
            if dup:
                try:
                    transport.comment("DELETE", dup, action=f"scoreboard collapse {dup}")
                except (ForgeError, OSError, subprocess.TimeoutExpired):
                    print("post.py: obsolete scoreboard cleanup deferred", file=sys.stderr)
    return sb_id, ok


REVIEW_THREADS_QUERY = (
    "query($owner:String!,$name:String!,$pr:Int!,$after:String){repository(owner:$owner,name:$name){"
    "pullRequest(number:$pr){reviewThreads(first:100,after:$after){nodes{id isResolved isOutdated "
    "comments(first:1){nodes{databaseId}}} pageInfo{hasNextPage endCursor}}}}}")
RESOLVE_MUTATION = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"


def review_thread_states(repo, pr):
    """{root comment id: {thread_id, resolved, outdated}} for the PR's review threads, or None.

    One GraphQL read (a page per 100 threads). It tells the poster which of its roots are still
    live threads worth editing in place and which need resolving, without any comment write."""
    owner, name = repo.split("/")
    states, after = {}, None
    for _ in range(20):
        cmd = ["gh", "api", "graphql", "-f", f"query={REVIEW_THREADS_QUERY}", "-F", f"owner={owner}",
               "-F", f"name={name}", "-F", f"pr={int(pr)}"] + (["-f", f"after={after}"] if after else [])
        r = subprocess.run(cmd, text=True, capture_output=True)
        if r.returncode != 0:
            print(f"review-thread query failed: {r.stderr[-300:]}", file=sys.stderr)
            return None
        try:
            threads = json.loads(r.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]
            for t in threads["nodes"]:
                first = (t.get("comments") or {}).get("nodes") or []
                if first and first[0].get("databaseId") is not None:
                    states[first[0]["databaseId"]] = {"thread_id": t["id"],
                                                      "resolved": bool(t.get("isResolved")),
                                                      "outdated": bool(t.get("isOutdated"))}
            if not threads["pageInfo"]["hasNextPage"]:
                return states
            after = threads["pageInfo"]["endCursor"]
        except (ValueError, KeyError, TypeError):
            print("review-thread query returned malformed JSON", file=sys.stderr)
            return None
    return None


def resolve_thread_id(thread_id):
    """Resolve one review thread by node id (a GraphQL mutation, not a comment write)."""
    r = subprocess.run(["gh", "api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-F", f"id={thread_id}"],
                       text=True, capture_output=True)
    if r.returncode != 0:
        print(f"resolve failed: {r.stderr[-300:]}", file=sys.stderr)
        return False
    return True


def resolve_thread(repo, pr, comment_id):
    """Resolve (collapse) the review thread whose root comment is `comment_id`, so a finding the
    author has cleared stops cluttering the conversation. Best-effort; failures are logged.
    Returns True only when this call resolved it."""
    state = (review_thread_states(repo, pr) or {}).get(comment_id)
    if not state or state["resolved"]:
        return False
    return resolve_thread_id(state["thread_id"])


CONTENT_WRITES = ("review_batch", "thread_create", "thread_edit", "reply",
                  "scoreboard_create", "scoreboard_edit")


def report_writes(writes):
    """One greppable line per run: the comment writes GitHub counts toward its content limit
    (new comments, batched reviews, replies, edits), then other mutations that it does not."""
    content = sum(writes[k] for k in CONTENT_WRITES)
    detail = " ".join(f"{k}={writes[k]}" for k in CONTENT_WRITES if writes[k])
    other = " ".join(f"{k}={v}" for k, v in sorted(writes.items()) if k not in CONTENT_WRITES and v)
    print(f"post.py: content-writes={content}" + (f" {detail}" if detail else "")
          + (f"; other: {other}" if other else ""))


def archive_outcome(archive_dir, repo, pr, plan, sb_id, scoreboard_ok, posted_threads, failures,
                    writes=None):
    """Archive only confirmed remote ids, including partial/failed publication attempts."""
    if writes is not None:
        report_writes(writes)
    if not archive_dir:
        return
    sb_body = pathlib.Path(plan["scoreboard_body"]).read_text()
    rec = {"schema": "tauceti.post/v1", "repo": repo, "pr": int(pr),
           "round": plan.get("round"), "head_sha": plan.get("head_sha") or None,
           "posted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "scoreboard_comment_id": sb_id if scoreboard_ok else None,
           "scoreboard_body_sha256": hashlib.sha256(sb_body.encode()).hexdigest(),
           "threads": posted_threads or None, "failures": failures or None,
           "writes": {k: v for k, v in sorted((writes or {}).items()) if v} or None}
    try:
        archive.archive_post(archive_dir, {k: v for k, v in rec.items() if v is not None})
    except Exception as e:
        print(f"WARNING: post archive write failed: {e}", file=sys.stderr)


def execute_post(repo, pr, plan, ledger_path, archive_dir="", transport=None, expected_publisher=None):
    if plan.get("review_changed") is False:
        return 0
    transport = transport or transport_for(repo, int(pr), plan.get("head_sha", ""), plan, sys.modules[__name__])
    with getattr(transport, 'publication_scope', contextlib.nullcontext)():
        return _execute_post(repo, pr, plan, ledger_path, archive_dir, transport, expected_publisher)


def _execute_post(repo, pr, plan, ledger_path, archive_dir="", transport=None, expected_publisher=None):
    """Execute one trusted plan. Return a process-style status code (0 success, 1 incomplete)."""
    if plan.get("review_changed") is False:
        return 0
    head = plan.get("head_sha", "")
    transport = transport or transport_for(repo, int(pr), head, plan, sys.modules[__name__])
    if not transport.current():
        print("post.py: source head changed; retained plan is superseded", file=sys.stderr)
        return 82
    ledger_path = pathlib.Path(ledger_path)
    ledger = json.loads(ledger_path.read_text())
    pr_state = ledger["prs"].setdefault(str(pr), {})
    pr_state.setdefault("state", {})
    failures, posted_threads = [], {}
    writes = collections.Counter()
    sb_id, scoreboard_ok = None, False
    actions = plan.get("threads", [])
    required = [t for t in actions
                if t.get("required", t.get("action") == "upsert")]
    optional = [t for t in actions if t not in required]
    me = transport.identity()
    if expected_publisher is not None and me != expected_publisher:
        print('post.py: retained review requires its original publisher', file=sys.stderr)
        return 86
    if getattr(transport, 'publication_superseded', lambda body: False)(plan["scoreboard_body"]):
        return 87  # Before discovery or edits of another run's required roots.

    # Phase 1: every genuine blocker root is required. Remote discovery is itself required because
    # it proves ownership before PATCH and makes a POST-crash retry idempotent.
    roots = transport.roots()
    if required and roots is None:
        failures.append({"action": "required thread discovery",
                         "error": "could not list review roots"})
    else:
        creates = []
        for t in required:
            rubric = t["rubric"]
            cf = pr_state["state"].setdefault(rubric, {})
            kind, chosen = choose_required_root(head, t, cf, roots, me, transport)
            if kind == "create":
                creates.append(t)
                continue
            if kind == "edit":
                if transport.edit_finding(chosen["id"], t, failures) is None:
                    required_root_failed(rubric, failures)
                    continue
                writes["thread_edit"] += 1
            posted_threads[rubric] = record_required_root(cf, chosen, t, roots, me)
            # Persist the confirmed root immediately. Keep its pending marker until the
            # scoreboard succeeds; a crash or scoreboard failure must schedule another repair.
            atomic_write_json(ledger_path, ledger)
        def confirmed(t, chosen):
            cf = pr_state["state"].setdefault(t["rubric"], {})
            posted_threads[t["rubric"]] = record_required_root(cf, chosen, t, roots, me)
            atomic_write_json(ledger_path, ledger)

        if creates:
            transport.create_findings(creates, failures, writes, confirmed)
            for t in creates:
                if t["rubric"] not in posted_threads:
                    required_root_failed(t["rubric"], failures)

    required_failed = any(t["rubric"] not in posted_threads for t in required)
    if required_failed:
        archive_outcome(archive_dir, repo, pr, plan, None, False, posted_threads, failures, writes)
        print(f"post.py: withheld scoreboard; {len(required)} required thread(s), "
              f"{len(failures)} failure(s); confirmed ids saved.")
        return 1

    # Phase 2: the current-head scoreboard is the publication commit marker. Only after it lands do
    # we clear the PR/thread write-ahead records.
    if not transport.current():
        print("post.py: source head changed before scoreboard; retained plan is superseded", file=sys.stderr)
        report_writes(writes)
        return 82
    try:
        sb_id, scoreboard_ok = upsert_scoreboard(
            repo, pr, plan["scoreboard_body"], plan.get("scoreboard_comment_id"), pr_state, failures,
            mine=me, transport=transport, writes=writes)
    except PublicationSuperseded:
        report_writes(writes)
        return 87
    atomic_write_json(ledger_path, ledger)
    if not scoreboard_ok:
        archive_outcome(archive_dir, repo, pr, plan, sb_id, False, posted_threads, failures, writes)
        print(f"post.py: scoreboard failed after {len(required)} required thread(s); "
              f"{len(failures)} failure(s); publication remains pending.")
        return 1

    if plan.get("in_progress"):
        # The visible board no longer represents a completed review. A same-head
        # Actions rerun must restore it even when no reviewer needs to run again.
        pr_state.pop("published_head_sha", None)
    else:
        pr_state["published_head_sha"] = head
    if pr_state.get("pending_publication_head_sha") == head:
        pr_state.pop("pending_publication_head_sha", None)
    for t in required:
        cf = pr_state["state"].setdefault(t["rubric"], {})
        if (not t.get("run_id")
                or cf.get("pending_thread_run_id") == t.get("run_id")):
            cf.pop("pending_thread_run_id", None)
    atomic_write_json(ledger_path, ledger)

    # Phase 3: closes and direct contest answers improve the UI but do not affect whether the
    # review verdict was published. Optional failures are archived and reported, never rolled back.
    roots_by_id = {c.get("id"): c for c in (roots or []) if c.get("id")}
    for t in optional:
        try:
            rubric = t["rubric"]
            cf = pr_state["state"].setdefault(rubric, {})
            if t["action"] == "reply":
                if t.get("clears") and not transport.posts_clearing_replies:
                    continue  # the green scoreboard and the resolved thread already say it
                parent = t.get("in_reply_to") or (cf.get("thread") or {}).get("comment_id")
                if not parent or transport.replied(rubric, t.get("reply_dedupe"), me):
                    continue
                resp = transport.reply(parent, t, failures)
                if resp and resp.get("id"):
                    posted_threads[f"{rubric}:reply"] = resp["id"]
                    writes["reply"] += 1
                continue
            if t["action"] != "close":
                continue
            cid = (cf.get("thread") or {}).get("comment_id") or t.get("comment_id")
            root = roots_by_id.get(cid)
            if not cid or not root or root.get("login") != me:
                continue  # nothing of ours to edit; never cross-edit another identity's thread
            if transport.closes_by_resolving:
                # The scoreboard shows the rubric green; resolving collapses the thread without
                # spending a comment write on a "now passing" note.
                posted_threads[rubric] = cid
                if transport.resolve(cid):
                    writes["resolve"] += 1
                continue
            if root.get("body") != pathlib.Path(t["body"]).read_text():
                if transport.edit_finding(cid, t, failures) is None:
                    continue
                writes["thread_edit"] += 1
            posted_threads[rubric] = cid
            transport.resolve(cid)
        except (ForgeError, OSError) as error:
            failures.append({'action': f"optional {t['action']} {t['rubric']}",
                             'error': type(error).__name__})
    for rubric, cf in pr_state["state"].items():
        thread = cf.get("thread") if isinstance(cf, dict) else None
        if not isinstance(thread, dict) or not thread.get("superseded"):
            continue
        # A new root replaced these older ones of ours (resolved, outdated, or on another head);
        # collapse them, and forget each once its thread is resolved or gone.
        # A root missing from the (possibly lagging) comment history is kept until its thread
        # state shows it resolved or gone.
        pending = []
        for old_id in thread["superseded"]:
            old = roots_by_id.get(old_id)
            if old and old.get("login") != me:
                continue  # never resolve another identity's thread
            if roots is None:
                pending.append(old_id)
                continue
            try:
                if not transport.settled(old_id) and transport.resolve(old_id):
                    writes["resolve"] += 1
            except (ForgeError, OSError) as error:
                failures.append({'action': f"optional resolve {rubric}", 'error': type(error).__name__})
            if not transport.settled(old_id):
                pending.append(old_id)
        if pending:
            thread["superseded"] = pending
        else:
            thread.pop("superseded", None)

    atomic_write_json(ledger_path, ledger)
    archive_outcome(archive_dir, repo, pr, plan, sb_id, True, posted_threads, failures, writes)
    print(f"post.py: scoreboard id={pr_state.get('scoreboard_comment_id')}; "
          f"{len(actions)} thread action(s); {len(failures)} failure(s); ledger updated.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--archive-dir", default="",
                    help="outbox directory: record which comment ids actually landed "
                         "(records/posts/) for the durable archive")
    a = ap.parse_args()
    if not (os.environ.get("GH_TOKEN") or os.environ.get("TAUCETI_SOURCE_BINDING")):
        print("post.py: GH_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    plan = json.loads(pathlib.Path(a.plan).read_text())
    try:
        status = execute_post(a.repo, a.pr, plan, pathlib.Path(a.store) / "ledger.json",
                              a.archive_dir)
    except (ForgeError, OSError, subprocess.TimeoutExpired) as error:
        print(f'post.py: plan retained at {a.plan}; source unavailable ({type(error).__name__})', file=sys.stderr)
        status = 85
    if status == 82:
        return  # A superseded Actions review is a successful no-op.
    if status:
        sys.exit(status)


if __name__ == "__main__":
    main()
