"""Large reviews must expose the omitted patch inside the read-only workspace."""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
from diff_context import description_prompt, diff_prompt


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


class DescriptionContext(unittest.TestCase):
    def test_short_description_keeps_its_existing_prompt(self):
        self.assertEqual(description_prompt("  # Title\n\nbody\n", "unused", "."),
                         ("# Title\n\nbody", ""))

    def test_cut_description_names_the_complete_file_in_the_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            full = "# Title\n" + "Quality pass line\n" * 100
            (root / "pr_desc.txt").write_text(full)
            excerpt, notice = description_prompt(full, root / "pr_desc.txt", root, limit=8)
            self.assertEqual(excerpt, "# Title")
            self.assertIn(f"Only the first 8 of its {len(full)} characters are shown below.", notice)
            self.assertIn('available to your read-only tools at "./pr_desc.txt"', notice)
            self.assertIn("before reporting anything in the description as missing", notice)

    def test_ci_staged_copy_is_named_only_when_it_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            original = root / "pr_desc.txt"
            original.write_text("complete description")
            (workspace / "pr_desc.txt").write_text(original.read_text())
            _, notice = description_prompt(original.read_text(), original, workspace, limit=1)
            self.assertIn('"./pr_desc.txt"', notice)
            (workspace / "pr_desc.txt").write_text("another description")
            _, notice = description_prompt(original.read_text(), original, workspace, limit=1)
            self.assertNotIn("pr_desc.txt", notice)

    def test_cut_description_outside_the_workspace_still_warns_of_the_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            original = root / "pr_desc.txt"
            original.write_text("complete description")
            excerpt, notice = description_prompt(original.read_text(), original, workspace, limit=8)
            self.assertEqual(excerpt, "complete")
            self.assertIn("Only the first 8 of its 20 characters are shown below.", notice)
            self.assertIn("do not report anything in the description as missing because of the cut",
                          notice)


if __name__ == "__main__":
    unittest.main()
