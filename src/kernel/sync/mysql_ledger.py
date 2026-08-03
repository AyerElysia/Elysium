"""MySQL 8 implementation of the shared, idempotent event ledger."""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import URL, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .models import PublishResult, SyncEnvelope


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


@dataclass(frozen=True, slots=True)
class MySQLLedgerConfig:
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


def _ssl_context(config: MySQLLedgerConfig) -> ssl.SSLContext | None:
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


class RemoteMySQLLedger:
    """Append-only remote ledger with transactional fan-out Outbox."""

    def __init__(self, config: MySQLLedgerConfig) -> None:
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

    async def close(self) -> None:
        await self._engine.dispose()

    async def initialize(self) -> None:
        """Create versioned Phase-2 tables; never mutate application tables."""

        statements = (
            """
            CREATE TABLE IF NOT EXISTS elysium_sync_schema_meta (
                schema_key VARCHAR(80) PRIMARY KEY,
                schema_version INT UNSIGNED NOT NULL,
                updated_at VARCHAR(64) NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_sync_nodes (
                node_id VARCHAR(80) PRIMARY KEY,
                last_origin_sequence BIGINT UNSIGNED NOT NULL DEFAULT 0,
                created_at VARCHAR(64) NOT NULL,
                last_seen_at VARCHAR(64) NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_shared_events (
                remote_position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                event_id VARCHAR(160) NOT NULL UNIQUE,
                origin_node_id VARCHAR(80) NOT NULL,
                origin_sequence BIGINT UNSIGNED NOT NULL,
                occurred_at VARCHAR(64) NOT NULL,
                recorded_at VARCHAR(64) NOT NULL,
                event_type VARCHAR(160) NOT NULL,
                actor_id VARCHAR(160) NOT NULL DEFAULT '',
                consciousness_instance_id VARCHAR(160) NOT NULL DEFAULT '',
                visibility VARCHAR(32) NOT NULL,
                causation_id VARCHAR(160) NOT NULL DEFAULT '',
                correlation_id VARCHAR(160) NOT NULL DEFAULT '',
                payload_json LONGTEXT NOT NULL,
                payload_hash CHAR(64) NOT NULL,
                schema_version INT UNSIGNED NOT NULL DEFAULT 1,
                received_at VARCHAR(64) NOT NULL,
                UNIQUE KEY uq_shared_event_origin (origin_node_id, origin_sequence),
                KEY idx_shared_event_visibility_position (visibility, remote_position)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_shared_event_outbox (
                remote_position BIGINT UNSIGNED PRIMARY KEY,
                event_id VARCHAR(160) NOT NULL UNIQUE,
                state VARCHAR(24) NOT NULL DEFAULT 'pending',
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL,
                CONSTRAINT fk_shared_outbox_event FOREIGN KEY (remote_position)
                    REFERENCES elysium_shared_events(remote_position)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_sync_conflicts (
                conflict_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                conflict_key VARCHAR(255) NOT NULL,
                incoming_event_id VARCHAR(160) NOT NULL,
                existing_event_id VARCHAR(160) NOT NULL DEFAULT '',
                incoming_hash CHAR(64) NOT NULL,
                existing_hash CHAR(64) NOT NULL DEFAULT '',
                detail VARCHAR(1000) NOT NULL,
                state VARCHAR(24) NOT NULL DEFAULT 'open',
                created_at VARCHAR(64) NOT NULL,
                KEY idx_remote_conflict_state (state, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS elysium_consumer_cursors (
                consumer_id VARCHAR(160) PRIMARY KEY,
                remote_position BIGINT UNSIGNED NOT NULL DEFAULT 0,
                updated_at VARCHAR(64) NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
        )
        async with self._engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
            await connection.execute(
                text(
                    """INSERT INTO elysium_sync_schema_meta (
                        schema_key, schema_version, updated_at
                    ) VALUES ('offline_sync', 2, :now)
                    ON DUPLICATE KEY UPDATE
                        schema_version = GREATEST(schema_version, VALUES(schema_version)),
                        updated_at = VALUES(updated_at)"""
                ),
                {"now": _now_iso()},
            )
            version_row = (
                (
                    await connection.execute(
                        text(
                            """SELECT schema_version FROM elysium_sync_schema_meta
                        WHERE schema_key = 'offline_sync'"""
                        )
                    )
                )
                .mappings()
                .one()
            )
            if int(version_row["schema_version"]) != 2:
                raise RuntimeError(
                    "unsupported offline sync schema version: "
                    f"{version_row['schema_version']}"
                )

    @staticmethod
    def _params(envelope: SyncEnvelope, *, received_at: str) -> dict[str, Any]:
        values = envelope.as_dict()
        values["received_at"] = received_at
        return values

    @staticmethod
    def _same(existing: Any, envelope: SyncEnvelope) -> bool:
        return (
            str(existing["event_id"]) == envelope.event_id
            and str(existing["origin_node_id"]) == envelope.origin_node_id
            and int(existing["origin_sequence"]) == envelope.origin_sequence
            and str(existing["payload_hash"]) == envelope.payload_hash
            and int(existing["schema_version"]) == envelope.schema_version
        )

    async def _record_conflict(
        self,
        connection: Any,
        *,
        envelope: SyncEnvelope,
        existing: Any,
        key: str,
        detail: str,
        received_at: str,
    ) -> PublishResult:
        existing_event_id = str(existing["event_id"] or "")
        existing_hash = str(existing["payload_hash"] or "")
        await connection.execute(
            text(
                """INSERT INTO elysium_sync_conflicts (
                    conflict_key, incoming_event_id, existing_event_id,
                    incoming_hash, existing_hash, detail, created_at
                ) VALUES (:key, :incoming_event_id, :existing_event_id,
                    :incoming_hash, :existing_hash, :detail, :created_at)"""
            ),
            {
                "key": key,
                "incoming_event_id": envelope.event_id,
                "existing_event_id": existing_event_id,
                "incoming_hash": envelope.payload_hash,
                "existing_hash": existing_hash,
                "detail": detail,
                "created_at": received_at,
            },
        )
        return PublishResult(
            status="conflict",
            remote_position=int(existing["remote_position"] or 0),
            conflict_reason=detail,
            existing_hash=existing_hash,
        )

    async def _publish_once(self, envelope: SyncEnvelope) -> PublishResult:
        """Accept once, acknowledge exact replays, quarantine collisions."""

        if envelope.visibility not in {"shared", "public"}:
            return PublishResult(
                status="conflict",
                conflict_reason="remote policy rejects private visibility",
            )
        received_at = _now_iso()
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO elysium_sync_nodes (
                        node_id, last_origin_sequence, created_at, last_seen_at
                    ) VALUES (:node_id, 0, :now, :now)
                    ON DUPLICATE KEY UPDATE last_seen_at = VALUES(last_seen_at)"""
                ),
                {"node_id": envelope.origin_node_id, "now": received_at},
            )
            event_row = (
                (
                    await connection.execute(
                        text(
                            """SELECT remote_position, event_id, origin_node_id,
                        origin_sequence, payload_hash, schema_version
                        FROM elysium_shared_events WHERE event_id = :event_id
                        FOR UPDATE"""
                        ),
                        {"event_id": envelope.event_id},
                    )
                )
                .mappings()
                .first()
            )
            if event_row is not None:
                if self._same(event_row, envelope):
                    return PublishResult(
                        status="duplicate",
                        remote_position=int(event_row["remote_position"]),
                    )
                return await self._record_conflict(
                    connection,
                    envelope=envelope,
                    existing=event_row,
                    key=f"event:{envelope.event_id}",
                    detail="event id already exists with different immutable content",
                    received_at=received_at,
                )
            origin_row = (
                (
                    await connection.execute(
                        text(
                            """SELECT remote_position, event_id, origin_node_id,
                        origin_sequence, payload_hash, schema_version
                        FROM elysium_shared_events
                        WHERE origin_node_id = :node_id AND origin_sequence = :sequence
                        FOR UPDATE"""
                        ),
                        {
                            "node_id": envelope.origin_node_id,
                            "sequence": envelope.origin_sequence,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if origin_row is not None:
                if self._same(origin_row, envelope):
                    return PublishResult(
                        status="duplicate",
                        remote_position=int(origin_row["remote_position"]),
                    )
                return await self._record_conflict(
                    connection,
                    envelope=envelope,
                    existing=origin_row,
                    key=f"origin:{envelope.origin_node_id}:{envelope.origin_sequence}",
                    detail="origin sequence already belongs to another event",
                    received_at=received_at,
                )
            result = await connection.execute(
                text(
                    """INSERT INTO elysium_shared_events (
                        event_id, origin_node_id, origin_sequence, occurred_at,
                        recorded_at, event_type, actor_id,
                        consciousness_instance_id, visibility, causation_id,
                        correlation_id, payload_json, payload_hash, schema_version,
                        received_at
                    ) VALUES (
                        :event_id, :origin_node_id, :origin_sequence, :occurred_at,
                        :recorded_at, :event_type, :actor_id,
                        :consciousness_instance_id, :visibility, :causation_id,
                        :correlation_id, :payload_json, :payload_hash,
                        :schema_version, :received_at
                    )"""
                ),
                self._params(envelope, received_at=received_at),
            )
            remote_position = int(result.lastrowid or 0)
            await connection.execute(
                text(
                    """INSERT INTO elysium_shared_event_outbox (
                        remote_position, event_id, state, created_at, updated_at
                    ) VALUES (:position, :event_id, 'pending', :now, :now)"""
                ),
                {
                    "position": remote_position,
                    "event_id": envelope.event_id,
                    "now": received_at,
                },
            )
            await connection.execute(
                text(
                    """UPDATE elysium_sync_nodes SET
                    last_origin_sequence = GREATEST(last_origin_sequence, :sequence),
                    last_seen_at = :now WHERE node_id = :node_id"""
                ),
                {
                    "sequence": envelope.origin_sequence,
                    "now": received_at,
                    "node_id": envelope.origin_node_id,
                },
            )
            return PublishResult(status="accepted", remote_position=remote_position)

    async def publish(self, envelope: SyncEnvelope) -> PublishResult:
        """Publish safely even when different nodes race on the same key."""

        try:
            return await self._publish_once(envelope)
        except IntegrityError:
            received_at = _now_iso()
            async with self._engine.begin() as connection:
                row = (
                    (
                        await connection.execute(
                            text(
                                """SELECT remote_position, event_id, origin_node_id,
                            origin_sequence, payload_hash, schema_version
                            FROM elysium_shared_events
                            WHERE event_id = :event_id OR (
                                origin_node_id = :node_id AND origin_sequence = :sequence
                            ) ORDER BY (event_id = :event_id) DESC LIMIT 1 FOR UPDATE"""
                            ),
                            {
                                "event_id": envelope.event_id,
                                "node_id": envelope.origin_node_id,
                                "sequence": envelope.origin_sequence,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise
                if self._same(row, envelope):
                    return PublishResult(
                        status="duplicate",
                        remote_position=int(row["remote_position"]),
                    )
                return await self._record_conflict(
                    connection,
                    envelope=envelope,
                    existing=row,
                    key=f"race:{envelope.event_id}",
                    detail="concurrent immutable identity collision",
                    received_at=received_at,
                )

    @staticmethod
    def _from_row(row: Any) -> tuple[int, SyncEnvelope]:
        return int(row["remote_position"]), SyncEnvelope(
            event_id=str(row["event_id"]),
            origin_node_id=str(row["origin_node_id"]),
            origin_sequence=int(row["origin_sequence"]),
            occurred_at=str(row["occurred_at"]),
            recorded_at=str(row["recorded_at"]),
            event_type=str(row["event_type"]),
            actor_id=str(row["actor_id"]),
            consciousness_instance_id=str(row["consciousness_instance_id"]),
            visibility=str(row["visibility"]),
            causation_id=str(row["causation_id"]),
            correlation_id=str(row["correlation_id"]),
            payload_json=str(row["payload_json"]),
            payload_hash=str(row["payload_hash"]),
            schema_version=int(row["schema_version"]),
        )

    async def fetch_after(
        self,
        remote_position: int,
        *,
        limit: int,
        allowed_visibilities: set[str],
    ) -> list[tuple[int, SyncEnvelope]]:
        if not allowed_visibilities:
            return []
        visibility_params = {
            f"visibility_{index}": value
            for index, value in enumerate(sorted(allowed_visibilities))
        }
        placeholders = ", ".join(f":{name}" for name in visibility_params)
        query = text(
            f"""SELECT * FROM elysium_shared_events
            WHERE remote_position > :position AND visibility IN ({placeholders})
            ORDER BY remote_position LIMIT :batch_limit"""
        )
        params: dict[str, Any] = {
            "position": int(remote_position),
            "batch_limit": max(1, int(limit)),
            **visibility_params,
        }
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query, params)).mappings().all()
        return [self._from_row(row) for row in rows]

    async def health(self) -> dict[str, Any]:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT COUNT(*) AS total,
                        COALESCE(MAX(remote_position), 0) AS latest_position
                        FROM elysium_shared_events"""
                        )
                    )
                )
                .mappings()
                .one()
            )
            conflicts = (
                (
                    await connection.execute(
                        text(
                            """SELECT COUNT(*) AS total FROM elysium_sync_conflicts
                        WHERE state = 'open'"""
                        )
                    )
                )
                .mappings()
                .one()
            )
        return {
            "total": int(row["total"]),
            "latest_position": int(row["latest_position"]),
            "open_conflict_count": int(conflicts["total"]),
        }
