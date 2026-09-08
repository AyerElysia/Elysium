"""Glob/inventory regressions using tiny synthetic files and metadata, no DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.tools import file_tools
from plugins.life_engine.tools import managed_file_inventory as inventory
from test.plugins.life_engine.test_file_tools_industrial import _plugin
from test.plugins.life_engine.test_managed_file_inventory import _binding, _session


@pytest.fixture(autouse=True)
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("this synthetic file-only regression cannot access a DB")

    monkeypatch.setattr(sqlite3, "connect", forbidden)


class _ErrorOnlyLogger:
    """Match the actual custom Logger: error exists; exception does not."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str, **_kwargs: Any) -> None:
        self.errors.append(message)


async def test_projected_binding_overlap_and_shared_directory_are_counted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inventory, "_MAX_INVENTORY_ENTRIES", 3)
    (tmp_path / "notes").mkdir()
    for name in ("a.md", "b.md"):
        (tmp_path / "notes" / name).write_bytes(b"synthetic stale projection")
    session = _session(tmp_path, [_binding("notes/a.md"), _binding("notes/b.md")])
    monkeypatch.setattr(file_tools, "selected_file_session", lambda *_args: session)
    items = await inventory.load_managed_inventory(session, root=tmp_path)
    assert len(items) == 3
    assert {item["path"] for item in items} == {"notes", "notes/a.md", "notes/b.md"}
    ok, result = await file_tools.LifeEngineGlobFileTool(plugin=_plugin(tmp_path)).execute(
        "**/*.md"
    )
    assert ok, result
    assert isinstance(result, dict) and result["total_items"] == 2
    assert all(item["file_ref"].startswith("subject-file:") for item in result["items"])


@pytest.mark.parametrize("source", ["disk", "authority"])
async def test_each_inventory_source_keeps_its_own_scan_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    monkeypatch.setattr(inventory, "_MAX_INVENTORY_ENTRIES", 2)
    rows = []
    for number in range(3):
        name = f"item-{number}.md"
        if source == "disk":
            (tmp_path / name).write_bytes(b"synthetic")
        else:
            rows.append(_binding(name))
    with pytest.raises(inventory.ManagedFileInventoryError, match="budget_exceeded"):
        await inventory.load_managed_inventory(_session(tmp_path, rows), root=tmp_path)


async def test_unique_view_still_bounds_derived_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inventory, "_MAX_INVENTORY_ENTRIES", 2)
    session = _session(tmp_path, [_binding("one/two/file.md")])
    with pytest.raises(inventory.ManagedFileInventoryError, match="budget_exceeded"):
        await inventory.load_managed_inventory(session, root=tmp_path)


async def test_glob_exposes_inventory_reason_without_unsupported_logger_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _binding("synthetic.md")
    row["recorded_at"] = None
    session = _session(tmp_path, [row])
    logger = _ErrorOnlyLogger()
    monkeypatch.setattr(file_tools, "selected_file_session", lambda *_args: session)
    monkeypatch.setattr(file_tools, "logger", logger)
    ok, result = await file_tools.LifeEngineGlobFileTool(plugin=_plugin(tmp_path)).execute(
        "synthetic-private-pattern"
    )
    assert not ok
    assert result == "查找文件失败: workspace_inventory_invalid_recorded_time"
    assert logger.errors == ["查找文件失败: error_type=ManagedFileInventoryError"]
    assert "synthetic-private-pattern" not in " ".join(logger.errors)


async def test_patch_io_failure_logging_does_not_mask_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = _ErrorOnlyLogger()

    async def failed_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("synthetic-private-io-detail")

    monkeypatch.setattr(file_tools, "logger", logger)
    monkeypatch.setattr(file_tools.asyncio, "to_thread", failed_write)
    ok, result = await file_tools.LifeEngineApplyPatchTool(plugin=_plugin(tmp_path)).execute(
        "*** Begin Patch\n*** Add File: synthetic.txt\n+synthetic\n*** End Patch\n"
    )
    assert not ok and result == "应用 patch 失败: synthetic-private-io-detail"
    assert logger.errors == ["应用 patch 失败: error_type=OSError"]
    assert "synthetic-private-io-detail" not in " ".join(logger.errors)
    assert not (tmp_path / "synthetic.txt").exists()
