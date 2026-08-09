"""BaseRouter 阶段三迁移期弃用标记契约测试。

阶段三新 /api/v1 接口上线后，被取代的旧插件路由必须在迁移期保留，
并通过 Deprecation / Sunset / Link 响应头向旧客户端宣告迁移去向。
本测试验证声明式弃用标记在基类层的通用行为，不修改旧路由处理逻辑。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.core.components.base.router import BaseRouter


class _DeprecatedRouter(BaseRouter):
    """声明弃用标记的测试 Router。"""

    __test__ = False
    router_name = "deprecated_router"
    router_description = "Deprecated test router"
    custom_route_path = "/old-api"
    deprecation_notice = "已被 /api/v1/legacy 取代，请迁移；迁移期保留"
    deprecation_sunset_date = "2027-02-01"
    deprecation_migration_link = "https://example.com/docs/api"

    def register_endpoints(self) -> None:
        @self.app.get("/health")
        async def health() -> dict:
            return {"status": "ok"}

        @self.app.post("/mutate")
        async def mutate() -> dict:
            return {"accepted": True}


class _PlainRouter(BaseRouter):
    """未声明弃用标记的测试 Router。"""

    __test__ = False
    router_name = "plain_router"
    router_description = "Plain test router"
    custom_route_path = "/new-api"

    def register_endpoints(self) -> None:
        @self.app.get("/health")
        async def health() -> dict:
            return {"status": "ok"}


@pytest.fixture
def deprecated_client() -> TestClient:
    with TestClient(_DeprecatedRouter(plugin=object()).app) as client:
        yield client


@pytest.fixture
def plain_client() -> TestClient:
    with TestClient(_PlainRouter(plugin=object()).app) as client:
        yield client


class TestRouterDeprecationHeaders:
    def test_deprecated_router_returns_deprecation_headers_on_get(
        self, deprecated_client: TestClient
    ) -> None:
        response = deprecated_client.get("/health")
        assert response.status_code == 200
        assert response.headers["Deprecation"] == "true"
        assert "UTF-8''" in response.headers["Deprecation-Notice"]
        assert (
            "%E5%B7%B2%E8%A2%AB" in response.headers["Deprecation-Notice"]
        )  # "已被" 的 RFC 5987 编码
        assert response.headers["Sunset"] == "2027-02-01"
        assert "deprecation" in response.headers["Link"]

    def test_deprecated_router_returns_headers_on_mutation(
        self, deprecated_client: TestClient
    ) -> None:
        response = deprecated_client.post("/mutate")
        assert response.status_code == 200
        assert response.headers["Deprecation"] == "true"

    def test_deprecation_headers_do_not_change_payload(
        self, deprecated_client: TestClient
    ) -> None:
        response = deprecated_client.get("/health")
        assert response.json() == {"status": "ok"}

    def test_plain_router_has_no_deprecation_headers(
        self, plain_client: TestClient
    ) -> None:
        response = plain_client.get("/health")
        assert response.status_code == 200
        assert "Deprecation" not in response.headers
        assert "Sunset" not in response.headers
        assert "Link" not in response.headers

    def test_router_without_deprecation_does_not_add_middleware(self) -> None:
        router = _PlainRouter(plugin=object())
        middleware_classes = [
            m.cls for m in router.app.user_middleware
        ]
        assert "DeprecationHeaderMiddleware" not in str(middleware_classes)
