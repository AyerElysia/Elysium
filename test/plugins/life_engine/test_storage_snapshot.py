from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import scripts.backup_life_data as backup_script
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
        connection.execute("PRAGMA journal_mode = WAL")
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


def test_frozen_snapshot_verifies_and_builds_verified_generation(
    tmp_path: Path,
) -> None:
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
    assert hashlib.sha256(
        (data_root / "documents/SOUL.md").read_bytes()
    ).hexdigest() == (source_hash_before)


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


def test_sqlite_backup_is_complete_without_sidecar_files(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    layout = _fixture_data(data_root)
    output = tmp_path / "snapshot"

    create_local_snapshot(data_root, output, layout=layout)

    backup = output / "sqlite/ledger.sqlite3"
    assert backup.is_file()
    assert not Path(f"{backup}-journal").exists()
    assert not Path(f"{backup}-wal").exists()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    assert verify_local_snapshot(output)["verified"] is True


def test_snapshot_secures_empty_output_before_copying_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    layout = _fixture_data(data_root)
    inspected: list[Path] = []

    def inspect_empty_output(path: Path) -> None:
        assert path.is_dir()
        assert not any(path.iterdir())
        inspected.append(path)

    monkeypatch.setattr(
        "plugins.life_engine.storage.migration.snapshot._restrict_windows_output_acl",
        inspect_empty_output,
    )
    output = tmp_path / "snapshot"

    create_local_snapshot(data_root, output, layout=layout)

    assert inspected == [output]
    assert (output / "manifest.json").is_file()


@pytest.mark.parametrize("link_kind", ["file", "directory"])
def test_snapshot_rejects_symlinks_inside_exact_roots(
    tmp_path: Path,
    link_kind: str,
) -> None:
    data_root = tmp_path / "data"
    layout = _fixture_data(data_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    if link_kind == "file":
        outside_target = outside / "private.txt"
        outside_target.write_text("must not enter backup", encoding="utf-8")
        link = data_root / "documents/linked.txt"
    else:
        outside_target = outside
        (outside / "private.txt").write_text(
            "must not enter backup",
            encoding="utf-8",
        )
        link = data_root / "documents/linked-directory"
    try:
        link.symlink_to(outside_target, target_is_directory=link_kind == "directory")
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    with pytest.raises(LifeSnapshotError, match="contains a symlink"):
        create_local_snapshot(data_root, tmp_path / "snapshot", layout=layout)

    assert outside_target.exists()
    snapshot = tmp_path / "snapshot"
    copied_files = list(snapshot.rglob("private.txt")) if snapshot.exists() else []
    assert not copied_files


def test_backup_wrapper_persists_independent_verification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    output = tmp_path / "snapshot"

    def fake_create(*_args: object, **_kwargs: object) -> dict[str, object]:
        output.mkdir()
        return {
            "manifest_sha256": "a" * 64,
            "source_snapshot_sha256": "b" * 64,
            "writer_frozen": False,
            "sqlite": [],
            "exact_file_count": 0,
        }

    monkeypatch.setattr(backup_script, "create_local_snapshot", fake_create)
    monkeypatch.setattr(
        backup_script,
        "verify_local_snapshot",
        lambda _path: {
            "verified": False,
            "verified_at": "2026-08-04T00:00:00+00:00",
            "failure_count": 1,
            "failures": [{"kind": "sqlite", "reason": "checksum mismatch"}],
        },
    )

    result = backup_script.create_life_backup(data_root, output)

    marker = output / "VERIFICATION_FAILED.json"
    assert result["generation_eligible"] is False
    assert marker.is_file()
    assert "checksum mismatch" in marker.read_text(encoding="utf-8")
