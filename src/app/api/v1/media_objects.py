"""Durable managed-media storage, resolver, and public HTTP routes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response

from src.core.models.media import MediaAttachment, MediaSegmentType
from src.kernel.llm.exceptions import MediaValidationError
from src.kernel.llm.payload.media import MediaKind, MediaRef
from src.kernel.sync.local_store import (
    create_local_sync_schema,
    enqueue_in_transaction,
)

from .auth_store import SessionRecord
from .media_contracts import ManagedMediaFailure
from .runtime import ERROR_RESPONSES, MAX_UPLOAD_BYTES, APIError
from .schemas.media import (
    MediaDerivative,
    MediaDerivativeList,
    MediaObjectDescriptor,
    MediaRecognition,
    MediaRecognizeRequest,
    MediaSaveResponse,
    MediaUploadCreateRequest,
    MediaUploadSession,
)

UPLOAD_TTL = timedelta(hours=1)
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _serialized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """Serialize one store operation across asyncio worker threads."""

    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        store = args[0]
        with store._lock:
            if store._closed:
                raise RuntimeError("managed media store is closed")
            return method(*args, **kwargs)

    return wrapped
_KIND_SEGMENT = {
    "image": MediaSegmentType.IMAGE,
    "emoji": MediaSegmentType.EMOJI,
    "voice": MediaSegmentType.VOICE,
    "audio": MediaSegmentType.VOICE,
    "video": MediaSegmentType.VIDEO,
    "file": MediaSegmentType.FILE,
}
_EXPECTED_KIND = {
    "image": "image",
    "emoji": "image",
    "voice": "audio",
    "audio": "audio",
    "video": "video",
    "file": "file",
}


class MediaObjectFailure(ManagedMediaFailure):
    """A stable managed-media domain failure."""


@dataclass(frozen=True, slots=True)
class MediaContent:
    """Authorized media bytes with safe public metadata."""

    descriptor: MediaObjectDescriptor
    data: bytes


class MediaObjectStore:
    """Own SQLite metadata and files beneath one explicit runtime root."""

    def __init__(self, database_path: Path, storage_root: Path) -> None:
        self.database_path = database_path.resolve()
        self.storage_root = storage_root.resolve()
        runtime_root = self.storage_root.parent.resolve()
        if self.storage_root.name != "media" or not self.storage_root.is_relative_to(runtime_root):
            raise ValueError("managed media root must be runtime/media")
        self.temporary_root = self.storage_root / "uploads"
        self.object_root = self.storage_root / "objects"
        self.temporary_root.mkdir(parents=True, exist_ok=True)
        self.object_root.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA busy_timeout = 30000")
            create_local_sync_schema(self._connection)
            self._connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS api_media_uploads (
                upload_id TEXT PRIMARY KEY,
                owner_actor_id TEXT NOT NULL,
                resource_grant TEXT,
                state TEXT NOT NULL CHECK (
                    state IN ('created', 'uploaded', 'completed', 'failed', 'expired')
                ),
                kind TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                file_name TEXT,
                storage_key TEXT NOT NULL UNIQUE,
                media_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_media_objects (
                media_id TEXT PRIMARY KEY,
                owner_actor_id TEXT NOT NULL,
                resource_grant TEXT,
                state TEXT NOT NULL CHECK (state IN ('ready', 'saved', 'quarantined')),
                kind TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                file_name TEXT,
                storage_key TEXT NOT NULL,
                recognition_state TEXT NOT NULL DEFAULT 'not_requested',
                recognition_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_api_media_upload_expiry
                ON api_media_uploads(state, expires_at);
                """
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    @_serialized
    def create_upload(
        self,
        request: MediaUploadCreateRequest,
        *,
        actor_id: str,
        grants: tuple[str, ...] = (),
        administrator: bool = False,
    ) -> MediaUploadSession:
        if request.resource_grant and not administrator and not self._grant_matches(
            request.resource_grant,
            grants,
        ):
            raise MediaObjectFailure("resource_grant_forbidden", status_code=403)
        now = self._now()
        upload_id = f"upload_{secrets.token_urlsafe(18)}"
        storage_key = f"uploads/{upload_id}.part"
        expires_at = now + UPLOAD_TTL
        self._connection.execute(
            """
            INSERT INTO api_media_uploads (
                upload_id, owner_actor_id, resource_grant, state, kind, mime_type,
                size_bytes, sha256, file_name, storage_key, created_at, updated_at,
                expires_at
            ) VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                upload_id,
                actor_id,
                request.resource_grant,
                request.kind,
                request.mime_type.strip().lower(),
                request.size_bytes,
                request.sha256,
                request.file_name,
                storage_key,
                self._iso(now),
                self._iso(now),
                self._iso(expires_at),
            ),
        )
        return MediaUploadSession(
            upload_id=upload_id,
            state="created",
            kind=request.kind,
            mime_type=request.mime_type.strip().lower(),
            size_bytes=request.size_bytes,
            sha256=request.sha256,
            expires_at=expires_at,
        )

    @_serialized
    def put_upload(self, upload_id: str, data: bytes, *, actor_id: str) -> None:
        row = self._upload_row(upload_id, actor_id=actor_id)
        if row["state"] not in {"created", "uploaded"}:
            raise MediaObjectFailure("upload_state_conflict", status_code=409)
        if self._expired(row):
            self._connection.execute(
                "UPDATE api_media_uploads SET state = 'expired', updated_at = ? WHERE upload_id = ?",
                (self._iso(self._now()), upload_id),
            )
            raise MediaObjectFailure("upload_not_found", status_code=404)
        if len(data) != row["size_bytes"]:
            raise MediaObjectFailure("media_size_mismatch", status_code=422)
        path = self._owned_path(row["storage_key"], temporary=True)
        staged = path.with_suffix(".staging")
        try:
            staged.write_bytes(data)
            os.replace(staged, path)
        finally:
            if staged.exists():
                staged.unlink()
        self._connection.execute(
            "UPDATE api_media_uploads SET state = 'uploaded', updated_at = ? WHERE upload_id = ?",
            (self._iso(self._now()), upload_id),
        )

    @_serialized
    def complete_upload(self, upload_id: str, *, actor_id: str) -> MediaObjectDescriptor:
        row = self._upload_row(upload_id, actor_id=actor_id)
        if row["state"] == "completed" and row["media_id"]:
            return self.get_descriptor(row["media_id"], actor_id=actor_id, grants=())
        if row["state"] != "uploaded" or self._expired(row):
            raise MediaObjectFailure("upload_state_conflict", status_code=409)
        temporary_path = self._owned_path(row["storage_key"], temporary=True)
        try:
            data = temporary_path.read_bytes()
            if len(data) != row["size_bytes"]:
                raise MediaObjectFailure("media_size_mismatch", status_code=422)
            digest = hashlib.sha256(data).hexdigest()
            if digest != row["sha256"]:
                raise MediaObjectFailure("media_hash_mismatch", status_code=422)
            ref = MediaRef.from_bytes(
                data,
                kind=MediaKind(row["kind"]),
                mime_type=row["mime_type"],
                max_item_bytes=32 * 1024 * 1024,
                origin="managed_upload",
                persistence_policy="managed",
            )
        except MediaObjectFailure:
            self._mark_upload_failed(upload_id)
            raise
        except (OSError, MediaValidationError, ValueError) as exc:
            self._mark_upload_failed(upload_id)
            raise MediaObjectFailure("media_validation_failed", status_code=422) from exc

        media_id = f"media_{secrets.token_urlsafe(18)}"
        object_key = f"objects/{ref.sha256[:2]}/{ref.sha256}"
        object_path = self._owned_path(object_key, temporary=False)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        now = self._now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT * FROM api_media_objects WHERE media_id = ?",
                (media_id,),
            ).fetchone()
            if existing is not None and (
                existing["sha256"] != ref.sha256
                or existing["size_bytes"] != ref.size_bytes
                or existing["mime_type"] != ref.mime_type
            ):
                raise MediaObjectFailure("media_identity_conflict", status_code=409)
            if not object_path.exists():
                staged_object = object_path.with_suffix(".staging")
                try:
                    staged_object.write_bytes(data)
                    os.replace(staged_object, object_path)
                finally:
                    if staged_object.exists():
                        staged_object.unlink()
            elif hashlib.sha256(object_path.read_bytes()).hexdigest() != ref.sha256:
                raise MediaObjectFailure("media_integrity_failed", status_code=409)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO api_media_objects (
                    media_id, owner_actor_id, resource_grant, state, kind, mime_type,
                    size_bytes, sha256, file_name, storage_key, created_at, updated_at
                ) VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    media_id,
                    actor_id,
                    row["resource_grant"],
                    row["kind"],
                    row["mime_type"],
                    row["size_bytes"],
                    row["sha256"],
                    row["file_name"],
                    object_key,
                    self._iso(now),
                    self._iso(now),
                ),
            )
            self._connection.execute(
                "UPDATE api_media_uploads SET state = 'completed', media_id = ?, updated_at = ? WHERE upload_id = ?",
                (media_id, self._iso(now), upload_id),
            )
            self._append_event(
                event_type="media.upload.completed",
                actor_id=actor_id,
                media_id=media_id,
                upload_id=upload_id,
                descriptor={
                    "media_id": media_id,
                    "kind": row["kind"],
                    "mime_type": row["mime_type"],
                    "size_bytes": row["size_bytes"],
                    "sha256": row["sha256"],
                    "state": "ready",
                },
                occurred_at=now,
            )
            self._connection.execute("COMMIT")
            if temporary_path.exists():
                temporary_path.unlink()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return self.get_descriptor(media_id, actor_id=actor_id, grants=())

    @_serialized
    def get_descriptor(
        self,
        media_id: str,
        *,
        actor_id: str,
        grants: tuple[str, ...],
    ) -> MediaObjectDescriptor:
        return self._descriptor(self._authorized_object(media_id, actor_id, grants))

    @_serialized
    def get_content(
        self,
        media_id: str,
        *,
        actor_id: str,
        grants: tuple[str, ...],
    ) -> MediaContent:
        row = self._authorized_object(media_id, actor_id, grants)
        path = self._owned_path(row["storage_key"], temporary=False)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise MediaObjectFailure("media_integrity_failed", status_code=409) from exc
        if len(data) != row["size_bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise MediaObjectFailure("media_integrity_failed", status_code=409)
        return MediaContent(descriptor=self._descriptor(row), data=data)

    @_serialized
    def save(
        self,
        media_id: str,
        *,
        actor_id: str,
        grants: tuple[str, ...],
    ) -> tuple[MediaObjectDescriptor, bool]:
        row = self._authorized_object(media_id, actor_id, grants)
        self.get_content(media_id, actor_id=actor_id, grants=grants)
        changed = row["state"] != "saved"
        if changed:
            now = self._now()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "UPDATE api_media_objects SET state = 'saved', updated_at = ? WHERE media_id = ?",
                    (self._iso(now), media_id),
                )
                self._append_event(
                    event_type="media.object.saved",
                    actor_id=actor_id,
                    media_id=media_id,
                    upload_id=None,
                    descriptor={
                        "media_id": media_id,
                        "kind": row["kind"],
                        "mime_type": row["mime_type"],
                        "size_bytes": row["size_bytes"],
                        "sha256": row["sha256"],
                        "state": "saved",
                    },
                    occurred_at=now,
                )
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        return self.get_descriptor(media_id, actor_id=actor_id, grants=grants), changed

    @_serialized
    def set_recognition(
        self,
        media_id: str,
        *,
        actor_id: str,
        grants: tuple[str, ...],
        text: str | None,
        completed: bool = True,
    ) -> MediaRecognition:
        self._authorized_object(media_id, actor_id, grants)
        state = "completed" if completed else "failed"
        now = self._now()
        self._connection.execute(
            "UPDATE api_media_objects SET recognition_state = ?, recognition_text = ?, updated_at = ? WHERE media_id = ?",
            (state, text, self._iso(now), media_id),
        )
        return MediaRecognition(media_id=media_id, state=state, text=text, updated_at=now)

    @_serialized
    def derivatives(
        self,
        media_id: str,
        *,
        actor_id: str,
        grants: tuple[str, ...],
    ) -> MediaDerivativeList:
        row = self._authorized_object(media_id, actor_id, grants)
        if row["recognition_state"] == "not_requested":
            return MediaDerivativeList(items=())
        return MediaDerivativeList(
            items=(
                MediaDerivative(
                    derivative_id=f"recognition:{media_id}",
                    kind="recognition",
                    state=row["recognition_state"],
                    text=row["recognition_text"],
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                ),
            )
        )

    @_serialized
    def verify(self, media_id: str, *, actor_id: str, grants: tuple[str, ...]) -> bool:
        self.get_content(media_id, actor_id=actor_id, grants=grants)
        return True

    @_serialized
    def cleanup_candidates(self) -> tuple[str, ...]:
        now = self._iso(self._now())
        rows = self._connection.execute(
            "SELECT upload_id, storage_key FROM api_media_uploads WHERE state != 'completed' AND expires_at <= ?",
            (now,),
        ).fetchall()
        return tuple(row["upload_id"] for row in rows if self._owned_path(row["storage_key"], temporary=True).exists())

    def _append_event(
        self,
        *,
        event_type: str,
        actor_id: str,
        media_id: str | None,
        upload_id: str | None,
        descriptor: dict[str, object],
        occurred_at: datetime,
    ) -> None:
        """Append a safe private event to the existing sync Outbox transaction."""

        if {"path", "url", "base64", "data"}.intersection(descriptor):
            raise ValueError("media event descriptor contains a forbidden source field")
        event_id = f"media_evt_{secrets.token_urlsafe(18)}"
        enqueue_in_transaction(
            self._connection,
            event_id=event_id,
            occurred_at=self._iso(occurred_at),
            recorded_at=self._iso(occurred_at),
            event_type=event_type,
            actor_id=actor_id,
            visibility="private",
            causation_id=upload_id or media_id or "",
            payload={
                "media_id": media_id,
                "upload_id": upload_id,
                "descriptor": descriptor,
            },
            export_requested=True,
        )

    def _upload_row(self, upload_id: str, *, actor_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM api_media_uploads WHERE upload_id = ? AND owner_actor_id = ?",
            (upload_id, actor_id),
        ).fetchone()
        if row is None:
            raise MediaObjectFailure("upload_not_found", status_code=404)
        return row

    def _authorized_object(
        self,
        media_id: str,
        actor_id: str,
        grants: tuple[str, ...],
    ) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM api_media_objects WHERE media_id = ?",
            (media_id,),
        ).fetchone()
        allowed = row is not None and (
            row["owner_actor_id"] == actor_id
            or (
                row["resource_grant"]
                and self._grant_matches(str(row["resource_grant"]), grants)
            )
        )
        if not allowed:
            raise MediaObjectFailure("media_not_found", status_code=404)
        return row

    @staticmethod
    def _grant_matches(required: str, grants: tuple[str, ...]) -> bool:
        values = set(grants)
        if required in values or "*" in values:
            return True
        namespace = required.split(":", 1)[0]
        return f"{namespace}:*" in values

    def _owned_path(self, storage_key: str, *, temporary: bool) -> Path:
        expected_prefix = "uploads/" if temporary else "objects/"
        if not storage_key.startswith(expected_prefix):
            raise MediaObjectFailure("media_integrity_failed", status_code=409)
        path = (self.storage_root / storage_key).resolve()
        expected_root = self.temporary_root if temporary else self.object_root
        if not path.is_relative_to(expected_root.resolve()):
            raise MediaObjectFailure("media_integrity_failed", status_code=409)
        return path

    @staticmethod
    def _descriptor(row: sqlite3.Row) -> MediaObjectDescriptor:
        return MediaObjectDescriptor(
            media_id=row["media_id"],
            state=row["state"],
            kind=row["kind"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            file_name=row["file_name"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            recognition_state=row["recognition_state"],
        )

    @staticmethod
    def _expired(row: sqlite3.Row) -> bool:
        return datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC)

    def _mark_upload_failed(self, upload_id: str) -> None:
        self._connection.execute(
            "UPDATE api_media_uploads SET state = 'failed', updated_at = ? WHERE upload_id = ?",
            (self._iso(self._now()), upload_id),
        )


class ManagedMediaService:
    """Async facade and P3-06-compatible ready-media resolver."""

    def __init__(
        self,
        store: MediaObjectStore,
        *,
        recognizer: Callable[[bytes, str, bool], Awaitable[str | None]] | None = None,
    ) -> None:
        self.store = store
        self.recognizer = recognizer

    async def resolve_ready(
        self,
        media_id: str,
        *,
        actor_id: str,
        expected_type: str,
        resource_grants: tuple[str, ...] = (),
    ) -> MediaAttachment:
        expected_kind = _EXPECTED_KIND.get(expected_type)
        segment = _KIND_SEGMENT.get(expected_type)
        if expected_kind is None or segment is None:
            raise MediaObjectFailure("media_type_mismatch", status_code=422)
        content = await asyncio.to_thread(
            self.store.get_content,
            media_id,
            actor_id=actor_id,
            grants=resource_grants,
        )
        descriptor = content.descriptor
        if descriptor.state not in {"ready", "saved"} or descriptor.kind != expected_kind:
            raise MediaObjectFailure("media_type_mismatch", status_code=422)
        ref = MediaRef.from_bytes(
            content.data,
            kind=MediaKind(descriptor.kind),
            mime_type=descriptor.mime_type,
            max_item_bytes=32 * 1024 * 1024,
            origin="managed_object",
            persistence_policy="managed",
        )
        return MediaAttachment(
            segment_type=segment,
            media_ref=ref,
            filename=descriptor.file_name,
            resource_id=media_id,
        )

    async def recognize(
        self,
        media_id: str,
        *,
        session: SessionRecord,
        use_cache: bool,
    ) -> MediaRecognition:
        if self.recognizer is None:
            raise MediaObjectFailure("media_recognition_unavailable", status_code=503)
        content = await asyncio.to_thread(
            self.store.get_content,
            media_id,
            actor_id=session.actor_id,
            grants=session.resource_grants,
        )
        try:
            text = await self.recognizer(
                content.data,
                content.descriptor.kind,
                use_cache,
            )
        except Exception as exc:
            await asyncio.to_thread(
                self.store.set_recognition,
                media_id,
                actor_id=session.actor_id,
                grants=session.resource_grants,
                text=None,
                completed=False,
            )
            raise MediaObjectFailure(
                "media_recognition_failed",
                status_code=502,
            ) from exc
        return await asyncio.to_thread(
            self.store.set_recognition,
            media_id,
            actor_id=session.actor_id,
            grants=session.resource_grants,
            text=text,
        )


def _api_error(exc: MediaObjectFailure) -> APIError:
    messages = {
        "upload_not_found": "上传会话不存在。",
        "media_not_found": "媒体对象不存在。",
        "media_size_mismatch": "媒体大小与声明不一致。",
        "media_hash_mismatch": "媒体哈希与声明不一致。",
        "media_validation_failed": "媒体内容与声明类型不一致。",
        "media_integrity_failed": "媒体完整性校验失败。",
        "media_type_mismatch": "媒体类型与使用位置不一致。",
        "media_recognition_unavailable": "媒体识别能力当前不可用。",
        "media_recognition_failed": "媒体识别服务执行失败。",
        "resource_grant_forbidden": "不能把媒体绑定到当前会话无权使用的资源。",
        "range_not_satisfiable": "请求的媒体字节范围无效。",
    }
    return APIError(
        exc.code,
        messages.get(exc.code, "媒体请求无法处理。"),
        status_code=exc.status_code,
        retryable=exc.status_code == 503,
    )


def create_media_router(
    *,
    service: ManagedMediaService,
    require_scope: Callable[..., Callable[[SessionRecord], SessionRecord]],
) -> APIRouter:
    """Create authorized managed-media routes."""

    router = APIRouter(prefix="/media")
    read_session = Depends(require_scope("media:read"))
    write_session = Depends(require_scope("media:write"))
    recognize_session = Depends(require_scope("media:recognize"))

    @router.post("/uploads", response_model=MediaUploadSession, operation_id="createMediaUpload", responses=ERROR_RESPONSES)
    async def create_upload(body: MediaUploadCreateRequest, session: SessionRecord = write_session) -> MediaUploadSession:
        try:
            return await asyncio.to_thread(
                service.store.create_upload,
                body,
                actor_id=session.actor_id,
                grants=session.resource_grants,
                administrator=session.role == "administrator",
            )
        except MediaObjectFailure as exc:
            raise _api_error(exc) from exc

    @router.put(
        "/uploads/{upload_id}",
        status_code=204,
        operation_id="uploadMediaContent",
        responses=ERROR_RESPONSES,
    )
    async def upload_content(
        upload_id: str,
        request: Request,
        session: SessionRecord = write_session,
    ) -> Response:
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_UPLOAD_BYTES:
                raise APIError(
                    "body_too_large",
                    "请求体超过允许上限。",
                    status_code=413,
                )
            data.extend(chunk)
        try:
            await asyncio.to_thread(
                service.store.put_upload,
                upload_id,
                bytes(data),
                actor_id=session.actor_id,
            )
        except MediaObjectFailure as exc:
            raise _api_error(exc) from exc
        return Response(status_code=204)

    @router.post("/uploads/{upload_id}:complete", response_model=MediaObjectDescriptor, operation_id="completeMediaUpload", responses=ERROR_RESPONSES)
    async def complete_upload(upload_id: str, session: SessionRecord = write_session) -> MediaObjectDescriptor:
        try:
            return await asyncio.to_thread(service.store.complete_upload, upload_id, actor_id=session.actor_id)
        except MediaObjectFailure as exc:
            raise _api_error(exc) from exc

    @router.get("/{media_id}", response_model=MediaObjectDescriptor, operation_id="getMediaObject", responses=ERROR_RESPONSES)
    async def get_media(media_id: str, session: SessionRecord = read_session) -> MediaObjectDescriptor:
        try:
            return await asyncio.to_thread(service.store.get_descriptor, media_id, actor_id=session.actor_id, grants=session.resource_grants)
        except MediaObjectFailure as exc:
            raise _api_error(exc) from exc

    @router.get("/{media_id}/content", operation_id="downloadMediaContent", responses=ERROR_RESPONSES)
    async def download(media_id: str, session: SessionRecord = read_session, range_header: str | None = Header(default=None, alias="Range")) -> Response:
        try:
            content = await asyncio.to_thread(service.store.get_content, media_id, actor_id=session.actor_id, grants=session.resource_grants)
            start, end, status = _range_bounds(range_header, len(content.data))
        except MediaObjectFailure as exc:
            raise _api_error(exc) from exc
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": f'"sha256:{content.descriptor.sha256}"',
            "Cache-Control": "private, no-store",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{len(content.data)}"
        return Response(content=content.data[start : end + 1], status_code=status, media_type=content.descriptor.mime_type, headers=headers)

    @router.post("/{media_id}:save", response_model=MediaSaveResponse, operation_id="saveMediaObject", responses=ERROR_RESPONSES)
    async def save(media_id: str, session: SessionRecord = write_session) -> MediaSaveResponse:
        try:
            descriptor, changed = await asyncio.to_thread(service.store.save, media_id, actor_id=session.actor_id, grants=session.resource_grants)
        except MediaObjectFailure as exc:
            raise _api_error(exc) from exc
        return MediaSaveResponse(media=descriptor, saved=changed)

    @router.post("/{media_id}:recognize", response_model=MediaRecognition, operation_id="recognizeMediaObject", responses=ERROR_RESPONSES)
    async def recognize(media_id: str, body: MediaRecognizeRequest, session: SessionRecord = recognize_session) -> MediaRecognition:
        try:
            return await service.recognize(media_id, session=session, use_cache=body.use_cache)
        except MediaObjectFailure as exc:
            raise _api_error(exc) from exc

    @router.get("/{media_id}/derivatives", response_model=MediaDerivativeList, operation_id="getMediaDerivatives", responses=ERROR_RESPONSES)
    async def derivatives(media_id: str, session: SessionRecord = read_session) -> MediaDerivativeList:
        try:
            return await asyncio.to_thread(service.store.derivatives, media_id, actor_id=session.actor_id, grants=session.resource_grants)
        except MediaObjectFailure as exc:
            raise _api_error(exc) from exc

    return router


def _range_bounds(value: str | None, length: int) -> tuple[int, int, int]:
    if value is None:
        return 0, length - 1, 200
    match = _RANGE_RE.fullmatch(value.strip())
    if match is None or length < 1:
        raise MediaObjectFailure("range_not_satisfiable", status_code=416)
    start_text, end_text = match.groups()
    if not start_text:
        count = int(end_text or "0")
        if count <= 0:
            raise MediaObjectFailure("range_not_satisfiable", status_code=416)
        start = max(0, length - count)
        end = length - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else length - 1
    if start >= length or end < start:
        raise MediaObjectFailure("range_not_satisfiable", status_code=416)
    return start, min(end, length - 1), 206


async def default_media_recognizer(data: bytes, kind: str, use_cache: bool) -> str | None:
    """Adapt managed bytes to the existing recognition service without paths."""

    from src.app.plugin_system.api.media_api import recognize_media

    encoded = base64.b64encode(data).decode("ascii")
    if kind == "audio":
        from src.core.managers.media_manager import get_media_manager

        return await get_media_manager().recognize_voice(
            voice_data=encoded,
            use_cache=use_cache,
        )
    return await recognize_media(encoded, kind, use_cache=use_cache)


__all__ = [
    "ManagedMediaService",
    "MediaContent",
    "MediaObjectFailure",
    "MediaObjectStore",
    "create_media_router",
    "default_media_recognizer",
]
