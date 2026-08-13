"""SQLite → MySQL 显式同步工具的安全契约测试。"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from scripts import sync_local_to_mysql as sync_script

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _config(
    tmp_path: Path, *, password: str = "private secret"
) -> sync_script.SyncConfig:
    sqlite_path = tmp_path / "source.sqlite3"
    sqlite_path.touch()
    mysql_client = tmp_path / "mysql"
    mysql_client.touch(mode=0o700)
    return sync_script.SyncConfig(
        sqlite_path=sqlite_path,
        mysql_client=mysql_client,
        host="database.internal",
        port=3306,
        user="sync-user",
        password=password,
        database="target-database",
        ssl_mode="disabled",
        ssl_ca=None,
        ssl_cert=None,
        ssl_key=None,
        command_timeout_seconds=30,
    )


def test_defaults_file_is_private_and_removed(tmp_path: Path) -> None:
    config = _config(tmp_path, password='secret with # ; " and \\')

    with sync_script.mysql_defaults_file(config) as defaults_file:
        assert stat.S_IMODE(defaults_file.stat().st_mode) == 0o600
        contents = defaults_file.read_text(encoding="utf-8")
        assert "password=" in contents
        assert "secret with" in contents
        retained_path = defaults_file

    assert not retained_path.exists()


def test_defaults_file_is_acl_protected_before_secret_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, password="must-not-exist-before-acl")
    inspected: list[Path] = []

    def inspect_empty_file(path: Path) -> None:
        assert path.read_bytes() == b""
        inspected.append(path)

    monkeypatch.setattr(sync_script, "_restrict_windows_file_acl", inspect_empty_file)

    with sync_script.mysql_defaults_file(config) as defaults_file:
        retained = defaults_file
        assert config.password in defaults_file.read_text(encoding="utf-8")

    assert inspected == [retained]
    assert not retained.exists()


def test_mysql_password_is_absent_from_argv_and_child_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "do-not-leak-this-password"
    config = _config(tmp_path, password=password)
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        defaults_argument = command[1]
        defaults_file = Path(defaults_argument.split("=", maxsplit=1)[1])
        assert password in defaults_file.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            command, 0, stdout="existing-key\n", stderr=""
        )

    monkeypatch.setenv("ELYSIUM_MYSQL_PASSWORD", password)
    monkeypatch.setenv("MYSQL_PWD", password)
    monkeypatch.setattr(sync_script.subprocess, "run", fake_run)

    with sync_script.mysql_defaults_file(config) as defaults_file:
        assert sync_script.run_mysql(config, defaults_file, "SELECT 1;\n") == (
            "existing-key\n"
        )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[1].startswith("--defaults-file=")
    assert not any(item.startswith("--defaults-extra-file=") for item in command)
    assert all(password not in item for item in command)
    child_environment = captured["environment"]
    assert isinstance(child_environment, dict)
    assert "ELYSIUM_MYSQL_PASSWORD" not in child_environment
    assert "MYSQL_PWD" not in child_environment


def test_config_has_no_remote_identity_defaults(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "source.sqlite3"
    sqlite_path.touch()
    mysql_client = tmp_path / "mysql"
    mysql_client.touch(mode=0o700)
    environment = {
        "ELYSIUM_SYNC_SQLITE_PATH": str(sqlite_path),
        "ELYSIUM_MYSQL_CLIENT": str(mysql_client),
        "ELYSIUM_MYSQL_HOST": "configured-host",
        "ELYSIUM_MYSQL_PORT": "3307",
        "ELYSIUM_MYSQL_USER": "configured-user",
        "ELYSIUM_MYSQL_PASSWORD": "configured-password",
        "ELYSIUM_MYSQL_DATABASE": "configured-database",
        "ELYSIUM_MYSQL_SSL_MODE": "disabled",
    }

    config = sync_script.load_config(environment)

    assert config.sqlite_path == sqlite_path
    assert config.mysql_client == mysql_client
    assert config.host == "configured-host"
    assert config.port == 3307
    assert config.user == "configured-user"
    assert config.database == "configured-database"
    assert "configured-password" not in repr(config)

    for missing_name in (
        "ELYSIUM_SYNC_SQLITE_PATH",
        "ELYSIUM_MYSQL_HOST",
        "ELYSIUM_MYSQL_PORT",
        "ELYSIUM_MYSQL_USER",
        "ELYSIUM_MYSQL_PASSWORD",
        "ELYSIUM_MYSQL_DATABASE",
        "ELYSIUM_MYSQL_SSL_MODE",
    ):
        incomplete = dict(environment)
        incomplete.pop(missing_name)
        with pytest.raises(sync_script.SyncError):
            sync_script.load_config(incomplete)


def test_database_confirmation_is_checked_before_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, password="not-in-output")
    monkeypatch.setattr(sync_script, "load_config", lambda: config)

    def unexpected_sync(*args: object, **kwargs: object) -> int:
        pytest.fail("database mismatch must be rejected before sync")

    monkeypatch.setattr(sync_script, "sync", unexpected_sync)

    assert sync_script.main(["--dry-run", "--confirm-database", "wrong-database"]) == 2
    output = capsys.readouterr()
    assert "不一致" in output.err
    assert "not-in-output" not in output.err


def test_text_values_are_encoded_without_raw_sql_interpolation() -> None:
    dangerous = "value'); DROP TABLE messages; --"
    literal = sync_script.mysql_literal(dangerous)

    assert dangerous not in literal
    assert literal.startswith("CONVERT(X'")
    assert literal.endswith("' USING utf8mb4)")


def test_sync_entrypoints_cannot_restore_implicit_remote_actions() -> None:
    cleanup_entrypoint = (PROJECT_ROOT / "scripts/cleanup_leases.sh").read_text(
        encoding="utf-8"
    )
    sync_entrypoint = (PROJECT_ROOT / "scripts/sync_job.sh").read_text(encoding="utf-8")

    assert "asyncmy" not in cleanup_entrypoint
    assert "exit 78" in cleanup_entrypoint
    assert "crontab" not in sync_entrypoint
    assert "--confirm-explicit-run" in sync_entrypoint

    combined = f"{cleanup_entrypoint}\n{sync_entrypoint}"
    assert "/root/" not in combined
    assert "export ELYSIUM_MYSQL_PASSWORD=" not in combined
