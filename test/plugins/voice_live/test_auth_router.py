from __future__ import annotations

from types import SimpleNamespace

from plugins.voice_live.auth import TicketAuthority
from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.router import VoiceLiveRouter


def test_ticket_is_single_use_and_tamper_evident() -> None:
    authority = TicketAuthority("test-secret", 30)
    ticket = authority.issue()

    assert authority.consume(ticket) is True
    assert authority.consume(ticket) is False
    assert authority.consume(ticket + "x") is False


def test_router_uses_configured_path_and_strict_origin_matching() -> None:
    config = VoiceLiveConfig()
    router = VoiceLiveRouter(SimpleNamespace(config=config))

    assert router.get_route_path() == "/voice-live"
    assert router._origin_allowed("http://localhost:8080") is True
    assert router._origin_allowed("http://localhost.evil.example") is False
    assert router._origin_allowed("file://localhost") is False
    paths = {route.path for route in router.app.routes}
    assert {"/ticket", "/ws", "/observe", "/overlay"} <= paths
