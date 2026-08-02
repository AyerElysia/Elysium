from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from plugins.livestream.config import LivestreamConfig
from plugins.livestream.domain import HealthSnapshot
from plugins.livestream.router import LivestreamRouter


class FakeRuntime:
    state = "stopped"

    def __init__(self) -> None:
        self.start_calls = 0

    async def start(self) -> str:
        self.start_calls += 1
        self.state = "running"
        return "session-1"

    async def stop(self, *, reason: str) -> None:
        self.state = "stopped"

    async def interrupt(self) -> bool:
        return False

    async def manual_say(self, text: str) -> str:
        return "utterance-1"

    async def health(self) -> HealthSnapshot:
        return HealthSnapshot(status=self.state)


def _router() -> tuple[LivestreamRouter, FakeRuntime]:
    config = LivestreamConfig(platform={"room_id": "42"})
    runtime = FakeRuntime()
    router = LivestreamRouter(SimpleNamespace(config=config), runtime=runtime)
    return router, runtime


def test_router_mount_does_not_auto_start_runtime() -> None:
    router, runtime = _router()
    with TestClient(router.app) as client:
        response = client.get("/health")
        page = client.get("/")
    assert response.status_code == 200
    assert runtime.start_calls == 0
    assert response.json()["status"] == "stopped"
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"


def test_mutation_requires_same_origin_single_use_ticket() -> None:
    router, runtime = _router()
    origin = {"Origin": "http://testserver", "Host": "testserver"}
    with TestClient(router.app) as client:
        ticket_response = client.post("/ticket", headers=origin)
        assert ticket_response.status_code == 200
        ticket = ticket_response.json()["ticket"]
        headers = {**origin, "Authorization": f"Bearer {ticket}"}
        start = client.post("/api/start", headers=headers)
        replay = client.post("/api/start", headers=headers)

    assert start.status_code == 200
    assert replay.status_code == 401
    assert runtime.start_calls == 1


def test_cross_origin_ticket_request_is_rejected() -> None:
    router, _runtime = _router()
    with TestClient(router.app) as client:
        response = client.post(
            "/ticket",
            headers={"Origin": "https://evil.example", "Host": "testserver"},
        )
    assert response.status_code == 403


def test_allowed_origin_without_port_does_not_allow_arbitrary_ports() -> None:
    router, _runtime = _router()
    router._config.server.allowed_origins = ["https://studio.example"]

    assert router._origin_allowed("https://studio.example", "testserver")
    assert not router._origin_allowed("https://studio.example:444", "testserver")


def test_explicit_origin_receives_exact_cors_permission() -> None:
    config = LivestreamConfig(
        platform={"room_id": "42"},
        server={"allowed_origins": ["https://studio.example"]},
    )
    router = LivestreamRouter(
        SimpleNamespace(config=config),
        runtime=FakeRuntime(),
    )
    with TestClient(router.app) as client:
        response = client.post(
            "/ticket",
            headers={"Origin": "https://studio.example", "Host": "testserver"},
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://studio.example"
