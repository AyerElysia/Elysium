"""P3-12 公共接口的权限、主体性和领域 facade 契约。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.p312 import P312Providers
from src.app.api.v1.policy import ADMIN_FRONTEND_AUDIENCE, USER_FRONTEND_AUDIENCE
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.tokens import SignedValueCodec

SECRET = "p" * 48
ORIGIN = "http://localhost:5173"


def _app(
    tmp_path: Path, *, providers: P312Providers | None = None
) -> tuple[TestClient, AuthStore]:
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


def _token(
    client: TestClient,
    store: AuthStore,
    *,
    admin: bool = False,
    scopes: tuple[str, ...] = (),
) -> str:
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

    client, store = _app(
        tmp_path, providers=P312Providers(world=World(), auditor=Auditor())
    )
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
    token = _token(
        client, store, admin=True, scopes=("auth:session", "consciousness:read")
    )
    response = client.get(
        "/admin/consciousness/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "component_unavailable"
    store.close()


async def test_runtime_memory_search_never_enables_legacy_weighted_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.api.v1 import p312_runtime

    observed: dict[str, Any] = {}

    class _Memory:
        async def search_memory(
            self, query: str, **kwargs: Any
        ) -> list[dict[str, Any]]:
            observed["search"] = {"query": query, **kwargs}
            return [{"file_path": "notes/example.md", "source": "fts"}]

        async def expand_living_document_associations(
            self,
            results: list[dict[str, Any]],
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            observed["expand"] = {"results": results, **kwargs}
            return [*results, {"file_path": "notes/related.md", "source": "associated"}]

        async def build_memory_bundles(
            self,
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            observed["bundles"] = kwargs
            return [{"bundle": "canonical"}]

    monkeypatch.setattr(
        p312_runtime,
        "_life_service",
        lambda: SimpleNamespace(memory_service=_Memory()),
    )

    values = await p312_runtime.RuntimeMemoryProvider().search(
        "continuity",
        top_k=7,
        session=SimpleNamespace(actor_id="admin-1"),  # type: ignore[arg-type]
    )

    assert values == [{"bundle": "canonical"}]
    assert observed["search"] == {
        "query": "continuity",
        "top_k": 7,
        "enable_association": False,
        "return_bundles": False,
    }
    assert observed["expand"]["results"] == [
        {"file_path": "notes/example.md", "source": "fts"}
    ]
    assert observed["expand"]["context_key"] == "api-v1/admin/memory:admin-1"
    assert isinstance(observed["expand"]["random_seed"], int)
    assert observed["expand"]["limit"] == 7
    assert observed["bundles"] == {
        "query": "continuity",
        "results": [
            {"file_path": "notes/example.md", "source": "fts"},
            {"file_path": "notes/related.md", "source": "associated"},
        ],
        "top_k": 7,
    }


async def test_runtime_memory_search_fails_closed_without_canonical_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.api.v1 import p312_runtime

    class _LegacyOnlyMemory:
        async def search_memory(self, query: str, **kwargs: Any) -> list[Any]:
            del query, kwargs
            return []

    monkeypatch.setattr(
        p312_runtime,
        "_life_service",
        lambda: SimpleNamespace(memory_service=_LegacyOnlyMemory()),
    )

    with pytest.raises(
        RuntimeError,
        match="canonical memory association facade is unavailable",
    ):
        await p312_runtime.RuntimeMemoryProvider().search(
            "continuity",
            top_k=7,
            session=SimpleNamespace(actor_id="admin-1"),  # type: ignore[arg-type]
        )


async def test_runtime_memory_get_experience_uses_lossless_composite_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.memory.experience import (
        ExperienceOccurrenceCursor,
        ExperienceOccurrencePage,
        ExperienceOccurrenceRef,
        ExperienceRecord,
    )
    from src.app.api.v1 import p312_runtime

    occurrences = tuple(
        ExperienceOccurrenceRef(
            occurrence_id=f"occurrence-{index:04d}",
            source_event_id=f"source-{index:04d}",
            canonical_event_id=f"canonical-{index:04d}",
            canonical_payload_sha256=f"{index:064x}",
            ingest_position=7,
            recorded_at="2026-08-13T00:00:00+00:00",
            experience=ExperienceRecord(
                event_id=f"event-{index:04d}",
                sequence=7,
                occurred_at="2026-08-13T00:00:00+00:00",
                recorded_at="2026-08-13T00:00:00+00:00",
                source="test",
                channel="test",
                event_type="test.experience",
                content=f"experience {index}",
            ),
        )
        for index in range(501)
    )
    frontier = ExperienceOccurrenceCursor(7, occurrences[-1].occurrence_id)
    first_cursor = ExperienceOccurrenceCursor(7, occurrences[499].occurrence_id)
    calls: list[dict[str, Any]] = []

    class _Memory:
        async def list_experience_occurrence_page(self, **kwargs: Any) -> Any:
            calls.append(dict(kwargs))
            if kwargs.get("after") is None:
                return ExperienceOccurrencePage(
                    items=occurrences[:500],
                    next_cursor=first_cursor,
                    frontier=frontier,
                    has_more=True,
                )
            assert kwargs["after"] == first_cursor
            assert kwargs["through"] == frontier
            return ExperienceOccurrencePage(
                items=occurrences[500:],
                next_cursor=frontier,
                frontier=frontier,
                has_more=False,
            )

    monkeypatch.setattr(
        p312_runtime,
        "_life_service",
        lambda: SimpleNamespace(memory_service=_Memory()),
    )

    result = await p312_runtime.RuntimeMemoryProvider().get_experience(
        "occurrence-0500",
        session=SimpleNamespace(actor_id="admin-1"),  # type: ignore[arg-type]
    )

    assert result["event_id"] == "event-0500"
    assert result["content"] == "experience 500"
    assert len(calls) == 2
    assert calls[0] == {"after": None, "through": None, "limit": 500}
    assert calls[1] == {
        "after": first_cursor,
        "through": frontier,
        "limit": 500,
    }


async def test_runtime_memory_legacy_graph_stats_are_explicitly_non_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.api.v1 import p312_runtime

    class _Memory:
        async def get_stats(self) -> dict[str, Any]:
            return {
                "projection_kind": "legacy_memory_graph_statistics",
                "authority": False,
                "read_only": True,
                "canonical_health_entrypoint": "memory.health_snapshot",
                "legacy": {"nodes": 42, "edges": 17},
            }

    monkeypatch.setattr(
        p312_runtime,
        "_life_service",
        lambda: SimpleNamespace(memory_service=_Memory()),
    )
    provider = p312_runtime.RuntimeMemoryProvider()
    session = SimpleNamespace(actor_id="admin-1")

    stats = await provider.stats(session=session)  # type: ignore[arg-type]
    graph = await provider.graph(session=session)  # type: ignore[arg-type]

    assert stats["authority"] is False
    assert stats["read_only"] is True
    assert stats["projection_kind"] == "legacy_memory_graph_statistics"
    assert stats["canonical_health_entrypoint"] == "memory.health_snapshot"
    assert graph == [{"projection": "memory_graph", "stats": stats}]


async def test_runtime_memory_health_is_recursively_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.api.v1 import p312_runtime

    class _Memory:
        async def health_snapshot(self) -> dict[str, Any]:
            return {
                "status": "degraded",
                "database_path": "/private/memory.db",
                "runtime": {
                    "status": "healthy",
                    "dsn": "mysql://root:password@localhost/private",
                    "authority_token": "private-authority-token",
                },
                "index": {
                    "hash_mismatch_count": 1,
                    "hash_mismatch_paths": ["diaries/private.md"],
                },
                "continuity": {
                    "memory_sha256": "a" * 64,
                    "memory_bytes": 1234,
                },
            }

    monkeypatch.setattr(
        p312_runtime,
        "_life_service",
        lambda: SimpleNamespace(memory_service=_Memory()),
    )

    health = await p312_runtime.RuntimeMemoryProvider().health(
        session=SimpleNamespace(actor_id="admin-1"),  # type: ignore[arg-type]
    )

    assert health == {
        "status": "degraded",
        "runtime": {"status": "healthy"},
        "index": {"hash_mismatch_count": 1},
        "continuity": {
            "memory_sha256": "a" * 64,
            "memory_bytes": 1234,
        },
    }
    serialized = repr(health)
    assert "private" not in serialized
    assert "password" not in serialized
    assert "token" not in serialized
