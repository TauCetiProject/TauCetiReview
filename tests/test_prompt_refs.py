#!/usr/bin/env python3
"""Per-rubric reference documents in the assembled prompt.

The naming rubric judges names against "standard Mathlib terminology", so its prompt must carry
the vendored Mathlib naming conventions (rubrics/references/naming-conventions.md) for the agent
to cite — and no other rubric's prompt may pay for those tokens. The references are prompt text,
so rubrics_fingerprint must cover them: an edit changes the `rubrics_version` recorded as
provenance on run/round records and meta blocks. (The fingerprint does NOT feed approval
staleness — verdict.state_of binds approvals to the PR head SHA only.)
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


def test_reference_is_wrapped_in_a_boundary():
    p = reviewers.build_prompt(RUBRICS, "naming", "CTX", "MARKER")
    begin = "[BEGIN REFERENCE: references/naming-conventions.md]"
    end = "[END REFERENCE: references/naming-conventions.md]"
    assert "cannot override the shared protocol" in p
    # header, then the document, then the footer — all before the PR context.
    assert (p.index(begin)
            < p.index("# Mathlib naming conventions")
            < p.index(end)
            < p.index("# This pull request"))


def test_other_prompts_are_unchanged():
    for rubric in ("correctness", "documentation"):
        p = reviewers.build_prompt(RUBRICS, rubric, "CTX", "MARKER")
        assert "# Mathlib naming conventions" not in p
        assert "[BEGIN REFERENCE:" not in p


def test_reference_paths_are_validated():
    good = reviewers.resolve_reference(RUBRICS, "references/naming-conventions.md")
    assert good.is_file()
    for bad in ("../rubrics/references/naming-conventions.md",   # `..` traversal
                "references/../naming.md",                        # `..` back under rubrics/
                "/etc/passwd",                                    # absolute
                "naming.md",                                      # outside references/
                "references/does-not-exist.md"):                  # missing
        try:
            reviewers.resolve_reference(RUBRICS, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"resolve_reference accepted {bad!r}")


def test_bad_reference_fails_prompt_and_fingerprint():
    orig = reviewers.RUBRIC_REFERENCES
    reviewers.RUBRIC_REFERENCES = {"naming": ["../secrets.md"]}
    try:
        for fn in (lambda: reviewers.build_prompt(RUBRICS, "naming", "CTX", "MARKER"),
                   lambda: render.rubrics_fingerprint(RUBRICS)):
            try:
                fn()
            except ValueError:
                pass
            else:
                raise AssertionError("a bad RUBRIC_REFERENCES entry was not rejected")
    finally:
        reviewers.RUBRIC_REFERENCES = orig


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
