"""tauceti-review ledger — the on-disk review state (ledger.json).

Run as a script (runner/ on sys.path), so imports are flat siblings, not package-relative.

The ledger is one JSON file: {"days": {date: usd_spent}, "prs": {pr: {"rounds": [...],
"state": {rubric: case_file}, "scoreboard_comment_id": id}}}. This wraps that dict with the
load / per-day spend / persist operations; the format on disk is unchanged (json.dumps indent=2),
and `.data` is the same dict the rest of the runner reads and mutates directly.
"""
import json
import os
import pathlib
import tempfile


def atomic_write_json(path, value):
    """Publish complete JSON; a failed or killed writer leaves the old file intact."""
    path = pathlib.Path(path)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = None
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Ledger:
    def __init__(self, path):
        self.path = path
        self.data = (json.loads(path.read_text()) if path.exists()
                     else {"days": {}, "prs": {}})

    def spent(self, day):
        """USD already spent today (across all PRs), the base for the daily-budget reservation."""
        return self.data["days"].get(day, 0.0)

    def set_spent(self, day, amount):
        self.data["days"][day] = round(amount, 6)

    def pr_state(self, pr):
        """The per-PR record, with its sub-structures defaulted (rounds / case files / scoreboard id)."""
        ps = self.data["prs"].setdefault(str(pr), {})
        ps.setdefault("rounds", [])
        ps.setdefault("state", {})            # per-rubric case files (= scoreboard/staleness)
        ps.setdefault("scoreboard_comment_id", None)
        return ps

    def persist(self):
        atomic_write_json(self.path, self.data)
