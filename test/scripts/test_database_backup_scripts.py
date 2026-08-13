"""MySQL 与本地生命域备份脚本的安全契约测试。"""

from __future__ import annotations

import gzip
import io
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from scripts.backup_life_data import SQLITE_SOURCES, LifeBackupError, create_life_backup
from scripts.backup_mysql import (
    BackupError,
    _file_sha256,
    _mysql_defaults_file,
    _target_url,
    create_snapshot,
    verify_snapshot,
)
from scripts.sync_unified_memory import RESTORE_SQLITE_TARGETS
from src.kernel.memory_archive.sources import (
    DEFAULT_SQLITE_SOURCES,
    ArchiveSourceError,
    verify_backup_manifest,
)


def test_mysql_backup_url_must_come_from_a_mysql_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_MYSQL_URL", raising=False)
    with pytest.raises(BackupError, match="未设置"):
        _target_url("TEST_MYSQL_URL")

    monkeypatch.setenv("TEST_MYSQL_URL", "sqlite:///data.db")
    with pytest.raises(BackupError, match="MySQL"):
        _target_url("TEST_MYSQL_URL")


def test_core_sqlite_default_is_consistent_across_backup_archive_and_restore() -> None:
    assert SQLITE_SOURCES[0] == Path("MoFox.db")
    assert DEFAULT_SQLITE_SOURCES[0].domain == "core"
    assert DEFAULT_SQLITE_SOURCES[0].relative_path == Path("MoFox.db")
    assert RESTORE_SQLITE_TARGETS["core"] == Path("MoFox.db")


def test_mysql_backup_verify_checks_sha_and_gzip_crc(tmp_path: Path) -> None:
    snapshot = tmp_path / "backup.sql.gz"
    with gzip.open(snapshot, "wb") as compressed:
        compressed.write(b"CREATE TABLE proof (id INTEGER);\n")
    manifest = snapshot.with_suffix(snapshot.suffix + ".manifest.json")
    manifest.write_text(
        json.dumps({"sha256": _file_sha256(snapshot)}),
        encoding="utf-8",
    )

    result = verify_snapshot(snapshot)
    assert result["verified"] is True
    assert result["uncompressed_bytes"] > 0

    snapshot.write_bytes(snapshot.read_bytes() + b"corruption")
    with pytest.raises(BackupError, match="SHA-256"):
        verify_snapshot(snapshot)


def test_mysql_backup_defaults_file_is_private_and_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = 'secret with # ; " and \\'
    monkeypatch.setenv(
        "TEST_MYSQL_URL",
        "mysql+asyncmy://backup-user:" + password.replace("#", "%23") + "@db/elysium",
    )
    url = _target_url("TEST_MYSQL_URL")

    with _mysql_defaults_file(url) as defaults_file:
        assert stat.S_IMODE(defaults_file.stat().st_mode) == 0o600
        contents = defaults_file.read_text(encoding="utf-8")
        assert "password=" in contents
        assert "secret with" in contents
        retained_path = defaults_file

    assert not retained_path.exists()


def test_mysql_backup_protects_empty_defaults_file_before_writing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "must-never-exist-before-acl"
    monkeypatch.setenv(
        "TEST_MYSQL_URL",
        f"mysql+asyncmy://backup-user:{password}@db/elysium",
    )
    inspected: list[Path] = []

    def inspect_empty_file(path: Path) -> None:
        assert path.read_bytes() == b""
        inspected.append(path)

    monkeypatch.setattr(
        "scripts.backup_mysql._restrict_windows_file_acl",
        inspect_empty_file,
    )

    with _mysql_defaults_file(_target_url("TEST_MYSQL_URL")) as defaults_file:
        retained = defaults_file
        assert password in defaults_file.read_text(encoding="utf-8")

    assert inspected == [retained]
    assert not retained.exists()


def test_mysql_backup_acl_failure_removes_empty_defaults_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEST_MYSQL_URL",
        "mysql+asyncmy://backup-user:secret@db/elysium",
    )
    retained: list[Path] = []

    def fail_acl(path: Path) -> None:
        assert path.read_bytes() == b""
        retained.append(path)
        raise BackupError("acl failed")

    monkeypatch.setattr(
        "scripts.backup_mysql._restrict_windows_file_acl",
        fail_acl,
    )

    with (
        pytest.raises(BackupError, match="acl failed"),
        _mysql_defaults_file(_target_url("TEST_MYSQL_URL")),
    ):
        pytest.fail("credential file must not be yielded")

    assert len(retained) == 1
    assert not retained[0].exists()


def test_mysql_backup_hides_secret_from_argv_environment_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "do-not-leak-backup-password"
    monkeypatch.setenv(
        "TEST_MYSQL_URL",
        f"mysql+asyncmy://backup-user:{password}@db/elysium",
    )
    monkeypatch.setenv("ELYSIUM_MYSQL_URL", password)
    captured: dict[str, object] = {}

    class FailedProcess:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            captured["command"] = command
            captured["environment"] = kwargs["env"]
            self.stdout = io.BytesIO()

        def wait(self) -> int:
            return 7

        def kill(self) -> None:
            return None

    monkeypatch.setattr("scripts.backup_mysql.shutil.which", lambda name: "/fake/dump")
    monkeypatch.setattr("scripts.backup_mysql.subprocess.Popen", FailedProcess)

    with pytest.raises(BackupError, match="详细输出已隐藏") as caught:
        create_snapshot(_target_url("TEST_MYSQL_URL"), tmp_path / "output")

    command = captured["command"]
    assert isinstance(command, list)
    assert all(password not in item for item in command)
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert password not in environment.values()
    assert password not in str(caught.value)


def test_mysql_backup_secures_empty_credential_and_output_files_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEST_MYSQL_URL",
        "mysql+asyncmy://backup-user:secret@db/elysium",
    )
    protected: list[Path] = []

    def inspect_empty_file(path: Path) -> None:
        assert path.is_file()
        assert path.read_bytes() == b""
        protected.append(path)

    class SuccessfulProcess:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            del command, kwargs
            self.stdout = io.BytesIO(b"CREATE TABLE proof (id INTEGER);\n")

        def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr(
        "scripts.backup_mysql._restrict_windows_file_acl",
        inspect_empty_file,
    )
    monkeypatch.setattr("scripts.backup_mysql.shutil.which", lambda name: "/fake/dump")
    monkeypatch.setattr("scripts.backup_mysql.subprocess.Popen", SuccessfulProcess)

    result = create_snapshot(_target_url("TEST_MYSQL_URL"), tmp_path / "output")

    protected_names = [path.name for path in protected]
    assert any(name.endswith(".cnf") for name in protected_names)
    assert any(name.endswith(".partial") for name in protected_names)
    assert any(name.endswith(".manifest.json") for name in protected_names)
    assert Path(str(result["snapshot"])).is_file()


def test_mysql_backup_closes_partial_descriptor_when_fdopen_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEST_MYSQL_URL",
        "mysql+asyncmy://backup-user:secret@db/elysium",
    )
    opened_partial: list[int] = []
    original_fdopen = os.fdopen
    fdopen_calls = 0

    def fail_partial_fdopen(
        descriptor: int,
        *args: object,
        **kwargs: object,
    ) -> io.TextIOWrapper:
        nonlocal fdopen_calls
        fdopen_calls += 1
        if fdopen_calls == 2:
            opened_partial.append(descriptor)
            raise RuntimeError("fdopen failed")
        return original_fdopen(descriptor, *args, **kwargs)  # type: ignore[return-value]

    class Process:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            del command, kwargs
            self.stdout = io.BytesIO(b"snapshot")

        def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr("scripts.backup_mysql.shutil.which", lambda name: "/fake/dump")
    monkeypatch.setattr("scripts.backup_mysql.subprocess.Popen", Process)
    monkeypatch.setattr("scripts.backup_mysql.os.fdopen", fail_partial_fdopen)

    with pytest.raises(RuntimeError, match="fdopen failed"):
        create_snapshot(_target_url("TEST_MYSQL_URL"), tmp_path / "output")

    assert len(opened_partial) == 1
    with pytest.raises(OSError):
        os.fstat(opened_partial[0])
    assert not list((tmp_path / "output").glob("*.partial"))


def test_mysql_backup_precreated_output_must_be_empty_ordinary_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEST_MYSQL_URL",
        "mysql+asyncmy://backup-user:secret@db/elysium",
    )
    url = _target_url("TEST_MYSQL_URL")
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "existing.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(BackupError, match="必须为空"):
        create_snapshot(url, nonempty, precreated_output=True)

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "output-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(BackupError, match="符号链接"):
        create_snapshot(url, link, precreated_output=True)

    assert (nonempty / "existing.txt").read_text(encoding="utf-8") == "preserve"
    assert not any(target.iterdir())


def test_life_backup_uses_sqlite_online_backup_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    for relative in SQLITE_SOURCES:
        source = data_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(source)
        try:
            connection.execute(
                "CREATE TABLE proof (id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.execute("INSERT INTO proof(value) VALUES ('爱莉')")
            connection.commit()
        finally:
            connection.close()
    note = data_root / "life_engine_workspace/notes/proof.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# 记忆\n", encoding="utf-8")

    output = tmp_path / "backup"
    result = create_life_backup(data_root, output)

    assert len(result["sqlite"]) == len(SQLITE_SOURCES)
    assert result["workspace_file_count"] == 1
    assert (output / "manifest.json").is_file()
    assert all(item["integrity_check"] == "ok" for item in result["sqlite"])
    assert verify_backup_manifest(output) == {
        "sqlite": len(SQLITE_SOURCES),
        "workspace": 1,
    }
    note_backup = output / "workspace/life_engine_workspace/notes/proof.md"
    note_backup.write_text("corrupted", encoding="utf-8")
    with pytest.raises(ArchiveSourceError, match="mismatch"):
        verify_backup_manifest(output)
    with pytest.raises(LifeBackupError, match="拒绝覆盖"):
        create_life_backup(data_root, output)
