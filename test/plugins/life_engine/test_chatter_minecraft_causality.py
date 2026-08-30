"""Causal delivery contracts for Minecraft calls made by life_chatter."""

from __future__ import annotations

from types import SimpleNamespace

from plugins.life_engine.core.chatter import (
    LifeChatter,
    _Phase,
    _WorkflowRuntime,
)
from src.app.plugin_system.base import Success
from src.kernel.llm import ROLE, LLMPayload, ToolResult


async def _skip_snapshot_save(*_args, **_kwargs) -> None:
    return None


async def test_minecraft_receipt_precedes_same_turn_visible_promise(
    monkeypatch,
) -> None:
    """A same-turn promise must wait until the model has read the MC receipt."""

    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads: list[LLMPayload] = []
            self.call_list = [
                SimpleNamespace(
                    id="mc-status-1",
                    name="tool-nucleus_minecraft",
                    args={"action": "status", "reason": "准备加入共享世界"},
                ),
                SimpleNamespace(
                    id="send-promise-1",
                    name="action-life_send_text",
                    args={"content": "我已经启动身体，马上到。"},
                ),
            ]
            self.message = ""

        def add_payload(self, payload: LLMPayload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    runtime = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-minecraft",
        must_reply=True,
    )
    LifeChatter._GLOBAL_RUNTIME = runtime
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-minecraft"
    executed: list[str] = []

    async def fake_fetch_unreads():
        return [], []

    async def fake_run_tool_call(call, llm_response, *_args, **_kwargs):
        calls = call if isinstance(call, list) else [call]
        results = []
        for item in calls:
            executed.append(str(item.name))
            llm_response.add_payload(
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(
                        value='{"active": false, "readiness": "idle"}',
                        call_id=item.id,
                        name=item.name,
                    ),
                )
            )
            results.append((True, True))
        return results if isinstance(call, list) else results[0]

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "run_tool_call", fake_run_tool_call)
    monkeypatch.setattr(
        chatter,
        "_maybe_compact_runtime_context",
        lambda _response: None,
    )
    monkeypatch.setattr(
        chatter,
        "_save_rolling_context_snapshot",
        _skip_snapshot_save,
    )
    monkeypatch.setattr(
        "src.kernel.concurrency.get_watchdog",
        lambda: SimpleNamespace(feed_dog=lambda _stream_id: None),
    )

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-minecraft"),
        service=None,
    )

    tool_results = [
        part
        for payload in response.payloads
        for part in payload.content
        if isinstance(part, ToolResult)
    ]
    deferred = next(
        item for item in tool_results if item.call_id == "send-promise-1"
    )

    assert isinstance(result, Success)
    assert executed == ["tool-nucleus_minecraft"]
    assert "未发送" in str(deferred.value)
    assert "Minecraft" in str(deferred.value)
    assert runtime.phase == _Phase.FOLLOW_UP
    assert runtime.sent_visible_reply is False
    assert runtime.must_reply is True
    assert runtime.follow_up_rounds == 1

    LifeChatter.reset_global_runtime()
