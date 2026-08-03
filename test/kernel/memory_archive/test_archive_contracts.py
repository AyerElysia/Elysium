from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest

from src.kernel.memory_archive.coordinator import (
    ArchiveVerificationError,
    MemoryArchiveCoordinator,
)
from src.kernel.memory_archive.models import (
    ArchiveMode,
    ArchivePublishResult,
    ArchiveRecord,
)
from src.kernel.memory_archive.restore import (
    ArchiveRestoreError,
    restore_sqlite_domain,
    restore_workspace,
)
from src.kernel.memory_archive.sources import (
    iter_sqlite_records,
    iter_workspace_records,
    sqlite_table_archive_contract,
)
from src.kernel.memory_archive.state import ArchiveState


class _HeadRemote:
    def __init__(self, records: list[ArchiveRecord]) -> None:
        self.rows = list(enumerate(records, start=1))

    async def fetch_heads(
        self,
        *,
        source_node_id: str,
        source_domain: str,
        after_position: int = 0,
        limit: int = 1000,
    ) -> list[tuple[int, ArchiveRecord]]:
        return [
            (position, record)
            for position, record in self.rows
            if position > after_position
            and record.source_node_id == source_node_id
            and record.source_domain == source_domain
        ][:limit]


class _ConcurrentPublishRemote:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.finished: dict[str, object] = {}

    async def initialize(self) -> None:
        return None

    async def start_run(self, *_: object, **__: object) -> None:
        return None

    async def publish_batch(
        self,
        records: list[ArchiveRecord],
        *,
        manifest_id: str,
        starting_ordinal: int,
        update_projections: bool = True,
    ) -> list[ArchivePublishResult]:
        del manifest_id, update_projections
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep((10 - starting_ordinal) / 1000)
        self.active -= 1
        return [
            ArchivePublishResult(
                record_id=record.record_id,
                status="accepted",
                archive_position=starting_ordinal + index + 1,
            )
            for index, record in enumerate(records)
        ]

    async def finish_run(self, manifest_id: str, **values: object) -> None:
        self.finished = {"manifest_id": manifest_id, **values}

    async def verify_run(self, manifest_id: str) -> dict[str, object]:
        return {"manifest_id": manifest_id, "verified": True}

    async def finalize_full_snapshot(self, manifest_id: str) -> None:
        del manifest_id


class _VerificationFailRemote(_ConcurrentPublishRemote):
    def __init__(self) -> None:
        super().__init__()
        self.marked_failed = False

    async def verify_run(self, manifest_id: str) -> dict[str, object]:
        return {"manifest_id": manifest_id, "verified": False}

    async def mark_run_verification_failed(
        self,
        manifest_id: str,
        error_summary: str,
    ) -> None:
        del manifest_id, error_summary
        self.marked_failed = True


class _ThreadBoundRecords:
    def __init__(self, records: list[ArchiveRecord]) -> None:
        self._records = iter(records)
        self.owner_thread_id: int | None = None

    def __iter__(self):
        return self

    def __next__(self) -> ArchiveRecord:
        current = threading.get_ident()
        if self.owner_thread_id is None:
            self.owner_thread_id = current
        if current != self.owner_thread_id:
            raise RuntimeError("iterator advanced from a different thread")
        return next(self._records)


def _record(*, mode: ArchiveMode, payload: dict) -> ArchiveRecord:
    return ArchiveRecord.build(
        source_node_id="node-proof",
        source_domain="life_memory",
        record_kind="proof",
        logical_key="same-logical-key",
        mode=mode,
        source_sequence=1,
        recorded_at="2026-08-03T00:00:00+08:00",
        visibility="owner_private",
        archive_role="immutable_history_replica",
        payload=payload,
    )


def test_archive_identity_distinguishes_immutable_conflict_from_version() -> None:
    immutable_one = _record(mode=ArchiveMode.IMMUTABLE, payload={"value": 1})
    immutable_two = _record(mode=ArchiveMode.IMMUTABLE, payload={"value": 2})
    version_one = _record(mode=ArchiveMode.VERSIONED, payload={"value": 1})
    version_two = _record(mode=ArchiveMode.VERSIONED, payload={"value": 2})

    assert immutable_one.record_id == immutable_two.record_id
    assert immutable_one.payload_hash != immutable_two.payload_hash
    assert immutable_one.immutable_key == immutable_two.immutable_key
    assert version_one.record_id != version_two.record_id
    assert version_one.immutable_key is None
    wire = immutable_one.as_dict()
    assert wire["authority"] == "immutable_history_replica"
    assert "archive_role" not in wire


def test_archive_contracts_never_promote_unknown_tables() -> None:
    history = sqlite_table_archive_contract("life_memory", "memory_claims")
    projection = sqlite_table_archive_contract(
        "life_memory", "memory_association_projection"
    )
    unknown = sqlite_table_archive_contract("life_memory", "memory_future_shape")

    assert history.mode is ArchiveMode.IMMUTABLE
    assert history.archive_role == "immutable_history_replica"
    assert projection.mode is ArchiveMode.VERSIONED
    assert projection.archive_role == "rebuildable_projection"
    assert unknown.mode is ArchiveMode.VERSIONED
    assert unknown.archive_role == "unclassified_storage_record"


@pytest.mark.asyncio
async def test_sqlite_archive_restores_rows_and_rejects_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE proof (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                payload BLOB NOT NULL
            );
            CREATE INDEX idx_proof_title ON proof(title);
            """
        )
        connection.execute(
            "INSERT INTO proof(id, title, payload) VALUES (?, ?, ?)",
            (1, "爱莉", b"\x00\xffmemory"),
        )

    records = list(
        iter_sqlite_records(
            source,
            source_node_id="node-proof",
            domain="core",
        )
    )
    assert {record.record_kind for record in records} == {
        "sqlite_schema:table",
        "sqlite_schema:index",
        "sqlite_row:proof",
    }

    remote = _HeadRemote(records)
    restored = tmp_path / "restored.db"
    result = await restore_sqlite_domain(
        remote,  # type: ignore[arg-type]
        source_node_id="node-proof",
        source_domain="core",
        output=restored,
    )
    with sqlite3.connect(restored) as connection:
        assert connection.execute(
            "SELECT id, title, payload FROM proof"
        ).fetchall() == [(1, "爱莉", b"\x00\xffmemory")]
    assert result["integrity_check"] == "ok"
    assert result["record_identity_check"] == "ok"
    with pytest.raises(ArchiveRestoreError, match="already exists"):
        await restore_sqlite_domain(
            remote,  # type: ignore[arg-type]
            source_node_id="node-proof",
            source_domain="core",
            output=restored,
        )


@pytest.mark.asyncio
async def test_workspace_archive_restores_inline_and_chunked_files(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    soul = data_root / "life_engine_workspace/SOUL.md"
    soul.parent.mkdir(parents=True)
    soul.write_bytes("爱莉的原文\n".encode())
    large = data_root / "life_engine_workspace/received/proof.bin"
    large.parent.mkdir(parents=True)
    large.write_bytes(bytes(range(256)) * 4097)
    external_diary = data_root / "diaries/2026-08/2026-08-04.md"
    external_diary.parent.mkdir(parents=True)
    external_diary.write_text("逐字节日记", encoding="utf-8")

    records = list(iter_workspace_records(data_root, source_node_id="node-proof"))
    assert sum(record.record_kind == "workspace_file" for record in records) == 3
    assert sum(record.record_kind == "workspace_file_chunk" for record in records) >= 2
    roles = {
        record.payload()["path"]: record.archive_role
        for record in records
        if record.record_kind == "workspace_file"
    }
    assert roles["life_engine_workspace/SOUL.md"] == (
        "declared_subject_artifact_exact_bytes"
    )
    assert roles["life_engine_workspace/received/proof.bin"] == (
        "unclassified_workspace_exact_bytes"
    )
    assert roles["diaries/2026-08/2026-08-04.md"] == (
        "declared_subject_artifact_exact_bytes"
    )

    output = tmp_path / "restore"
    result = await restore_workspace(
        _HeadRemote(records),  # type: ignore[arg-type]
        source_node_id="node-proof",
        output_root=output,
    )
    assert (output / "life_engine_workspace/SOUL.md").read_bytes() == soul.read_bytes()
    assert (
        output / "life_engine_workspace/received/proof.bin"
    ).read_bytes() == large.read_bytes()
    assert (output / "diaries/2026-08/2026-08-04.md").read_bytes() == (
        external_diary.read_bytes()
    )
    assert result["files"] == 3
    assert result["byte_hash_check"] == "ok"


def test_archive_state_only_skips_exact_remote_acknowledgements(
    tmp_path: Path,
) -> None:
    state = ArchiveState(tmp_path / "archive-state.sqlite3")
    first = _record(mode=ArchiveMode.VERSIONED, payload={"value": 1})
    changed = _record(mode=ArchiveMode.VERSIONED, payload={"value": 2})
    result = ArchivePublishResult(
        record_id=first.record_id,
        status="accepted",
        archive_position=7,
    )
    state.remember([first], [result])

    assert state.exact_known_ids([first, changed]) == {first.record_id}
    assert state.health()["known_records"] == 1
    assert state.node_id() == state.node_id()


@pytest.mark.asyncio
async def test_concurrent_publisher_preserves_manifest_hash_order(
    tmp_path: Path,
) -> None:
    state = ArchiveState(tmp_path / "archive-state.sqlite3")
    node_id = state.node_id()
    records = [
        ArchiveRecord.build(
            source_node_id=node_id,
            source_domain="proof",
            record_kind="concurrency",
            logical_key=str(index),
            mode=ArchiveMode.VERSIONED,
            source_sequence=index,
            recorded_at="",
            visibility="owner_private",
            archive_role="test",
            payload={"index": index},
        )
        for index in range(6)
    ]
    remote = _ConcurrentPublishRemote()
    thread_bound_records = _ThreadBoundRecords(records)
    main_thread_id = threading.get_ident()
    coordinator = MemoryArchiveCoordinator(
        state,
        remote,  # type: ignore[arg-type]
        publish_batch_size=2,
        scan_batch_size=6,
        publish_concurrency=3,
    )

    summary = await coordinator.synchronize(
        lambda: thread_bound_records,
        full_snapshot=True,
    )

    expected = hashlib.sha256()
    for record in records:
        expected.update(record.record_id.encode("ascii"))
        expected.update(b":")
        expected.update(record.payload_hash.encode("ascii"))
        expected.update(b"\n")
    assert remote.max_active == 3
    assert thread_bound_records.owner_thread_id != main_thread_id
    assert summary.root_hash == expected.hexdigest()
    assert summary.source_counts["role:test"] == 6
    assert remote.finished["root_hash"] == expected.hexdigest()
    assert state.health()["known_records"] == 6


@pytest.mark.asyncio
async def test_failed_remote_hash_cannot_remain_a_complete_manifest(
    tmp_path: Path,
) -> None:
    state = ArchiveState(tmp_path / "archive-state.sqlite3")
    node_id = state.node_id()
    record = ArchiveRecord.build(
        source_node_id=node_id,
        source_domain="proof",
        record_kind="verification",
        logical_key="one",
        mode=ArchiveMode.VERSIONED,
        source_sequence=1,
        recorded_at="",
        visibility="owner_private",
        archive_role="test",
        payload={"value": 1},
    )
    remote = _VerificationFailRemote()
    coordinator = MemoryArchiveCoordinator(
        state,
        remote,  # type: ignore[arg-type]
    )

    with pytest.raises(ArchiveVerificationError):
        await coordinator.synchronize(
            lambda: iter([record]),
            full_snapshot=True,
        )

    assert remote.marked_failed is True
    assert state.health()["last_error"].startswith("ArchiveVerificationError:")
