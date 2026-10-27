"""The diagnostic tests read-only enforcement without credentials, inference, or GitHub."""
import json
import pathlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from runner import sandbox_check as check


def test_version_compatible_commands():
    assert check.sandbox_command("Usage: codex sandbox [COMMAND]...", "Linux")[:2] == ["codex", "sandbox"]
    assert check.sandbox_command("Commands:\n  linux  Run under Landlock\n", "Linux")[:3] == ["codex", "sandbox", "linux"]
    assert check.sandbox_command("Commands:\n  macos  Run under Seatbelt\n", "Darwin")[:3] == ["codex", "sandbox", "macos"]


def test_probe_rejects_an_unsandboxed_shell():
    with tempfile.TemporaryDirectory() as tmp:
        source, target = pathlib.Path(tmp) / "input", pathlib.Path(tmp) / "target"
        source.write_text("nonce\n")
        result = subprocess.run(["/bin/sh", "-c", check.PROBE, "probe", str(source), "nonce", str(target)],
                                capture_output=True, text=True)
        assert result.returncode == 42, "a writable environment must never be reported healthy"


def test_diagnostic_preserves_the_real_error_and_cleans_up():
    for scenario in ("healthy", "namespace_denied", "launcher_missing"):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            assert kwargs["env"] == {"clean": "yes"}
            assert "exec" not in command and "login" not in command
            if scenario == "launcher_missing":
                raise FileNotFoundError("missing codex launcher")
            if command[-1] == "--version":
                return SimpleNamespace(returncode=0, stdout="codex-cli 0.163.0", stderr="")
            if command[-1] == "--help":
                return SimpleNamespace(returncode=0, stdout="Usage: codex sandbox [COMMAND]...", stderr="")
            assert pathlib.Path(command[-3]).read_text().strip() == command[-2]
            return SimpleNamespace(returncode=0 if scenario == "healthy" else 1,
                                   stdout="TAUCETI_SANDBOX_READ_OK_WRITE_DENIED\n" if scenario == "healthy" else "",
                                   stderr="" if scenario == "healthy" else "bwrap: Creating new namespace failed: Operation not permitted")

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(check, "reviewer_env", return_value=({"clean": "yes"}, "scratch")) as env, \
             patch.object(check, "cleanup_rev_home") as cleanup, \
             patch.object(check.subprocess, "run", side_effect=run):
            report = check.diagnose(pathlib.Path(tmp))
            env.assert_called_once_with("codex", {"openai": ""})
            cleanup.assert_called_once_with("scratch")
            assert not list(pathlib.Path(tmp).iterdir())
            assert report["ok"] == (scenario == "healthy")
            if scenario == "namespace_denied":
                assert "Operation not permitted" in report["stderr"]
            if scenario == "launcher_missing":
                assert "missing codex launcher" in report["error"]
            assert "scratch" not in json.dumps(report)


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS: {name}")
