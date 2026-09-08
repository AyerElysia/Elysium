"""Real file tools against temporary selected authority; no model or live runtime."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.tools.file_tools import (
    LifeEngineApplyPatchTool,
    LifeEngineReadFileTool,
)
from test.plugins.life_engine.test_minimal_subject_file_continuity import (
    _ACTOR_ID,
    _OCCURRED_AT,
    _STREAM_ID,
    _bound_write_tool,
    _memory_plugin,
)
from test.plugins.life_engine.test_subject_document_storage_contract import _local_store


def _patch_tool(plugin: SimpleNamespace, source: str) -> LifeEngineApplyPatchTool:
    origin = _bound_write_tool(plugin, source_id=source)
    tool = LifeEngineApplyPatchTool(plugin=plugin)
    tool._bind_runtime_context(
        stream_id=_STREAM_ID,
        message=origin.trigger_message,
        tool_call_id=origin._tool_call_id,
    )
    tool._life_source_instance_id = _ACTOR_ID
    tool._life_source_occurrence_id = source
    tool._life_source_occurred_at = _OCCURRED_AT
    tool._runtime_task_name = "life_chatter"
    return tool


async def _read(plugin, path, **kwargs):
    ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(path, **kwargs)
    assert ok, result
    assert isinstance(result, dict)
    return result


async def _write(plugin, path, content, source, expected=""):
    ok, result = await _bound_write_tool(plugin, source_id=source).execute(
        path,
        content,
        expected_version=expected,
    )
    assert ok, result
    assert result["commit_status"] == "committed"
    return result


async def test_file_tool_pin_conflict_and_exact_operation_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, trace = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        first = await _write(plugin, "misc/task.txt", "before\n", "activity:first")
        pinned = await _read(plugin, "misc/task.txt")
        second = await _write(
            plugin,
            "misc/task.txt",
            "after\n",
            "activity:second",
            pinned["expected_version"],
        )
        tool = _bound_write_tool(plugin, source_id="activity:stale")
        ok, rejected = await tool.execute(
            "misc/task.txt",
            "must not win\n",
            expected_version=pinned["expected_version"],
        )
        assert not ok
        assert rejected["commit_status"] == "not_committed"
        assert rejected["error_type"] == "SubjectDocumentConflict"
        replay = await _write(
            plugin,
            "misc/task.txt",
            "after\n",
            "activity:second",
            pinned["expected_version"],
        )
        assert replay["idempotent_replay"]
        assert replay["version_id"] == second["version_id"]
        assert len(await store.list_document_history(first["document_id"])) == 2
        assert len(await trace.history("misc/task.txt")) == 2
        receipt = await _read(
            plugin,
            "misc/task.txt",
            view="operation",
            occurrence_id=second["occurrence_id"],
        )
        assert receipt["items"][0]["result"]["version_id"] == second["version_id"]
        original = await _read(plugin, "misc/task.txt", version_id=first["version_id"])
        assert original["content"] == "1\tbefore"


async def test_rename_copy_delete_reuse_keeps_old_identity_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        first = await _write(plugin, "a.txt", "same bytes\n", "activity:create")
        pin_a = await _read(plugin, "a.txt")
        rename_patch = "*** Begin Patch\n*** Update File: a.txt\n*** Move to: folder/b.txt\n*** End Patch\n"
        rename_tool = _patch_tool(plugin, "activity:rename")
        ok, renamed = await rename_tool.execute(
            rename_patch, expected_versions={"a.txt": pin_a["expected_version"]}
        )
        assert ok, renamed
        rename_receipt = renamed["files"][0]
        assert rename_receipt["document_id"] == first["document_id"]
        assert rename_receipt["version_id"] == first["version_id"]
        assert not (tmp_path / "data/life_engine_workspace/a.txt").exists()
        assert (
            tmp_path / "data/life_engine_workspace/folder/b.txt"
        ).read_bytes() == b"same bytes\n"
        reused = await _write(plugin, "a.txt", "new occupant\n", "activity:reuse")
        assert reused["document_id"] != first["document_id"]
        ok, replayed = await rename_tool.execute(
            rename_patch, expected_versions={"a.txt": pin_a["expected_version"]}
        )
        assert ok, replayed
        assert replayed["idempotent_replay"]
        assert (await _read(plugin, "a.txt"))["content"] == "1\tnew occupant"
        pin_b = await _read(plugin, "folder/b.txt")
        ok, copied = await _patch_tool(plugin, "activity:copy").execute(
            "*** Begin Patch\n*** Update File: folder/b.txt\n*** Copy to: copy.txt\n*** End Patch\n",
            expected_versions={"folder/b.txt": pin_b["expected_version"]},
        )
        assert ok, copied
        copied_version = await store.get_version(copied["files"][0]["version_id"])
        original_version = await store.get_version(first["version_id"])
        assert copied_version.document_id != first["document_id"]
        assert copied_version.semantic_source_id == original_version.semantic_source_id
        assert copied_version.content_bytes == original_version.content_bytes
        pin_b = await _read(plugin, "folder/b.txt")
        ok, deleted = await _patch_tool(plugin, "activity:delete").execute(
            "*** Begin Patch\n*** Delete File: folder/b.txt\n*** End Patch\n",
            expected_versions={"folder/b.txt": pin_b["expected_version"]},
        )
        assert ok, deleted
        assert not (tmp_path / "data/life_engine_workspace/folder/b.txt").exists()
        head = await store.get_document_head(first["document_id"])
        assert head.deleted
        old = await _read(plugin, "folder/b.txt", version_id=first["version_id"])
        assert old["content"] == "1\tsame bytes"
        assert old["deleted"]
        history = await _read(
            plugin, "a.txt", view="history", document_id=first["document_id"]
        )
        assert history["items"][0]["version_id"] == first["version_id"]
        operations = await _read(
            plugin, "a.txt", view="operations", document_id=first["document_id"]
        )
        assert {item["operation"] for item in operations["items"]} == {
            "write",
            "rename",
            "delete",
        }
        from plugins.life_engine.storage.migration.subject_history import (
            capture_subject_history,
            verify_subject_history_bundle,
        )

        bundle = await capture_subject_history(store.runtime)
        report = verify_subject_history_bundle(bundle)
        assert report.table_counts["subject_document_versions"] == 3


async def test_committed_projection_failure_never_looks_like_rejected_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        original_project = service._project_subject_version

        async def fail_projection(**kwargs):
            raise RuntimeError("synthetic projection unavailable")

        monkeypatch.setattr(service, "_project_subject_version", fail_projection)
        committed = await _write(plugin, "pending.txt", "durable\n", "activity:pending")
        assert committed["projection"]["status"] == "pending_recovery"
        assert not (tmp_path / "data/life_engine_workspace/pending.txt").exists()
        assert (await _read(plugin, "pending.txt"))["content"] == "1\tdurable"
        monkeypatch.setattr(service, "_project_subject_version", original_project)
        replayed = await _write(plugin, "pending.txt", "durable\n", "activity:pending")
        assert replayed["idempotent_replay"]
        assert replayed["projection"]["status"] in {"projected", "confirmed_existing"}
        assert len(await store.list_document_history(committed["document_id"])) == 1


async def test_legacy_binary_copy_preserves_exact_old_bytes_and_unknown_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        workspace = tmp_path / "data/life_engine_workspace"
        (workspace / "old.bin").write_bytes(b"\x00\xff\r\nraw")
        # A binary read can request an encoding to obtain a revision pin; metadata
        # becomes available as soon as first enrollment captures the old bytes.
        pin = await _read(plugin, "old.bin", encoding="latin-1")
        ok, result = await _patch_tool(plugin, "activity:binary-copy").execute(
            "*** Begin Patch\n*** Update File: old.bin\n*** Copy to: copied.bin\n*** End Patch\n",
            expected_versions={"old.bin": pin["expected_version"]},
        )
        assert ok, result
        source_head = await store.get_head("life_engine_workspace/old.bin")
        original = await store.get_version(source_head.current_version_id)
        copied = await store.get_version(result["files"][0]["version_id"])
        assert original.content_bytes == copied.content_bytes == b"\x00\xff\r\nraw"
        assert original.semantic_actor_id is None
        assert copied.semantic_actor_id is None
        assert (workspace / "old.bin").read_bytes() == (
            workspace / "copied.bin"
        ).read_bytes()
        metadata = await _read(plugin, "copied.bin", view="metadata")
        assert metadata["items"][0]["byte_length"] == len(original.content_bytes)
        assert "content_bytes" not in metadata["items"][0]
        assert '"content_bytes"' not in metadata["content"]


async def test_remote_released_path_reuse_ignores_stale_cache_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protocol model only: SQLite persistence does not claim real MySQL coverage."""
    from plugins.life_engine.storage.models import BackendKind

    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        first = await _write(
            plugin, "remote.txt", "old occupant\n", "activity:remote-first"
        )
        pinned = await _read(plugin, "remote.txt")

        class RemoteAuthorityView:
            backend = BackendKind.MYSQL

            def __getattr__(self, name):
                return getattr(store, name)

        async def remote_projection(**kwargs):
            return {"status": "remote_committed"}

        service._subject_document_store = RemoteAuthorityView()
        monkeypatch.setattr(service, "_project_subject_version", remote_projection)
        ok, deleted = await _patch_tool(plugin, "activity:remote-delete").execute(
            "*** Begin Patch\n*** Delete File: remote.txt\n*** End Patch\n",
            expected_versions={"remote.txt": pinned["expected_version"]},
        )
        assert ok, deleted
        cache = tmp_path / "data/life_engine_workspace/remote.txt"
        assert cache.read_bytes() == b"old occupant\n"
        replacement = await _write(
            plugin, "remote.txt", "new occupant\n", "activity:remote-reuse"
        )
        assert replacement["document_id"] != first["document_id"]
        assert cache.read_bytes() == b"old occupant\n"
        assert (await _read(plugin, "remote.txt"))["content"] == "1\tnew occupant"
        assert (await _read(plugin, "remote.txt", version_id=first["version_id"]))[
            "content"
        ] == "1\told occupant"
        ok, rejected = await _bound_write_tool(
            plugin, source_id="activity:remote-no-pin"
        ).execute(
            "remote.txt",
            "must not replace active binding\n",
        )
        assert not ok and rejected["commit_status"] == "not_committed"


async def test_batch_preflight_rejects_all_without_partial_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        a = await _write(plugin, "a.txt", "first\n", "activity:a")
        b = await _write(plugin, "b.txt", "second\n", "activity:b")
        pin_a, pin_b = await _read(plugin, "a.txt"), await _read(plugin, "b.txt")
        ok, rejected = await _patch_tool(plugin, "activity:bad-batch").execute(
            "*** Begin Patch\n*** Update File: a.txt\n@@\n-first\n+changed\n*** Update File: b.txt\n@@\n-does-not-match\n+changed\n*** End Patch\n",
            expected_versions={
                "a.txt": pin_a["expected_version"],
                "b.txt": pin_b["expected_version"],
            },
        )
        assert not ok
        assert rejected["commit_status"] == "not_committed"
        assert (
            await store.get_head("life_engine_workspace/a.txt")
        ).current_version_id == a["version_id"]
        assert (
            await store.get_head("life_engine_workspace/b.txt")
        ).current_version_id == b["version_id"]


async def test_current_inventory_and_grep_ignore_stale_deleted_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from plugins.life_engine.tools.file_tools import (
        LifeEngineGlobFileTool,
        LifeEngineListFilesTool,
    )
    from plugins.life_engine.tools.grep_tools import LifeEngineGrepFileTool

    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        await _write(plugin, "gone.txt", "retired phrase\n", "activity:old-search")
        pin = await _read(plugin, "gone.txt")
        original_project = service._project_subject_version

        async def unavailable(**kwargs):
            raise RuntimeError("synthetic disk projection outage")

        monkeypatch.setattr(service, "_project_subject_version", unavailable)
        ok, deleted = await _patch_tool(plugin, "activity:delete-search").execute(
            "*** Begin Patch\n*** Delete File: gone.txt\n*** End Patch\n",
            expected_versions={"gone.txt": pin["expected_version"]},
        )
        assert ok, deleted
        await _write(
            plugin, "virtual/note.txt", "visible phrase\n", "activity:virtual-search"
        )
        assert (tmp_path / "data/life_engine_workspace/gone.txt").exists()
        assert not (tmp_path / "data/life_engine_workspace/virtual").exists()
        for tool, args in (
            (LifeEngineListFilesTool(plugin=plugin), {"recursive": True}),
            (LifeEngineGlobFileTool(plugin=plugin), {"pattern": "**/*.txt"}),
        ):
            ok, listed = await tool.execute(**args)
            assert ok, listed
            paths = {item["path"] for item in listed["items"]}
            assert "virtual/note.txt" in paths
            assert "gone.txt" not in paths
        ok, matches = await LifeEngineGrepFileTool(plugin=plugin).execute(
            "phrase",
            output_mode="content",
        )
        assert ok, matches
        assert [item["path"] for item in matches["results"]] == ["virtual/note.txt"]
        assert matches["results"][0]["file_ref"].startswith("subject-file:")
        monkeypatch.setattr(service, "_project_subject_version", original_project)


async def test_explicit_recovery_adds_no_new_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        original_project = service._project_subject_version

        async def unavailable(**kwargs):
            raise RuntimeError("synthetic outage")

        monkeypatch.setattr(service, "_project_subject_version", unavailable)
        first = await _write(
            plugin, "repair.txt", "already saved\n", "activity:save-first"
        )
        monkeypatch.setattr(service, "_project_subject_version", original_project)
        ok, repaired = await _patch_tool(plugin, "activity:repair-only").execute(
            "",
            recover_occurrence_id=first["occurrence_id"],
        )
        assert ok, repaired
        assert repaired["version_id"] == first["version_id"]
        assert repaired["projection"]["status"] in {"projected", "confirmed_existing"}
        assert len(await store.list_document_operations(first["document_id"])) == 1
        assert len(await store.list_document_history(first["document_id"])) == 1


async def test_content_continuation_keeps_original_version_after_path_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        text = (
            "\n".join(f"row-{number:03d}-" + "旧文" * 25 for number in range(100))
            + "\n"
        )
        first = await _write(plugin, "long.txt", text, "activity:long-original")
        page = await _read(plugin, "long.txt", limit=0, max_bytes=4096)
        assert page["continuation"].startswith("mfc1.")
        assert len(str(page).encode("utf-8")) <= 4096
        ok, renamed = await _patch_tool(plugin, "activity:long-rename").execute(
            "*** Begin Patch\n*** Update File: long.txt\n*** Move to: moved.txt\n*** End Patch\n",
            expected_versions={"long.txt": page["expected_version"]},
        )
        assert ok, renamed
        await _write(plugin, "long.txt", "new occupant\n", "activity:long-reuse")
        pieces = [page["content"]]
        cursor = page["continuation"]
        for _ in range(30):
            if not cursor:
                break
            page = await _read(
                plugin, "long.txt", limit=0, max_bytes=4096, continuation=cursor
            )
            assert page["subject_version_id"] == first["version_id"]
            assert len(str(page).encode("utf-8")) <= 4096
            pieces.append(page["content"])
            cursor = page["continuation"]
        assert not cursor
        expected = "\n".join(
            f"{number}\t{line}" for number, line in enumerate(text.splitlines(), 1)
        )
        assert "".join(pieces) == expected


async def test_batch_receipt_budget_retains_every_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        tool = _patch_tool(plugin, "activity:bounded-batch")
        tool._runtime_task_name = "life_engine_internal"
        names = ["nested/" + ("a" * 110) + str(index) + ".txt" for index in range(8)]
        patch = (
            "*** Begin Patch\n"
            + "".join(f"*** Add File: {name}\n+content\n" for name in names)
            + "*** End Patch\n"
        )
        ok, result = await tool.execute(patch)
        assert ok, result
        assert len(str(result).encode("utf-8")) <= 8192
        assert len(result["files"]) == 8
        assert len({item["occurrence_id"] for item in result["files"]}) == 8
        for item in result["files"]:
            operation = await store.get_document_operation(item["occurrence_id"])
            assert operation is not None


async def test_registered_file_ancestor_is_rejected_without_disk_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )

        async def unavailable(**kwargs):
            raise RuntimeError("synthetic outage")

        monkeypatch.setattr(service, "_project_subject_version", unavailable)
        await _write(
            plugin, "parent.dat", "registered file\n", "activity:registered-parent"
        )
        ok, result = await _bound_write_tool(
            plugin, source_id="activity:invalid-child"
        ).execute(
            "parent.dat/child.txt",
            "not a directory\n",
        )
        assert not ok
        assert result["commit_status"] == "not_committed"
        assert (
            await store.get_head("life_engine_workspace/parent.dat/child.txt") is None
        )


async def test_long_operation_metadata_is_fully_byte_pageable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        reason = "保留这次修改的完整原因。" * 1500
        ok, written = await _bound_write_tool(
            plugin, source_id="activity:long-reason"
        ).execute(
            "reason.txt",
            "short content\n",
            reason=reason,
        )
        assert ok, written
        pieces = []
        cursor = ""
        for _ in range(80):
            page = await _read(
                plugin,
                "reason.txt",
                view="operation",
                occurrence_id=written["occurrence_id"],
                continuation=cursor,
                max_bytes=4096,
            )
            assert len(str(page).encode("utf-8")) <= 4096
            pieces.append(page["content"])
            cursor = page["continuation"]
            if not cursor:
                break
        assert not cursor
        operation = json.loads("".join(pieces))
        assert operation["change_context"]["reason"] == reason
        ok, rejected = await LifeEngineReadFileTool(plugin=plugin).execute(
            "reason.txt",
            view="operation",
            occurrence_id="x" * 20000,
            max_bytes=1024,
        )
        assert not ok
        assert len(str(rejected).encode("utf-8")) < 1024
        assert "x" * 500 not in str(rejected)
