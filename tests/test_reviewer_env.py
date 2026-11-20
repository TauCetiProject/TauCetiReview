#!/usr/bin/env python3
"""The isolated reviewer environment keeps login identity without inheriting personal config."""

import os
import json
import pathlib
import sys
import tempfile
import sqlite3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import reviewers  # noqa: E402


def test_reviewer_env_keeps_public_login_identity():
    old = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory() as home:
            os.environ.clear()
            os.environ.update(
                HOME=home,
                PATH="/usr/bin",
                LANG="C.UTF-8",
                USER="alice",
                LOGNAME="login-alice",
                PERSONAL_SETTING="must-not-leak",
            )
            env, isolated_home = reviewers.reviewer_env("claude", {"anthropic": "test-key"})
    finally:
        os.environ.clear()
        os.environ.update(old)

    assert env["USER"] == "alice"
    assert env["LOGNAME"] == "login-alice"
    assert "PERSONAL_SETTING" not in env
    assert set(env) == {"PATH", "HOME", "LANG", "CI", "USER", "LOGNAME", "ANTHROPIC_API_KEY"}
    reviewers.cleanup_rev_home(isolated_home)


def test_logname_is_a_user_fallback():
    old = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory() as home:
            os.environ.clear()
            os.environ.update(HOME=home, PATH="/usr/bin", LOGNAME="fallback-user")
            env, isolated_home = reviewers.reviewer_env("claude", {"anthropic": "test-key"})
    finally:
        os.environ.clear()
        os.environ.update(old)

    assert env["USER"] == "fallback-user"
    assert env["LOGNAME"] == "fallback-user"
    reviewers.cleanup_rev_home(isolated_home)


def test_codex_uses_private_writable_temporary_storage():
    old = os.environ.copy()
    isolated_home = None
    try:
        os.environ["TMPDIR"] = "/must/not/inherit/host/tmp"
        env, isolated_home = reviewers.reviewer_env("codex", {"openai": "test-key"})
        tmp = pathlib.Path(env["TMPDIR"])
        assert tmp.is_relative_to(pathlib.Path(isolated_home))
        assert tmp.stat().st_mode & 0o777 == 0o700
        (tmp / "writable").write_text("launcher bookkeeping")
        assert env["TMPDIR"] != os.environ["TMPDIR"]
    finally:
        os.environ.clear()
        os.environ.update(old)
        reviewers.cleanup_rev_home(isolated_home)
    assert not tmp.exists()


def test_kiro_subscription_copies_only_login_store():
    old = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory() as real_home:
            data = pathlib.Path(real_home) / ".local" / "share" / "kiro-cli"
            data.mkdir(parents=True)
            db = data / "data.sqlite3"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO auth_kv VALUES ('login', 'test')")
            os.environ.clear()
            os.environ.update(HOME=real_home, PATH="/usr/bin")
            env, isolated_home = reviewers.reviewer_env("kiro", {}, subscription=True)
            copied = pathlib.Path(env["XDG_DATA_HOME"]) / "kiro-cli" / "data.sqlite3"
            assert copied.is_file()
            with sqlite3.connect(copied) as conn:
                assert conn.execute("SELECT value FROM auth_kv WHERE key = 'login'").fetchone() == ("test",)
            assert env["KIRO_HOME"].startswith(isolated_home)
            assert "KIRO_API_KEY" not in env
            assert copied.stat().st_mode & 0o777 == 0o600
    finally:
        os.environ.clear()
        os.environ.update(old)
    reviewers.cleanup_rev_home(isolated_home)


def test_kiro_api_key_cannot_be_shadowed_by_browser_login():
    env, isolated_home = reviewers.reviewer_env(
        "kiro", {"kiro": "  ksk_test  "}, subscription=True
    )
    try:
        assert env["KIRO_API_KEY"] == "ksk_test"
        assert env["XDG_DATA_HOME"].startswith(isolated_home)
        assert not (pathlib.Path(env["XDG_DATA_HOME"]) / "kiro-cli" / "data.sqlite3").exists()
    finally:
        reviewers.cleanup_rev_home(isolated_home)


def test_kiro_macos_uses_native_private_data_path():
    old_env, old_platform = os.environ.copy(), reviewers.sys.platform
    isolated_home = None
    try:
        with tempfile.TemporaryDirectory() as real_home:
            data = pathlib.Path(real_home) / "Library" / "Application Support" / "kiro-cli"
            data.mkdir(parents=True)
            with sqlite3.connect(data / "data.sqlite3") as conn:
                conn.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO auth_kv VALUES ('login', 'mac')")
            os.environ.clear()
            os.environ.update(HOME=real_home, PATH="/usr/bin", XDG_DATA_HOME="/must/not/win")
            reviewers.sys.platform = "darwin"
            env, isolated_home = reviewers.reviewer_env("kiro", {}, subscription=True)
            copied = (
                pathlib.Path(isolated_home)
                / "Library"
                / "Application Support"
                / "kiro-cli"
                / "data.sqlite3"
            )
            assert copied.is_file()
            assert "XDG_DATA_HOME" not in env
            assert env["HOME"] == isolated_home
    finally:
        reviewers.sys.platform = old_platform
        os.environ.clear()
        os.environ.update(old_env)
        reviewers.cleanup_rev_home(isolated_home)



def test_claude_attempts_reuse_one_empty_home():
    # Claude Code puts HOME paths in its system prompt, ahead of the cached rubric prefix, so every
    # attempt of a process must see the same HOME path. Each must still start empty.
    env1, home1 = reviewers.reviewer_env("claude", {"anthropic": "k"})
    try:
        pathlib.Path(home1, "left-by-attempt-1").write_text("x")
        assert oct(os.stat(home1).st_mode & 0o777) == "0o700"
    finally:
        reviewers.cleanup_rev_home(home1)
    env2, home2 = reviewers.reviewer_env("sonnet", {"anthropic": "k"})
    env3, home3 = reviewers.reviewer_env("claude", {"anthropic": "k"})
    try:
        assert home3 == home1 and env3["HOME"] == env1["HOME"]
        assert os.listdir(home3) == []           # recreated, nothing carried over
        assert home2 != home1                     # sonnet keeps its own
        assert reviewers._CLAUDE_HOME_TOKEN in home1 and len(reviewers._CLAUDE_HOME_TOKEN) == 16
    finally:
        reviewers.cleanup_rev_home(home2)
        reviewers.cleanup_rev_home(home3)
    assert not os.path.exists(home1)

def test_claude_subscription_honors_configured_file_and_keeps_usage_login_out():
    old_env, old_base = os.environ.copy(), reviewers.REV_HOME_BASE
    try:
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            operator, source, usage = root / "operator", root / "agent", root / "usage.json"
            (operator / ".claude").mkdir(parents=True)
            source.mkdir()
            (operator / ".claude" / ".credentials.json").write_text("OPERATOR-CREDENTIAL")
            (operator / ".claude" / "settings.json").write_text("OPERATOR-SETTINGS")
            (source / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "AGENT-ONE", "refreshToken": "OWNER-REFRESH", "expiresAt": 9999999999999}, "ownerOnly": "must-not-copy"}))
            (source / "CLAUDE.md").write_text("AGENT-PERSONAL-CONFIG")
            usage.write_text("USAGE-ONLY-CREDENTIAL")
            reviewers.REV_HOME_BASE = str(root / "reviews")
            os.environ.clear()
            os.environ.update(HOME=str(operator), PATH="/usr/bin", USER="test-user", LOGNAME="test-user",
                              CLAUDE_CONFIG_DIR=str(source), TAUCETI_CLAUDE_USAGE_CREDENTIALS=str(usage),
                              GH_TOKEN="GITHUB-SECRET", CLAUDE_CODE_OAUTH_TOKEN="ENVIRONMENT-AGENT")
            for provider in ("claude", "sonnet"):
                env, home = reviewers.reviewer_env(provider, {}, subscription=True)
                try:
                    private = pathlib.Path(env["CLAUDE_CONFIG_DIR"])
                    assert env["HOME"] == home != str(operator)
                    assert private == pathlib.Path(home) / ".claude"
                    assert json.loads((private / ".credentials.json").read_text()) == {"claudeAiOauth": {"accessToken": "AGENT-ONE", "expiresAt": 9999999999999}}
                    assert json.loads((source / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"] == "OWNER-REFRESH"
                    assert (private / ".credentials.json").stat().st_mode & 0o777 == 0o600
                    assert set(p.name for p in private.iterdir()) == {".credentials.json"}
                    assert not any(key in env for key in ("GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "TAUCETI_CLAUDE_USAGE_CREDENTIALS"))
                    # The final subprocess sees the private config, not the parent config or usage login.
                    child = reviewers.subprocess.run(
                        [sys.executable, "-c", "import os,pathlib,json; print(json.loads((pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])/'.credentials.json').read_text())['claudeAiOauth']['accessToken']); print('TAUCETI_CLAUDE_USAGE_CREDENTIALS' in os.environ)"],
                        env=env, capture_output=True, text=True, check=True)
                    assert child.stdout.splitlines() == ["AGENT-ONE", "False"]
                finally:
                    reviewers.cleanup_rev_home(home)
            (source / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "AGENT-TWO", "refreshToken": "OWNER-REFRESH-TWO"}}))
            env, home = reviewers.reviewer_env("claude", {}, subscription=True)
            try:
                assert json.loads((pathlib.Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json").read_text()) == {"claudeAiOauth": {"accessToken": "AGENT-TWO"}}
                assert usage.read_text() == "USAGE-ONLY-CREDENTIAL"
            finally:
                reviewers.cleanup_rev_home(home)
            # Explicit missing source must not use the perfectly readable operator credential.
            (source / ".credentials.json").unlink()
            try:
                reviewers.reviewer_env("claude", {}, subscription=True)
                raise AssertionError("missing explicit file unexpectedly accepted")
            except ValueError as error:
                assert "CLAUDE_CONFIG_DIR" in str(error) and "--auth api" in str(error)
            assert not any(pathlib.Path(reviewers.REV_HOME_BASE).iterdir())
            (source / ".credentials.json").write_text("invalid explicit JSON")
            try:
                reviewers.reviewer_env("claude", {}, subscription=True)
                raise AssertionError("invalid explicit file unexpectedly accepted")
            except ValueError as error:
                assert "CLAUDE_CONFIG_DIR" in str(error) and "--auth api" in str(error)
            assert not any(pathlib.Path(reviewers.REV_HOME_BASE).iterdir())
    finally:
        reviewers.REV_HOME_BASE = old_base
        os.environ.clear()
        os.environ.update(old_env)


def test_configured_claude_source_does_not_change_other_provider_or_api_environments():
    old = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory() as directory:
            os.environ.clear()
            os.environ.update(HOME=directory, PATH="/usr/bin", CLAUDE_CONFIG_DIR="/missing/agent", TAUCETI_CLAUDE_USAGE_CREDENTIALS="/missing/usage")
            for provider, subscription, keys in (("claude", False, {"anthropic": "api-key"}), ("codex", True, {})):
                env, home = reviewers.reviewer_env(provider, keys, subscription=subscription)
                try:
                    assert "CLAUDE_CONFIG_DIR" not in env
                    assert "TAUCETI_CLAUDE_USAGE_CREDENTIALS" not in env
                finally:
                    reviewers.cleanup_rev_home(home)
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_default_claude_file_and_unset_legacy_fallback_are_preserved():
    old = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory() as directory:
            os.environ.clear()
            os.environ.update(HOME=directory, PATH="/usr/bin")
            source = pathlib.Path(directory) / ".claude" / ".credentials.json"
            source.parent.mkdir()
            source.write_text("DEFAULT-AGENT")
            env, home = reviewers.reviewer_env("claude", {}, subscription=True)
            try:
                assert env["HOME"] == home
                assert (pathlib.Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json").read_text() == "DEFAULT-AGENT"
            finally:
                reviewers.cleanup_rev_home(home)
            source.unlink()
            env, home = reviewers.reviewer_env("claude", {}, subscription=True)
            try:
                assert env["HOME"] == directory
                assert "CLAUDE_CONFIG_DIR" not in env
            finally:
                reviewers.cleanup_rev_home(home)
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_unusable_explicit_file_refuses_before_mixed_provider_round_preparation():
    import contextlib
    import io
    from unittest.mock import patch
    import review

    with tempfile.TemporaryDirectory() as directory:
        err = io.StringIO()
        # No input files/ledger exist: reaching their read or a rubric means preflight was too late.
        argv = ["review", "--repo", "o/r", "--pr", "1", "--providers", "claude,codex", "--auth", "subscription",
                "--store", str(pathlib.Path(directory) / "store"), "--rubrics-dir", "/not/a/rubric/directory",
                "--diff-file", "/not/a/diff", "--tool-cwd", directory]
        with patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(pathlib.Path(directory) / "missing")), patch.object(sys, "argv", argv), patch.object(review, "run_rubric", side_effect=AssertionError("must not infer")), contextlib.redirect_stderr(err):
            try:
                review.main()
                raise AssertionError("invalid configured file unexpectedly accepted")
            except SystemExit as error:
                assert "CLAUDE_CONFIG_DIR" in str(error)
        assert not (pathlib.Path(directory) / "store").exists()
        assert not err.getvalue()  # No traceback, token, or arbitrary file content.


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} reviewer environment checks passed")
