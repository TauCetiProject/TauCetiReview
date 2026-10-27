"""Diagnose Codex's actual read-only sandbox without inference, credentials, or GitHub."""
import argparse
import json
import pathlib
import platform
import re
import secrets
import shutil
import subprocess
import tempfile

if __package__:
    from .reviewers import cleanup_rev_home, reviewer_env
else:
    from reviewers import cleanup_rev_home, reviewer_env


PROBE = '''
IFS= read -r actual < "$1" || exit 40
test "$actual" = "$2" || exit 41
if (printf forbidden > "$3") 2>/dev/null; then
    printf 'ERROR: sandbox allowed a workspace write\n' >&2
    exit 42
fi
printf 'TAUCETI_SANDBOX_READ_OK_WRITE_DENIED\n'
'''


def sandbox_command(help_text, system):
    """Older CLIs require a platform subcommand; current CLIs select the native sandbox."""
    if system not in ("Linux", "Darwin"):
        raise ValueError("this diagnostic supports Linux and macOS")
    subcommand = "linux" if system == "Linux" else "macos"
    legacy = bool(re.search(rf"^\s+{subcommand}\s+", help_text, re.M))
    return ["codex", "sandbox", *([subcommand] if legacy else []),
            "-c", 'sandbox_mode="read-only"',
            "-c", 'shell_environment_policy.inherit="none"', "--"]


def kernel_facts():
    facts = {}
    for name in ("unprivileged_userns_clone", "apparmor_restrict_unprivileged_userns"):
        try:
            facts[name] = (pathlib.Path("/proc/sys/kernel") / name).read_text().strip()
        except OSError:
            pass
    try:
        for line in pathlib.Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("Seccomp:", "NoNewPrivs:")):
                key, value = line.split(":", 1)
                facts[key] = value.strip()
    except OSError:
        pass
    return facts


def diagnose(cwd):
    report = {"platform": f"{platform.system()} {platform.release()} {platform.machine()}",
              "codex": shutil.which("codex"),
              "kernel": kernel_facts(), "ok": False}
    # Match the reviewer's clean environment but seed NO credential. No model is called.
    env, home = reviewer_env("codex", {"openai": ""})
    try:
        version = subprocess.run(["codex", "--version"], env=env, cwd=cwd,
                                 capture_output=True, text=True, timeout=20)
        report["version"] = version.stdout.strip()
        help_result = subprocess.run(["codex", "sandbox", "--help"], env=env, cwd=cwd,
                                     capture_output=True, text=True, timeout=20)
        if help_result.returncode:
            report.update(stage="sandbox_help", returncode=help_result.returncode,
                          stderr=help_result.stderr, stdout=help_result.stdout)
            return report
        command = sandbox_command(help_result.stdout, platform.system())
        # Use a host-owned scratch directory inside the selected workspace. The sandbox must
        # read its known input and refuse a write there. Cleanup stays on the host.
        with tempfile.TemporaryDirectory(prefix=".tauceti-sandbox-probe-", dir=cwd) as tmp:
            source, target = pathlib.Path(tmp) / "input", pathlib.Path(tmp) / "write-test"
            nonce = secrets.token_hex(16)
            source.write_text(nonce + "\n")
            command += ["/bin/sh", "-c", PROBE, "sandbox-probe", str(source), nonce, str(target)]
            result = subprocess.run(command, env=env, cwd=cwd, capture_output=True,
                                    text=True, timeout=20)
            report.update(stage="sandbox_probe", returncode=result.returncode,
                          stdout=result.stdout, stderr=result.stderr)
            report["ok"] = (result.returncode == 0 and not target.exists()
                            and "TAUCETI_SANDBOX_READ_OK_WRITE_DENIED" in result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report.update(stage="launcher", error=str(error))
    finally:
        cleanup_rev_home(home)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=pathlib.Path, default=pathlib.Path.cwd(),
                        help="review workspace to inspect (default: current directory)")
    args = parser.parse_args()
    report = diagnose(args.cwd.resolve())
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
