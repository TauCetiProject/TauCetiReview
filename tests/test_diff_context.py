"""Large reviews must expose the omitted patch inside the read-only workspace."""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
from diff_context import diff_prompt


class CompleteDiffContext(unittest.TestCase):
    def test_small_diff_keeps_its_existing_prompt(self):
        self.assertEqual(diff_prompt("small patch", "unused", "."), ("small patch", ""))

    def test_local_large_patch_remains_readable_beyond_the_excerpt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            patch = root / "diff.txt"
            full = "prefix\n" + "omitted change\n" * 100
            patch.write_text(full)
            excerpt, notice = diff_prompt(full, patch, root, limit=7)
            self.assertEqual(excerpt, "prefix\n")
            self.assertIn('"./diff.txt"', notice)
            self.assertIn(f"7 of {len(full)} characters", notice)
            self.assertIn("approval based only on the inline prefix is incomplete", notice)
            self.assertEqual((root / "diff.txt").read_text(), full)

    def test_ci_staged_copy_must_match_the_original_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            original = root / "diff.txt"
            original.write_text("complete patch")
            staged = workspace / "diff.txt"
            staged.write_text(original.read_text())
            _, notice = diff_prompt(original.read_text(), original, workspace, limit=1)
            self.assertIn('"./diff.txt"', notice)
            staged.write_text("different patch")
            with self.assertRaisesRegex(ValueError, "no matching complete artifact"):
                diff_prompt(original.read_text(), original, workspace, limit=1)

    def test_outside_artifact_and_escaping_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            original = root / "diff.txt"
            original.write_text("complete patch")
            for symlink in (False, True):
                if symlink:
                    (workspace / "diff.txt").symlink_to(original)
                with self.assertRaisesRegex(ValueError, "no matching complete artifact"):
                    diff_prompt(original.read_text(), original, workspace, limit=1)


if __name__ == "__main__":
    unittest.main()
