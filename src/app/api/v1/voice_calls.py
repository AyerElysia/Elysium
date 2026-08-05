"""P3-09 durable voice-call control plane and realtime gateways."""

# FastAPI dependencies are intentionally declared in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from fastapi import APIRouter, Depends, Header, Query, Response, WebSocket

from plugins.voice_live.router import VoiceLiveRouter
from plugins.voice_live.runtime_store import EpisodeRecord, VoiceEpisodeStore
from src.kernel.commands import (
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
from .schemas.auth import WSTicketResponse
from .schemas.voice_call import (
    VoiceCallCommandAccepted,
    VoiceCallCreated,
    VoiceCallCreateRequest,
    VoiceCallInterruptRequest,
    VoiceCallStatus,
    VoiceCallTextRequest,
    VoiceCallTicketRequest,
    VoiceTranscriptEntry,
    VoiceTranscriptPage,
)
from .tokens import SignedValueCodec, SignedValueError

_PARTICIPANT_PROTOCOL = "elysium.voice-call.participant.v1"
_OBSERVER_PROTOCOL = "elysium.voice-call.observer.v1"
_TICKET_TTL = timedelta(seconds=30)


class VoiceCallAction(StrEnum):
    RESUME = "voice_call.resume"
    INTERRUPT = "voice_call.interrupt"
    END = "voice_call.end"
    TEXT = "voice_call.text"


class VoiceCallProvider(Protocol):
    def router(self) -> VoiceLiveRouter | None: ...
    def store(self, call_id: str) -> VoiceEpisodeStore | None: ...


class VoiceCallQueryService:
    """Project durable episode metadata without reading raw audio."""

    def __init__(self, provider: VoiceCallProvider, codec: SignedValueCodec) -> None:
        self.provider = provider
        self.codec = codec

    async def create(self, actor_id: str, mode: str) -> VoiceCallStatus:
        store = self._new_store()
        now = datetime.now(UTC).isoformat()
        await store.append_async(
            "call.created",
            {
                "call_id": store.episode_id,
                "episode_id": store.episode_id,
                "owner_actor_id": actor_id,
                "mode": mode,
                "visibility": "participants",
                "created_at": now,
            },
        )
        await store.checkpoint_async(
            "created",
            session_id=store.episode_id,
            owner_actor_id=actor_id,
            mode=mode,
            created_at=now,
            resumable=True,
        )
        return await self.get(store.episode_id, actor_id=actor_id, grants=())

    async def get(
        self,
        call_id: str,
        *,
        actor_id: str,
        grants: tuple[str, ...],
    ) -> VoiceCallStatus:
        store = self._store(call_id)
        records = await asyncio.to_thread(store.read_all)
        owner = self._owner(records)
        self._authorize(call_id, owner, actor_id, grants)
        checkpoint = await asyncio.to_thread(store.load_checkpoint)
        router = self.provider.router()
        active = router.get_session(call_id) if router is not None else None
        if active is not None:
            snapshot = active.snapshot()
            state = str(snapshot["state"])
        else:
            snapshot = {}
            state = str(checkpoint.get("state") or self._terminal_state(records) or "created")
        created = next(record for record in records if record.event == "call.created")
        updated = records[-1]
        return VoiceCallStatus(
            call_id=call_id,
            episode_id=call_id,
            state=state,
            mode=str(snapshot.get("mode") or checkpoint.get("mode") or created.payload.get("mode") or ""),
            provider=str(snapshot.get("provider") or checkpoint.get("provider") or ""),
            created_at=self._time(str(created.payload.get("created_at") or created.timestamp)),
            updated_at=self._time(updated.timestamp),
            resumable=active is None and state in {"created", "ended", "failed", "suspended"},
            connected=active is not None,
            input_audio_bytes=int(snapshot.get("input_audio_bytes") or 0),
            output_audio_bytes=int(snapshot.get("output_audio_bytes") or 0),
            interruptions=int(snapshot.get("interruptions") or 0),
            failure_reason=str(snapshot.get("failure_reason") or ""),
        )

    async def transcripts(
        self,
        call_id: str,
        *,
        actor_id: str,
        grants: tuple[str, ...],
        cursor: str | None,
        limit: int,
    ) -> VoiceTranscriptPage:
        store = self._store(call_id)
        records = await asyncio.to_thread(store.read_all)
        self._authorize(call_id, self._owner(records), actor_id, grants)
        position = self._decode_cursor(call_id, cursor)
        finals = [record for record in records if record.sequence > position and record.event == "transcript.final"]
        page = finals[:limit]
        has_more = len(finals) > limit
        next_position = page[-1].sequence if page else position
        return VoiceTranscriptPage(
            transcripts=tuple(self._transcript(record) for record in page),
            next_cursor=(self.codec.encode_cursor(next_position, ledger=f"voice-call:{call_id}") if has_more else None),
            has_more=has_more,
        )

    async def authorize(self, call_id: str, session: SessionRecord) -> None:
        store = self._store(call_id)
        records = await asyncio.to_thread(store.read_all)
        self._authorize(call_id, self._owner(records), session.actor_id, session.resource_grants)

    def _new_store(self) -> VoiceEpisodeStore:
        call_id = f"call_{secrets.token_urlsafe(12)}"
        store = self.provider.store(call_id)
        if store is None:
            raise APIError("capability_disabled", "实时语音能力当前未加载。", status_code=404)
        return store

    def _store(self, call_id: str) -> VoiceEpisodeStore:
        store = self.provider.store(call_id)
        if store is None or not store.events_path.is_file():
            raise APIError("resource_not_found", "请求的语音通话不存在。", status_code=404)
        return store

    @staticmethod
    def _owner(records: list[EpisodeRecord]) -> str:
        created = next((record for record in records if record.event == "call.created"), None)
        if created is None:
            raise APIError("resource_not_found", "请求的语音通话不存在。", status_code=404)
        return str(created.payload.get("owner_actor_id") or "")

    @staticmethod
    def _authorize(call_id: str, owner: str, actor_id: str, grants: tuple[str, ...]) -> None:
        values = set(grants)
        if actor_id == owner or "*" in values or "voice_call:*" in values or f"voice_call:{call_id}" in values:
            return
        raise APIError("resource_forbidden", "当前身份无权访问该语音通话。", status_code=403)

    def _decode_cursor(self, call_id: str, cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            return self.codec.decode_cursor(cursor, ledger=f"voice-call:{call_id}")
        except SignedValueError as exc:
            raise APIError("cursor_invalid", "转写 cursor 无效。", status_code=422) from exc

    @staticmethod
    def _terminal_state(records: list[EpisodeRecord]) -> str:
        if any(record.event == "session.failed" for record in records):
            return "failed"
        if any(record.event == "session.ended" for record in records):
            return "ended"
        return ""

    @staticmethod
    def _time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @classmethod
    def _transcript(cls, record: EpisodeRecord) -> VoiceTranscriptEntry:
        role = str(record.payload.get("role") or "")
        if role not in {"user", "assistant"}:
            raise APIError("projection_invalid", "转写记录角色无效。", status_code=500)
        return VoiceTranscriptEntry(
            sequence=record.sequence,
            occurred_at=cls._time(record.timestamp),
            role=role,
            text=str(record.payload.get("text") or ""),
            provider_event_id=str(record.payload.get("provider_event_id") or ""),
        )


class VoiceCallCommandService:
    def __init__(self, provider: VoiceCallProvider) -> None:
        self.provider = provider

    def register(self, registry: HandlerRegistry) -> None:
        for action in VoiceCallAction:
            registry.register(
                action.value,
                self.handle,
                required_scopes=frozenset({"voice_call:operate"}),
                timeout_seconds=30.0,
            )

    async def handle(self, command: CommandRecord) -> CommandOutcome:
        call_id = str(command.target.get("call_id") or "")
        store = self.provider.store(call_id)
        router = self.provider.router()
        if store is None or not store.events_path.is_file():
            return CommandOutcome(status=CommandStatus.REJECTED, error_code="voice_call_not_found", safe_error_detail="语音通话不存在。")
        records = await asyncio.to_thread(store.read_all)
        owner = VoiceCallQueryService._owner(records)
        active = router.get_session(call_id) if router is not None else None
        action = VoiceCallAction(command.command_type)
        if action is VoiceCallAction.RESUME:
            if active is not None:
                return CommandOutcome(status=CommandStatus.REJECTED, error_code="voice_call_active", safe_error_detail="通话已经连接。")
            await store.append_async("call.resume_requested", {"actor_id": command.actor_id})
            await store.checkpoint_async("created", session_id=call_id, owner_actor_id=owner, resumable=True)
            result = {"call_id": call_id, "resumable": True}
        elif active is None:
            return CommandOutcome(status=CommandStatus.REJECTED, error_code="voice_call_not_connected", safe_error_detail="通话当前未连接。")
        elif action is VoiceCallAction.INTERRUPT:
            await active.handle_message({"type": "interrupt", "played_audio_ms": command.payload.get("played_audio_ms")})
            await store.append_async("call.interrupted", {"actor_id": command.actor_id})
            result = {"interrupted": True}
        elif action is VoiceCallAction.END:
            await active.stop(reason="authenticated API participant end")
            result = {"status": "ended"}
        else:
            await active.handle_message({"type": "text", "text": str(command.payload["text"])})
            result = {"accepted": True}
        return CommandOutcome(status=CommandStatus.SUCCEEDED, result=result)


def create_voice_call_router(
    *,
    queries: VoiceCallQueryService,
    store: CommandStore,
    dispatcher: CommandDispatcher,
    require_scope: Callable[..., Callable[[SessionRecord], SessionRecord]],
    auth_store: AuthStore,
    codec: SignedValueCodec,
) -> APIRouter:
    router = APIRouter(prefix="/voice-calls")

    async def participant(session: SessionRecord = Depends(require_scope("voice_call:operate"))) -> SessionRecord:
        return session

    async def reader(session: SessionRecord = Depends(require_scope("voice_call:read"))) -> SessionRecord:
        return session

    def issue_ticket(call_id: str, role: str, origin: str | None, session: SessionRecord) -> WSTicketResponse:
        scope = "voice_call:operate" if role == "participant" else "voice_call:observe"
        if scope not in session.scopes:
            raise APIError("scope_required", f"缺少权限：{scope}", status_code=403)
        resource = f"/api/v1/voice-calls/{call_id}/{'ws' if role == 'participant' else 'observe'}"
        subprotocol = _PARTICIPANT_PROTOCOL if role == "participant" else _OBSERVER_PROTOCOL
        try:
            ticket, token = auth_store.issue_ws_ticket(
                session=session,
                codec=codec,
                resource=resource,
                subprotocol=subprotocol,
                scopes=(scope,),
                origin=origin,
                ttl=_TICKET_TTL,
            )
        except ValueError as exc:
            raise APIError("ticket_rejected", "无法签发语音通话 ticket。", status_code=403) from exc
        return WSTicketResponse(ticket=token, expires_at=ticket.expires_at, resource=resource, subprotocol=subprotocol, scopes=ticket.scopes)

    @router.post("", response_model=VoiceCallCreated, status_code=201, operation_id="createVoiceCall", responses=ERROR_RESPONSES)
    async def create(
        body: VoiceCallCreateRequest,
        origin: str | None = Header(default=None, alias="Origin"),
        session: SessionRecord = Depends(participant),
    ) -> VoiceCallCreated:
        call = await queries.create(session.actor_id, body.mode)
        connection = issue_ticket(call.call_id, "participant", origin, session)
        return VoiceCallCreated(call=call, connection=connection)

    @router.get("/{call_id}", response_model=VoiceCallStatus, operation_id="getVoiceCall", responses=ERROR_RESPONSES)
    async def get(call_id: str, session: SessionRecord = Depends(reader)) -> VoiceCallStatus:
        return await queries.get(call_id, actor_id=session.actor_id, grants=session.resource_grants)

    @router.get("/{call_id}/transcripts", response_model=VoiceTranscriptPage, operation_id="queryVoiceCallTranscripts", responses=ERROR_RESPONSES)
    async def transcripts(call_id: str, session: SessionRecord = Depends(reader), cursor: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> VoiceTranscriptPage:
        return await queries.transcripts(call_id, actor_id=session.actor_id, grants=session.resource_grants, cursor=cursor, limit=limit)

    async def accept(action: VoiceCallAction, call_id: str, payload: dict[str, Any], session: SessionRecord, key: str | None, response: Response) -> VoiceCallCommandAccepted:
        await queries.authorize(call_id, session)
        normalized = (key or "").strip()
        if not IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
            raise APIError("idempotency_key_required", "该命令需要有效的 Idempotency-Key。", status_code=422)
        request_hash = store.request_hash(command_type=action.value, schema_version=1, target={"domain": "voice_call", "call_id": call_id}, payload=payload, correlation_id=None, expected_revision=None)
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
                target={"domain": "voice_call", "call_id": call_id},
                payload=payload,
            )
        except IdempotencyConflict as exc:
            raise APIError("idempotency_conflict", "该 Idempotency-Key 已用于不同命令。", status_code=409) from exc
        if created:
            dispatcher.schedule(command.command_id)
        else:
            response.status_code = 200
        return VoiceCallCommandAccepted(command=_response(command))

    @router.post("/{call_id}:resume", response_model=VoiceCallCommandAccepted, status_code=202, operation_id="resumeVoiceCall", responses=ERROR_RESPONSES)
    async def resume(call_id: str, response: Response, session: SessionRecord = Depends(participant), key: str | None = Header(default=None, alias="Idempotency-Key")) -> VoiceCallCommandAccepted:
        return await accept(VoiceCallAction.RESUME, call_id, {}, session, key, response)

    @router.post("/{call_id}:interrupt", response_model=VoiceCallCommandAccepted, status_code=202, operation_id="interruptVoiceCall", responses=ERROR_RESPONSES)
    async def interrupt(call_id: str, body: VoiceCallInterruptRequest, response: Response, session: SessionRecord = Depends(participant), key: str | None = Header(default=None, alias="Idempotency-Key")) -> VoiceCallCommandAccepted:
        return await accept(VoiceCallAction.INTERRUPT, call_id, body.model_dump(mode="json", exclude={"schema_version"}), session, key, response)

    @router.post("/{call_id}:end", response_model=VoiceCallCommandAccepted, status_code=202, operation_id="endVoiceCall", responses=ERROR_RESPONSES)
    async def end(call_id: str, response: Response, session: SessionRecord = Depends(participant), key: str | None = Header(default=None, alias="Idempotency-Key")) -> VoiceCallCommandAccepted:
        return await accept(VoiceCallAction.END, call_id, {}, session, key, response)

    @router.post("/{call_id}/text", response_model=VoiceCallCommandAccepted, status_code=202, operation_id="sendVoiceCallText", responses=ERROR_RESPONSES)
    async def text(call_id: str, body: VoiceCallTextRequest, response: Response, session: SessionRecord = Depends(participant), key: str | None = Header(default=None, alias="Idempotency-Key")) -> VoiceCallCommandAccepted:
        return await accept(VoiceCallAction.TEXT, call_id, {"text": body.text.strip()}, session, key, response)

    @router.post("/{call_id}/tickets", response_model=WSTicketResponse, operation_id="issueVoiceCallTicket", responses=ERROR_RESPONSES)
    async def tickets(call_id: str, body: VoiceCallTicketRequest, session: SessionRecord = Depends(require_scope("voice_call:read"))) -> WSTicketResponse:
        await queries.authorize(call_id, session)
        return issue_ticket(call_id, body.role, body.origin, session)

    async def consume_socket(websocket: WebSocket, call_id: str, role: str) -> tuple[VoiceLiveRouter, Any] | None:
        resource = f"/api/v1/voice-calls/{call_id}/{'ws' if role == 'participant' else 'observe'}"
        subprotocol = websocket.headers.get("sec-websocket-protocol", "").split(",", 1)[0].strip()
        expected = _PARTICIPANT_PROTOCOL if role == "participant" else _OBSERVER_PROTOCOL
        if subprotocol != expected:
            await websocket.close(code=1008, reason="invalid voice-call subprotocol")
            return None
        try:
            grant = await asyncio.to_thread(
                auth_store.consume_ws_ticket,
                token=websocket.query_params.get("ticket", ""),
                codec=codec,
                resource=resource,
                subprotocol=subprotocol,
                origin=websocket.headers.get("origin"),
            )
        except (SignedValueError, TypeError, ValueError):
            await websocket.close(code=1008, reason="invalid or expired voice-call ticket")
            return None
        runtime = queries.provider.router()
        if runtime is None:
            await websocket.close(code=1013, reason="voice-call runtime unavailable")
            return None
        await websocket.accept(subprotocol=subprotocol)
        return runtime, grant

    @router.websocket("/{call_id}/ws", name="voiceCallParticipantWebsocket")
    async def participant_socket(websocket: WebSocket, call_id: str) -> None:
        resolved = await consume_socket(websocket, call_id, "participant")
        if resolved is not None:
            await resolved[0].handle_public_participant(websocket, call_id)

    @router.websocket("/{call_id}/observe", name="voiceCallObserverWebsocket")
    async def observer_socket(websocket: WebSocket, call_id: str) -> None:
        resolved = await consume_socket(websocket, call_id, "observer")
        if resolved is not None:
            await resolved[0].handle_public_observer(websocket, call_id)

    return router


__all__ = [
    "VoiceCallAction",
    "VoiceCallCommandService",
    "VoiceCallProvider",
    "VoiceCallQueryService",
    "create_voice_call_router",
]
