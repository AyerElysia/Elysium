"""P3-12 公共接口的权限、主体性和领域 facade 契约。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.p312 import P312Providers
from src.app.api.v1.policy import ADMIN_FRONTEND_AUDIENCE, USER_FRONTEND_AUDIENCE
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.tokens import SignedValueCodec

SECRET = "p" * 48
ORIGIN = "http://localhost:5173"


def _app(tmp_path: Path, *, providers: P312Providers | None = None) -> tuple[TestClient, AuthStore]:
    store = AuthStore(tmp_path / "api.sqlite3", installation_id="p312")
    codec = SignedValueCodec(SECRET)
    context = APIContext(
        store=store,
        codec=codec,
        installation_id="p312",
        allowed_origins=(ORIGIN,),
        p312=providers or P312Providers(),
    )
    return TestClient(create_api_app(context)), store


def _token(client: TestClient, store: AuthStore, *, admin: bool = False, scopes: tuple[str, ...] = ()) -> str:
    codec = SignedValueCodec(SECRET)
    audience = ADMIN_FRONTEND_AUDIENCE if admin else USER_FRONTEND_AUDIENCE
    challenge = store.create_bootstrap_challenge(
        codec=codec,
        audience=audience,
        origin=ORIGIN,
        scopes=scopes or ("auth:session",),
    )
    response = client.post(
        "/auth/sessions",
        headers={"Origin": ORIGIN},
        json={
            "grant_type": "bootstrap_challenge",
            "audience": audience,
            "bootstrap_challenge": challenge,
            "origin": ORIGIN,
        },
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def test_p312_admin_routes_are_not_available_to_regular_user(tmp_path: Path) -> None:
    client, store = _app(tmp_path)
    token = _token(client, store, scopes=("auth:session", "world:read"))
    response = client.get(
        "/admin/world/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "role_required"
    store.close()


def test_p312_world_observation_is_append_only_and_audited(tmp_path: Path) -> None:
    audit: list[dict[str, Any]] = []

    class Auditor:
        def record(self, **payload: Any) -> None:
            audit.append(payload)

    class World:
        async def report_observation(self, **payload: Any) -> dict[str, Any]:
            return {"event_id": "evt-1", "occurrence_id": payload["occurrence_id"]}

    client, store = _app(tmp_path, providers=P312Providers(world=World(), auditor=Auditor()))
    token = _token(client, store, admin=True, scopes=("auth:session", "world:observe"))
    response = client.post(
        "/admin/world/observations",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "report": "外部观察文本",
            "source_instance_id": "chat_global",
            "subject": "surface",
            "predicate": "observed_state",
            "occurrence_id": "occ-1",
        },
    )
    assert response.status_code == 201
    assert response.json()["result"]["occurrence_id"] == "occ-1"
    assert audit[0]["action"] == "append"
    store.close()


def test_p312_memory_has_no_write_route(tmp_path: Path) -> None:
    client, store = _app(tmp_path)
    token = _token(client, store, admin=True, scopes=("auth:session", "memory:read"))
    openapi = client.get("/openapi.json").json()
    assert "/admin/memory/search" in openapi["paths"]
    assert not any(
        "/admin/memory" in path
        and path != "/admin/memory/projections/{projection}:rebuild"
        and method.lower() in {"post", "put", "patch", "delete"}
        for path, methods in openapi["paths"].items()
        for method in methods
    )
    response = client.get(
        "/admin/memory/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    store.close()


def test_p312_empty_provider_is_explicitly_unavailable(tmp_path: Path) -> None:
    client, store = _app(tmp_path)
    token = _token(client, store, admin=True, scopes=("auth:session", "consciousness:read"))
    response = client.get(
        "/admin/consciousness/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "component_unavailable"
    store.close()
