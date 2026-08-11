"""Read-only readiness contract for the selected MySQL Memory storage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.storage.memory.mysql import (
    _MYSQL_MEMORY_READINESS_COLUMN_CONTRACTS,
    _MYSQL_MEMORY_READINESS_INDEX_REQUIREMENTS,
    _MYSQL_MEMORY_READINESS_REQUIREMENTS,
    MySQLMemoryReadinessProbeError,
    inspect_mysql_memory_readiness,
)
from plugins.life_engine.storage.memory.schema import (
    MEMORY_MIGRATIONS,
    MEMORY_SCHEMA_VERSION,
)
from plugins.life_engine.storage.models import BackendKind, StorageAvailability


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _Connection:
    def __init__(
        self,
        rows: list[dict[str, Any]],
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


def _schema_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for requirements in _MYSQL_MEMORY_READINESS_REQUIREMENTS.values():
        for table, columns in requirements.items():
            for column in columns:
                identity = (table, column)
                if identity in seen:
                    continue
                seen.add(identity)
                contract = _MYSQL_MEMORY_READINESS_COLUMN_CONTRACTS.get(
                    "document_index",
                    {},
                ).get(identity)
                rows.append(
                    {
                        "metadata_kind": "column",
                        "table_name": table,
                        "metadata_name": column,
                        "non_unique": None,
                        "seq_in_index": None,
                        "index_column_name": None,
                        "data_type": contract[0] if contract else None,
                        "character_maximum_length": (
                            contract[1] if contract else None
                        ),
                        "is_nullable": contract[2] if contract else None,
                        "column_default": contract[3] if contract else None,
                        "character_set_name": contract[4] if contract else None,
                        "collation_name": contract[5] if contract else None,
                    }
                )
    for requirements in _MYSQL_MEMORY_READINESS_INDEX_REQUIREMENTS.values():
        for table, indexes in requirements.items():
            for index, (non_unique, columns) in indexes.items():
                for sequence, column in enumerate(columns, start=1):
                    rows.append(
                        {
                            "metadata_kind": "index",
                            "table_name": table,
                            "metadata_name": index,
                            "non_unique": non_unique,
                            "seq_in_index": sequence,
                            "index_column_name": column,
                            "data_type": None,
                            "character_maximum_length": None,
                            "is_nullable": None,
                            "column_default": None,
                            "character_set_name": None,
                            "collation_name": None,
                        }
                    )
    return rows


def _runtime(connection: _Connection) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        backend=BackendKind.MYSQL,
        engine=_Engine(connection),
    )


def test_index_job_lease_migration_is_contiguous_and_checksummed() -> None:
    assert [migration.version for migration in MEMORY_MIGRATIONS] == list(
        range(1, MEMORY_SCHEMA_VERSION + 1)
    )
    migration = next(item for item in MEMORY_MIGRATIONS if item.version == 10)
    sql = " ".join(migration.statements[0].split()).upper()

    assert migration.name == "life_memory_index_job_lease_v1"
    assert len(migration.checksum) == 64
    assert "ADD COLUMN CLAIM_TOKEN CHAR(32)" in sql
    assert "DROP PRIMARY KEY" in sql
    assert "ADD PRIMARY KEY (JOB_ID, INDEX_REVISION)" in sql
    assert "UNIQUE KEY UQ_MEMORY_JOBS_NODE_REVISION" in sql
    assert "(NODE_ID, INDEX_REVISION)" in sql
    checks = " ".join(migration.completion_checks).upper()
    assert "COLUMN_DEFAULT = ''" in checks
    assert "SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'JOB_ID'" in checks
    assert "SEQ_IN_INDEX = 2 AND COLUMN_NAME = 'INDEX_REVISION'" in checks

    tombstone = next(item for item in MEMORY_MIGRATIONS if item.version == 11)
    assert tombstone.name == "life_memory_vector_tombstone_force_delete_v1"
    assert "ADD COLUMN FORCE_DELETE BOOLEAN NOT NULL DEFAULT FALSE" in " ".join(
        tombstone.statements[0].split()
    ).upper()
    assert "COLUMN_DEFAULT = '0'" in " ".join(
        tombstone.completion_checks
    ).upper()


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
    assert "INFORMATION_SCHEMA.STATISTICS" in sql
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
            and row["metadata_name"] == "embedding_content_hash"
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
async def test_missing_revision_identity_index_fails_document_domain() -> None:
    rows = [
        row
        for row in _schema_rows()
        if row["metadata_name"] != "uq_memory_jobs_node_revision"
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
@pytest.mark.parametrize(
    ("table", "column", "field", "drifted_value"),
    [
        ("memory_index_jobs", "claim_token", "data_type", "varchar"),
        (
            "memory_index_jobs",
            "claim_token",
            "character_maximum_length",
            64,
        ),
        ("memory_index_jobs", "claim_token", "is_nullable", "YES"),
        ("memory_index_jobs", "claim_token", "column_default", None),
        ("memory_index_jobs", "claim_token", "character_set_name", "utf8mb4"),
        ("memory_index_jobs", "claim_token", "collation_name", "utf8mb4_bin"),
        (
            "memory_vector_tombstones",
            "force_delete",
            "data_type",
            "smallint",
        ),
        (
            "memory_vector_tombstones",
            "force_delete",
            "column_default",
            "1",
        ),
    ],
)
async def test_readiness_rejects_post_migration_column_contract_drift(
    table: str,
    column: str,
    field: str,
    drifted_value: Any,
) -> None:
    rows = _schema_rows()
    target = next(
        row
        for row in rows
        if row["metadata_kind"] == "column"
        and row["table_name"] == table
        and row["metadata_name"] == column
    )
    target[field] = drifted_value

    statuses = await inspect_mysql_memory_readiness(  # type: ignore[arg-type]
        _runtime(_Connection(rows))
    )

    assert statuses["document_index"] == StorageAvailability.FAILED


@pytest.mark.asyncio
async def test_readiness_requires_tombstone_collection_identity_column() -> None:
    rows = [
        row
        for row in _schema_rows()
        if not (
            row["table_name"] == "memory_vector_tombstones"
            and row["metadata_name"] == "collection_name"
        )
    ]

    statuses = await inspect_mysql_memory_readiness(  # type: ignore[arg-type]
        _runtime(_Connection(rows))
    )

    assert statuses["document_index"] == StorageAvailability.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "index_name",
    ["primary", "uq_memory_jobs_node_revision"],
)
@pytest.mark.parametrize("drift", ["order", "non_unique", "sequence"])
async def test_revision_identity_index_structure_drift_fails_closed(
    index_name: str,
    drift: str,
) -> None:
    rows = _schema_rows()
    target = [
        row
        for row in rows
        if row["metadata_kind"] == "index"
        and row["metadata_name"] == index_name
    ]
    assert len(target) == 2
    if drift == "order":
        target[0]["index_column_name"], target[1]["index_column_name"] = (
            target[1]["index_column_name"],
            target[0]["index_column_name"],
        )
    elif drift == "non_unique":
        target[0]["non_unique"] = 1
    else:
        target[0]["seq_in_index"] = 2

    statuses = await inspect_mysql_memory_readiness(  # type: ignore[arg-type]
        _runtime(_Connection(rows))
    )

    assert statuses["document_index"] == StorageAvailability.FAILED


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
