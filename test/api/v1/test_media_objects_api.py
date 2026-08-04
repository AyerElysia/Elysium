"""P3-07 managed-media lifecycle and security contracts."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from src.app.api.v1.media_objects import (
    ManagedMediaService,
    MediaObjectFailure,
    MediaObjectStore,
)

from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.policy import USER_FRONTEND_AUDIENCE
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.tokens import SignedValueCodec

ORIGIN = "http://localhost:5173"
PNG = b"\x89PNG\r\n\x1a\n" + b"safe-png-payload"


@pytest.fixture
async def media_context(tmp_path):
    auth_store = AuthStore(installation_id="installation-media")
    media_store = MediaObjectStore(
        tmp_path / "api.sqlite3",
        tmp_path / "runtime" / "media",
    )

    async def recognize(data: bytes, kind: str, use_cache: bool) -> str:
        assert data == PNG
        assert kind == "image"
        assert use_cache is True
        return "一张测试图片"

    context = APIContext(
        store=auth_store,
        codec=SignedValueCodec("x" * 48),
        installation_id="installation-media",
        allowed_origins=(ORIGIN,),
        media=ManagedMediaService(media_store, recognizer=recognize),
    )
    yield context, media_store
    media_store.close()
    auth_store.close()


def _token(
    context: APIContext,
    *,
    actor_id: str,
    scopes: tuple[str, ...],
    grants: tuple[str, ...] = (),
) -> str:
    challenge = context.store.create_bootstrap_challenge(
        codec=context.codec,
        audience=USER_FRONTEND_AUDIENCE,
        origin=ORIGIN,
        actor_id=actor_id,
        scopes=("auth:session", *scopes),
        resource_grants=grants,
    )
    response = TestClient(create_api_app(context)).post(
        "/auth/sessions",
        headers={"Origin": ORIGIN},
        json={
            "grant_type": "bootstrap_challenge",
            "audience": USER_FRONTEND_AUDIENCE,
            "bootstrap_challenge": challenge,
            "origin": ORIGIN,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _upload_body(*, data: bytes = PNG, grant: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "kind": "image",
        "mime_type": "image/png",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "file_name": "test.png",
        "resource_grant": grant,
    }


async def _upload(client: AsyncClient, headers: dict[str, str], body: dict | None = None) -> dict:
    declared = body or _upload_body()
    created = await client.post("/media/uploads", headers=headers, json=declared)
    assert created.status_code == 200, created.text
    upload_id = created.json()["upload_id"]
    uploaded = await client.put(
        f"/media/uploads/{upload_id}",
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=PNG,
    )
    assert uploaded.status_code == 204, uploaded.text
    completed = await client.post(
        f"/media/uploads/{upload_id}:complete",
        headers=headers,
        json={},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


@pytest.mark.asyncio
async def test_lifecycle_download_range_save_recognize_and_no_path_leak(media_context) -> None:
    context, _store = media_context
    token = _token(
        context,
        actor_id="owner",
        scopes=("media:read", "media:write", "media:recognize"),
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=create_api_app(context)),
        base_url="http://test",
    ) as client:
        descriptor = await _upload(client, headers)
        media_id = descriptor["media_id"]
        assert "path" not in str(descriptor).lower()
        assert "base64" not in str(descriptor).lower()

        whole = await client.get(f"/media/{media_id}/content", headers=headers)
        partial = await client.get(
            f"/media/{media_id}/content",
            headers={**headers, "Range": "bytes=0-7"},
        )
        first_save = await client.post(f"/media/{media_id}:save", headers=headers, json={})
        second_save = await client.post(f"/media/{media_id}:save", headers=headers, json={})
        recognized = await client.post(
            f"/media/{media_id}:recognize",
            headers=headers,
            json={"schema_version": 1, "use_cache": True},
        )
        derivatives = await client.get(f"/media/{media_id}/derivatives", headers=headers)

    assert whole.content == PNG
    assert whole.headers["etag"] == f'"sha256:{descriptor["sha256"]}"'
    assert whole.headers["cache-control"] == "private, no-store"
    assert partial.status_code == 206
    assert partial.content == PNG[:8]
    assert partial.headers["content-range"] == f"bytes 0-7/{len(PNG)}"
    assert first_save.json()["saved"] is True
    assert second_save.json()["saved"] is False
    outbox = _store._connection.execute(
        """SELECT event_type, payload_json, state, visibility, origin_sequence
        FROM sync_outbox WHERE event_type LIKE 'media.%' ORDER BY created_at"""
    ).fetchall()
    assert [row["event_type"] for row in outbox] == [
        "media.upload.completed",
        "media.object.saved",
    ]
    assert {row["state"] for row in outbox} == {"held"}
    assert {row["visibility"] for row in outbox} == {"private"}
    assert all(row["origin_sequence"] is None for row in outbox)
    assert all("path" not in row["payload_json"] for row in outbox)
    assert all("base64" not in row["payload_json"] for row in outbox)
    assert recognized.json()["text"] == "一张测试图片"
    assert derivatives.json()["items"][0]["kind"] == "recognition"


@pytest.mark.asyncio
async def test_upload_stream_enforces_hard_limit_without_content_length(
    media_context,
    monkeypatch,
) -> None:
    context, _store = media_context
    token = _token(context, actor_id="owner", scopes=("media:write",))
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=create_api_app(context)),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/media/uploads",
            headers=headers,
            json=_upload_body(),
        )
        upload_id = created.json()["upload_id"]
        monkeypatch.setattr(
            "src.app.api.v1.media_objects.MAX_UPLOAD_BYTES",
            len(PNG) - 1,
        )
        response = await client.put(
            f"/media/uploads/{upload_id}",
            headers={**headers, "Content-Type": "application/octet-stream"},
            content=PNG,
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


@pytest.mark.asyncio
async def test_hash_mime_and_size_mismatch_are_rejected(media_context) -> None:
    context, _store = media_context
    token = _token(context, actor_id="owner", scopes=("media:write",))
    headers = {"Authorization": f"Bearer {token}"}
    app = create_api_app(context)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        wrong_size = _upload_body()
        wrong_size["size_bytes"] += 1
        created = await client.post("/media/uploads", headers=headers, json=wrong_size)
        put = await client.put(
            f"/media/uploads/{created.json()['upload_id']}",
            headers={**headers, "Content-Type": "application/octet-stream"},
            content=PNG,
        )
        assert put.status_code == 422
        assert put.json()["error"]["code"] == "media_size_mismatch"

        wrong_hash = _upload_body()
        wrong_hash["sha256"] = "0" * 64
        created = await client.post("/media/uploads", headers=headers, json=wrong_hash)
        upload_id = created.json()["upload_id"]
        assert (await client.put(f"/media/uploads/{upload_id}", headers={**headers, "Content-Type": "application/octet-stream"}, content=PNG)).status_code == 204
        complete = await client.post(f"/media/uploads/{upload_id}:complete", headers=headers, json={})
        assert complete.status_code == 422
        assert complete.json()["error"]["code"] == "media_hash_mismatch"

        wrong_mime = _upload_body()
        wrong_mime["mime_type"] = "image/jpeg"
        created = await client.post("/media/uploads", headers=headers, json=wrong_mime)
        upload_id = created.json()["upload_id"]
        assert (await client.put(f"/media/uploads/{upload_id}", headers={**headers, "Content-Type": "application/octet-stream"}, content=PNG)).status_code == 204
        complete = await client.post(f"/media/uploads/{upload_id}:complete", headers=headers, json={})
        assert complete.status_code == 422
        assert complete.json()["error"]["code"] == "media_validation_failed"


@pytest.mark.asyncio
async def test_private_object_is_hidden_but_resource_grant_allows_access(media_context) -> None:
    context, _store = media_context
    owner = _token(
        context,
        actor_id="owner",
        scopes=("media:read", "media:write"),
        grants=("stream:shared",),
    )
    stranger = _token(context, actor_id="stranger", scopes=("media:read",))
    granted = _token(
        context,
        actor_id="reader",
        scopes=("media:read",),
        grants=("stream:shared",),
    )
    async with AsyncClient(transport=ASGITransport(app=create_api_app(context)), base_url="http://test") as client:
        descriptor = await _upload(
            client,
            {"Authorization": f"Bearer {owner}"},
            _upload_body(grant="stream:shared"),
        )
        path = f"/media/{descriptor['media_id']}"
        hidden = await client.get(path, headers={"Authorization": f"Bearer {stranger}"})
        visible = await client.get(path, headers={"Authorization": f"Bearer {granted}"})
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "media_not_found"
    assert visible.status_code == 200


@pytest.mark.asyncio
async def test_resolver_revalidates_integrity_and_expected_type(media_context) -> None:
    context, store = media_context
    token = _token(context, actor_id="owner", scopes=("media:read", "media:write"))
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=create_api_app(context)), base_url="http://test") as client:
        descriptor = await _upload(client, headers)
    media_id = descriptor["media_id"]
    attachment = await context.media.resolve_ready(media_id, actor_id="owner", expected_type="image")
    assert attachment.resource_id == media_id
    assert attachment.media_ref.data == PNG
    with pytest.raises(MediaObjectFailure, match="media_type_mismatch"):
        await context.media.resolve_ready(media_id, actor_id="owner", expected_type="voice")

    row = store._connection.execute(
        "SELECT storage_key FROM api_media_objects WHERE media_id = ?", (media_id,)
    ).fetchone()
    (store.storage_root / row["storage_key"]).write_bytes(b"tampered")
    with pytest.raises(MediaObjectFailure, match="media_integrity_failed"):
        await context.media.resolve_ready(media_id, actor_id="owner", expected_type="image")


def test_saved_descriptor_survives_store_restart(tmp_path) -> None:
    database = tmp_path / "api.sqlite3"
    media_root = tmp_path / "runtime" / "media"
    store = MediaObjectStore(database, media_root)
    request = _upload_body()
    from src.app.api.v1.schemas.media import MediaUploadCreateRequest

    upload = store.create_upload(MediaUploadCreateRequest(**request), actor_id="owner")
    store.put_upload(upload.upload_id, PNG, actor_id="owner")
    descriptor = store.complete_upload(upload.upload_id, actor_id="owner")
    saved, changed = store.save(descriptor.media_id, actor_id="owner", grants=())
    assert saved.state == "saved" and changed is True
    store.close()

    reopened = MediaObjectStore(database, media_root)
    assert reopened.get_descriptor(descriptor.media_id, actor_id="owner", grants=()).state == "saved"
    reopened.close()


def test_cleanup_candidates_are_read_only_and_owned(tmp_path) -> None:
    store = MediaObjectStore(tmp_path / "api.sqlite3", tmp_path / "runtime" / "media")
    from src.app.api.v1.schemas.media import MediaUploadCreateRequest

    upload = store.create_upload(MediaUploadCreateRequest(**_upload_body()), actor_id="owner")
    store.put_upload(upload.upload_id, PNG, actor_id="owner")
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    store._connection.execute(
        "UPDATE api_media_uploads SET expires_at = ? WHERE upload_id = ?",
        (old, upload.upload_id),
    )
    unknown = store.temporary_root / "unknown.part"
    unknown.write_bytes(b"do not touch")
    assert store.cleanup_candidates() == (upload.upload_id,)
    assert unknown.read_bytes() == b"do not touch"
    store.close()


@pytest.mark.asyncio
async def test_upload_rejects_resource_grant_not_held_by_session(media_context) -> None:
    context, _store = media_context
    token = _token(context, actor_id="owner", scopes=("media:write",))
    async with AsyncClient(
        transport=ASGITransport(app=create_api_app(context)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/media/uploads",
            headers={"Authorization": f"Bearer {token}"},
            json=_upload_body(grant="stream:forbidden"),
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "resource_grant_forbidden"


@pytest.mark.asyncio
async def test_invalid_range_is_416_and_does_not_leak_content(media_context) -> None:
    context, _store = media_context
    token = _token(
        context,
        actor_id="owner",
        scopes=("media:read", "media:write"),
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=create_api_app(context)),
        base_url="http://test",
    ) as client:
        descriptor = await _upload(client, headers)
        response = await client.get(
            f"/media/{descriptor['media_id']}/content",
            headers={**headers, "Range": f"bytes={len(PNG)}-"},
        )
    assert response.status_code == 416
    assert response.json()["error"]["code"] == "range_not_satisfiable"
    assert PNG not in response.content


@pytest.mark.asyncio
async def test_recognition_failure_is_persisted_without_provider_detail(
    media_context,
) -> None:
    context, store = media_context

    async def fail_recognition(_data: bytes, _kind: str, _use_cache: bool) -> str:
        raise RuntimeError("provider secret detail")

    context.media.recognizer = fail_recognition
    token = _token(
        context,
        actor_id="owner",
        scopes=("media:read", "media:write", "media:recognize"),
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=create_api_app(context)),
        base_url="http://test",
    ) as client:
        descriptor = await _upload(client, headers)
        response = await client.post(
            f"/media/{descriptor['media_id']}:recognize",
            headers=headers,
            json={"schema_version": 1, "use_cache": False},
        )
        derivatives = await client.get(
            f"/media/{descriptor['media_id']}/derivatives",
            headers=headers,
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "media_recognition_failed"
    assert "provider secret detail" not in response.text
    assert derivatives.json()["items"][0]["state"] == "failed"
    row = store._connection.execute(
        "SELECT recognition_state, recognition_text FROM api_media_objects WHERE media_id = ?",
        (descriptor["media_id"],),
    ).fetchone()
    assert row["recognition_state"] == "failed"
    assert row["recognition_text"] is None


@pytest.mark.asyncio
async def test_store_serializes_threaded_reads_and_close_is_idempotent(tmp_path) -> None:
    store = MediaObjectStore(
        tmp_path / "api.sqlite3",
        tmp_path / "runtime" / "media",
    )
    from src.app.api.v1.schemas.media import MediaUploadCreateRequest

    upload = store.create_upload(
        MediaUploadCreateRequest(**_upload_body()),
        actor_id="owner",
    )
    store.put_upload(upload.upload_id, PNG, actor_id="owner")
    descriptor = store.complete_upload(upload.upload_id, actor_id="owner")
    results = await asyncio.gather(
        *(
            asyncio.to_thread(
                store.get_content,
                descriptor.media_id,
                actor_id="owner",
                grants=(),
            )
            for _ in range(12)
        )
    )
    assert all(result.data == PNG for result in results)
    store.close()
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.get_descriptor(descriptor.media_id, actor_id="owner", grants=())


def test_openapi_exports_only_media_id_interfaces(media_context) -> None:
    context, _store = media_context
    schema = create_api_app(context).openapi()
    assert schema["paths"]["/media/uploads"]["post"]["operationId"] == "createMediaUpload"
    request_schema = schema["components"]["schemas"]["MediaUploadCreateRequest"]
    properties = request_schema["properties"]
    assert "path" not in properties
    assert "url" not in properties
    assert "base64" not in properties
