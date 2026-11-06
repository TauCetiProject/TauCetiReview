#!/usr/bin/env python3
"""Failed and killed ledger writers must preserve the previous review state."""
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

RUNNER = pathlib.Path(__file__).resolve().parents[1] / "runner"
sys.path.insert(0, str(RUNNER))
import ledger
import post


class AtomicLedger(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.path = self.root / "ledger.json"
        self.before = {"days": {"2026-11-06": 1.25}, "prs": {"42": {"state": {"scope": "keep"}}}}
        self.path.write_text(json.dumps(self.before))
        self.path.chmod(0o640)
        self.mode = self.path.stat().st_mode & 0o777
        self.original = self.path.read_bytes()
        self.saved = ledger.Ledger(self.path)
        self.saved.set_spent("2026-11-06", 2.5)

    def test_readers_see_old_version_until_complete_replacement(self):
        replace = ledger.os.replace

        def publish(source, target):
            self.assertEqual(self.path.read_bytes(), self.original)
            self.assertEqual(json.loads(pathlib.Path(source).read_text()), self.saved.data)
            self.assertEqual(pathlib.Path(source).parent, self.path.parent)
            replace(source, target)

        for writer in (self.saved.persist, lambda: post.atomic_write_json(self.path, self.saved.data)):
            self.path.write_bytes(self.original)
            with patch.object(ledger.os, "replace", side_effect=publish):
                writer()
            self.assertEqual(ledger.Ledger(self.path).data, self.saved.data)
            self.assertEqual(self.path.stat().st_mode & 0o777, self.mode)
            self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_first_write_publishes_complete_ledger(self):
        self.path.unlink()
        fresh = ledger.Ledger(self.path)
        fresh.persist()
        self.assertEqual(ledger.Ledger(self.path).data, {"days": {}, "prs": {}})
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_failures_preserve_old_ledger_and_clean_temporary_files(self):
        def partial_write(value, stream, **kwargs):
            stream.write('{"days":')
            stream.flush()
            raise OSError("disk full after partial write")

        for target, failure in (("json.dump", partial_write),
                                ("os.fsync", OSError("flush failed")),
                                ("os.replace", OSError("replace failed"))):
            with self.subTest(target=target), patch("ledger." + target, side_effect=failure):
                with self.assertRaises(OSError):
                    self.saved.persist()
            self.assertEqual(self.path.read_bytes(), self.original)
            self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_killed_writer_preserves_old_ledger_and_next_write_succeeds(self):
        ready = self.root / "ready"
        code = '''
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import ledger
def interrupted(value, stream, **kwargs):
    stream.write('{"days":')
    stream.flush()
    pathlib.Path(sys.argv[3]).touch()
    time.sleep(60)
ledger.json.dump = interrupted
ledger.Ledger(pathlib.Path(sys.argv[2])).persist()
'''
        child = subprocess.Popen([sys.executable, "-c", code, str(RUNNER), str(self.path), str(ready)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "writer did not reach its partial write")
            child.kill()
            child.communicate(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(ledger.Ledger(self.path).data, self.before)
        self.saved.persist()
        self.assertEqual(ledger.Ledger(self.path).data, self.saved.data)


if __name__ == "__main__":
    unittest.main()
