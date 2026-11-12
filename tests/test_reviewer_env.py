#!/usr/bin/env python3
"""The isolated reviewer environment keeps login identity without inheriting personal config."""

import os
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


def _claude_subscription_env(provider, token=None, credential_file=True, subscription=True, keys=None):
    """reviewer_env under a synthetic real HOME and reviewer-home base. Returns the env, whether
    the isolated home held a seeded credential file, and the synthetic real HOME."""
    old_env, old_base = os.environ.copy(), reviewers.REV_HOME_BASE
    try:
        with tempfile.TemporaryDirectory() as real_home, tempfile.TemporaryDirectory() as base:
            reviewers.REV_HOME_BASE = base
            if credential_file:
                cdir = pathlib.Path(real_home) / ".claude"
                cdir.mkdir()
                (cdir / ".credentials.json").write_text('{"synthetic": true}')
            os.environ.clear()
            os.environ.update(HOME=real_home, PATH="/usr/bin", USER="synthetic-user")
            if token is not None:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
            env, isolated_home = reviewers.reviewer_env(provider, keys or {}, subscription=subscription)
            try:
                seeded = (pathlib.Path(isolated_home) / ".claude" / ".credentials.json").exists()
                return env, isolated_home, seeded, real_home
            finally:
                reviewers.cleanup_rev_home(isolated_home)
    finally:
        reviewers.REV_HOME_BASE = old_base
        os.environ.clear()
        os.environ.update(old_env)


def test_claude_subscription_token_replaces_credential_file():
    for provider in ("claude", "sonnet"):
        env, isolated_home, seeded, real_home = _claude_subscription_env(provider, token=" tok-synthetic ")
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-synthetic"
        assert env["HOME"] == isolated_home != real_home
        assert not seeded
        assert set(env) == {"PATH", "HOME", "LANG", "CI", "USER", "LOGNAME", "CLAUDE_CODE_OAUTH_TOKEN"}


def test_claude_subscription_token_keeps_clean_home_without_credential_file():
    env, isolated_home, seeded, real_home = _claude_subscription_env(
        "claude", token="tok-synthetic", credential_file=False)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-synthetic"
    assert env["HOME"] == isolated_home != real_home
    assert not seeded


def test_claude_subscription_without_token_is_unchanged():
    for token in (None, "", "   "):
        env, isolated_home, seeded, _ = _claude_subscription_env("claude", token=token)
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env["HOME"] == isolated_home and seeded
        env, isolated_home, seeded, real_home = _claude_subscription_env(
            "claude", token=token, credential_file=False)
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env["HOME"] == real_home and not seeded


def test_oauth_token_reaches_no_other_reviewer():
    cases = [("codex", True, {}), ("deepseek", True, {"openrouter": "or-synthetic"}),
             ("kiro", True, {}), ("claude", False, {"anthropic": "sk-synthetic"}),
             ("codex", False, {"openai": "sk-synthetic"})]
    for provider, subscription, keys in cases:
        env, *_ = _claude_subscription_env(provider, token="tok-synthetic",
                                           subscription=subscription, keys=keys)
        assert "tok-synthetic" not in env.values(), (provider, subscription)
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env, (provider, subscription)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} reviewer environment checks passed")
