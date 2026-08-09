from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.event_handler import (
    VOICE_LIVE_COMMAND_EVENT,
    VoiceLiveEventHandler,
)
from plugins.voice_live.protocol import SessionState
from plugins.voice_live.router import VoiceLiveRouter
from plugins.voice_live.runtime_store import VoiceEpisodeStore
from plugins.voice_live.tool_broker import VoiceToolBroker
from src.core.components.types import ComponentType
from src.kernel.event import EventDecision


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, name: str, payload: dict[str, Any]) -> None:
        self.events.append((name, payload))


@pytest.mark.asyncio
async def test_router_prewarms_subject_projection_without_persisting_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = VoiceLiveConfig()
    calls: list[dict[str, Any]] = []

    class Service:
        async def get_subject_context_projection_snapshot(
            self,
            **kwargs: Any,
        ) -> dict[str, Any]:
            calls.append(kwargs)
            return {
                "text": "must-not-enter-router-health",
                "revision": "revision-1",
                "source_digest": "a" * 64,
                "projection_sha256": "b" * 64,
                "budget": {"delivered_bytes": 1024},
            }

    monkeypatch.setattr(
        "plugins.voice_live.router.get_running_life_service",
        lambda: Service(),
    )
    router = VoiceLiveRouter(SimpleNamespace(config=config))

    await router._prewarm_subject_context()

    assert calls == [
        {
            "projection_kind": "voice_live",
            "max_bytes": config.session.subject_context_max_bytes,
        }
    ]
    status = router._readiness_snapshot()["subject_context_prewarm"]
    assert status["status"] == "ready"
    assert status["revision"] == "revision-1"
    assert "text" not in status
    assert "must-not-enter-router-health" not in str(status)


@pytest.mark.asyncio
async def test_event_handler_commands_use_public_router_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = VoiceLiveEventHandler(SimpleNamespace())
    bus = FakeBus()
    router = SimpleNamespace(active_session_count=2, reasons=[])

    async def stop_all(*, reason: str) -> None:
        router.reasons.append(reason)

    router.stop_all = stop_all
    monkeypatch.setattr("src.kernel.event.get_event_bus", lambda: bus)
    monkeypatch.setattr(handler, "_get_router", lambda: router)

    decision, _ = await handler.execute(
        VOICE_LIVE_COMMAND_EVENT, {"command": "voice_live_start", "user_id": "u"}
    )
    assert decision is EventDecision.SUCCESS
    decision, _ = await handler.execute(
        VOICE_LIVE_COMMAND_EVENT, {"action": "voice_live_status"}
    )
    assert decision is EventDecision.SUCCESS
    decision, _ = await handler.execute(
        VOICE_LIVE_COMMAND_EVENT, {"command": "voice_live_stop"}
    )
    assert decision is EventDecision.SUCCESS
    assert router.reasons == ["event_command"]
    assert [name for name, _ in bus.events] == [
        "voice_live_started",
        "voice_live_stopped",
    ]
    decision, _ = await handler.execute("unrelated", {})
    assert decision is EventDecision.PASS


class FakeComponent:
    def __init__(self, name: str, properties: dict[str, Any] | None = None) -> None:
        self.name = name
        self.properties = properties or {}

    def to_schema(self) -> dict[str, Any]:
        return {
            "function": {
                "name": self.name,
                "description": f"execute {self.name}",
                "parameters": {"type": "object", "properties": self.properties},
            }
        }


class FakeRegistry:
    def get_by_type(self, component_type: ComponentType) -> dict[str, Any]:
        if component_type is ComponentType.ACTION:
            return {
                "demo:action:act": FakeComponent(
                    "act", {"scene_id": {"type": "string"}}
                )
            }
        if component_type is ComponentType.TOOL:
            return {"demo:tool:look": FakeComponent("look")}
        return {}


@pytest.mark.asyncio
async def test_tool_broker_manifest_schema_and_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = VoiceLiveConfig()
    store = VoiceEpisodeStore(tmp_path, "voice_live_tools", "tools")
    consciousness = SimpleNamespace(instance_id="voice_live_tools", stream_id="voice_live_tools")
    broker = VoiceToolBroker(consciousness, config, store)
    monkeypatch.setattr("plugins.voice_live.tool_broker.get_global_registry", FakeRegistry)
    monkeypatch.setattr(
        "plugins.voice_live.tool_broker.get_tool_manifest", lambda _: ("act", "look")
    )
    schemas = broker.schemas()
    assert {schema["name"] for schema in schemas} == {"act", "look"}

    monkeypatch.setattr(
        "src.core.managers.get_plugin_manager",
        lambda: SimpleNamespace(get_plugin=lambda _: object()),
    )

    class ActionManager:
        async def execute_action(self, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
            return True, {"kind": "action", "args": kwargs}

    class ToolUse:
        async def execute_tool(self, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
            return True, {"kind": "tool", "args": kwargs}

    monkeypatch.setattr(
        "src.core.managers.action_manager.get_action_manager", lambda: ActionManager()
    )
    monkeypatch.setattr("src.core.managers.tool_manager.get_tool_use", lambda: ToolUse())
    action = await broker.execute("act", '{"value":1,"scene_id":"spoofed"}')
    tool = await broker.execute("look", "{}")
    assert action["success"] and action["result"]["kind"] == "action"
    assert action["result"]["args"]["scene_id"] == "voice_live_tools"
    assert tool["success"] and tool["result"]["kind"] == "tool"
    with pytest.raises(ValueError, match="manifest"):
        await broker.execute("hidden", "{}")


class FakeCallSession:
    created: list["FakeCallSession"] = []

    def __init__(self, config: VoiceLiveConfig) -> None:
        del config
        self.session_id = f"fake-{len(self.created)}"
        self.state = SessionState.CREATED
        self.send_json: Any = None
        self.send_bytes: Any = None
        self.stop_reasons: list[str] = []
        self.created.append(self)

    @property
    def is_active(self) -> bool:
        return self.state is SessionState.ACTIVE

    def snapshot(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "state": self.state.value}

    def set_send_callbacks(self, send_json: Any, send_bytes: Any) -> None:
        self.send_json = send_json
        self.send_bytes = send_bytes

    async def handle_message(self, message: dict[str, Any]) -> None:
        if message["type"] == "start":
            self.state = SessionState.ACTIVE
            await self.send_json({"type": "ready", "session_id": self.session_id})
        elif message["type"] == "stop":
            self.state = SessionState.ENDED

    async def handle_audio(self, data: bytes) -> None:
        await self.send_bytes(data)

    async def stop(self, *, reason: str) -> None:
        self.stop_reasons.append(reason)
        self.state = SessionState.ENDED


def test_router_websocket_and_observer_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeCallSession.created.clear()
    config = VoiceLiveConfig()
    router = VoiceLiveRouter(SimpleNamespace(config=config))
    monkeypatch.setattr("plugins.voice_live.router.CallSession", FakeCallSession)
    client = TestClient(router.app)
    headers = {"origin": "http://testserver", "host": "testserver"}

    ticket = client.post("/ticket", headers=headers).json()["ticket"]
    with client.websocket_connect(f"/ws?ticket={ticket}", headers=headers) as websocket:
        websocket.send_json({"type": "start"})
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_bytes(b"audio")
        assert websocket.receive_bytes() == b"audio"
        websocket.send_json({"type": "stop"})
    assert FakeCallSession.created[-1].stop_reasons == ["disconnect"]

    observer_ticket = client.post("/ticket", headers=headers).json()["ticket"]
    with client.websocket_connect(
        f"/observe?ticket={observer_ticket}", headers=headers
    ) as observer:
        assert observer.receive_json()["type"] == "observer.ready"
        observer.send_json({"type": "ping"})
        assert observer.receive_json()["type"] == "pong"
