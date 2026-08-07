from __future__ import annotations

from pathlib import Path

import pytest

from src.kernel.storage.engine import (
    MySQLStorageConfig,
    SQLiteStorageConfig,
    create_sqlite_storage_engine,
    sqlite_storage_health,
)
from src.kernel.storage.migration_runner import SchemaMigration


def test_mysql_config_is_secret_free_and_requires_utf8mb4() -> None:
    config = MySQLStorageConfig(
        host="db.example",
        port=3306,
        database="elysium",
        user="app",
        password="do-not-log",
    )

    assert config.safe_identity == "mysql://app@db.example:3306/elysium"
    assert config.idle_session_timeout_seconds == 180
    assert "do-not-log" not in config.safe_identity
    with pytest.raises(ValueError):
        MySQLStorageConfig(
            host="db.example",
            port=3306,
            database="elysium",
            user="app",
            password="secret",
            charset="utf8",
        )
    with pytest.raises(ValueError, match="idle_session_timeout_seconds"):
        MySQLStorageConfig(
            host="db.example",
            port=3306,
            database="elysium",
            user="app",
            password="secret",
            idle_session_timeout_seconds=0,
        )


async def test_sqlite_storage_health_has_required_session_contract(tmp_path: Path) -> None:
    config = SQLiteStorageConfig(tmp_path / "storage.sqlite3", busy_timeout_seconds=2)
    engine = create_sqlite_storage_engine(config)
    try:
        health = await sqlite_storage_health(
            engine,
            backend_identity=config.safe_identity,
        )
    finally:
        await engine.dispose()

    assert health["status"] == "healthy"
    assert health["quick_check"] == "ok"
    assert health["foreign_keys"] is True
    assert health["journal_mode"] == "wal"


def test_schema_migration_checksum_covers_exact_statements() -> None:
    left = SchemaMigration(1, "base", ("CREATE TABLE alpha (id INT)",))
    same = SchemaMigration(1, "base", ("CREATE TABLE alpha (id INT)",))
    changed = SchemaMigration(1, "base", ("CREATE TABLE alpha (id BIGINT)",))

    assert left.checksum == same.checksum
    assert left.checksum != changed.checksum
    with pytest.raises(ValueError):
        SchemaMigration(0, "base", ("SELECT 1",))
