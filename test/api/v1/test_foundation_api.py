"""P3-02 bootstrap、capabilities 与 readiness 契约测试。"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.foundation import (
    FoundationProjection,
    FoundationSnapshot,
    snapshot_from_bot,
)
from src.app.api.v1.policy import PLATFORM_SERVICE_AUDIENCE
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.schemas import AdapterStatus, ComponentStatus
from src.app.api.v1.schemas.common import utc_now
from src.app.api.v1.tokens import SignedValueCodec

ORIGIN = "http://localhost:5173"


def _context(
    projection: FoundationProjection,
) -> tuple[APIContext, AuthStore]:
    store = AuthStore(installation_id="installation-foundation")
    context = APIContext(
        store=store,
        codec=SignedValueCodec("x" * 48),
        installation_id="installation-foundation",
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
        allowed_origins=(ORIGIN,),
        foundation=projection,
    )
    return context, store


def _access_token(context: APIContext, *scopes: str) -> str:
    secret = "foundation-service-secret"
    context.store.add_credential(
        actor_id="actor-foundation",
        audience=PLATFORM_SERVICE_AUDIENCE,
        role="platform_service",
        secret=secret,
        scopes=scopes,
    )
    response = TestClient(create_api_app(context)).post(
        "/auth/sessions",
        json={
            "grant_type": "service_credential",
            "audience": PLATFORM_SERVICE_AUDIENCE,
            "service_credential": secret,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _snapshot() -> FoundationSnapshot:
    return FoundationSnapshot(
        generated_at=utc_now(),
        node_id="node-foundation",
        modules=(
            ComponentStatus(
                component="api",
                state="ready",
                enabled=True,
                owner="app_api_v1",
            ),
            ComponentStatus(
                component="life_event_ledger",
                state="ready",
                enabled=True,
                owner="life_engine",
            ),
            ComponentStatus(
                component="remote_sync",
                state="degraded",
                enabled=True,
                owner="life_engine.shared_sync",
                degraded_reason="RuntimeError: 远程同步不可用。",
            ),
            ComponentStatus(
                component="command_store",
                state="unavailable",
                enabled=True,
                owner="kernel.commands",
                degraded_reason="P3-04 尚未实现。",
            ),
            ComponentStatus(
                component="plugin:feishu_adapter",
                state="ready",
                enabled=True,
                owner="plugin:feishu_adapter",
            ),
        ),
        adapters=(
            AdapterStatus(
                provider="feishu",
                component="feishu_adapter:adapter:feishu_adapter",
                state="degraded",
                enabled=True,
                connected=False,
                degraded_reason="Adapter 已加载但当前未连接。",
            ),
            AdapterStatus(
                provider="qq",
                component="napcat_adapter:adapter",
                state="disabled",
                enabled=False,
                connected=False,
            ),
        ),
        migration_version="api-v1-schema-1",
    )


def test_foundation_routes_enforce_scope_and_keep_health_anonymous() -> None:
    projection = FoundationProjection(
        node_id="node-foundation",
        snapshot_provider=_snapshot,
    )
    context, store = _context(projection)
    client = TestClient(create_api_app(context))
    try:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["alive"] is True

        assert client.get("/bootstrap").status_code == 401
        token = _access_token(context, "system:read")
        headers = {"Authorization": f"Bearer {token}"}
        bootstrap = client.get("/bootstrap", headers=headers)
        readiness = client.get("/readiness", headers=headers)
        capabilities = client.get("/capabilities", headers=headers)

        assert bootstrap.status_code == 200
        assert bootstrap.json()["identity"]["actor_id"] == "actor-foundation"
        assert readiness.status_code == 200
        assert readiness.json()["local_ready"] is True
        assert readiness.json()["state"] == "degraded"
        assert capabilities.status_code == 403
    finally:
        store.close()


def test_capabilities_follow_adapter_state_and_caller_authorization() -> None:
    projection = FoundationProjection(
        node_id="node-foundation",
        snapshot_provider=_snapshot,
    )
    context, store = _context(projection)
    client = TestClient(create_api_app(context))
    try:
        token = _access_token(
            context,
            "capabilities:read",
            "chat:read",
        )
        response = client.get(
            "/capabilities",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        manifests = {
            item["module"]: item for item in response.json()["capabilities"]
        }
        chat = manifests["chat"]
        assert manifests["events"]["state"] == "ready"
        assert chat["state"] == "degraded"
        assert chat["features"]["message.read"] == {
            "schema_version": 1,
            "supported": True,
            "scope": "chat:read",
            "authorized": True,
        }
        assert chat["features"]["message.send.text"]["authorized"] is False
    finally:
        store.close()


def test_health_does_not_read_runtime_snapshot() -> None:
    provider = Mock(side_effect=AssertionError("health must not read runtime state"))
    projection = FoundationProjection(
        node_id="node-foundation",
        snapshot_provider=provider,
    )
    context, store = _context(projection)
    try:
        response = TestClient(create_api_app(context)).get("/health")
        assert response.status_code == 200
        provider.assert_not_called()
    finally:
        store.close()


def test_snapshot_does_not_trigger_lazy_life_service_creation(monkeypatch) -> None:
    class LazyLifePlugin:
        config = SimpleNamespace(shared_sync=SimpleNamespace(enabled=False))
        _service = None

        @property
        def service(self):
            raise AssertionError("readiness must not create the Life Engine service")

    bot = SimpleNamespace(
        bot_name="Elysium",
        manifests={"life_engine": SimpleNamespace(enabled=True)},
        load_results={"life_engine": True},
        plugin_manager=SimpleNamespace(
            get_all_plugins=lambda: {"life_engine": LazyLifePlugin()},
        ),
    )
    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_adapter_manager",
        lambda: SimpleNamespace(get_all_adapters=dict),
    )

    snapshot = snapshot_from_bot(bot)

    states = {item.component: item.state for item in snapshot.modules}
    assert states["life_event_ledger"] == "unavailable"


def test_snapshot_exposes_config_disabled_adapter_without_marking_failure(
    monkeypatch,
) -> None:
    life_service = SimpleNamespace(
        _event_bus=object(),
        _shared_sync_bridge=None,
        _shared_sync_error="",
    )
    loaded_plugins = {
        "life_engine": SimpleNamespace(
            _service=life_service,
            config=SimpleNamespace(
                shared_sync=SimpleNamespace(enabled=False),
            ),
        ),
        "napcat_adapter": SimpleNamespace(
            config=SimpleNamespace(
                plugin=SimpleNamespace(enabled=False),
            ),
        ),
    }
    bot = SimpleNamespace(
        bot_name="Elysium",
        manifests={
            "life_engine": SimpleNamespace(enabled=True),
            "napcat_adapter": SimpleNamespace(enabled=True),
        },
        load_results={"life_engine": True, "napcat_adapter": True},
        plugin_manager=SimpleNamespace(
            get_all_plugins=lambda: loaded_plugins,
        ),
    )
    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_adapter_manager",
        lambda: SimpleNamespace(get_all_adapters=dict),
    )

    snapshot = snapshot_from_bot(bot)
    adapters = {item.provider: item for item in snapshot.adapters}
    readiness = FoundationProjection(
        node_id="Elysium",
        snapshot_provider=lambda: snapshot,
    ).readiness()

    assert adapters["qq"].state == "disabled"
    assert adapters["qq"].enabled is False
    assert readiness.local_ready is True
    assert readiness.state == "degraded"


def test_openapi_contains_stable_foundation_operation_ids() -> None:
    projection = FoundationProjection(node_id="node-foundation")
    context, store = _context(projection)
    try:
        schema = create_api_app(context).openapi()
        assert schema["paths"]["/bootstrap"]["get"]["operationId"] == "getBootstrap"
        assert schema["paths"]["/capabilities"]["get"]["operationId"] == "getCapabilities"
        assert schema["paths"]["/readiness"]["get"]["operationId"] == "getReadiness"
        assert schema["paths"]["/health"]["get"]["operationId"] == "getHealth"
    finally:
        store.close()
