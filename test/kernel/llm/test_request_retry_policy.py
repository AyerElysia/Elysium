import pytest

from src.kernel.llm.exceptions import LLMAPIError
from src.kernel.llm.model_client import StreamEvent
from src.kernel.llm.model_client import ModelClientRegistry
from src.kernel.llm.payload import LLMPayload, Text
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


def _model(identifier: str, *, max_retry: int):
    return {
        "api_provider": "OpenAI",
        "base_url": "https://api.openai.com/v1",
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
