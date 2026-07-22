from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from plugins.neko_surface.protocol import SurfaceEvent
from plugins.neko_surface.router import NekoSurfaceRouter
from plugins.neko_surface.service import NekoSurfaceGateway, SurfaceGatewayConfig


def _hello() -> dict:
    return SurfaceEvent.create(
        "hello",
        event_id="hello-1",
        sequence=0,
        session_id="session-1",
        surface_id="neko-main",
        character="Elysia",
        origin="neko",
        payload={"capabilities": ["assistant.text"]},
        priority=9,
    ).to_dict()


def test_surface_websocket_authenticates_and_dispatches_user_input() -> None:
    received = []
    gateway = NekoSurfaceGateway(SurfaceGatewayConfig(token="secret"))

    async def handle_input(event) -> None:
        received.append(event)

    gateway.bind_input_handler(handle_input)
    app = NekoSurfaceRouter(plugin=SimpleNamespace(gateway=gateway)).app

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws",
            headers={"Authorization": "Bearer secret"},
        ) as websocket:
            websocket.send_json(_hello())
            ready = websocket.receive_json()
            assert ready["type"] == "ready"

            user_event = SurfaceEvent.create(
                "user.text",
                event_id="user-1",
                sequence=1,
                session_id="session-1",
                surface_id="neko-main",
                character="Elysia",
                origin="neko",
                payload={"text": "hello"},
            )
            websocket.send_json(user_event.to_dict())
            ack = websocket.receive_json()

            assert ack["type"] == "ack"
            assert ack["payload"]["event_id"] == "user-1"
            assert [event.event_id for event in received] == ["user-1"]


def test_surface_websocket_rejects_invalid_token() -> None:
    gateway = NekoSurfaceGateway(SurfaceGatewayConfig(token="secret"))
    app = NekoSurfaceRouter(plugin=SimpleNamespace(gateway=gateway)).app

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws",
            headers={"Authorization": "Bearer wrong"},
        ) as websocket:
            error = websocket.receive_json()

    assert error["type"] == "error"
    assert error["payload"]["code"] == "unauthorized"
