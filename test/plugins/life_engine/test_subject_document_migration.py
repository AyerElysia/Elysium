"""Exact-byte subject snapshot copy and reverse-export tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.storage.authority import (
    FileAuthorityRegistry,
    StaleAuthorityToken,
)
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.migration.copy_authority import CopyAuthorityToken
from plugins.life_engine.storage.migration.manifest import snapshot_manifest_sha256
from plugins.life_engine.storage.migration.subject_copy import (
    SubjectDocumentCopyError,
    copy_subject_documents_from_snapshot,
)
from plugins.life_engine.storage.migration.subject_export import (
    export_subject_documents,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.subject_contracts import SubjectDocumentStorePort
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from src.kernel.storage import canonical_json


class _ProgressRegistry:
    def __init__(self) -> None:
        self.progress: list[int] = []
        self.conflicts: list[dict[str, Any]] = []

    async def set_progress(
        self,
        _token: CopyAuthorityToken,
        *,
        copied_records: int,
    ) -> None:
        self.progress.append(copied_records)

    async def record_conflict(
        self,
        _token: CopyAuthorityToken,
        **evidence: Any,
    ) -> None:
        self.conflicts.append(evidence)


def _token() -> CopyAuthorityToken:
    return CopyAuthorityToken(
        run_id="subject-copy-contract",
        authority_epoch=1,
        owner_id="subject-copy-test",
        lease_until="2026-08-04T12:00:00+00:00",
        fencing_token="test-only-token",
    )


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="subject-migration-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="a" * 64,
        root_hashes={"subject": "b" * 64},
        frontiers={"subject": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@asynccontextmanager
async def _local_store(
    tmp_path: Path,
) -> AsyncIterator[tuple[StorageBackendRuntime, SubjectDocumentStorePort]]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    await registry.register_generation(_generation())
    token = await registry.activate_generation(
        _generation().generation_id,
        expected_epoch=0,
        owner_id="subject-migration-contract",
        lease_seconds=300,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=_generation().generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_SUBJECT_MIGRATION_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "subject.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_SUBJECT_MIGRATION_FENCE": token.fencing_token},
    )
    store = await open_subject_document_store(runtime, initialize_schema=True)
    try:
        yield runtime, store
    finally:
        await runtime.close()
        try:
            await registry.revoke(token)
        except StaleAuthorityToken:
            pass


def _snapshot(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    root = tmp_path / "snapshot"
    root.mkdir()
    contents = {
        "diaries/2026-08/04.md": b"external diary\r\nexact\r\n",
        "life_engine_workspace/SOUL.md": b"\xef\xbb\xbf# Elysia\r\nSoul\r\n",
        "life_engine_workspace/diaries/private.md": b"private diary\nexact\n",
        "media_cache/ignored.bin": b"not a declared subject document",
    }
    entries: list[dict[str, Any]] = []
    for index, (source_relative, content) in enumerate(contents.items(), start=1):
        output = root / "workspace" / source_relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        entries.append(
            {
                "backup": str(output),
                "backup_relative": output.relative_to(root).as_posix(),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "source": f"/source/{source_relative}",
                "source_relative": source_relative,
                "source_stat": {
                    "bytes": len(content),
                    "device": 1,
                    "inode": index,
                    "mtime_ns": index * 1000,
                },
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "created_at": "2026-08-04T00:00:00+00:00",
        "created_at_utc": "2026-08-04T00:00:00Z",
        "source_snapshot_sha256": "c" * 64,
        "writer_frozen": False,
        "root_hashes": {},
        "frontiers": {},
        "sqlite": [],
        "exact_files": entries,
        "workspace_files": entries,
        "exact_file_count": len(entries),
        "workspace_file_count": len(entries),
    }
    manifest["manifest_sha256"] = snapshot_manifest_sha256(manifest)
    (root / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
    return root, contents


async def test_subject_snapshot_copy_and_reverse_export_are_exact(
    tmp_path: Path,
) -> None:
    snapshot, contents = _snapshot(tmp_path)
    registry = _ProgressRegistry()
    async with _local_store(tmp_path) as (_, store):
        report = await copy_subject_documents_from_snapshot(
            snapshot,
            store,
            copy_registry=registry,  # type: ignore[arg-type]
            token=_token(),
            progress_interval=2,
        )
        assert report.verified is True
        assert report.document_count == 3
        assert report.copied_count == 3
        assert report.source_root_sha256 == report.target_root_sha256
        assert registry.progress == [2, 3]
        assert registry.conflicts == []

        soul = await store.get_head("life_engine_workspace/SOUL.md")
        assert soul is not None
        version = await store.get_version(soul.current_version_id)
        assert version.content_bytes == contents["life_engine_workspace/SOUL.md"]
        assert version.encoding == "utf-8-sig"
        assert version.newline_style == "crlf"
        assert version.semantic_actor_id is None
        assert version.semantic_source_id is None

        replay = await copy_subject_documents_from_snapshot(
            snapshot,
            store,
            copy_registry=registry,  # type: ignore[arg-type]
            token=_token(),
        )
        assert replay == report

        export_root = tmp_path / "reverse-export"
        exported = await export_subject_documents(store, export_root)
        assert exported.verified is True
        assert exported.document_count == 3
        assert exported.root_sha256 == report.target_root_sha256
        assert not (export_root / "EXPORT_INCOMPLETE").exists()
        assert (
            export_root / "workspace/life_engine_workspace/SOUL.md"
        ).read_bytes() == contents["life_engine_workspace/SOUL.md"]
        assert (
            export_root / "workspace/diaries/2026-08/04.md"
        ).read_bytes() == contents["diaries/2026-08/04.md"]
        manifest = json.loads((export_root / "manifest.json").read_text())
        assert manifest["document_count"] == 3
        assert len(manifest["documents"]) == 3


async def test_subject_snapshot_copy_fails_closed_on_changed_bytes(
    tmp_path: Path,
) -> None:
    snapshot, _ = _snapshot(tmp_path)
    target = snapshot / "workspace/life_engine_workspace/SOUL.md"
    target.write_bytes(b"changed after manifest")
    async with _local_store(tmp_path) as (_, store):
        with pytest.raises(SubjectDocumentCopyError, match="checksum mismatch"):
            await copy_subject_documents_from_snapshot(
                snapshot,
                store,
                copy_registry=_ProgressRegistry(),  # type: ignore[arg-type]
                token=_token(),
            )
