"""P3-08 livestream query facade and durable command routes."""

# FastAPI dependencies are intentionally declared in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from fastapi import (
    APIRouter,
    Depends,
    Header,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
)

from plugins.livestream.ledger import LedgerRecord, LivestreamLedger
from src.kernel.commands import (
    CommandBacklogFull,
    CommandDispatcher,
    CommandOutcome,
    CommandRecord,
    CommandStatus,
    CommandStore,
    HandlerRegistry,
    IdempotencyConflict,
)

from .auth_store import AuthStore, SessionRecord
from .commands import IDEMPOTENCY_KEY_PATTERN, _response
from .runtime import ERROR_RESPONSES, APIError
from .schemas.livestream import (
    LivestreamCommandAccepted,
    LivestreamDanmakuRequest,
    LivestreamEvent,
    LivestreamEventPage,
    LivestreamSessionPage,
    LivestreamSessionSummary,
    LivestreamSpeechRequest,
    LivestreamStatus,
    _required_timestamp,
    _timestamp,
)
from .tokens import SignedValueCodec, SignedValueError

_LEDGER_ID = "livestream-ledger-v1"


class LivestreamAction(StrEnum):
    START = "livestream.session.start"
    STOP = "livestream.session.stop"
    INTERRUPT = "livestream.session.interrupt"
    SPEECH_REQUEST = "livestream.speech.request"
    DANMAKU_SEND = "livestream.danmaku.send"


class LivestreamRuntimeFacade(Protocol):
    state: str
    session_id: str | None

    async def health(self) -> Any: ...
    async def start(self) -> str: ...
    async def stop(self, *, reason: str = "manual stop") -> None: ...
    async def interrupt(self, *, reason: str = "operator interrupt") -> bool: ...
    async def manual_say(self, text: str) -> str: ...
    async def send_danmaku(self, text: str) -> Mapping[str, Any]: ...

    @property
    def stage(self) -> Any: ...


class LivestreamProvider(Protocol):
    def runtime(self) -> LivestreamRuntimeFacade | None: ...
    def ledger_path(self) -> Path | None: ...


@dataclass(slots=True)
class StaticLivestreamProvider:
    runtime_value: LivestreamRuntimeFacade | None = None
    ledger_path_value: Path | None = None

    def runtime(self) -> LivestreamRuntimeFacade | None:
        return self.runtime_value

    def ledger_path(self) -> Path | None:
        return self.ledger_path_value


class LivestreamQueryService:
    """Read the live runtime and the append-only ledger without starting either."""

    def __init__(self, provider: LivestreamProvider, codec: SignedValueCodec) -> None:
        self.provider = provider
        self.codec = codec

    async def status(self) -> LivestreamStatus:
        runtime = self.provider.runtime()
        if runtime is None:
            return LivestreamStatus(status="stopped")
        snapshot = await runtime.health()
        status = str(snapshot.status)
        reasons = tuple(str(value) for value in snapshot.degraded_reasons)
        if status == "running" and (not snapshot.platform_connected or reasons):
            status = "degraded"
        return LivestreamStatus(
            status=status,
            session_id=snapshot.session_id,
            platform_connected=snapshot.platform_connected,
            stage_clients=snapshot.stage_clients,
            primary_stage_connected=snapshot.primary_stage_connected,
            event_backlog=snapshot.event_backlog,
            performance_backlog=snapshot.performance_backlog,
            current_utterance_id=snapshot.current_utterance_id,
            last_platform_event_at=_timestamp(snapshot.last_platform_event_at),
            last_decision_at=_timestamp(snapshot.last_decision_at),
            last_playback_completed_at=_timestamp(snapshot.last_playback_completed_at),
            degraded_reasons=reasons,
        )

    async def sessions(self, *, cursor: str | None, limit: int) -> LivestreamSessionPage:
        before = self._decode_cursor(cursor) if cursor is not None else None
        path = self.provider.ledger_path()
        if path is None or not path.is_file():
            return LivestreamSessionPage(sessions=())
        ledger = LivestreamLedger(path)
        await ledger.start()
        try:
            starts = await ledger.read_before(
                before,
                kinds={"session.started"},
                limit=limit + 1,
            )
            page_starts = starts[:limit]
            values: list[LivestreamSessionSummary] = []
            for started in page_starts:
                records = await ledger.read_since(
                    started.sequence - 1,
                    session_id=started.session_id,
                    limit=1000,
                )
                values.append(self._session_summary(records))
        finally:
            await ledger.stop()
        has_more = len(starts) > limit
        next_position = page_starts[-1].sequence if page_starts else None
        return LivestreamSessionPage(
            sessions=tuple(values),
            next_cursor=(
                self.codec.encode_cursor(next_position, ledger=_LEDGER_ID)
                if has_more and next_position is not None
                else None
            ),
            has_more=has_more,
        )

    async def session(self, session_id: str) -> LivestreamSessionSummary:
        records = await self._read(0, session_id=session_id, limit=1000)
        if not records:
            raise APIError("resource_not_found", "请求的直播场次不存在。", status_code=404)
        return self._session_summary(records)

    async def events(
        self,
        session_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> LivestreamEventPage:
        position = self._decode_cursor(cursor)
        records = await self._read(position, session_id=session_id, limit=limit + 1)
        if not records and position == 0:
            await self.session(session_id)
        page = records[:limit]
        has_more = len(records) > limit
        next_position = page[-1].sequence if page else position
        return LivestreamEventPage(
            events=tuple(self._event(record) for record in page),
            next_cursor=(self.codec.encode_cursor(next_position, ledger=_LEDGER_ID) if has_more else None),
            has_more=has_more,
        )

    async def _read(
        self,
        sequence: int,
        *,
        session_id: str | None = None,
        limit: int,
    ) -> list[LedgerRecord]:
        path = self.provider.ledger_path()
        if path is None or not path.is_file():
            return []
        ledger = LivestreamLedger(path)
        await ledger.start()
        try:
            return await ledger.read_since(sequence, session_id=session_id, limit=limit)
        finally:
            await ledger.stop()

    def _decode_cursor(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            return self.codec.decode_cursor(cursor, ledger=_LEDGER_ID)
        except SignedValueError as exc:
            raise APIError("cursor_invalid", "直播 cursor 无效。", status_code=422) from exc

    @staticmethod
    def _session_summary(records: list[LedgerRecord]) -> LivestreamSessionSummary:
        started = next((item for item in records if item.kind == "session.started"), records[0])
        stopped = next((item for item in reversed(records) if item.kind == "session.stopped"), None)
        payload = started.payload
        state = "stopped" if stopped is not None else ("running" if started.kind == "session.started" else "unknown")
        return LivestreamSessionSummary(
            session_id=started.session_id,
            platform=_safe_text(payload.get("platform")),
            room_id=_safe_text(payload.get("room_id")),
            state=state,
            started_at=_required_timestamp(started.occurred_at),
            stopped_at=_required_timestamp(stopped.occurred_at) if stopped else None,
            start_mode=_safe_text(payload.get("start_mode")),
            last_sequence=records[-1].sequence,
            event_count=len(records),
        )

    @staticmethod
    def _event(record: LedgerRecord) -> LivestreamEvent:
        payload = _public_payload(record)
        return LivestreamEvent(
            sequence=record.sequence,
            record_id=record.record_id,
            session_id=record.session_id,
            event_type=_public_event_type(record),
            occurred_at=_required_timestamp(record.occurred_at),
            source=record.source,
            correlation_id=record.correlation_id,
            causation_id=record.causation_id,
            payload=payload,
        )


class LivestreamCommandService:
    def __init__(self, provider: LivestreamProvider) -> None:
        self.provider = provider

    def register(self, registry: HandlerRegistry) -> None:
        for action in LivestreamAction:
            registry.register(
                action.value,
                self.handle,
                required_scopes=frozenset({"livestream:operate"}),
                timeout_seconds=30.0,
            )

    async def handle(self, command: CommandRecord) -> CommandOutcome:
        runtime = self.provider.runtime()
        if runtime is None:
            return CommandOutcome(
                status=CommandStatus.REJECTED,
                error_code="capability_disabled",
                safe_error_detail="直播运行时当前未加载。",
            )
        action = LivestreamAction(command.command_type)
        try:
            if action is LivestreamAction.START:
                result = {"session_id": await runtime.start()}
            elif action is LivestreamAction.STOP:
                await runtime.stop(reason="authenticated API operator stop")
                result = {"status": "stopped"}
            elif action is LivestreamAction.INTERRUPT:
                result = {"interrupted": await runtime.interrupt(reason="authenticated API operator interrupt")}
            elif action is LivestreamAction.SPEECH_REQUEST:
                result = {"utterance_id": await runtime.manual_say(str(command.payload["text"]))}
            else:
                result = dict(await runtime.send_danmaku(str(command.payload["text"])))
        except RuntimeError as exc:
            return CommandOutcome(
                status=CommandStatus.FAILED,
                error_code="livestream_failed",
                safe_error_detail=str(exc),
            )
        return CommandOutcome(status=CommandStatus.SUCCEEDED, result=result)


def create_livestream_router(
    *,
    queries: LivestreamQueryService,
    store: CommandStore,
    dispatcher: CommandDispatcher,
    require_scope: Callable[..., Callable[[SessionRecord], SessionRecord]],
    auth_store: AuthStore,
    codec: SignedValueCodec,
) -> APIRouter:
    router = APIRouter(prefix="/livestream")

    @router.get("/status", response_model=LivestreamStatus, operation_id="getLivestreamStatus", responses=ERROR_RESPONSES)
    async def status(_session: SessionRecord = Depends(require_scope("livestream:read"))) -> LivestreamStatus:
        return await queries.status()

    @router.get("/sessions", response_model=LivestreamSessionPage, operation_id="queryLivestreamSessions", responses=ERROR_RESPONSES)
    async def sessions(
        _session: SessionRecord = Depends(require_scope("livestream:read")),
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> LivestreamSessionPage:
        return await queries.sessions(cursor=cursor, limit=limit)

    @router.get("/sessions/{session_id}", response_model=LivestreamSessionSummary, operation_id="getLivestreamSession", responses=ERROR_RESPONSES)
    async def session(session_id: str, _session: SessionRecord = Depends(require_scope("livestream:read"))) -> LivestreamSessionSummary:
        return await queries.session(session_id)

    @router.get("/sessions/{session_id}/events", response_model=LivestreamEventPage, operation_id="queryLivestreamEvents", responses=ERROR_RESPONSES)
    async def events(
        session_id: str,
        _session: SessionRecord = Depends(require_scope("livestream:read")),
        cursor: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> LivestreamEventPage:
        return await queries.events(session_id, cursor=cursor, limit=limit)

    async def accept(
        action: LivestreamAction,
        payload: dict[str, Any],
        session: SessionRecord,
        key: str | None,
        response: Response,
    ) -> LivestreamCommandAccepted:
        normalized = (key or "").strip()
        if not IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
            raise APIError("idempotency_key_required", "该命令需要有效的 Idempotency-Key。", status_code=422)
        request_hash = store.request_hash(
            command_type=action.value,
            schema_version=1,
            target={"domain": "livestream"},
            payload=payload,
            correlation_id=None,
            expected_revision=None,
        )
        try:
            command, created = await asyncio.to_thread(
                store.accept,
                idempotency_key=normalized,
                request_hash=request_hash,
                command_type=action.value,
                schema_version=1,
                actor_id=session.actor_id,
                caller_role=session.role,
                scopes=session.scopes,
                target={"domain": "livestream"},
                payload=payload,
                max_pending=dispatcher.max_backlog,
            )
        except CommandBacklogFull as exc:
            raise APIError(
                "command_backlog_full",
                "命令积压已达到技术上限，请稍后重试。",
                status_code=429,
                retryable=True,
            ) from exc
        except IdempotencyConflict as exc:
            raise APIError("idempotency_conflict", "该 Idempotency-Key 已用于不同命令。", status_code=409) from exc
        if created:
            try:
                dispatcher.schedule(command.command_id)
            except RuntimeError as exc:
                raise APIError(
                    "command_backlog_full",
                    "命令积压已达到技术上限，请稍后重试。",
                    status_code=429,
                    retryable=True,
                ) from exc
        else:
            response.status_code = 200
        return LivestreamCommandAccepted(command=_response(command))

    async def operator(session: SessionRecord = Depends(require_scope("livestream:operate"))) -> SessionRecord:
        if session.role not in {"administrator", "platform_service"}:
            raise APIError("role_required", "该直播操作需要管理员或受信平台服务身份。", status_code=403)
        return session

    def command_route(path: str, action: LivestreamAction, operation_id: str, body_model=None) -> None:
        async def endpoint(
            response: Response,
            session: SessionRecord = Depends(operator),
            key: str | None = Header(default=None, alias="Idempotency-Key"),
            body: Any = None,
        ) -> LivestreamCommandAccepted:
            payload = body.model_dump(mode="json", exclude={"schema_version"}) if body is not None else {}
            return await accept(action, payload, session, key, response)

        router.add_api_route(
            path,
            endpoint,
            methods=["POST"],
            response_model=LivestreamCommandAccepted,
            status_code=202,
            operation_id=operation_id,
            responses=ERROR_RESPONSES,
        )

    command_route("/session:start", LivestreamAction.START, "startLivestreamSession")
    command_route("/session:stop", LivestreamAction.STOP, "stopLivestreamSession")
    command_route("/session:interrupt", LivestreamAction.INTERRUPT, "interruptLivestreamSession")

    @router.post("/speech:request", response_model=LivestreamCommandAccepted, status_code=202, operation_id="requestLivestreamSpeech", responses=ERROR_RESPONSES)
    async def speech(
        body: LivestreamSpeechRequest,
        response: Response,
        session: SessionRecord = Depends(operator),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> LivestreamCommandAccepted:
        return await accept(LivestreamAction.SPEECH_REQUEST, {"text": body.text.strip()}, session, key, response)

    @router.post("/danmaku:send", response_model=LivestreamCommandAccepted, status_code=202, operation_id="sendLivestreamDanmaku", responses=ERROR_RESPONSES)
    async def danmaku(
        body: LivestreamDanmakuRequest,
        response: Response,
        session: SessionRecord = Depends(operator),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> LivestreamCommandAccepted:
        return await accept(LivestreamAction.DANMAKU_SEND, {"text": body.text.strip()}, session, key, response)

    @router.websocket("/stage/ws", name="livestreamStageWebsocket")
    async def stage_socket(websocket: WebSocket) -> None:
        resource = "/api/v1/livestream/stage/ws"
        ticket = websocket.query_params.get("ticket", "")
        origin = websocket.headers.get("origin")
        subprotocol = websocket.headers.get("sec-websocket-protocol", "")
        if "," in subprotocol:
            subprotocol = subprotocol.split(",", 1)[0].strip()
        try:
            grant = await asyncio.to_thread(
                auth_store.consume_ws_ticket,
                token=ticket,
                codec=codec,
                resource=resource,
                subprotocol=subprotocol,
                origin=origin,
            )
        except (SignedValueError, TypeError, ValueError):
            await websocket.close(code=1008, reason="invalid livestream stage ticket")
            return
        runtime = queries.provider.runtime()
        if runtime is None:
            await websocket.close(code=1013, reason="livestream runtime unavailable")
            return
        stage = runtime.stage
        client_id = websocket.query_params.get("client_id", "") or grant.ticket_id
        operator_grant = "livestream:operate" in grant.scopes
        request_primary = operator_grant and websocket.query_params.get("primary", "0") == "1"
        await websocket.accept(subprotocol=subprotocol or None)
        try:
            is_primary = await stage.attach(
                client_id,
                websocket,
                request_primary=request_primary,
            )
            await websocket.send_json(
                {
                    "version": 1,
                    "type": "stage.ready",
                    "payload": {
                        "client_id": client_id,
                        "role": "operator" if operator_grant else "observer",
                        "primary": is_primary,
                        "runtime_status": runtime.state,
                    },
                }
            )
            while True:
                raw = await websocket.receive_text()
                if not operator_grant:
                    data = json.loads(raw)
                    if data.get("type") not in {"hello", "pong"}:
                        await websocket.close(code=1008, reason="livestream observer is read-only")
                        return
                else:
                    data = json.loads(raw)
                    await stage.handle_message(client_id, data)
        except (WebSocketDisconnect, json.JSONDecodeError):
            pass
        finally:
            await stage.detach(client_id)

    return router


def _safe_text(value: Any) -> str | None:
    return str(value) if value not in {None, ""} else None


def _public_event_type(record: LedgerRecord) -> str:
    if record.kind == "platform.event":
        kind = str(record.payload.get("kind") or "event")
        return f"livestream.platform.{kind}_received"
    return f"livestream.{record.kind.replace('.', '_')}"


def _public_payload(record: LedgerRecord) -> dict[str, Any]:
    payload = dict(record.payload)
    if record.kind == "platform.event":
        payload.pop("raw_payload", None)
    for forbidden in ("path", "base64", "audio", "content_bytes"):
        payload.pop(forbidden, None)
    return payload


__all__ = [
    "LivestreamAction",
    "LivestreamCommandService",
    "LivestreamProvider",
    "LivestreamQueryService",
    "StaticLivestreamProvider",
    "create_livestream_router",
]
