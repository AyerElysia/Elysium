"""life_engine 对话提示词与叙事测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.chatter import LifeChatter, _Phase, _WorkflowRuntime
from plugins.life_engine.constants import LIFE_CHATTER_GLOBAL_CURSOR_KEY
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent
from plugins.life_engine.tools.exec_tools import LifeEngineBashTool
from plugins.life_engine.tools.file_tools import LifeEngineRunAgentTool, LifeEngineWakeDFCTool
from src.core.components.base.chatter import BaseChatter, Success, Wait
from src.core.models.message import Message
from src.kernel.llm import LLMContextManager, LLMPayload, ROLE, Text
import pytest

def test_life_chatter_system_prompt_includes_memory_and_chatter_tools_not_heartbeat_tool(tmp_path) -> None:
    """聊天态应共享 SOUL/USER/MEMORY/TOOLS，并保留核心工具说明。"""
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text(
        "\n".join(
            [
                "# 值得记住的事",
                "",
                "这里是一大段给编辑者看的说明，不该原样注入。",
                "",
                "### Durable（持久）",
                "- MEMORY_DURABLE",
                "",
                "### Active（活跃）",
                "- MEMORY_ACTIVE",
                "",
                "### Fading（待审视）",
                "- MEMORY_FADING",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "TOOL.md").write_text("TOOL_CONTENT", encoding="utf-8")
    (tmp_path / "TOOLS.md").write_text("CHATTER_TOOLS_CONTENT", encoding="utf-8")

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)
    prompt = chatter._build_chat_system_prompt(service=None)

    assert "SOUL_CONTENT" in prompt
    assert "USER_CONTENT" in prompt
    assert "MEMORY_DURABLE" in prompt
    assert "MEMORY_ACTIVE" in prompt
    assert "MEMORY_FADING" not in prompt
    assert "给编辑者看的说明" not in prompt
    assert "TOOL_CONTENT" not in prompt
    assert "CHATTER_TOOLS_CONTENT" in prompt
    assert "action-think" in prompt
    assert "action-life_pass_and_wait" in prompt
    assert "life_send_text" in prompt
    assert "reason" in prompt


def test_life_chatter_persistent_user_prompt_excludes_dynamic_context() -> None:
    """持久 USER prompt 不应写入 inner_state/recent_context 等动态快照。"""
    chatter = LifeChatter.__new__(LifeChatter)
    chat_stream = SimpleNamespace(stream_name="Test", stream_id="stream-1")

    prompt = chatter._build_chat_user_prompt(
        chat_stream,
        unread_lines="新消息",
        history_text="历史消息",
    )

    assert "<chat_history>" in prompt
    assert "<new_messages>" in prompt
    assert "<inner_state>" not in prompt
    assert "<recent_context>" not in prompt
    assert "<runtime_assistant_context>" not in prompt


def test_life_chatter_context_compression_hook_preserves_dropped_history() -> None:
    manager = LLMContextManager()
    request = SimpleNamespace(context_manager=manager)

    LifeChatter._install_context_compression_hook(request)

    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("system")),
        LLMPayload(ROLE.USER, Text("旧用户消息")),
        LLMPayload(ROLE.ASSISTANT, Text("旧回复")),
        LLMPayload(ROLE.USER, Text("新用户消息")),
        LLMPayload(ROLE.ASSISTANT, Text("新回复")),
    ]

    trimmed = manager.maybe_trim(
        payloads,
        max_token_budget=4,
        token_counter=lambda items: len(items),
    )

    assert len(trimmed) == 4
    assert trimmed[0].role == ROLE.SYSTEM
    assert trimmed[1].role == ROLE.USER
    compressed = trimmed[1].content[0].text
    assert "<compressed_life_chatter_context>" in compressed
    assert "旧用户消息" in compressed
    assert "旧回复" in compressed
    assert trimmed[2].content[0].text == "新用户消息"


@pytest.mark.asyncio
async def test_life_chatter_global_runtime_is_reused(monkeypatch) -> None:
    LifeChatter.reset_global_runtime()
    created_requests: list[SimpleNamespace] = []

    def fake_create_request(self, *_args, **_kwargs):
        request = SimpleNamespace(payloads=[])
        request.add_payload = lambda payload: request.payloads.append(payload)
        created_requests.append(request)
        return request

    async def fake_inject_usables(self, request):
        return {"request_id": id(request)}

    monkeypatch.setattr(LifeChatter, "create_request", fake_create_request)
    monkeypatch.setattr(LifeChatter, "inject_usables", fake_inject_usables)

    first = LifeChatter.__new__(LifeChatter)
    first.plugin = SimpleNamespace(config=None)
    first.stream_id = "stream-a"
    second = LifeChatter.__new__(LifeChatter)
    second.plugin = SimpleNamespace(config=None)
    second.stream_id = "stream-b"

    stream_a = SimpleNamespace(stream_id="stream-a")
    stream_b = SimpleNamespace(stream_id="stream-b")

    rt_a, usable_a = await first._get_or_create_global_runtime(None, stream_a)
    rt_b, usable_b = await second._get_or_create_global_runtime(None, stream_b)

    assert rt_a is rt_b
    assert usable_a is usable_b
    assert len(created_requests) == 1

    LifeChatter.reset_global_runtime()


@pytest.mark.asyncio
async def test_life_chatter_global_runtime_follow_up_stays_on_owner_stream(monkeypatch) -> None:
    LifeChatter.reset_global_runtime()
    rt = _WorkflowRuntime(
        response=SimpleNamespace(payloads=[]),
        phase=_Phase.FOLLOW_UP,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-b"

    async def fail_fetch_unreads():
        raise AssertionError("non-owner stream must not inspect or advance shared runtime")

    monkeypatch.setattr(chatter, "fetch_unreads", fail_fetch_unreads)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-b"),
        service=None,
    )

    assert isinstance(result, Wait)
    assert rt.phase == _Phase.FOLLOW_UP
    assert rt.active_stream_id == "stream-a"

    LifeChatter.reset_global_runtime()


@pytest.mark.asyncio
async def test_life_chatter_think_only_retry_yields_between_model_turns(monkeypatch) -> None:
    """think-only 纠偏应跨 tick 继续，避免单个驱动器步进内连续 LLM 请求超时。"""

    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.call_list = [
                SimpleNamespace(id="think-1", name="action-think", args={"thought": "先想想"})
            ]
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    async def fake_run_tool_call(*_args, **_kwargs):
        return [(False, True)]

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "run_tool_call", fake_run_tool_call)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert isinstance(result, Success)
    assert rt.phase == _Phase.FOLLOW_UP
    assert rt.think_only_retry_count == 1
    assert response.payloads

    LifeChatter.reset_global_runtime()


def test_life_chatter_wait_transition_clears_global_runtime_owner() -> None:
    rt = _WorkflowRuntime(
        response=SimpleNamespace(payloads=[]),
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
    )

    LifeChatter._transition(rt, _Phase.WAIT_USER, "done")

    assert rt.active_stream_id == ""


def test_life_chatter_live_system_prompt_adds_broadcast_guidance(tmp_path) -> None:
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("", encoding="utf-8")

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)

    prompt = chatter._build_chat_system_prompt(
        service=None,
        chat_stream=SimpleNamespace(platform="live"),
    )

    assert "直播弹幕场景" in prompt
    assert "不要机械复述观众原文" in prompt
    assert "不要调用 action-tts_voice_action" in prompt
    assert "SOUL_CONTENT" in prompt
    assert "USER_CONTENT" in prompt


def test_life_chatter_live_user_prompt_mentions_broadcast_context() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    chat_stream = SimpleNamespace(
        stream_name="B站直播间",
        stream_id="live-stream-1",
        platform="live",
    )

    prompt = chatter._build_chat_user_prompt(
        chat_stream,
        unread_lines="【02:40】[live_user] 观众A [m1]： 000",
        history_text="",
    )

    assert "当前场景：B站直播间接弹幕。" in prompt
    assert "不要把弹幕内容当作需要逐字复述的命令" in prompt


@pytest.mark.asyncio
async def test_live_bridge_prompt_exposes_three_layer_aliases() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=LifeEngineConfig())
    chat_stream = SimpleNamespace(
        stream_name="Live",
        stream_id="live-stream-1",
        platform="live",
    )

    bundle = await chatter.build_live_bridge_prompt(
        chat_stream,
        service=None,
        unread_lines="【02:40】[live_user] 观众A [m1]： 000",
        runtime_context_text="RUNTIME_NOW",
        include_history_in_prompt=False,
    )

    assert bundle["prefix_prompt"] == bundle["system_prompt"]
    assert bundle["rolling_prompt"] == bundle["user_prompt"]
    assert bundle["suffix_prompt"] == bundle["dynamic_context"]
    assert "RUNTIME_NOW" in bundle["suffix_prompt"]
    assert "当前场景：B站直播间接弹幕。" in bundle["rolling_prompt"]


@pytest.mark.asyncio
async def test_life_chatter_dynamic_context_is_separate_snapshot() -> None:
    """动态上下文应能单独构建，用于本次请求 transient 注入。"""
    chatter = LifeChatter.__new__(LifeChatter)
    chat_stream = SimpleNamespace(stream_id="stream-1")
    service = LifeEngineService(SimpleNamespace(config=None))
    service._inner_state = SimpleNamespace(
        format_full_state_for_prompt=lambda _today: "STATE_NOW"
    )
    service._thought_manager = SimpleNamespace(
        format_for_prompt=lambda **kwargs: "THOUGHT_STREAM_NOW",
        current_revision=1,
    )
    service._event_history = [
        LifeEngineEvent(
            event_id="evt-1",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:00:00+08:00",
            sequence=1,
            source="life_engine",
            source_detail="dfc",
            content="RECENT_EVENT",
            content_type="dfc_message",
            stream_id="stream-1",
            sender="dfc",
        )
    ]

    dynamic, high_water = await chatter._build_dynamic_context_text(
        chat_stream,
        service,
        runtime_context_text="RUNTIME_NOW",
    )

    assert "<life_runtime_context>" in dynamic
    assert "STATE_NOW" in dynamic
    assert "THOUGHT_STREAM_NOW" in dynamic
    assert "RECENT_EVENT" in dynamic
    assert "RUNTIME_NOW" in dynamic
    assert high_water == 1


@pytest.mark.asyncio
async def test_life_chatter_filters_tts_action_for_live_bridge(monkeypatch) -> None:
    class FakeTTSAction:
        @classmethod
        def get_signature(cls) -> str:
            return "tts_voice_plugin:action:tts_voice_action"

    class FakeTextAction:
        @classmethod
        def get_signature(cls) -> str:
            return "life_engine:action:life_send_text"

    async def fake_super_modify(self, llm_usables):
        return [FakeTTSAction, FakeTextAction]

    class DummyStreamManager:
        async def get_or_create_stream(self, stream_id: str):
            return SimpleNamespace(stream_id=stream_id, platform="live")

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.stream_id = "live-stream-1"
    chatter.plugin = SimpleNamespace(config=None)

    monkeypatch.setattr(BaseChatter, "modify_llm_usables", fake_super_modify)
    import src.core.managers as managers

    monkeypatch.setattr(managers, "get_stream_manager", lambda: DummyStreamManager())

    available = await chatter.modify_llm_usables([])

    assert [cls.get_signature() for cls in available] == [
        "life_engine:action:life_send_text"
    ]


@pytest.mark.asyncio
async def test_life_chatter_watchdog_keepalive_feeds_during_long_await(monkeypatch) -> None:
    feed_calls: list[str] = []

    class DummyWatchDog:
        def feed_dog(self, stream_id: str) -> None:
            feed_calls.append(stream_id)

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.stream_id = "live-stream-1"

    import src.kernel.concurrency as concurrency

    monkeypatch.setattr(concurrency, "get_watchdog", lambda: DummyWatchDog())

    async def slow_job() -> str:
        await asyncio.sleep(0.12)
        return "ok"

    result = await chatter._await_with_watchdog_keepalive(
        slow_job(),
        interval=0.02,
    )

    assert result == "ok"
    assert len(feed_calls) >= 2
    assert all(stream_id == "live-stream-1" for stream_id in feed_calls)


@pytest.mark.asyncio
async def test_life_chatter_runtime_context_cursor_avoids_repeat_injection() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    service._event_history = [
        LifeEngineEvent(
            event_id="evt-1",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:00:00+08:00",
            sequence=1,
            source="life_engine",
            source_detail="dfc",
            content="OLD_LIFE_EVENT",
            content_type="dfc_message",
            stream_id="stream-1",
            sender="dfc",
        ),
        LifeEngineEvent(
            event_id="evt-2",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:01:00+08:00",
            sequence=2,
            source="life_engine",
            source_detail="dfc",
            content="NEW_LIFE_EVENT",
            content_type="dfc_message",
            stream_id="stream-1",
            sender="dfc",
        ),
    ]
    chat_stream = SimpleNamespace(stream_id="stream-1")

    first_text, first_high_water = await service.build_chatter_runtime_context(chat_stream)
    await service.mark_chatter_runtime_context_seen(chat_stream.stream_id, 1)
    second_text, second_high_water = await service.build_chatter_runtime_context(chat_stream)
    await service.mark_chatter_runtime_context_seen(chat_stream.stream_id, first_high_water)
    third_text, third_high_water = await service.build_chatter_runtime_context(chat_stream)

    assert "OLD_LIFE_EVENT" in first_text
    assert "NEW_LIFE_EVENT" in first_text
    assert first_high_water == 2
    assert "OLD_LIFE_EVENT" not in second_text
    assert "NEW_LIFE_EVENT" in second_text
    assert second_high_water == 2
    assert third_text == ""
    assert third_high_water == 2


@pytest.mark.asyncio
async def test_life_chatter_unified_runtime_context_uses_global_cursor() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    service._event_history = [
        LifeEngineEvent(
            event_id="evt-a",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:00:00+08:00",
            sequence=1,
            source="qq",
            source_detail="qq | 入站 | 私聊 | A",
            content="A_STREAM_EVENT",
            content_type="text",
            stream_id="stream-a",
            sender="A",
        ),
        LifeEngineEvent(
            event_id="evt-b",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:01:00+08:00",
            sequence=2,
            source="qq",
            source_detail="qq | 入站 | 私聊 | B",
            content="B_CURRENT_STREAM_TEXT",
            content_type="text",
            stream_id="stream-b",
            sender="B",
        ),
    ]

    chat_stream = SimpleNamespace(stream_id="stream-b")
    first_text, first_high_water = await service.build_chatter_runtime_context(
        chat_stream,
        unified_chatter_context=True,
    )
    await service.mark_chatter_runtime_context_seen(
        chat_stream.stream_id,
        first_high_water,
        unified_chatter_context=True,
    )
    second_text, second_high_water = await service.build_chatter_runtime_context(
        chat_stream,
        unified_chatter_context=True,
    )

    assert "A_STREAM_EVENT" in first_text
    assert "B_CURRENT_STREAM_TEXT" in first_text
    assert first_high_water == 2
    assert service._state.chatter_context_cursors[LIFE_CHATTER_GLOBAL_CURSOR_KEY] == 2
    assert second_text == ""
    assert second_high_water == 2


@pytest.mark.asyncio
async def test_life_chatter_unified_runtime_context_summarizes_event_flood() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    service._event_history = [
        LifeEngineEvent(
            event_id=f"evt-{index}",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:00:00+08:00",
            sequence=index,
            source="live",
            source_detail="live | 入站 | 弹幕",
            content=f"BULK_EVENT_{index:03d}",
            content_type="text",
            stream_id="live-stream",
            sender="viewer",
        )
        for index in range(1, 101)
    ]

    first_text, first_high_water = await service.build_chatter_runtime_context(
        SimpleNamespace(stream_id="chat-stream"),
        unified_chatter_context=True,
    )

    assert first_high_water == 100
    assert "潜意识已压缩" in first_text
    assert "BULK_EVENT_100" in first_text
    assert "BULK_EVENT_001" in first_text


def test_life_chatter_transient_context_can_be_stripped() -> None:
    """发送前临时注入的动态上下文不应残留在持久 payload。"""
    response = SimpleNamespace(
        payloads=[LLMPayload(ROLE.USER, Text("PERSISTENT_USER"))]
    )

    LifeChatter._append_transient_context(response, "STATE_NOW")
    assert any(
        isinstance(part, Text) and "STATE_NOW" in part.text
        for part in response.payloads[0].content
    )

    LifeChatter._strip_transient_context(response)

    assert response.payloads[0].content == [response.payloads[0].content[0]]
    assert response.payloads[0].content[0].text == "PERSISTENT_USER"


def test_life_chatter_second_turn_prompt_does_not_repeat_history() -> None:
    """第二轮应只追加新消息，不重复注入 chat_history 尾巴。"""
    chatter = LifeChatter.__new__(LifeChatter)
    chat_stream = SimpleNamespace(stream_name="Test", stream_id="stream-1")

    first_turn = chatter._build_chat_user_prompt(
        chat_stream,
        unread_lines="第一轮新消息",
        history_text="首轮历史",
    )
    second_turn = chatter._build_chat_user_prompt(
        chat_stream,
        unread_lines="第二轮新消息",
        history_text="",
    )

    assert "<chat_history>" in first_turn
    assert "首轮历史" in first_turn
    assert "<chat_history>" not in second_turn
    assert "第二轮新消息" in second_turn


def test_life_chatter_history_excludes_internal_prompt_messages() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    chat_stream = SimpleNamespace(
        context=SimpleNamespace(
            history_messages=[
                Message(
                    message_id="user_1",
                    processed_plain_text="真正的聊天历史",
                    sender_name="Ayer",
                    stream_id="stream-1",
                ),
                Message(
                    message_id="proactive_opportunity_x",
                    processed_plain_text="系统主动机会",
                    sender_name="系统",
                    stream_id="stream-1",
                    is_proactive_opportunity_trigger=True,
                ),
                Message(
                    message_id="inner_monologue_x",
                    processed_plain_text="[内心独白] 我有点想他",
                    sender_name="爱莉",
                    stream_id="stream-1",
                    is_inner_monologue=True,
                ),
            ]
        )
    )

    history = chatter._build_history_text(chat_stream, max_messages=10)

    assert "真正的聊天历史" in history
    assert "系统主动机会" not in history
    assert "内心独白" not in history


def test_tell_dfc_tool_description_frames_as_runtime_mode_sync() -> None:
    """nucleus_tell_dfc 的叙事应指向运行模式同步，而不是双意识。"""
    description = LifeEngineWakeDFCTool.tool_description

    assert "同一主体的表达层" in description
    assert "不是在和另一个意识体对话" in description
    assert "信息差" in description
    assert "不用于指导" in description
    assert "事实、背景、记忆线索、情绪来源或潜在风险" in description
    assert "台词、步骤或策略" in description
    assert "你应该回复 X" in description
    assert "不用于催表达层开口" in description


def test_execution_tool_descriptions_respect_heartbeat_boundary() -> None:
    """执行类工具 schema 自身也要约束心跳态，不只依赖系统 prompt。"""
    bash_description = LifeEngineBashTool.tool_description
    agent_description = LifeEngineRunAgentTool.tool_description

    assert "潜意识 / 内在状态层" in bash_description
    assert "只在诊断 life_engine 自己的 workspace、日志、工具链异常时使用" in bash_description
    assert "不要用它查用户项目配置、跑用户任务、生成图片、改代码或处理外部系统" in bash_description
    assert "交给 life_chatter / 表达层" in bash_description

    assert "不是把用户请求转交后台执行的入口" in agent_description
    assert "只用于整理 life_engine 私有记忆、笔记、思考流" in agent_description
    assert "不要让子代理承接用户任务、查项目配置、跑命令、改代码、画图" in agent_description
    assert "交给 life_chatter / 表达层判断和执行" in agent_description


def test_heartbeat_prompt_bounds_tell_dfc_to_context_gap(tmp_path) -> None:
    """心跳 prompt 应把 nucleus_tell_dfc 限定为补信息差，而不是指导表达层。"""
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(SimpleNamespace(config=config))

    prompt = "\n".join(service._build_prompt_header())

    assert "观察、思考、联想和沉淀" in prompt
    assert "不是后台执行器，也不是表达层" in prompt
    assert "主动表达" not in prompt
    assert "是否画画、是否查配置或跑命令，由表达层结合用户请求自行决定" in prompt
    assert "只在表达层当前看不到事实、背景、线索或风险时" in prompt
    assert "这个工具用于补充背景，不用于指导表达层怎么说、怎么做" in prompt
    assert "不要拿它查项目配置、跑用户任务或处理外部操作" in prompt
    assert "不要用子智能体承接用户任务、画图、查项目配置、跑命令" in prompt
    assert "你应该回复 X" in prompt
    assert "你去安慰/追问 Y" in prompt
    assert "只服务高优先级信息差，不用于催表达层开口" in prompt
    assert "如果没有明确需要，可以不调用工具" in prompt
    assert "有冲动就行动" not in prompt


def test_social_impulses_do_not_route_directly_to_tell_dfc() -> None:
    """社交类冲动不应默认把 nucleus_tell_dfc 当作主动表达出口。"""
    from plugins.life_engine.drives.rules import break_silence, social_reach_out

    assert "nucleus_tell_dfc" not in social_reach_out.tools
    assert "nucleus_tell_dfc" not in break_silence.tools
    assert "表达层缺失的关键背景" in social_reach_out.suggestion
    assert "明确的信息差" in break_silence.suggestion
