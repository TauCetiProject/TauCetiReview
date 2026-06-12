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


def write_record(outbox, rel, record):
    """Write-if-absent; an existing file with different content is an id collision (refuse)."""
    path = pathlib.Path(outbox) / rel
    body = json.dumps(record, indent=1, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text() != body:
            raise RuntimeError(f"archive id collision at {rel}: refusing to overwrite")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def archive_run(outbox, record, transcript_text=None, diff_text=None):
    if transcript_text:
        record["transcript_blob"] = write_blob(outbox, transcript_text)
    if diff_text:
        record["diff_blob"] = write_blob(outbox, diff_text)
    return write_record(outbox, f"records/runs/{record['pr']}/{record['run_id']}.json", record)


def archive_round(outbox, record):
    return write_record(outbox, f"records/rounds/{record['pr']}/{record['round_id']}.json", record)


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


def sync(outbox, data_dir, remote="", retries=3):
    """Drain the outbox into a TauCetiData checkout and push. Idempotent and safe to re-run:
    files are copied write-if-absent, and an outbox entry is deleted only after the push that
    contains it succeeds. Returns the number of files landed."""
    outbox = pathlib.Path(outbox)
    data_dir = pathlib.Path(data_dir)
    entries = [p for p in sorted(outbox.rglob("*")) if p.is_file()]
    if not entries:
        return 0
    url = remote or f"https://github.com/{DATA_REPO}"
    if not (data_dir / ".git").is_dir():
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "-q", url, str(data_dir)],
                           text=True, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"clone of {DATA_REPO} failed: {r.stderr[-500:]}")
    elif remote:
        _git(["remote", "set-url", "origin", remote], data_dir)

    copied = []
    for src in entries:
        rel = src.relative_to(outbox)
        dst = data_dir / rel
        if dst.exists():
            if dst.read_bytes() != src.read_bytes() and dst.suffix == ".json":
                raise RuntimeError(f"sync id collision at {rel}: outbox disagrees with data repo")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        copied.append((src, rel))

    _git(["add", "-A"], data_dir)
    if not _git(["status", "--porcelain"], data_dir).stdout.strip():
        for src, _ in copied:  # everything already upstream: just drain
            src.unlink()
        return 0
    _git(["-c", "user.name=tauceti-archive", "-c",
          "user.email=tauceti-archive@users.noreply.github.com",
          "commit", "-q", "-m", f"archive: {len(copied)} file(s) from outbox"], data_dir)
    for i in range(retries):
        _git(["pull", "-q", "--rebase", "origin", "main"], data_dir, check=False)
        if _git(["push", "-q", "origin", "HEAD:main"], data_dir, check=False).returncode == 0:
            for src, _ in copied:
                src.unlink()
            for d in sorted({s.parent for s, _ in copied}, reverse=True):
                if d != outbox and not any(d.iterdir()):
                    d.rmdir()
            return len(copied)
    raise RuntimeError(f"push to {DATA_REPO} failed after {retries} attempts; "
                       "outbox kept for a later sync")


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
