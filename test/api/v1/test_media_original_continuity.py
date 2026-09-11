"""G02 binary-original contracts using temporary stores and signature fixtures.

These opaque samples test storage/signature handling, not codec validity,
recognition quality, subject authorship, or a unified subject-document namespace.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.app.api.v1.media_objects import (
    ManagedMediaService,
    MediaObjectFailure,
    MediaObjectStore,
)
from src.app.api.v1.schemas.media import (
    MediaObjectDescriptor,
    MediaUploadCreateRequest,
    MediaUploadSession,
)

_BODY = b"synthetic-only\x00\xff\xfe\x80\r\n"
_BMFF = b"\x00\x00\x00\x18ftypisom"
_EBML = b"\x1a\x45\xdf\xa3"
_FORMATS = [
    pytest.param("image", "image/png", b"\x89PNG\r\n\x1a\n" + _BODY, id="png"),
    pytest.param("image", "image/jpeg", b"\xff\xd8\xff" + _BODY, id="jpeg"),
    pytest.param("image", "image/gif", b"GIF89a" + _BODY, id="gif"),
    pytest.param("image", "image/bmp", b"BM" + _BODY, id="bmp"),
    pytest.param("image", "image/webp", b"RIFF\x00\x00\x00\x00WEBP" + _BODY, id="webp"),
    pytest.param("image", "image/tiff", b"II*\x00" + _BODY, id="tiff"),
    pytest.param("image", "image/svg+xml", b"<svg>synthetic</svg>", id="svg"),
    pytest.param("audio", "audio/wav", b"RIFF\x00\x00\x00\x00WAVE" + _BODY, id="wav"),
    pytest.param("audio", "audio/amr", b"#!AMR\n" + _BODY, id="amr"),
    pytest.param("audio", "audio/silk", b"#!SILK_V3" + _BODY, id="silk"),
    pytest.param("audio", "audio/mpeg", b"ID3" + _BODY, id="mp3"),
    pytest.param("audio", "audio/aac", b"\xff\xf1" + _BODY, id="aac"),
    pytest.param("audio", "audio/ogg", b"OggS" + _BODY, id="ogg"),
    pytest.param("audio", "audio/flac", b"fLaC" + _BODY, id="flac"),
    pytest.param("audio", "audio/mp4", _BMFF + _BODY, id="audio-mp4"),
    pytest.param("video", "video/mp4", _BMFF + _BODY, id="video-mp4"),
    pytest.param(
        "video", "video/x-msvideo", b"RIFF\x00\x00\x00\x00AVI " + _BODY, id="avi"
    ),
    pytest.param("audio", "audio/webm", _EBML + b"webm" + _BODY, id="audio-webm"),
    pytest.param("video", "video/webm", _EBML + b"webm" + _BODY, id="video-webm"),
    pytest.param("audio", "audio/x-matroska", _EBML + _BODY, id="audio-matroska"),
    pytest.param("video", "video/x-matroska", _EBML + _BODY, id="video-matroska"),
    pytest.param("file", "application/octet-stream", _BODY, id="opaque-binary"),
]


def _upload(
    store: MediaObjectStore,
    kind: str,
    mime: str,
    data: bytes,
    *,
    actor: str = "synthetic-owner",
) -> tuple[MediaUploadSession, MediaObjectDescriptor]:
    request = MediaUploadCreateRequest.model_validate(
        {
            "schema_version": 1,
            "kind": kind,
            "mime_type": mime,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "file_name": "not-a-format-hint.bin",
        }
    )
    upload = store.create_upload(request, actor_id=actor)
    store.put_upload(upload.upload_id, data, actor_id=actor)
    return upload, store.complete_upload(upload.upload_id, actor_id=actor)


@pytest.mark.parametrize(("kind", "mime", "data"), _FORMATS)
def test_original_bytes_identity_and_save_survive_reopen(
    tmp_path: Path,
    kind: str,
    mime: str,
    data: bytes,
) -> None:
    database = tmp_path / "api.sqlite3"
    media_root = tmp_path / "runtime" / "media"
    actor = "synthetic-owner"
    store = MediaObjectStore(database, media_root)
    try:
        upload, descriptor = _upload(store, kind, mime, data)
        saved, changed = store.save(descriptor.media_id, actor_id=actor, grants=())
        assert changed is True and saved.state == "saved"
        assert (
            store.get_content(descriptor.media_id, actor_id=actor, grants=()).data
            == data
        )
        assert saved.kind == kind and saved.mime_type == mime
        assert saved.sha256 == hashlib.sha256(data).hexdigest()
        assert saved.size_bytes == len(data)
        assert not {"path", "storage_key", "data", "base64"}.intersection(
            saved.model_dump()
        )
        store.set_recognition(
            descriptor.media_id,
            actor_id=actor,
            grants=(),
            text="synthetic external observation; not the original bytes",
        )
        assert (
            store.get_content(descriptor.media_id, actor_id=actor, grants=()).data
            == data
        )
    finally:
        store.close()

    reopened = MediaObjectStore(database, media_root)
    try:
        content = reopened.get_content(descriptor.media_id, actor_id=actor, grants=())
        assert content.data == data
        assert content.descriptor.sha256 == saved.sha256
        assert content.descriptor.size_bytes == len(data)
        assert content.descriptor.state == "saved"
        assert (
            reopened.complete_upload(upload.upload_id, actor_id=actor).media_id
            == descriptor.media_id
        )
        again, changed = reopened.save(descriptor.media_id, actor_id=actor, grants=())
        assert changed is False and again.media_id == descriptor.media_id
        derived = reopened.derivatives(descriptor.media_id, actor_id=actor, grants=())
        assert len(derived.items) == 1
        assert derived.items[0].derivative_id == f"recognition:{descriptor.media_id}"
        with pytest.raises(MediaObjectFailure, match="media_not_found"):
            reopened.get_content(
                descriptor.media_id, actor_id="unrelated-actor", grants=()
            )

        # Same-length damage isolates the hash check from the length check.
        blob = reopened.object_root / saved.sha256[:2] / saved.sha256
        blob.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        with pytest.raises(MediaObjectFailure, match="media_integrity_failed"):
            reopened.get_content(descriptor.media_id, actor_id=actor, grants=())
        with pytest.raises(MediaObjectFailure, match="media_integrity_failed"):
            reopened.save(descriptor.media_id, actor_id=actor, grants=())
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("kind", "mime", "data", "expected"),
    [
        ("image", "image/png", b"\x89PNG\r\n\x1a\n" + _BODY, "emoji"),
        ("audio", "audio/wav", b"RIFF\x00\x00\x00\x00WAVE" + _BODY, "voice"),
        ("video", "video/mp4", _BMFF + _BODY, "video"),
        ("file", "application/octet-stream", _BODY, "file"),
    ],
)
async def test_resolver_keeps_exact_original_and_kind_boundary(
    tmp_path: Path,
    kind: str,
    mime: str,
    data: bytes,
    expected: str,
) -> None:
    store = MediaObjectStore(tmp_path / "api.sqlite3", tmp_path / "runtime" / "media")
    try:
        _, descriptor = _upload(store, kind, mime, data)
        service = ManagedMediaService(store)
        attachment = await service.resolve_ready(
            descriptor.media_id,
            actor_id="synthetic-owner",
            expected_type=expected,
        )
        assert attachment.resource_id == descriptor.media_id
        assert attachment.media_ref is not None and attachment.media_ref.data == data
        assert attachment.media_ref.sha256 == descriptor.sha256
        other_kind = "audio" if kind != "audio" else "image"
        with pytest.raises(MediaObjectFailure, match="media_type_mismatch"):
            await service.resolve_ready(
                descriptor.media_id,
                actor_id="synthetic-owner",
                expected_type=other_kind,
            )
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["complete", "save"])
def test_failed_event_commit_retries_without_duplicate_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store = MediaObjectStore(tmp_path / "api.sqlite3", tmp_path / "runtime" / "media")
    actor = "synthetic-owner"
    request = MediaUploadCreateRequest(
        kind="file",
        mime_type="application/octet-stream",
        size_bytes=len(_BODY),
        sha256=hashlib.sha256(_BODY).hexdigest(),
    )
    try:
        upload = store.create_upload(request, actor_id=actor)
        store.put_upload(upload.upload_id, _BODY, actor_id=actor)
        if operation == "save":
            descriptor = store.complete_upload(upload.upload_id, actor_id=actor)
        original_append = store._append_event

        def failed_append(**_kwargs: object) -> None:
            raise RuntimeError("synthetic event failure")

        monkeypatch.setattr(store, "_append_event", failed_append)
        with pytest.raises(RuntimeError, match="synthetic event failure"):
            if operation == "complete":
                store.complete_upload(upload.upload_id, actor_id=actor)
            else:
                store.save(descriptor.media_id, actor_id=actor, grants=())
        count = store._connection.execute(
            "SELECT COUNT(*) FROM api_media_objects"
        ).fetchone()[0]
        assert count == (0 if operation == "complete" else 1)
        if operation == "save":
            assert (
                store.get_descriptor(
                    descriptor.media_id, actor_id=actor, grants=()
                ).state
                == "ready"
            )
        monkeypatch.setattr(store, "_append_event", original_append)
        descriptor = store.complete_upload(upload.upload_id, actor_id=actor)
        saved, changed = store.save(descriptor.media_id, actor_id=actor, grants=())
        assert changed is True and saved.state == "saved"
        assert (
            store.get_content(descriptor.media_id, actor_id=actor, grants=()).data
            == _BODY
        )
        assert (
            store.complete_upload(upload.upload_id, actor_id=actor).media_id
            == descriptor.media_id
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM api_media_objects"
            ).fetchone()[0]
            == 1
        )
        assert store.save(descriptor.media_id, actor_id=actor, grants=())[1] is False
    finally:
        store.close()
