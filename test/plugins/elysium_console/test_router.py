from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.elysium_console.router import (
    MINECRAFT_ACTION_HEADER,
    ElysiumConsoleRouter,
    LocalConsoleSessions,
    _is_loopback_host,
)

ROOT = Path(__file__).resolve().parents[3]


class _Catalog:
    async def overview(self) -> dict:
        return {"schema": "elysium-data-console.v1", "kind": "overview"}

    async def timeline(self, **_kwargs) -> dict:
        return {"kind": "timeline", "items": []}

    async def subject_documents(self) -> dict:
        return {"kind": "subject_documents", "items": []}

    async def memory_summary(self) -> dict:
        return {"kind": "memory_summary"}

    async def memory_experiences(self, **_kwargs) -> dict:
        return {"kind": "memory_experiences", "items": []}

    async def world_page(self, **_kwargs) -> dict:
        return {"kind": "world_assertions", "items": []}

    async def world_assertion_value(self, *_args, **_kwargs) -> dict:
        return {"kind": "world_assertion_value", "chunk": {}}

    async def attention_page(self, **_kwargs) -> dict:
        return {"kind": "attention_threads", "page": {"items": []}}

    async def workspace_page(self, **_kwargs) -> dict:
        return {"kind": "workspace_page", "items": []}

    async def workspace_text(self, **_kwargs) -> dict:
        return {"kind": "workspace_text", "content": ""}

    async def data_map(self) -> dict:
        return {"kind": "data_map", "domains": []}

    async def minecraft_status(self) -> dict:
        return {"kind": "minecraft_status", "available": True, "active": False}

    async def minecraft_preflight(self) -> dict:
        return {"kind": "minecraft_preflight", "result": {"success": True}}

    async def minecraft_start(self, **kwargs) -> dict:
        return {"kind": "minecraft_start", "goal": kwargs.get("goal", "")}

    async def minecraft_stop(self) -> dict:
        return {"kind": "minecraft_stop", "result": {"success": True}}


def _app() -> FastAPI:
    router = ElysiumConsoleRouter(
        SimpleNamespace(),
        catalog=_Catalog(),
        sessions=LocalConsoleSessions(),
    )
    app = FastAPI()
    app.mount("/console", router.app)
    return app


def test_console_shell_issues_local_http_only_session_and_security_headers() -> None:
    with TestClient(
        _app(),
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50100),
    ) as client:
        response = client.get("/console/")

        assert response.status_code == 200
        assert "elysium_console_session=" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert "Path=/console" in response.headers["set-cookie"]
        assert response.headers["cache-control"] == "no-store"
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY"
        assert "<style" not in response.text
        assert "<script>" not in response.text
        assert 'src="assets/app.js"' in response.text


def test_console_api_requires_session_and_rejects_cross_origin() -> None:
    app = _app()
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50100),
    ) as anonymous:
        assert anonymous.get("/console/api/v1/overview").status_code == 401

    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50101),
    ) as client:
        assert client.get("/console/").status_code == 200
        accepted = client.get(
            "/console/api/v1/overview",
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        rejected = client.get(
            "/console/api/v1/overview",
            headers={"Origin": "https://example.invalid"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["kind"] == "overview"
        assert rejected.status_code == 403


def test_console_rejects_non_loopback_client_before_serving_shell() -> None:
    with TestClient(
        _app(),
        base_url="http://127.0.0.1:8000",
        client=("203.0.113.8", 50100),
    ) as client:
        response = client.get("/console/")

    assert response.status_code == 403
    assert "elysium_console_session" not in response.headers.get("set-cookie", "")


def test_loopback_detection_supports_ipv4_ipv6_and_mapped_ipv4() -> None:
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("::1") is True
    assert _is_loopback_host("::ffff:127.0.0.1") is True
    assert _is_loopback_host("203.0.113.1") is False
    assert _is_loopback_host("localhost") is False


def test_manifest_registers_only_the_read_only_router() -> None:
    manifest = json.loads(
        (ROOT / "plugins/elysium_console/manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["dependencies"]["plugins"] == ["life_engine"]
    assert manifest["include"] == [
        {
            "component_type": "router",
            "component_name": "elysium_console",
            "dependencies": [],
            "enabled": True,
        }
    ]


def test_console_router_exposes_only_narrow_minecraft_mutations() -> None:
    router = ElysiumConsoleRouter(SimpleNamespace(), catalog=_Catalog())
    mutations = {
        (getattr(route, "path", ""), method)
        for route in router.app.routes
        for method in (getattr(route, "methods", None) or set())
        if method not in {"GET", "HEAD"}
    }

    assert mutations == {
        ("/api/v1/minecraft/start", "POST"),
        ("/api/v1/minecraft/stop", "POST"),
    }


def test_minecraft_mutations_require_same_origin_and_explicit_action_header() -> None:
    with TestClient(
        _app(),
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50102),
    ) as client:
        assert client.get("/console/").status_code == 200
        path = "/console/api/v1/minecraft/start?goal=together"
        assert client.post(path).status_code == 403
        assert client.post(
            path,
            headers={
                "Origin": "https://example.invalid",
                "X-Elysium-Console-Action": MINECRAFT_ACTION_HEADER,
            },
        ).status_code == 403
        accepted = client.post(
            path,
            headers={
                "Origin": "http://127.0.0.1:8000",
                "X-Elysium-Console-Action": MINECRAFT_ACTION_HEADER,
            },
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"kind": "minecraft_start", "goal": "together"}


def test_all_read_routes_are_mounted_without_cors() -> None:
    paths = [
        "/console/api/v1/overview",
        "/console/api/v1/minecraft",
        "/console/api/v1/minecraft/preflight",
        "/console/api/v1/timeline",
        "/console/api/v1/subject",
        "/console/api/v1/memory",
        "/console/api/v1/memory/experiences",
        "/console/api/v1/world",
        "/console/api/v1/world/assertions/assertion-1/value",
        "/console/api/v1/attention",
        "/console/api/v1/workspace",
        "/console/api/v1/workspace/text?path=SOUL.md",
        "/console/api/v1/catalog",
    ]
    with TestClient(
        _app(),
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50100),
    ) as client:
        assert client.get("/console/").status_code == 200
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, path
            assert "access-control-allow-origin" not in response.headers


def test_console_sessions_expire_without_storing_plaintext_tokens() -> None:
    now = [100.0]
    sessions = LocalConsoleSessions(ttl_seconds=60, clock=lambda: now[0])

    token = sessions.issue()

    assert sessions.valid(token) is True
    assert token not in sessions._sessions
    now[0] = 161.0
    assert sessions.valid(token) is False
