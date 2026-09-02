from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.elysium_console.catalog import (
    ConsoleDataInvalid,
    ElysiumDataCatalog,
    content_free_health,
    project_text,
    safe_value,
)


class _WorkspaceService:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def _workspace_dir(self) -> Path:
        return self._workspace


class _EventStore:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self.events = events
        self.read_limits: list[int] = []

    async def read_tail(self, limit: int) -> list[SimpleNamespace]:
        self.read_limits.append(limit)
        return self.events[-limit:]


class _TimelineService:
    def __init__(self, store: _EventStore) -> None:
        self.store = store

    def _get_life_event_store(self) -> _EventStore:
        return self.store


def test_text_projection_preserves_utf8_boundaries_and_exact_hash() -> None:
    text = "爱莉希雅"

    projection = project_text(text, 7)

    assert projection["content"] == "爱莉"
    assert projection["delivered_bytes"] == 6
    assert projection["original_bytes"] == len(text.encode("utf-8"))
    assert projection["content_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert projection["complete"] is False


def test_public_serialization_redacts_credentials_and_health_bodies() -> None:
    payload = {
        "status": "healthy",
        "api_key": "must-not-leak",
        "nested": {"access_token": "also-secret", "count": 3},
    }

    assert safe_value(payload)["api_key"] == "<redacted>"
    assert safe_value(payload)["nested"]["access_token"] == "<redacted>"
    health = content_free_health({"status": "healthy", "thought": "private"})
    assert health == {"status": "healthy", "thought": "<redacted>"}


@pytest.mark.asyncio
async def test_timeline_is_bounded_and_does_not_mutate_event_store() -> None:
    event = SimpleNamespace(
        event_id="event-1",
        occurrence_id="occurrence-1",
        sequence=7,
        timestamp="2026-09-02T12:00:00+08:00",
        recorded_at="2026-09-02T12:00:01+08:00",
        source="chatter",
        source_instance_id="instance-1",
        channel="chat",
        event_type="message.sent",
        stream_id="stream-1",
        causation_id="cause-1",
        correlation_id="correlation-1",
        priority=50,
        content="爱" * 10_000,
        metadata={"password": "hidden", "direction": "sent"},
    )
    store = _EventStore([event])
    catalog = ElysiumDataCatalog(lambda: _TimelineService(store))

    result = await catalog.timeline(limit=500)

    assert store.read_limits == [200]
    assert result["items"][0]["content"]["complete"] is False
    assert result["items"][0]["metadata"]["password"] == "<redacted>"
    assert event.content == "爱" * 10_000


@pytest.mark.asyncio
async def test_subject_documents_are_exact_current_authority_snapshots() -> None:
    class Service:
        async def read_subject_authority_texts(self) -> dict[str, str]:
            return {"SOUL.md": "灵魂", "USER.md": "小星星", "MEMORY.md": "经历"}

    result = await ElysiumDataCatalog(Service).subject_documents()

    assert [item["path"] for item in result["items"]] == [
        "SOUL.md",
        "USER.md",
        "MEMORY.md",
    ]
    assert all(item["complete"] for item in result["items"])
    assert result["items"][0]["sha256"] == hashlib.sha256("灵魂".encode()).hexdigest()


@pytest.mark.asyncio
async def test_workspace_only_exposes_allowlisted_roots_and_utf8_chunks(
    tmp_path: Path,
) -> None:
    (tmp_path / "SOUL.md").write_text("爱莉希雅", encoding="utf-8")
    (tmp_path / "config.toml").write_text("secret = true", encoding="utf-8")
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "today.md").write_text("今天很好", encoding="utf-8")
    (notes / ".hidden.md").write_text("hidden", encoding="utf-8")
    (notes / "escape.md").symlink_to(tmp_path / "config.toml")
    catalog = ElysiumDataCatalog(lambda: _WorkspaceService(tmp_path))

    root = await catalog.workspace_page(path="", limit=100)
    assert {item["name"] for item in root["items"]} == {"SOUL.md", "notes"}

    page = await catalog.workspace_page(path="notes", limit=100)
    assert [item["name"] for item in page["items"]] == ["today.md"]

    first = await catalog.workspace_text(path="SOUL.md", max_bytes=4)
    assert first["content"] == "爱"
    assert first["next_offset_bytes"] == 3
    second = await catalog.workspace_text(
        path="SOUL.md",
        offset_bytes=first["next_offset_bytes"],
        max_bytes=64,
    )
    assert first["content"] + second["content"] == "爱莉希雅"
    assert second["complete"] is True

    with pytest.raises(ConsoleDataInvalid):
        await catalog.workspace_text(path="SOUL.md", offset_bytes=1)

    with pytest.raises(ConsoleDataInvalid):
        await catalog.workspace_text(path="../config.toml")
    with pytest.raises(ConsoleDataInvalid):
        await catalog.workspace_text(path="notes/.hidden.md")
    with pytest.raises(ConsoleDataInvalid):
        await catalog.workspace_text(path="notes/escape.md")


def test_experience_cursor_requires_composite_identity() -> None:
    assert ElysiumDataCatalog._experience_cursor(0, "") is None
    cursor = ElysiumDataCatalog._experience_cursor(12, "occurrence-12")
    assert cursor is not None
    assert cursor.ingest_position == 12
    with pytest.raises(ConsoleDataInvalid):
        ElysiumDataCatalog._experience_cursor(12, "")
