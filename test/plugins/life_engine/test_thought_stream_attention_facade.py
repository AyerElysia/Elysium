"""Retired ThoughtStream archive is read-only; the model tool class is gone."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.life_engine.streams import tools as stream_tools
from plugins.life_engine.streams.legacy_snapshot import LegacySnapshotNotFoundError
from plugins.life_engine.streams.tools import (
    ThoughtStreamProjectionError,
    read_legacy_thought_stream_page,
)
from plugins.life_engine.tools import ALL_TOOLS


def _write_snapshot(workspace: Path) -> Path:
    path = workspace / "thoughts" / "streams.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "global_revision": 1,
                "streams": [
                    {
                        "id": "ts_old",
                        "title": "旧时代原始记录",
                        "status": "active",
                        "created_at": "2026-08-01T00:00:00+00:00",
                        "last_advanced_at": "2026-08-01T00:00:00+00:00",
                        "curiosity_score": 0.5,
                        "advance_count": 0,
                        "revision": 1,
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return path


def test_thought_stream_tool_class_is_removed() -> None:
    assert not hasattr(stream_tools, "LifeEngineManageThoughtStreamTool")
    assert not hasattr(stream_tools, "STREAM_TOOLS")
    assert "nucleus_manage_thought_stream" not in {
        tool.tool_name for tool in ALL_TOOLS
    }


def test_legacy_list_reads_only_the_old_bounded_snapshot(tmp_path: Path) -> None:
    snapshot = _write_snapshot(tmp_path)
    before = snapshot.read_bytes()

    result = read_legacy_thought_stream_page(
        tmp_path,
        include_dormant=True,
        page_size=7,
        max_bytes=16 * 1024,
    )

    assert "旧时代原始记录" in result
    assert "authority=attention_thread" not in result
    assert len(result.encode("utf-8")) <= 16 * 1024
    assert snapshot.read_bytes() == before


def test_legacy_list_fails_closed_without_snapshot(tmp_path: Path) -> None:
    with pytest.raises(LegacySnapshotNotFoundError):
        read_legacy_thought_stream_page(tmp_path)


def test_legacy_list_rejects_invalid_budget(tmp_path: Path) -> None:
    _write_snapshot(tmp_path)

    with pytest.raises(ThoughtStreamProjectionError, match="max_bytes must be between"):
        read_legacy_thought_stream_page(tmp_path, max_bytes=1024)
