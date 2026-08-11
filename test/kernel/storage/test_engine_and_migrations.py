from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.kernel.storage import (
    MigrationPostconditionError as ExportedMigrationPostconditionError,
)
from src.kernel.storage.engine import (
    MySQLStorageConfig,
    SQLiteStorageConfig,
    create_sqlite_storage_engine,
    sqlite_storage_health,
)
from src.kernel.storage.migration_runner import (
    MigrationDriftError,
    MigrationPostconditionError,
    MySQLMigrationRunner,
    SchemaMigration,
)


class _MigrationRows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> _MigrationRows:
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.rows)


class _MigrationConnection:
    def __init__(
        self,
        *,
        applied: list[dict[str, Any]] | None = None,
        completion_values: list[int | BaseException] | None = None,
    ) -> None:
        self.applied = list(applied or [])
        self.completion_values = list(completion_values or [])
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0

    async def scalar(
        self,
        statement: object,
        _parameters: dict[str, Any] | None = None,
    ) -> int:
        sql = " ".join(str(statement).split())
        if sql.startswith("SELECT GET_LOCK"):
            return 1
        if not self.completion_values:
            raise AssertionError(f"unexpected scalar query: {sql}")
        value = self.completion_values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def execute(
        self,
        statement: object,
        parameters: dict[str, Any] | None = None,
    ) -> _MigrationRows:
        sql = " ".join(str(statement).split())
        params = dict(parameters or {})
        self.executed.append((sql, params))
        if sql.startswith("SELECT version, name, checksum"):
            return _MigrationRows(self.applied)
        return _MigrationRows([])

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _MigrationConnectionContext:
    def __init__(self, connection: _MigrationConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _MigrationConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _MigrationEngine:
    def __init__(self, connection: _MigrationConnection) -> None:
        self.connection = connection

    def connect(self) -> _MigrationConnectionContext:
        return _MigrationConnectionContext(self.connection)


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


def test_schema_migration_checksum_covers_completion_checks() -> None:
    left = SchemaMigration(
        1,
        "recoverable",
        ("ALTER TABLE alpha ADD COLUMN value INT",),
        ("SELECT 1",),
    )
    changed = SchemaMigration(
        1,
        "recoverable",
        ("ALTER TABLE alpha ADD COLUMN value INT",),
        ("SELECT 0",),
    )

    assert left.checksum != changed.checksum
    assert ExportedMigrationPostconditionError is MigrationPostconditionError


@pytest.mark.asyncio
async def test_runner_adopts_completed_ddl_without_replaying_statement() -> None:
    connection = _MigrationConnection(completion_values=[1])
    migration = SchemaMigration(
        1,
        "recoverable",
        ("ALTER TABLE alpha ADD COLUMN value INT",),
        ("SELECT 1",),
    )
    runner = MySQLMigrationRunner(_MigrationEngine(connection))  # type: ignore[arg-type]

    result = await runner.apply((migration,))

    statements = [sql for sql, _params in connection.executed]
    assert not any(sql.startswith("ALTER TABLE alpha") for sql in statements)
    inserts = [
        params
        for sql, params in connection.executed
        if sql.startswith("INSERT INTO storage_schema_migrations")
    ]
    assert inserts == [
        {
            "version": 1,
            "name": "recoverable",
            "checksum": migration.checksum,
        }
    ]
    assert result.applied_versions == (1,)


@pytest.mark.asyncio
async def test_runner_fails_closed_when_postcondition_remains_false() -> None:
    connection = _MigrationConnection(completion_values=[0, 0])
    migration = SchemaMigration(
        1,
        "recoverable",
        ("ALTER TABLE alpha ADD COLUMN value INT",),
        ("SELECT 1",),
    )
    runner = MySQLMigrationRunner(_MigrationEngine(connection))  # type: ignore[arg-type]

    with pytest.raises(MigrationPostconditionError):
        await runner.apply((migration,))

    statements = [sql for sql, _params in connection.executed]
    assert any(sql.startswith("ALTER TABLE alpha") for sql in statements)
    assert not any(
        sql.startswith("INSERT INTO storage_schema_migrations")
        for sql in statements
    )


@pytest.mark.asyncio
async def test_runner_fails_closed_on_partial_or_failed_completion_probe() -> None:
    partial = SchemaMigration(
        1,
        "partial",
        ("ALTER TABLE alpha ADD COLUMN value INT",),
        ("SELECT 1", "SELECT 0"),
    )
    partial_connection = _MigrationConnection(completion_values=[1, 0])
    partial_runner = MySQLMigrationRunner(  # type: ignore[arg-type]
        _MigrationEngine(partial_connection)
    )

    with pytest.raises(MigrationPostconditionError, match="partially applied"):
        await partial_runner.apply((partial,))
    assert not any(
        sql.startswith("ALTER TABLE alpha")
        or sql.startswith("INSERT INTO storage_schema_migrations")
        for sql, _params in partial_connection.executed
    )

    failed_connection = _MigrationConnection(
        completion_values=[RuntimeError("metadata unavailable")]
    )
    failed_runner = MySQLMigrationRunner(  # type: ignore[arg-type]
        _MigrationEngine(failed_connection)
    )
    with pytest.raises(RuntimeError, match="metadata unavailable"):
        await failed_runner.apply((partial,))
    assert not any(
        sql.startswith("ALTER TABLE alpha")
        or sql.startswith("INSERT INTO storage_schema_migrations")
        for sql, _params in failed_connection.executed
    )


@pytest.mark.asyncio
async def test_runner_detects_completion_check_checksum_drift() -> None:
    original = SchemaMigration(
        1,
        "recoverable",
        ("ALTER TABLE alpha ADD COLUMN value INT",),
        ("SELECT 1",),
    )
    changed = SchemaMigration(
        1,
        "recoverable",
        ("ALTER TABLE alpha ADD COLUMN value INT",),
        ("SELECT 0",),
    )
    connection = _MigrationConnection(
        applied=[
            {
                "version": 1,
                "name": original.name,
                "checksum": original.checksum,
            }
        ]
    )
    runner = MySQLMigrationRunner(_MigrationEngine(connection))  # type: ignore[arg-type]

    with pytest.raises(MigrationDriftError):
        await runner.apply((changed,))


@pytest.mark.asyncio
async def test_runner_without_completion_checks_keeps_legacy_replay_contract() -> None:
    connection = _MigrationConnection()
    migration = SchemaMigration(
        1,
        "legacy",
        ("CREATE TABLE IF NOT EXISTS alpha (id INT)",),
    )
    runner = MySQLMigrationRunner(_MigrationEngine(connection))  # type: ignore[arg-type]

    await runner.apply((migration,))

    statements = [sql for sql, _params in connection.executed]
    assert "CREATE TABLE IF NOT EXISTS alpha (id INT)" in statements
    assert any(
        sql.startswith("INSERT INTO storage_schema_migrations")
        for sql in statements
    )
