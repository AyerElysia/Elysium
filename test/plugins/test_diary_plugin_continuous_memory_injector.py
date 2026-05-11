"""diary_plugin 连续记忆 prompt 注入测试。"""

from __future__ import annotations

import asyncio
from typing import Any
from types import SimpleNamespace

from plugins.diary_plugin.config import DiaryConfig
from plugins.diary_plugin.event_handler import (
    AutoDiaryEventHandler,
    ContinuousMemoryPromptInjector,
    _PENDING_RUNTIME_USER_PROMPT_INJECTIONS,
    _push_runtime_user_prompt_injection,
)
from src.kernel.event import EventDecision


def test_continuous_memory_injects_into_user_prompt_head_block() -> None:
    """连续记忆应注入到 user prompt 开头的 continuous_memory 区块。"""

    config = DiaryConfig()
    config.continuous_memory.enabled = True
    config.continuous_memory.inject_prompt = True
    config.continuous_memory.target_prompt_names = ["default_chatter_user_prompt"]

    handler = ContinuousMemoryPromptInjector(plugin=SimpleNamespace(config=config))

    block = "## 连续记忆\n\n- [L1] 已经存在的内容"

    class _DummyService:
        def render_continuous_memory_for_prompt(self, stream_id: str, chat_type: str | None = None) -> str:
            assert stream_id == "sid_x"
            assert chat_type == "private"
            return block

    handler._get_service = lambda: _DummyService()  # type: ignore[method-assign]

    params: dict[str, Any] = {
        "name": "default_chatter_user_prompt",
        "template": "{continuous_memory}\n{history}",
        "values": {
            "stream_id": "sid_x",
            "chat_type": "private",
            "continuous_memory": "old",
            "history": "keep",
        },
        "policies": {},
        "strict": False,
    }

    decision, out = asyncio.run(handler.execute("on_prompt_build", params))

    assert decision is EventDecision.SUCCESS
    assert out["values"]["continuous_memory"] == (
        "<continuous_memory_block>\n"
        f"{block}\n"
        "</continuous_memory_block>"
    )
    assert out["values"]["history"] == "keep"


def test_auto_diary_runtime_user_prompt_injection_is_one_shot() -> None:
    """自动日记摘要应只在下一次 user prompt 注入一次，然后清空。"""

    _PENDING_RUNTIME_USER_PROMPT_INJECTIONS.clear()
    config = DiaryConfig()
    config.auto_diary.inject_runtime_user_prompt_once = True
    config.auto_diary.runtime_user_prompt_target_names = ["default_chatter_user_prompt"]
    config.continuous_memory.enabled = False

    handler = ContinuousMemoryPromptInjector(plugin=SimpleNamespace(config=config))
    handler._get_service = lambda: None  # type: ignore[method-assign]

    _push_runtime_user_prompt_injection(
        "sid_once",
        "【自动日记摘要】测试内容\n使用提示：可将其视为前面对话的小总结，知道发生了什么即可，不必强制引用其中表述。",
    )

    params_once: dict[str, Any] = {
        "name": "default_chatter_user_prompt",
        "template": "{extra}",
        "values": {
            "stream_id": "sid_once",
            "chat_type": "private",
            "extra": "keep",
        },
        "policies": {},
        "strict": False,
    }
    decision_once, out_once = asyncio.run(handler.execute("on_prompt_build", params_once))
    assert decision_once is EventDecision.SUCCESS
    assert out_once["values"]["extra"] == (
        "keep\n【自动日记摘要】测试内容\n"
        "使用提示：可将其视为前面对话的小总结，知道发生了什么即可，不必强制引用其中表述。"
    )

    # 第二次应不再重复注入
    params_twice: dict[str, Any] = {
        "name": "default_chatter_user_prompt",
        "template": "{extra}",
        "values": {
            "stream_id": "sid_once",
            "chat_type": "private",
            "extra": "keep",
        },
        "policies": {},
        "strict": False,
    }
    decision_twice, out_twice = asyncio.run(handler.execute("on_prompt_build", params_twice))
    assert decision_twice is EventDecision.SUCCESS
    assert out_twice["values"]["extra"] == "keep"


def test_auto_diary_threshold_starts_background_task_without_waiting() -> None:
    """自动日记达到阈值时应启动后台任务并立刻重置计数。"""

    config = DiaryConfig()
    config.auto_diary.enabled = True
    config.auto_diary.message_threshold = 2
    config.auto_diary.summary_timeout_seconds = 99

    handler = AutoDiaryEventHandler(plugin=SimpleNamespace(config=config))
    calls: list[tuple[str, int, int]] = []

    def fake_start(
        stream_id: str,
        *,
        summary_count: int,
        timeout_seconds: int,
    ) -> bool:
        calls.append((stream_id, summary_count, timeout_seconds))
        return True

    handler._start_auto_summary_task = fake_start  # type: ignore[method-assign]

    params = {"stream_id": "sid_auto", "chat_type": "private"}
    decision_one, _ = asyncio.run(handler.execute("on_chatter_step", params))
    decision_two, _ = asyncio.run(handler.execute("on_chatter_step", params))

    assert decision_one is EventDecision.SUCCESS
    assert decision_two is EventDecision.SUCCESS
    assert calls == [("sid_auto", 2, 99)]
    assert handler._message_counts["sid_auto"] == 0


def test_auto_diary_inflight_summary_prevents_retry_storm() -> None:
    """已有后台总结时不应重复启动，也不应让计数无限增长。"""

    config = DiaryConfig()
    config.auto_diary.enabled = True
    config.auto_diary.message_threshold = 2

    handler = AutoDiaryEventHandler(plugin=SimpleNamespace(config=config))
    handler._summary_streams.add("sid_auto")

    params = {"stream_id": "sid_auto", "chat_type": "private"}
    asyncio.run(handler.execute("on_chatter_step", params))
    asyncio.run(handler.execute("on_chatter_step", params))
    asyncio.run(handler.execute("on_chatter_step", params))

    assert handler._message_counts["sid_auto"] == 2
