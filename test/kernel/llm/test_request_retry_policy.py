import asyncio

import pytest

from src.kernel.llm import request as request_module
from src.kernel.llm.exceptions import LLMAPIError, LLMModelsCoolingDownError
from src.kernel.llm.model_client import ModelClientRegistry, StreamEvent
from src.kernel.llm.payload import LLMPayload, Text
from src.kernel.llm.policy.failover import FailoverPolicy
from src.kernel.llm.request import LLMRequest
from src.kernel.llm.response import LLMResponse
from src.kernel.llm.roles import ROLE


class DummyClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._fail_once_for: set[str] = {"a"}

    async def create(
        self,
        *,
        model_name: str,
        payloads,
        tools,
        request_name: str,
        model_set,
        stream: bool,
    ):
        self.calls.append(model_name)
        if model_name in self._fail_once_for:
            self._fail_once_for.remove(model_name)
            raise RuntimeError("boom")
        return "ok", [], None


def _model(
    identifier: str,
    *,
    max_retry: int,
    base_url: str = "https://api.openai.com/v1",
):
    return {
        "api_provider": "OpenAI",
        "base_url": base_url,
        "model_identifier": identifier,
        "api_key": "dummy-key",
        "client_type": "openai",
        "max_retry": max_retry,
        "timeout": 1,
        "retry_interval": 0,
        "price_in": 0.0,
        "price_out": 0.0,
        "temperature": 0.1,
        "max_tokens": 10,
        "max_context": 4096,
        "extra_params": {},
    }


async def test_retry_is_driven_by_policy_switch_or_retry():
    # a 会失败一次；max_retry=0 => policy 应立刻切换到 b
    model_set = [_model("a", max_retry=0), _model("b", max_retry=0)]

    dummy = DummyClient()
    req = LLMRequest(
        model_set, request_name="req", clients=ModelClientRegistry(openai=dummy)
    )

    resp = await req.send(stream=False)
    assert resp.message == "ok"
    assert dummy.calls == ["a", "b"]


async def test_empty_content_fails_over_to_next_model():
    """HTTP 成功但助手正文为空（上游截断流）必须触发模型切换。"""

    model_set = [_model("a", max_retry=0), _model("b", max_retry=0)]

    class EmptyThenOkClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create(
            self, *, model_name, payloads, tools, request_name, model_set, stream
        ):
            self.calls.append(model_name)
            if model_name == "a":
                # 上游截断的典型形态：200 + 空正文 + 无工具调用
                return "", [], None
            return "ok", [], None

    client = EmptyThenOkClient()
    req = LLMRequest(
        model_set, request_name="req", clients=ModelClientRegistry(openai=client)
    )

    resp = await req.send(stream=False)
    assert resp.message == "ok"
    assert client.calls == ["a", "b"]


async def test_tool_call_response_with_blank_content_is_not_treated_as_empty():
    """带工具调用的响应即使正文为空白也必须原样返回，不得误判失败。"""

    model_set = [_model("a", max_retry=0), _model("b", max_retry=0)]

    class ToolCallClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create(
            self, *, model_name, payloads, tools, request_name, model_set, stream
        ):
            self.calls.append(model_name)
            return "  ", [{"id": "t1", "name": "lookup", "args": "{}"}], None

    client = ToolCallClient()
    req = LLMRequest(
        model_set, request_name="req", clients=ModelClientRegistry(openai=client)
    )

    resp = await req.send(stream=False)
    assert client.calls == ["a"]
    assert resp.call_list and resp.call_list[0].name == "lookup"


async def test_transient_failure_cools_model_for_matching_future_requests():
    """Repeated heartbeat rounds should bypass a recently failed primary."""

    model_set = [_model("a", max_retry=0), _model("b", max_retry=0)]

    class CoolingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def create(
            self,
            *,
            model_name: str,
            payloads,
            tools,
            request_name: str,
            model_set,
            stream: bool,
        ):
            self.calls.append((request_name, model_name))
            if model_name == "a":
                raise LLMAPIError("upstream unavailable", status_code=500)
            return "ok", [], None

    client = CoolingClient()
    clients = ModelClientRegistry(openai=client)
    first = LLMRequest(
        model_set,
        request_name="life_engine_heartbeat",
        clients=clients,
        policy=FailoverPolicy(),
    )
    second = LLMRequest(
        model_set,
        request_name="life_engine_heartbeat",
        clients=clients,
        policy=FailoverPolicy(),
    )
    isolated = LLMRequest(
        model_set,
        request_name="life_memory_witness",
        clients=clients,
        policy=FailoverPolicy(),
    )

    assert (await first.send(stream=False)).message == "ok"
    assert (await second.send(stream=False)).message == "ok"
    assert (await isolated.send(stream=False)).message == "ok"

    assert client.calls == [
        ("life_engine_heartbeat", "a"),
        ("life_engine_heartbeat", "b"),
        ("life_engine_heartbeat", "b"),
        ("life_memory_witness", "a"),
        ("life_memory_witness", "b"),
    ]


async def test_cooldown_skip_logs_authoritative_route_context(monkeypatch):
    """A skipped primary must be explainable without exposing credentials."""

    model_set = [
        {
            **_model("a", max_retry=0),
            "routing_task": "expression",
            "routing_model_alias": "primary",
            "routing_priority": 0,
            "routing_snapshot": "snapshot-1234",
        },
        {
            **_model("b", max_retry=0),
            "routing_task": "expression",
            "routing_model_alias": "backup",
            "routing_priority": 1,
            "routing_snapshot": "snapshot-1234",
        },
    ]

    class CoolingClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create(
            self,
            *,
            model_name: str,
            payloads,
            tools,
            request_name: str,
            model_set,
            stream: bool,
        ):
            self.calls.append(model_name)
            if model_name == "a":
                raise LLMAPIError("upstream unavailable", status_code=500)
            return "ok", [], None

    info_logs: list[str] = []
    monkeypatch.setattr(
        "src.kernel.llm.request.logger.info",
        lambda message: info_logs.append(str(message)),
    )
    client = CoolingClient()
    clients = ModelClientRegistry(openai=client)
    first = LLMRequest(
        model_set,
        request_name="life_chatter",
        clients=clients,
        policy=FailoverPolicy(),
    )
    second = LLMRequest(
        model_set,
        request_name="life_chatter",
        clients=clients,
        policy=FailoverPolicy(),
    )

    assert (await first.send(stream=False)).message == "ok"
    assert (await second.send(stream=False)).message == "ok"
    assert client.calls == ["a", "b", "b"]
    assert any(
        "configured_primary=a" in message
        and "selected=b" in message
        and "configured_priority=1" in message
        and "snapshot=snapshot-1234" in message
        and "skipped=['a']" in message
        for message in info_logs
    )


async def test_gateway_resource_overload_skips_models_on_same_endpoint():
    """A gateway-wide guard cannot be repaired by changing model aliases."""

    model_set = [
        _model("a", max_retry=0, base_url="http://127.0.0.1:3000/v1"),
        _model("b", max_retry=0, base_url="http://127.0.0.1:3000/v1/"),
        _model("c", max_retry=0, base_url="https://backup.example/v1"),
    ]

    class GatewayAwareClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create(
            self,
            *,
            model_name: str,
            payloads,
            tools,
            request_name: str,
            model_set,
            stream: bool,
        ):
            self.calls.append(model_name)
            if model_name == "a":
                raise LLMAPIError(
                    "local gateway overloaded",
                    status_code=503,
                    error_code="system_cpu_overloaded",
                )
            return "ok", [], None

    client = GatewayAwareClient()
    request = LLMRequest(
        model_set,
        request_name="life_memory_witness",
        clients=ModelClientRegistry(openai=client),
        policy=FailoverPolicy(),
    )

    assert (await request.send(stream=False)).message == "ok"
    assert client.calls == ["a", "c"]


async def test_gateway_resource_overload_cools_endpoint_across_request_names():
    """New requests must not hammer another alias on the same local gateway."""

    model_set = [
        _model("a", max_retry=0, base_url="http://127.0.0.1:3000/v1"),
        _model("b", max_retry=0, base_url="http://127.0.0.1:3000/v1"),
    ]

    class OverloadedClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create(
            self,
            *,
            model_name: str,
            payloads,
            tools,
            request_name: str,
            model_set,
            stream: bool,
        ):
            self.calls.append(model_name)
            raise LLMAPIError(
                "local gateway overloaded",
                status_code=503,
                error_code="system_cpu_overloaded",
            )

    client = OverloadedClient()
    clients = ModelClientRegistry(openai=client)
    first = LLMRequest(
        model_set,
        request_name="life_memory_witness",
        clients=clients,
        policy=FailoverPolicy(),
    )

    with pytest.raises(LLMAPIError) as first_error:
        await first.send(stream=False)

    assert first_error.value.error_code == "system_cpu_overloaded"
    assert 29 < first_error.value.retry_after <= 30
    assert client.calls == ["a"]

    second = LLMRequest(
        model_set,
        request_name="router",
        clients=clients,
        policy=FailoverPolicy(),
    )
    with pytest.raises(LLMModelsCoolingDownError) as second_error:
        await second.send(stream=False)

    assert 29 < second_error.value.retry_after <= 30
    assert client.calls == ["a"]


async def test_permanent_failure_does_not_enter_transient_cooldown():
    """Configuration and authentication faults must remain visible every time."""

    model_set = [_model("a", max_retry=0), _model("b", max_retry=0)]

    class PermanentFailureClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create(
            self,
            *,
            model_name: str,
            payloads,
            tools,
            request_name: str,
            model_set,
            stream: bool,
        ):
            self.calls.append(model_name)
            if model_name == "a":
                raise LLMAPIError("invalid request", status_code=400)
            return "ok", [], None

    client = PermanentFailureClient()
    clients = ModelClientRegistry(openai=client)
    for _ in range(2):
        request = LLMRequest(
            model_set,
            request_name="life_engine_heartbeat",
            clients=clients,
            policy=FailoverPolicy(),
        )
        assert (await request.send(stream=False)).message == "ok"

    assert client.calls == ["a", "b", "a", "b"]


async def test_all_cooling_models_are_not_probed_before_expiry():
    """A fully failed chain must not be hammered by each new request."""

    model_set = [
        {
            **_model("a", max_retry=0),
            "routing_task": "witness",
            "routing_snapshot": "snapshot-cooling",
        },
        {
            **_model("b", max_retry=0),
            "routing_task": "witness",
            "routing_snapshot": "snapshot-cooling",
        },
    ]

    class FailingClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create(
            self,
            *,
            model_name: str,
            payloads,
            tools,
            request_name: str,
            model_set,
            stream: bool,
        ):
            self.calls.append(model_name)
            raise LLMAPIError("upstream unavailable", status_code=500)

    client = FailingClient()
    clients = ModelClientRegistry(openai=client)
    first = LLMRequest(
        model_set,
        request_name="life_memory_witness",
        clients=clients,
        policy=FailoverPolicy(),
    )
    with pytest.raises(LLMAPIError):
        await first.send(stream=False)
    assert client.calls == ["a", "b"]

    second = LLMRequest(
        model_set,
        request_name="life_memory_witness",
        clients=clients,
        policy=FailoverPolicy(),
    )
    with pytest.raises(LLMModelsCoolingDownError) as exc_info:
        await second.send(stream=False)

    assert 29 < exc_info.value.retry_after <= 30
    assert exc_info.value.routing_task == "witness"
    assert exc_info.value.routing_snapshot == "snapshot-cooling"
    assert "snapshot=snapshot-cooling" in str(exc_info.value)
    assert client.calls == ["a", "b"]


def test_transient_cooldown_expiry_restores_primary(monkeypatch):
    """The preferred model is probed again after its cooldown expires."""

    now = [100.0]
    monkeypatch.setattr(
        "src.kernel.llm.policy.failover.time.monotonic",
        lambda: now[0],
    )
    model_set = [_model("a", max_retry=0), _model("b", max_retry=0)]
    policy = FailoverPolicy(cooldown_seconds=10)

    failed = policy.new_session(
        model_set=model_set,
        request_name="life_engine_heartbeat",
    )
    assert failed.first().model["model_identifier"] == "a"
    assert (
        failed.next_after_error(
            LLMAPIError("upstream unavailable", status_code=500)
        ).model["model_identifier"]
        == "b"
    )

    cooling = policy.new_session(
        model_set=model_set,
        request_name="life_engine_heartbeat",
    )
    assert cooling.first().model["model_identifier"] == "b"

    now[0] = 110.0
    recovered = policy.new_session(
        model_set=model_set,
        request_name="life_engine_heartbeat",
    )
    assert recovered.first().model["model_identifier"] == "a"


async def test_forced_stream_collection_error_is_retried_inside_request():
    model_set = [
        {**_model("a", max_retry=0), "force_stream_mode": True},
        _model("b", max_retry=0),
    ]

    async def broken_stream():
        yield StreamEvent(text_delta="partial")
        raise TimeoutError("stream stalled")

    class DummyClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        async def create(
            self,
            *,
            model_name: str,
            payloads,
            tools,
            request_name: str,
            model_set,
            stream: bool,
        ):
            self.calls.append((model_name, stream))
            if model_name == "a":
                return None, None, broken_stream()
            return "ok", [], None

    dummy = DummyClient()
    req = LLMRequest(
        model_set, request_name="req", clients=ModelClientRegistry(openai=dummy)
    )

    resp = await req.send(stream=False)

    assert resp.message == "ok"
    assert dummy.calls == [("a", True), ("b", False)]


async def test_forced_stream_precollection_keeps_response_awaitable():
    model_set = [{**_model("a", max_retry=0), "force_stream_mode": True}]

    async def stream_ok():
        yield StreamEvent(text_delta="hello")
        yield StreamEvent(text_delta=" world")

    class DummyClient:
        async def create(
            self,
            *,
            model_name: str,
            payloads,
            tools,
            request_name: str,
            model_set,
            stream: bool,
        ):
            assert stream is True
            return None, None, stream_ok()

    req = LLMRequest(
        model_set, request_name="req", clients=ModelClientRegistry(openai=DummyClient())
    )
    req.add_payload(LLMPayload(ROLE.USER, Text("hello")))

    resp = await req.send(stream=False)
    assert resp.message == "hello world"
    assert await resp == "hello world"


def test_attempt_deadline_reports_only_monotonic_remaining_budget(monkeypatch):
    moments = iter((100.0, 103.5, 109.0, 110.0))
    monkeypatch.setattr(request_module, "_monotonic", lambda: next(moments))

    deadline = request_module._new_attempt_deadline(10.0)

    assert deadline == 110.0
    assert request_module._remaining_attempt_timeout(deadline) == 6.5
    assert request_module._remaining_attempt_timeout(deadline) == 1.0
    with pytest.raises(asyncio.TimeoutError):
        request_module._remaining_attempt_timeout(deadline)


async def test_forced_stream_precollection_reuses_create_attempt_deadline(
    monkeypatch,
):
    model_set = [
        {
            **_model("shared-deadline", max_retry=0),
            "force_stream_mode": True,
            "timeout": 10.0,
        }
    ]
    moments = iter((100.0, 102.0, 106.0))
    observed_timeouts: list[float] = []
    real_remaining = request_module._remaining_attempt_timeout
    monkeypatch.setattr(request_module, "_monotonic", lambda: next(moments))

    def observing_remaining(deadline):
        remaining = real_remaining(deadline)
        assert remaining is not None
        observed_timeouts.append(remaining)
        return remaining

    monkeypatch.setattr(
        request_module,
        "_remaining_attempt_timeout",
        observing_remaining,
    )

    async def stream_ok():
        yield StreamEvent(text_delta="ok")

    class DummyClient:
        async def create(self, **kwargs):
            assert kwargs["stream"] is True
            return None, None, stream_ok()

    req = LLMRequest(
        model_set,
        request_name="shared_attempt_deadline",
        clients=ModelClientRegistry(openai=DummyClient()),
    )

    await req.send(stream=False)

    assert observed_timeouts == [8.0, 4.0]


async def test_expired_attempt_does_not_start_forced_stream_precollection(
    monkeypatch,
):
    model_set = [
        {
            **_model("expired-before-precollect", max_retry=0),
            "force_stream_mode": True,
            "timeout": 10.0,
        }
    ]
    moments = iter((100.0, 100.0, 110.0))
    precollect_calls = 0
    monkeypatch.setattr(request_module, "_monotonic", lambda: next(moments))

    async def must_not_precollect(self):
        nonlocal precollect_calls
        precollect_calls += 1

    monkeypatch.setattr(
        LLMResponse,
        "precollect_stream_for_non_stream",
        must_not_precollect,
    )

    async def stream_ok():
        yield StreamEvent(text_delta="too late")

    class DummyClient:
        async def create(self, **kwargs):
            return None, None, stream_ok()

    req = LLMRequest(
        model_set,
        request_name="expired_before_precollect",
        clients=ModelClientRegistry(openai=DummyClient()),
    )

    with pytest.raises(asyncio.TimeoutError):
        await req.send(stream=False)
    assert precollect_calls == 0


async def test_attempt_deadline_cancels_stalled_create():
    model_set = [
        {**_model("create-timeout", max_retry=0), "timeout": 0.02}
    ]
    create_cancelled = asyncio.Event()

    class DummyClient:
        async def create(self, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                create_cancelled.set()

    req = LLMRequest(
        model_set,
        request_name="create_attempt_timeout",
        clients=ModelClientRegistry(openai=DummyClient()),
    )

    with pytest.raises(asyncio.TimeoutError):
        await req.send(stream=False)
    assert create_cancelled.is_set()


async def test_forced_stream_timeout_cancels_stream_and_preserves_failover_identity(
    monkeypatch,
):
    model_set = [
        {
            **_model("precollect-timeout", max_retry=0),
            "force_stream_mode": True,
            "timeout": 0.03,
        },
        {**_model("fallback", max_retry=0), "timeout": 1.0},
    ]
    stream_started = asyncio.Event()
    stream_cancelled = asyncio.Event()
    captured: list[dict] = []
    monkeypatch.setattr(
        request_module,
        "_trajectory_settings",
        lambda: (True, "unused", 0.0, 1, 1, 0),
    )
    monkeypatch.setattr(
        request_module,
        "record_trajectory",
        lambda event, **_kwargs: captured.append(dict(event)),
    )

    async def stalled_stream():
        try:
            stream_started.set()
            await asyncio.Event().wait()
            yield StreamEvent(text_delta="unreachable")
        finally:
            stream_cancelled.set()

    class DummyClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        async def create(self, **kwargs):
            self.calls.append((kwargs["model_name"], kwargs["stream"]))
            if kwargs["model_name"] == "precollect-timeout":
                return None, None, stalled_stream()
            return "fallback ok", [], None

    client = DummyClient()
    req = LLMRequest(
        model_set,
        request_name="precollect_attempt_timeout",
        clients=ModelClientRegistry(openai=client),
        trace_id="trace-shared",
    )

    response = await req.send(stream=False)

    assert response.message == "fallback ok"
    assert stream_started.is_set()
    assert stream_cancelled.is_set()
    assert client.calls == [
        ("precollect-timeout", True),
        ("fallback", False),
    ]
    assert len(captured) == 2
    assert [event["success"] for event in captured] == [False, True]
    assert {event["request_id"] for event in captured} == {captured[0]["request_id"]}
    assert {event["trace_id"] for event in captured} == {"trace-shared"}
    assert captured[0]["attempt_id"] != captured[1]["attempt_id"]
    assert captured[1]["parent_attempt_id"] == captured[0]["attempt_id"]


async def test_external_cancel_during_precollection_does_not_fail_over():
    model_set = [
        {
            **_model("cancel-precollect", max_retry=0),
            "force_stream_mode": True,
            "timeout": 1.0,
        },
        _model("must-not-run", max_retry=0),
    ]
    stream_started = asyncio.Event()
    stream_cancelled = asyncio.Event()

    async def stalled_stream():
        try:
            stream_started.set()
            await asyncio.Event().wait()
            yield StreamEvent(text_delta="unreachable")
        finally:
            stream_cancelled.set()

    class DummyClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create(self, **kwargs):
            self.calls.append(kwargs["model_name"])
            return None, None, stalled_stream()

    client = DummyClient()
    req = LLMRequest(
        model_set,
        request_name="cancel_during_precollect",
        clients=ModelClientRegistry(openai=client),
    )
    pending = asyncio.create_task(req.send(stream=False))
    await asyncio.wait_for(stream_started.wait(), timeout=1.0)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert stream_cancelled.is_set()
    assert client.calls == ["cancel-precollect"]


async def test_caller_stream_consumption_is_not_capped_by_attempt_deadline():
    model_set = [
        {
            **_model("caller-stream", max_retry=0),
            "force_stream_mode": True,
            "timeout": 0.1,
        }
    ]
    stream_started = asyncio.Event()

    async def delayed_stream():
        stream_started.set()
        await asyncio.sleep(0.15)
        yield StreamEvent(text_delta="later")

    class DummyClient:
        async def create(self, **kwargs):
            assert kwargs["stream"] is True
            return None, None, delayed_stream()

    req = LLMRequest(
        model_set,
        request_name="caller_owned_stream",
        clients=ModelClientRegistry(openai=DummyClient()),
    )
    req.add_payload(LLMPayload(ROLE.USER, Text("hello")))

    response = await req.send(stream=True)

    assert not stream_started.is_set()
    assert await response == "later"
    assert stream_started.is_set()


async def test_retry_skips_permanent_404_and_switches_model():
    model_set = [_model("a", max_retry=3), _model("b", max_retry=0)]

    class DummyClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create(
            self,
            *,
            model_name: str,
            payloads,
            tools,
            request_name: str,
            model_set,
            stream: bool,
        ):
            self.calls.append(model_name)
            if model_name == "a":
                raise LLMAPIError("Not found", status_code=404)
            return "ok", [], None

    dummy = DummyClient()
    req = LLMRequest(
        model_set, request_name="req", clients=ModelClientRegistry(openai=dummy)
    )

    resp = await req.send(stream=False)
    assert resp.message == "ok"
    assert dummy.calls == ["a", "b"]
