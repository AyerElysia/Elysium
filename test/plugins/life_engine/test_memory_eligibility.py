"""Eligibility boundaries for Life Engine memory documents."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.life_engine.memory.eligibility import (
    assess_document_path,
    assess_workspace_document,
    scan_workspace_documents,
)
from plugins.life_engine.memory.indexing import create_memory_schema, upsert_document_rows


@pytest.mark.parametrize(
    ("path", "eligible", "reason"),
    [
        ("MEMORY.md", True, ""),
        ("AyerElysia_preferences.txt", True, ""),
        ("diaries/2026-07-20.md", True, ""),
        ("dreams/2026-07-20.md", True, ""),
        ("notes/relationships/elysia.md", True, ""),
        ("narrative/autobiography.md", True, ""),
        ("runtime/life_chatter_rolling_context.json", False, "unsupported_suffix"),
        ("thoughts/streams.json", False, "unsupported_suffix"),
        (".life_trace/blobs/a.txt", False, "hidden_directory"),
        ("notes/.draft.md", False, "hidden_directory"),
        ("notes/idea.md.backup", False, "temporary_name"),
        ("life_events.jsonl", False, "unsupported_suffix"),
        ("todos.json", False, "unsupported_suffix"),
        ("misc/note.md", False, "unsupported_directory"),
        ("unlisted.md", False, "root_not_whitelisted"),
        ("../outside.md", False, "invalid_path"),
        ("/absolute.md", False, "absolute_path"),
    ],
)
def test_document_path_eligibility_matrix(path: str, eligible: bool, reason: str) -> None:
    decision = assess_document_path(path)

    assert decision.eligible is eligible
    assert decision.reason == reason


def test_workspace_scan_does_not_recurse_rejected_runtime_or_hidden_trees(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes").mkdir()
    runtime = tmp_path / "runtime"
    trace = tmp_path / ".life_trace"
    runtime.mkdir()
    trace.mkdir()
    (runtime / "nested").mkdir()
    (trace / "nested").mkdir()
    (tmp_path / "notes" / "kept.md").write_text("kept", encoding="utf-8")
    (runtime / "state.json").write_text("{}", encoding="utf-8")
    (runtime / "nested" / "ignored.md").write_text("ignored", encoding="utf-8")
    (trace / "trace.txt").write_text("trace", encoding="utf-8")
    (trace / "nested" / "ignored.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "large.md").write_text("not whitelisted", encoding="utf-8")

    scan = scan_workspace_documents(tmp_path)

    assert [item.path for item in scan.documents] == ["notes/kept.md"]
    assert scan.rejected_reason_counts == {
        "blocked_directory": 1,
        "hidden_directory": 1,
        "root_not_whitelisted": 1,
    }
    assert {item.path for item in scan.rejected} == {".life_trace", "large.md", "runtime"}


def test_workspace_eligibility_rejects_symlink_and_oversized_file(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    target = notes / "target.md"
    target.write_text("body", encoding="utf-8")
    link = notes / "link.md"
    link.symlink_to(target)
    diary_target = tmp_path / "diary_target"
    diary_target.mkdir()
    (diary_target / "entry.md").write_text("entry", encoding="utf-8")
    linked_diaries = tmp_path / "diaries"
    linked_diaries.symlink_to(diary_target, target_is_directory=True)
    oversized = notes / "oversized.md"
    oversized.write_text("12345", encoding="utf-8")

    assert assess_workspace_document(tmp_path, "notes/link.md").reason == "symlink"
    assert assess_workspace_document(tmp_path, "diaries/entry.md").reason == "symlink"
    assert assess_workspace_document(tmp_path, "notes/oversized.md", max_bytes=4).reason == "too_large"


def test_document_upsert_rejects_runtime_path_before_creating_rows(tmp_path: Path) -> None:
    db = sqlite3.connect(str(tmp_path / "memory.db"))
    db.row_factory = sqlite3.Row
    create_memory_schema(db)

    with pytest.raises(ValueError, match="不支持索引的记忆文档路径: unsupported_suffix"):
        upsert_document_rows(db, "runtime/life_chatter_rolling_context.json", "{}")

    assert db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_file_tool_does_not_sync_ineligible_runtime_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.tools.file_tools import _sync_memory_embedding_for_file

    upsert_document = AsyncMock()
    fake_service = SimpleNamespace(
        _memory_service=SimpleNamespace(upsert_document=upsert_document),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.LifeEngineService.get_instance",
        lambda: fake_service,
    )

    await _sync_memory_embedding_for_file(object(), "runtime/state.json", "{}")

    upsert_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_memory_tool_rejects_runtime_file(tmp_path: Path) -> None:
    from plugins.life_engine.core.config import LifeEngineConfig
    from plugins.life_engine.tools.file_tools import FetchLifeMemoryTool

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "state.json").write_text('{"private": true}', encoding="utf-8")
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)

    ok, payload = await FetchLifeMemoryTool(
        plugin=SimpleNamespace(config=config),
    ).execute(["runtime/state.json"])

    assert ok is True
    assert payload["successful"] == 0
    assert payload["failed"] == 1
    assert payload["files"] == [
        {
            "path": "runtime/state.json",
            "error": "不是可读取的记忆文档: unsupported_suffix",
        }
    ]


@pytest.mark.asyncio
async def test_sync_embedding_skips_ineligible_runtime_file() -> None:
    from plugins.life_engine.memory.search import sync_embedding

    lookup = AsyncMock()
    collection = Mock()

    await sync_embedding(
        sqlite3.connect(":memory:"),
        collection,
        "runtime/state.json",
        "{}",
        lookup,
    )

    lookup.assert_not_awaited()
    collection.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_dream_archive_skips_ineligible_runtime_path(tmp_path: Path) -> None:
    from plugins.life_engine.dream.residue import DreamReport, integrate_archive_into_memory

    upsert_document = AsyncMock()
    result = await integrate_archive_into_memory(
        DreamReport(archive_path="runtime/state.json"),
        tmp_path,
        SimpleNamespace(upsert_document=upsert_document),
        [],
    )

    assert result == {"archive_written": False, "linked_refs": 0}
    upsert_document.assert_not_awaited()
