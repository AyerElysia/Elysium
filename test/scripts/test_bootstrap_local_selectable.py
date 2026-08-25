from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from plugins.life_engine.storage.authority import StaleAuthorityToken
from plugins.life_engine.storage.migration.runtime_context_copy import (
    RuntimeContextCopyError,
    copy_runtime_context_from_snapshot,
)
from plugins.life_engine.storage.migration.snapshot import inspect_sqlite_database
from plugins.life_engine.storage.runtime_adapters import SQLRuntimeStateStore
from plugins.life_engine.storage.runtime_schema import ensure_runtime_state_schema
from scripts.bootstrap_local_selectable import _open_local_copy_runtime
from src.kernel.storage import canonical_json


def _runtime_snapshot(tmp_path: Path, *, heartbeat_count: int = 7) -> Path:
    snapshot = tmp_path / "snapshot"
    backup = snapshot / "workspace" / "life_engine_workspace" / "life_engine_context.json"
    backup.parent.mkdir(parents=True)
    payload = {
        "version": 2,
        "state": {
            "heartbeat_count": heartbeat_count,
            "event_sequence": 3,
            "heartbeat_context_cursor": 2,
            "subconscious_summary": {"schema_version": 1, "entries": []},
        },
        "pending_events": [],
        "event_history": [
            {
                "event_id": "event:one",
                "sequence": 1,
                "event_type": "heartbeat",
            },
            {
                "event_id": "event:two",
                "sequence": 2,
                "event_type": "heartbeat",
            },
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    backup.write_bytes(encoded)
    relative = "workspace/life_engine_workspace/life_engine_context.json"
    exact = {
        "source_relative": "life_engine_workspace/life_engine_context.json",
        "backup_relative": relative,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "writer_frozen": True,
        "source_snapshot_sha256": "a" * 64,
        "root_hashes": {},
        "frontiers": {},
        "exact_files": [exact],
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        canonical_json(manifest).encode()
    ).hexdigest()
    (snapshot / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return snapshot


async def test_runtime_context_copy_is_exact_and_idempotent(tmp_path: Path) -> None:
    runtime, registry, token = await _open_local_copy_runtime(
        tmp_path / "candidate.sqlite3",
        tmp_path / "authority.json",
    )
    await ensure_runtime_state_schema(runtime)
    snapshot = _runtime_snapshot(tmp_path)
    try:
        first = await copy_runtime_context_from_snapshot(snapshot, runtime)
        replay = await copy_runtime_context_from_snapshot(snapshot, runtime)
        record = await SQLRuntimeStateStore(runtime).get_state(
            "life_engine.runtime_context",
            "global",
        )
    finally:
        await runtime.close()
        await registry.revoke(token)

    assert first["verified"] is True
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert first["frontiers"]["heartbeat_count"] == 7
    assert record is not None
    assert record.revision == 1
    assert record.payload["state"]["heartbeat_count"] == 7


async def test_runtime_context_copy_rejects_conflict_and_expectation_mismatch(
    tmp_path: Path,
) -> None:
    runtime, registry, token = await _open_local_copy_runtime(
        tmp_path / "candidate.sqlite3",
        tmp_path / "authority.json",
    )
    await ensure_runtime_state_schema(runtime)
    snapshot = _runtime_snapshot(tmp_path, heartbeat_count=4)
    try:
        with pytest.raises(RuntimeContextCopyError, match="heartbeat precondition"):
            await copy_runtime_context_from_snapshot(
                snapshot,
                runtime,
                expect_heartbeat_count=1653,
            )
        await copy_runtime_context_from_snapshot(snapshot, runtime)
        async with runtime.unit_of_work() as uow:
            await uow.session.execute(
                text(
                    "UPDATE runtime_states SET payload_json = :payload, "
                    "payload_sha256 = :digest WHERE namespace = :namespace "
                    "AND state_key = :state_key"
                ),
                {
                    "payload": "{}",
                    "digest": hashlib.sha256(b"{}").hexdigest(),
                    "namespace": "life_engine.runtime_context",
                    "state_key": "global",
                },
            )
        with pytest.raises(RuntimeContextCopyError, match="target conflicts"):
            await copy_runtime_context_from_snapshot(snapshot, runtime)
    finally:
        await runtime.close()
        await registry.revoke(token)


async def test_local_copy_runtime_returns_revocable_bootstrap_authority(
    tmp_path: Path,
) -> None:
    runtime, registry, authority_token = await _open_local_copy_runtime(
        tmp_path / "candidate.sqlite3",
        tmp_path / "bootstrap-authority.json",
    )
    try:
        async with runtime.unit_of_work() as uow:
            await uow.session.execute(text("CREATE TABLE bootstrap_probe (id INTEGER)"))
    finally:
        await runtime.close()

    await registry.revoke(authority_token)
    health = await registry.health()

    assert health["active_generation"] == ""
    assert health["owner_id"] == ""
    with pytest.raises(StaleAuthorityToken):
        await registry.validate(authority_token)


async def test_runtime_context_copy_prefers_selected_database_checkpoint(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "selected-snapshot"
    database = snapshot / "sqlite" / "life_storage" / "local.sqlite3"
    database.parent.mkdir(parents=True)
    source = tmp_path / "source.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute(
        """CREATE TABLE runtime_states (
            namespace TEXT NOT NULL,
            state_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (namespace, state_key)
        )"""
    )
    payload = {
        "version": 2,
        "state": {
            "heartbeat_count": 237,
            "event_sequence": 859,
            "heartbeat_context_cursor": 856,
            "subconscious_summary": {"schema_version": 1, "entries": []},
        },
        "pending_events": [],
        "event_history": [],
    }
    encoded = canonical_json(payload)
    payload_sha256 = hashlib.sha256(encoded.encode()).hexdigest()
    connection.execute(
        "INSERT INTO runtime_states VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "life_engine.runtime_context",
            "global",
            717,
            2,
            encoded,
            payload_sha256,
            "2026-08-24T05:50:36+00:00",
        ),
    )
    connection.commit()
    connection.close()
    database.write_bytes(source.read_bytes())
    inspection = inspect_sqlite_database(database)

    legacy_payload = {
        **payload,
        "state": {
            **payload["state"],
            "heartbeat_count": 0,
            "event_sequence": 1,
            "heartbeat_context_cursor": 0,
        },
        "pending_events": [{"event_id": "legacy", "sequence": 1}],
    }
    legacy = snapshot / "workspace" / "life_engine_workspace" / "life_engine_context.json"
    legacy.parent.mkdir(parents=True)
    legacy_bytes = json.dumps(legacy_payload).encode()
    legacy.write_bytes(legacy_bytes)
    exact = {
        "source_relative": "life_engine_workspace/life_engine_context.json",
        "backup_relative": "workspace/life_engine_workspace/life_engine_context.json",
        "bytes": len(legacy_bytes),
        "sha256": hashlib.sha256(legacy_bytes).hexdigest(),
    }
    database_bytes = database.read_bytes()
    sqlite_entry = {
        "source_relative": "life_storage/local.sqlite3",
        "backup_relative": "sqlite/life_storage/local.sqlite3",
        "bytes": len(database_bytes),
        "sha256": hashlib.sha256(database_bytes).hexdigest(),
        "backup_bytes": len(database_bytes),
        "backup_sha256": hashlib.sha256(database_bytes).hexdigest(),
        "database_root_sha256": inspection["database_root_sha256"],
        "tables": inspection["tables"],
        "frontiers": inspection["frontiers"],
    }
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "writer_frozen": True,
        "source_snapshot_sha256": "b" * 64,
        "root_hashes": {
            "sqlite:life_storage/local.sqlite3": inspection[
                "database_root_sha256"
            ]
        },
        "frontiers": inspection["frontiers"],
        "sqlite": [sqlite_entry],
        "exact_files": [exact],
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        canonical_json(manifest).encode()
    ).hexdigest()
    (snapshot / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    runtime, registry, token = await _open_local_copy_runtime(
        tmp_path / "target.sqlite3",
        tmp_path / "authority.json",
    )
    await ensure_runtime_state_schema(runtime)
    try:
        report = await copy_runtime_context_from_snapshot(snapshot, runtime)
        record = await SQLRuntimeStateStore(runtime).get_state(
            "life_engine.runtime_context",
            "global",
        )
    finally:
        await runtime.close()
        await registry.revoke(token)

    assert report["source_relative"] == "life_storage/local.sqlite3"
    assert report["revision"] == 717
    assert report["frontiers"]["heartbeat_count"] == 237
    assert record is not None
    assert record.revision == 717
    assert record.schema_version == 2
    assert record.payload_sha256 == payload_sha256
    assert record.payload == payload
