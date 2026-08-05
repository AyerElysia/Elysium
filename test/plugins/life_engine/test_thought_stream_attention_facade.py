from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.life_engine.attention_threads import AttentionThreadCommit
from plugins.life_engine.streams.manager import ThoughtStreamManager
from plugins.life_engine.streams.tools import LifeEngineManageThoughtStreamTool
from src.core.models.message import Message


class _LegacyWriteBomb:
    def __getattr__(self, name: str):
        raise AssertionError(f"canonical facade touched legacy manager: {name}")


class _CanonicalService:
    def __init__(self) -> None:
        self._attention_thread_service = object()
        self._thought_manager = _LegacyWriteBomb()
        self.commands = []
        self.queries = []
        self.consciousness_registry = SimpleNamespace(
            get=lambda identity: SimpleNamespace(
                instance_id=identity,
                is_active=True,
            )
        )

    def resolve_consciousness_instance(self, stream_id: str) -> str:
        assert stream_id == "stream:legacy-facade"
        return "consciousness:legacy-facade"

    async def decide_attention_thread(self, command):
        self.commands.append(command)
        status = {
            "pause": "paused",
            "close": "closed",
        }.get(command.action, "open")
        return AttentionThreadCommit(
            event_id=f"attention:event:{len(self.commands)}",
            occurrence_id=command.occurrence_id,
            thread_id=command.thread_id,
            revision=command.expected_revision + 1,
            status=status,
            idempotent_replay=False,
        )

    async def page_attention_threads(self, query):
        self.queries.append(query)
        content = (
            '<attention_threads algorithm="attention-thread-ref-v1">\n'
            '- thread_ref="attention:thread:1" status="open"\n'
            "</attention_threads>"
        )
        return SimpleNamespace(
            content=content,
            source_frontier=7,
            items=(object(),),
            delivered_bytes=len(content.encode("utf-8")),
            omitted_count=3,
            continuation="canonical-next",
        )


def _tool(service, monkeypatch) -> LifeEngineManageThoughtStreamTool:
    monkeypatch.setattr(
        "plugins.life_engine.streams.tools._get_service",
        lambda: service,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    tool = LifeEngineManageThoughtStreamTool(SimpleNamespace())
    tool._bind_runtime_context(
        stream_id="stream:legacy-facade",
        message=Message(
            message_id="message:legacy-facade:1",
            time=1785960000.0,
            stream_id="stream:legacy-facade",
        ),
    )
    return tool


def test_legacy_mutations_map_only_explicit_subject_fields(monkeypatch) -> None:
    service = _CanonicalService()
    tool = _tool(service, monkeypatch)

    opened_ok, opened = asyncio.run(
        tool.execute(
            action="create",
            title="我愿意持续关注这条线索",
            reason="legacy reason must not be persisted",
            absorb_curiosity=True,
        )
    )
    thread_id = service.commands[-1].thread_id
    noted_ok, noted = asyncio.run(
        tool.execute(
            action="advance",
            stream_id=thread_id,
            expected_revision=1,
            thought="这是我明确愿意长期公开保留的新记录",
            curiosity_delta=0.9,
        )
    )
    paused_ok, paused = asyncio.run(
        tool.execute(
            action="retire",
            stream_id=thread_id,
            expected_revision=2,
            new_status="dormant",
            conclusion="legacy pause reason must not be persisted",
        )
    )
    resumed_ok, resumed = asyncio.run(
        tool.execute(
            action="reactivate",
            stream_id=thread_id,
            expected_revision=3,
        )
    )
    closed_ok, closed = asyncio.run(
        tool.execute(
            action="retire",
            stream_id=thread_id,
            expected_revision=4,
            new_status="completed",
            conclusion="我明确选择关闭，并只保留这句公开结语",
        )
    )

    assert all((opened_ok, noted_ok, paused_ok, resumed_ok, closed_ok))
    assert [command.action for command in service.commands] == [
        "open",
        "note",
        "pause",
        "resume",
        "close",
    ]
    assert [command.public_statement for command in service.commands] == [
        "我愿意持续关注这条线索",
        "这是我明确愿意长期公开保留的新记录",
        "",
        "",
        "我明确选择关闭，并只保留这句公开结语",
    ]
    assert all(
        command.actor_consciousness_instance_id
        == "consciousness:legacy-facade"
        for command in service.commands
    )
    assert opened["ignored_legacy_fields"] == ["reason", "absorb_curiosity"]
    assert noted["ignored_legacy_fields"] == ["curiosity_delta"]
    assert paused["ignored_legacy_fields"] == ["conclusion"]
    assert resumed["ignored_legacy_fields"] == []
    assert closed["ignored_legacy_fields"] == []


def test_legacy_list_prefers_canonical_bounded_projection(monkeypatch) -> None:
    service = _CanonicalService()
    tool = _tool(service, monkeypatch)

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
    assert len(result.encode("utf-8")) <= 16 * 1024
    assert "authority=attention_thread" in result
    assert "next_cursor=canonical-next" in result
    query = service.queries[0]
    assert query.statuses == ("open", "paused")
    assert query.limit == 7
    assert query.max_bytes == 11 * 1024
    assert query.projection_kind == "legacy_thought_stream_facade"


def test_legacy_mutation_fails_closed_without_canonical_authority(
    tmp_path, monkeypatch
) -> None:
    manager = ThoughtStreamManager(str(tmp_path))
    service = SimpleNamespace(
        _attention_thread_service=None,
        _thought_manager=manager,
    )
    tool = _tool(service, monkeypatch)
    before = manager._index_file.read_bytes() if manager._index_file.exists() else b""

    ok, result = asyncio.run(tool.execute(action="create", title="不能落入旧权威"))

    after = manager._index_file.read_bytes() if manager._index_file.exists() else b""
    assert ok is False
    assert "拒绝创建或修改第二套权威" in result
    assert manager.list_all() == []
    assert after == before


def test_canonical_list_rejects_budget_too_small_for_exact_continuation(
    monkeypatch,
) -> None:
    service = _CanonicalService()
    tool = _tool(service, monkeypatch)

    ok, result = asyncio.run(tool.execute(action="list", max_bytes=8 * 1024))

    assert ok is False
    assert "requires max_bytes" in result
    assert service.queries == []
