"""Synthetic exact-reference reads; no model, live authority or subject data."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.memory.search import SearchResult
from plugins.life_engine.tools.file_tools import LifeEngineReadFileTool
from test.plugins.life_engine.test_managed_file_lifecycle_tools import (
    _patch_tool,
    _read,
    _write,
)
from test.plugins.life_engine.test_minimal_subject_file_continuity import _memory_plugin
from test.plugins.life_engine.test_subject_document_storage_contract import _local_store


async def test_output_file_ref_alone_executes_and_keeps_path_version_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store,
            data_root=tmp_path / "data",
            monkeypatch=monkeypatch,
        )
        first = await _write(plugin, "notes/original.txt", "original\n", "ref:first")
        path_read = await _read(
            plugin, "notes/original.txt", version_id=first["version_id"]
        )
        ok, exact = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref=path_read["file_ref"],
        )
        assert ok, exact
        for key in (
            "content",
            "document_id",
            "subject_version_id",
            "file_content_sha256",
        ):
            assert exact[key] == path_read[key]
        assert exact["path"] == exact["version_path"] == "notes/original.txt"


async def test_file_ref_survives_update_rename_delete_reuse_and_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.storage.subject_factory import open_subject_document_store

    async with _local_store(tmp_path) as (runtime, store, _):
        data_root = tmp_path / "data"
        plugin, _, _ = _memory_plugin(
            store, data_root=data_root, monkeypatch=monkeypatch
        )
        first = await _write(plugin, "a.txt", "original\r\nbytes\n", "ref:create")
        original = await _read(plugin, "a.txt")
        await _write(
            plugin, "a.txt", "revised\n", "ref:update", original["expected_version"]
        )
        pin = await _read(plugin, "a.txt")
        ok, result = await _patch_tool(plugin, "ref:rename").execute(
            "*** Begin Patch\n*** Update File: a.txt\n*** Move to: folder/b.txt\n*** End Patch\n",
            expected_versions={"a.txt": pin["expected_version"]},
        )
        assert ok, result
        pin = await _read(plugin, "folder/b.txt")
        ok, result = await _patch_tool(plugin, "ref:delete").execute(
            "*** Begin Patch\n*** Delete File: folder/b.txt\n*** End Patch\n",
            expected_versions={"folder/b.txt": pin["expected_version"]},
        )
        assert ok, result
        reused = await _write(plugin, "a.txt", "different document\n", "ref:reuse")
        assert reused["document_id"] != first["document_id"]
        reopened = await open_subject_document_store(runtime, initialize_schema=False)
        plugin, _, _ = _memory_plugin(
            reopened, data_root=data_root, monkeypatch=monkeypatch
        )
        ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref=original["file_ref"]
        )
        assert ok, result
        assert result["content"] == "1\toriginal\n2\tbytes"
        assert (
            result["file_content_sha256"]
            == hashlib.sha256(b"original\r\nbytes\n").hexdigest()
        )
        assert result["document_id"] == first["document_id"]
        assert result["subject_version_id"] == first["version_id"]
        assert result["version_path"] == "a.txt"
        assert result["current_path"] == "folder/b.txt"
        assert result["deleted"]
        assert (await _read(plugin, "a.txt"))["content"] == "1\tdifferent document"


@pytest.mark.parametrize(
    "file_ref",
    [
        "document:a.txt",
        "subject-file:doc_a",
        "subject-file:@ver_a",
        "subject-file:doc_a@",
        "subject-file:doc_a@ver_a@ver_b",
        " subject-file:doc_a@ver_a",
        "subject-file:doc_a@ver_a ",
        "subject-file:doc_a@ver_a?latest=1",
        "subject-file:doc_a@ver_a#fragment",
        "subject-file:doc_a%40ver_a",
        "subject-file:doc_a@../ver_a",
        "subject-file:doc_a@ver_" + "a" * 256,
    ],
)
async def test_malformed_file_ref_fails_before_store_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_ref: str,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        lookup = AsyncMock(
            side_effect=AssertionError("invalid reference reached store")
        )
        monkeypatch.setattr(store, "get_version_descriptor", lookup)
        ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref=file_ref
        )
        assert not ok
        assert "ManagedFileReferenceInvalid" in str(result)
        lookup.assert_not_awaited()


@pytest.mark.parametrize(
    "selectors",
    [
        {"path": "a.txt"},
        {"document_id": "doc_a"},
        {"version_id": "ver_a"},
        {"view": "metadata"},
        {"view": "history"},
        {"occurrence_id": "op_a"},
        {"after_id": "ver_a"},
        {"after_recorded_at": "2026-01-01"},
    ],
)
async def test_file_ref_rejects_conflicting_selectors_before_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selectors: dict,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        lookup = AsyncMock(side_effect=AssertionError("conflict reached store"))
        monkeypatch.setattr(store, "get_version_descriptor", lookup)
        ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref="subject-file:doc_a@ver_a",
            **selectors,
        )
        assert not ok
        assert "ManagedFileReferenceSelectorConflict" in str(result)
        lookup.assert_not_awaited()


async def test_file_ref_document_mismatch_is_rejected_before_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        first = await _write(plugin, "a.txt", "a\n", "ref:doc-a")
        second = await _write(plugin, "b.txt", "b\n", "ref:doc-b")
        blob = AsyncMock(side_effect=AssertionError("wrong document fetched blob"))
        monkeypatch.setattr(store, "get_version", blob)
        ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref=f"subject-file:{first['document_id']}@{second['version_id']}",
        )
        assert not ok
        assert "ManagedFileVersionDocumentConflict" in str(result)
        blob.assert_not_awaited()


def test_read_schema_exposes_optional_exact_reference() -> None:
    parameters = LifeEngineReadFileTool.to_schema()["function"]["parameters"]
    assert parameters["properties"]["file_ref"]["type"] == "string"
    assert "path" not in parameters.get("required", [])
    assert "file_ref" not in parameters.get("required", [])


async def test_actor_search_hint_and_emitted_ref_execute_real_read_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        first = await _write(plugin, "hint.txt", "hint source\n", "ref:hint")
        service._memory_service = SimpleNamespace(
            search_memory=AsyncMock(
                return_value=[
                    SearchResult(
                        file_path="hint.txt",
                        title="Synthetic hint",
                        snippet="hint source",
                        relevance=1.0,
                        source="direct",
                        document_id=first["document_id"],
                        version_id=first["version_id"],
                    )
                ]
            ),
            expand_living_document_associations=AsyncMock(
                side_effect=AssertionError("baseline expanded")
            ),
            build_memory_bundles=AsyncMock(
                side_effect=AssertionError("baseline read relations")
            ),
        )
        output = await service.search_actor_memory("hint", enable_association=False)
        assert "path、version_id 精确读取（正文不传 document_id）" in output
        assert "或仅传完整 file_ref" in output
        match = re.search(r"file_ref=(subject-file:[^\s]+)", output)
        assert match is not None
        tool = LifeEngineReadFileTool(plugin=plugin)
        ok, by_ref = await tool.execute(file_ref=match.group(1))
        assert ok, by_ref
        ok, by_path = await tool.execute(
            path="hint.txt", version_id=first["version_id"]
        )
        assert ok, by_path
        assert by_ref["content"] == by_path["content"] == "1\thint source"


@pytest.mark.parametrize(
    "fault",
    [
        "descriptor_version",
        "descriptor_document",
        "descriptor_namespace",
        "descriptor_traversal",
        "descriptor_alias",
        "head_namespace",
        "head_document",
    ],
)
async def test_reference_rejects_corrupt_or_out_of_scope_metadata_before_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        first = await _write(plugin, "a.txt", "synthetic\n", "ref:metadata")
        descriptor = dict(await store.get_version_descriptor(first["version_id"]))
        head = await store.get_document_head(first["document_id"])
        if fault == "descriptor_version":
            descriptor["version_id"] = "ver_other"
        elif fault == "descriptor_document":
            descriptor["document_id"] = "doc_other"
        elif fault == "descriptor_namespace":
            descriptor["logical_path"] = "private_context/a.txt"
        elif fault == "descriptor_traversal":
            descriptor["logical_path"] = (
                "life_engine_workspace/../private_context/a.txt"
            )
        elif fault == "descriptor_alias":
            descriptor["logical_path"] = "life_engine_workspace/folder//a.txt"
        elif fault == "head_namespace":
            head = replace(head, logical_path="private_context/a.txt")
        else:
            head = replace(head, document_id="doc_other")
        monkeypatch.setattr(
            store, "get_version_descriptor", AsyncMock(return_value=descriptor)
        )
        monkeypatch.setattr(store, "get_document_head", AsyncMock(return_value=head))
        blob = AsyncMock(side_effect=AssertionError("unauthorized blob read"))
        monkeypatch.setattr(store, "get_version", blob)
        ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref=f"subject-file:{first['document_id']}@{first['version_id']}",
        )
        assert not ok
        assert "ManagedFile" in str(result)
        blob.assert_not_awaited()


@pytest.mark.parametrize("destination", ["inside", "outside"])
async def test_reference_rejects_historical_path_symlink_alias_before_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        first = await _write(plugin, "a.txt", "original\n", "ref:symlink")
        workspace = tmp_path / "data/life_engine_workspace"
        link = workspace / "a.txt"
        link.unlink()
        target = (
            workspace / "alias.txt"
            if destination == "inside"
            else tmp_path / "outside.txt"
        )
        target.write_text("decoy\n")
        link.symlink_to(target)
        blob = AsyncMock(side_effect=AssertionError("symlink alias reached blob"))
        monkeypatch.setattr(store, "get_version", blob)
        ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref=f"subject-file:{first['document_id']}@{first['version_id']}",
        )
        assert not ok
        assert "decoy" not in str(result)
        blob.assert_not_awaited()


@pytest.mark.parametrize("state", ["disabled", "not_started", "missing_version"])
async def test_reference_never_falls_back_to_disk_when_authority_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        first = await _write(plugin, "a.txt", "authority\n", "ref:unavailable")
        (tmp_path / "data/life_engine_workspace/a.txt").write_text("disk decoy\n")
        reference = f"subject-file:{first['document_id']}@{first['version_id']}"
        if state == "disabled":
            service._selectable_storage_enabled = False
        elif state == "not_started":
            service._subject_document_store = None
        else:
            reference = f"subject-file:{first['document_id']}@ver_missing"
        disk = AsyncMock(side_effect=AssertionError("read reference from disk"))
        from plugins.life_engine.tools import file_tools

        monkeypatch.setattr(file_tools, "_read_selected_subject_file", disk)
        ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref=reference
        )
        assert not ok
        assert "disk decoy" not in str(result)
        disk.assert_not_awaited()


async def test_reference_integrity_decode_and_missing_selector_fail_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        first = await _write(plugin, "bytes.txt", "original\n", "ref:integrity")
        reference = f"subject-file:{first['document_id']}@{first['version_id']}"
        tool = LifeEngineReadFileTool(plugin=plugin)
        ok, result = await tool.execute()
        assert not ok and "FileReadSelectorRequired" in str(result)
        original_get = store.get_version
        original = await original_get(first["version_id"])
        monkeypatch.setattr(
            store,
            "get_version",
            AsyncMock(return_value=replace(original, content_bytes=b"tampered")),
        )
        ok, result = await tool.execute(file_ref=reference)
        assert not ok and "SelectedSubjectVersionIntegrityError" in str(result)
        binary = b"\xff\x00\r\n"
        monkeypatch.setattr(
            store,
            "get_version",
            AsyncMock(
                return_value=replace(
                    original,
                    content_bytes=binary,
                    byte_length=len(binary),
                    content_hash=hashlib.sha256(binary).hexdigest(),
                )
            ),
        )
        ok, result = await tool.execute(file_ref=reference)
        assert not ok and "文件编码错误" in str(result)
        ok, result = await tool.execute(file_ref=reference, encoding="latin-1")
        assert ok, result
        assert result["file_content_sha256"] == hashlib.sha256(binary).hexdigest()
        assert result["source_file_bytes"] == len(binary)


async def test_reference_continuation_is_version_bound_and_budgeted_after_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        content = (
            "\n".join(f"row-{number}-" + "旧文" * 25 for number in range(100)) + "\n"
        )
        first = await _write(plugin, "long.txt", content, "ref:pages")
        original = await _read(plugin, "long.txt")
        tool = LifeEngineReadFileTool(plugin=plugin)
        reference = original["file_ref"]
        ok, page = await tool.execute(file_ref=reference, limit=0, max_bytes=4096)
        assert ok, page
        cursor = page["continuation"]
        assert cursor.startswith("mfc1.")
        assert len(str(page).encode()) <= 4096
        ok, result = await _patch_tool(plugin, "ref:page-rename").execute(
            "*** Begin Patch\n*** Update File: long.txt\n*** Move to: moved.txt\n*** End Patch\n",
            expected_versions={"long.txt": original["expected_version"]},
        )
        assert ok, result
        reused = await _write(
            plugin, "long.txt", "different document\n", "ref:page-reuse"
        )
        bad_pin = cursor.replace(first["version_id"], reused["version_id"], 1)
        ok, result = await tool.execute(
            file_ref=reference, limit=0, max_bytes=4096, continuation=bad_pin
        )
        assert not ok and "ManagedFileContinuationVersionConflict" in str(result)
        other_ref = f"subject-file:{reused['document_id']}@{reused['version_id']}"
        ok, result = await tool.execute(
            file_ref=other_ref, limit=0, max_bytes=4096, continuation=cursor
        )
        assert not ok and "ManagedFileContinuationVersionConflict" in str(result)
        ok, result = await tool.execute(
            file_ref=reference, limit=1, max_bytes=4096, continuation=cursor
        )
        assert not ok
        pieces = [page["content"]]
        for _ in range(40):
            if not cursor:
                break
            ok, page = await tool.execute(
                file_ref=reference, limit=0, max_bytes=4096, continuation=cursor
            )
            assert ok, page
            assert page["file_ref"] == reference
            assert page["document_id"] == first["document_id"]
            assert len(str(page).encode()) <= 4096
            pieces.append(page["content"])
            cursor = page["continuation"]
        assert not cursor
        assert "".join(pieces) == "\n".join(
            f"{n}\t{line}" for n, line in enumerate(content.splitlines(), 1)
        )
