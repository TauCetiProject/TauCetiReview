"""Durable public-safe attempt receipts, written before provider history is reset.

Receipts separate billed work from session lifetime. Replaying one after a crash
does not call a model or add its spend twice. They contain runner-validated verdicts
and sanitized results, never the private session ID or credential-bearing errors.
"""
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import uuid

if __package__:
    from .ledger import atomic_write_json
    from .pricing import sum_usage
else:
    from ledger import atomic_write_json
    from pricing import sum_usage


class Attempts:
    def __init__(self, outdir, rubric, fingerprint, ledger, day, dry_run=False, head_sha=None):
        self.directory = pathlib.Path(outdir) / ("dry-run-attempts" if dry_run else "attempts")
        self.rubric, self.fingerprint = rubric, fingerprint
        self.ledger, self.day, self.dry_run = ledger, day, dry_run
        self.head_sha = head_sha
        self.saved, self.position, self.used = [], 0, {}
        # An invocation can advance its round before an earlier paid case file was
        # saved. Search this PR, never another PR or the dry-run namespace.
        roots = sorted(self.directory.parent.parent.glob("*/" + self.directory.name),
                       key=lambda p: (int(p.parent.name) if p.parent.name.isdigit() else -1, str(p)))
        if self.directory not in roots:
            roots.append(self.directory)
        completed = set(ledger.data.get("completed_review_attempts", []))
        for root in roots:
            for path in sorted(root.glob("*.json")):
                receipt = json.loads(path.read_text())
                if (receipt.get("rubric") == rubric and receipt.get("fingerprint") == fingerprint
                        and receipt["id"] not in completed):
                    if receipt.get("phase", "rubric") == "rubric":
                        self.saved.append(receipt)
                    else:
                        self.used[receipt["id"]] = receipt

    def replay(self, provider, model):
        if self.position >= len(self.saved):
            return None
        receipt = self.saved[self.position]
        if receipt["provider"] != provider or receipt["model"] != model:
            # A changed dispatch configuration cannot replay an old model's result.
            self.saved = self.saved[:self.position]
            return None
        self.position += 1
        self.used[receipt["id"]] = receipt
        self.settle(receipt)
        return receipt

    def record(self, provider, model, result, verdict, prompt_sha256, kind, model_unavailable,
               *, phase="rubric", settlement_cost=None, settlement_usage=None):
        self.directory.mkdir(parents=True, exist_ok=True)
        receipt = {"schema": "tauceti.review-attempt/v1", "id": uuid.uuid4().hex,
                   "rubric": self.rubric, "fingerprint": self.fingerprint,
                   "provider": provider, "model": model, "day": self.day,
                   "phase": phase,
                   "head_sha": self.head_sha,
                   "settlement_cost_usd": (result.get("cost_usd") or 0) if settlement_cost is None else settlement_cost,
                   "settlement_usage": result.get("usage") if settlement_usage is None else settlement_usage,
                   "result": result, "verdict": verdict, "prompt_sha256": prompt_sha256,
                   "error_kind": kind, "model_unavailable": model_unavailable}
        path = self.directory / (f"{self.rubric}-{len(self.saved):04d}-" + receipt["id"] + ".json")
        atomic_write_json(path, receipt)
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if phase == "rubric":
            self.saved.append(receipt)
            self.position += 1
        self.used[receipt["id"]] = receipt
        self.settle(receipt)
        return receipt

    def settle(self, receipt):
        accounted = self.ledger.data.setdefault("review_attempts", {})
        if receipt["id"] not in accounted:
            day = receipt["day"]
            cost = receipt.get("settlement_cost_usd", receipt["result"].get("cost_usd") or 0)
            self.ledger.set_spent(day, self.ledger.spent(day) + cost)
            accounted[receipt["id"]] = {"day": day, "cost_usd": cost,
                                         **({"usage_incomplete": True} if receipt["result"].get("usage_incomplete") else {})}
            if not self.dry_run:
                self.ledger.persist()

    def complete(self):
        completed = self.ledger.data.setdefault("completed_review_attempts", [])
        for receipt in self.used.values():
            if receipt["id"] not in completed:
                completed.append(receipt["id"])

    @staticmethod
    def round_totals(outdir, head_sha, run_results, recovered=(), reported=(), dry_run=False):
        # Receipt IDs, rather than provider names, prevent a recovered rubric
        # from disappearing beside new work by the same provider or being billed
        # twice in successive round summaries.
        receipts = {r["id"]: r for r in recovered}
        directory = pathlib.Path(outdir) / ("dry-run-attempts" if dry_run else "attempts")
        for path in directory.glob("*.json"):
            receipt = json.loads(path.read_text())
            if receipt.get("head_sha") == head_sha:
                receipts[receipt["id"]] = receipt
        covered = {rid for result in run_results for rid in result.get("attempt_receipt_ids", [])}
        reported = set(reported)
        selected = [r for rid, r in receipts.items() if rid not in reported]
        usage_records = [{"provider": r["provider"], "usage": r.get("settlement_usage")} for r in selected]
        # Old callers/results without receipt IDs retain their ordinary totals.
        uncached = [r for r in run_results if not r.get("attempt_receipt_ids")]
        if not covered:  # compatibility for direct Attempts users predating result linkage
            providers = {r["provider"] for r in receipts.values()}
            uncached = [r for r in uncached if r.get("provider") not in providers]
        usage_records.extend(uncached)
        return {"cost_usd": round(sum(r.get("settlement_cost_usd", 0) for r in selected)
                                  + sum(r.get("cost_usd") or 0 for r in uncached), 6),
                "tokens": sum_usage(usage_records),
                "cost_incomplete": any(r["result"].get("usage_incomplete") for r in selected)
                                   or any(r.get("usage_incomplete") for r in uncached),
                "attempt_receipt_ids": sorted(receipts)}

    @classmethod
    def reconcile(cls, store, ledger, dry_run=False):
        """Recover completed receipt writes that preceded a killed ledger update."""
        roots = pathlib.Path(store) / "reviews"
        if not roots.exists():
            return
        accounted = ledger.data.get("review_attempts", {})
        for path in roots.glob("*/*/attempts/*.json"):
            if path.stem.rsplit("-", 1)[-1] in accounted:
                continue
            receipt = json.loads(path.read_text())
            if receipt.get("schema") != "tauceti.review-attempt/v1":
                continue
            holder = cls.__new__(cls)
            holder.ledger, holder.dry_run = ledger, dry_run
            holder.settle(receipt)


def fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def source_identity(path):
    """Identity of visible source, independent of its temporary containing directory.

    Git trees use immutable object IDs plus actual dirty/untracked bytes. Exported
    trees have no object database, so hash them once per invocation, not per rubric.
    """
    root = pathlib.Path(path).absolute()
    if root.is_symlink():
        raise ValueError("reviewer source root is a symlink")
    try:
        root.lstat()
    except FileNotFoundError:
        return ["missing"]
    root = root.resolve()

    def inside(item):
        # Resolve one component at a time. Path.resolve() would already follow
        # an intermediate link outside the boundary before we could reject it.
        pending = list(item.relative_to(root).parts)
        current, links = root, 0
        while pending:
            candidate = current / pending.pop(0)
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raw_target = pathlib.Path(os.readlink(candidate))
                if ".git" in raw_target.parts:
                    raise ValueError("reviewer source link enters excluded metadata")
                target = pathlib.Path(os.path.abspath(candidate.parent / raw_target))
                if not target.is_relative_to(root):
                    raise ValueError("reviewer source link leaves its root")
                links += 1
                if links > 40:
                    raise ValueError("cyclic reviewer source link")
                pending = list(target.relative_to(root).parts) + pending
                current = root
            else:
                current = candidate
        return current

    def content(item):
        if item != root:
            item = inside(item.parent) / item.name
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            resolved = inside(item)
            relative = resolved.relative_to(root)
            if ".git" in relative.parts:
                raise ValueError("reviewer source link enters excluded metadata")
            # Target bytes are already covered by the ordinary tree inventory.
            # Expanding targets here turns acyclic link fan-out exponential.
            return ["link", os.readlink(item), str(relative)]
        if item.is_dir():
            return ["tree", [(p.name, content(p)) for p in sorted(item.iterdir()) if p.name != ".git"]]
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("unsupported reviewer source entry")
        digest = hashlib.sha256()
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return ["file", bool(info.st_mode & 0o111), digest.hexdigest()]

    if root.is_dir() and (root / ".git").exists():
        def git(*arguments):
            result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(root), *arguments], capture_output=True,
                                    timeout=30, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
            if result.returncode:
                raise ValueError("reviewer source Git identity unavailable")
            return result.stdout
        head = git("rev-parse", "HEAD^{tree}").decode().strip()
        dirty = hashlib.sha256(git("diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--", ".")).hexdigest()
        # Include ignored files too: the reviewer can read them. No timestamps or
        # workspace path enters the key, and symlink targets are measured as read.
        others = git("ls-files", "--others", "-z").split(b"\0")
        extra = [(os.fsdecode(name), content(root / os.fsdecode(name))) for name in others if name]
        links, submodules, flagged = [], [], []
        for entry in git("ls-files", "--stage", "-z").split(b"\0"):
            if entry.startswith((b"120000 ", b"160000 ")):
                name = os.fsdecode(entry.split(b"\t", 1)[1])
                path = root / name
                safe = inside(path.parent) / path.name
                if entry.startswith(b"160000 "):
                    submodules.append((name, source_identity(safe)))
                elif safe.is_symlink():
                    links.append((name, content(safe)))
        # Git's stat cache deliberately ignores assume-unchanged / skip-worktree
        # paths. Their actual readable bytes must still invalidate recovery.
        for entry in git("ls-files", "-v", "-z").split(b"\0"):
            if entry and (entry[:1].islower() or entry[:1].upper() == b"S"):
                name = os.fsdecode(entry[2:])
                try:
                    value = content(root / name)
                except FileNotFoundError:
                    value = ["missing"]
                flagged.append((name, value))
        return ["git", head, dirty, extra, links, submodules, flagged]
    return content(root)


def _context_identity(a):
    workspace = pathlib.Path(a.tool_cwd).resolve()
    sources = []
    for name in ("code_path", "roadmap_path", "mathlib_path", "lean_src"):
        value = getattr(a, name, "")
        if value:
            path = pathlib.Path(value)
            path = path if path.is_absolute() else workspace / path
            sources.append((name, source_identity(path)))
    if not any(value != ["missing"] for _, value in sources):
        sources.append(("workspace", source_identity(workspace)))
    artifacts = []
    for name in ("diff_file", "pr_desc_file", "paths_file", "reply_file"):
        value = getattr(a, name, "")
        artifacts.append((name, source_identity(value) if value else ["missing"]))
    return fingerprint("review-context-v2", sources, artifacts,
                       getattr(a, "base_sha", ""), getattr(a, "merge_base_sha", ""))


def context_identity(a):
    """Unavailable source identity disables receipt reuse, never ordinary review.

    A fresh nonce is essential: reusing a constant failure key would certify
    changed content we could not inspect. PR-controlled links are never scanned
    beyond their declared source root.
    """
    try:
        return _context_identity(a)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, UnicodeError):
        return "nonreusable:" + uuid.uuid4().hex
