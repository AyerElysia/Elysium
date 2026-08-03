
import pytest

from src.kernel.llm.exceptions import LLMAPIError, LLMModelsCoolingDownError
from src.kernel.llm.model_client import ModelClientRegistry, StreamEvent
from src.kernel.llm.payload import LLMPayload, Text
from src.kernel.llm.policy.failover import FailoverPolicy
from src.kernel.llm.request import LLMRequest
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
    req = LLMRequest(model_set, request_name="req", clients=ModelClientRegistry(openai=dummy))

    resp = await req.send(stream=False)
    assert resp.message == "ok"
    assert dummy.calls == ["a", "b"]


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

    model_set = [_model("a", max_retry=0), _model("b", max_retry=0)]

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
    req = LLMRequest(model_set, request_name="req", clients=ModelClientRegistry(openai=dummy))

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

    req = LLMRequest(model_set, request_name="req", clients=ModelClientRegistry(openai=DummyClient()))
    req.add_payload(LLMPayload(ROLE.USER, Text("hello")))

    resp = await req.send(stream=False)
    assert resp.message == "hello world"
    assert await resp == "hello world"


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
    req = LLMRequest(model_set, request_name="req", clients=ModelClientRegistry(openai=dummy))

    resp = await req.send(stream=False)
    assert resp.message == "ok"
    assert dummy.calls == ["a", "b"]
