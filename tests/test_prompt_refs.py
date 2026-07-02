#!/usr/bin/env python3
"""Per-rubric reference documents in the assembled prompt.

The naming rubric judges names against "standard Mathlib terminology", so its prompt must carry
the vendored Mathlib naming conventions (rubrics/references/naming-conventions.md) for the agent
to cite — and no other rubric's prompt may pay for those tokens. The references are prompt text,
so rubrics_fingerprint must cover them (an edit invalidates carried-forward approvals).
Dependency-free — run with `python tests/test_prompt_refs.py` or under pytest.
"""
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import render  # noqa: E402
import reviewers  # noqa: E402

RUBRICS = pathlib.Path(__file__).resolve().parent.parent / "rubrics"


def test_listed_references_exist():
    for rubric, paths in reviewers.RUBRIC_REFERENCES.items():
        assert (RUBRICS / f"{rubric}.md").is_file(), rubric
        for rel in paths:
            assert (RUBRICS / rel).is_file(), rel


def test_naming_prompt_carries_the_conventions():
    p = reviewers.build_prompt(RUBRICS, "naming", "CTX", "MARKER")
    assert "# Mathlib naming conventions" in p
    assert "TauCeti addendum" in p
    # Assembly order: shared protocol, angle, reference document, then the PR context.
    assert (p.index("# Review agents: shared protocol")
            < p.index("# Naming and notation")
            < p.index("# Mathlib naming conventions")
            < p.index("# This pull request"))


def test_other_prompts_are_unchanged():
    for rubric in ("correctness", "documentation"):
        p = reviewers.build_prompt(RUBRICS, rubric, "CTX", "MARKER")
        assert "# Mathlib naming conventions" not in p


def test_fingerprint_covers_references():
    with tempfile.TemporaryDirectory() as td:
        shutil.copytree(RUBRICS, td, dirs_exist_ok=True)
        before = render.rubrics_fingerprint(td)
        ref = pathlib.Path(td) / "references" / "naming-conventions.md"
        ref.write_text(ref.read_text() + "\nedited\n")
        assert render.rubrics_fingerprint(td) != before


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
