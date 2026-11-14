"""Durable public-safe attempt receipts, written before provider history is reset.

Receipts separate billed work from session lifetime. Replaying one after a crash
does not call a model or add its spend twice. They contain runner-validated verdicts
and sanitized results, never the private session ID or credential-bearing errors.
"""
import hashlib
import json
import os
import pathlib
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
        self.saved, self.position = [], 0
        if self.directory.exists():
            for path in sorted(self.directory.glob("*.json")):
                receipt = json.loads(path.read_text())
                if (receipt.get("rubric") == rubric and receipt.get("fingerprint") == fingerprint
                        and receipt.get("phase", "rubric") == "rubric"
                        and receipt["id"] not in ledger.data.get("completed_review_attempts", [])):
                    self.saved.append(receipt)

    def replay(self, provider, model):
        if self.position >= len(self.saved):
            return None
        receipt = self.saved[self.position]
        if receipt["provider"] != provider or receipt["model"] != model:
            # A changed dispatch configuration cannot replay an old model's result.
            self.saved = self.saved[:self.position]
            return None
        self.position += 1
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
        for receipt in self.saved[:self.position]:
            if receipt["id"] not in completed:
                completed.append(receipt["id"])

    @staticmethod
    def round_totals(outdir, head_sha, run_results):
        directory = pathlib.Path(outdir) / "attempts"
        cost = 0
        cached = set()
        usage_records = []
        incomplete = False
        for path in directory.glob("*.json"):
            receipt = json.loads(path.read_text())
            if receipt.get("head_sha") == head_sha:
                cost += receipt.get("settlement_cost_usd", 0)
                incomplete = incomplete or bool(receipt["result"].get("usage_incomplete"))
                cached.add(receipt["provider"])
                usage_records.append({"provider": receipt["provider"], "usage": receipt.get("settlement_usage")})
        cost += sum(result.get("cost_usd") or 0 for result in run_results
                    if result.get("provider") not in cached)
        usage_records.extend(result for result in run_results if result.get("provider") not in cached)
        return {"cost_usd": round(cost, 6), "tokens": sum_usage(usage_records),
                "cost_incomplete": incomplete or any(r.get("usage_incomplete") for r in run_results)}

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
