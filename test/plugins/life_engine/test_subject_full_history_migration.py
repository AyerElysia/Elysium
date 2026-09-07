"""Small synthetic-only tests for exact schema-v5 subject history bundles."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from plugins.life_engine.storage.contracts import (
    StorageBackendRuntime,
    StorageWriterRole,
)
from plugins.life_engine.storage.migration import subject_history as history
from plugins.life_engine.storage.models import BackendKind
from plugins.life_engine.storage.subject_schema import ensure_subject_document_schema
from src.kernel.storage import SQLiteStorageConfig, create_sqlite_storage_engine

_TIME = "2026-09-07T09:00:00.123456+00:00"
_NOTE = "life_engine_workspace/fixture-note.md"
_ARCHIVE = "life_engine_workspace/fixture-archive.md"
_COPY = "life_engine_workspace/fixture-copy.bin"
_ROOT = "life_engine_workspace/MEMORY.md"
_TABLES = (
    "subject_documents",
    "subject_document_versions",
    "subject_document_head_events",
    "subject_projection_outbox",
    "subject_authority_decisions",
    "subject_document_path_bindings",
    "subject_document_path_events",
    "subject_document_operations",
)


def _rows() -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in _TABLES}
    versions: dict[str, dict[str, Any]] = {}

    def add_version(
        identity: str, owner: str, path: str, parent: str, raw: bytes
    ) -> None:
        row = {
            "version_id": identity,
            "document_id": owner,
            "logical_path": path,
            "parent_version_id": parent,
            "occurrence_id": "occ:" + identity,
            "semantic_actor_id": None,
            "semantic_source_id": None,
            "occurred_at": None,
            "recorded_by": "synthetic-fixture",
            "recorded_source": "test:exact-history",
            "recorded_at": _TIME,
            "provenance_status": "semantic_source_missing",
            "content_bytes": raw,
            "content_hash": hashlib.sha256(raw).hexdigest(),
            "byte_length": len(raw),
            "byte_fidelity": "exact_bytes",
            "encoding": None,
            "newline_style": None,
            "change_context_json": {"fixture": True},
        }
        versions[identity] = row
        rows["subject_document_versions"].append(row)

    add_version("old-v1", "old", _NOTE, "", b"\xef\xbb\xbfsynthetic\r\n")
    add_version("old-v2", "old", _ARCHIVE, "old-v1", b"\x00\xff\x80binary\r\n")
    add_version("new-v1", "new", _NOTE, "", b"")
    add_version("copy-v1", "copy", _COPY, "", versions["old-v2"]["content_bytes"])
    versions["copy-v1"]["change_context_json"] = {
        "copied_from_document_id": "old",
        "copied_from_version_id": "old-v2",
    }
    add_version("root-v1", "root", _ROOT, "", b"# synthetic authority fixture\n")
    versions["root-v1"].update(
        {
            "semantic_actor_id": "synthetic-instance",
            "semantic_source_id": "test:decision",
            "occurred_at": _TIME,
            "provenance_status": "complete",
        }
    )

    def add_head(
        owner: str, path: str, current: str, revision: int, binding: int, deleted: int
    ) -> None:
        rows["subject_documents"].append(
            {
                "document_id": owner,
                "logical_path": path,
                "declared_owner": "synthetic-owner",
                "current_version_id": current,
                "revision": revision,
                "binding_revision": binding,
                "is_deleted": deleted,
            }
        )

    add_head("old", _ARCHIVE, "old-v2", 4, 2, 1)
    add_head("new", _NOTE, "new-v1", 1, 3, 0)
    add_head("copy", _COPY, "copy-v1", 1, 1, 0)
    add_head("root", _ROOT, "root-v1", 1, 1, 0)
    event_specs = (
        ("old", "old-v1", "", _NOTE, "write", 1, 1),
        ("old", "old-v1", "old-v1", _ARCHIVE, "rename", 2, 1),
        ("old", "old-v2", "old-v1", _ARCHIVE, "write", 3, 1),
        ("old", "old-v2", "old-v2", _ARCHIVE, "delete", 4, 2),
        ("new", "new-v1", "", _NOTE, "write", 1, 3),
        ("copy", "copy-v1", "", _COPY, "copy", 1, 1),
        ("root", "root-v1", "", _ROOT, "write", 1, 1),
    )
    for number, (
        owner,
        current,
        previous,
        path,
        operation,
        revision,
        binding,
    ) in enumerate(event_specs, 1):
        event_id = f"event-{number}"
        occurrence = f"event-occurrence-{number}"
        rows["subject_document_head_events"].append(
            {
                "head_event_id": event_id,
                "document_id": owner,
                "previous_version_id": previous,
                "next_version_id": current,
                "occurrence_id": occurrence,
                "actor_id": "synthetic-fixture",
                "source_id": "test:exact-history",
                "occurred_at": _TIME,
                "authority_epoch": 7,
                "change_context_json": {"operation": operation},
            }
        )
        rows["subject_projection_outbox"].append(
            {
                "outbox_id": number,
                "head_event_id": event_id,
                "document_id": owner,
                "logical_path": path,
                "version_id": current,
                "content_hash": versions[current]["content_hash"],
                "state": "pending",
                "attempt_count": 0,
                "created_at": _TIME,
                "confirmed_at": "",
                "last_error": "",
                "lease_owner": "",
                "lease_until": "",
                "revision": 0,
                "operation": operation,
                "previous_logical_path": _NOTE if operation == "rename" else "",
                "binding_revision": binding,
                "previous_binding_revision": max(0, binding - 1),
                "previous_version_id": previous,
                "previous_content_hash": versions[previous]["content_hash"]
                if previous
                else "",
            }
        )
        rows["subject_document_operations"].append(
            {
                "occurrence_id": occurrence,
                "operation": operation,
                "document_id": owner,
                "command_digest": hashlib.sha256(occurrence.encode()).hexdigest(),
                "result_json": {
                    "head": {
                        "document_id": owner,
                        "logical_path": path,
                        "declared_owner": "synthetic-owner",
                        "current_version_id": current,
                        "revision": revision,
                        "binding_revision": binding,
                        "deleted": operation == "delete",
                    },
                    "version_id": current,
                },
                "change_context_json": {"fixture": True},
                "recorded_at": _TIME,
            }
        )
    rows["subject_projection_outbox"][0].update(
        {"state": "confirmed", "confirmed_at": _TIME}
    )
    rows["subject_projection_outbox"][2].update(
        {"lease_owner": "synthetic-lease", "lease_until": _TIME}
    )
    root_hash = versions["root-v1"]["content_hash"]
    rows["subject_authority_decisions"].append(
        {
            "decision_occurrence_id": "synthetic-decision",
            "authority_occurrence_id": "synthetic-authority",
            "candidate_id": "synthetic-candidate",
            "candidate_revision": 1,
            "candidate_sha256": "a" * 64,
            "candidate_occurrence_id": "synthetic-candidate-occurrence",
            "actor_consciousness_instance_id": "synthetic-instance",
            "expected_subject_revision": "b" * 64,
            "target_path": "MEMORY.md",
            "accepted_content_sha256": root_hash,
            "occurred_at": _TIME,
            "previous_subject_revision": "b" * 64,
            "new_subject_revision": "c" * 64,
            "document_version_id": "root-v1",
            "document_revision": 1,
            "command_sha256": "d" * 64,
            "committed_at": _TIME,
        }
    )
    for path, owner, revision in (
        (_NOTE, "new", 3),
        (_ARCHIVE, None, 2),
        (_COPY, "copy", 1),
        (_ROOT, "root", 1),
    ):
        rows["subject_document_path_bindings"].append(
            {"logical_path": path, "document_id": owner, "revision": revision}
        )
    for path, identities in (
        (_NOTE, ("old", None, "new")),
        (_ARCHIVE, ("old", None)),
        (_COPY, ("copy",)),
        (_ROOT, ("root",)),
    ):
        previous = None
        for revision, identity in enumerate(identities, 1):
            rows["subject_document_path_events"].append(
                {
                    "event_id": f"path:{path}:{revision}",
                    "logical_path": path,
                    "previous_document_id": previous,
                    "document_id": identity,
                    "previous_revision": revision - 1,
                    "revision": revision,
                    "occurrence_id": f"path-occ:{path}:{revision}",
                    "recorded_at": _TIME,
                }
            )
            previous = identity
    return rows


def _bundle(
    rows: dict[str, list[dict[str, Any]]] | None = None, **limits: Any
) -> bytes:
    return history.encode_subject_history_bundle(
        rows if rows is not None else _rows(),
        source_identity_sha256="0" * 64,
        captured_at=_TIME,
        **limits,
    )


@asynccontextmanager
async def _candidate(path: Path) -> AsyncIterator[StorageBackendRuntime]:
    engine = create_sqlite_storage_engine(SQLiteStorageConfig(database_path=path))

    async def fence(_session: Any) -> None:
        return None

    async def validate() -> None:
        return None

    runtime = StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.LOCAL,
        backend_identity="synthetic-candidate",
        generation=None,
        authority_registry=None,
        authority_token=None,
        engine=engine,
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        _write_fence=fence,
        _writer_validator=validate,
        writer_role=StorageWriterRole.CANDIDATE_COPY,
        writer_epoch=1,
    )
    try:
        await ensure_subject_document_schema(runtime)
        yield runtime
    finally:
        await runtime.close()


async def _seed(
    runtime: StorageBackendRuntime, rows: dict[str, list[dict[str, Any]]]
) -> None:
    async with runtime.unit_of_work() as uow:
        for table in _TABLES:
            for row in rows[table]:
                names = tuple(row)
                await uow.session.execute(
                    text(
                        f"INSERT INTO {table} ({', '.join(names)}) "
                        f"VALUES ({', '.join(':' + name for name in names)})"
                    ),
                    {
                        name: json.dumps(value) if name.endswith("_json") else value
                        for name, value in row.items()
                    },
                )


async def _counts(runtime: StorageBackendRuntime) -> dict[str, int]:
    assert runtime.engine is not None
    async with runtime.engine.connect() as connection:
        return {
            table: int(await connection.scalar(text(f"SELECT COUNT(*) FROM {table}")))
            for table in _TABLES
        }


def test_history_bundle_covers_deleted_reused_and_binary_history() -> None:
    payload = _bundle()
    receipt = history.verify_subject_history_bundle(payload)
    assert receipt.table_counts == {table: len(rows) for table, rows in _rows().items()}
    decoded = json.loads(payload)
    assert decoded["scope"] == "subject-domain-only"
    versions = {
        row["version_id"]: row
        for row in decoded["tables"]["subject_document_versions"]["rows"]
    }
    assert (
        base64.b64decode(versions["old-v1"]["content_bytes"])
        == b"\xef\xbb\xbfsynthetic\r\n"
    )
    assert (
        base64.b64decode(versions["old-v2"]["content_bytes"])
        == b"\x00\xff\x80binary\r\n"
    )
    assert base64.b64decode(versions["new-v1"]["content_bytes"]) == b""
    assert versions["old-v1"]["semantic_actor_id"] is None
    assert versions["old-v1"]["semantic_source_id"] is None
    assert versions["old-v1"]["occurred_at"] is None
    assert versions["old-v1"]["recorded_at"] == _TIME
    assert versions["old-v1"]["logical_path"] == _NOTE
    assert versions["old-v2"]["logical_path"] == _ARCHIVE
    assert (
        versions["copy-v1"]["change_context_json"]["copied_from_version_id"] == "old-v2"
    )


@pytest.mark.parametrize(
    "damage, message",
    [
        ("bytes", "checksum"),
        ("parent", "version reference"),
        ("cross_parent", "cross-document"),
        ("cycle", "cycle"),
        ("path_gap", "discontinuous"),
        ("missing_outbox", "projection record"),
        ("duplicate", "duplicate"),
        ("unknown_column", "columns"),
    ],
)
def test_history_rejects_incomplete_or_corrupt_fixture(
    damage: str, message: str
) -> None:
    rows = _rows()
    if damage == "bytes":
        rows["subject_document_versions"][0]["content_bytes"] = b"altered"
    elif damage == "parent":
        rows["subject_document_versions"][1]["parent_version_id"] = "absent"
    elif damage == "cross_parent":
        rows["subject_document_versions"][1]["parent_version_id"] = "new-v1"
    elif damage == "cycle":
        rows["subject_document_versions"][0]["parent_version_id"] = "old-v2"
    elif damage == "path_gap":
        rows["subject_document_path_events"].pop(1)
    elif damage == "missing_outbox":
        rows["subject_projection_outbox"].pop()
    elif damage == "duplicate":
        rows["subject_document_versions"].append(
            copy.deepcopy(rows["subject_document_versions"][0])
        )
    elif damage == "unknown_column":
        rows["subject_documents"][0]["future_column"] = "cannot omit"
    with pytest.raises(history.SubjectHistoryError, match=message):
        _bundle(rows)


def test_history_checks_manifest_limits_and_timestamp_types() -> None:
    payload = _bundle()
    changed = json.loads(payload)
    changed["tables"]["subject_documents"]["rows"][0]["declared_owner"] = "altered"
    with pytest.raises(history.SubjectHistoryError, match="bundle checksum"):
        history.verify_subject_history_bundle(json.dumps(changed).encode())
    with pytest.raises(history.SubjectHistoryError, match="duplicate key"):
        history.verify_subject_history_bundle(b'{"format":1,"format":2}')
    with pytest.raises(history.SubjectHistoryError, match="row limit"):
        _bundle(max_rows=1)
    with pytest.raises(history.SubjectHistoryError, match="byte limit"):
        _bundle(max_bytes=100)
    mysql_shaped = _rows()
    mysql_shaped["subject_document_versions"][0]["recorded_at"] = datetime(
        2026, 9, 7, 9, 0, 0, 123456, tzinfo=UTC
    ).replace(tzinfo=None)
    mysql_shaped["subject_document_versions"][0]["change_context_json"] = (
        '{ "fixture" : true }'
    )
    assert _bundle(mysql_shaped) == payload
    assert history._bind_value("timestamp", _TIME, BackendKind.MYSQL) == datetime(
        2026, 9, 7, 9, 0, 0, 123456, tzinfo=UTC
    ).replace(tzinfo=None)
    assert history._bind_value("nullable_timestamp", None, BackendKind.MYSQL) is None
    assert datetime.fromisoformat(_TIME).tzinfo == UTC


async def test_candidate_exact_roundtrip_replay_and_private_export(
    tmp_path: Path,
) -> None:
    async with (
        _candidate(tmp_path / "source.sqlite3") as source,
        _candidate(tmp_path / "target.sqlite3") as target,
    ):
        await _seed(source, _rows())
        before = await _counts(source)
        directory = tmp_path / "history-bundle"
        exported = await history.export_subject_history(source, directory)
        assert not (directory / "SUBJECT_HISTORY_INCOMPLETE").exists()
        original_file = (directory / "subject-history.json").read_bytes()
        imported = await history.import_subject_history(directory, target)
        assert imported.table_counts == before
        assert imported.table_roots == exported.table_roots
        assert not imported.idempotent_replay
        replay = await history.import_subject_history(directory, target)
        assert replay.idempotent_replay
        assert replay.bundle_sha256 == imported.bundle_sha256
        assert await _counts(source) == await _counts(target) == before
        target_export = history.verify_subject_history_bundle(
            await history.capture_subject_history(target)
        )
        assert target_export.table_roots == exported.table_roots
        assert target.authority_token is None and target.generation is None
        assert target.writer_role == StorageWriterRole.CANDIDATE_COPY
        with pytest.raises(FileExistsError):
            await history.export_subject_history(source, directory)
        assert (directory / "subject-history.json").read_bytes() == original_file
        assert target.engine is not None
        async with target.engine.connect() as connection:
            preserved = (
                await connection.execute(
                    text(
                        "SELECT content_bytes, semantic_actor_id, semantic_source_id, occurred_at, recorded_at "
                        "FROM subject_document_versions WHERE version_id = 'old-v1'"
                    )
                )
            ).one()
            assert tuple(preserved) == (
                b"\xef\xbb\xbfsynthetic\r\n",
                None,
                None,
                None,
                _TIME,
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT is_deleted FROM subject_documents WHERE document_id = 'old'"
                    )
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT document_id FROM subject_document_path_bindings WHERE logical_path = :path"
                    ),
                    {"path": _NOTE},
                )
                == "new"
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT lease_owner FROM subject_projection_outbox WHERE outbox_id = 3"
                    )
                )
                == "synthetic-lease"
            )


async def test_history_rejects_active_missing_fence_and_same_source(
    tmp_path: Path,
) -> None:
    async with _candidate(tmp_path / "candidate.sqlite3") as target:
        for denied in (
            replace(target, writer_role=StorageWriterRole.ACTIVE),
            replace(target, _write_fence=None),
        ):
            with pytest.raises(history.SubjectHistoryError, match="CANDIDATE_COPY"):
                await history.import_subject_history_bundle(_bundle(), denied)
        assert all(count == 0 for count in (await _counts(target)).values())
        await _seed(target, _rows())
        payload = await history.capture_subject_history(target)
        with pytest.raises(history.SubjectHistoryError, match="same database"):
            await history.import_subject_history_bundle(payload, target)


@pytest.mark.parametrize("extra", [False, True])
async def test_history_does_not_replace_conflicting_or_new_target_history(
    tmp_path: Path, extra: bool
) -> None:
    async with _candidate(tmp_path / "candidate.sqlite3") as target:
        seeded = {table: [] for table in _TABLES}
        head = copy.deepcopy(_rows()["subject_documents"][0])
        if extra:
            head["document_id"] = "later-history"
        else:
            head["declared_owner"] = "conflicting-record"
        seeded["subject_documents"].append(head)
        await _seed(target, seeded)
        before = await _counts(target)
        with pytest.raises(history.SubjectHistoryError, match="extra or conflicting"):
            await history.import_subject_history_bundle(_bundle(), target)
        assert await _counts(target) == before


@pytest.mark.parametrize("cancelled", [False, True])
async def test_candidate_lost_fence_or_cancel_rolls_back_all_insertions(
    tmp_path: Path, cancelled: bool
) -> None:
    async with _candidate(tmp_path / "candidate.sqlite3") as target:
        checks = 0

        async def failing_fence(_session: Any) -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                if cancelled:
                    raise asyncio.CancelledError("synthetic cancellation")
                raise RuntimeError("synthetic copy fence expired")

        target._write_fence = failing_fence
        exception = asyncio.CancelledError if cancelled else RuntimeError
        with pytest.raises(exception):
            await history.import_subject_history_bundle(_bundle(), target)
        assert checks == 2
        assert all(count == 0 for count in (await _counts(target)).values())


async def test_post_insert_verification_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _candidate(tmp_path / "candidate.sqlite3") as target:
        original = history._bind_value

        def damaged(kind: str, value: Any, backend: BackendKind) -> Any:
            return (
                b"synthetic driver damage"
                if kind == "binary"
                else original(kind, value, backend)
            )

        monkeypatch.setattr(history, "_bind_value", damaged)
        with pytest.raises(history.SubjectHistoryError, match="verification mismatch"):
            await history.import_subject_history_bundle(_bundle(), target)
        assert all(count == 0 for count in (await _counts(target)).values())


async def test_snapshot_keeps_one_read_view_during_other_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _candidate(tmp_path / "source.sqlite3") as source:
        await _seed(source, _rows())
        original = history._read_tables

        async def interleaved(executor: Any, **limits: Any) -> Any:
            await executor.execute(text("SELECT document_id FROM subject_documents"))
            assert source.engine is not None
            async with source.engine.begin() as writer:
                await writer.execute(
                    text(
                        "UPDATE subject_documents SET declared_owner = 'later-fixture-state'"
                    )
                )
            return await original(executor, **limits)

        monkeypatch.setattr(history, "_read_tables", interleaved)
        captured = json.loads(await history.capture_subject_history(source))
        assert {
            row["declared_owner"]
            for row in captured["tables"]["subject_documents"]["rows"]
        } == {"synthetic-owner"}
        assert source.engine is not None
        async with source.engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT declared_owner FROM subject_documents LIMIT 1")
                )
                == "later-fixture-state"
            )


async def test_mislabelled_candidate_cannot_import_into_active_database(
    tmp_path: Path,
) -> None:
    async with _candidate(tmp_path / "candidate.sqlite3") as target:
        assert target.engine is not None
        async with target.engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE storage_authority_registry (active_generation TEXT NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO storage_authority_registry VALUES ('synthetic-active-generation')"
                )
            )
        with pytest.raises(history.SubjectHistoryError, match="active authority"):
            await history.import_subject_history_bundle(_bundle(), target)
        assert all(count == 0 for count in (await _counts(target)).values())


async def test_incomplete_directory_and_unknown_schema_fail_closed(
    tmp_path: Path,
) -> None:
    async with _candidate(tmp_path / "candidate.sqlite3") as target:
        directory = tmp_path / "incomplete"
        directory.mkdir()
        (directory / "SUBJECT_HISTORY_INCOMPLETE").touch()
        with pytest.raises(history.SubjectHistoryError, match="incomplete"):
            await history.import_subject_history(directory, target)
        assert target.engine is not None
        async with target.engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE subject_future_history (id INTEGER)")
            )
        with pytest.raises(history.SubjectHistoryError, match="table set"):
            await history.capture_subject_history(target)
