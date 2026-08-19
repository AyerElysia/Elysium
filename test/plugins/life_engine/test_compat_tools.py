"""life_engine 兼容工具暴露测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.life_engine.core.compat_tools import (
    LifeInnerQueryTool,
    LifeRecordInnerMonologueAction,
    LifeThinkAction,
)
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.plugin import LifeEnginePlugin


def test_life_engine_exposes_only_active_compat_tools_when_chatter_enabled() -> None:
    """意识实例只暴露活跃工具，不重新注册已收束的别名和零使用 action。"""
    config = LifeEngineConfig()
    config.chatter.enabled = True
    plugin = LifeEnginePlugin(config=config)

    component_names = {getattr(comp, "__name__", "") for comp in plugin.get_components()}

    assert "LifeThinkAction" not in component_names
    assert "LifeInnerDialogueTool" in component_names
    assert "LifeRecordInnerMonologueAction" in component_names
    assert "LifeMessageNucleusTool" not in component_names
    assert "LifeConsultNucleusTool" not in component_names
    assert "LifeSearchLifeMemoryTool" not in component_names
    assert "LifeRetrieveMemoryTool" not in component_names
    assert "LifeScheduleFollowupMessageAction" not in component_names


async def test_record_inner_monologue_action_delegates_to_life_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class _FakeService:
        async def record_chatter_inner_monologue(self, thought: str, **kwargs):
            calls.append((thought, kwargs))
            return {"queued": True}

    fake_plugin = SimpleNamespace(service=_FakeService())
    monkeypatch.setattr(
        "plugins.life_engine.core.compat_tools.get_plugin_manager",
        lambda: SimpleNamespace(get_plugin=lambda name: fake_plugin if name == "life_engine" else None),
    )

    action = LifeRecordInnerMonologueAction.__new__(LifeRecordInnerMonologueAction)
    action.chat_stream = SimpleNamespace(
        stream_id="stream-1",
        platform="qq",
        chat_type="private",
        bot_nickname="爱莉",
    )

    ok, message = await action.execute(
        thought="还是会想知道他现在在做什么。",
        mood="想念",
        intent="先轻轻等一下",
        topic="沉默等待",
    )

    assert ok is True
    assert "已记录" in message
    assert calls == [
        (
            "还是会想知道他现在在做什么。",
            {
                "stream_id": "stream-1",
                "platform": "qq",
                "chat_type": "private",
                "sender_name": "爱莉",
                "mood": "想念",
                "intent": "先轻轻等一下",
                "topic": "沉默等待",
            },
        )
    ]


async def test_think_action_delegates_snapshot_to_life_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class _FakeService:
        async def record_chatter_think_snapshot(self, **kwargs):
            calls.append(kwargs)
            return {"channel": "chatter_think_snapshot"}

    fake_plugin = SimpleNamespace(service=_FakeService())
    monkeypatch.setattr(
        "plugins.life_engine.core.compat_tools.get_plugin_manager",
        lambda: SimpleNamespace(get_plugin=lambda name: fake_plugin if name == "life_engine" else None),
    )

    action = LifeThinkAction.__new__(LifeThinkAction)
    action.plugin = fake_plugin
    action.chat_stream = SimpleNamespace(stream_id="stream-2")

    ok, message = await action.execute(
        mood="在意",
        decision="先把语气放软再回她",
        expected_response="她会觉得我还在认真听",
        thought="刚刚那句其实是在确认我是不是还在。",
    )

    assert ok is True
    assert "思考动作已记录" in message
    assert calls == [
        {
            "stream_id": "stream-2",
            "thought": "刚刚那句其实是在确认我是不是还在。",
            "mood": "在意",
            "decision": "先把语气放软再回她",
            "expected_response": "她会觉得我还在认真听",
        }
    ]


async def test_inner_query_awaits_async_world_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class _FakeService:
        @staticmethod
        def resolve_consciousness_instance(stream_id: str) -> str:
            assert stream_id == "stream-query"
            return "instance-query"

        async def query_world(self, instance_id: str, query: str) -> str:
            calls.append((instance_id, query))
            return "traceable world answer"

    fake_plugin = SimpleNamespace(service=_FakeService())
    monkeypatch.setattr(
        "plugins.life_engine.core.compat_tools.get_plugin_manager",
        lambda: SimpleNamespace(
            get_plugin=lambda name: fake_plugin if name == "life_engine" else None
        ),
    )
    tool = LifeInnerQueryTool.__new__(LifeInnerQueryTool)
    tool.chat_stream = SimpleNamespace(stream_id="stream-query")

    ok, result = await tool.execute("what changed")

    assert ok is True
    assert result == "traceable world answer"
    assert calls == [("instance-query", "what changed")]


async def test_schedule_followup_message_action_is_read_only_legacy() -> None:
    from plugins.life_engine.core.compat_tools import LifeScheduleFollowupMessageAction

    action = LifeScheduleFollowupMessageAction.__new__(LifeScheduleFollowupMessageAction)

    ok, message = await action.execute(
        delay_seconds=30.0,
        thought="继续想一下",
        topic="脑内思考",
        followup_type="add_detail",
    )

    assert ok is False
    assert "LegacyFollowupReadOnly" in message
