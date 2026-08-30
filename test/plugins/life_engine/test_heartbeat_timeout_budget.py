"""心跳超时预算与续问重试的回归测试。

背景：外层 ``asyncio.wait_for`` 超时值若等于 provider 的单模型超时，两个定时器
同时到点、外层取消先赢，抛出的 ``CancelledError`` 在 request.py 里是裸 raise，
不进 failover——于是 life 任务里 6 个候补模型一个都轮不到。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.core import (
    HEARTBEAT_TOTAL_BUDGET_MAX_SECONDS,
    HeartbeatBudgetExhausted,
    LifeEngineService,
    _await_with_heartbeat_deadline,
    _heartbeat_tool_round_progress,
    _resolve_heartbeat_timeout,
    _resolve_heartbeat_total_budget,
    _sleep_with_heartbeat_deadline,
)
from src.kernel.llm import LLMPayload, ToolResult


def _model_set(*timeouts: float) -> list[dict[str, object]]:
    return [{"model_identifier": f"m{i}", "timeout": t} for i, t in enumerate(timeouts)]


def test_outer_budget_exceeds_single_attempt_timeout() -> None:
    """核心回归：外层预算必须严格大于单模型超时，否则 failover 永远轮不到。"""
    per_attempt = 120.0
    budget = _resolve_heartbeat_timeout(120.0, _model_set(*([per_attempt] * 6)))

    assert budget > per_attempt
    # 至少要容得下两次完整尝试，才谈得上"换一个模型"
    assert budget >= per_attempt * 2


def test_live_config_shape_no_longer_collides() -> None:
    """复现线上配置：heartbeat_timeout=120 且 provider timeout=120。"""
    budget = _resolve_heartbeat_timeout(120.0, _model_set(120.0, 120.0, 120.0))

    assert budget == pytest.approx(255.0)


def test_uses_slowest_model_in_set() -> None:
    """预算按集合里最慢的模型算，不能被快模型拉低。"""
    budget = _resolve_heartbeat_timeout(60.0, _model_set(30.0, 300.0, 45.0))

    assert budget == pytest.approx(615.0)


def test_configured_value_wins_when_larger() -> None:
    """配置值已经足够大时不再上抬。"""
    budget = _resolve_heartbeat_timeout(500.0, _model_set(60.0))

    assert budget == pytest.approx(500.0)


@pytest.mark.parametrize(
    "model_set",
    [
        None,
        [],
        [{"model_identifier": "m0"}],
        [{"timeout": 0}],
        [{"timeout": -5}],
        [{"timeout": "not-a-number"}],
        ["garbage"],
    ],
)
def test_degrades_to_configured_value_on_unusable_model_set(model_set: object) -> None:
    """模型集读不出超时时退回配置值，绝不抛异常打断心跳。"""
    assert _resolve_heartbeat_timeout(120.0, model_set) == pytest.approx(120.0)


def test_clamped_to_sane_bounds() -> None:
    assert _resolve_heartbeat_timeout(0.0, None) == pytest.approx(10.0)
    assert _resolve_heartbeat_timeout(99999.0, None) == pytest.approx(900.0)
    assert _resolve_heartbeat_timeout(120.0, _model_set(5000.0)) == pytest.approx(900.0)


def test_total_budget_is_shared_and_never_exceeds_five_minutes() -> None:
    assert _resolve_heartbeat_total_budget(120.0) == pytest.approx(300.0)
    assert _resolve_heartbeat_total_budget(99999.0) == pytest.approx(
        HEARTBEAT_TOTAL_BUDGET_MAX_SECONDS
    )


async def test_deadline_aware_steps_cannot_replenish_the_total_budget() -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 0.15

    async def _first_step() -> str:
        await asyncio.sleep(0.02)
        return "ok"

    assert (
        await _await_with_heartbeat_deadline(
            _first_step,
            deadline=deadline,
            stage="first",
            reserve_seconds=0.0,
        )
        == "ok"
    )
    with pytest.raises(HeartbeatBudgetExhausted, match="second"):
        await _await_with_heartbeat_deadline(
            lambda: asyncio.sleep(0.20),
            deadline=deadline,
            stage="second",
            reserve_seconds=0.0,
        )


async def test_cooldown_that_does_not_fit_fails_before_sleeping() -> None:
    deadline = asyncio.get_running_loop().time() + 0.02
    with pytest.raises(HeartbeatBudgetExhausted, match="cooldown"):
        await _sleep_with_heartbeat_deadline(
            1.0,
            deadline=deadline,
            stage="cooldown",
        )


def test_tool_progress_fingerprint_is_content_free_and_protocol_aware() -> None:
    call = SimpleNamespace(
        id="call-1",
        name="nucleus_read_file",
        args={"path": "private.md", "continuation": "secret-cursor"},
    )
    progress = _heartbeat_tool_round_progress(
        [call],
        [
            ToolResult(
                value="执行失败: invalid bounded-result continuation",
                call_id="call-1",
                name="nucleus_read_file",
            )
        ],
    )

    assert progress.has_success is False
    assert progress.has_protocol_failure is True
    assert "private.md" not in progress.fingerprint
    assert "secret-cursor" not in progress.fingerprint


class _FakeHeartbeatResponse:
    def __init__(
        self,
        *,
        text: str,
        calls: list[Any],
        next_response: _FakeHeartbeatResponse | None = None,
        request_record_id: str,
        send_timeouts_before_success: int = 0,
    ) -> None:
        self.text = text
        self.call_list = calls
        self.next_response = next_response
        self.request_record_id = request_record_id
        self.send_timeouts_before_success = send_timeouts_before_success
        self.payloads: list[LLMPayload] = []
        self.send_calls = 0

    def __await__(self):
        async def _done() -> str:
            return self.text

        return _done().__await__()

    def add_payload(self, payload: LLMPayload) -> None:
        self.payloads.append(payload)

    def register_context_delivery(self, *_args: object, **_kwargs: object) -> None:
        return None

    def effective_context_receipt(self, _delivery_id: str) -> object:
        return SimpleNamespace(
            exact_present=True,
            expected_utf8_bytes=9,
            effective_utf8_bytes=9,
            expected_sha256="a" * 64,
            effective_sha256="a" * 64,
        )

    async def send(self, *, stream: bool = False) -> _FakeHeartbeatResponse:
        assert stream is False
        self.send_calls += 1
        if self.send_timeouts_before_success > 0:
            self.send_timeouts_before_success -= 1
            raise TimeoutError
        assert self.next_response is not None
        return self.next_response


class _FakeHeartbeatRequest:
    def __init__(self, response: _FakeHeartbeatResponse) -> None:
        self.response = response
        self.payloads: list[LLMPayload] = []
        self.send_calls = 0

    def add_payload(self, payload: LLMPayload) -> None:
        self.payloads.append(payload)

    def register_context_delivery(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def send(self, *, stream: bool = False) -> _FakeHeartbeatResponse:
        assert stream is False
        self.send_calls += 1
        return self.response


async def test_followup_retry_then_repeated_protocol_failure_stops_third_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    config.settings.max_rounds_per_heartbeat = 5
    config.settings.max_consecutive_tool_stalls_per_heartbeat = 2
    service = LifeEngineService(SimpleNamespace(config=config))

    class FailingReadTool:
        @staticmethod
        def to_schema() -> dict[str, object]:
            return {
                "type": "function",
                "function": {"name": "nucleus_read_file", "parameters": {}},
            }

        def __init__(self, plugin: object) -> None:
            self.plugin = plugin

        async def execute(self, **_kwargs: object) -> tuple[bool, str]:
            return False, "invalid bounded-result continuation"

    first_call = SimpleNamespace(
        id="call-1",
        name="nucleus_read_file",
        args={"path": "notes/a.md", "continuation": "bad"},
    )
    second_call = SimpleNamespace(
        id="call-2",
        name="nucleus_read_file",
        args={"path": "notes/a.md", "continuation": "bad"},
    )
    second = _FakeHeartbeatResponse(
        text="我已经完成了，但这句话和失败工具调用同轮，不能当成最终回执。",
        calls=[second_call],
        request_record_id="response-2",
    )
    first = _FakeHeartbeatResponse(
        text="",
        calls=[first_call],
        next_response=second,
        request_record_id="response-1",
        send_timeouts_before_success=1,
    )
    request = _FakeHeartbeatRequest(first)
    audit_rows: list[dict[str, object]] = []

    async def _empty_text() -> str:
        return ""

    async def _system_prompt() -> str:
        return "system"

    async def _sections() -> dict[str, str]:
        return {}

    async def _no_maintenance() -> None:
        return None

    async def _record(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "plugins.life_engine.service.core.create_llm_request",
        lambda **_kwargs: request,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.core.get_model_set_by_task",
        lambda _task: _model_set(1.0),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.core.log_heartbeat_event",
        lambda **fields: audit_rows.append(fields),
    )
    monkeypatch.setattr(service, "_build_heartbeat_system_prompt", _system_prompt)
    monkeypatch.setattr(service, "_build_memory_maintenance_prompt_if_due", _empty_text)
    monkeypatch.setattr(service, "_render_heartbeat_sections", _sections)
    monkeypatch.setattr(service, "_run_learning_heartbeat_maintenance", _no_maintenance)
    monkeypatch.setattr(service, "_get_nucleus_tools", lambda: [FailingReadTool])
    monkeypatch.setattr(service, "record_tool_call", _record)
    monkeypatch.setattr(service, "record_tool_result", _record)

    world = SimpleNamespace(
        delivery_id="world-delivery",
        delivery_marker="world-marker",
        projection_sha256="a" * 64,
        delivered_bytes=9,
    )
    result = await service._run_heartbeat_model(
        "wake",
        heartbeat_run_id="heartbeat-test",
        world_perception=world,
        heartbeat_deadline=asyncio.get_running_loop().time() + 10.0,
    )

    assert request.send_calls == 1
    assert first.send_calls == 2
    assert second.send_calls == 0
    assert result.perception_receipt is not None
    assert result.perception_receipt.transport_request_id == "response-2"
    assert result.text == ""
    assert any(row.get("stop_reason") == "consecutive_tool_stalls" for row in audit_rows)
