#!/usr/bin/env python3
"""Write-time archival for the Tau Ceti review runner.

Reviews are durable the moment they finish: the runner writes one JSON record per execution
into a local OUTBOX (`<store>/outbox/`), and a separate sync step drains the outbox into a
checkout of FormalFrontier/TauCetiData (write-if-absent, commit, rebase, push). The split is
load-bearing: a data-repo push outage must never fail an otherwise-good review, and in CI the
outbox rides along in the reviews-branch store commit, so an unsynced record survives the
runner and syncs on a later run.

Records are public: they are built by the caller from an explicit field allowlist (no provider
session ids, no raw stderr), and blob text passes through redact() here. Blobs (reviewed diffs,
reviewer transcripts) are content-addressed under blobs/<aa>/<sha256>.gz so identical content
is stored once and a later move to LFS/release assets is a file move, not a schema change.

Usage as a library: archive_run / archive_round / archive_post.
Usage as a command:  python3 archive.py sync --store <store> --data-dir <checkout> [--remote URL]
"""
import argparse
import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

DATA_REPO = "FormalFrontier/TauCetiData"

# Conservative scrubbing for blob text that may quote tool/CLI output: known credential shapes
# and home paths. Records themselves never carry these fields, so this is defense in depth.
_REDACT = [
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|"
                r"github_pat_[A-Za-z0-9_]{8,}|xoxb-[A-Za-z0-9-]{8,})\b"), "[REDACTED]"),
    (re.compile(r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)=\S+"),
     r"\1=[REDACTED]"),
    (re.compile(r"(/home/|/Users/)[^/\s]+"), r"\1[user]"),
]


def redact(text):
    for pat, rep in _REDACT:
        text = pat.sub(rep, text)
    return text


def write_blob(outbox, text):
    """Store text content-addressed (sha256 of the redacted bytes); return the key."""
    data = redact(text).encode()
    sha = hashlib.sha256(data).hexdigest()
    path = pathlib.Path(outbox) / "blobs" / sha[:2] / f"{sha}.gz"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # mtime=0 keeps the gzip output deterministic for a given content.
        with gzip.GzipFile(filename="", mode="wb", fileobj=open(path, "wb"), mtime=0) as f:
            f.write(data)
    return sha


def write_record(outbox, rel, record, id_field=None):
    """Write-if-absent, but never lossy on a collision.

    Same path + identical content is a no-op. Same path + *different* content is a real id
    collision: rather than clobber the existing record or drop the new one, we preserve both by
    writing the newcomer to a content-disambiguated sibling `<stem>-<disc>.json`. Crucially we
    also rewrite the record's own primary key (`id_field`, e.g. run_id / round_id) to carry the
    same `disc`, because the derived DB keys those columns PRIMARY KEY: a sibling file that kept
    the original id would silently collapse back to one row on the next `build_db`. The real
    defense against collisions is unique-at-source ids (see review.py); this is the backstop.

    Records carrying git conflict markers are refused outright so a botched merge can never be
    committed as data (it would otherwise be silently skipped by the DB loader)."""
    outbox = pathlib.Path(outbox)
    body = json.dumps(record, indent=1, sort_keys=True) + "\n"
    if "<<<<<<<" in body or "\n>>>>>>>" in body:
        raise RuntimeError(f"refusing to archive record with conflict markers at {rel}")
    path = outbox / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path
    if path.read_text() == body:
        return path
    disc = hashlib.sha256(body.encode()).hexdigest()[:12]
    if id_field and record.get(id_field):
        record = dict(record, **{id_field: f"{record[id_field]}-{disc}"})
        body = json.dumps(record, indent=1, sort_keys=True) + "\n"
    sib = path.with_name(f"{path.stem}-{disc}{path.suffix}")
    if sib.exists():
        if sib.read_text() == body:
            return sib
        raise RuntimeError(f"archive collision at {rel}: sibling {sib.name} already differs")
    sib.write_text(body)
    print(f"archive: id collision at {rel}; preserved newcomer as {sib.name}", file=sys.stderr)
    return sib


def archive_run(outbox, record, transcript_text=None, diff_text=None):
    if transcript_text:
        record["transcript_blob"] = write_blob(outbox, transcript_text)
    if diff_text:
        record["diff_blob"] = write_blob(outbox, diff_text)
    return write_record(outbox, f"records/runs/{record['pr']}/{record['run_id']}.json",
                        record, id_field="run_id")


def archive_round(outbox, record):
    return write_record(outbox, f"records/rounds/{record['pr']}/{record['round_id']}.json",
                        record, id_field="round_id")


def archive_post(outbox, record):
    # The timestamp disambiguates multiple posts within one ledger round (e.g. an init-mode
    # "running now" post followed by the real one, or a budget-capped round that never appends
    # a ledger round record).
    ts = re.sub(r"[-:]|\..*", "", record.get("posted_at") or "")
    rid = f"{record['pr']}-{record.get('round') or 'r'}" + (f"-{ts}" if ts else "")
    return write_record(outbox, f"records/posts/{record['pr']}/{rid}.json", record)


def _git(args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr[-500:]}")
    return r


def _heal(data_dir):
    """Clear any half-finished rebase/merge left by a crash or a botched prior sync. A stale
    .git/rebase-merge is exactly what wedged the checkout for days: every `pull --rebase` then
    aborted with 'already a rebase-merge directory' and every push stayed non-fast-forward."""
    for op in ("rebase", "merge", "cherry-pick"):
        _git([op, "--abort"], data_dir, check=False)
    for d in (".git/rebase-merge", ".git/rebase-apply"):
        shutil.rmtree(data_dir / d, ignore_errors=True)


def _assert_no_lfs(data_dir):
    """The store is plain gzipped blobs today, no Git LFS. If a .gitattributes ever starts routing
    paths through LFS and git-lfs is absent, fail loudly rather than push broken pointer files."""
    ga = data_dir / ".gitattributes"
    if ga.is_file() and "filter=lfs" in ga.read_text() and not shutil.which("git-lfs"):
        raise RuntimeError(f"{DATA_REPO} now uses Git LFS but git-lfs is not installed; install "
                           "it before syncing to avoid committing corrupt pointer files")


def _rescue_unpushed(data_dir, outbox):
    """Before reset-to-origin discards local commits, copy back any file they added that is not
    already in the outbox. Reset is only safe because the outbox is the source of truth; this
    guard upholds that invariant even if a prior run committed locally but never pushed."""
    r = _git(["log", "--name-only", "--diff-filter=A", "--pretty=format:",
              "origin/main..HEAD"], data_dir, check=False)
    rescued = 0
    for rel in {l.strip() for l in r.stdout.splitlines() if l.strip()}:
        src, dst = data_dir / rel, outbox / rel
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            rescued += 1
    if rescued:
        print(f"sync: rescued {rescued} unpushed file(s) into the outbox before reset",
              file=sys.stderr)


def _preserve_collision(dst, src):
    """A record path already exists upstream with different content. Keep upstream's; preserve the
    outbox newcomer under a disc'd sibling with its primary key rewritten to match, so neither is
    lost and build_db keeps both rows. Unique-at-source ids make this a legacy-only backstop."""
    body = src.read_text()
    disc = hashlib.sha256(body.encode()).hexdigest()[:12]
    try:
        rec = json.loads(body)
    except Exception:
        rec = None
    if isinstance(rec, dict):
        for k in ("run_id", "round_id"):
            if rec.get(k):
                rec[k] = f"{rec[k]}-{disc}"
        body = json.dumps(rec, indent=1, sort_keys=True) + "\n"
    sib = dst.with_name(f"{dst.stem}-{disc}{dst.suffix}")
    if not sib.exists():
        sib.write_text(body)
        print(f"sync: collision at {dst.name}; preserved outbox copy as {sib.name}",
              file=sys.stderr)


def _drain(outbox, srcs):
    for src in srcs:
        if src.exists():
            src.unlink()
    for d in sorted({s.parent for s in srcs}, reverse=True):
        if d != outbox and d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def sync(outbox, data_dir, remote="", retries=5):
    """Drain the outbox into a TauCetiData checkout and push.

    Each attempt rebuilds the working branch on origin/main (fetch -> reset) instead of rebasing
    local commits onto it. The outbox is the source of truth and records are write-if-absent, so
    the push is always a fast-forward and a transient conflict or a crash mid-operation can never
    wedge the checkout. Returns the number of files landed."""
    outbox = pathlib.Path(outbox)
    data_dir = pathlib.Path(data_dir)
    url = remote or f"https://github.com/{DATA_REPO}"
    if not (data_dir / ".git").is_dir():
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "-q", url, str(data_dir)],
                           text=True, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"clone of {DATA_REPO} failed: {r.stderr[-500:]}")
    elif remote:
        _git(["remote", "set-url", "origin", remote], data_dir)

    last = ""
    for _ in range(retries):
        _heal(data_dir)
        _git(["fetch", "-q", "origin", "main"], data_dir)
        _rescue_unpushed(data_dir, outbox)
        _git(["checkout", "-q", "-B", "main", "origin/main"], data_dir)
        _assert_no_lfs(data_dir)
        entries = [p for p in sorted(outbox.rglob("*")) if p.is_file()]
        copied = []
        for src in entries:
            rel = src.relative_to(outbox)
            dst = data_dir / rel
            if dst.exists():
                if dst.suffix == ".json" and dst.read_bytes() != src.read_bytes():
                    _preserve_collision(dst, src)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            copied.append(src)
        _git(["add", "-A"], data_dir)
        if not _git(["status", "--porcelain"], data_dir).stdout.strip():
            _drain(outbox, copied)  # everything already upstream
            return 0
        _git(["-c", "user.name=tauceti-archive", "-c",
              "user.email=tauceti-archive@users.noreply.github.com",
              "commit", "-q", "-m", f"archive: {len(copied)} file(s) from outbox"], data_dir)
        push = _git(["push", "-q", "origin", "HEAD:main"], data_dir, check=False)
        if push.returncode == 0:
            _drain(outbox, copied)
            return len(copied)
        last = push.stderr.strip()[-300:]
    raise RuntimeError(f"push to {DATA_REPO} failed after {retries} attempts; outbox kept for a "
                       f"later sync. last push error: {last or '(none)'}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="drain <store>/outbox into a TauCetiData checkout and push")
    s.add_argument("--store", required=True)
    s.add_argument("--data-dir", required=True)
    s.add_argument("--remote", default="",
                   help="override the origin URL (e.g. a tokened https URL in CI)")
    a = ap.parse_args()
    outbox = pathlib.Path(a.store) / "outbox"
    if not outbox.is_dir():
        print("archive: no outbox; nothing to sync")
        return
    n = sync(outbox, a.data_dir, a.remote)
    print(f"archive: synced {n} file(s) to {DATA_REPO}")


if __name__ == "__main__":
    main()
