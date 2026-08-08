"""Generation-scoped singleton writer claims for shared storage runtimes.

The active MySQL generation intentionally allows several processes to serve
independent domains.  A singleton technical state still needs exactly one
writer, so this module adds a smaller database-time lease scoped by
``(generation_id, namespace, state_key)``.  Every protected write validates
the lease in the same transaction that commits the domain mutation.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage.migration_runner import (
    MySQLMigrationRunner,
    MySQLTriggerContract,
    SchemaMigration,
    verify_mysql_trigger_contract,
)

from .models import BackendKind

_MAX_NAMESPACE_CHARS = 128
_MAX_STATE_KEY_CHARS = 255
_MAX_OWNER_CHARS = 255
_MIN_LEASE_SECONDS = 5
_MAX_LEASE_SECONDS = 3600


class SingletonWriterClaimError(RuntimeError):
    """Base class for singleton writer claim failures."""


class SingletonWriterClaimConflict(SingletonWriterClaimError):
    """Raised when another live owner already holds the singleton key."""


class SingletonWriterClaimLost(SingletonWriterClaimError):
    """Raised when the exact epoch/token can no longer be proven live."""


@dataclass(frozen=True, slots=True)
class SingletonWriterClaim:
    """One opaque, database-time writer lease.

    ``fencing_token`` is intentionally excluded from repr so diagnostics can
    identify the writer without disclosing the bearer secret.
    """

    generation_id: str
    namespace: str
    state_key: str
    owner_instance_id: str
    lease_epoch: int
    lease_until: str
    fencing_token: str = field(repr=False)


@runtime_checkable
class SingletonWriterClaimPort(Protocol):
    """Backend-neutral database-time singleton writer lease contract."""

    async def acquire(
        self,
        *,
        namespace: str,
        state_key: str,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SingletonWriterClaim:
        """Acquire an absent/expired key or reject a different live owner."""

    async def renew(
        self,
        claim: SingletonWriterClaim,
        *,
        lease_seconds: int,
    ) -> SingletonWriterClaim:
        """Renew only the exact live epoch/token using database time."""

    async def release(self, claim: SingletonWriterClaim) -> bool:
        """Release only the exact epoch/token; stale release is a no-op."""

    async def validate_in_transaction(
        self,
        session: AsyncSession,
        claim: SingletonWriterClaim,
    ) -> None:
        """Lock and validate the exact live claim before domain commit."""

    async def bind_runtime_state_write(
        self,
        session: AsyncSession,
        claim: SingletonWriterClaim,
    ) -> None:
        """Bind a MySQL connection for database-trigger guarded state write."""

    async def prepare_runtime_state_write(
        self,
        session: AsyncSession,
        *,
        namespace: str,
        state_key: str,
        claim: SingletonWriterClaim | None,
    ) -> None:
        """Require a claim once a singleton key has ever been registered."""

    async def clear_runtime_state_write(self, session: AsyncSession) -> None:
        """Remove the transaction-local MySQL trigger binding."""

    async def health_snapshot(self) -> dict[str, Any]:
        """Return content-free claim counts and expiry diagnostics."""


def _identity(value: Any, *, field_name: str, maximum: int) -> str:
    identity = str(value or "").strip()
    if not identity or len(identity) > maximum:
        raise ValueError(f"{field_name} must be 1..{maximum} characters")
    return identity


def _lease_seconds(value: Any) -> int:
    seconds = int(value)
    if not _MIN_LEASE_SECONDS <= seconds <= _MAX_LEASE_SECONDS:
        raise ValueError(
            f"lease_seconds must be {_MIN_LEASE_SECONDS}..{_MAX_LEASE_SECONDS}"
        )
    return seconds


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: Any) -> str:
    return _parse_datetime(value).isoformat()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


LOCAL_SINGLETON_WRITER_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS runtime_singleton_writer_claims (
        generation_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        state_key TEXT NOT NULL,
        owner_instance_id TEXT NOT NULL,
        lease_epoch INTEGER NOT NULL,
        fencing_token_sha256 TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL,
        lease_until TEXT NOT NULL,
        released_at TEXT,
        PRIMARY KEY (generation_id, namespace, state_key)
    )""",
    """CREATE TABLE IF NOT EXISTS runtime_singleton_writer_events (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        generation_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        state_key TEXT NOT NULL,
        owner_instance_id TEXT NOT NULL,
        lease_epoch INTEGER NOT NULL,
        event_kind TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_runtime_writer_events_key_position
        ON runtime_singleton_writer_events(
            generation_id, namespace, state_key, position
        )""",
    """CREATE TRIGGER IF NOT EXISTS runtime_writer_events_immutable_update_v1
        BEFORE UPDATE ON runtime_singleton_writer_events BEGIN
            SELECT RAISE(ABORT, 'RuntimeWriterEventImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS runtime_writer_events_immutable_delete_v1
        BEFORE DELETE ON runtime_singleton_writer_events BEGIN
            SELECT RAISE(ABORT, 'RuntimeWriterEventImmutable');
        END""",
)

MYSQL_SINGLETON_WRITER_MIGRATION = SchemaMigration(
    version=1,
    name="life_singleton_writer_claim_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS runtime_singleton_writer_claims (
            generation_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            namespace VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            state_key VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            owner_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            lease_epoch BIGINT UNSIGNED NOT NULL,
            fencing_token_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            acquired_at DATETIME(6) NOT NULL,
            renewed_at DATETIME(6) NOT NULL,
            lease_until DATETIME(6) NOT NULL,
            released_at DATETIME(6) NULL,
            PRIMARY KEY (generation_id, namespace, state_key),
            KEY idx_runtime_writer_claim_expiry (lease_until)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS runtime_singleton_writer_events (
            position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            generation_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            namespace VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            state_key VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            owner_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            lease_epoch BIGINT UNSIGNED NOT NULL,
            event_kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            occurred_at DATETIME(6) NOT NULL,
            KEY idx_runtime_writer_events_key_position (
                generation_id, namespace, state_key, position
            )
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS runtime_singleton_writer_bindings (
            connection_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
            generation_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            namespace VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            state_key VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            owner_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            lease_epoch BIGINT UNSIGNED NOT NULL,
            fencing_token_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            bound_at DATETIME(6) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TRIGGER IF NOT EXISTS runtime_writer_events_immutable_update_v1
        BEFORE UPDATE ON runtime_singleton_writer_events FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'RuntimeWriterEventImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS runtime_writer_events_immutable_delete_v1
        BEFORE DELETE ON runtime_singleton_writer_events FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'RuntimeWriterEventImmutable'""",
    ),
)

MYSQL_SINGLETON_WRITER_EVENT_TRIGGERS = (
    MySQLTriggerContract(
        "runtime_writer_events_immutable_update_v1",
        "runtime_singleton_writer_events",
        "UPDATE",
        "BEFORE",
        "RuntimeWriterEventImmutable",
    ),
    MySQLTriggerContract(
        "runtime_writer_events_immutable_delete_v1",
        "runtime_singleton_writer_events",
        "DELETE",
        "BEFORE",
        "RuntimeWriterEventImmutable",
    ),
)


class SQLSingletonWriterClaimStore:
    """SQL implementation shared by local and MySQL selected runtimes."""

    def __init__(self, runtime: Any) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("singleton writer claims require enabled storage")
        if runtime.generation is None:
            raise RuntimeError("singleton writer claims require a generation")
        self.runtime = runtime
        self.backend = runtime.backend
        self.generation_id = runtime.generation.generation_id

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    def _bind_time(self, value: datetime) -> datetime | str:
        if self.backend == BackendKind.MYSQL:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value.astimezone(UTC).isoformat()

    async def _database_now(self, session: AsyncSession) -> datetime:
        if self.backend == BackendKind.MYSQL:
            value = await session.scalar(text("SELECT CURRENT_TIMESTAMP(6)"))
        else:
            value = await session.scalar(
                text("SELECT STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')")
            )
        return _parse_datetime(value)

    async def _locked_row(
        self,
        session: AsyncSession,
        namespace: str,
        state_key: str,
    ) -> Any | None:
        return (
            (
                await session.execute(
                    text(
                        """SELECT generation_id, namespace, state_key,
                        owner_instance_id, lease_epoch, fencing_token_sha256,
                        acquired_at, renewed_at, lease_until, released_at
                    FROM runtime_singleton_writer_claims
                    WHERE generation_id = :generation_id
                        AND namespace = :namespace AND state_key = :state_key"""
                        + self._for_update
                    ),
                    {
                        "generation_id": self.generation_id,
                        "namespace": namespace,
                        "state_key": state_key,
                    },
                )
            )
            .mappings()
            .first()
        )

    @staticmethod
    async def _append_event(
        session: AsyncSession,
        *,
        claim: SingletonWriterClaim,
        event_kind: str,
        occurred_at: datetime | str,
    ) -> None:
        await session.execute(
            text(
                """INSERT INTO runtime_singleton_writer_events (
                    generation_id, namespace, state_key, owner_instance_id,
                    lease_epoch, event_kind, occurred_at
                ) VALUES (
                    :generation_id, :namespace, :state_key, :owner_instance_id,
                    :lease_epoch, :event_kind, :occurred_at
                )"""
            ),
            {
                "generation_id": claim.generation_id,
                "namespace": claim.namespace,
                "state_key": claim.state_key,
                "owner_instance_id": claim.owner_instance_id,
                "lease_epoch": claim.lease_epoch,
                "event_kind": event_kind,
                "occurred_at": occurred_at,
            },
        )

    async def acquire(
        self,
        *,
        namespace: str,
        state_key: str,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SingletonWriterClaim:
        namespace = _identity(
            namespace, field_name="namespace", maximum=_MAX_NAMESPACE_CHARS
        )
        state_key = _identity(
            state_key, field_name="state_key", maximum=_MAX_STATE_KEY_CHARS
        )
        owner_instance_id = _identity(
            owner_instance_id,
            field_name="owner_instance_id",
            maximum=_MAX_OWNER_CHARS,
        )
        seconds = _lease_seconds(lease_seconds)
        token = secrets.token_urlsafe(32)
        token_sha256 = _token_digest(token)

        async with self.runtime.unit_of_work() as uow:
            session = uow.session
            now = await self._database_now(session)
            row = await self._locked_row(session, namespace, state_key)
            if row is not None:
                live = (
                    row["released_at"] is None
                    and _parse_datetime(row["lease_until"]) > now
                )
                if live:
                    raise SingletonWriterClaimConflict(
                        "SingletonWriterAlreadyClaimed:"
                        f"{namespace}:{state_key}:owner={row['owner_instance_id']}:"
                        f"epoch={int(row['lease_epoch'])}"
                    )
                lease_epoch = int(row["lease_epoch"]) + 1
            else:
                lease_epoch = 1
            lease_until = now + timedelta(seconds=seconds)
            parameters = {
                "generation_id": self.generation_id,
                "namespace": namespace,
                "state_key": state_key,
                "owner_instance_id": owner_instance_id,
                "lease_epoch": lease_epoch,
                "fencing_token_sha256": token_sha256,
                "acquired_at": self._bind_time(now),
                "renewed_at": self._bind_time(now),
                "lease_until": self._bind_time(lease_until),
            }
            if row is None:
                await session.execute(
                    text(
                        """INSERT INTO runtime_singleton_writer_claims (
                            generation_id, namespace, state_key,
                            owner_instance_id, lease_epoch,
                            fencing_token_sha256, acquired_at, renewed_at,
                            lease_until, released_at
                        ) VALUES (
                            :generation_id, :namespace, :state_key,
                            :owner_instance_id, :lease_epoch,
                            :fencing_token_sha256, :acquired_at, :renewed_at,
                            :lease_until, NULL
                        )"""
                    ),
                    parameters,
                )
                event_kind = "acquired"
            else:
                await session.execute(
                    text(
                        """UPDATE runtime_singleton_writer_claims SET
                            owner_instance_id = :owner_instance_id,
                            lease_epoch = :lease_epoch,
                            fencing_token_sha256 = :fencing_token_sha256,
                            acquired_at = :acquired_at,
                            renewed_at = :renewed_at,
                            lease_until = :lease_until,
                            released_at = NULL
                        WHERE generation_id = :generation_id
                            AND namespace = :namespace AND state_key = :state_key"""
                    ),
                    parameters,
                )
                event_kind = "expired_takeover"
            claim = SingletonWriterClaim(
                generation_id=self.generation_id,
                namespace=namespace,
                state_key=state_key,
                owner_instance_id=owner_instance_id,
                lease_epoch=lease_epoch,
                lease_until=lease_until.isoformat(),
                fencing_token=token,
            )
            await self._append_event(
                session,
                claim=claim,
                event_kind=event_kind,
                occurred_at=self._bind_time(now),
            )
        return claim

    async def _validate_locked(
        self,
        session: AsyncSession,
        claim: SingletonWriterClaim,
    ) -> datetime:
        if claim.generation_id != self.generation_id:
            raise SingletonWriterClaimLost("SingletonWriterGenerationMismatch")
        row = await self._locked_row(session, claim.namespace, claim.state_key)
        now = await self._database_now(session)
        matches = (
            row is not None
            and row["released_at"] is None
            and str(row["owner_instance_id"]) == claim.owner_instance_id
            and int(row["lease_epoch"]) == int(claim.lease_epoch)
            and str(row["fencing_token_sha256"]) == _token_digest(claim.fencing_token)
            and _parse_datetime(row["lease_until"]) > now
        )
        if not matches:
            actual_owner = str(row["owner_instance_id"]) if row is not None else ""
            actual_epoch = int(row["lease_epoch"]) if row is not None else 0
            raise SingletonWriterClaimLost(
                "SingletonWriterClaimLost:"
                f"{claim.namespace}:{claim.state_key}:"
                f"owner={actual_owner}:epoch={actual_epoch}"
            )
        return now

    async def validate_in_transaction(
        self,
        session: AsyncSession,
        claim: SingletonWriterClaim,
    ) -> None:
        await self._validate_locked(session, claim)

    async def renew(
        self,
        claim: SingletonWriterClaim,
        *,
        lease_seconds: int,
    ) -> SingletonWriterClaim:
        seconds = _lease_seconds(lease_seconds)
        async with self.runtime.unit_of_work() as uow:
            session = uow.session
            now = await self._validate_locked(session, claim)
            lease_until = now + timedelta(seconds=seconds)
            result = await session.execute(
                text(
                    """UPDATE runtime_singleton_writer_claims SET
                        renewed_at = :renewed_at, lease_until = :lease_until
                    WHERE generation_id = :generation_id
                        AND namespace = :namespace AND state_key = :state_key
                        AND owner_instance_id = :owner_instance_id
                        AND lease_epoch = :lease_epoch
                        AND fencing_token_sha256 = :fencing_token_sha256
                        AND released_at IS NULL"""
                ),
                {
                    "generation_id": claim.generation_id,
                    "namespace": claim.namespace,
                    "state_key": claim.state_key,
                    "owner_instance_id": claim.owner_instance_id,
                    "lease_epoch": claim.lease_epoch,
                    "fencing_token_sha256": _token_digest(claim.fencing_token),
                    "renewed_at": self._bind_time(now),
                    "lease_until": self._bind_time(lease_until),
                },
            )
            if result.rowcount != 1:
                raise SingletonWriterClaimLost("SingletonWriterRenewalLost")
            renewed = SingletonWriterClaim(
                generation_id=claim.generation_id,
                namespace=claim.namespace,
                state_key=claim.state_key,
                owner_instance_id=claim.owner_instance_id,
                lease_epoch=claim.lease_epoch,
                lease_until=lease_until.isoformat(),
                fencing_token=claim.fencing_token,
            )
            await self._append_event(
                session,
                claim=renewed,
                event_kind="renewed",
                occurred_at=self._bind_time(now),
            )
        return renewed

    async def release(self, claim: SingletonWriterClaim) -> bool:
        async with self.runtime.unit_of_work() as uow:
            session = uow.session
            row = await self._locked_row(session, claim.namespace, claim.state_key)
            if row is None or (
                str(row["owner_instance_id"]) != claim.owner_instance_id
                or int(row["lease_epoch"]) != int(claim.lease_epoch)
                or str(row["fencing_token_sha256"])
                != _token_digest(claim.fencing_token)
                or row["released_at"] is not None
            ):
                return False
            now = await self._database_now(session)
            result = await session.execute(
                text(
                    """UPDATE runtime_singleton_writer_claims SET
                        lease_until = :now, released_at = :now
                    WHERE generation_id = :generation_id
                        AND namespace = :namespace AND state_key = :state_key
                        AND owner_instance_id = :owner_instance_id
                        AND lease_epoch = :lease_epoch
                        AND fencing_token_sha256 = :fencing_token_sha256
                        AND released_at IS NULL"""
                ),
                {
                    "generation_id": claim.generation_id,
                    "namespace": claim.namespace,
                    "state_key": claim.state_key,
                    "owner_instance_id": claim.owner_instance_id,
                    "lease_epoch": claim.lease_epoch,
                    "fencing_token_sha256": _token_digest(claim.fencing_token),
                    "now": self._bind_time(now),
                },
            )
            if result.rowcount != 1:
                return False
            await self._append_event(
                session,
                claim=claim,
                event_kind="released",
                occurred_at=self._bind_time(now),
            )
        return True

    async def bind_runtime_state_write(
        self,
        session: AsyncSession,
        claim: SingletonWriterClaim,
    ) -> None:
        await self._validate_locked(session, claim)
        if self.backend != BackendKind.MYSQL:
            return
        connection_id = int(await session.scalar(text("SELECT CONNECTION_ID()")) or 0)
        await session.execute(
            text(
                """DELETE FROM runtime_singleton_writer_bindings
                WHERE connection_id = :connection_id"""
            ),
            {"connection_id": connection_id},
        )
        await session.execute(
            text(
                """INSERT INTO runtime_singleton_writer_bindings (
                    connection_id, generation_id, namespace, state_key,
                    owner_instance_id, lease_epoch, fencing_token_sha256,
                    bound_at
                ) VALUES (
                    :connection_id, :generation_id, :namespace, :state_key,
                    :owner_instance_id, :lease_epoch, :fencing_token_sha256,
                    CURRENT_TIMESTAMP(6)
                )"""
            ),
            {
                "connection_id": connection_id,
                "generation_id": claim.generation_id,
                "namespace": claim.namespace,
                "state_key": claim.state_key,
                "owner_instance_id": claim.owner_instance_id,
                "lease_epoch": claim.lease_epoch,
                "fencing_token_sha256": _token_digest(claim.fencing_token),
            },
        )

    async def prepare_runtime_state_write(
        self,
        session: AsyncSession,
        *,
        namespace: str,
        state_key: str,
        claim: SingletonWriterClaim | None,
    ) -> None:
        if claim is not None:
            await self.bind_runtime_state_write(session, claim)
            return
        # Multi-writer generations retired the generation-scoped singleton
        # claim for runtime context (spec 5.2 / 16.2).  Concurrent nodes write
        # the shared technical state through typed deltas or CAS without a
        # global claim, so the legacy "registered key must be claimed" rule
        # must not apply on a shared-writer runtime.
        if bool(getattr(self.runtime, "shared_writers", False)):
            await self.clear_runtime_state_write(session)
            return
        registered = await session.scalar(
            text(
                """SELECT 1 FROM runtime_singleton_writer_claims
                WHERE generation_id = :generation_id
                    AND namespace = :namespace AND state_key = :state_key
                LIMIT 1"""
            ),
            {
                "generation_id": self.generation_id,
                "namespace": namespace,
                "state_key": state_key,
            },
        )
        await self.clear_runtime_state_write(session)
        if registered is not None:
            raise SingletonWriterClaimLost(
                f"SingletonWriterClaimRequired:{namespace}:{state_key}"
            )

    async def clear_runtime_state_write(self, session: AsyncSession) -> None:
        if self.backend != BackendKind.MYSQL:
            return
        connection_id = int(await session.scalar(text("SELECT CONNECTION_ID()")) or 0)
        await session.execute(
            text(
                """DELETE FROM runtime_singleton_writer_bindings
                WHERE connection_id = :connection_id"""
            ),
            {"connection_id": connection_id},
        )

    async def health_snapshot(self) -> dict[str, Any]:
        async with self.runtime.unit_of_work() as uow:
            session = uow.session
            now = await self._database_now(session)
            total = int(
                await session.scalar(
                    text(
                        """SELECT COUNT(*) FROM runtime_singleton_writer_claims
                        WHERE generation_id = :generation_id"""
                    ),
                    {"generation_id": self.generation_id},
                )
                or 0
            )
            live = int(
                await session.scalar(
                    text(
                        """SELECT COUNT(*) FROM runtime_singleton_writer_claims
                        WHERE generation_id = :generation_id
                            AND released_at IS NULL AND lease_until > :now"""
                    ),
                    {
                        "generation_id": self.generation_id,
                        "now": self._bind_time(now),
                    },
                )
                or 0
            )
        return {
            "status": "healthy",
            "backend": self.backend.value,
            "generation_id": self.generation_id,
            "known_claim_count": total,
            "live_claim_count": live,
        }


async def ensure_singleton_writer_claim_schema(runtime: Any) -> None:
    """Create the generic writer-claim tables under fenced authority."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("singleton writer schema requires enabled storage")
    await runtime.validate_writer()
    if runtime.backend == BackendKind.MYSQL:
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name="life_singleton_writer_schema_migrations",
            lock_name="elysium:life-singleton-writer-schema",
        )
        await runner.apply((MYSQL_SINGLETON_WRITER_MIGRATION,))
        await verify_mysql_trigger_contract(
            runtime.engine,
            MYSQL_SINGLETON_WRITER_EVENT_TRIGGERS,
        )
    else:
        async with runtime.unit_of_work() as uow:
            for statement in LOCAL_SINGLETON_WRITER_SCHEMA_STATEMENTS:
                await uow.session.execute(text(statement))
    await runtime.validate_writer()


__all__ = [
    "LOCAL_SINGLETON_WRITER_SCHEMA_STATEMENTS",
    "MYSQL_SINGLETON_WRITER_EVENT_TRIGGERS",
    "MYSQL_SINGLETON_WRITER_MIGRATION",
    "SQLSingletonWriterClaimStore",
    "SingletonWriterClaim",
    "SingletonWriterClaimConflict",
    "SingletonWriterClaimError",
    "SingletonWriterClaimLost",
    "SingletonWriterClaimPort",
    "ensure_singleton_writer_claim_schema",
]
