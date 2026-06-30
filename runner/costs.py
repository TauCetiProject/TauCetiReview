#!/usr/bin/env python3
"""costs.py — attribute Tau Ceti AI-review spend (tokens AND $) to PRs and merged LOC.

Three data sources, picked automatically:

  * **data** (canonical) — a TauCetiData checkout, the durable/public/append-only
    archive: ``records/runs/<pr>/<run_id>.json`` with a full ``usage`` block,
    ``started_at``, ``model`` and ``cost_usd``. Reproducible by anyone; defaults to
    the production arm and de-dupes by ``dedupe_key``. ``--source data --data-dir``.
  * **store** — the engine's local cache, ``~/.cache/tauceti-review/store/<repo>/``
    (``reviews/<pr>/<round>/<rubric>.json`` + ``ledger.json``). Fast and local but
    ephemeral and single-machine.
  * **logs** (fallback) — ``task-*.log`` ``ROUND n ... cost $X`` lines. Dollars
    only, no tokens.

`--source auto` (default) prefers ``data`` when ``--data-dir`` is given, else the
store, else logs. Sources are never mixed, so nothing is double-counted.

It joins each round to its PR's outcome and size (via ``gh``), stores everything
in SQLite, and reports/plots, separately:

  * **token costs** — input/cached/output/reasoning, and tokens per merged LOC
  * **imputed dollars** — $ per merged LOC, $ wasted on closed PRs, $/day & /week
  * both split by the agent that *authored* the PR (codex-self vs other)

Caveat the engine itself flags: most ``cost_usd`` values are ``cost_estimated``
(price model inferred), so **tokens are measured, dollars are an estimate**. The
report shows the estimated fraction.

Only the REVIEW side is accounted, by request — authoring/fixing spend is ignored.

Stdlib-only, matching the rest of the engine — it shells out to ``gh`` and reads
the local store; no third-party Python dependencies.

Usage (installed as the ``tauceti-review-costs`` console script, or
``python3 -m runner.costs``)::

    tauceti-review-costs all                 # ingest + refresh PRs + report
    tauceti-review-costs ingest [--source auto|data|store|logs] [--data-dir PATH] [--store PATH]
    tauceti-review-costs prs                  # PR outcomes/LOC from GitHub (cached)
    tauceti-review-costs report [--window day|week] [--csv FILE]
    tauceti-review-costs graph [--out FILE]   # dependency-free SVG
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path

# Read the dated price table through the engine's pricing module — one source of truth for the
# prices.json path and its load, so the analytics price runs from exactly the file the engine bills
# from. (costs runs as `runner.costs` / `python3 -m runner.costs`, so this package import resolves.)
from runner.pricing import load_price_windows as load_history

CACHE = Path.home() / ".cache" / "tauceti-review"
DEFAULT_DB = CACHE / "review-costs.db"
DEFAULT_OUT = CACHE / "review-costs.svg"
DEFAULT_REPO = "TauCetiProject/TauCeti"
SCHEMA_VERSION = 5

# Cost is DERIVED, not a stored fact. We recompute every *estimated* (codex/pi) review from the
# token counts — the only immutable fact — applying the cache-aware formula. Crucially we price
# each run AS OF ITS OWN DATE, not today's prices: a faithful answer to "what did this run cost?"
# must use the rate that was in effect when it ran. prices.json is a single DATED table (the same
# file the engine bills from) — there is no separate history file to drift against. Real
# provider-billed costs (cost_estimated=false) are kept as recorded.
DEFAULT_PRICE = {"input": 3.0, "output": 15.0, "cache_read": 3.0}  # review.py's unpriced fallback


def price_window(history: dict, model: str, date: str) -> dict | None:
    """The rate window in effect for `model` on `date` ('YYYY-MM-DD'); None if unpriced."""
    windows = history.get(model)
    if not windows:
        return None
    eligible = [w for w in windows if w["effective"] <= date]
    return eligible[-1] if eligible else windows[0]  # before the first window: earliest known


def cost_from_window(win: dict, inp: int, cached: int, out: int) -> float:
    """Cache-aware cost (cached_input is a subset of input billed at the cache-read rate),
    escalating the WHOLE request to the long-context tier when input crosses its threshold."""
    rates = win
    lc = win.get("long_context")
    if lc and inp > lc["threshold"]:
        rates = lc
    pin, pout = rates["input"], rates["output"]
    cr = rates.get("cache_read", pin)
    return ((inp - cached) * pin + cached * cr + out * pout) / 1e6


HEADER_RE = re.compile(r"round: reviewing PR #(?P<pr>\d+) @ (?P<sha>[0-9a-f]+)")
TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")
FNAME_TS_RE = re.compile(r"task-(\d{8})-(\d{6})\.log$")
COST_RE = re.compile(
    r"^ROUND (?P<n>\d+) \(commit\) (?P<verdict>approved|changes requested|blocked)\s+"
    r"\(ran (?P<k>\d+):.*?;\s*cost \$(?P<cost>[0-9.]+),\s*today \$(?P<today>[0-9.]+)",
    re.MULTILINE,
)


def store_for(repo: str) -> Path:
    return Path.home() / ".cache" / "tauceti-review" / "store" / repo.replace("/", "__")


# --------------------------------------------------------------------------- DB
def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    if ver and ver != SCHEMA_VERSION:
        for t in ("review_rounds", "rubric_runs", "seen_logs"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS seen_logs (log_file TEXT PRIMARY KEY, is_review INTEGER);
        CREATE TABLE IF NOT EXISTS review_rounds (
            key                 TEXT PRIMARY KEY,   -- 'store:PR:ROUND' or 'log:<file>'
            source              TEXT NOT NULL,
            pr                  INTEGER NOT NULL,
            round_no            INTEGER,
            ts                  TEXT NOT NULL,
            day                 TEXT NOT NULL,
            verdict             TEXT,
            rubrics_run         INTEGER,
            input_tokens        INTEGER,
            cached_input_tokens INTEGER,
            output_tokens       INTEGER,
            reasoning_tokens    INTEGER,
            cost                REAL NOT NULL DEFAULT 0,
            est_frac            REAL
        );
        CREATE TABLE IF NOT EXISTS rubric_runs (
            run_key TEXT PRIMARY KEY,  -- store: 'pr:round:rubric'; data: the run's dedupe_key
            pr INTEGER, round_no INTEGER, rubric TEXT, provider TEXT, model TEXT,
            input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
            reasoning_tokens INTEGER,
            cost_usd REAL,        -- faithful: priced as of the run's own date (the headline metric)
            cost_today REAL,      -- forecast: the same tokens valued at today's prices.json
            cost_recorded REAL,   -- what the engine wrote at review time (stale rates / old formula)
            cost_estimated INTEGER, verdict TEXT, ts TEXT
        );
        CREATE TABLE IF NOT EXISTS prs (
            pr INTEGER PRIMARY KEY, state TEXT, additions INTEGER, deletions INTEGER,
            created_at TEXT, merged_at TEXT, closed_at TEXT, title TEXT,
            author_agent TEXT, author_name TEXT, fetched_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rounds_pr ON review_rounds(pr);
        """
    )
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return con


# --------------------------------------------------------- ingest: store / data
def _norm_ts(ts: str | None) -> str:
    if not ts:
        return "1970-01-01 00:00:00"
    return ts.replace("T", " ")[:19]


def _usage_tokens(u: dict) -> tuple[int, int, int, int]:
    return (u.get("input_tokens", 0) or 0,
            u.get("cached_input_tokens", 0) or u.get("cache_read_input_tokens", 0) or 0,
            u.get("output_tokens", 0) or 0,
            u.get("reasoning_output_tokens", 0) or 0)


def _add_run(con, agg, history, today, unpriced, *, run_key, pr, rd, rubric, provider, model,
             it, ct, ot, rt, recorded, est, verdict, ts):
    """Derive the three cost lenses for one rubric run, persist it, and fold into the round agg.
    Estimated (codex/pi) costs are recomputed from tokens — faithfully at the run's own date, and
    at today's prices for the forecast; real provider-billed costs are kept as recorded."""
    if est:
        win = price_window(history, model, ts[:10])
        now = price_window(history, model, today)
        if win is None:
            win = now = {**DEFAULT_PRICE}
            unpriced[model] = unpriced.get(model, 0) + 1
        cost = cost_from_window(win, it, ct, ot)
        cost_today = cost_from_window(now, it, ct, ot)
    else:
        cost = cost_today = recorded
    con.execute(
        "INSERT OR REPLACE INTO rubric_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_key, pr, rd, rubric, provider, model, it, ct, ot, rt,
         cost, cost_today, recorded, est, verdict, ts))
    a = agg.setdefault((pr, rd), dict(it=0, ct=0, ot=0, rt=0, cost=0.0, est=0, n=0, req=0, ts=ts))
    a["it"] += it; a["ct"] += ct; a["ot"] += ot; a["rt"] += rt
    a["cost"] += cost; a["est"] += est; a["n"] += 1
    if verdict == "request_changes":
        a["req"] += 1
    a["ts"] = min(a["ts"], ts) if a["ts"] else ts


def _flush_rounds(con, agg, source):
    for (pr, rd), a in agg.items():
        con.execute(
            "INSERT OR REPLACE INTO review_rounds "
            "(key,source,pr,round_no,ts,day,verdict,rubrics_run,"
            " input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,cost,est_frac) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{source}:{pr}:{rd}", source, pr, rd, a["ts"], a["ts"][:10],
             "changes requested" if a["req"] else "approved",
             a["n"], a["it"], a["ct"], a["ot"], a["rt"], a["cost"],
             a["est"] / a["n"] if a["n"] else None))


def _warn_unpriced(unpriced):
    if unpriced:
        miss = ", ".join(f"{m or '?'}×{n}" for m, n in sorted(unpriced.items()))
        print(f"  warning: {sum(unpriced.values())} rubric run(s) used a model absent from "
              f"prices.json (fell back to {DEFAULT_PRICE['input']}/{DEFAULT_PRICE['output']}"
              f"): {miss}", file=sys.stderr)


def ingest_store(con: sqlite3.Connection, store: Path) -> tuple[int, int]:
    reviews = store / "reviews"
    if not reviews.is_dir():
        raise FileNotFoundError(f"no review store at {reviews}")
    ts_map: dict[tuple[int, int], str] = {}
    ledger = store / "ledger.json"
    if ledger.exists():
        L = json.loads(ledger.read_text())
        for pr, info in (L.get("prs") or {}).items():
            for rd in info.get("rounds", []):
                ts_map[(int(pr), rd.get("round"))] = rd.get("ts")
    con.execute("DELETE FROM rubric_runs")
    con.execute("DELETE FROM review_rounds WHERE source='store'")
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    unpriced: dict[str, int] = {}
    agg: dict[tuple[int, int], dict] = {}
    nrub = 0
    for f in sorted(reviews.glob("*/*/*.json")):
        if f.stem == "scoreboard":
            continue
        try:
            d = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict) or ("usage" not in d and "cost_usd" not in d):
            continue
        try:
            pr, rd = int(f.parent.parent.name), int(f.parent.name)
        except ValueError:
            continue
        it, ct, ot, rt = _usage_tokens(d.get("usage") or {})
        raw_ts = ts_map.get((pr, rd))
        ts = (_norm_ts(raw_ts) if raw_ts
              else datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"))
        _add_run(con, agg, history, today, unpriced, run_key=f"{pr}:{rd}:{f.stem}",
                 pr=pr, rd=rd, rubric=f.stem,
                 provider=d.get("provider"), model=d.get("model"), it=it, ct=ct, ot=ot, rt=rt,
                 recorded=d.get("cost_usd", 0) or 0, est=1 if d.get("cost_estimated") else 0,
                 verdict=(d.get("verdict_obj") or {}).get("verdict") or d.get("verdict"), ts=ts)
        nrub += 1
    _flush_rounds(con, agg, "store")
    con.commit()
    _warn_unpriced(unpriced)
    return len(agg), nrub


def ingest_data(con: sqlite3.Connection, data_dir: Path, include_shadows: bool = False) -> tuple[int, int]:
    """Read the durable public archive (a TauCetiData checkout): records/runs/<pr>/<run_id>.json.
    This is the reproducible source — anyone can clone TauCetiData and get the same numbers,
    independent of a local cache. Defaults to the production arm (the reviews that gated PRs);
    pass include_shadows to also count the archived A/B experiment arms."""
    runs = data_dir / "records" / "runs"
    if not runs.is_dir():
        raise FileNotFoundError(f"no records/runs under {data_dir} (clone TauCetiProject/TauCetiData)")
    con.execute("DELETE FROM rubric_runs")
    con.execute("DELETE FROM review_rounds WHERE source='data'")
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    unpriced: dict[str, int] = {}
    agg: dict[tuple[int, int], dict] = {}
    seen: set[str] = set()
    nrub = skipped_shadow = skipped_dup = 0
    for f in sorted(runs.glob("*/*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict) or "usage" not in d:
            continue
        if not include_shadows and (d.get("arm") or "production") != "production":
            skipped_shadow += 1
            continue
        # dedupe_key identifies one logical run; the same run can be archived from several
        # backfill sources, so collapse to one record (and it is the rubric_runs key, since
        # (pr,round,rubric) collapses genuinely distinct runs at different commits/models).
        run_key = d.get("dedupe_key") or d.get("run_id") or f.stem
        if run_key in seen:
            skipped_dup += 1
            continue
        seen.add(run_key)
        try:
            pr, rd = int(d["pr"]), int(d.get("round") or 0)
        except (KeyError, ValueError, TypeError):
            continue
        it, ct, ot, rt = _usage_tokens(d.get("usage") or {})
        _add_run(con, agg, history, today, unpriced, run_key=run_key, pr=pr, rd=rd,
                 rubric=d.get("rubric"), provider=d.get("provider"), model=d.get("model"),
                 it=it, ct=ct, ot=ot, rt=rt, recorded=d.get("cost_usd", 0) or 0,
                 est=1 if d.get("cost_estimated") else 0,
                 verdict=d.get("verdict"), ts=_norm_ts(d.get("started_at")))
        nrub += 1
    _flush_rounds(con, agg, "data")
    con.commit()
    _warn_unpriced(unpriced)
    if skipped_shadow:
        print(f"  ({skipped_shadow} shadow-arm runs excluded; --include-shadows to count them)",
              file=sys.stderr)
    if skipped_dup:
        print(f"  ({skipped_dup} duplicate run records collapsed by dedupe_key)", file=sys.stderr)
    return len(agg), nrub
    return len(agg), nrub


# ---------------------------------------------------------------- ingest: logs
def _round_ts(text: str, fname: str) -> str:
    m = TS_RE.search(text)
    if m:
        return m.group("ts")
    fm = FNAME_TS_RE.search(fname)
    if fm:
        d, t = fm.groups()
        return f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
    return "1970-01-01 00:00:00"


def ingest_logs(con: sqlite3.Connection, logs_dir: Path, reingest: bool = False) -> tuple[int, int]:
    if reingest:
        con.execute("DELETE FROM review_rounds WHERE source='log'")
        con.execute("DELETE FROM seen_logs")
    seen = {r[0] for r in con.execute("SELECT log_file FROM seen_logs")}
    added = skipped = 0
    for path in sorted(logs_dir.glob("task-*.log")):
        name = path.name
        if name in seen:
            skipped += 1
            continue
        try:
            head = path.open("r", errors="replace").read(4096)
        except OSError:
            continue
        if "round: reviewing PR #" not in head:
            con.execute("INSERT OR REPLACE INTO seen_logs VALUES (?,0)", (name,))
            continue
        text = path.read_text(errors="replace")
        hm = HEADER_RE.search(text)
        if not hm:
            con.execute("INSERT OR REPLACE INTO seen_logs VALUES (?,0)", (name,))
            continue
        ts = _round_ts(text, name)
        pr = int(hm.group("pr"))
        costs = list(COST_RE.finditer(text))
        if costs:
            c = costs[-1]
            verdict, cost = c.group("verdict"), float(c.group("cost"))
            round_no, rubrics = int(c.group("n")), int(c.group("k"))
        else:
            verdict, cost, round_no, rubrics = "errored", 0.0, None, None
        con.execute(
            "INSERT OR REPLACE INTO review_rounds "
            "(key,source,pr,round_no,ts,day,verdict,rubrics_run,cost) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"log:{name}", "log", pr, round_no, ts, ts[:10], verdict, rubrics, cost),
        )
        con.execute("INSERT OR REPLACE INTO seen_logs VALUES (?,1)", (name,))
        added += 1
    con.commit()
    return added, skipped


def ingest(con, source, store, logs_dir, data_dir=None, include_shadows=False, reingest=False) -> str:
    have_data = data_dir is not None and (data_dir / "records" / "runs").is_dir()
    have_store = (store / "reviews").is_dir()
    chosen = source
    if source == "auto":
        # Prefer the durable archive when a checkout is given (reproducible); else the local
        # cache; else the worker logs.
        chosen = "data" if have_data else "store" if have_store else "logs"
    if chosen == "data":
        if data_dir is None:
            raise SystemExit("data source needs --data-dir (a TauCetiData checkout)")
        rounds, rubrics = ingest_data(con, data_dir, include_shadows)
        return f"data: {rounds} rounds / {rubrics} rubric runs from TauCetiData (tokens + $)"
    if chosen == "store":
        rounds, rubrics = ingest_store(con, store)
        return f"store: {rounds} rounds / {rubrics} rubric runs (tokens + $)"
    if logs_dir is None:
        raise SystemExit("log source needs --logs-dir (e.g. the worker's logs/); "
                         "the store cache was not found at " + str(store / "reviews"))
    added, skipped = ingest_logs(con, logs_dir, reingest)
    return f"logs: {added} new rounds ($ only, no tokens; {skipped} seen)"


# ------------------------------------------------------------------- PR lookup
def _agent_of(name: str) -> str:
    n = (name or "").lower()
    if "codex" in n:
        return "codex"
    if "claude" in n or "opus" in n or "sonnet" in n:
        return "claude"
    if "kim" in n or "morrison" in n:
        return "human"
    return "other"


def _authoring_agent(data: dict) -> tuple[str, str]:
    body = data.get("body") or ""
    if re.search(r"Prepared with Codex|with Codex\b", body, re.I):
        agent = "codex"
    elif re.search(r"Claude Code|Prepared with Claude|with Claude\b|Opus", body, re.I):
        agent = "claude"
    else:
        agent = None
    name = ""
    commits = data.get("commits") or []
    if commits:
        authors = commits[0].get("authors") or []
        for a in authors:
            if _agent_of(a.get("name", "")) not in ("human", "other"):
                name = a.get("name", "")
                break
        if not name and authors:
            name = authors[0].get("name", "")
    if agent is None:
        agent = _agent_of(name)
    return agent, name


def _gh_pr(repo: str, pr: int) -> dict | None:
    try:
        out = subprocess.run(
            ["gh", "pr", "view", str(pr), "--repo", repo, "--json",
             "number,state,additions,deletions,createdAt,mergedAt,closedAt,title,body,commits"],
            capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None


def refresh_prs(con, repo, refresh_all=False) -> int:
    prs = [r[0] for r in con.execute("SELECT DISTINCT pr FROM review_rounds ORDER BY pr")]
    cached = {r["pr"]: r["state"] for r in con.execute("SELECT pr,state FROM prs")}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated = 0
    for pr in prs:
        if not refresh_all and cached.get(pr) in ("MERGED", "CLOSED"):
            continue
        data = _gh_pr(repo, pr)
        if not data:
            print(f"  PR #{pr}: gh lookup failed (skipped)", file=sys.stderr)
            continue
        agent, author = _authoring_agent(data)
        con.execute(
            "INSERT OR REPLACE INTO prs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pr, data.get("state"), data.get("additions"), data.get("deletions"),
             data.get("createdAt"), data.get("mergedAt"), data.get("closedAt"),
             data.get("title"), agent, author, now))
        updated += 1
        print(f"  PR #{pr}: {data.get('state'):7} +{data.get('additions')}  [{agent}]",
              file=sys.stderr)
    con.commit()
    return updated


# ----------------------------------------------------------------- aggregation
TOK_COLS = "input_tokens,cached_input_tokens,output_tokens,reasoning_tokens"


def load_rows(con):
    return list(con.execute(
        "SELECT r.*, p.state, p.additions, p.deletions, p.title, p.author_agent "
        "FROM review_rounds r LEFT JOIN prs p ON p.pr=r.pr ORDER BY r.ts"))


def per_pr(con):
    rows = con.execute(
        f"""SELECT r.pr pr, COUNT(*) rounds, SUM(r.cost) cost,
               SUM(r.input_tokens) it, SUM(r.cached_input_tokens) ct,
               SUM(r.output_tokens) ot, SUM(r.reasoning_tokens) rt,
               p.state state, p.additions additions, p.deletions deletions,
               p.title title, p.author_agent agent
           FROM review_rounds r LEFT JOIN prs p ON p.pr=r.pr
           GROUP BY r.pr ORDER BY cost DESC""")
    out = []
    for r in rows:
        d = dict(r)
        add = d["additions"] or 0
        d["dollar_per_loc"] = (d["cost"] / add) if add else None
        d["tok_per_loc"] = (((d["ot"] or 0)) / add) if add else None
        out.append(d)
    return out


def summary(con):
    rows = per_pr(con)
    total = sum(d["cost"] for d in rows)
    has_tokens = any((d["it"] or 0) for d in rows)
    buckets = {k: dict(cost=0.0, add=0, prs=0, rounds=0, it=0, ct=0, ot=0, rt=0)
               for k in ("MERGED", "CLOSED", "OPEN", "UNKNOWN")}
    for d in rows:
        b = buckets.setdefault(d["state"] or "UNKNOWN",
                               dict(cost=0.0, add=0, prs=0, rounds=0, it=0, ct=0, ot=0, rt=0))
        b["cost"] += d["cost"]; b["add"] += d["additions"] or 0
        b["prs"] += 1; b["rounds"] += d["rounds"]
        for k in ("it", "ct", "ot", "rt"):
            b[k] += d[k] or 0
    m = buckets["MERGED"]
    return dict(total=total, buckets=buckets, rows=rows, has_tokens=has_tokens,
                dollar_per_merged_loc=(m["cost"] / m["add"]) if m["add"] else None,
                outtok_per_merged_loc=(m["ot"] / m["add"]) if m["add"] else None)


def windowed(con, window):
    agg = defaultdict(lambda: [0.0, 0, 0])  # cost, rounds, output_tokens
    for r in con.execute("SELECT day,cost,output_tokens FROM review_rounds ORDER BY day"):
        if window == "week":
            y, w, _ = date.fromisoformat(r["day"]).isocalendar()
            key = f"{y}-W{w:02d}"
        else:
            key = r["day"]
        agg[key][0] += r["cost"]; agg[key][1] += 1
        agg[key][2] += r["output_tokens"] or 0
    return [(k, *v) for k, v in sorted(agg.items())]


# --------------------------------------------------------------------- reports
def fmt_money(x):
    return f"${x:,.2f}" if x is not None else "—"


def fmt_tok(x):
    if not x:
        return "—"
    if x >= 1e6:
        return f"{x/1e6:.1f}M"
    if x >= 1e3:
        return f"{x/1e3:.0f}k"
    return str(int(x))


def report(con, window="day", csv_path=None):
    s = summary(con)
    b = s["buckets"]
    tok = s["has_tokens"]
    n_rounds = con.execute("SELECT COUNT(*) FROM review_rounds").fetchone()[0]
    n_prs = con.execute("SELECT COUNT(DISTINCT pr) FROM review_rounds").fetchone()[0]
    src = con.execute("SELECT DISTINCT source FROM review_rounds").fetchall()
    src = ",".join(r[0] for r in src) or "—"

    print("\n=== Tau Ceti review-cost report ===")
    print(f"source: {src} · {n_rounds} rounds over {n_prs} PRs")
    if tok:
        ti = sum(d["it"] or 0 for d in s["rows"]); tc = sum(d["ct"] or 0 for d in s["rows"])
        to = sum(d["ot"] or 0 for d in s["rows"]); tr = sum(d["rt"] or 0 for d in s["rows"])
        est = con.execute("SELECT AVG(est_frac) FROM review_rounds WHERE est_frac IS NOT NULL").fetchone()[0]
        print(f"\nTOKENS   input {fmt_tok(ti)} (cached {fmt_tok(tc)}, "
              f"{100*tc/ti:.0f}%) · output {fmt_tok(to)} · reasoning {fmt_tok(tr)}")
        print(f"DOLLARS  {fmt_money(s['total'])} imputed — each run priced as of its own date "
              f"(faithful; {100*(est or 0):.0f}% of rounds derived from tokens, tokens measured)")
        rec = con.execute("SELECT SUM(cost_usd), SUM(cost_today), SUM(cost_recorded) "
                          "FROM rubric_runs").fetchone()
        if rec and rec[2] is not None:
            faithful, fc, recorded = rec[0] or 0, rec[1] or 0, rec[2] or 0
            d = faithful - recorded
            print(f"         vs at today's prices.json (forecast): {fmt_money(fc)}"
                  + ("  (same — no provider price changes on record yet)"
                     if abs(fc - faithful) < 0.01 else ""))
            print(f"         vs engine's as-recorded total: {fmt_money(recorded)} "
                  f"({'+' if d >= 0 else '−'}{fmt_money(abs(d))} — old runs used a stale table "
                  f"with no cache discount, since fixed)")
    else:
        print(f"DOLLARS  {fmt_money(s['total'])} imputed  (no token data — log source)")

    print("\n-- by PR outcome --")
    hdr = f"{'outcome':9}{'PRs':>4}{'rounds':>7}{'review$':>10}{'$/LOC':>8}"
    if tok:
        hdr += f"{'in(tok)':>9}{'out(tok)':>9}{'out/LOC':>8}"
    print(hdr)
    for st in ("MERGED", "CLOSED", "OPEN", "UNKNOWN"):
        v = b.get(st)
        if not v or v["prs"] == 0:
            continue
        dpl = f"${v['cost']/v['add']:.3f}" if v["add"] else "—"
        line = f"{st:9}{v['prs']:>4}{v['rounds']:>7}{fmt_money(v['cost']):>10}{dpl:>8}"
        if tok:
            opl = f"{v['ot']/v['add']:.0f}" if v["add"] else "—"
            line += f"{fmt_tok(v['it']):>9}{fmt_tok(v['ot']):>9}{opl:>8}"
        print(line)

    print(f"\n>>> $ per MERGED line of code: "
          f"{('$%.4f'%s['dollar_per_merged_loc']) if s['dollar_per_merged_loc'] else '—'}")
    if tok and s["outtok_per_merged_loc"]:
        print(f">>> output tokens per MERGED line of code: {s['outtok_per_merged_loc']:.0f}")
    wasted = b["CLOSED"]["cost"]
    pct = 100 * wasted / s["total"] if s["total"] else 0
    print(f">>> $ on CLOSED-unmerged PRs (wasted): {fmt_money(wasted)} ({pct:.0f}% of total)")
    if tok:
        print(f">>> tokens on CLOSED PRs (wasted): in {fmt_tok(b['CLOSED']['it'])} / "
              f"out {fmt_tok(b['CLOSED']['ot'])}")

    print("\n-- by authoring agent (whose PRs the loop reviewed) --")
    agg = defaultdict(lambda: [0.0, 0, 0, 0, 0])  # cost, rounds, mLOC, in, out
    for d in s["rows"]:
        a = agg[d["agent"] or "?"]
        a[0] += d["cost"]; a[1] += d["rounds"]; a[3] += d["it"] or 0; a[4] += d["ot"] or 0
        if d["state"] == "MERGED":
            a[2] += d["additions"] or 0
    head = f"{'agent':8}{'review$':>10}{'rounds':>7}{'mLOC':>7}{'$/mLOC':>9}"
    if tok:
        head += f"{'in':>8}{'out':>8}"
    print(head)
    for a, (c, rd, ma, ti2, to2) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        dpl = f"${c/ma:.4f}" if ma else "—"
        line = f"{a:8}{fmt_money(c):>10}{rd:>7}{ma:>7}{dpl:>9}"
        if tok:
            line += f"{fmt_tok(ti2):>8}{fmt_tok(to2):>8}"
        print(line)

    print(f"\n-- review $ / output-tokens per {window} --")
    for key, cost, n, ot in windowed(con, window):
        bar = "#" * min(48, int(cost / max(0.25, s['total'] / 160)))
        ts = f" · out {fmt_tok(ot)}" if tok else ""
        print(f"{key}  {fmt_money(cost):>9} ({n:>3} rnd){ts}  {bar}")

    print("\n-- top PRs by review spend --")
    h = f"{'PR':>5}{'state':>8}{'agent':>7}{'rnd':>4}{'review$':>9}{'$/LOC':>8}"
    if tok:
        h += f"{'out(tok)':>9}"
    print(h + "  title")
    for d in s["rows"][:15]:
        dpl = f"${d['dollar_per_loc']:.3f}" if d["dollar_per_loc"] else "—"
        line = (f"{d['pr']:>5}{str(d['state'] or '?'):>8}{str(d['agent'] or '?'):>7}"
                f"{d['rounds']:>4}{fmt_money(d['cost']):>9}{dpl:>8}")
        if tok:
            line += f"{fmt_tok(d['ot']):>9}"
        print(line + f"  {(d['title'] or '')[:42]}")

    if csv_path:
        import csv
        with open(csv_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["pr", "state", "agent", "rounds", "review_cost_usd",
                        "input_tokens", "cached_input_tokens", "output_tokens",
                        "reasoning_tokens", "additions", "deletions",
                        "dollar_per_added_loc", "output_tok_per_added_loc", "title"])
            for d in s["rows"]:
                w.writerow([d["pr"], d["state"], d["agent"], d["rounds"], f"{d['cost']:.4f}",
                            d["it"], d["ct"], d["ot"], d["rt"], d["additions"], d["deletions"],
                            f"{d['dollar_per_loc']:.6f}" if d["dollar_per_loc"] else "",
                            f"{d['tok_per_loc']:.2f}" if d["tok_per_loc"] else "", d["title"]])
        print(f"\nwrote per-PR CSV -> {csv_path}")
    print()


# ----------------------------------------------------------------------- graph
COLORS = {"MERGED": "#2e7d32", "CLOSED": "#c62828", "OPEN": "#1565c0", "UNKNOWN": "#9e9e9e"}


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _panel(x0, y0, w, h, title):
    return (f'<g><rect x="{x0}" y="{y0}" width="{w}" height="{h}" fill="#fff" stroke="#ddd"/>'
            f'<text x="{x0+8}" y="{y0+18}" font-size="13" font-weight="bold" fill="#222">'
            f'{_esc(title)}</text></g>')


def graph(con, out):
    rows = load_rows(con)
    if not rows:
        print("no data; run `ingest` and `prs` first.", file=sys.stderr)
        return
    s = summary(con)
    b = s["buckets"]
    total = s["total"]
    wasted = b["CLOSED"]["cost"]
    tok = s["has_tokens"]
    W, PH, pad = 980, 240, 16
    NP = 4 if tok else 3
    H = 96 + NP * (PH + pad)
    P = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
         f'font-family="-apple-system,Helvetica,Arial,sans-serif">',
         f'<rect width="{W}" height="{H}" fill="#fafafa"/>',
         f'<text x="{pad}" y="28" font-size="18" font-weight="bold">'
         f'Tau Ceti AI-review — tokens &amp; imputed $</text>']
    sub = f'total {_esc(fmt_money(total))} · $/merged-LOC ${s["dollar_per_merged_loc"]:.4f} · ' \
          f'wasted {_esc(fmt_money(wasted))} ({100*wasted/total:.0f}%)'
    if tok:
        ti = sum(d["it"] or 0 for d in s["rows"]); to = sum(d["ot"] or 0 for d in s["rows"])
        sub += f' · in {fmt_tok(ti)} / out {fmt_tok(to)} tok'
    P.append(f'<text x="{pad}" y="50" font-size="13" fill="#555">{_esc(sub)}</text>')
    lx = pad
    for st in ("MERGED", "CLOSED", "OPEN"):
        P.append(f'<rect x="{lx}" y="62" width="12" height="12" fill="{COLORS[st]}"/>'
                 f'<text x="{lx+16}" y="72" font-size="11" fill="#444">{st}</text>')
        lx += 90

    def axes(y0, title):
        P.append(_panel(pad, y0, W - 2 * pad, PH, title))
        return pad + 56, y0 + PH - 28, W - 2 * pad - 76, PH - 56

    # daily cost by outcome
    daily = defaultdict(lambda: defaultdict(float))
    dtok = defaultdict(float)
    for r in rows:
        daily[r["day"]][r["state"] or "UNKNOWN"] += r["cost"]
        dtok[r["day"]] += r["output_tokens"] or 0
    days = sorted(daily)
    y = 84
    px0, base, pw, ph = axes(y, "Daily review $ by PR outcome")
    dmax = max((sum(daily[d].values()) for d in days), default=1) or 1
    bw = pw / max(1, len(days))
    for i, d in enumerate(days):
        x = px0 + i * bw; yb = base
        for st in ("MERGED", "CLOSED", "OPEN", "UNKNOWN"):
            v = daily[d].get(st, 0.0)
            if v <= 0:
                continue
            hh = v / dmax * ph
            P.append(f'<rect x="{x+2:.1f}" y="{yb-hh:.1f}" width="{bw-4:.1f}" '
                     f'height="{hh:.1f}" fill="{COLORS[st]}"/>')
            yb -= hh
        P.append(f'<text x="{x+bw/2:.1f}" y="{base+14}" font-size="9" '
                 f'text-anchor="middle" fill="#666">{d[5:]}</text>')
    P.append(f'<text x="{px0-8}" y="{y+30}" font-size="10" text-anchor="end" fill="#888">${dmax:,.0f}</text>')

    # cumulative spend
    y = 84 + (PH + pad)
    px0, base, pw, ph = axes(y, f"Cumulative review $ (total {fmt_money(total)})")
    rs = sorted(rows, key=lambda r: r["ts"]); run = 0.0; cum = []
    for r in rs:
        run += r["cost"]; cum.append(run)
    n = len(cum)
    pts = " ".join(f"{px0+(i/(n-1 or 1))*pw:.1f},{base-(c/(total or 1))*ph:.1f}"
                   for i, c in enumerate(cum))
    P.append(f'<polyline points="{pts}" fill="none" stroke="#6a1b9a" stroke-width="2"/>')
    P.append(f'<text x="{px0-8}" y="{y+30}" font-size="10" text-anchor="end" fill="#888">{fmt_money(total)}</text>')

    # cumulative $/merged-LOC
    y = 84 + 2 * (PH + pad)
    px0, base, pw, ph = axes(y, "Cumulative $ per merged LOC (PR order)")
    merged = sorted([d for d in s["rows"] if d["state"] == "MERGED" and (d["additions"] or 0) > 0],
                    key=lambda d: d["pr"])
    cc = ca = 0.0; ys = []
    for d in merged:
        cc += d["cost"]; ca += d["additions"]; ys.append(cc / ca)
    if ys:
        ymax = max(ys) * 1.1 or 1; m = len(ys)
        pts = " ".join(f"{px0+(i/(m-1 or 1))*pw:.1f},{base-(v/ymax)*ph:.1f}"
                       for i, v in enumerate(ys))
        P.append(f'<polyline points="{pts}" fill="none" stroke="#00838f" stroke-width="2"/>')
        fy = base - ys[-1] / ymax * ph
        P.append(f'<line x1="{px0}" y1="{fy:.1f}" x2="{px0+pw}" y2="{fy:.1f}" '
                 f'stroke="#999" stroke-dasharray="4 3"/>')
        P.append(f'<text x="{px0+pw}" y="{fy-4:.1f}" font-size="11" text-anchor="end" '
                 f'fill="#00838f">${ys[-1]:.4f}/LOC</text>')

    # daily output-token volume (only when tokens available)
    if tok:
        y = 84 + 3 * (PH + pad)
        px0, base, pw, ph = axes(y, "Daily output-token volume")
        tmax = max(dtok.values(), default=1) or 1
        for i, d in enumerate(days):
            x = px0 + i * bw; hh = dtok[d] / tmax * ph
            P.append(f'<rect x="{x+2:.1f}" y="{base-hh:.1f}" width="{bw-4:.1f}" '
                     f'height="{hh:.1f}" fill="#ef6c00"/>')
            P.append(f'<text x="{x+bw/2:.1f}" y="{base+14}" font-size="9" '
                     f'text-anchor="middle" fill="#666">{d[5:]}</text>')
        P.append(f'<text x="{px0-8}" y="{y+30}" font-size="10" text-anchor="end" '
                 f'fill="#888">{fmt_tok(tmax)}</text>')

    P.append("</svg>")
    out = out.with_suffix(".svg")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(P))
    print(f"wrote graph -> {out}")


# ------------------------------------------------------------------------ main
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--logs-dir", type=Path, default=None,
                   help="worker logs/ dir, only for --source logs (the $-only fallback)")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--store", type=Path, default=None,
                   help="review-engine store dir (default ~/.cache/tauceti-review/store/<repo>)")
    p.add_argument("--data-dir", type=Path, default=None,
                   help="TauCetiData checkout — the durable, public, reproducible source")
    p.add_argument("--source", choices=["auto", "data", "store", "logs"], default="auto")
    p.add_argument("--include-shadows", action="store_true",
                   help="(data source) also count archived A/B shadow-arm runs")
    sub = p.add_subparsers(dest="cmd", required=True)
    ig = sub.add_parser("ingest"); ig.add_argument("--reingest", action="store_true")
    pp = sub.add_parser("prs"); pp.add_argument("--refresh-all", action="store_true")
    rp = sub.add_parser("report")
    rp.add_argument("--window", choices=["day", "week"], default="day"); rp.add_argument("--csv")
    gp = sub.add_parser("graph")
    gp.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap = sub.add_parser("all")
    ap.add_argument("--window", choices=["day", "week"], default="day")
    ap.add_argument("--graph", action="store_true")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)

    a = p.parse_args(argv)
    store = a.store or store_for(a.repo)
    con = connect(a.db)

    if a.cmd == "ingest":
        print(ingest(con, a.source, store, a.logs_dir, a.data_dir, a.include_shadows,
                     getattr(a, "reingest", False)))
    elif a.cmd == "prs":
        print(f"refreshed {refresh_prs(con, a.repo, a.refresh_all)} PRs")
    elif a.cmd == "report":
        report(con, a.window, a.csv)
    elif a.cmd == "graph":
        graph(con, a.out)
    elif a.cmd == "all":
        print(ingest(con, a.source, store, a.logs_dir, a.data_dir, a.include_shadows))
        print(f"refreshed {refresh_prs(con, a.repo)} PRs")
        report(con, a.window)
        if a.graph:
            graph(con, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
