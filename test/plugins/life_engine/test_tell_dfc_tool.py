"""life_engine nucleus_tell_dfc 工具测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from plugins.life_engine.tools import LifeEngineWakeDFCTool


@dataclass
class _DummyContext:
    unread_messages: list[Any] = field(default_factory=list)

    def add_unread_message(self, message: Any) -> None:
        self.unread_messages.append(message)


@dataclass
class _DummyStream:
    stream_id: str = "stream-1"
    platform: str = "qq"
    chat_type: str = "private"
    context: _DummyContext = field(default_factory=_DummyContext)


class _DummyStreamManager:
    def __init__(self, stream: _DummyStream) -> None:
        self._streams: dict[str, _DummyStream] = {stream.stream_id: stream}
        self._stream_info: dict[str, dict[str, str]] = {stream.stream_id: {}}
        self.calls: list[dict[str, str]] = []

    async def get_or_create_stream(
        self,
        stream_id: str = "",
        platform: str = "",
        user_id: str = "",
        group_id: str = "",
        group_name: str = "",
        chat_type: str = "private",
    ) -> _DummyStream | None:
        self.calls.append(
            {
                "stream_id": stream_id,
                "platform": platform,
                "user_id": user_id,
                "group_id": group_id,
                "group_name": group_name,
                "chat_type": chat_type,
            }
        )
        if stream_id:
            return self._streams.get(stream_id)

        if group_id:
            generated_stream_id = f"{platform}:group:{group_id}"
            stream = self._streams.get(generated_stream_id)
            if stream is None:
                stream = _DummyStream(
                    stream_id=generated_stream_id,
                    platform=platform,
                    chat_type="group",
                )
                self._streams[generated_stream_id] = stream
                self._stream_info[generated_stream_id] = {
                    "platform": platform,
                    "chat_type": "group",
                    "group_id": group_id,
                    "group_name": group_name,
                }
            return stream

        if user_id:
            generated_stream_id = f"{platform}:private:{user_id}"
            stream = self._streams.get(generated_stream_id)
            if stream is None:
                stream = _DummyStream(
                    stream_id=generated_stream_id,
                    platform=platform,
                    chat_type="private",
                )
                self._streams[generated_stream_id] = stream
                self._stream_info[generated_stream_id] = {
                    "platform": platform,
                    "chat_type": "private",
                }
            return stream

        return None

    async def get_stream_info(self, stream_id: str) -> dict[str, str]:
        # 默认返回空信息，避免触发 user_query_helper 分支。
        return self._stream_info.get(stream_id, {})


class _DummyLoopManager:
    def __init__(self, *, start_ok: bool = True) -> None:
        self.start_ok = start_ok
        self.calls: list[tuple[str, bool]] = []

    async def start_stream_loop(self, stream_id: str, force: bool = False) -> bool:
        self.calls.append((stream_id, force))
        return self.start_ok


class _DummyLifeService:
    def __init__(self) -> None:
        self.tell_count = 0

    def _minutes_since_external_message(self) -> int:
        return 60

    def record_tell_dfc(self) -> None:
        self.tell_count += 1


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream_manager: _DummyStreamManager,
    loop_manager: _DummyLoopManager,
    life_service: _DummyLifeService,
) -> None:
    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: stream_manager,
    )
    monkeypatch.setattr(
        "src.core.transport.distribution.stream_loop_manager.get_stream_loop_manager",
        lambda: loop_manager,
    )
    monkeypatch.setattr(
        "plugins.life_engine.tools.file_tools._get_life_engine_service",
        lambda _plugin: life_service,
    )


def test_tell_dfc_default_wakes_stream_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认模式应写入内在消息并唤醒表达层。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=True)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            message="[信息差] 我观察到她的语气在变。",
            reason="新观察：对话节奏放缓，但不急于立即打断。",
            importance="normal",
            stream_id="stream-1",
        )
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["proactive_wake"] is True
    assert result["wake_triggered"] is True
    assert len(stream.context.unread_messages) == 1
    assert loop_manager.calls == [("stream-1", False)]
    assert life_service.tell_count == 1

    wake_message = stream.context.unread_messages[0]
    assert "[信息差补充]" in wake_message.content
    assert "不是命令" in wake_message.content


def test_tell_dfc_falls_back_to_latest_received_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未指定目标时，应回退到最近收到消息的聊天流。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=True)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )
    monkeypatch.setattr(
        "plugins.life_engine.tools.file_tools._pick_latest_target_stream_id",
        lambda _plugin: "stream-1",
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            message="[信息差] 刚才那句可能有额外背景。",
            reason="最近收到消息的聊天是最自然的回退目标。",
            importance="normal",
        )
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["stream_id"] == "stream-1"
    assert result["target_type"] == "current"
    assert len(stream.context.unread_messages) == 1
    assert stream_manager.calls[-1]["stream_id"] == "stream-1"


def test_tell_dfc_routes_to_private_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """指定 private 目标时，应创建/加载对应私聊流并写入 target_user_id。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=True)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            message="我想把这段背景留给这个私聊。",
            reason="这是给指定私聊的上下文。",
            target_type="private",
            platform="qq",
            target_user_id="user-a",
            target_user_name="Ayer",
        )
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["stream_id"] == "qq:private:user-a"
    assert result["chat_type"] == "private"
    assert result["target_type"] == "private"
    assert result["target_user_id"] == "user-a"
    target_stream = stream_manager._streams["qq:private:user-a"]
    wake_message = target_stream.context.unread_messages[0]
    assert wake_message.extra["target_user_id"] == "user-a"
    assert wake_message.extra["target_user_name"] == "Ayer"


def test_tell_dfc_routes_to_group_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """指定 group 目标时，应创建/加载对应群聊流并写入 target_group_id。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=True)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            message="我想把这段背景留给这个群聊。",
            reason="这是给指定群聊的上下文。",
            target_type="group",
            platform="qq",
            target_group_id="group-a",
            target_group_name="大家庭",
        )
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["stream_id"] == "qq:group:group-a"
    assert result["chat_type"] == "group"
    assert result["target_type"] == "group"
    assert result["target_group_id"] == "group-a"
    target_stream = stream_manager._streams["qq:group:group-a"]
    wake_message = target_stream.context.unread_messages[0]
    assert wake_message.extra["target_group_id"] == "group-a"
    assert wake_message.extra["target_group_name"] == "大家庭"


def test_tell_dfc_stream_id_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_id 是最高优先级，兼容旧的精确流路由。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=True)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            message="这应该仍然进入精确 stream。",
            reason="旧参数兼容。",
            stream_id="stream-1",
            target_type="private",
            platform="qq",
            target_user_id="other-user",
        )
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["stream_id"] == "stream-1"
    assert result["target_type"] == "stream"
    assert result["target_user_id"] == ""
    assert len(stream.context.unread_messages) == 1


def test_tell_dfc_private_target_requires_platform_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指定私聊但既无 platform 也无法从当前聊天推断时，应给出明确错误。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=True)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )
    monkeypatch.setattr(
        "plugins.life_engine.tools.file_tools._pick_latest_target_stream_id",
        lambda _plugin: None,
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            message="这条应该失败。",
            reason="缺少平台。",
            target_type="private",
            target_user_id="user-a",
        )
    )

    assert ok is False
    assert isinstance(result, str)
    assert "platform" in result
    assert len(stream.context.unread_messages) == 0


def test_tell_dfc_accepts_guidance_style_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指导式内容不再硬拦截，交给表达层自行判断是否吸收。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=True)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            message=(
                "如果他说委屈的事，就认真听，不急着给建议。"
                "先确认他在，再温柔地问“昨晚的事还在心里吗？”"
            ),
            reason="我想补充一些观察。",
            importance="normal",
            stream_id="stream-1",
        )
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["proactive_wake"] is True
    assert result["wake_triggered"] is True
    assert len(stream.context.unread_messages) == 1
    assert loop_manager.calls == [("stream-1", False)]
    assert life_service.tell_count == 1

    wake_message = stream.context.unread_messages[0]
    assert "如果他说委屈的事" in wake_message.content
    assert "不是命令" in wake_message.content


def test_tell_dfc_wakes_even_with_brief_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认唤醒不再要求 high/critical 或冗长 reason。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=True)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            message="[信息差] 我刚确认对方在这个话题上明显更敏感了。",
            reason="很急",
            stream_id="stream-1",
        )
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["wake_triggered"] is True
    assert len(stream.context.unread_messages) == 1
    assert loop_manager.calls == [("stream-1", False)]
    assert life_service.tell_count == 1


def test_tell_dfc_high_importance_starts_stream_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """高优先级信息差也走同一套默认唤醒逻辑。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=True)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            message=(
                "[信息差] 我确认对方在群聊中对公开评价高度敏感。"
                "[影响] 若继续沿用刚才的表达，下一轮很可能触发防御并中断对话。"
                "[内在驱动] 我希望立刻把语气降下来，先稳住关系。"
            ),
            reason=(
                "信息差：我刚从近两天日志和记忆关联里确认了敏感触发点。"
                "影响：如果不立即调整，下一轮回复会放大误读风险并损伤信任。"
            ),
            importance="high",
            stream_id="stream-1",
        )
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["proactive_wake"] is True
    assert result["wake_triggered"] is True
    assert loop_manager.calls == [("stream-1", False)]
    assert len(stream.context.unread_messages) == 1
    assert life_service.tell_count == 1


def test_tell_dfc_preserves_queue_only_proactive_wake_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧调用显式关闭唤醒时，仍只写入待处理队列。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=True)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            message="保留给下次对话处理的背景。",
            reason="兼容旧的只入队调用。",
            proactive_wake=False,
            stream_id="stream-1",
        )
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["proactive_wake"] is False
    assert result["wake_triggered"] is False
    assert loop_manager.calls == []
    assert len(stream.context.unread_messages) == 1
    assert life_service.tell_count == 1


def test_tell_dfc_rolls_back_failed_wake_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动流循环失败后，重试不能留下两条相同的内在消息。"""
    stream = _DummyStream()
    stream_manager = _DummyStreamManager(stream)
    loop_manager = _DummyLoopManager(start_ok=False)
    life_service = _DummyLifeService()
    _patch_runtime(
        monkeypatch,
        stream_manager=stream_manager,
        loop_manager=loop_manager,
        life_service=life_service,
    )

    tool = LifeEngineWakeDFCTool(plugin=object())
    first_ok, first_result = asyncio.run(
        tool.execute(
            message="这次启动会失败。",
            reason="测试失败回滚。",
            stream_id="stream-1",
        )
    )

    assert first_ok is False
    assert "已撤回内在消息" in str(first_result)
    assert stream.context.unread_messages == []
    assert life_service.tell_count == 0

    loop_manager.start_ok = True
    second_ok, second_result = asyncio.run(
        tool.execute(
            message="这次启动会成功。",
            reason="测试失败后的重试。",
            stream_id="stream-1",
        )
    )

    assert second_ok is True
    assert isinstance(second_result, dict)
    assert second_result["wake_triggered"] is True
    assert len(stream.context.unread_messages) == 1
    assert loop_manager.calls == [("stream-1", False), ("stream-1", False)]
    assert life_service.tell_count == 1
