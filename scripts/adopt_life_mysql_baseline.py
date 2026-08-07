#!/usr/bin/env python3
"""Audit and explicitly adopt an existing MySQL life-domain baseline."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from plugins.life_engine.storage.attention_schema import (
    _MYSQL_IMMUTABILITY_TRIGGERS as _ATTENTION_TRIGGERS,
)
from plugins.life_engine.storage.attention_schema import (
    _MYSQL_LEGACY_MIGRATION_V2 as _ATTENTION_LEGACY_MIGRATION,
)
from plugins.life_engine.storage.attention_schema import (
    _MYSQL_MIGRATION_V1 as _ATTENTION_MIGRATION,
)
from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.event_schema import (
    _MYSQL_IMMUTABILITY_MIGRATION,
    _MYSQL_IMMUTABILITY_TRIGGERS,
)
from plugins.life_engine.storage.learning_schema import (
    _MYSQL_IMMUTABILITY_TRIGGERS as _LEARNING_TRIGGERS,
)
from plugins.life_engine.storage.learning_schema import (
    _MYSQL_SCHEMA_MIGRATION as _LEARNING_MIGRATION,
)
from plugins.life_engine.storage.learning_schema import (
    MYSQL_LEARNING_CLAIM_GUARD_MIGRATION,
    MYSQL_LEARNING_CLAIM_GUARD_TRIGGERS,
)
from plugins.life_engine.storage.memory.schema import (
    MEMORY_IMMUTABILITY_MIGRATIONS,
    MEMORY_IMMUTABILITY_TRIGGER_CONTRACT,
)
from plugins.life_engine.storage.migration.domain_copy import (
    TABLE_ORDER as PRESENCE_WORLD_TABLES,
)
from plugins.life_engine.storage.migration.memory_copy import SOURCE_TABLE_ORDER
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.runtime_schema import (
    MYSQL_RUNTIME_EVENT_TRIGGERS,
    MYSQL_RUNTIME_STATE_CLAIM_GUARD_MIGRATION,
    MYSQL_RUNTIME_STATE_CLAIM_GUARD_TRIGGERS,
    MYSQL_RUNTIME_STATE_MIGRATION,
)
from plugins.life_engine.storage.subject_schema import (
    _MYSQL_SUBJECT_IMMUTABILITY,
    _MYSQL_SUBJECT_IMMUTABILITY_TRIGGERS,
)
from plugins.life_engine.storage.writer_claims import (
    MYSQL_SINGLETON_WRITER_EVENT_TRIGGERS,
    MYSQL_SINGLETON_WRITER_MIGRATION,
)
from src.kernel.storage import (
    MySQLMigrationRunner,
    MySQLStorageConfig,
    canonical_json,
    create_mysql_storage_engine,
)
from src.kernel.storage.migration_runner import (
    MySQLTriggerContract,
    verify_mysql_trigger_contract,
)

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENV_REFERENCE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")

DOMAIN_TABLES = {
    "life_event": (
        "raw_life_events",
        "raw_event_consumer_offsets",
        "raw_event_ledger_meta",
        "raw_event_export_outbox",
    ),
    "life_memory": tuple(SOURCE_TABLE_ORDER),
    "subject_document": (
        "subject_documents",
        "subject_document_versions",
        "subject_document_head_events",
        "subject_projection_outbox",
        "subject_authority_decisions",
    ),
    "presence_world": tuple(PRESENCE_WORLD_TABLES),
    "life_learning": ("learning_events", "learning_projections"),
}

RUNTIME_STATE_TABLES = (
    "runtime_states",
    "runtime_events",
    "runtime_singleton_writer_claims",
    "runtime_singleton_writer_events",
    "runtime_singleton_writer_bindings",
)
LEARNING_TABLES = ("learning_events", "learning_projections")
ATTENTION_TABLES = (
    "attention_thread_events",
    "attention_thread_heads",
    "attention_instance_focus",
    "attention_legacy_snapshots",
    "attention_legacy_candidates",
)


@dataclass(frozen=True, slots=True)
class TableEvidence:
    """Content-only evidence for one explicitly selected table."""

    table_name: str
    row_count: int
    root_sha256: str
    primary_key: tuple[str, ...]
    engine: str
    table_collation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "row_count": self.row_count,
            "root_sha256": self.root_sha256,
            "primary_key": list(self.primary_key),
            "engine": self.engine,
            "table_collation": self.table_collation,
        }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "audit",
            "activate",
            "upgrade-runtime-state",
            "upgrade-learning",
            "upgrade-attention",
        ),
    )
    parser.add_argument("--config", type=Path, default=_ROOT / "config" / "core.toml")
    parser.add_argument("--generation-id")
    parser.add_argument("--owner-id")
    parser.add_argument("--registry-id", default="life-domain")
    parser.add_argument("--lease-seconds", type=int, default=3600)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-remote-baseline", action="store_true")
    return parser.parse_args()


def _load_database_config(path: Path) -> MySQLStorageConfig:
    with path.resolve().open("rb") as handle:
        value = tomllib.load(handle)
    if str(dict(value.get("storage") or {}).get("backend") or "") != "mysql":
        raise RuntimeError("storage.backend must be mysql before remote adoption")
    database = dict(value.get("database") or {})
    match = _ENV_REFERENCE.fullmatch(str(database.get("mysql_password") or ""))
    if match is None:
        raise RuntimeError("database.mysql_password must be an environment reference")
    password = os.environ.get(match.group(1), "")
    if not password:
        raise RuntimeError(f"required environment variable is missing: {match.group(1)}")
    return MySQLStorageConfig(
        host=str(database.get("mysql_host") or ""),
        port=int(database.get("mysql_port") or 0),
        database=str(database.get("mysql_database") or ""),
        user=str(database.get("mysql_user") or ""),
        password=password,
        ssl_mode=str(database.get("mysql_ssl_mode") or "disabled"),
        ssl_ca=str(database.get("mysql_ssl_ca") or ""),
        ssl_cert=str(database.get("mysql_ssl_cert") or ""),
        ssl_key=str(database.get("mysql_ssl_key") or ""),
        pool_size=2,
        max_overflow=0,
        connect_timeout_seconds=int(database.get("connection_timeout") or 10),
        pool_timeout_seconds=int(database.get("mysql_pool_timeout_seconds") or 10),
        application_query_timeout_seconds=300,
        innodb_lock_wait_timeout_seconds=int(
            database.get("mysql_lock_wait_timeout_seconds") or 5
        ),
    )


def _encoded_value(value: Any) -> bytes:
    if value is None:
        return b"null"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b"bytes:" + bytes(value)
    if isinstance(value, bool):
        return b"bool:1" if value else b"bool:0"
    if isinstance(value, int):
        return f"int:{value}".encode("ascii")
    if isinstance(value, Decimal):
        return f"decimal:{value}".encode("ascii")
    if isinstance(value, float):
        return f"float:{value.hex()}".encode("ascii")
    if isinstance(value, datetime):
        return f"datetime:{value.isoformat(timespec='microseconds')}".encode("ascii")
    if isinstance(value, (date, time)):
        return f"temporal:{value.isoformat()}".encode("ascii")
    if isinstance(value, str):
        return b"text:" + value.encode("utf-8")
    raise TypeError(f"unsupported MySQL evidence value: {type(value).__name__}")


def _update_digest(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


async def _table_metadata(
    connection: AsyncConnection,
    table_name: str,
) -> tuple[tuple[str, ...], tuple[str, ...], str, str]:
    if not _IDENTIFIER.fullmatch(table_name):
        raise ValueError(f"unsafe table name: {table_name!r}")
    table = (
        (
            await connection.execute(
                text(
                    "SELECT ENGINE, TABLE_COLLATION FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name"
                ),
                {"table_name": table_name},
            )
        )
        .mappings()
        .one_or_none()
    )
    if table is None:
        raise RuntimeError(f"required MySQL table is missing: {table_name}")
    if str(table["ENGINE"] or "").upper() != "INNODB":
        raise RuntimeError(f"required MySQL table is not InnoDB: {table_name}")
    columns = (
        (
            await connection.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name "
                    "ORDER BY ORDINAL_POSITION"
                ),
                {"table_name": table_name},
            )
        )
        .scalars()
        .all()
    )
    keys = (
        (
            await connection.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name "
                    "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION"
                ),
                {"table_name": table_name},
            )
        )
        .scalars()
        .all()
    )
    normalized_columns = tuple(str(item) for item in columns)
    normalized_keys = tuple(str(item) for item in keys)
    if not normalized_columns or not normalized_keys:
        raise RuntimeError(f"required MySQL table has no stable primary key: {table_name}")
    identifiers = (*normalized_columns, *normalized_keys)
    if any(not _IDENTIFIER.fullmatch(item) for item in identifiers):
        raise RuntimeError(f"required MySQL table has an unsafe identifier: {table_name}")
    return (
        normalized_columns,
        normalized_keys,
        str(table["ENGINE"]),
        str(table["TABLE_COLLATION"] or ""),
    )


async def _audit_table(
    connection: AsyncConnection,
    table_name: str,
) -> TableEvidence:
    columns, keys, engine, collation = await _table_metadata(connection, table_name)
    select_columns = ", ".join(f"`{item}`" for item in columns)
    order = ", ".join(f"`{item}`" for item in keys)
    result = await connection.stream(
        text(f"SELECT {select_columns} FROM `{table_name}` ORDER BY {order}")
    )
    digest = hashlib.sha256()
    count = 0
    async for row in result.mappings():
        row_digest = hashlib.sha256()
        for column in columns:
            _update_digest(row_digest, column.encode("ascii"))
            _update_digest(row_digest, _encoded_value(row[column]))
        _update_digest(digest, row_digest.digest())
        count += 1
    return TableEvidence(
        table_name=table_name,
        row_count=count,
        root_sha256=digest.hexdigest(),
        primary_key=keys,
        engine=engine,
        table_collation=collation,
    )


def _domain_root(items: list[TableEvidence]) -> str:
    digest = hashlib.sha256()
    for item in items:
        _update_digest(digest, item.table_name.encode("ascii"))
        _update_digest(digest, str(item.row_count).encode("ascii"))
        _update_digest(digest, bytes.fromhex(item.root_sha256))
    return digest.hexdigest()


async def audit_remote_baseline(engine: AsyncEngine) -> dict[str, Any]:
    """Read one repeatable MySQL snapshot without exposing row content."""

    domains: dict[str, Any] = {}
    async with engine.connect() as connection:
        await connection.execute(
            text("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        )
        await connection.execute(
            text("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        )
        try:
            for domain, table_names in DOMAIN_TABLES.items():
                items = [
                    await _audit_table(connection, table_name)
                    for table_name in table_names
                ]
                domains[domain] = {
                    "root_sha256": _domain_root(items),
                    "row_count": sum(item.row_count for item in items),
                    "tables": [item.to_dict() for item in items],
                }
            database_now = await connection.scalar(text("SELECT CURRENT_TIMESTAMP(6)"))
            server_version = str(await connection.scalar(text("SELECT VERSION()")))
            server_policy = (
                await connection.execute(
                    text(
                        "SELECT @@GLOBAL.log_bin AS log_bin, "
                        "@@GLOBAL.log_bin_trust_function_creators "
                        "AS log_bin_trust_function_creators"
                    )
                )
            ).mappings().one()
            account = str(await connection.scalar(text("SELECT CURRENT_USER()")))
            raw_grants = (await connection.execute(text("SHOW GRANTS"))).scalars().all()
            grants = " ".join(str(item).upper() for item in raw_grants)
            control_tables = {
                str(item)
                for item in (
                    await connection.execute(
                        text(
                            "SELECT TABLE_NAME FROM information_schema.TABLES "
                            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN "
                            "('storage_backend_generations', "
                            "'storage_authority_registry')"
                        )
                    )
                ).scalars()
            }
            registered_generations = 0
            generation_summaries: list[dict[str, Any]] = []
            active_authorities = 0
            if "storage_backend_generations" in control_tables:
                generation_rows = (
                    await connection.execute(
                        text(
                            "SELECT generation_id, backend, status, manifest_sha256 "
                            "FROM storage_backend_generations ORDER BY generation_id"
                        )
                    )
                ).mappings().all()
                registered_generations = len(generation_rows)
                generation_summaries = [
                    {
                        "generation_id": str(row["generation_id"]),
                        "backend": str(row["backend"]),
                        "status": str(row["status"]),
                        "manifest_sha256": str(row["manifest_sha256"]),
                    }
                    for row in generation_rows
                ]
            if "storage_authority_registry" in control_tables:
                active_authorities = int(
                    await connection.scalar(
                        text(
                            "SELECT COUNT(*) FROM storage_authority_registry "
                            "WHERE active_generation <> ''"
                        )
                    )
                    or 0
                )
            installed_triggers = int(
                await connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM information_schema.TRIGGERS "
                        "WHERE TRIGGER_SCHEMA = DATABASE()"
                    )
                )
                or 0
            )
        finally:
            await connection.rollback()
    global_digest = hashlib.sha256()
    for domain in DOMAIN_TABLES:
        _update_digest(global_digest, domain.encode("ascii"))
        _update_digest(global_digest, bytes.fromhex(domains[domain]["root_sha256"]))
    return {
        "schema_version": 1,
        "audited_at": (
            database_now.replace(tzinfo=database_now.tzinfo or UTC).isoformat()
            if isinstance(database_now, datetime)
            else datetime.now(UTC).isoformat()
        ),
        "server_version": server_version,
        "server_policy": {
            "log_bin": bool(server_policy["log_bin"]),
            "log_bin_trust_function_creators": bool(
                server_policy["log_bin_trust_function_creators"]
            ),
            "account": account.split("@", maxsplit=1)[0] + "@<redacted>",
            "trigger_privilege": "TRIGGER" in grants or "ALL PRIVILEGES" in grants,
            "system_variables_admin": "SYSTEM_VARIABLES_ADMIN" in grants,
            "super_privilege": "SUPER" in grants,
            "registered_generation_count": registered_generations,
            "registered_generations": generation_summaries,
            "active_authority_count": active_authorities,
            "installed_trigger_count": installed_triggers,
        },
        "global_root_sha256": global_digest.hexdigest(),
        "domains": domains,
    }


def _guard_trigger_installation(evidence: dict[str, Any]) -> None:
    policy = dict(evidence.get("server_policy") or {})
    if not bool(policy.get("trigger_privilege")):
        raise RuntimeError("MySQL account lacks TRIGGER privilege")
    if bool(policy.get("log_bin")) and not bool(
        policy.get("log_bin_trust_function_creators")
    ):
        raise RuntimeError(
            "MySQL binary logging requires log_bin_trust_function_creators=ON "
            "before trigger installation"
        )


def build_remote_generation(
    evidence: dict[str, Any],
    *,
    generation_id: str,
) -> BackendGeneration:
    """Sign remote evidence without claiming parity with the old local state."""

    domains = dict(evidence["domains"])
    now = datetime.now(UTC).isoformat()
    return BackendGeneration(
        generation_id=generation_id,
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256=str(evidence["global_root_sha256"]),
        root_hashes={
            f"mysql:{name}": str(value["root_sha256"])
            for name, value in domains.items()
        },
        frontiers={
            f"mysql:{name}:records": int(value["row_count"])
            for name, value in domains.items()
        },
        created_at=now,
        verified_at=now,
        status=GenerationStatus.VERIFIED,
        metadata={
            "adoption_mode": "existing_remote_shadow_baseline",
            "evidence_schema_version": int(evidence["schema_version"]),
            "remote_global_root_sha256": str(evidence["global_root_sha256"]),
            "local_parity_claimed": False,
            "user_confirmed_remote_authority": True,
            "required_domains": list(DOMAIN_TABLES),
        },
    )


async def _audit_runtime_state_tables(
    engine: AsyncEngine,
) -> dict[str, Any]:
    """Return content evidence for the additive runtime-state schema."""

    async with engine.connect() as connection:
        tables = [
            await _audit_table(connection, table_name)
            for table_name in RUNTIME_STATE_TABLES
        ]
        await connection.rollback()
    return {
        "root_sha256": _domain_root(tables),
        "row_count": sum(item.row_count for item in tables),
        "tables": [item.to_dict() for item in tables],
    }


async def _install_runtime_state_schema(engine: AsyncEngine) -> None:
    """Apply only the additive runtime-state schema without changing authority."""

    claim_runner = MySQLMigrationRunner(
        engine,
        table_name="life_singleton_writer_schema_migrations",
        lock_name="elysium:life-singleton-writer-schema",
    )
    await claim_runner.apply((MYSQL_SINGLETON_WRITER_MIGRATION,))
    await verify_mysql_trigger_contract(
        engine,
        MYSQL_SINGLETON_WRITER_EVENT_TRIGGERS,
    )
    runner = MySQLMigrationRunner(
        engine,
        table_name="life_runtime_state_schema_migrations",
        lock_name="elysium:life-runtime-state-schema",
    )
    await runner.apply(
        (
            MYSQL_RUNTIME_STATE_MIGRATION,
            MYSQL_RUNTIME_STATE_CLAIM_GUARD_MIGRATION,
        )
    )
    await verify_mysql_trigger_contract(
        engine,
        MYSQL_RUNTIME_EVENT_TRIGGERS
        + MYSQL_RUNTIME_STATE_CLAIM_GUARD_TRIGGERS,
    )


async def _audit_learning_tables(engine: AsyncEngine) -> dict[str, Any]:
    """Return content-free evidence for the additive Learning schema."""

    async with engine.connect() as connection:
        tables = [
            await _audit_table(connection, table_name) for table_name in LEARNING_TABLES
        ]
        await connection.rollback()
    return {
        "root_sha256": _domain_root(tables),
        "row_count": sum(item.row_count for item in tables),
        "tables": [item.to_dict() for item in tables],
    }


async def _install_learning_schema(engine: AsyncEngine) -> None:
    """Apply only Learning technical guards without changing authority/data."""

    claim_runner = MySQLMigrationRunner(
        engine,
        table_name="life_singleton_writer_schema_migrations",
        lock_name="elysium:life-singleton-writer-schema",
    )
    await claim_runner.apply((MYSQL_SINGLETON_WRITER_MIGRATION,))
    await verify_mysql_trigger_contract(
        engine,
        MYSQL_SINGLETON_WRITER_EVENT_TRIGGERS,
    )
    runner = MySQLMigrationRunner(
        engine,
        table_name="life_learning_schema_migrations",
        lock_name="elysium:life-learning-schema",
    )
    await runner.apply(
        (
            _LEARNING_MIGRATION,
            MYSQL_LEARNING_CLAIM_GUARD_MIGRATION,
        )
    )
    await verify_mysql_trigger_contract(
        engine,
        _LEARNING_TRIGGERS + MYSQL_LEARNING_CLAIM_GUARD_TRIGGERS,
    )


async def _audit_attention_tables(engine: AsyncEngine) -> dict[str, Any]:
    """Return content-free evidence for the additive AttentionThread schema."""

    async with engine.connect() as connection:
        tables = [
            await _audit_table(connection, table_name)
            for table_name in ATTENTION_TABLES
        ]
        await connection.rollback()
    return {
        "root_sha256": _domain_root(tables),
        "row_count": sum(item.row_count for item in tables),
        "tables": [item.to_dict() for item in tables],
    }


async def _install_attention_schema(engine: AsyncEngine) -> None:
    """Apply only additive AttentionThread schema without changing authority."""

    runner = MySQLMigrationRunner(
        engine,
        table_name="life_attention_schema_migrations",
        lock_name="elysium:life-attention-schema",
    )
    await runner.apply((_ATTENTION_MIGRATION, _ATTENTION_LEGACY_MIGRATION))
    await verify_mysql_trigger_contract(engine, _ATTENTION_TRIGGERS)


async def _install_immutability(engine: AsyncEngine) -> None:
    stages = (
        (
            "life_event",
            MySQLMigrationRunner(
                engine,
                table_name="life_event_immutability_schema_migrations",
                lock_name="elysium:life-event-immutability-schema",
            ),
            (_MYSQL_IMMUTABILITY_MIGRATION,),
            _MYSQL_IMMUTABILITY_TRIGGERS,
        ),
        (
            "life_memory",
            MySQLMigrationRunner(
                engine,
                table_name="life_memory_immutability_schema_migrations",
                lock_name="elysium:life-memory-immutability",
            ),
            MEMORY_IMMUTABILITY_MIGRATIONS,
            tuple(
                MySQLTriggerContract(
                    name=name,
                    table=table,
                    manipulation=event_name,
                    timing="BEFORE",
                    action_fragment=(
                        "MemoryWitnessAuthorityImmutable"
                        if table == "memory_witnesses"
                        else "MemoryAuthorityRecordImmutable"
                    ),
                )
                for name, event_name, table in MEMORY_IMMUTABILITY_TRIGGER_CONTRACT
            ),
        ),
        (
            "subject_document",
            MySQLMigrationRunner(
                engine,
                table_name="subject_document_immutability_migrations",
                lock_name="elysium:subject-document-immutability",
            ),
            (_MYSQL_SUBJECT_IMMUTABILITY,),
            _MYSQL_SUBJECT_IMMUTABILITY_TRIGGERS,
        ),
        (
            "life_learning",
            MySQLMigrationRunner(
                engine,
                table_name="life_learning_schema_migrations",
                lock_name="elysium:life-learning-schema",
            ),
            (_LEARNING_MIGRATION,),
            _LEARNING_TRIGGERS,
        ),
        (
            "life_runtime",
            MySQLMigrationRunner(
                engine,
                table_name="life_runtime_state_schema_migrations",
                lock_name="elysium:life-runtime-state-schema",
            ),
            (MYSQL_RUNTIME_STATE_MIGRATION,),
            MYSQL_RUNTIME_EVENT_TRIGGERS,
        ),
    )
    for stage, runner, migrations, contracts in stages:
        try:
            await runner.apply(migrations)
            await verify_mysql_trigger_contract(engine, contracts)
        except Exception as exc:
            raise RuntimeError(
                f"immutability stage failed: {stage}:{type(exc).__name__}"
            ) from exc


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _safe_failure(exc: Exception, *, stage: str) -> dict[str, Any]:
    cause: BaseException = exc
    while cause.__cause__ is not None:
        cause = cause.__cause__
    error_code: int | None = None
    args = getattr(cause, "args", ())
    if args and isinstance(args[0], int):
        error_code = args[0]
    return {
        "status": "failed",
        "stage": stage,
        "reason": type(exc).__name__,
        "root_reason": type(cause).__name__,
        "database_error_code": error_code,
    }


async def _activate(args: argparse.Namespace, engine: AsyncEngine) -> dict[str, Any]:
    if not args.confirm_remote_baseline:
        raise RuntimeError("activation requires --confirm-remote-baseline")
    generation_id = str(args.generation_id or "").strip()
    owner_id = str(args.owner_id or "").strip()
    if not generation_id or not owner_id:
        raise RuntimeError("activation requires --generation-id and --owner-id")
    if args.output is None:
        raise RuntimeError("activation requires --output")
    if int(args.lease_seconds) < 120 or int(args.lease_seconds) > 86400:
        raise ValueError("lease-seconds must be between 120 and 86400")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)

    try:
        before = await audit_remote_baseline(engine)
        _write_json(output / "baseline-before.json", before)
    except Exception as exc:
        _write_json(output / "failure.json", _safe_failure(exc, stage="baseline_before"))
        raise
    try:
        _guard_trigger_installation(before)
    except Exception as exc:
        _write_json(output / "failure.json", _safe_failure(exc, stage="trigger_policy"))
        raise
    try:
        await _install_immutability(engine)
    except Exception as exc:
        stage = str(exc) if str(exc).startswith("immutability stage failed:") else "immutability"
        _write_json(output / "failure.json", _safe_failure(exc, stage=stage))
        raise
    try:
        after = await audit_remote_baseline(engine)
        _write_json(output / "baseline-after.json", after)
        if before["global_root_sha256"] != after["global_root_sha256"]:
            raise RuntimeError("remote baseline changed while immutability was installed")
    except Exception as exc:
        _write_json(output / "failure.json", _safe_failure(exc, stage="baseline_after"))
        raise

    generation = build_remote_generation(after, generation_id=generation_id)
    sealed = {**generation.to_dict(), "manifest_sha256": generation.manifest_sha256}
    _write_json(output / "generation.json", sealed)

    try:
        registry = MySQLAuthorityRegistry(engine, registry_id=str(args.registry_id))
        existing = await registry.health()
        if str(existing.get("active_generation") or ""):
            raise RuntimeError("authority registry already has an active generation")
        await registry.register_generation(generation)
        registered = await registry.health()
        token = await registry.activate_generation(
            generation.generation_id,
            expected_epoch=int(registered.get("authority_epoch") or 0),
            owner_id=owner_id,
            lease_seconds=int(args.lease_seconds),
            confirm_previous_writers_stopped=True,
        )
        await registry.validate(token)
    except Exception as exc:
        _write_json(output / "failure.json", _safe_failure(exc, stage="authority"))
        raise
    token_path = output / "fencing-token.txt"
    token_path.write_text(token.fencing_token, encoding="ascii")
    activation = {
        "status": "activated",
        "backend": "mysql",
        "backend_generation": generation.generation_id,
        "generation_manifest_sha256": generation.manifest_sha256,
        "registry_id": token.registry_id,
        "authority_epoch": token.authority_epoch,
        "authority_owner_id": token.owner_id,
        "lease_until": token.lease_until,
        "fencing_token_file": str(token_path),
        "evidence_directory": str(output),
    }
    _write_json(output / "activation.json", activation)
    return activation


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    config = _load_database_config(args.config)
    engine = create_mysql_storage_engine(config)
    try:
        if args.mode == "upgrade-runtime-state":
            await _install_runtime_state_schema(engine)
            return {
                "status": "runtime_state_schema_upgraded",
                "backend_identity": config.safe_identity,
                "runtime_state": await _audit_runtime_state_tables(engine),
            }
        if args.mode == "upgrade-attention":
            await _install_attention_schema(engine)
            return {
                "status": "attention_schema_upgraded",
                "backend_identity": config.safe_identity,
                "attention": await _audit_attention_tables(engine),
            }
        if args.mode == "upgrade-learning":
            await _install_learning_schema(engine)
            return {
                "status": "learning_schema_upgraded",
                "backend_identity": config.safe_identity,
                "learning": await _audit_learning_tables(engine),
            }
        if args.mode == "audit":
            evidence = await audit_remote_baseline(engine)
            result = {
                "status": "audited",
                "backend_identity": config.safe_identity,
                "evidence": evidence,
            }
            if args.output is not None:
                output = args.output.resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                output.touch(exist_ok=False)
                _write_json(output, result)
            return result
        result = await _activate(args, engine)
        return {"backend_identity": config.safe_identity, **result}
    finally:
        await engine.dispose()


def main() -> int:
    try:
        result = asyncio.run(_run(_arguments()))
    except Exception as exc:  # noqa: BLE001 - never print secrets from DB errors
        print(canonical_json({"status": "failed", "reason": type(exc).__name__}))
        return 2
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
