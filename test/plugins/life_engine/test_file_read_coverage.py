"""Synthetic read-window coverage; no live process, model, or subject data."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from plugins.life_engine.tools.file_tools import LifeEngineReadFileTool
from test.plugins.life_engine.test_bounded_tool_projections import _workspace_plugin
from test.plugins.life_engine.test_managed_file_lifecycle_tools import _write
from test.plugins.life_engine.test_minimal_subject_file_continuity import _memory_plugin
from test.plugins.life_engine.test_subject_document_storage_contract import _local_store


@pytest.mark.parametrize(
    ("offset", "limit", "from_end", "partial"),
    [
        (41, 14, False, True),
        (1, 0, False, False),
        (1, 2, False, True),
        (3, 0, False, False),
        (1, 2, True, True),
        (1, 0, True, False),
    ],
)
async def test_managed_line_selection_reports_omissions_on_either_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offset: int,
    limit: int,
    from_end: bool,
    partial: bool,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store,
            data_root=tmp_path / "data",
            monkeypatch=monkeypatch,
        )
        content = "\n".join(f"synthetic line {n}" for n in range(1, 52)) + "\n"
        receipt = await _write(plugin, "coverage.txt", content, "coverage:one")
        reference = f"subject-file:{receipt['document_id']}@{receipt['version_id']}"
        ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref=reference,
            offset=offset,
            limit=limit,
            from_end=from_end,
        )
        assert ok, result
        assert result["source_selection_truncated"] is partial
        assert (
            result["truncated"] is False
        )  # The chosen lines fit in one transport page.
        assert result["continuation"] == ""
        assert result["file_ref"] == reference
        assert (
            result["file_content_sha256"]
            == hashlib.sha256(content.encode()).hexdigest()
        )
        if offset == 41:
            assert result["showing"] == "41-51"
            assert result["remaining_lines_before"] == 40
            assert (
                "next_offset" not in result
            )  # No later lines; earlier ones are omitted.


@pytest.mark.parametrize("content", ["a\nb\nc\n", ""])
async def test_unregistered_full_read_has_explicit_complete_selection(
    tmp_path: Path,
    content: str,
) -> None:
    (tmp_path / "plain.txt").write_text(content, encoding="utf-8")
    ok, result = await LifeEngineReadFileTool(
        plugin=_workspace_plugin(tmp_path)
    ).execute(
        "plain.txt",
        limit=0,
    )
    assert ok, result
    assert result["source_selection_truncated"] is False
    assert result["truncated"] is False


async def test_unregistered_tail_is_not_full_file(tmp_path: Path) -> None:
    (tmp_path / "plain.txt").write_text("a\nb\nc\n", encoding="utf-8")
    ok, result = await LifeEngineReadFileTool(
        plugin=_workspace_plugin(tmp_path)
    ).execute(
        "plain.txt",
        from_end=True,
        limit=1,
    )
    assert ok, result
    assert result["showing"] == "3-3"
    assert result["remaining_lines_before"] == 2
    assert result["source_selection_truncated"] is True
    assert result["truncated"] is False


async def test_transport_continuation_does_not_erase_selection_omissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store,
            data_root=tmp_path / "data",
            monkeypatch=monkeypatch,
        )
        content = "\n".join(f"line-{n}:" + "汉字" * 30 for n in range(100))
        receipt = await _write(plugin, "paged.txt", content, "coverage:paged")
        reference = f"subject-file:{receipt['document_id']}@{receipt['version_id']}"
        cursor = ""
        pieces: list[str] = []
        for _ in range(30):
            ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
                file_ref=reference,
                offset=41,
                limit=60,
                continuation=cursor,
                max_bytes=4096,
            )
            assert ok, result
            assert result["source_selection_truncated"] is True
            assert result["remaining_lines_before"] == 40
            assert result["file_ref"] == reference
            assert result["delivered_bytes"] == len(str(result).encode()) <= 4096
            pieces.append(result["content"])
            cursor = result["continuation"]
            if not cursor:
                assert result["truncated"] is True  # Last page still omits prior pages.
                break
        else:
            pytest.fail("synthetic read did not finish within its bounded test budget")
        assert len(pieces) > 1
        expected = "\n".join(
            f"{n + 1}\t{line}" for n, line in enumerate(content.splitlines()) if n >= 40
        )
        assert "".join(pieces) == expected
