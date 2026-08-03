"""MySQL 与本地生命域备份脚本的安全契约测试。"""

from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.backup_life_data import SQLITE_SOURCES, LifeBackupError, create_life_backup
from scripts.backup_mysql import BackupError, _file_sha256, _target_url, verify_snapshot


def test_mysql_backup_url_must_come_from_a_mysql_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_MYSQL_URL", raising=False)
    with pytest.raises(BackupError, match="未设置"):
        _target_url("TEST_MYSQL_URL")

    monkeypatch.setenv("TEST_MYSQL_URL", "sqlite:///data.db")
    with pytest.raises(BackupError, match="MySQL"):
        _target_url("TEST_MYSQL_URL")


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


def test_life_backup_uses_sqlite_online_backup_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    for relative in SQLITE_SOURCES:
        source = data_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(source)
        try:
            connection.execute("CREATE TABLE proof (id INTEGER PRIMARY KEY, value TEXT)")
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
    with pytest.raises(LifeBackupError, match="拒绝覆盖"):
        create_life_backup(data_root, output)
