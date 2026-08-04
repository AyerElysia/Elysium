"""FastAPI router for durable command submission, query, and cancellation."""

import re
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from src.kernel.commands import (
    TERMINAL_STATUSES,
    CommandDispatcher,
    CommandNotCancellable,
    CommandNotFound,
    CommandRecord,
    CommandStatus,
    CommandStore,
    IdempotencyConflict,
)

from .auth_store import SessionRecord
from .runtime import ERROR_RESPONSES, APIError
from .schemas import (
    CommandCancelResponse,
    CommandCreateRequest,
    CommandListResponse,
    CommandResponse,
)

IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{8,200}")


def create_commands_router(
    *,
    store: CommandStore,
    dispatcher: CommandDispatcher,
    require_scope: Callable[..., Callable[[SessionRecord], SessionRecord]],
) -> APIRouter:
    """Create the command router without owning runtime lifecycle resources."""

    router = APIRouter(prefix="/commands")

    @router.post(
        "",
        response_model=CommandResponse,
        operation_id="createCommand",
        status_code=202,
        responses=ERROR_RESPONSES,
    )
    async def create_command(
        payload: CommandCreateRequest,
        response: Response,
        session: Annotated[
            SessionRecord,
            Depends(require_scope("jobs:operate")),
        ],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CommandResponse:
        key = (idempotency_key or "").strip()
        if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
            raise APIError(
                "idempotency_key_required",
                "该命令需要有效的 Idempotency-Key。",
                status_code=422,
            )
        request_hash = store.request_hash(
            command_type=payload.command_type,
            schema_version=payload.schema_version,
            target=payload.target,
            payload=payload.payload,
            correlation_id=payload.correlation_id,
            expected_revision=payload.expected_revision,
        )
        try:
            command, created = await _to_thread(
                store.accept,
                idempotency_key=key,
                request_hash=request_hash,
                command_type=payload.command_type,
                schema_version=payload.schema_version,
                actor_id=session.actor_id,
                caller_role=session.role,
                scopes=session.scopes,
                target=payload.target,
                payload=payload.payload,
                correlation_id=payload.correlation_id,
                expected_revision=payload.expected_revision,
            )
        except IdempotencyConflict as exc:
            raise APIError(
                "idempotency_conflict",
                "该 Idempotency-Key 已用于不同命令。",
                status_code=409,
            ) from exc
        if created:
            dispatcher.schedule(command.command_id)
        else:
            response.status_code = 200
        return _response(command)

    @router.get(
        "",
        response_model=CommandListResponse,
        operation_id="listCommands",
        responses=ERROR_RESPONSES,
    )
    async def list_commands(
        session: Annotated[
            SessionRecord,
            Depends(require_scope("jobs:read")),
        ],
        status: Annotated[CommandStatus | None, Query()] = None,
        command_type: Annotated[str | None, Query(max_length=160)] = None,
        actor_id: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> CommandListResponse:
        owner = _query_actor(session, actor_id)
        commands = await _to_thread(
            store.list,
            actor_id=owner,
            status=status,
            command_type=command_type,
            limit=limit,
        )
        projections = tuple(_response(item) for item in commands)
        return CommandListResponse(commands=projections, count=len(projections))

    @router.get(
        "/{command_id}",
        response_model=CommandResponse,
        operation_id="getCommand",
        responses=ERROR_RESPONSES,
    )
    async def get_command(
        command_id: str,
        session: Annotated[
            SessionRecord,
            Depends(require_scope("jobs:read")),
        ],
    ) -> CommandResponse:
        command = await _owned_command(store, command_id, session)
        return _response(command)

    @router.post(
        "/{command_id}:cancel",
        response_model=CommandCancelResponse,
        operation_id="cancelCommand",
        responses=ERROR_RESPONSES,
    )
    async def cancel_command(
        command_id: str,
        session: Annotated[
            SessionRecord,
            Depends(require_scope("jobs:operate")),
        ],
    ) -> CommandCancelResponse:
        command = await _owned_command(store, command_id, session)
        try:
            command = await dispatcher.cancel(command)
        except CommandNotCancellable as exc:
            raise APIError(
                "command_not_cancellable",
                "当前命令不支持取消或已无法安全取消。",
                status_code=409,
            ) from exc
        return CommandCancelResponse(
            command=_response(command),
            cancellation_requested=(
                command.status not in TERMINAL_STATUSES
                and command.cancellation_requested
            ),
        )

    return router


async def _owned_command(
    store: CommandStore,
    command_id: str,
    session: SessionRecord,
) -> CommandRecord:
    try:
        command = await _to_thread(store.get, command_id)
    except CommandNotFound as exc:
        raise APIError(
            "resource_not_found",
            "请求的资源不存在。",
            status_code=404,
        ) from exc
    if session.role != "administrator" and command.actor_id != session.actor_id:
        raise APIError(
            "resource_not_found",
            "请求的资源不存在。",
            status_code=404,
        )
    return command


def _query_actor(session: SessionRecord, requested: str | None) -> str | None:
    if session.role == "administrator":
        return requested
    if requested is not None and requested != session.actor_id:
        raise APIError(
            "resource_not_found",
            "请求的资源不存在。",
            status_code=404,
        )
    return session.actor_id


def _response(command: CommandRecord) -> CommandResponse:
    return CommandResponse(
        command_id=command.command_id,
        command_type=command.command_type,
        actor_id=command.actor_id,
        status=command.status.value,
        target=command.target,
        created_at=command.created_at,
        accepted_at=command.accepted_at,
        started_at=command.started_at,
        finished_at=command.finished_at,
        result_event_id=command.result_event_id,
        result=command.result,
        error_code=command.error_code,
        safe_error_detail=command.safe_error_detail,
        correlation_id=command.correlation_id,
        attempt_count=command.attempt_count,
        cancellation_requested=command.cancellation_requested,
    )


async def _to_thread(function, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(function, *args, **kwargs)


__all__ = ["IDEMPOTENCY_KEY_PATTERN", "create_commands_router"]
