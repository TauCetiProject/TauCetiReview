#!/usr/bin/env python3
"""The isolated reviewer environment keeps login identity without inheriting personal config."""

import os
import pathlib
import sys
import tempfile
import sqlite3
import json
import subprocess
import types
from unittest.mock import patch

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


def test_bedrock_profile_exports_only_selected_credentials_in_the_parent():
    with tempfile.TemporaryDirectory() as real_home:
        aws = pathlib.Path(real_home) / ".aws"
        aws.mkdir()
        (aws / "config").write_text("[profile review]\nregion = us-east-1\n")
        (aws / "credentials").write_text("[another-profile]\naws_secret_access_key = other-secret\n")
        parent = {
            "HOME": real_home, "PATH": "/usr/bin",
            "CLAUDE_CODE_USE_BEDROCK": "false", "AWS_PROFILE": "review",
            "AWS_REGION": "us-east-1", "CLAUDE_CODE_EFFORT_LEVEL": "high",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "us.anthropic.claude-opus-5",
            "AWS_CONFIG_FILE": os.path.relpath(aws / "config"),
            "AWS_SHARED_CREDENTIALS_FILE": os.path.relpath(aws / "credentials"),
            "AWS_ACCESS_KEY_ID": "ambient-access", "AWS_SECRET_ACCESS_KEY": "ambient-secret",
            "AWS_WEB_IDENTITY_TOKEN_FILE": "/private/token", "AWS_ROLE_ARN": "private-role",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://private/credentials",
            "GH_TOKEN": "not-for-the-reviewer", "OPENAI_API_KEY": "not-for-claude",
            "ANTHROPIC_API_KEY": "not-for-bedrock", "CLAUDE_CODE_OAUTH_TOKEN": "not-for-bedrock",
            "CLAUDE_CONFIG_DIR": "/personal/config", "PERSONAL_SETTING": "must-not-leak",
        }
        def export(cmd, **kwargs):
            assert cmd == ["aws", "configure", "export-credentials", "--format", "process",
                           "--profile", "review"]
            # Profile/SSO helpers run with the real HOME, before the child HOME exists.
            assert kwargs["cwd"] == real_home
            assert kwargs["env"]["HOME"] == real_home
            assert kwargs["env"]["AWS_CONFIG_FILE"] == str(aws / "config")
            assert kwargs["env"]["AWS_SHARED_CREDENTIALS_FILE"] == str(aws / "credentials")
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert kwargs["capture_output"] and kwargs["timeout"] == 60
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({
                "Version": 1, "AccessKeyId": "selected-access",
                "SecretAccessKey": "selected-secret", "SessionToken": "selected-session",
                "Expiration": "2099-01-01T00:00:00Z",
            }))
        with patch.dict(os.environ, parent, clear=True), patch.object(
            reviewers.subprocess, "run", side_effect=export
        ) as exported:
            for provider in ("claude", "sonnet"):
                env, isolated_home = reviewers.reviewer_env(
                    provider, {}, bedrock=True
                )
                try:
                    assert env["HOME"] == isolated_home != real_home
                    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
                    assert env["AWS_REGION"] == "us-east-1"
                    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "high"
                    assert env["AWS_ACCESS_KEY_ID"] == "selected-access"
                    assert env["AWS_SECRET_ACCESS_KEY"] == "selected-secret"
                    assert env["AWS_SESSION_TOKEN"] == "selected-session"
                    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
                    assert {k for k in env if k.startswith("AWS_")} == {
                        "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                        "AWS_SESSION_TOKEN", "AWS_EC2_METADATA_DISABLED",
                    }
                    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env  # exact --model owns selection
                    assert str(aws) not in json.dumps(env)
                    assert not (pathlib.Path(isolated_home) / ".claude" / ".credentials.json").exists()
                    for name in (
                        "GH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                        "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "PERSONAL_SETTING",
                    ):
                        assert name not in env, name
                finally:
                    reviewers.cleanup_rev_home(isolated_home)
            assert exported.call_count == 2  # credentials refreshed between attempts


def test_bedrock_environment_credentials_need_no_aws_cli_or_host_paths():
    with patch.dict(os.environ, {
        "PATH": "/usr/bin", "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_CONFIG_FILE": "/task/aws-config",
        "AWS_SHARED_CREDENTIALS_FILE": "/task/aws-credentials",
        "AWS_ACCESS_KEY_ID": "test-access", "AWS_SECRET_ACCESS_KEY": "test-secret",
        "AWS_SESSION_TOKEN": "test-session",
    }, clear=True), patch.object(reviewers.subprocess, "run") as exported:
        env, isolated_home = reviewers.reviewer_env("sonnet", {}, bedrock=True)
        try:
            assert env["HOME"] == isolated_home
            assert "AWS_CONFIG_FILE" not in env
            assert "AWS_SHARED_CREDENTIALS_FILE" not in env
            assert env["AWS_REGION"] == "us-east-1"
            assert env["AWS_SESSION_TOKEN"] == "test-session"
            assert env["AWS_SECRET_ACCESS_KEY"] == "test-secret"
            exported.assert_not_called()
        finally:
            reviewers.cleanup_rev_home(isolated_home)


def test_bedrock_bearer_excludes_other_aws_credentials():
    with patch.dict(os.environ, {
        "PATH": "/usr/bin", "AWS_REGION": "us-east-1",
        "AWS_BEARER_TOKEN_BEDROCK": "test-bearer", "AWS_PROFILE": "unused",
        "AWS_ACCESS_KEY_ID": "unused-access", "AWS_SECRET_ACCESS_KEY": "unused-secret",
    }, clear=True), patch.object(reviewers.subprocess, "run") as exported:
        env, home = reviewers.reviewer_env("claude", {}, bedrock=True)
        try:
            assert env["AWS_BEARER_TOKEN_BEDROCK"] == "test-bearer"
            assert {k for k in env if k.startswith("AWS_")} == {
                "AWS_REGION", "AWS_BEARER_TOKEN_BEDROCK", "AWS_EC2_METADATA_DISABLED",
            }
            exported.assert_not_called()
        finally:
            reviewers.cleanup_rev_home(home)


def test_ambient_bedrock_flags_cannot_override_explicit_api_or_subscription():
    with tempfile.TemporaryDirectory() as home:
        auth = pathlib.Path(home) / ".claude" / ".credentials.json"
        auth.parent.mkdir()
        auth.write_text('{"test": "subscription"}')
        for flag in ("1", "true", "TRUE", "yes", "false", ""):
            with patch.dict(os.environ, {
                "HOME": home, "PATH": "/usr/bin", "CLAUDE_CODE_USE_BEDROCK": flag,
                "AWS_SECRET_ACCESS_KEY": "not-for-claude",
            }, clear=True), patch.object(reviewers.subprocess, "run") as exported:
                for subscription in (False, True):
                    env, isolated = reviewers.reviewer_env(
                        "claude", {"anthropic": "api-key"}, subscription=subscription
                    )
                    try:
                        assert env["HOME"] == isolated != home
                        assert "CLAUDE_CODE_USE_BEDROCK" not in env
                        assert not any(k.startswith("AWS_") for k in env)
                        if subscription:
                            assert "ANTHROPIC_API_KEY" not in env
                            assert (pathlib.Path(isolated) / ".claude/.credentials.json").read_text() == auth.read_text()
                        else:
                            assert env["ANTHROPIC_API_KEY"] == "api-key"
                    finally:
                        reviewers.cleanup_rev_home(isolated)
                exported.assert_not_called()


def test_bedrock_export_failures_expose_no_output_and_leave_no_home():
    secret_output = "credential-process-SECRET-must-not-leak"
    failures = [
        types.SimpleNamespace(returncode=1, stdout=secret_output, stderr=secret_output),
        types.SimpleNamespace(returncode=0, stdout=secret_output),
        types.SimpleNamespace(returncode=0, stdout="[]"),
        types.SimpleNamespace(returncode=0, stdout='{"Version":1,"AccessKeyId":"x"}'),
        FileNotFoundError(secret_output),
        subprocess.TimeoutExpired("aws", 60, output=secret_output, stderr=secret_output),
    ]
    with tempfile.TemporaryDirectory() as base, patch.object(reviewers, "REV_HOME_BASE", base):
        for failure in failures:
            mock = ({"side_effect": failure} if isinstance(failure, Exception)
                    else {"return_value": failure})
            with patch.dict(os.environ, {"AWS_REGION": "us-east-1"}, clear=True), patch.object(
                reviewers.subprocess, "run", **mock
            ):
                try:
                    reviewers.reviewer_env("claude", {}, bedrock=True)
                except reviewers.BedrockAuthError as e:
                    assert secret_output not in str(e)
                    assert e.__suppress_context__
                else:
                    raise AssertionError("bad credentials did not fail closed")
                assert list(pathlib.Path(base).iterdir()) == []


def test_bedrock_missing_region_or_partial_keys_fail_before_launch():
    for parent in ({}, {"AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "partial"}):
        with patch.dict(os.environ, parent, clear=True), patch.object(
            reviewers.subprocess, "run"
        ) as exported:
            try:
                reviewers.reviewer_env("claude", {}, bedrock=True)
            except reviewers.BedrockAuthError:
                pass
            else:
                raise AssertionError("incomplete AWS setup was accepted")
            exported.assert_not_called()


def test_bedrock_credentials_do_not_reach_codex():
    with patch.dict(os.environ, {
        "PATH": "/usr/bin", "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_PROFILE": "review", "AWS_SECRET_ACCESS_KEY": "not-for-codex",
        "AWS_CONFIG_FILE": "/task/aws-config",
    }, clear=True):
        env, isolated_home = reviewers.reviewer_env("codex", {"openai": "codex-key"})
        try:
            assert not any(name.startswith("AWS_") for name in env)
            assert "CLAUDE_CODE_USE_BEDROCK" not in env
            assert env["OPENAI_API_KEY"] == "codex-key"
        finally:
            reviewers.cleanup_rev_home(isolated_home)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} reviewer environment checks passed")
