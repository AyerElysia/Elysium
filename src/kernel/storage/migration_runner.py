"""Versioned, checksum-verified MySQL schema migration runner."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class MigrationDriftError(RuntimeError):
    """Raised when an applied migration no longer matches its source checksum."""


class MigrationLockError(RuntimeError):
    """Raised when the schema migration advisory lock cannot be acquired."""


class MigrationPostconditionError(RuntimeError):
    """Raised when recoverable DDL does not satisfy its declared structure."""


@dataclass(frozen=True, slots=True)
class MySQLTriggerContract:
    """Expected identity and bounded behavior marker for one MySQL trigger."""

    name: str
    table: str
    manipulation: str
    timing: str
    action_fragment: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name):
            raise ValueError(f"unsafe trigger name: {self.name!r}")
        if not _IDENTIFIER.fullmatch(self.table):
            raise ValueError(f"unsafe trigger table: {self.table!r}")
        if self.manipulation.upper() not in {"INSERT", "UPDATE", "DELETE"}:
            raise ValueError("unsupported trigger manipulation")
        if self.timing.upper() not in {"BEFORE", "AFTER"}:
            raise ValueError("unsupported trigger timing")
        if not self.action_fragment:
            raise ValueError("trigger action_fragment must not be empty")


@dataclass(frozen=True, slots=True)
class SchemaMigration:
    """One ordered group of idempotent or postcondition-recoverable statements.

    ``completion_checks`` are read-only scalar SQL queries.  Every query must
    return exactly ``1`` only when the complete migration structure is already
    present.  This lets the runner adopt MySQL DDL that auto-committed before
    the checksum row was recorded, without blindly replaying a non-idempotent
    ``ALTER TABLE``.  A migration without checks retains the stricter legacy
    contract: every statement must itself be replay-safe.
    """

    version: int
    name: str
    statements: tuple[str, ...]
    completion_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if int(self.version) <= 0:
            raise ValueError("migration version must be positive")
        if not self.name.strip():
            raise ValueError("migration name must not be empty")
        if not self.statements or any(not item.strip() for item in self.statements):
            raise ValueError("migration must contain non-empty idempotent statements")
        if any(not item.strip() for item in self.completion_checks):
            raise ValueError("migration completion checks must not be empty")

    @property
    def checksum(self) -> str:
        """Return a stable checksum over version, name, and exact statements."""

        payload = "\n-- statement --\n".join(
            (str(self.version), self.name, *self.statements)
        )
        if self.completion_checks:
            payload += "\n-- completion-check --\n" + (
                "\n-- completion-check --\n".join(self.completion_checks)
            )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Secret-free result of one migration run."""

    applied_versions: tuple[int, ...]
    current_version: int


class MySQLMigrationRunner:
    """Serialize and verify one namespace's MySQL schema migrations.

    MySQL DDL may auto-commit.  Every migration statement must therefore be
    idempotent; a crash before the checksum row is recorded safely replays the
    same statements instead of pretending the migration was atomic.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        table_name: str = "storage_schema_migrations",
        lock_name: str = "elysium:life-storage-schema",
        lock_timeout_seconds: int = 10,
    ) -> None:
        if not _IDENTIFIER.fullmatch(table_name):
            raise ValueError(f"unsafe migration table name: {table_name!r}")
        if not lock_name or len(lock_name.encode("utf-8")) > 64:
            raise ValueError("MySQL advisory lock name must be 1..64 bytes")
        if int(lock_timeout_seconds) < 0:
            raise ValueError("migration lock timeout must not be negative")
        self.engine = engine
        self.table_name = table_name
        self.lock_name = lock_name
        self.lock_timeout_seconds = int(lock_timeout_seconds)

    @property
    def _create_table_sql(self) -> str:
        return f"""CREATE TABLE IF NOT EXISTS {self.table_name} (
            version BIGINT UNSIGNED PRIMARY KEY,
            name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            checksum CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""

    @staticmethod
    def _ordered(migrations: tuple[SchemaMigration, ...]) -> tuple[SchemaMigration, ...]:
        ordered = tuple(sorted(migrations, key=lambda item: item.version))
        versions = [item.version for item in ordered]
        if len(versions) != len(set(versions)):
            raise ValueError("migration versions must be unique")
        return ordered

    @staticmethod
    async def _completion_satisfied(
        connection: Any,
        migration: SchemaMigration,
    ) -> bool:
        """Return whether every declared structural postcondition is exact."""

        if not migration.completion_checks:
            return False
        results: list[bool] = []
        for statement in migration.completion_checks:
            value = await connection.scalar(text(statement))
            results.append(int(value or 0) == 1)
        if all(results):
            return True
        if any(results):
            raise MigrationPostconditionError(
                f"migration {migration.version} is only partially applied"
            )
        return False

    async def _record_migration(self, connection: Any, migration: SchemaMigration) -> None:
        await connection.execute(
            text(
                f"INSERT INTO {self.table_name} "
                "(version, name, checksum) "
                "VALUES (:version, :name, :checksum)"
            ),
            {
                "version": migration.version,
                "name": migration.name,
                "checksum": migration.checksum,
            },
        )

    async def current(self) -> dict[int, tuple[str, str]]:
        """Return applied version -> (name, checksum), creating metadata if needed."""

        async with self.engine.connect() as connection:
            await connection.execute(text(self._create_table_sql))
            await connection.commit()
            rows = (
                await connection.execute(
                    text(
                        f"SELECT version, name, checksum FROM {self.table_name} "
                        "ORDER BY version"
                    )
                )
            ).mappings()
            result = {
                int(row["version"]): (str(row["name"]), str(row["checksum"]))
                for row in rows
            }
            await connection.commit()
            return result

    async def apply(self, migrations: tuple[SchemaMigration, ...]) -> MigrationResult:
        """Apply all missing migrations under one connection-scoped advisory lock."""

        ordered = self._ordered(migrations)
        applied_now: list[int] = []
        async with self.engine.connect() as connection:
            acquired = await connection.scalar(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": self.lock_name, "timeout": self.lock_timeout_seconds},
            )
            await connection.commit()
            if int(acquired or 0) != 1:
                raise MigrationLockError(
                    f"could not acquire schema migration lock {self.lock_name!r}"
                )
            primary_error: BaseException | None = None
            try:
                await connection.execute(text(self._create_table_sql))
                await connection.commit()
                rows = (
                    await connection.execute(
                        text(
                            f"SELECT version, name, checksum FROM {self.table_name} "
                            "ORDER BY version"
                        )
                    )
                ).mappings()
                applied = {
                    int(row["version"]): (str(row["name"]), str(row["checksum"]))
                    for row in rows
                }
                await connection.commit()

                for migration in ordered:
                    existing = applied.get(migration.version)
                    if existing is not None:
                        if existing != (migration.name, migration.checksum):
                            raise MigrationDriftError(
                                f"migration {migration.version} checksum/name drift"
                            )
                        continue
                    try:
                        already_complete = await self._completion_satisfied(
                            connection,
                            migration,
                        )
                        if not already_complete:
                            for statement in migration.statements:
                                await connection.execute(text(statement))
                            if migration.completion_checks and not await self._completion_satisfied(
                                connection,
                                migration,
                            ):
                                raise MigrationPostconditionError(
                                    f"migration {migration.version} postcondition failed"
                                )
                        await self._record_migration(connection, migration)
                        await connection.commit()
                    except BaseException:
                        await connection.rollback()
                        raise
                    applied[migration.version] = (
                        migration.name,
                        migration.checksum,
                    )
                    applied_now.append(migration.version)
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                try:
                    await connection.execute(
                        text("SELECT RELEASE_LOCK(:name)"),
                        {"name": self.lock_name},
                    )
                    await connection.commit()
                except BaseException:
                    await connection.rollback()
                    if primary_error is None:
                        raise

        return MigrationResult(
            applied_versions=tuple(applied_now),
            current_version=max((item.version for item in ordered), default=0),
        )


async def verify_mysql_trigger_contract(
    engine: AsyncEngine,
    contracts: tuple[MySQLTriggerContract, ...],
) -> None:
    """Fail closed if required database-level trigger protection drifted."""

    if not contracts:
        raise ValueError("at least one trigger contract is required")
    expected = {item.name: item for item in contracts}
    if len(expected) != len(contracts):
        raise ValueError("trigger contract names must be unique")
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE,
                        EVENT_MANIPULATION, ACTION_TIMING, ACTION_STATEMENT
                    FROM information_schema.TRIGGERS
                    WHERE TRIGGER_SCHEMA = DATABASE()"""
                )
            )
        ).mappings()
        observed: dict[str, dict[str, Any]] = {
            str(row["TRIGGER_NAME"]): dict(row)
            for row in rows
            if str(row["TRIGGER_NAME"]) in expected
        }
    drifted: list[str] = []
    for name, contract in expected.items():
        row = observed.get(name)
        if row is None:
            drifted.append(name)
            continue
        action = str(row.get("ACTION_STATEMENT") or "")
        if (
            str(row.get("EVENT_OBJECT_TABLE") or "") != contract.table
            or str(row.get("EVENT_MANIPULATION") or "").upper()
            != contract.manipulation.upper()
            or str(row.get("ACTION_TIMING") or "").upper()
            != contract.timing.upper()
            or contract.action_fragment not in action
        ):
            drifted.append(name)
    if drifted:
        raise MigrationDriftError(
            "required MySQL trigger contract is missing or drifted: "
            + ", ".join(sorted(drifted))
        )


__all__ = [
    "MigrationDriftError",
    "MigrationLockError",
    "MigrationPostconditionError",
    "MigrationResult",
    "MySQLMigrationRunner",
    "MySQLTriggerContract",
    "SchemaMigration",
    "verify_mysql_trigger_contract",
]
