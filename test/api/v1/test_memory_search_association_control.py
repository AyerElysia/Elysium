"""The public memory enhancement switch preserves direct evidence and gates reads."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from plugins.life_engine.memory.search import SearchResult
from src.app.api.v1 import p312_runtime
from src.app.api.v1.p312 import P312Providers
from src.app.api.v1.runtime import APIError
from src.app.api.v1.schemas import P312MemorySearchRequest
from test.api.v1.test_p312_api import _app, _token


def _direct_hit() -> SearchResult:
    return SearchResult(
        file_path="notes/continuity.md",
        title="continuity",
        snippet="direct evidence excerpt",
        relevance=0.7,
        source="direct",
        node_id="subject-file:doc-1",
        document_id="doc-1",
        version_id="version-3",
        document_revision=3,
        binding_revision=2,
        content_sha256="d" * 64,
    )


@pytest.mark.parametrize("selected", [None, "false", "true"])
def test_memory_search_public_switch_reaches_runtime_and_preserves_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: str | None
) -> None:
    calls: list[tuple[str, Any]] = []
    audits: list[dict[str, Any]] = []
    direct = _direct_hit()
    bundles = [{"primary_path": direct.file_path, "evidence": [asdict(direct)]}]

    class Memory:
        async def search_memory(self, query: str, **kwargs: Any) -> list[SearchResult]:
            calls.append(("direct", {"query": query, **kwargs}))
            return [direct]

        async def expand_living_document_associations(
            self, results: list[SearchResult], **kwargs: Any
        ) -> list[SearchResult]:
            assert selected == "true", "disabled enhancement performed extra reads"
            assert results == [direct]
            assert kwargs["context_key"].startswith("api-v1/admin/memory:")
            assert type(kwargs["random_seed"]) is int
            assert kwargs["limit"] == 3
            calls.append(("expand", kwargs))
            return results

        async def build_memory_bundles(self, **kwargs: Any) -> list[dict[str, Any]]:
            assert selected == "true", "disabled enhancement built bundles"
            assert kwargs == {"query": "continuity", "results": [direct], "top_k": 3}
            calls.append(("bundles", kwargs))
            return bundles

    monkeypatch.setattr(
        p312_runtime, "_life_service", lambda: SimpleNamespace(memory_service=Memory())
    )
    client, store = _app(
        tmp_path,
        providers=P312Providers(
            memory=p312_runtime.RuntimeMemoryProvider(),
            auditor=SimpleNamespace(record=lambda **payload: audits.append(payload)),
        ),
    )
    try:
        token = _token(
            client, store, admin=True, scopes=("auth:session", "memory:read")
        )
        params = {"query": "continuity", "top_k": "3"}
        if selected is not None:
            params["enable_association"] = selected
        response = client.get(
            "/admin/memory/search",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert calls[0] == (
            "direct",
            {
                "query": "continuity",
                "top_k": 3,
                "enable_association": False,
                "return_bundles": False,
            },
        )
        if selected == "true":
            assert [name for name, _ in calls] == ["direct", "expand", "bundles"]
            assert response.json()["items"] == bundles
        else:
            assert [name for name, _ in calls] == ["direct"]
            assert response.json()["items"] == [
                {**asdict(direct), "file_ref": "subject-file:doc-1@version-3"}
            ]
        assert response.json()["has_more"] is False
        assert audits[0]["action"] == "read_sensitive"
        assert audits[0]["resource"] == "memory.search"
        assert "query" not in audits[0]
    finally:
        store.close()


@pytest.mark.parametrize("selected", [None, False])
async def test_runtime_direct_search_never_resolves_optional_facade(
    monkeypatch: pytest.MonkeyPatch, selected: bool | None
) -> None:
    direct = {"file_path": "legacy.md", "source": "direct", "snippet": "legacy excerpt"}

    class DirectOnly:
        async def search_memory(
            self, query: str, **kwargs: Any
        ) -> list[dict[str, Any]]:
            assert kwargs == {
                "top_k": 2,
                "enable_association": False,
                "return_bundles": False,
            }
            return [direct]

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"direct search resolved optional facade: {name}")

    monkeypatch.setattr(
        p312_runtime,
        "_life_service",
        lambda: SimpleNamespace(memory_service=DirectOnly()),
    )
    kwargs: dict[str, Any] = {
        "top_k": 2,
        "session": SimpleNamespace(actor_id="admin-1"),
    }
    if selected is not None:
        kwargs["enable_association"] = selected
    result = await p312_runtime.RuntimeMemoryProvider().search("continuity", **kwargs)
    assert result == [direct]
    assert "file_ref" not in result[0]  # Do not invent document identity from a path.


@pytest.mark.parametrize("selected", ["false", "true", 0, 1, None])
async def test_runtime_rejects_non_boolean_before_accessing_memory(
    monkeypatch: pytest.MonkeyPatch, selected: Any
) -> None:
    def unavailable() -> Any:
        raise AssertionError("invalid input must not access memory")

    monkeypatch.setattr(p312_runtime, "_life_service", unavailable)
    with pytest.raises(TypeError, match="enable_association must be a bool"):
        await p312_runtime.RuntimeMemoryProvider().search(
            "continuity",
            top_k=2,
            enable_association=selected,
            session=SimpleNamespace(actor_id="admin-1"),  # type: ignore[arg-type]
        )


def test_memory_search_schema_and_openapi_default_to_direct_only(
    tmp_path: Path,
) -> None:
    assert P312MemorySearchRequest(query="continuity").enable_association is False
    assert (
        P312MemorySearchRequest(
            query="continuity", enable_association=True
        ).enable_association
        is True
    )
    with pytest.raises(ValidationError):
        P312MemorySearchRequest(query="continuity", enable_association="invalid")
    client, store = _app(tmp_path)
    try:
        route = client.get("/openapi.json").json()["paths"]["/admin/memory/search"][
            "get"
        ]
        parameter = next(
            item for item in route["parameters"] if item["name"] == "enable_association"
        )
        assert parameter["in"] == "query"
        assert parameter["required"] is False
        assert parameter["schema"]["type"] == "boolean"
        assert parameter["schema"]["default"] is False
        assert "503" in route["responses"]
    finally:
        store.close()


@pytest.mark.parametrize(
    "params",
    [
        {"enable_association": "maybe"},
        {"enable_association": "2"},
        {"enable_association": "null"},
        {"top_k": "0"},
        {"top_k": "101"},
        {"query": "x" * 2001},
    ],
)
def test_memory_search_rejects_invalid_query_without_calling_provider(
    tmp_path: Path, params: dict[str, str]
) -> None:
    called = False

    async def search(*args: Any, **kwargs: Any) -> list[Any]:
        nonlocal called
        called = True
        return []

    client, store = _app(
        tmp_path, providers=P312Providers(memory=SimpleNamespace(search=search))
    )
    try:
        token = _token(
            client, store, admin=True, scopes=("auth:session", "memory:read")
        )
        response = client.get(
            "/admin/memory/search",
            params={"query": "continuity", **params},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"
        assert not called
    finally:
        store.close()


@pytest.mark.parametrize(
    ("admin", "scopes", "code"),
    [
        (False, ("auth:session", "memory:read"), "role_required"),
        (True, ("auth:session",), "scope_required"),
    ],
)
@pytest.mark.parametrize("selected", ["false", "true"])
def test_memory_search_switch_does_not_expand_permissions(
    tmp_path: Path, admin: bool, scopes: tuple[str, ...], code: str, selected: str
) -> None:
    called = False

    async def search(*args: Any, **kwargs: Any) -> list[Any]:
        nonlocal called
        called = True
        return [{"file_ref": "subject-file:restricted@v1"}]

    client, store = _app(
        tmp_path, providers=P312Providers(memory=SimpleNamespace(search=search))
    )
    try:
        token = _token(client, store, admin=admin, scopes=scopes)
        response = client.get(
            "/admin/memory/search",
            params={"query": "continuity", "enable_association": selected},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == code
        assert not called
        assert "restricted" not in response.text
    finally:
        store.close()


@pytest.mark.parametrize("selected", [None, "false", "true"])
@pytest.mark.parametrize("kind", ["absent", "no_search", "legacy_signature"])
def test_memory_provider_capability_is_explicitly_unavailable(
    tmp_path: Path, selected: str | None, kind: str
) -> None:
    called = False

    async def old_search(query: str, *, top_k: int, session: Any) -> list[Any]:
        nonlocal called
        called = True
        return []

    provider = {
        "absent": None,
        "no_search": SimpleNamespace(),
        "legacy_signature": SimpleNamespace(search=old_search),
    }[kind]
    client, store = _app(tmp_path, providers=P312Providers(memory=provider))
    try:
        token = _token(
            client, store, admin=True, scopes=("auth:session", "memory:read")
        )
        params = {"query": "continuity"}
        if selected is not None:
            params["enable_association"] = selected
        response = client.get(
            "/admin/memory/search",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "component_unavailable"
        assert not called
    finally:
        store.close()


def test_explicit_enhancement_without_canonical_facade_is_not_direct_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def search_memory(query: str, **kwargs: Any) -> list[SearchResult]:
        return [_direct_hit()]

    monkeypatch.setattr(
        p312_runtime,
        "_life_service",
        lambda: SimpleNamespace(
            memory_service=SimpleNamespace(search_memory=search_memory)
        ),
    )
    client, store = _app(
        tmp_path, providers=P312Providers(memory=p312_runtime.RuntimeMemoryProvider())
    )
    try:
        token = _token(
            client, store, admin=True, scopes=("auth:session", "memory:read")
        )
        response = client.get(
            "/admin/memory/search",
            params={"query": "continuity", "enable_association": "true"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "component_unavailable"
        assert "items" not in response.json()
    finally:
        store.close()


@pytest.mark.parametrize(
    ("phase", "selected"),
    [("direct", "false"), ("direct", "true"), ("expand", "true"), ("bundles", "true")],
)
def test_memory_execution_failure_never_becomes_success_or_empty_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, selected: str
) -> None:
    calls: list[str] = []

    class Memory:
        async def search_memory(self, query: str, **kwargs: Any) -> list[SearchResult]:
            calls.append("direct")
            if phase == "direct":
                raise RuntimeError("private-storage-detail")
            return [_direct_hit()]

        async def expand_living_document_associations(
            self, results: Any, **kwargs: Any
        ) -> Any:
            calls.append("expand")
            if phase == "expand":
                raise RuntimeError("private-storage-detail")
            return results

        async def build_memory_bundles(self, **kwargs: Any) -> list[Any]:
            calls.append("bundles")
            raise RuntimeError("private-storage-detail")

    monkeypatch.setattr(
        p312_runtime, "_life_service", lambda: SimpleNamespace(memory_service=Memory())
    )
    client, store = _app(
        tmp_path, providers=P312Providers(memory=p312_runtime.RuntimeMemoryProvider())
    )
    try:
        token = _token(
            client, store, admin=True, scopes=("auth:session", "memory:read")
        )
        with TestClient(client.app, raise_server_exceptions=False) as no_raise:
            response = no_raise.get(
                "/admin/memory/search",
                params={"query": "continuity", "enable_association": selected},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert "private-storage-detail" not in response.text
        assert "items" not in response.json()
        assert (
            calls
            == ["direct", "expand", "bundles"][
                : ["direct", "expand", "bundles"].index(phase) + 1
            ]
        )
    finally:
        store.close()


@pytest.mark.parametrize("selected", ["false", "true"])
def test_memory_provider_access_denial_is_not_replaced_by_empty_results(
    tmp_path: Path, selected: str
) -> None:
    async def search(*args: Any, **kwargs: Any) -> list[Any]:
        raise APIError("scope_required", "资源访问被拒绝。", status_code=403)

    client, store = _app(
        tmp_path, providers=P312Providers(memory=SimpleNamespace(search=search))
    )
    try:
        token = _token(
            client, store, admin=True, scopes=("auth:session", "memory:read")
        )
        response = client.get(
            "/admin/memory/search",
            params={"query": "continuity", "enable_association": selected},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "scope_required"
        assert "items" not in response.json()
    finally:
        store.close()
