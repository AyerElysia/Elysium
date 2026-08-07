"""Read-only readiness contract for the selected MySQL Memory storage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.storage.memory.mysql import (
    _MYSQL_MEMORY_READINESS_REQUIREMENTS,
    MySQLMemoryReadinessProbeError,
    inspect_mysql_memory_readiness,
)
from plugins.life_engine.storage.models import BackendKind, StorageAvailability


class _Rows:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _Connection:
    def __init__(
        self,
        rows: list[dict[str, str]],
        *,
        error: Exception | None = None,
    ) -> None:
        self.rows = rows
        self.error = error
        self.execute_calls = 0
        self.statements: list[str] = []
        self.parameters: list[dict[str, Any]] = []

    async def execute(self, statement: Any, parameters: dict[str, Any]) -> _Rows:
        self.execute_calls += 1
        self.statements.append(str(statement))
        self.parameters.append(parameters)
        if self.error is not None:
            raise self.error
        return _Rows(self.rows)


class _ConnectionContext:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.connect_calls = 0

    def connect(self) -> _ConnectionContext:
        self.connect_calls += 1
        return _ConnectionContext(self.connection)


def _schema_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for requirements in _MYSQL_MEMORY_READINESS_REQUIREMENTS.values():
        for table, columns in requirements.items():
            for column in columns:
                identity = (table, column)
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append({"table_name": table, "column_name": column})
    return rows


def _runtime(connection: _Connection) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        backend=BackendKind.MYSQL,
        engine=_Engine(connection),
    )


@pytest.mark.asyncio
async def test_readiness_uses_one_read_only_metadata_query_for_all_domains() -> None:
    connection = _Connection(_schema_rows())
    runtime = _runtime(connection)

    statuses = await inspect_mysql_memory_readiness(runtime)  # type: ignore[arg-type]

    assert statuses == {
        domain: StorageAvailability.HEALTHY
        for domain in _MYSQL_MEMORY_READINESS_REQUIREMENTS
    }
    assert runtime.engine.connect_calls == 1
    assert connection.execute_calls == 1
    sql = connection.statements[0].upper()
    assert "INFORMATION_SCHEMA.COLUMNS" in sql
    assert "CREATE " not in sql
    assert "ALTER " not in sql
    assert "ENSURE_SCHEMA" not in sql


@pytest.mark.asyncio
async def test_missing_table_fails_only_its_memory_domain() -> None:
    rows = [
        row for row in _schema_rows() if row["table_name"] != "memory_claims"
    ]

    statuses = await inspect_mysql_memory_readiness(  # type: ignore[arg-type]
        _runtime(_Connection(rows))
    )

    assert statuses["epistemic"] == StorageAvailability.FAILED
    assert all(
        status == StorageAvailability.HEALTHY
        for domain, status in statuses.items()
        if domain != "epistemic"
    )


@pytest.mark.asyncio
async def test_missing_key_column_fails_only_its_memory_domain() -> None:
    rows = [
        row
        for row in _schema_rows()
        if not (
            row["table_name"] == "memory_nodes"
            and row["column_name"] == "embedding_content_hash"
        )
    ]

    statuses = await inspect_mysql_memory_readiness(  # type: ignore[arg-type]
        _runtime(_Connection(rows))
    )

    assert statuses["document_index"] == StorageAvailability.FAILED
    assert all(
        status == StorageAvailability.HEALTHY
        for domain, status in statuses.items()
        if domain != "document_index"
    )


@pytest.mark.asyncio
async def test_probe_failure_exposes_only_exception_type() -> None:
    secret = "mysql://elysia:do-not-leak@localhost/life"
    connection = _Connection([], error=RuntimeError(secret))

    with pytest.raises(MySQLMemoryReadinessProbeError) as raised:
        await inspect_mysql_memory_readiness(  # type: ignore[arg-type]
            _runtime(connection)
        )

    assert raised.value.error_type == "RuntimeError"
    assert secret not in str(raised.value)
