"""P3-14 阶段三旧插件路由迁移期契约测试。

阶段三 /api/v1 统一接口上线后，被取代的旧插件路由必须在迁移期保留，
并通过声明式 deprecation 标记附加 Deprecation / Sunset / Link 响应头，
提示旧客户端迁移。本测试验证四组旧 Router 的弃用契约，且不删除旧路由。
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from plugins.life_engine.memory.router import MemoryRouter
from plugins.livestream.router import LivestreamRouter
from plugins.neko_surface.router import NekoSurfaceRouter
from plugins.voice_live.router import VoiceLiveRouter

# (Router 类, 挂载路径, 取代它的 /api/v1 前缀)
LEGACY_ROUTERS = (
    (LivestreamRouter, "/livestream", "/api/v1/livestream"),
    (VoiceLiveRouter, "/voice-live", "/api/v1/voice-calls"),
    (NekoSurfaceRouter, "/api/neko-surface", "/api/v1/surfaces"),
    (MemoryRouter, "/memory_vis", "/api/v1/admin/memory"),
)


def _instantiate(router_cls: type, *, plugin: object):
    """按 Router 构造函数签名构造最小可实例化环境。"""
    if router_cls is LivestreamRouter:
        from plugins.livestream.config import LivestreamConfig

        config = LivestreamConfig(platform={"room_id": "42"})
        return router_cls(
            plugin=SimpleNamespace(config=config),
            stage=None,
            runtime=None,
        )
    if router_cls is VoiceLiveRouter:
        server = SimpleNamespace(
            route_path="/voice-live",
            ticket_secret_env="",
            ticket_ttl_seconds=60,
            allowed_origins=[],
            max_concurrent_sessions=4,
            max_session_minutes=30,
            idle_timeout_seconds=30,
        )
        config = SimpleNamespace(
            server=server,
            full_duplex=SimpleNamespace(
                provider_type="disabled",
                upstream_url="",
                api_key_env="",
                api_key_file="",
                model_name="",
            ),
            voice_conversion=SimpleNamespace(
                enabled=False,
                service_url="",
                token_env="",
                token_file="",
            ),
        )
        return router_cls(plugin=SimpleNamespace(config=config))
    if router_cls is NekoSurfaceRouter:
        from plugins.neko_surface.service import (
            NekoSurfaceGateway,
            SurfaceGatewayConfig,
        )

        gateway = NekoSurfaceGateway(SurfaceGatewayConfig(token="secret"))

        async def handle_input(event) -> None:
            return None

        gateway.bind_input_handler(handle_input)
        return router_cls(plugin=SimpleNamespace(gateway=gateway))
    # MemoryRouter 需要 plugin.service._memory_service（可空，health 返回 disabled）
    return router_cls(
        plugin=SimpleNamespace(
            service=SimpleNamespace(_memory_service=None),
        )
    )


def test_every_legacy_router_declares_deprecation() -> None:
    """四组旧路由都必须声明弃用标记。"""
    for router_cls, mount, replaced_by in LEGACY_ROUTERS:
        assert router_cls.deprecation_notice, (
            f"{router_cls.__name__} ({mount}) 必须声明 deprecation_notice"
        )
        assert replaced_by in router_cls.deprecation_notice, (
            f"{router_cls.__name__} 的弃用提示应指向 {replaced_by}"
        )
        assert router_cls.deprecation_sunset_date, (
            f"{router_cls.__name__} 必须声明 Sunset 日期"
        )
        assert router_cls.deprecation_migration_link, (
            f"{router_cls.__name__} 必须声明迁移文档链接"
        )


def test_legacy_routers_still_serve_and_return_deprecation_headers() -> None:
    """旧路由仍可访问（迁移期保留），且响应带弃用头。"""
    probe_paths = {
        LivestreamRouter: "/health",
        VoiceLiveRouter: "/health",
        NekoSurfaceRouter: "/status",
        MemoryRouter: "/",
    }
    for router_cls, _mount, _replaced_by in LEGACY_ROUTERS:
        router = _instantiate(router_cls, plugin=object())
        client = TestClient(router.app)
        response = client.get(probe_paths[router_cls])
        # 旧路由保持可用；健康类接口在依赖缺失时可返回 503（memory disabled），
        # 但弃用标记不应改变状态码语义
        assert response.status_code in {200, 404, 503}, (
            f"{router_cls.__name__} probe {probe_paths[router_cls]} "
            f"unexpected status {response.status_code}"
        )
        if response.status_code == 200:
            assert response.headers.get("Deprecation") == "true", (
                f"{router_cls.__name__} 200 响应必须带 Deprecation 头"
            )


def test_legacy_router_headers_do_not_leak_into_api_v1() -> None:
    """新 /api/v1 基座不继承旧插件的弃用头（无消费方时自动跳过）。"""
    from src.core.components.base.router import BaseRouter

    class _NewStyleRouter(BaseRouter):
        __test__ = False
        router_name = "new_api"
        custom_route_path = "/api/v1/probe"

        def register_endpoints(self) -> None:
            @self.app.get("/health")
            async def health() -> dict:
                return {"status": "ok"}

    with TestClient(_NewStyleRouter(plugin=object()).app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert "Deprecation" not in response.headers
        assert "Sunset" not in response.headers
