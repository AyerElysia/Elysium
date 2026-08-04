"""Fenced copy authority that never activates a candidate backend generation."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from src.kernel.storage import canonical_json
from src.kernel.storage.migration_runner import MySQLMigrationRunner, SchemaMigration

from ..contracts import StorageBackendRuntime, StorageWriterRole
from ..models import BackendKind


class CopyAuthorityError(RuntimeError):
    """Base class for migration-control-plane failures."""


class CopyAuthorityConflict(CopyAuthorityError):
    """Raised when a copy lease or immutable run identity conflicts."""


class StaleCopyAuthority(CopyAuthorityError):
    """Raised when a copy writer no longer owns the exact fenced lease."""


@dataclass(frozen=True, slots=True)
class CopyAuthorityToken:
    """Short-lived capability for one candidate copy run."""

    run_id: str
    authority_epoch: int
    owner_id: str
    lease_until: str
    fencing_token: str

    @property
    def fencing_token_sha256(self) -> str:
        return hashlib.sha256(self.fencing_token.encode()).hexdigest()


_COPY_CONTROL_MIGRATION = SchemaMigration(
    version=1,
    name="life_storage_copy_control_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS life_storage_copy_runs (
            run_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            source_manifest_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            source_snapshot_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            target_backend VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            writer_frozen BOOLEAN NOT NULL,
            state VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            authority_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0,
            owner_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            fencing_token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            lease_until DATETIME(6) NULL,
            copied_records BIGINT UNSIGNED NOT NULL DEFAULT 0,
            conflict_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
            metadata_json JSON NOT NULL,
            verification_json JSON NULL,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            completed_at DATETIME(6) NULL,
            KEY idx_life_storage_copy_state (state, updated_at),
            CONSTRAINT chk_life_storage_copy_state CHECK (
                state IN ('pending', 'copying', 'copied', 'verified', 'failed')
            )
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS life_storage_copy_conflicts (
            conflict_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            run_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            domain_name VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            source_identity VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            expected_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            actual_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            detail TEXT NOT NULL,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            KEY idx_life_storage_copy_conflict_run (run_id, conflict_id),
            CONSTRAINT fk_life_storage_copy_conflict_run
                FOREIGN KEY (run_id) REFERENCES life_storage_copy_runs(run_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)


def _iso(value: Any) -> str:
    if not isinstance(value, datetime):
        raise CopyAuthorityError("MySQL did not return a copy-control timestamp")
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise CopyAuthorityError("copy-control JSON must be an object")
    return decoded


class MySQLCopyAuthorityRegistry:
    """Coordinate an isolated candidate copy without changing active authority."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def ensure_schema(self) -> None:
        """Install only migration-control-plane tables."""

        runner = MySQLMigrationRunner(
            self.engine,
            table_name="life_storage_copy_schema_migrations",
            lock_name="elysium:life-storage-copy-control",
        )
        await runner.apply((_COPY_CONTROL_MIGRATION,))

    async def create_run(
        self,
        *,
        run_id: str,
        source_manifest_sha256: str,
        source_snapshot_sha256: str,
        writer_frozen: bool,
        target_backend: str = "mysql",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an idempotent immutable copy identity."""

        identity = str(run_id).strip()
        if not identity:
            raise ValueError("copy run_id must not be empty")
        if len(source_manifest_sha256) != 64 or len(source_snapshot_sha256) != 64:
            raise ValueError("copy source hashes must be SHA-256 hex strings")
        target = str(target_backend).strip().lower()
        if target not in {"local", "mysql"}:
            raise ValueError("copy target_backend must be local or mysql")
        await self.ensure_schema()
        encoded_metadata = canonical_json(metadata or {})
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT IGNORE INTO life_storage_copy_runs (
                        run_id, source_manifest_sha256, source_snapshot_sha256,
                        target_backend, writer_frozen, state, owner_id,
                        fencing_token_hash, metadata_json
                    ) VALUES (
                        :run_id, :manifest_hash, :snapshot_hash, :target_backend,
                        :writer_frozen, 'pending', '', '', :metadata_json
                    )"""
                ),
                {
                    "run_id": identity,
                    "manifest_hash": source_manifest_sha256,
                    "snapshot_hash": source_snapshot_sha256,
                    "target_backend": target,
                    "writer_frozen": bool(writer_frozen),
                    "metadata_json": encoded_metadata,
                },
            )
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM life_storage_copy_runs "
                            "WHERE run_id = :run_id FOR UPDATE"
                        ),
                        {"run_id": identity},
                    )
                )
                .mappings()
                .one()
            )
            expected = {
                "source_manifest_sha256": source_manifest_sha256,
                "source_snapshot_sha256": source_snapshot_sha256,
                "target_backend": target,
                "writer_frozen": bool(writer_frozen),
                "metadata_json": metadata or {},
            }
            actual = {
                "source_manifest_sha256": str(row["source_manifest_sha256"]),
                "source_snapshot_sha256": str(row["source_snapshot_sha256"]),
                "target_backend": str(row["target_backend"]),
                "writer_frozen": bool(row["writer_frozen"]),
                "metadata_json": _json_object(row["metadata_json"]),
            }
            if actual != expected:
                raise CopyAuthorityConflict(f"copy run identity conflict: {identity}")
        return await self.get_run(identity)

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """Return secret-free copy state."""

        await self.ensure_schema()
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT run_id, source_manifest_sha256,
                            source_snapshot_sha256, target_backend, writer_frozen,
                            state, authority_epoch, owner_id, lease_until,
                            copied_records, conflict_count, metadata_json,
                            verification_json, created_at, updated_at, completed_at
                            FROM life_storage_copy_runs WHERE run_id = :run_id"""
                        ),
                        {"run_id": str(run_id)},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise CopyAuthorityError(f"copy run does not exist: {run_id}")
        return {
            "run_id": str(row["run_id"]),
            "source_manifest_sha256": str(row["source_manifest_sha256"]),
            "source_snapshot_sha256": str(row["source_snapshot_sha256"]),
            "target_backend": str(row["target_backend"]),
            "writer_frozen": bool(row["writer_frozen"]),
            "state": str(row["state"]),
            "authority_epoch": int(row["authority_epoch"]),
            "owner_id": str(row["owner_id"] or ""),
            "lease_until": _iso(row["lease_until"]) if row["lease_until"] else "",
            "copied_records": int(row["copied_records"]),
            "conflict_count": int(row["conflict_count"]),
            "metadata": _json_object(row["metadata_json"]),
            "verification": _json_object(row["verification_json"]),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "completed_at": _iso(row["completed_at"]) if row["completed_at"] else "",
        }

    async def acquire(
        self,
        run_id: str,
        *,
        expected_epoch: int,
        owner_id: str,
        lease_seconds: int,
    ) -> CopyAuthorityToken:
        """Acquire only an expired or inactive candidate-copy lease."""

        if int(lease_seconds) <= 0:
            raise ValueError("copy lease_seconds must be positive")
        owner = str(owner_id).strip()
        if not owner:
            raise ValueError("copy owner_id must not be empty")
        secret = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(secret.encode()).hexdigest()
        await self.ensure_schema()
        async with self.engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT *, CURRENT_TIMESTAMP(6) AS database_now
                            FROM life_storage_copy_runs
                            WHERE run_id = :run_id FOR UPDATE"""
                        ),
                        {"run_id": str(run_id)},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise CopyAuthorityError(f"copy run does not exist: {run_id}")
            if str(row["state"]) in {"copied", "verified"}:
                raise CopyAuthorityConflict("completed copy run is immutable")
            if int(row["authority_epoch"]) != int(expected_epoch):
                raise CopyAuthorityConflict(
                    "copy authority epoch changed before acquisition"
                )
            if (
                row["lease_until"] is not None
                and row["lease_until"] > row["database_now"]
            ):
                raise CopyAuthorityConflict("copy run already has a live writer")
            next_epoch = int(row["authority_epoch"]) + 1
            await connection.execute(
                text(
                    """UPDATE life_storage_copy_runs SET
                        state = 'copying', authority_epoch = :authority_epoch,
                        owner_id = :owner_id, fencing_token_hash = :token_hash,
                        lease_until = TIMESTAMPADD(
                            SECOND, :lease_seconds, CURRENT_TIMESTAMP(6)
                        ), updated_at = CURRENT_TIMESTAMP(6), completed_at = NULL
                    WHERE run_id = :run_id"""
                ),
                {
                    "authority_epoch": next_epoch,
                    "owner_id": owner,
                    "token_hash": token_hash,
                    "lease_seconds": int(lease_seconds),
                    "run_id": str(run_id),
                },
            )
            lease_until = await connection.scalar(
                text(
                    "SELECT lease_until FROM life_storage_copy_runs "
                    "WHERE run_id = :run_id"
                ),
                {"run_id": str(run_id)},
            )
        return CopyAuthorityToken(
            run_id=str(run_id),
            authority_epoch=next_epoch,
            owner_id=owner,
            lease_until=_iso(lease_until),
            fencing_token=secret,
        )

    async def validate_in_transaction(
        self,
        connection: AsyncConnection,
        token: CopyAuthorityToken,
        *,
        for_update: bool = False,
    ) -> None:
        """Fence candidate writes inside the exact mutating transaction."""

        suffix = " FOR UPDATE" if for_update else " FOR SHARE"
        row = (
            (
                await connection.execute(
                    text(
                        """SELECT state, authority_epoch, owner_id,
                        fencing_token_hash, lease_until,
                        CURRENT_TIMESTAMP(6) AS database_now
                        FROM life_storage_copy_runs WHERE run_id = :run_id"""
                        + suffix
                    ),
                    {"run_id": token.run_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        checks = row is not None and all(
            (
                str(row["state"]) == "copying",
                int(row["authority_epoch"]) == token.authority_epoch,
                str(row["owner_id"]) == token.owner_id,
                secrets.compare_digest(
                    str(row["fencing_token_hash"]),
                    token.fencing_token_sha256,
                ),
                row["lease_until"] is not None
                and row["lease_until"] > row["database_now"],
            )
        )
        if not checks:
            raise StaleCopyAuthority("candidate copy writer was fenced")

    async def validate(self, token: CopyAuthorityToken) -> None:
        """Validate one copy token outside a domain transaction."""

        await self.ensure_schema()
        async with self.engine.begin() as connection:
            await self.validate_in_transaction(connection, token)

    async def reconcile_expired_runs(self) -> int:
        """Fail only leases already expired by database time, retaining evidence."""

        await self.ensure_schema()
        async with self.engine.begin() as connection:
            result = await connection.execute(
                text(
                    """UPDATE life_storage_copy_runs SET
                    state = 'failed', owner_id = '', fencing_token_hash = '',
                    lease_until = NULL,
                    verification_json = :verification_json,
                    updated_at = CURRENT_TIMESTAMP(6)
                    WHERE state = 'copying'
                      AND lease_until IS NOT NULL
                      AND lease_until <= CURRENT_TIMESTAMP(6)"""
                ),
                {
                    "verification_json": canonical_json(
                        {"reason": "copy lease expired before completion"}
                    )
                },
            )
        return int(result.rowcount or 0)

    async def renew(
        self,
        token: CopyAuthorityToken,
        *,
        lease_seconds: int,
    ) -> CopyAuthorityToken:
        """Renew a live copy lease using database time."""

        if int(lease_seconds) <= 0:
            raise ValueError("copy lease_seconds must be positive")
        async with self.engine.begin() as connection:
            await self.validate_in_transaction(connection, token, for_update=True)
            await connection.execute(
                text(
                    """UPDATE life_storage_copy_runs SET
                    lease_until = TIMESTAMPADD(
                        SECOND, :lease_seconds, CURRENT_TIMESTAMP(6)
                    ), updated_at = CURRENT_TIMESTAMP(6)
                    WHERE run_id = :run_id"""
                ),
                {"lease_seconds": int(lease_seconds), "run_id": token.run_id},
            )
            lease_until = await connection.scalar(
                text(
                    "SELECT lease_until FROM life_storage_copy_runs "
                    "WHERE run_id = :run_id"
                ),
                {"run_id": token.run_id},
            )
        return CopyAuthorityToken(
            run_id=token.run_id,
            authority_epoch=token.authority_epoch,
            owner_id=token.owner_id,
            lease_until=_iso(lease_until),
            fencing_token=token.fencing_token,
        )

    async def set_progress(
        self,
        token: CopyAuthorityToken,
        *,
        copied_records: int,
    ) -> None:
        """Set an absolute monotonic count so crash retries cannot double-count."""

        if int(copied_records) < 0:
            raise ValueError("copied_records must not be negative")
        async with self.engine.begin() as connection:
            await self.validate_in_transaction(connection, token, for_update=True)
            await connection.execute(
                text(
                    """UPDATE life_storage_copy_runs SET
                    copied_records = GREATEST(copied_records, :copied_records),
                    updated_at = CURRENT_TIMESTAMP(6)
                    WHERE run_id = :run_id"""
                ),
                {"copied_records": int(copied_records), "run_id": token.run_id},
            )

    async def add_progress(
        self,
        token: CopyAuthorityToken,
        *,
        copied_records: int,
    ) -> None:
        """Backward-compatible alias for absolute monotonic progress."""

        await self.set_progress(token, copied_records=copied_records)

    async def record_conflict(
        self,
        token: CopyAuthorityToken,
        *,
        domain_name: str,
        source_identity: str,
        expected_hash: str,
        actual_hash: str,
        detail: str,
    ) -> None:
        """Persist append-only conflict evidence and block verification."""

        async with self.engine.begin() as connection:
            await self.validate_in_transaction(connection, token, for_update=True)
            await connection.execute(
                text(
                    """INSERT INTO life_storage_copy_conflicts (
                        run_id, domain_name, source_identity,
                        expected_hash, actual_hash, detail
                    ) VALUES (
                        :run_id, :domain_name, :source_identity,
                        :expected_hash, :actual_hash, :detail
                    )"""
                ),
                {
                    "run_id": token.run_id,
                    "domain_name": str(domain_name)[:128],
                    "source_identity": str(source_identity)[:1024],
                    "expected_hash": str(expected_hash)[:64],
                    "actual_hash": str(actual_hash)[:64],
                    "detail": str(detail)[:65535],
                },
            )
            await connection.execute(
                text(
                    """UPDATE life_storage_copy_runs SET
                    conflict_count = conflict_count + 1,
                    updated_at = CURRENT_TIMESTAMP(6)
                    WHERE run_id = :run_id"""
                ),
                {"run_id": token.run_id},
            )

    async def complete(
        self,
        token: CopyAuthorityToken,
        *,
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        """Seal a copy, marking verified only from frozen conflict-free evidence."""

        async with self.engine.begin() as connection:
            await self.validate_in_transaction(connection, token, for_update=True)
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT writer_frozen, conflict_count
                            FROM life_storage_copy_runs
                            WHERE run_id = :run_id FOR UPDATE"""
                        ),
                        {"run_id": token.run_id},
                    )
                )
                .mappings()
                .one()
            )
            verified = (
                bool(verification.get("verified"))
                and bool(row["writer_frozen"])
                and int(row["conflict_count"]) == 0
            )
            state = "verified" if verified else "copied"
            await connection.execute(
                text(
                    """UPDATE life_storage_copy_runs SET
                    state = :state, owner_id = '', fencing_token_hash = '',
                    lease_until = NULL, verification_json = :verification_json,
                    completed_at = CURRENT_TIMESTAMP(6),
                    updated_at = CURRENT_TIMESTAMP(6)
                    WHERE run_id = :run_id"""
                ),
                {
                    "state": state,
                    "verification_json": canonical_json(verification),
                    "run_id": token.run_id,
                },
            )
        return await self.get_run(token.run_id)

    async def fail(self, token: CopyAuthorityToken, *, reason: str) -> None:
        """Release a failed writer while retaining all copied rows and evidence."""

        async with self.engine.begin() as connection:
            await self.validate_in_transaction(connection, token, for_update=True)
            await connection.execute(
                text(
                    """UPDATE life_storage_copy_runs SET
                    state = 'failed', owner_id = '', fencing_token_hash = '',
                    lease_until = NULL,
                    verification_json = :verification_json,
                    updated_at = CURRENT_TIMESTAMP(6)
                    WHERE run_id = :run_id"""
                ),
                {
                    "verification_json": canonical_json({"reason": str(reason)}),
                    "run_id": token.run_id,
                },
            )


def open_mysql_copy_runtime(
    registry: MySQLCopyAuthorityRegistry,
    token: CopyAuthorityToken,
    *,
    backend_identity: str,
) -> StorageBackendRuntime:
    """Build a fenced candidate runtime without active-generation authority."""

    session_factory = async_sessionmaker(registry.engine, expire_on_commit=False)

    async def write_fence(session: Any) -> None:
        connection = await session.connection()
        await registry.validate_in_transaction(connection, token)

    async def validate_writer() -> None:
        await registry.validate(token)

    return StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.MYSQL,
        backend_identity=backend_identity,
        generation=None,
        authority_registry=None,
        authority_token=None,
        engine=registry.engine,
        session_factory=session_factory,
        _write_fence=write_fence,
        _writer_validator=validate_writer,
        writer_role=StorageWriterRole.CANDIDATE_COPY,
    )


__all__ = [
    "CopyAuthorityConflict",
    "CopyAuthorityError",
    "CopyAuthorityToken",
    "MySQLCopyAuthorityRegistry",
    "StaleCopyAuthority",
    "open_mysql_copy_runtime",
]
