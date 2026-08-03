"""MySQL 8 store for the owner-authorized unified memory archive."""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .models import (
    ARCHIVE_SCHEMA_VERSION,
    ArchiveMode,
    ArchivePublishResult,
    ArchiveRecord,
    canonical_json,
)


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


@dataclass(frozen=True, slots=True)
class MySQLArchiveConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    ssl_mode: str = "disabled"
    ssl_ca: str = ""
    ssl_cert: str = ""
    ssl_key: str = ""
    connect_timeout_seconds: int = 5
    pool_size: int = 3


def _ssl_context(config: MySQLArchiveConfig) -> ssl.SSLContext | None:
    mode = str(config.ssl_mode or "disabled").strip().lower()
    if mode == "disabled":
        return None
    if mode not in {"required", "verify-ca", "verify-full"}:
        raise ValueError(f"unsupported MySQL TLS mode: {config.ssl_mode}")
    context = ssl.create_default_context(cafile=config.ssl_ca or None)
    if mode == "required":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif mode == "verify-ca":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
    if config.ssl_cert or config.ssl_key:
        if not config.ssl_cert or not config.ssl_key:
            raise ValueError(
                "MySQL client certificate and key must be configured together"
            )
        context.load_cert_chain(config.ssl_cert, config.ssl_key)
    return context


class RemoteMemoryArchive:
    """Append-only logical archive plus rebuildable current-head projection."""

    def __init__(self, config: MySQLArchiveConfig) -> None:
        self.config = config
        url = URL.create(
            "mysql+asyncmy",
            username=config.user,
            password=config.password,
            host="127.0.0.1" if config.host == "localhost" else config.host,
            port=int(config.port),
            database=config.database,
            query={"charset": "utf8mb4"},
        )
        connect_args: dict[str, Any] = {
            "connect_timeout": int(config.connect_timeout_seconds),
            "charset": "utf8mb4",
        }
        tls = _ssl_context(config)
        if tls is not None:
            connect_args["ssl"] = tls
        self._engine: AsyncEngine = create_async_engine(
            url,
            pool_size=max(1, int(config.pool_size)),
            max_overflow=max(1, int(config.pool_size)),
            pool_pre_ping=True,
            pool_recycle=900,
            pool_timeout=max(1, int(config.connect_timeout_seconds)),
            connect_args=connect_args,
        )
        self._immutability_guard = "not_initialized"

    async def close(self) -> None:
        await self._engine.dispose()

    async def initialize(self) -> None:
        """Create only the versioned archive namespace and immutability guards."""

        statements = (
            """
            CREATE TABLE IF NOT EXISTS elysium_memory_archive_schema_meta (
                schema_key VARCHAR(80) PRIMARY KEY,
                schema_version INT UNSIGNED NOT NULL,
                updated_at VARCHAR(64) NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_memory_archive_records (
                archive_position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                record_id CHAR(64) NOT NULL UNIQUE,
                source_node_id VARCHAR(80) NOT NULL,
                source_domain VARCHAR(80) NOT NULL,
                record_kind VARCHAR(180) NOT NULL,
                logical_key LONGTEXT NOT NULL,
                logical_key_hash CHAR(64) NOT NULL,
                immutable_key CHAR(64) NULL,
                mode VARCHAR(16) NOT NULL,
                source_sequence BIGINT UNSIGNED NOT NULL DEFAULT 0,
                recorded_at VARCHAR(64) NOT NULL DEFAULT '',
                visibility VARCHAR(32) NOT NULL,
                authority VARCHAR(64) NOT NULL,
                payload_json LONGTEXT NOT NULL,
                payload_hash CHAR(64) NOT NULL,
                schema_version INT UNSIGNED NOT NULL,
                received_at VARCHAR(64) NOT NULL,
                UNIQUE KEY uq_memory_archive_immutable (immutable_key),
                KEY idx_memory_archive_source (
                    source_node_id, source_domain, record_kind, logical_key_hash
                ),
                KEY idx_memory_archive_position (source_node_id, archive_position)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_memory_archive_heads (
                source_node_id VARCHAR(80) NOT NULL,
                source_domain VARCHAR(80) NOT NULL,
                record_kind VARCHAR(180) NOT NULL,
                logical_key_hash CHAR(64) NOT NULL,
                record_id CHAR(64) NOT NULL,
                archive_position BIGINT UNSIGNED NOT NULL,
                updated_at VARCHAR(64) NOT NULL,
                PRIMARY KEY (
                    source_node_id, source_domain, record_kind, logical_key_hash
                ),
                CONSTRAINT fk_memory_archive_head_record FOREIGN KEY (record_id)
                    REFERENCES elysium_memory_archive_records(record_id),
                KEY idx_memory_archive_head_position (archive_position)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_memory_archive_outbox (
                archive_position BIGINT UNSIGNED PRIMARY KEY,
                record_id CHAR(64) NOT NULL UNIQUE,
                state VARCHAR(24) NOT NULL DEFAULT 'pending',
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL,
                CONSTRAINT fk_memory_archive_outbox_record FOREIGN KEY (record_id)
                    REFERENCES elysium_memory_archive_records(record_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_memory_archive_conflicts (
                conflict_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                conflict_key VARCHAR(255) NOT NULL,
                incoming_record_id CHAR(64) NOT NULL,
                existing_record_id CHAR(64) NOT NULL DEFAULT '',
                incoming_hash CHAR(64) NOT NULL,
                existing_hash CHAR(64) NOT NULL DEFAULT '',
                detail VARCHAR(1000) NOT NULL,
                state VARCHAR(24) NOT NULL DEFAULT 'open',
                created_at VARCHAR(64) NOT NULL,
                KEY idx_memory_archive_conflict_state (state, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_memory_archive_runs (
                manifest_id CHAR(36) PRIMARY KEY,
                source_node_id VARCHAR(80) NOT NULL,
                run_mode VARCHAR(24) NOT NULL,
                started_at VARCHAR(64) NOT NULL,
                completed_at VARCHAR(64) NOT NULL DEFAULT '',
                status VARCHAR(24) NOT NULL,
                scanned_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
                accepted_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
                duplicate_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
                conflict_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
                source_counts_json LONGTEXT NOT NULL,
                root_hash CHAR(64) NOT NULL DEFAULT '',
                error_summary VARCHAR(1000) NOT NULL DEFAULT '',
                KEY idx_memory_archive_run_source (source_node_id, started_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_memory_archive_run_records (
                manifest_id CHAR(36) NOT NULL,
                ordinal BIGINT UNSIGNED NOT NULL,
                record_id CHAR(64) NOT NULL,
                archive_position BIGINT UNSIGNED NOT NULL,
                PRIMARY KEY (manifest_id, ordinal),
                UNIQUE KEY uq_memory_archive_run_record (manifest_id, record_id),
                CONSTRAINT fk_memory_archive_run FOREIGN KEY (manifest_id)
                    REFERENCES elysium_memory_archive_runs(manifest_id),
                CONSTRAINT fk_memory_archive_run_record FOREIGN KEY (record_id)
                    REFERENCES elysium_memory_archive_records(record_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
        )
        async with self._engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
            now = _now_iso()
            await connection.execute(
                text(
                    """INSERT INTO elysium_memory_archive_schema_meta (
                        schema_key, schema_version, updated_at
                    ) VALUES ('unified_memory_archive', :version, :now)
                    AS incoming
                    ON DUPLICATE KEY UPDATE
                        schema_version = GREATEST(
                            elysium_memory_archive_schema_meta.schema_version,
                            incoming.schema_version
                        ),
                        updated_at = incoming.updated_at"""
                ),
                {"version": ARCHIVE_SCHEMA_VERSION, "now": now},
            )
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT schema_version
                            FROM elysium_memory_archive_schema_meta
                            WHERE schema_key = 'unified_memory_archive'"""
                        )
                    )
                )
                .mappings()
                .one()
            )
            if int(row["schema_version"]) != ARCHIVE_SCHEMA_VERSION:
                raise RuntimeError(
                    "unsupported unified memory archive schema version: "
                    f"{row['schema_version']}"
                )
        await self._ensure_immutable_triggers()

    async def _ensure_immutable_triggers(self) -> None:
        guards = {
            "elysium_memory_archive_records_no_update": "UPDATE",
            "elysium_memory_archive_records_no_delete": "DELETE",
        }
        async with self._engine.begin() as connection:
            existing = {
                str(row["TRIGGER_NAME"])
                for row in (
                    await connection.execute(
                        text(
                            """SELECT TRIGGER_NAME FROM information_schema.TRIGGERS
                            WHERE TRIGGER_SCHEMA = DATABASE()
                              AND TRIGGER_NAME IN (
                                'elysium_memory_archive_records_no_update',
                                'elysium_memory_archive_records_no_delete'
                              )"""
                        )
                    )
                ).mappings()
            }
            for name, operation in guards.items():
                if name in existing:
                    continue
                try:
                    await connection.execute(
                        text(
                            f"""CREATE TRIGGER {name}
                            BEFORE {operation} ON elysium_memory_archive_records
                            FOR EACH ROW SIGNAL SQLSTATE '45000'
                            SET MESSAGE_TEXT = 'unified memory archive records are append-only'"""
                        )
                    )
                except DBAPIError as exc:
                    error_code = getattr(exc.orig, "args", (None,))[0]
                    if error_code not in {1142, 1419}:
                        raise
                    self._immutability_guard = "application_hash_audit"
                    return
            self._immutability_guard = "database_trigger"

    async def start_run(
        self,
        manifest_id: str,
        source_node_id: str,
        *,
        run_mode: str,
    ) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO elysium_memory_archive_runs (
                        manifest_id, source_node_id, run_mode, started_at, status,
                        source_counts_json
                    ) VALUES (:manifest_id, :source_node_id, :run_mode, :started_at,
                        'running', '{}')"""
                ),
                {
                    "manifest_id": manifest_id,
                    "source_node_id": source_node_id,
                    "run_mode": run_mode,
                    "started_at": _now_iso(),
                },
            )

    @staticmethod
    def _record_params(record: ArchiveRecord, *, received_at: str) -> dict[str, Any]:
        values = record.as_dict()
        values["received_at"] = received_at
        return values

    async def publish_batch(
        self,
        records: Sequence[ArchiveRecord],
        *,
        manifest_id: str,
        starting_ordinal: int,
        update_projections: bool = True,
    ) -> list[ArchivePublishResult]:
        """Publish one transaction with bounded deadlock/lock-timeout retries."""

        for attempt in range(4):
            try:
                return await self._publish_batch_once(
                    records,
                    manifest_id=manifest_id,
                    starting_ordinal=starting_ordinal,
                    update_projections=update_projections,
                )
            except DBAPIError as exc:
                error_code = getattr(exc.orig, "args", (None,))[0]
                if error_code not in {1062, 1205, 1213, 2006, 2013} or attempt == 3:
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
        raise RuntimeError("unreachable archive publish retry state")

    async def _publish_batch_once(
        self,
        records: Sequence[ArchiveRecord],
        *,
        manifest_id: str,
        starting_ordinal: int,
        update_projections: bool,
    ) -> list[ArchivePublishResult]:
        """Publish one bounded batch and link exact duplicates to the run."""

        if not records:
            return []
        record_placeholders = ", ".join(
            f":record_id_{index}" for index in range(len(records))
        )
        immutable_records = [record for record in records if record.immutable_key]
        immutable_clause = ""
        params: dict[str, Any] = {
            f"record_id_{index}": record.record_id
            for index, record in enumerate(records)
        }
        if immutable_records:
            immutable_placeholders = ", ".join(
                f":immutable_key_{index}" for index in range(len(immutable_records))
            )
            immutable_clause = f" OR immutable_key IN ({immutable_placeholders})"
            params.update(
                {
                    f"immutable_key_{index}": record.immutable_key
                    for index, record in enumerate(immutable_records)
                }
            )
        now = _now_iso()
        async with self._engine.begin() as connection:
            await connection.execute(text("SET SESSION innodb_lock_wait_timeout = 5"))
            existing_rows = (
                (
                    await connection.execute(
                        text(
                            """SELECT archive_position, record_id, immutable_key,
                        payload_hash, schema_version
                        FROM elysium_memory_archive_records
                        WHERE record_id IN ("""
                            + record_placeholders
                            + ")"
                            + immutable_clause
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            by_record_id = {str(row["record_id"]): row for row in existing_rows}
            by_immutable_key = {
                str(row["immutable_key"]): row
                for row in existing_rows
                if row["immutable_key"]
            }
            results: list[ArchivePublishResult | None] = [None] * len(records)
            new_records: list[ArchiveRecord] = []
            for index, record in enumerate(records):
                existing = by_record_id.get(record.record_id)
                if existing is None and record.immutable_key:
                    existing = by_immutable_key.get(record.immutable_key)
                if existing is None:
                    new_records.append(record)
                    continue
                same = (
                    str(existing["record_id"]) == record.record_id
                    and str(existing["payload_hash"]) == record.payload_hash
                    and int(existing["schema_version"]) == record.schema_version
                )
                if same:
                    results[index] = ArchivePublishResult(
                        record_id=record.record_id,
                        status="duplicate",
                        archive_position=int(existing["archive_position"]),
                    )
                    continue
                detail = "immutable logical identity has different content"
                await connection.execute(
                    text(
                        """INSERT INTO elysium_memory_archive_conflicts (
                            conflict_key, incoming_record_id, existing_record_id,
                            incoming_hash, existing_hash, detail, created_at
                        ) VALUES (:conflict_key, :incoming_record_id,
                            :existing_record_id, :incoming_hash, :existing_hash,
                            :detail, :created_at)"""
                    ),
                    {
                        "conflict_key": record.immutable_key or record.record_id,
                        "incoming_record_id": record.record_id,
                        "existing_record_id": str(existing["record_id"]),
                        "incoming_hash": record.payload_hash,
                        "existing_hash": str(existing["payload_hash"]),
                        "detail": detail,
                        "created_at": now,
                    },
                )
                results[index] = ArchivePublishResult(
                    record_id=record.record_id,
                    status="conflict",
                    archive_position=int(existing["archive_position"]),
                    conflict_reason=detail,
                    existing_hash=str(existing["payload_hash"]),
                )
            if new_records:
                await connection.execute(
                    text(
                        """INSERT INTO elysium_memory_archive_records (
                            record_id, source_node_id, source_domain, record_kind,
                            logical_key, logical_key_hash, immutable_key, mode,
                            source_sequence, recorded_at, visibility, authority,
                            payload_json, payload_hash, schema_version, received_at
                        ) VALUES (
                            :record_id, :source_node_id, :source_domain, :record_kind,
                            :logical_key, :logical_key_hash, :immutable_key, :mode,
                            :source_sequence, :recorded_at, :visibility, :authority,
                            :payload_json, :payload_hash, :schema_version, :received_at
                        )"""
                    ),
                    [
                        self._record_params(record, received_at=now)
                        for record in new_records
                    ],
                )
                new_ids = {record.record_id for record in new_records}
                new_placeholders = ", ".join(
                    f":new_id_{index}" for index in range(len(new_records))
                )
                new_positions = (
                    (
                        await connection.execute(
                            text(
                                """SELECT archive_position, record_id
                            FROM elysium_memory_archive_records WHERE record_id IN ("""
                                + new_placeholders
                                + ")"
                            ),
                            {
                                f"new_id_{index}": record.record_id
                                for index, record in enumerate(new_records)
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
                position_by_id = {
                    str(row["record_id"]): int(row["archive_position"])
                    for row in new_positions
                }
                if set(position_by_id) != new_ids:
                    raise RuntimeError(
                        "remote archive did not return every inserted record"
                    )
                if update_projections:
                    await connection.execute(
                        text(
                            """INSERT INTO elysium_memory_archive_outbox (
                                archive_position, record_id, state, created_at,
                                updated_at
                            ) VALUES (:archive_position, :record_id, 'pending',
                                :now, :now)"""
                        ),
                        [
                            {
                                "archive_position": position_by_id[record.record_id],
                                "record_id": record.record_id,
                                "now": now,
                            }
                            for record in new_records
                        ],
                    )
                    await connection.execute(
                        text(
                            """INSERT INTO elysium_memory_archive_heads (
                                source_node_id, source_domain, record_kind,
                                logical_key_hash, record_id, archive_position,
                                updated_at
                            ) VALUES (:source_node_id, :source_domain, :record_kind,
                                :logical_key_hash, :record_id, :archive_position,
                                :updated_at)
                            AS incoming
                            ON DUPLICATE KEY UPDATE
                                record_id = IF(
                                    incoming.archive_position >=
                                        elysium_memory_archive_heads.archive_position,
                                    incoming.record_id,
                                        elysium_memory_archive_heads.record_id
                                ),
                                archive_position = GREATEST(
                                    elysium_memory_archive_heads.archive_position,
                                    incoming.archive_position
                                ),
                                updated_at = IF(
                                    incoming.archive_position >=
                                        elysium_memory_archive_heads.archive_position,
                                    incoming.updated_at,
                                        elysium_memory_archive_heads.updated_at
                                )"""
                        ),
                        [
                            {
                                "source_node_id": record.source_node_id,
                                "source_domain": record.source_domain,
                                "record_kind": record.record_kind,
                                "logical_key_hash": record.logical_key_hash,
                                "record_id": record.record_id,
                                "archive_position": position_by_id[record.record_id],
                                "updated_at": now,
                            }
                            for record in new_records
                        ],
                    )
                for index, record in enumerate(records):
                    if record.record_id in position_by_id:
                        results[index] = ArchivePublishResult(
                            record_id=record.record_id,
                            status="accepted",
                            archive_position=position_by_id[record.record_id],
                        )
            accepted_results = [result for result in results if result is not None]
            if len(accepted_results) != len(records):
                raise RuntimeError("archive batch produced an incomplete result set")
            run_rows = []
            for offset, result in enumerate(accepted_results):
                if not result.accepted:
                    continue
                run_rows.append(
                    {
                        "manifest_id": manifest_id,
                        "ordinal": int(starting_ordinal) + offset,
                        "record_id": result.record_id,
                        "archive_position": result.archive_position,
                    }
                )
            if run_rows:
                await connection.execute(
                    text(
                        """INSERT INTO elysium_memory_archive_run_records (
                            manifest_id, ordinal, record_id, archive_position
                        ) VALUES (:manifest_id, :ordinal, :record_id,
                            :archive_position)
                        ON DUPLICATE KEY UPDATE
                            archive_position = archive_position"""
                    ),
                    run_rows,
                )
                ordinal_placeholders = ", ".join(
                    f":run_ordinal_{index}" for index in range(len(run_rows))
                )
                linked_rows = (
                    (
                        await connection.execute(
                            text(
                                """SELECT ordinal, record_id, archive_position
                                FROM elysium_memory_archive_run_records
                                WHERE manifest_id = :linked_manifest_id
                                  AND ordinal IN ("""
                                + ordinal_placeholders
                                + ")"
                            ),
                            {
                                "linked_manifest_id": manifest_id,
                                **{
                                    f"run_ordinal_{index}": row["ordinal"]
                                    for index, row in enumerate(run_rows)
                                },
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
                linked_by_ordinal = {int(row["ordinal"]): row for row in linked_rows}
                for expected in run_rows:
                    linked = linked_by_ordinal.get(int(expected["ordinal"]))
                    if linked is None or (
                        str(linked["record_id"]) != expected["record_id"]
                        or int(linked["archive_position"])
                        != expected["archive_position"]
                    ):
                        raise RuntimeError(
                            "archive run ordinal is already linked to different data"
                        )
            return accepted_results

    async def finalize_full_snapshot(self, manifest_id: str) -> None:
        """Build rebuildable projections after all full-snapshot records exist."""

        async with self._engine.connect() as connection:
            run = (
                (
                    await connection.execute(
                        text(
                            """SELECT run_mode, status
                            FROM elysium_memory_archive_runs
                            WHERE manifest_id = :manifest_id"""
                        ),
                        {"manifest_id": manifest_id},
                    )
                )
                .mappings()
                .one()
            )
            domains = [
                str(row["source_domain"])
                for row in (
                    await connection.execute(
                        text(
                            """SELECT DISTINCT r.source_domain
                            FROM elysium_memory_archive_run_records AS rr
                            JOIN elysium_memory_archive_records AS r
                              ON r.record_id = rr.record_id
                            WHERE rr.manifest_id = :manifest_id
                            ORDER BY r.source_domain"""
                        ),
                        {"manifest_id": manifest_id},
                    )
                ).mappings()
            ]
        if str(run["run_mode"]) != "full_snapshot" or str(run["status"]) != "running":
            raise RuntimeError("only a running full snapshot can build projections")
        for domain in domains:
            for attempt in range(4):
                try:
                    await self._finalize_full_snapshot_domain(manifest_id, domain)
                    break
                except DBAPIError as exc:
                    error_code = getattr(exc.orig, "args", (None,))[0]
                    if error_code not in {1062, 1205, 1213, 2006, 2013} or attempt == 3:
                        raise
                    await asyncio.sleep(0.5 * (2**attempt))

    async def _finalize_full_snapshot_domain(
        self,
        manifest_id: str,
        source_domain: str,
    ) -> None:
        now = _now_iso()
        params = {
            "manifest_id": manifest_id,
            "source_domain": source_domain,
            "now": now,
        }
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO elysium_memory_archive_outbox (
                        archive_position, record_id, state, created_at, updated_at
                    )
                    SELECT r.archive_position, r.record_id, 'pending', :now, :now
                    FROM elysium_memory_archive_run_records AS rr
                    JOIN elysium_memory_archive_records AS r
                      ON r.record_id = rr.record_id
                    LEFT JOIN elysium_memory_archive_outbox AS existing
                      ON existing.archive_position = r.archive_position
                    WHERE rr.manifest_id = :manifest_id
                      AND r.source_domain = :source_domain
                      AND existing.archive_position IS NULL"""
                ),
                params,
            )
            await connection.execute(
                text(
                    """INSERT INTO elysium_memory_archive_heads (
                        source_node_id, source_domain, record_kind,
                        logical_key_hash, record_id, archive_position, updated_at
                    )
                    SELECT ranked.source_node_id, ranked.source_domain,
                        ranked.record_kind, ranked.logical_key_hash,
                        ranked.record_id, ranked.archive_position, :now
                    FROM (
                        SELECT r.source_node_id, r.source_domain, r.record_kind,
                            r.logical_key_hash, r.record_id, r.archive_position,
                            ROW_NUMBER() OVER (
                                PARTITION BY r.source_node_id, r.source_domain,
                                    r.record_kind, r.logical_key_hash
                                ORDER BY r.archive_position DESC
                            ) AS row_rank
                        FROM elysium_memory_archive_run_records AS rr
                        JOIN elysium_memory_archive_records AS r
                          ON r.record_id = rr.record_id
                        WHERE rr.manifest_id = :manifest_id
                          AND r.source_domain = :source_domain
                    ) AS ranked
                    WHERE ranked.row_rank = 1
                    ON DUPLICATE KEY UPDATE
                        record_id = IF(
                            VALUES(archive_position) >=
                                elysium_memory_archive_heads.archive_position,
                            VALUES(record_id),
                                elysium_memory_archive_heads.record_id
                        ),
                        archive_position = GREATEST(
                            elysium_memory_archive_heads.archive_position,
                            VALUES(archive_position)
                        ),
                        updated_at = IF(
                            VALUES(archive_position) >=
                                elysium_memory_archive_heads.archive_position,
                            VALUES(updated_at),
                                elysium_memory_archive_heads.updated_at
                        )"""
                ),
                params,
            )

    async def finish_run(
        self,
        manifest_id: str,
        *,
        status: str,
        scanned_count: int,
        accepted_count: int,
        duplicate_count: int,
        conflict_count: int,
        source_counts: dict[str, int],
        root_hash: str,
        error_summary: str = "",
    ) -> None:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """UPDATE elysium_memory_archive_runs SET
                        completed_at = :completed_at,
                        status = :status,
                        scanned_count = :scanned_count,
                        accepted_count = :accepted_count,
                        duplicate_count = :duplicate_count,
                        conflict_count = :conflict_count,
                        source_counts_json = :source_counts_json,
                        root_hash = :root_hash,
                        error_summary = :error_summary
                    WHERE manifest_id = :manifest_id AND status = 'running'"""
                ),
                {
                    "manifest_id": manifest_id,
                    "completed_at": _now_iso(),
                    "status": status,
                    "scanned_count": scanned_count,
                    "accepted_count": accepted_count,
                    "duplicate_count": duplicate_count,
                    "conflict_count": conflict_count,
                    "source_counts_json": canonical_json(source_counts),
                    "root_hash": root_hash,
                    "error_summary": str(error_summary)[:1000],
                },
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"archive run is missing or already finished: {manifest_id}"
                )

    async def verify_run(self, manifest_id: str) -> dict[str, Any]:
        async with self._engine.connect() as connection:
            run = (
                (
                    await connection.execute(
                        text(
                            """SELECT * FROM elysium_memory_archive_runs
                            WHERE manifest_id = :manifest_id"""
                        ),
                        {"manifest_id": manifest_id},
                    )
                )
                .mappings()
                .one()
            )
            rows = await connection.stream(
                text(
                    """SELECT rr.ordinal, rr.record_id, r.payload_json,
                        r.payload_hash
                    FROM elysium_memory_archive_run_records AS rr
                    JOIN elysium_memory_archive_records AS r
                      ON r.record_id = rr.record_id
                    WHERE rr.manifest_id = :manifest_id
                    ORDER BY rr.ordinal"""
                ),
                {"manifest_id": manifest_id},
            )
            digest = hashlib.sha256()
            linked_records = 0
            payload_hash_mismatches = 0
            async for row in rows.mappings():
                calculated_payload_hash = hashlib.sha256(
                    str(row["payload_json"]).encode("utf-8")
                ).hexdigest()
                if calculated_payload_hash != str(row["payload_hash"]):
                    payload_hash_mismatches += 1
                digest.update(str(row["record_id"]).encode("ascii"))
                digest.update(b":")
                digest.update(calculated_payload_hash.encode("ascii"))
                digest.update(b"\n")
                linked_records += 1
        calculated = digest.hexdigest()
        expected_rows = int(run["scanned_count"]) - int(run["conflict_count"])
        return {
            "manifest_id": manifest_id,
            "status": str(run["status"]),
            "linked_records": linked_records,
            "expected_linked_records": expected_rows,
            "payload_hash_mismatch_count": payload_hash_mismatches,
            "root_hash": str(run["root_hash"]),
            "calculated_root_hash": calculated,
            "verified": (
                str(run["status"]) == "complete"
                and linked_records == expected_rows
                and payload_hash_mismatches == 0
                and calculated == str(run["root_hash"])
            ),
        }

    async def mark_run_verification_failed(
        self,
        manifest_id: str,
        error_summary: str,
    ) -> None:
        """Ensure a manifest that failed hash verification is not left complete."""

        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """UPDATE elysium_memory_archive_runs
                    SET status = 'verification_failed',
                        error_summary = :error_summary
                    WHERE manifest_id = :manifest_id AND status = 'complete'"""
                ),
                {
                    "manifest_id": manifest_id,
                    "error_summary": str(error_summary)[:1000],
                },
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    "archive run could not be marked verification_failed: "
                    f"{manifest_id}"
                )

    async def fetch_heads(
        self,
        *,
        source_node_id: str,
        source_domain: str,
        after_position: int = 0,
        limit: int = 1000,
    ) -> list[tuple[int, ArchiveRecord]]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            """SELECT r.* FROM elysium_memory_archive_heads AS h
                        JOIN elysium_memory_archive_records AS r
                          ON r.record_id = h.record_id
                        WHERE h.source_node_id = :source_node_id
                          AND h.source_domain = :source_domain
                          AND h.archive_position > :after_position
                        ORDER BY h.archive_position LIMIT :batch_limit"""
                        ),
                        {
                            "source_node_id": source_node_id,
                            "source_domain": source_domain,
                            "after_position": max(0, int(after_position)),
                            "batch_limit": max(1, int(limit)),
                        },
                    )
                )
                .mappings()
                .all()
            )
        return [
            (
                int(row["archive_position"]),
                ArchiveRecord(
                    record_id=str(row["record_id"]),
                    source_node_id=str(row["source_node_id"]),
                    source_domain=str(row["source_domain"]),
                    record_kind=str(row["record_kind"]),
                    logical_key=str(row["logical_key"]),
                    logical_key_hash=str(row["logical_key_hash"]),
                    immutable_key=(
                        str(row["immutable_key"]) if row["immutable_key"] else None
                    ),
                    mode=ArchiveMode(str(row["mode"])),
                    source_sequence=int(row["source_sequence"]),
                    recorded_at=str(row["recorded_at"]),
                    visibility=str(row["visibility"]),
                    authority=str(row["authority"]),
                    payload_json=str(row["payload_json"]),
                    payload_hash=str(row["payload_hash"]),
                    schema_version=int(row["schema_version"]),
                ),
            )
            for row in rows
        ]

    async def source_domains(self, source_node_id: str) -> list[str]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            """SELECT DISTINCT source_domain
                        FROM elysium_memory_archive_heads
                        WHERE source_node_id = :source_node_id
                        ORDER BY source_domain"""
                        ),
                        {"source_node_id": source_node_id},
                    )
                )
                .mappings()
                .all()
            )
        return [str(row["source_domain"]) for row in rows]

    async def health(self, *, source_node_id: str = "") -> dict[str, Any]:
        where = ""
        params: dict[str, Any] = {}
        if source_node_id:
            where = " WHERE source_node_id = :source_node_id"
            params["source_node_id"] = source_node_id
        async with self._engine.connect() as connection:
            totals = (
                (
                    await connection.execute(
                        text(
                            "SELECT COUNT(*) AS total, "
                            "COALESCE(MAX(archive_position), 0) AS latest_position "
                            "FROM elysium_memory_archive_records" + where
                        ),
                        params,
                    )
                )
                .mappings()
                .one()
            )
            conflicts = (
                (
                    await connection.execute(
                        text(
                            """SELECT COUNT(*) AS total
                            FROM elysium_memory_archive_conflicts
                            WHERE state = 'open'"""
                        )
                    )
                )
                .mappings()
                .one()
            )
        return {
            "component": "unified_memory_archive",
            "status": (
                "healthy"
                if self._immutability_guard == "database_trigger"
                else "degraded"
            ),
            "immutability_guard": self._immutability_guard,
            "total": int(totals["total"]),
            "latest_position": int(totals["latest_position"]),
            "open_conflict_count": int(conflicts["total"]),
        }
