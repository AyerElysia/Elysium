"""Lossless legacy ThoughtStream archive and candidate-copy contracts."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import plugins.life_engine.storage.attention_schema as attention_schema_module
from plugins.life_engine.storage.attention_migration import (
    AttentionLegacyMigrationError,
    create_legacy_attention_archive,
    export_legacy_attention_snapshot,
    import_legacy_attention_snapshot,
    load_legacy_attention_archive,
    verify_legacy_attention_import,
)
from plugins.life_engine.storage.attention_schema import (
    ensure_attention_thread_schema,
)
from plugins.life_engine.storage.authority import (
    FileAuthorityRegistry,
    StaleAuthorityToken,
)
from plugins.life_engine.storage.contracts import (
    StorageBackendRuntime,
    StorageRuntimeDisabled,
    StorageWriterRole,
)
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="attention-migration-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="a" * 64,
        root_hashes={"attention_threads": "b" * 64},
        frontiers={"attention_threads": 0},
        created_at="2026-08-06T00:00:00+00:00",
        verified_at="2026-08-06T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@asynccontextmanager
async def _candidate_runtime(tmp_path: Path) -> AsyncIterator[StorageBackendRuntime]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    generation = _generation()
    await registry.register_generation(generation)
    token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=0,
        owner_id="attention-migration-contract",
        lease_seconds=300,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=generation.generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_ATTENTION_MIGRATION_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "attention.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_ATTENTION_MIGRATION_FENCE": token.fencing_token},
    )
    runtime.writer_role = StorageWriterRole.CANDIDATE_COPY
    await ensure_attention_thread_schema(
        runtime,
        require_database_immutability=False,
    )
    try:
        yield runtime
    finally:
        await runtime.close()
        try:
            await registry.revoke(token)
        except StaleAuthorityToken:
            pass


def _legacy_row(stream_id: str, *, status: str, revision: int) -> dict[str, object]:
    return {
        "id": stream_id,
        "title": f"关注：{stream_id}\n第二行",
        "created_at": "2026-08-06T00:00:00+00:00",
        "last_advanced_at": "2026-08-06T00:01:00+00:00",
        "advance_count": revision,
        "curiosity_score": 0.75,
        "last_thought": "逐字节保留，不解释成主体历史。",
        "related_memories": ["memory:一", "memory:two"],
        "status": status,
        "last_focused_at": "",
        "last_decay_at": "",
        "revision": revision,
        "extension": {"emoji": "🌸", "line": "a\r\nb"},
    }


def _legacy_snapshot(path: Path) -> bytes:
    raw = (
        json.dumps(
            {
                "schema_version": 2,
                "global_revision": 7,
                "streams": [
                    _legacy_row("thread:active", status="active", revision=1),
                    _legacy_row("thread:dormant", status="dormant", revision=2),
                    _legacy_row("thread:completed", status="completed", revision=3),
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\r\n"
    ).encode("utf-8")
    path.write_bytes(raw)
    return raw


def test_legacy_attention_archive_is_exact_and_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "streams.json"
    raw = _legacy_snapshot(source)
    archive = tmp_path / "archive"

    report = create_legacy_attention_archive(source, archive)
    snapshot, manifest = load_legacy_attention_archive(archive)

    assert report.verified is True
    assert snapshot.raw_bytes == raw
    assert (archive / "streams.json").read_bytes() == raw
    assert source.read_bytes() == raw
    assert manifest["import_mode"] == "snapshot_only"
    assert manifest["history_claim"] == "snapshot_only_no_fabricated_events"
    assert manifest["generation_eligible"] is False
    assert manifest["status_counts"] == {
        "active": 1,
        "completed": 1,
        "dormant": 1,
    }

    (archive / "streams.json").write_bytes(raw + b"\n")
    with pytest.raises(AttentionLegacyMigrationError, match="differs"):
        load_legacy_attention_archive(archive)


def test_incomplete_archive_is_never_consumable(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "ATTENTION_ARCHIVE_INCOMPLETE").write_text("incomplete")

    with pytest.raises(AttentionLegacyMigrationError, match="incomplete"):
        load_legacy_attention_archive(archive)


async def test_candidate_import_is_exact_idempotent_and_not_history(
    tmp_path: Path,
) -> None:
    source = tmp_path / "streams.json"
    raw = _legacy_snapshot(source)
    async with _candidate_runtime(tmp_path) as runtime:
        first = await import_legacy_attention_snapshot(source, runtime)
        replay = await import_legacy_attention_snapshot(source, runtime)
        verification = await verify_legacy_attention_import(source, runtime)

        assert first.verified is True
        assert first.idempotent_replay is False
        assert replay.idempotent_replay is True
        assert replay.canonical_event_count_before == 0
        assert replay.canonical_event_count_after == 0
        assert replay.canonical_head_count == 0
        assert replay.canonical_focus_count == 0
        assert len(replay.canonical_root_sha256) == 64
        assert verification == {
            "verified": True,
            "snapshot_sha256": first.snapshot_sha256,
            "byte_length": len(raw),
            "row_count": 3,
            "rows_root_sha256": first.rows_root_sha256,
            "exact_bytes_match": True,
            "candidate_rows_match": True,
            "metadata_match": True,
            "import_mode": "snapshot_only",
            "generation_eligible": False,
        }
        async with runtime.unit_of_work() as uow:
            canonical_events = await uow.session.scalar(
                text("SELECT COUNT(*) FROM attention_thread_events")
            )
            candidates = (
                await uow.session.execute(
                    text(
                        """SELECT legacy_status, candidate_state
                        FROM attention_legacy_candidates
                        ORDER BY source_ordinal"""
                    )
                )
            ).all()
        assert canonical_events == 0
        assert candidates == [
            ("active", "snapshot_only"),
            ("dormant", "snapshot_only"),
            ("completed", "snapshot_only"),
        ]
        assert source.read_bytes() == raw

        reverse = tmp_path / "reverse"
        exported = await export_legacy_attention_snapshot(
            runtime,
            snapshot_sha256=first.snapshot_sha256,
            archive_directory=reverse,
        )
        assert exported.verified is True
        assert (reverse / "streams.json").read_bytes() == raw


async def test_candidate_import_rejects_active_writer(tmp_path: Path) -> None:
    source = tmp_path / "streams.json"
    _legacy_snapshot(source)
    async with _candidate_runtime(tmp_path) as runtime:
        runtime.writer_role = StorageWriterRole.ACTIVE
        with pytest.raises(AttentionLegacyMigrationError, match="candidate-copy"):
            await import_legacy_attention_snapshot(source, runtime)


async def test_legacy_candidate_evidence_is_database_immutable(tmp_path: Path) -> None:
    source = tmp_path / "streams.json"
    _legacy_snapshot(source)
    async with _candidate_runtime(tmp_path) as runtime:
        report = await import_legacy_attention_snapshot(source, runtime)
        with pytest.raises(DBAPIError, match="AttentionLegacySnapshotImmutable"):
            async with runtime.unit_of_work() as uow:
                await uow.session.execute(
                    text(
                        """UPDATE attention_legacy_snapshots
                        SET source_label = 'changed'
                        WHERE snapshot_sha256 = :snapshot_sha256"""
                    ),
                    {"snapshot_sha256": report.snapshot_sha256},
                )
        verified = await verify_legacy_attention_import(source, runtime)
        assert verified["verified"] is True


async def test_mysql_candidate_schema_requires_fence_and_applies_both_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = 0
    applications: list[tuple[object, ...]] = []

    async def validate() -> None:
        nonlocal validations
        validations += 1

    class _Runner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def apply(self, migrations: tuple[object, ...]) -> None:
            applications.append(migrations)

    monkeypatch.setattr(attention_schema_module, "MySQLMigrationRunner", _Runner)
    runtime = StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.MYSQL,
        backend_identity="mysql://candidate",
        generation=None,
        authority_registry=None,
        authority_token=None,
        engine=object(),  # type: ignore[arg-type]
        session_factory=None,
        _writer_validator=validate,
        writer_role=StorageWriterRole.CANDIDATE_COPY,
        writer_epoch=3,
    )

    await ensure_attention_thread_schema(
        runtime,
        require_database_immutability=False,
    )

    assert validations == 2
    assert len(applications) == 1
    assert [migration.version for migration in applications[0]] == [1, 2]  # type: ignore[attr-defined]

    runtime._writer_validator = None
    with pytest.raises(StorageRuntimeDisabled, match="no writer authority"):
        await ensure_attention_thread_schema(
            runtime,
            require_database_immutability=False,
        )


async def test_opt_in_real_legacy_snapshot_round_trip(tmp_path: Path) -> None:
    configured = os.environ.get("ELYSIUM_TEST_LEGACY_ATTENTION_SNAPSHOT", "").strip()
    if not configured:
        pytest.skip("real legacy Attention snapshot is not configured")
    source = Path(configured)
    source_before = source.read_bytes()
    archive = create_legacy_attention_archive(source, tmp_path / "archive")

    async with _candidate_runtime(tmp_path) as runtime:
        copied = await import_legacy_attention_snapshot(source, runtime)
        verified = await verify_legacy_attention_import(source, runtime)
        reverse = await export_legacy_attention_snapshot(
            runtime,
            snapshot_sha256=copied.snapshot_sha256,
            archive_directory=tmp_path / "reverse",
        )

    assert archive.snapshot_sha256 == copied.snapshot_sha256
    assert copied.row_count == archive.row_count
    assert verified["verified"] is True
    assert reverse.verified is True
    assert (tmp_path / "reverse/streams.json").read_bytes() == source_before
    assert source.read_bytes() == source_before
