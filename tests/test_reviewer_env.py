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


def test_grok_subscription_copies_only_auth_json():
    old = os.environ.copy()
    isolated_home = None
    try:
        with tempfile.TemporaryDirectory() as real_home:
            real = pathlib.Path(real_home) / ".grok"
            real.mkdir()
            (real / "auth.json").write_text('{"token": "t"}')
            (real / "config.toml").write_text("personal = true\n")
            os.environ.clear()
            os.environ.update(HOME=real_home, PATH="/usr/bin", XAI_API_KEY="must-not-leak")
            env, isolated_home = reviewers.reviewer_env("grok", {"xai": "xk"}, subscription=True)
            grok_home = pathlib.Path(env["GROK_HOME"])
            assert env["HOME"] == isolated_home
            assert grok_home == pathlib.Path(isolated_home) / ".grok"
            assert sorted(p.name for p in grok_home.iterdir()) == ["auth.json"]
            assert (grok_home / "auth.json").read_text() == '{"token": "t"}'
            assert (grok_home / "auth.json").stat().st_mode & 0o777 == 0o600
            for key, value in reviewers.GROK_ENV.items():
                assert env[key] == value, key
            assert "XAI_API_KEY" not in env
            assert "GROK_FOLDER_TRUST" not in env
    finally:
        os.environ.clear()
        os.environ.update(old)
        reviewers.cleanup_rev_home(isolated_home)


def test_grok_subscription_without_login_raises():
    old = os.environ.copy()
    before = set(os.listdir(reviewers.REV_HOME_BASE)) if os.path.isdir(reviewers.REV_HOME_BASE) else set()
    try:
        with tempfile.TemporaryDirectory() as real_home:
            os.environ.clear()
            os.environ.update(HOME=real_home, PATH="/usr/bin")
            try:
                reviewers.reviewer_env("grok", {}, subscription=True)
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                assert "grok login" in str(exc), str(exc)
    finally:
        os.environ.clear()
        os.environ.update(old)
    # The throwaway HOME made before the failure is not leaked.
    after = set(os.listdir(reviewers.REV_HOME_BASE))
    assert not {n for n in after - before if n.startswith("rev-grok-")}, after - before


def test_grok_api_mode_passes_only_xai_key():
    env, isolated_home = reviewers.reviewer_env("grok", {"xai": "  xk  ", "openai": "o"})
    try:
        assert env["XAI_API_KEY"] == "xk"
        assert "OPENAI_API_KEY" not in env
        assert "CODEX_HOME" not in env
        grok_home = pathlib.Path(env["GROK_HOME"])
        assert grok_home.is_dir() and grok_home.is_relative_to(pathlib.Path(isolated_home))
        assert not (grok_home / "auth.json").exists()
        for key, value in reviewers.GROK_ENV.items():
            assert env[key] == value, key
    finally:
        reviewers.cleanup_rev_home(isolated_home)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} reviewer environment checks passed")
