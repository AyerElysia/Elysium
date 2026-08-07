"""P3-10 authenticated tabletop Werewolf API."""

# FastAPI dependencies are intentionally declared in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response, WebSocket
from starlette.websockets import WebSocketDisconnect

from plugins.werewolf_game.boards import list_boards
from plugins.werewolf_game.domain import (
    ActionConflict,
    DomainActionRejected,
    DomainAuthorizationError,
    RevisionConflict,
    RoomNotFound,
    WerewolfDomainService,
)

from .auth_store import AuthStore, SessionRecord
from .commands import IDEMPOTENCY_KEY_PATTERN
from .runtime import ERROR_RESPONSES, APIError
from .schemas.tabletop import (
    TabletopActionRequest,
    TabletopActionResponse,
    TabletopEndRequest,
    TabletopEvent,
    TabletopEventPage,
    TabletopGameDefinition,
    TabletopGamesResponse,
    TabletopJoinRequest,
    TabletopRevisionRequest,
    TabletopRoomCreateRequest,
    TabletopRoomResponse,
)
from .tokens import SignedValueCodec, SignedValueError


def create_tabletop_router(
    *,
    service: WerewolfDomainService,
    require_scope: Callable[..., Callable[[SessionRecord], SessionRecord]],
    auth_store: AuthStore,
    codec: SignedValueCodec,
) -> APIRouter:
    """Create the user tabletop router without owning ledger lifecycle."""

    router = APIRouter(prefix="/tabletop")

    async def reader(
        session: SessionRecord = Depends(require_scope("tabletop:read")),
    ) -> SessionRecord:
        return session

    async def player(
        session: SessionRecord = Depends(require_scope("tabletop:play")),
    ) -> SessionRecord:
        return session

    @router.get(
        "/games",
        response_model=TabletopGamesResponse,
        operation_id="listTabletopGames",
        responses=ERROR_RESPONSES,
    )
    async def games(session: SessionRecord = Depends(reader)) -> TabletopGamesResponse:
        del session
        boards = tuple(
            {
                "name": board.name,
                "player_count": board.player_count,
                "roles": [role.value for role in board.roles],
                "win_rule": board.win_rule.value,
                "description": board.description,
            }
            for board in list_boards()
        )
        return TabletopGamesResponse(
            games=(TabletopGameDefinition(boards=boards),)
        )

    @router.post(
        "/rooms",
        response_model=TabletopActionResponse,
        status_code=201,
        operation_id="createTabletopRoom",
        responses=ERROR_RESPONSES,
    )
    async def create_room(
        body: TabletopRoomCreateRequest,
        response: Response,
        session: SessionRecord = Depends(player),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TabletopActionResponse:
        action_id = _action_id(key)
        try:
            outcome = await service.create_room(
                actor_id=session.actor_id,
                display_name=body.display_name,
                platform=body.platform,
                group_id=body.group_id,
                group_name=body.group_name,
                group_stream_id=body.group_stream_id,
                board_name=body.board_name,
                action_id=action_id,
                room_id=_room_id_from_action(action_id),
            )
        except ActionConflict as exc:
            raise _conflict(exc) from exc
        except RuntimeError as exc:
            if str(exc) == "tabletop capability is unavailable":
                raise APIError(
                    "capability_disabled",
                    "狼人杀能力当前未加载。",
                    status_code=404,
                ) from exc
            raise
        if outcome["result"].get("revision") == 1:
            response.status_code = 201
        return TabletopActionResponse(result=outcome["result"], room=outcome["view"])

    @router.get(
        "/rooms/{room_id}",
        response_model=TabletopRoomResponse,
        operation_id="getTabletopRoom",
        responses=ERROR_RESPONSES,
    )
    async def room(
        room_id: str, session: SessionRecord = Depends(reader)
    ) -> TabletopRoomResponse:
        return TabletopRoomResponse(
            room=await _call(
                service.room_view,
                room_id,
                actor_id=session.actor_id,
                grants=session.resource_grants,
            )
        )

    async def action(
        *,
        room_id: str,
        action_type: str,
        payload: dict[str, Any],
        expected_revision: int | None,
        session: SessionRecord,
        key: str | None,
    ) -> TabletopActionResponse:
        try:
            outcome = await service.apply_action(
                room_id=room_id,
                actor_id=session.actor_id,
                action_id=_action_id(key),
                action_type=action_type,
                payload=payload,
                expected_revision=expected_revision,
            )
        except RoomNotFound as exc:
            raise _not_found() from exc
        except RuntimeError as exc:
            if str(exc) == "tabletop capability is unavailable":
                raise APIError(
                    "capability_disabled",
                    "狼人杀能力当前未加载。",
                    status_code=404,
                ) from exc
            raise
        except DomainAuthorizationError as exc:
            raise APIError("resource_forbidden", "当前身份无权执行该动作。", status_code=403) from exc
        except DomainActionRejected as exc:
            raise APIError("action_rejected", str(exc), status_code=409) from exc
        except (ActionConflict, RevisionConflict) as exc:
            raise _conflict(exc) from exc
        return TabletopActionResponse(result=outcome["result"], room=outcome["view"])

    @router.post(
        "/rooms/{room_id}:join",
        response_model=TabletopActionResponse,
        operation_id="joinTabletopRoom",
        responses=ERROR_RESPONSES,
    )
    async def join(
        room_id: str,
        body: TabletopJoinRequest,
        session: SessionRecord = Depends(player),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TabletopActionResponse:
        return await action(
            room_id=room_id,
            action_type="join",
            payload={"display_name": body.display_name},
            expected_revision=body.expected_revision,
            session=session,
            key=key,
        )

    @router.post(
        "/rooms/{room_id}:leave",
        response_model=TabletopActionResponse,
        operation_id="leaveTabletopRoom",
        responses=ERROR_RESPONSES,
    )
    async def leave(
        room_id: str,
        body: TabletopRevisionRequest,
        session: SessionRecord = Depends(player),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TabletopActionResponse:
        return await action(room_id=room_id, action_type="leave", payload={}, expected_revision=body.expected_revision, session=session, key=key)

    @router.post(
        "/rooms/{room_id}:start",
        response_model=TabletopActionResponse,
        operation_id="startTabletopRoom",
        responses=ERROR_RESPONSES,
    )
    async def start(
        room_id: str,
        body: TabletopRevisionRequest,
        session: SessionRecord = Depends(player),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TabletopActionResponse:
        return await action(room_id=room_id, action_type="start", payload={}, expected_revision=body.expected_revision, session=session, key=key)

    @router.post(
        "/rooms/{room_id}:end",
        response_model=TabletopActionResponse,
        operation_id="endTabletopRoom",
        responses=ERROR_RESPONSES,
    )
    async def end(
        room_id: str,
        body: TabletopEndRequest,
        session: SessionRecord = Depends(player),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TabletopActionResponse:
        return await action(room_id=room_id, action_type="end", payload={"reason": body.reason}, expected_revision=body.expected_revision, session=session, key=key)

    @router.post(
        "/rooms/{room_id}/actions",
        response_model=TabletopActionResponse,
        operation_id="submitTabletopAction",
        responses=ERROR_RESPONSES,
    )
    async def submit(
        room_id: str,
        body: TabletopActionRequest,
        session: SessionRecord = Depends(player),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TabletopActionResponse:
        payload = body.model_dump(mode="json", exclude={"schema_version", "action_type", "expected_revision"}, exclude_none=True)
        return await action(room_id=room_id, action_type=body.action_type, payload=payload, expected_revision=body.expected_revision, session=session, key=key)

    @router.get(
        "/rooms/{room_id}/view",
        response_model=TabletopRoomResponse,
        operation_id="getTabletopPlayerView",
        responses=ERROR_RESPONSES,
    )
    async def private(room_id: str, session: SessionRecord = Depends(player)) -> TabletopRoomResponse:
        return TabletopRoomResponse(room=await _call(service.private_view, room_id, actor_id=session.actor_id))

    @router.get(
        "/rooms/{room_id}/events",
        response_model=TabletopEventPage,
        operation_id="queryTabletopEvents",
        responses=ERROR_RESPONSES,
    )
    async def events(
        room_id: str,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        session: SessionRecord = Depends(reader),
    ) -> TabletopEventPage:
        records = await _call(
            service.events,
            room_id,
            actor_id=session.actor_id,
            grants=session.resource_grants,
            after_sequence=after_sequence,
            limit=limit + 1,
        )
        has_more = len(records) > limit
        page = records[:limit]
        next_sequence = page[-1].sequence if page else after_sequence
        return TabletopEventPage(
            events=tuple(
                TabletopEvent(
                    event_id=item.event_id,
                    room_id=item.room_id,
                    sequence=item.sequence,
                    event_type=item.event_type,
                    actor_id=item.actor_id,
                    visibility=item.visibility,
                    payload=item.payload,
                    occurred_at=item.occurred_at,
                )
                for item in page
            ),
            next_sequence=next_sequence,
            has_more=has_more,
        )

    @router.get(
        "/rooms/{room_id}/replay",
        response_model=TabletopRoomResponse,
        operation_id="getTabletopReplay",
        responses=ERROR_RESPONSES,
    )
    async def replay(room_id: str, session: SessionRecord = Depends(reader)) -> TabletopRoomResponse:
        return TabletopRoomResponse(
            room=await _call(service.replay, room_id, actor_id=session.actor_id, grants=session.resource_grants)
        )

    @router.websocket("/rooms/{room_id}/ws", name="tabletopRoomWebsocket")
    async def room_socket(websocket: WebSocket, room_id: str) -> None:
        resource = f"/api/v1/tabletop/rooms/{room_id}/ws"
        subprotocol = "elysium.tabletop.room.v1"
        offered = websocket.headers.get("sec-websocket-protocol", "").split(",", 1)[0].strip()
        if offered != subprotocol:
            await websocket.close(code=1008, reason="invalid tabletop subprotocol")
            return
        try:
            grant = await asyncio.to_thread(
                auth_store.consume_ws_ticket,
                token=websocket.query_params.get("ticket", ""),
                codec=codec,
                resource=resource,
                subprotocol=subprotocol,
                origin=websocket.headers.get("origin"),
            )
            await service.room_view(
                room_id,
                actor_id=grant.actor_id,
                grants=(),
            )
        except (SignedValueError, TypeError, ValueError, RoomNotFound, DomainAuthorizationError):
            await websocket.close(code=1008, reason="invalid tabletop ticket or room grant")
            return
        await websocket.accept(subprotocol=subprotocol)
        after_sequence = 0
        try:
            while True:
                events = await service.events(
                    room_id,
                    actor_id=grant.actor_id,
                    after_sequence=after_sequence,
                    limit=200,
                )
                for event in events:
                    await websocket.send_json(
                        {
                            "type": "tabletop.event",
                            "event": {
                                "event_id": event.event_id,
                                "room_id": event.room_id,
                                "sequence": event.sequence,
                                "event_type": event.event_type,
                                "actor_id": event.actor_id,
                                "visibility": event.visibility,
                                "payload": event.payload,
                                "occurred_at": event.occurred_at.isoformat(),
                            },
                        }
                    )
                    after_sequence = event.sequence
                await asyncio.sleep(0.25)
        except (WebSocketDisconnect, RuntimeError):
            return

    return router


async def _call(function, *args, **kwargs):
    try:
        return await function(*args, **kwargs)
    except RoomNotFound as exc:
        raise _not_found() from exc
    except DomainAuthorizationError as exc:
        raise APIError("resource_forbidden", "当前身份无权访问该房间。", status_code=403) from exc
    except ValueError as exc:
        raise APIError("resource_state_conflict", str(exc), status_code=409) from exc
    except RuntimeError as exc:
        if str(exc) == "tabletop capability is unavailable":
            raise APIError(
                "capability_disabled",
                "狼人杀能力当前未加载。",
                status_code=404,
            ) from exc
        raise


def _action_id(value: str | None) -> str:
    key = (value or "").strip()
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise APIError("idempotency_key_required", "该动作需要有效的 Idempotency-Key。", status_code=422)
    return key


def _room_id_from_action(action_id: str) -> str:
    import hashlib

    return f"room_{hashlib.sha256(action_id.encode('utf-8')).hexdigest()[:24]}"


def _not_found() -> APIError:
    return APIError("resource_not_found", "请求的狼人杀房间不存在。", status_code=404)


def _conflict(exc: Exception) -> APIError:
    code = "revision_conflict" if isinstance(exc, RevisionConflict) else "idempotency_conflict"
    return APIError(code, "动作身份或房间 revision 冲突。", status_code=409)


__all__ = ["create_tabletop_router"]
