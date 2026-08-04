from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from plugins.life_engine.storage.migration import (
    LifeSnapshotError,
    LifeStorageLayout,
    build_backend_generation,
    create_local_snapshot,
    load_snapshot_manifest,
    verify_local_snapshot,
)
from plugins.life_engine.storage.models import GenerationStatus


def _fixture_data(data_root: Path) -> LifeStorageLayout:
    data_root.mkdir()
    database = data_root / "ledger.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE events ("
            "sequence INTEGER PRIMARY KEY, payload TEXT, raw BLOB, score REAL, optional TEXT)"
        )
        connection.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            [
                (1, "爱莉\n第一条", b"\x00\xff", 0.5, None),
                (2, "emoji: 🌸", b"binary", float("inf"), "present"),
            ],
        )
    documents = data_root / "documents"
    documents.mkdir()
    (documents / "SOUL.md").write_bytes(b"\xef\xbb\xbf# Elysia\r\nexact bytes\r\n")
    return LifeStorageLayout(
        sqlite_sources=(Path("ledger.sqlite3"),),
        exact_roots=(Path("documents"),),
        excluded_rebuildable_roots=(),
        excluded_preserved_backup_roots=(),
    )


def test_frozen_snapshot_verifies_and_builds_verified_generation(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    layout = _fixture_data(data_root)
    source_hash_before = hashlib.sha256(
        (data_root / "documents/SOUL.md").read_bytes()
    ).hexdigest()

    manifest = create_local_snapshot(
        data_root,
        tmp_path / "snapshot",
        layout=layout,
        writer_frozen=True,
    )
    restored = load_snapshot_manifest(tmp_path / "snapshot/manifest.json")
    verification = verify_local_snapshot(tmp_path / "snapshot")
    generation = build_backend_generation(
        restored,
        generation_id="fixture-local-v1",
        verification=verification,
    )

    assert manifest == restored
    assert verification["verified"] is True
    assert generation.status == GenerationStatus.VERIFIED
    assert generation.frontiers["ledger.sqlite3:events.sequence"] == 2
    assert (data_root / "documents/SOUL.md").read_bytes().startswith(b"\xef\xbb\xbf")
    assert hashlib.sha256((data_root / "documents/SOUL.md").read_bytes()).hexdigest() == (
        source_hash_before
    )


def test_live_snapshot_stays_candidate_even_when_copy_verifies(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    layout = _fixture_data(data_root)
    manifest = create_local_snapshot(
        data_root,
        tmp_path / "snapshot",
        layout=layout,
        writer_frozen=False,
    )
    verification = verify_local_snapshot(tmp_path / "snapshot")
    generation = build_backend_generation(
        manifest,
        generation_id="fixture-live-v1",
        verification=verification,
    )

    assert verification["verified"] is True
    assert generation.status == GenerationStatus.CANDIDATE
    assert generation.verified_at == ""


def test_tamper_is_reported_and_existing_output_is_never_overwritten(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    layout = _fixture_data(data_root)
    output = tmp_path / "snapshot"
    create_local_snapshot(data_root, output, layout=layout, writer_frozen=True)
    (output / "workspace/documents/SOUL.md").write_text(
        "tampered",
        encoding="utf-8",
    )

    verification = verify_local_snapshot(output)
    assert verification["verified"] is False
    assert verification["failure_count"] == 1
    with pytest.raises(LifeSnapshotError, match="refusing overwrite"):
        create_local_snapshot(data_root, output, layout=layout, writer_frozen=True)


def test_snapshot_output_must_not_be_inside_source_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    layout = _fixture_data(data_root)

    with pytest.raises(LifeSnapshotError, match="outside the source data root"):
        create_local_snapshot(
            data_root,
            data_root / "nested-backup",
            layout=layout,
        )
