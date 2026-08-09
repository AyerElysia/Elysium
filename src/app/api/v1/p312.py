"""P3-12 主体观察、计划、能力与 Surface 公共 Facade。

Router 只处理 HTTP/WebSocket 合同；领域 owner 通过 ``P312Providers`` 注入，
因此 API 层不会创建第二套意识、记忆或 Surface 状态。
"""

# FastAPI 的依赖注入约定需要在参数默认值中调用 Depends。
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, WebSocket
from starlette.websockets import WebSocketDisconnect

from .auth_store import AuthStore, SessionRecord
from .runtime import ERROR_RESPONSES, APIError
from .schemas import (
    P312AbilitiesResponse,
    P312Ability,
    P312ActionRequest,
    P312ActionResponse,
    P312CommitmentSuggestionRequest,
    P312ConnectionActionRequest,
    P312Envelope,
    P312OccurrenceCancelRequest,
    P312Page,
    P312ProjectionRebuildRequest,
    P312ScheduleActionRequest,
    P312SurfaceTicketRequest,
    P312SurfaceTicketResponse,
    P312WorldObservationRequest,
)
from .tokens import SignedValueCodec, SignedValueError

P312_SUBPROTOCOL = "elysium.surface.v1"


def _result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"value": value}


async def _call(provider: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    if provider is None:
        raise APIError(
            "component_unavailable",
            "该领域能力当前未接入。",
            status_code=503,
            retryable=True,
        )
    function = getattr(provider, method, None)
    if not callable(function):
        raise APIError(
            "component_unavailable",
            "该领域能力当前未接入所需查询。",
            status_code=503,
            retryable=True,
        )
    try:
        value = function(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value
    except APIError:
        raise
    except KeyError as exc:
        raise APIError("resource_not_found", "请求的资源不存在。", status_code=404) from exc
    except ValueError as exc:
        raise APIError("resource_state_conflict", str(exc), status_code=409) from exc


def _admin_dependency(
    require_scope: Callable[..., Callable[[SessionRecord], SessionRecord]],
    *scopes: str,
) -> Callable[[SessionRecord], SessionRecord]:
    dependency = require_scope(*scopes)

    def guarded(session: SessionRecord = Depends(dependency)) -> SessionRecord:
        if session.role not in {"administrator", "platform_service"}:
            raise APIError("role_required", "该管理接口需要全能管理员身份。", status_code=403)
        return session

    return guarded


def _audit(auditor: Any, session: SessionRecord, action: str, resource: str) -> None:
    if auditor is None:
        return
    record = getattr(auditor, "record", None)
    if callable(record):
        value = record(
            actor_id=session.actor_id,
            session_id=session.session_id,
            action=action,
            resource=resource,
        )
        if inspect.isawaitable(value):
            raise RuntimeError("async auditor must be awaited by provider")


def _page(items: Any, *, limit: int, cursor: str | None = None) -> P312Page:
    values = tuple(dict(item) for item in (items or ()) if isinstance(item, dict))
    has_more = len(values) > limit
    selected = values[:limit]
    next_cursor = str(selected[-1].get("id") or selected[-1].get("sequence") or "") if has_more else None
    return P312Page(items=selected, next_cursor=next_cursor or None, has_more=has_more)


@dataclass(frozen=True, slots=True)
class P312Providers:
    """P3-12 领域 owner 依赖；未接入时显式返回 unavailable。"""

    consciousness: Any | None = None
    world: Any | None = None
    memory: Any | None = None
    commitments: Any | None = None
    autonomy: Any | None = None
    abilities: Any | None = None
    surfaces: Any | None = None
    auditor: Any | None = None


def create_p312_router(
    *,
    providers: P312Providers,
    require_scope: Callable[..., Callable[[SessionRecord], SessionRecord]],
    auth_store: AuthStore,
    codec: SignedValueCodec,
) -> APIRouter:
    """创建 P3-12 REST 路由；不持有任何领域生命周期。"""

    router = APIRouter()
    admin_read = _admin_dependency(require_scope, "consciousness:read")
    admin_operate = _admin_dependency(require_scope, "consciousness:operate")

    @router.get("/admin/consciousness/instances", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="queryAdminConsciousnessInstances")
    async def consciousness_instances(
        session: SessionRecord = Depends(admin_read),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> P312Page:
        _audit(providers.auditor, session, "read", "consciousness.instances")
        items = await _call(providers.consciousness, "list_instances")
        return _page(items, limit=limit)

    @router.get("/admin/consciousness/instances/{instance_id}", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminConsciousnessInstance")
    async def consciousness_instance(instance_id: str, session: SessionRecord = Depends(admin_read)) -> P312Envelope:
        _audit(providers.auditor, session, "read", f"consciousness.instance:{instance_id}")
        return P312Envelope(result=_result(await _call(providers.consciousness, "get_instance", instance_id)))

    @router.get("/admin/consciousness/streams/{stream_id}/owner", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminConsciousnessStreamOwner")
    async def consciousness_owner(stream_id: str, session: SessionRecord = Depends(admin_read)) -> P312Envelope:
        _audit(providers.auditor, session, "read", f"consciousness.stream:{stream_id}")
        return P312Envelope(result=_result(await _call(providers.consciousness, "get_stream_owner", stream_id)))

    @router.get("/admin/consciousness/health", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminConsciousnessHealth")
    async def consciousness_health(session: SessionRecord = Depends(admin_read)) -> P312Envelope:
        _audit(providers.auditor, session, "read", "consciousness.health")
        return P312Envelope(result=_result(await _call(providers.consciousness, "health")))

    async def consciousness_action(
        instance_id: str,
        action: str,
        body: P312ActionRequest,
        session: SessionRecord,
    ) -> P312ActionResponse:
        if instance_id == "chat_global":
            raise APIError("protected_instance", "chat_global 不允许通过公共管理接口控制。", status_code=403)
        result = await _call(
            providers.consciousness,
            action,
            instance_id,
            expected_revision=body.expected_revision,
            reason=body.reason,
        )
        return P312ActionResponse(accepted=bool(result is not False), result=_result(result))

    @router.post("/admin/consciousness/instances/{instance_id}:suspend", response_model=P312ActionResponse, responses=ERROR_RESPONSES, operation_id="suspendAdminConsciousnessInstance")
    async def suspend_consciousness(instance_id: str, body: P312ActionRequest, session: SessionRecord = Depends(admin_operate)) -> P312ActionResponse:
        _audit(providers.auditor, session, "operate", f"consciousness.instance:{instance_id}:suspend")
        return await consciousness_action(instance_id, "suspend", body, session)

    @router.post("/admin/consciousness/instances/{instance_id}:resume", response_model=P312ActionResponse, responses=ERROR_RESPONSES, operation_id="resumeAdminConsciousnessInstance")
    async def resume_consciousness(instance_id: str, body: P312ActionRequest, session: SessionRecord = Depends(admin_operate)) -> P312ActionResponse:
        _audit(providers.auditor, session, "operate", f"consciousness.instance:{instance_id}:resume")
        return await consciousness_action(instance_id, "resume", body, session)

    @router.post("/admin/consciousness/instances/{instance_id}:drain", response_model=P312ActionResponse, responses=ERROR_RESPONSES, operation_id="drainAdminConsciousnessInstance")
    async def drain_consciousness(instance_id: str, body: P312ActionRequest, session: SessionRecord = Depends(admin_operate)) -> P312ActionResponse:
        _audit(providers.auditor, session, "operate", f"consciousness.instance:{instance_id}:drain")
        return await consciousness_action(instance_id, "drain", body, session)

    world_read = _admin_dependency(require_scope, "world:read")
    world_observe = _admin_dependency(require_scope, "world:observe")
    world_maintain = _admin_dependency(require_scope, "world:maintain")

    @router.get("/admin/world/assertions", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="queryAdminWorldAssertions")
    async def world_assertions(session: SessionRecord = Depends(world_read), limit: int = Query(default=100, ge=1, le=500)) -> P312Page:
        _audit(providers.auditor, session, "read", "world.assertions")
        return _page(await _call(providers.world, "list_assertions", session=session), limit=limit)

    @router.get("/admin/world/changes", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="queryAdminWorldChanges")
    async def world_changes(session: SessionRecord = Depends(world_read), after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)) -> P312Page:
        _audit(providers.auditor, session, "read", "world.changes")
        return _page(await _call(providers.world, "changes_since", after, session=session), limit=limit)

    @router.get("/admin/world/health", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminWorldHealth")
    async def world_health(session: SessionRecord = Depends(world_read)) -> P312Envelope:
        _audit(providers.auditor, session, "read", "world.health")
        return P312Envelope(result=_result(await _call(providers.world, "health")))

    @router.post("/admin/world/observations", response_model=P312Envelope, status_code=201, responses=ERROR_RESPONSES, operation_id="appendAdminWorldObservation")
    async def world_observation(body: P312WorldObservationRequest, session: SessionRecord = Depends(world_observe)) -> P312Envelope:
        _audit(providers.auditor, session, "append", "world.observation")
        payload = body.model_dump(exclude={"schema_version"})
        return P312Envelope(result=_result(await _call(providers.world, "report_observation", **payload)))

    @router.post("/admin/world/projection:rebuild", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="rebuildAdminWorldProjection")
    async def world_rebuild(body: P312ProjectionRebuildRequest, session: SessionRecord = Depends(world_maintain)) -> P312Envelope:
        _audit(providers.auditor, session, "rebuild", "world.projection")
        return P312Envelope(result=_result(await _call(providers.world, "rebuild", batch_size=body.batch_size)))

    memory_read = _admin_dependency(require_scope, "memory:read")
    memory_summary = _admin_dependency(require_scope, "memory:summary")
    memory_maintain = _admin_dependency(require_scope, "memory:maintain_projection")

    @router.get("/admin/memory/search", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="searchAdminMemory")
    async def memory_search(
        query: str = Query(min_length=1, max_length=2000),
        top_k: int = Query(default=20, ge=1, le=100),
        session: SessionRecord = Depends(memory_read),
    ) -> P312Page:
        _audit(providers.auditor, session, "read_sensitive", "memory.search")
        return _page(await _call(providers.memory, "search", query, top_k=top_k, session=session), limit=top_k)

    @router.get("/admin/memory/experiences/{experience_id}", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminMemoryExperience")
    async def memory_experience(experience_id: str, session: SessionRecord = Depends(memory_read)) -> P312Envelope:
        _audit(providers.auditor, session, "read_sensitive", f"memory.experience:{experience_id}")
        return P312Envelope(result=_result(await _call(providers.memory, "get_experience", experience_id, session=session)))

    @router.get("/admin/memory/artifacts/{artifact_id}/versions", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="listAdminMemoryArtifactVersions")
    async def memory_versions(artifact_id: str, session: SessionRecord = Depends(memory_read), limit: int = Query(default=100, ge=1, le=500)) -> P312Page:
        _audit(providers.auditor, session, "read_sensitive", f"memory.artifact:{artifact_id}.versions")
        return _page(await _call(providers.memory, "artifact_versions", artifact_id, session=session), limit=limit)

    @router.get("/admin/memory/artifacts/{artifact_id}/versions/{version}", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminMemoryArtifactVersion")
    async def memory_version(artifact_id: str, version: int, session: SessionRecord = Depends(memory_read)) -> P312Envelope:
        _audit(providers.auditor, session, "read_sensitive", f"memory.artifact:{artifact_id}.version:{version}")
        return P312Envelope(result=_result(await _call(providers.memory, "artifact_version", artifact_id, version, session=session)))

    @router.get("/admin/memory/graph", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="getAdminMemoryGraph")
    async def memory_graph(session: SessionRecord = Depends(memory_summary), limit: int = Query(default=500, ge=1, le=1000)) -> P312Page:
        _audit(providers.auditor, session, "read_sensitive", "memory.graph")
        return _page(await _call(providers.memory, "graph", session=session), limit=limit)

    @router.get("/admin/memory/stats", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminMemoryStats")
    async def memory_stats(session: SessionRecord = Depends(memory_summary)) -> P312Envelope:
        _audit(providers.auditor, session, "read_sensitive", "memory.stats")
        return P312Envelope(result=_result(await _call(providers.memory, "stats", session=session)))

    @router.get("/admin/memory/health", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminMemoryHealth")
    async def memory_health(session: SessionRecord = Depends(memory_summary)) -> P312Envelope:
        _audit(providers.auditor, session, "read", "memory.health")
        return P312Envelope(result=_result(await _call(providers.memory, "health", session=session)))

    @router.post("/admin/memory/projections/{projection}:rebuild", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="rebuildAdminMemoryProjection")
    async def memory_rebuild(projection: str, session: SessionRecord = Depends(memory_maintain)) -> P312Envelope:
        _audit(providers.auditor, session, "rebuild", f"memory.projection:{projection}")
        return P312Envelope(result=_result(await _call(providers.memory, "rebuild_projection", projection, session=session)))

    commitment_read = _admin_dependency(require_scope, "commitments:read")
    commitment_schedule = _admin_dependency(require_scope, "commitments:operate_schedule")
    commitment_suggest = _admin_dependency(require_scope, "commitments:suggest")

    @router.get("/admin/commitments/todos", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="queryAdminCommitmentTodos")
    async def todos(session: SessionRecord = Depends(commitment_read), limit: int = Query(default=100, ge=1, le=500)) -> P312Page:
        _audit(providers.auditor, session, "read_sensitive", "commitments.todos")
        return _page(await _call(providers.commitments, "list_todos", session=session), limit=limit)

    @router.get("/admin/commitments/todos/{todo_id}", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminCommitmentTodo")
    async def todo(todo_id: str, session: SessionRecord = Depends(commitment_read)) -> P312Envelope:
        _audit(providers.auditor, session, "read_sensitive", f"commitments.todo:{todo_id}")
        return P312Envelope(result=_result(await _call(providers.commitments, "get_todo", todo_id, session=session)))

    @router.get("/admin/commitments/todos/{todo_id}/events", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="queryAdminCommitmentTodoEvents")
    async def todo_events(todo_id: str, session: SessionRecord = Depends(commitment_read), limit: int = Query(default=100, ge=1, le=500)) -> P312Page:
        _audit(providers.auditor, session, "read_sensitive", f"commitments.todo:{todo_id}.events")
        return _page(await _call(providers.commitments, "todo_events", todo_id, session=session), limit=limit)

    @router.get("/admin/commitments/schedules", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="queryAdminCommitmentSchedules")
    async def schedules(session: SessionRecord = Depends(commitment_read), limit: int = Query(default=100, ge=1, le=500)) -> P312Page:
        _audit(providers.auditor, session, "read", "commitments.schedules")
        return _page(await _call(providers.commitments, "list_schedules", session=session), limit=limit)

    @router.get("/admin/commitments/schedules/{record_id}", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminCommitmentSchedule")
    async def schedule(record_id: str, session: SessionRecord = Depends(commitment_read)) -> P312Envelope:
        _audit(providers.auditor, session, "read", f"commitments.schedule:{record_id}")
        return P312Envelope(result=_result(await _call(providers.commitments, "get_schedule", record_id, session=session)))

    @router.post("/admin/commitment-suggestions", response_model=P312Envelope, status_code=202, responses=ERROR_RESPONSES, operation_id="appendAdminCommitmentSuggestion")
    async def suggestion(body: P312CommitmentSuggestionRequest, session: SessionRecord = Depends(commitment_suggest)) -> P312Envelope:
        _audit(providers.auditor, session, "append", "commitments.external_suggestion")
        payload = body.model_dump(exclude={"schema_version"})
        payload["actor_id"] = session.actor_id
        return P312Envelope(result=_result(await _call(providers.commitments, "suggest", **payload)))

    async def schedule_action(record_id: str, action: str, body: P312ScheduleActionRequest, session: SessionRecord) -> P312ActionResponse:
        result = await _call(providers.commitments, action, record_id, expected_revision=body.expected_revision, reason=body.reason)
        return P312ActionResponse(accepted=bool(result is not False), result=_result(result))

    @router.post("/admin/commitments/schedules/{record_id}:pause", response_model=P312ActionResponse, responses=ERROR_RESPONSES, operation_id="pauseAdminCommitmentSchedule")
    async def pause_schedule(record_id: str, body: P312ScheduleActionRequest, session: SessionRecord = Depends(commitment_schedule)) -> P312ActionResponse:
        _audit(providers.auditor, session, "operate", f"commitments.schedule:{record_id}:pause")
        return await schedule_action(record_id, "pause", body, session)

    @router.post("/admin/commitments/schedules/{record_id}:resume", response_model=P312ActionResponse, responses=ERROR_RESPONSES, operation_id="resumeAdminCommitmentSchedule")
    async def resume_schedule(record_id: str, body: P312ScheduleActionRequest, session: SessionRecord = Depends(commitment_schedule)) -> P312ActionResponse:
        _audit(providers.auditor, session, "operate", f"commitments.schedule:{record_id}:resume")
        return await schedule_action(record_id, "resume", body, session)

    autonomy_read = _admin_dependency(require_scope, "autonomy:read")
    autonomy_cancel = _admin_dependency(require_scope, "autonomy:cancel_occurrence")

    @router.get("/admin/autonomy/intents", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="queryAdminAutonomyIntents")
    async def intents(session: SessionRecord = Depends(autonomy_read), limit: int = Query(default=100, ge=1, le=500)) -> P312Page:
        _audit(providers.auditor, session, "read_sensitive", "autonomy.intents")
        return _page(await _call(providers.autonomy, "list_intents", session=session), limit=limit)

    @router.get("/admin/autonomy/intents/{intent_id}", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAdminAutonomyIntent")
    async def intent(intent_id: str, session: SessionRecord = Depends(autonomy_read)) -> P312Envelope:
        _audit(providers.auditor, session, "read_sensitive", f"autonomy.intent:{intent_id}")
        return P312Envelope(result=_result(await _call(providers.autonomy, "get_intent", intent_id, session=session)))

    @router.get("/admin/autonomy/intents/{intent_id}/occurrences", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="queryAdminAutonomyOccurrences")
    async def occurrences(intent_id: str, session: SessionRecord = Depends(autonomy_read), limit: int = Query(default=100, ge=1, le=500)) -> P312Page:
        _audit(providers.auditor, session, "read_sensitive", f"autonomy.intent:{intent_id}.occurrences")
        return _page(await _call(providers.autonomy, "occurrences", intent_id, session=session), limit=limit)

    @router.post("/admin/autonomy/occurrences/{occurrence_id}:cancel", response_model=P312ActionResponse, responses=ERROR_RESPONSES, operation_id="cancelAdminAutonomyOccurrence")
    async def cancel_occurrence(occurrence_id: str, body: P312OccurrenceCancelRequest, session: SessionRecord = Depends(autonomy_cancel)) -> P312ActionResponse:
        _audit(providers.auditor, session, "operate", f"autonomy.occurrence:{occurrence_id}:cancel")
        result = await _call(providers.autonomy, "cancel_occurrence", occurrence_id, reason=body.reason, actor_id=session.actor_id)
        return P312ActionResponse(accepted=bool(result is not False), result=_result(result))

    @router.get("/abilities", response_model=P312AbilitiesResponse, responses=ERROR_RESPONSES, operation_id="listAbilities")
    async def abilities(session: SessionRecord = Depends(require_scope("abilities:read"))) -> P312AbilitiesResponse:
        values = await _call(providers.abilities, "list_abilities", session=session)
        return P312AbilitiesResponse(abilities=tuple(P312Ability(**item) for item in values or ()))

    @router.get("/abilities/{ability_id}", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getAbility")
    async def ability(ability_id: str, session: SessionRecord = Depends(require_scope("abilities:read"))) -> P312Envelope:
        return P312Envelope(result=_result(await _call(providers.abilities, "get_ability", ability_id, session=session)))

    surface_read = require_scope("surface:read")
    surface_connect = require_scope("surface:connect")
    surface_admin = _admin_dependency(require_scope, "surface:admin")

    @router.get("/surfaces", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="listSurfaces")
    async def surfaces(session: SessionRecord = Depends(surface_read), limit: int = Query(default=100, ge=1, le=500)) -> P312Page:
        return _page(await _call(providers.surfaces, "list_surfaces", session=session), limit=limit)

    @router.get("/surfaces/{surface_id}/status", response_model=P312Envelope, responses=ERROR_RESPONSES, operation_id="getSurfaceStatus")
    async def surface_status(surface_id: str, session: SessionRecord = Depends(surface_read)) -> P312Envelope:
        return P312Envelope(result=_result(await _call(providers.surfaces, "status", surface_id, session=session)))

    @router.post("/surfaces/{surface_id}/tickets", response_model=P312SurfaceTicketResponse, responses=ERROR_RESPONSES, operation_id="createSurfaceTicket")
    async def surface_ticket(surface_id: str, body: P312SurfaceTicketRequest, session: SessionRecord = Depends(surface_connect)) -> P312SurfaceTicketResponse:
        resource = f"/api/v1/surfaces/{surface_id}/ws"
        scopes = ("surface:connect", "surface:input") if body.input_enabled else ("surface:connect",)
        try:
            ticket, token = auth_store.issue_ws_ticket(
                session=session,
                codec=codec,
                resource=resource,
                subprotocol=P312_SUBPROTOCOL,
                scopes=scopes,
                origin=body.origin,
                ttl=timedelta(minutes=1),
            )
        except (SignedValueError, TypeError, ValueError) as exc:
            raise APIError("forbidden", "无法为目标 Surface 创建 ticket。", status_code=403) from exc
        return P312SurfaceTicketResponse(ticket=token, resource=resource, subprotocol=P312_SUBPROTOCOL, scopes=ticket.scopes, expires_at=ticket.expires_at.isoformat())

    @router.websocket("/surfaces/{surface_id}/ws", name="p312SurfaceWebsocket")
    async def surface_socket(websocket: WebSocket, surface_id: str) -> None:
        resource = f"/api/v1/surfaces/{surface_id}/ws"
        offered = websocket.headers.get("sec-websocket-protocol", "").split(",", 1)[0].strip()
        if offered != P312_SUBPROTOCOL:
            await websocket.close(code=1008, reason="invalid surface subprotocol")
            return
        try:
            grant = await asyncio.to_thread(auth_store.consume_ws_ticket, token=websocket.query_params.get("ticket", ""), codec=codec, resource=resource, subprotocol=P312_SUBPROTOCOL, origin=websocket.headers.get("origin"))
            if "surface:connect" not in grant.scopes:
                raise ValueError("surface_scope_required")
        except (SignedValueError, TypeError, ValueError):
            await websocket.close(code=1008, reason="invalid surface ticket")
            return
        await websocket.accept(subprotocol=P312_SUBPROTOCOL)
        try:
            await _call(providers.surfaces, "serve", websocket, surface_id=surface_id, grant=grant)
        except WebSocketDisconnect:
            return

    @router.get("/admin/surfaces/{surface_id}/connections", response_model=P312Page, responses=ERROR_RESPONSES, operation_id="queryAdminSurfaceConnections")
    async def surface_connections(surface_id: str, session: SessionRecord = Depends(surface_admin), limit: int = Query(default=100, ge=1, le=500)) -> P312Page:
        _audit(providers.auditor, session, "read_sensitive", f"surface:{surface_id}.connections")
        return _page(await _call(providers.surfaces, "connections", surface_id, session=session), limit=limit)

    @router.post("/admin/surfaces/{surface_id}/connections/{connection_id}:disconnect", response_model=P312ActionResponse, responses=ERROR_RESPONSES, operation_id="disconnectAdminSurfaceConnection")
    async def surface_disconnect(surface_id: str, connection_id: str, body: P312ConnectionActionRequest, session: SessionRecord = Depends(surface_admin)) -> P312ActionResponse:
        _audit(providers.auditor, session, "operate", f"surface:{surface_id}.connection:{connection_id}:disconnect")
        result = await _call(providers.surfaces, "disconnect", surface_id, connection_id, reason=body.reason, session=session)
        return P312ActionResponse(accepted=bool(result is not False), result=_result(result))

    return router


__all__ = ["P312_SUBPROTOCOL", "P312Providers", "create_p312_router"]
