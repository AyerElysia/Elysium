"""Retired ThoughtStream compatibility is read-only and never migrates meaning."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.streams.tools import LifeEngineManageThoughtStreamTool
from src.core.models.message import Message


def _tool(service: object, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setattr(
        "plugins.life_engine.streams.tools._get_service",
        lambda: service,
    )
    tool = LifeEngineManageThoughtStreamTool(SimpleNamespace())
    tool._bind_runtime_context(
        stream_id="stream:legacy-archive",
        message=Message(
            message_id="message:legacy-archive:1",
            time=1785960000.0,
            stream_id="stream:legacy-archive",
        ),
    )
    return tool


def _service(workspace: Path) -> object:
    return SimpleNamespace(
        _cfg=lambda: SimpleNamespace(
            settings=SimpleNamespace(workspace_path=str(workspace))
        )
    )


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


@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        ("create", {"title": "不能迁移"}),
        (
            "advance",
            {
                "stream_id": "ts_old",
                "expected_revision": 1,
                "thought": "不能续写",
            },
        ),
        (
            "retire",
            {
                "stream_id": "ts_old",
                "expected_revision": 1,
                "conclusion": "不能替主体关闭",
            },
        ),
        (
            "reactivate",
            {"stream_id": "ts_old", "expected_revision": 1},
        ),
    ],
)
def test_legacy_mutations_are_rejected_without_canonical_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    kwargs: dict[str, object],
) -> None:
    snapshot = _write_snapshot(tmp_path)
    tool = _tool(_service(tmp_path), monkeypatch)
    before = snapshot.read_bytes()

    ok, result = asyncio.run(tool.execute(action=action, **kwargs))

    assert ok is False
    assert result == {
        "error": "ThoughtStreamArchiveReadOnly",
        "action": action,
        "authority_committed": False,
        "replacement": "nucleus_proactive_command",
    }
    assert snapshot.read_bytes() == before


def test_legacy_list_reads_only_the_old_bounded_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _write_snapshot(tmp_path)
    tool = _tool(_service(tmp_path), monkeypatch)
    before = snapshot.read_bytes()

    ok, result = asyncio.run(
        tool.execute(
            action="list",
            include_dormant=True,
            page_size=7,
            max_bytes=16 * 1024,
        )
    )

    assert ok is True
    assert isinstance(result, str)
    assert "旧时代原始记录" in result
    assert "authority=attention_thread" not in result
    assert len(result.encode("utf-8")) <= 16 * 1024
    assert snapshot.read_bytes() == before


def test_legacy_list_fails_closed_without_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _tool(_service(tmp_path), monkeypatch)

    ok, result = asyncio.run(tool.execute(action="list"))

    assert ok is False
    assert result == "旧思考流只读快照未初始化"


def test_legacy_list_rejects_invalid_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_snapshot(tmp_path)
    tool = _tool(_service(tmp_path), monkeypatch)

    ok, result = asyncio.run(tool.execute(action="list", max_bytes=1024))

    assert ok is False
    assert "max_bytes must be between" in str(result)
