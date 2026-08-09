"""P3-11 管理总览、访问、集成与作业路由。"""

# FastAPI dependencies are intentionally declared in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from src.kernel.commands import CommandNotFound, CommandStatus, CommandStore

from .admin_store import AdminStore
from .auth_store import (
    AuthStore,
    CredentialRecord,
    ManagedSessionRecord,
    SessionRecord,
)
from .commands import _response
from .foundation import FoundationProjection
from .runtime import ERROR_RESPONSES, APIError
from .schemas.admin import (
    AdminComponent,
    AdminCredential,
    AdminCredentialCreate,
    AdminCredentialSecret,
    AdminIntegrationTestRequest,
    AdminMetric,
    AdminOverview,
    AdminPage,
    AdminSession,
    AdminSettings,
    AdminSettingsPatch,
    AdminSettingsValidateRequest,
    AdminSettingsValidation,
    AdminSyncStatus,
)
from .schemas.common import utc_now


class AdminFacade:
    """组合已导出的安全投影；不读取配置、日志文件或插件私有对象。"""

    def __init__(
        self,
        *,
        foundation: FoundationProjection,
        auth: AuthStore,
        admin: AdminStore,
        commands: CommandStore,
        integration_tests: dict[str, Callable[[str], Any]] | None = None,
    ) -> None:
        self.foundation = foundation
        self.auth = auth
        self.admin = admin
        self.commands = commands
        self.integration_tests = dict(integration_tests or {})


def create_admin_router(
    *,
    facade: AdminFacade,
    require_scope: Callable[..., Callable[[SessionRecord], SessionRecord]],
) -> APIRouter:
    """创建统一要求全能管理员身份的管理路由。"""

    router = APIRouter(prefix="/admin")

    def guarded(*scopes: str) -> Callable[..., Any]:
        scoped = require_scope(*scopes)

        async def require_admin(
            session: SessionRecord = Depends(scoped),
        ) -> SessionRecord:
            if session.role != "administrator":
                raise APIError(
                    "role_required",
                    "该接口仅允许全能管理员调用。",
                    status_code=403,
                )
            return session

        return require_admin

    def page(items: tuple[dict[str, Any], ...]) -> AdminPage:
        return AdminPage(data=items, count=len(items))

    @router.get(
        "/overview",
        response_model=AdminOverview,
        operation_id="getAdminOverview",
        responses=ERROR_RESPONSES,
    )
    async def overview(
        session: SessionRecord = Depends(guarded("admin:overview")),
    ) -> AdminOverview:
        del session
        snapshot = facade.foundation.snapshot()
        command_counts = {
            status.value: len(
                facade.commands.list(actor_id=None, status=status, limit=200)
            )
            for status in CommandStatus
        }
        incidents = _incidents(snapshot.modules)
        return AdminOverview(
            node_id=snapshot.node_id,
            state=_aggregate_state((*snapshot.modules, *snapshot.adapters)),
            generated_at=snapshot.generated_at,
            components=snapshot.modules,
            adapters=snapshot.adapters,
            command_counts=command_counts,
            active_incidents=len(incidents),
            audit_events=len(facade.admin.audits(limit=500)),
        )

    @router.get(
        "/components",
        response_model=AdminPage,
        operation_id="listAdminComponents",
        responses=ERROR_RESPONSES,
    )
    async def components(
        session: SessionRecord = Depends(guarded("admin:overview")),
    ) -> AdminPage:
        del session
        snapshot = facade.foundation.snapshot()
        return page(tuple(item.model_dump(mode="json") for item in snapshot.modules))

    @router.get(
        "/components/{component_id}",
        response_model=AdminComponent,
        operation_id="getAdminComponent",
        responses=ERROR_RESPONSES,
    )
    async def component(
        component_id: str,
        session: SessionRecord = Depends(guarded("admin:overview")),
    ) -> AdminComponent:
        del session
        snapshot = facade.foundation.snapshot()
        value = next(
            (item for item in snapshot.modules if item.component == component_id), None
        )
        if value is None:
            raise APIError(
                "resource_not_found", "请求的组件不存在。", status_code=404
            )
        return AdminComponent(
            component=value,
            details={"read_only": True},
            generated_at=snapshot.generated_at,
        )

    @router.get(
        "/metrics",
        response_model=AdminPage,
        operation_id="listAdminMetrics",
        responses=ERROR_RESPONSES,
    )
    async def metrics(
        session: SessionRecord = Depends(guarded("metrics:read")),
    ) -> AdminPage:
        del session
        now = utc_now()
        total = len(facade.commands.list(actor_id=None, limit=200))
        active = sum(
            len(facade.commands.list(actor_id=None, status=status, limit=200))
            for status in (CommandStatus.ACCEPTED, CommandStatus.EXECUTING)
        )
        return page(
            (
                AdminMetric(
                    name="commands.total_bounded",
                    value=float(total),
                    unit="count",
                    generated_at=now,
                ).model_dump(mode="json"),
                AdminMetric(
                    name="commands.active",
                    value=float(active),
                    unit="count",
                    generated_at=now,
                ).model_dump(mode="json"),
            )
        )

    @router.get(
        "/incidents",
        response_model=AdminPage,
        operation_id="listAdminIncidents",
        responses=ERROR_RESPONSES,
    )
    async def incidents(
        session: SessionRecord = Depends(guarded("admin:overview")),
    ) -> AdminPage:
        del session
        return page(_incidents(facade.foundation.snapshot().modules))

    @router.get(
        "/audit-events",
        response_model=AdminPage,
        operation_id="listAdminAuditEvents",
        responses=ERROR_RESPONSES,
    )
    async def audit_events(
        session: SessionRecord = Depends(guarded("admin:audit")),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> AdminPage:
        del session
        return page(facade.admin.audits(limit=limit))

    @router.get(
        "/audit-events/{audit_id}",
        response_model=dict[str, Any],
        operation_id="getAdminAuditEvent",
        responses=ERROR_RESPONSES,
    )
    async def audit_event(
        audit_id: str,
        session: SessionRecord = Depends(guarded("admin:audit")),
    ) -> dict[str, Any]:
        del session
        values = facade.admin.audits(limit=1, audit_id=audit_id)
        if not values:
            raise APIError(
                "resource_not_found", "请求的审计事件不存在。", status_code=404
            )
        return values[0]

    @router.get(
        "/logs",
        response_model=AdminPage,
        operation_id="listAdminLogs",
        responses=ERROR_RESPONSES,
    )
    async def logs(
        session: SessionRecord = Depends(guarded("admin:logs")),
        component: str | None = None,
        level: str | None = None,
        request_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> AdminPage:
        del session
        return page(
            facade.admin.logs(
                component=component,
                level=level,
                request_id=request_id,
                limit=limit,
            )
        )

    @router.get(
        "/sync",
        response_model=AdminSyncStatus,
        operation_id="getAdminSyncStatus",
        responses=ERROR_RESPONSES,
    )
    async def sync_status(
        session: SessionRecord = Depends(guarded("sync:read")),
    ) -> AdminSyncStatus:
        del session
        snapshot = facade.foundation.snapshot()
        value = next(
            (item for item in snapshot.modules if item.component == "remote_sync"),
            None,
        )
        if value is None:
            return AdminSyncStatus(
                state="unavailable",
                mode="unavailable",
                backlog=0,
                degraded_reason="同步 owner 未接入管理投影。",
                generated_at=snapshot.generated_at,
            )
        return AdminSyncStatus(
            state=value.state,
            mode="managed",
            backlog=value.backlog or 0,
            last_success_at=value.last_success_at,
            degraded_reason=value.degraded_reason,
            generated_at=snapshot.generated_at,
        )

    @router.get(
        "/auth/sessions",
        response_model=AdminPage,
        operation_id="listAdminSessions",
        responses=ERROR_RESPONSES,
    )
    async def sessions(
        session: SessionRecord = Depends(guarded("admin:session")),
    ) -> AdminPage:
        del session
        return page(
            tuple(
                _session(item).model_dump(mode="json")
                for item in facade.auth.list_sessions()
            )
        )

    @router.delete(
        "/auth/sessions/{session_id}",
        response_model=dict[str, bool],
        operation_id="revokeAdminSession",
        responses=ERROR_RESPONSES,
    )
    async def revoke_session(
        session_id: str,
        request: Request,
        session: SessionRecord = Depends(guarded("admin:session")),
    ) -> dict[str, bool]:
        revoked = facade.auth.revoke_session(session_id)
        facade.admin.append_audit(
            event_type="admin.session.revoked",
            actor_id=session.actor_id,
            target_type="session",
            target_id=session_id,
            outcome="succeeded" if revoked else "not_found",
            request_id=request.state.request_id,
        )
        if not revoked:
            raise APIError(
                "resource_not_found", "请求的会话不存在。", status_code=404
            )
        return {"revoked": True}

    @router.get(
        "/credentials",
        response_model=AdminPage,
        operation_id="listAdminCredentials",
        responses=ERROR_RESPONSES,
    )
    async def credentials(
        session: SessionRecord = Depends(guarded("admin:credential")),
    ) -> AdminPage:
        del session
        return page(
            tuple(
                _credential(item).model_dump(mode="json")
                for item in facade.auth.list_credentials()
            )
        )

    @router.post(
        "/credentials",
        response_model=AdminCredentialSecret,
        status_code=201,
        operation_id="createAdminCredential",
        responses=ERROR_RESPONSES,
    )
    async def create_credential(
        body: AdminCredentialCreate,
        request: Request,
        session: SessionRecord = Depends(guarded("admin:credential")),
    ) -> AdminCredentialSecret:
        try:
            credential, secret = facade.auth.create_credential_secret(
                actor_id=body.actor_id,
                scopes=body.scopes,
                resource_grants=body.resource_grants,
            )
        except ValueError as exc:
            raise APIError(
                "validation_failed",
                "服务凭据 scope 不符合协议。",
                status_code=422,
            ) from exc
        facade.admin.append_audit(
            event_type="admin.credential.created",
            actor_id=session.actor_id,
            target_type="credential",
            target_id=credential.credential_id,
            outcome="succeeded",
            request_id=request.state.request_id,
        )
        return AdminCredentialSecret(
            credential=_credential(credential), secret=secret
        )

    @router.post(
        "/credentials/{credential_id}:rotate",
        response_model=AdminCredentialSecret,
        operation_id="rotateAdminCredential",
        responses=ERROR_RESPONSES,
    )
    async def rotate_credential(
        credential_id: str,
        request: Request,
        session: SessionRecord = Depends(guarded("admin:credential")),
    ) -> AdminCredentialSecret:
        try:
            credential, secret = facade.auth.rotate_credential_secret(credential_id)
        except ValueError as exc:
            raise APIError(
                "resource_not_found",
                "请求的凭据不存在或已撤销。",
                status_code=404,
            ) from exc
        facade.admin.append_audit(
            event_type="admin.credential.rotated",
            actor_id=session.actor_id,
            target_type="credential",
            target_id=credential_id,
            outcome="succeeded",
            safe_detail=f"replacement:{credential.credential_id}",
            request_id=request.state.request_id,
        )
        return AdminCredentialSecret(
            credential=_credential(credential), secret=secret
        )

    @router.delete(
        "/credentials/{credential_id}",
        response_model=dict[str, bool],
        operation_id="revokeAdminCredential",
        responses=ERROR_RESPONSES,
    )
    async def revoke_credential(
        credential_id: str,
        request: Request,
        session: SessionRecord = Depends(guarded("admin:credential")),
    ) -> dict[str, bool]:
        revoked = facade.auth.revoke_credential(credential_id)
        facade.admin.append_audit(
            event_type="admin.credential.revoked",
            actor_id=session.actor_id,
            target_type="credential",
            target_id=credential_id,
            outcome="succeeded" if revoked else "not_found",
            request_id=request.state.request_id,
        )
        if not revoked:
            raise APIError(
                "resource_not_found", "请求的凭据不存在。", status_code=404
            )
        return {"revoked": True}

    @router.get(
        "/settings",
        response_model=AdminSettings,
        operation_id="getAdminSettings",
        responses=ERROR_RESPONSES,
    )
    async def settings(
        session: SessionRecord = Depends(guarded("admin:settings")),
    ) -> AdminSettings:
        del session
        return _settings(facade)

    @router.post(
        "/settings:validate",
        response_model=AdminSettingsValidation,
        operation_id="validateAdminSettings",
        responses=ERROR_RESPONSES,
    )
    async def validate_settings(
        body: AdminSettingsValidateRequest,
        session: SessionRecord = Depends(guarded("admin:settings")),
    ) -> AdminSettingsValidation:
        del session
        values, errors = facade.admin.validate_settings(body.values)
        return AdminSettingsValidation(
            valid=not errors, errors=errors, values=values
        )

    @router.patch(
        "/settings",
        response_model=AdminSettings,
        operation_id="updateAdminSettings",
        responses=ERROR_RESPONSES,
    )
    async def update_settings(
        body: AdminSettingsPatch,
        request: Request,
        session: SessionRecord = Depends(guarded("admin:settings")),
    ) -> AdminSettings:
        try:
            revision, values = facade.admin.update_settings(
                actor_id=session.actor_id,
                expected_revision=body.expected_revision,
                values=body.values,
                request_id=request.state.request_id,
            )
        except ValueError as exc:
            if str(exc) == "revision_conflict":
                raise APIError(
                    "revision_conflict",
                    "设置 revision 已变化。",
                    status_code=409,
                ) from exc
            raise APIError(
                "validation_failed",
                "设置候选值不符合 allowlist schema。",
                status_code=422,
            ) from exc
        return AdminSettings(revision=revision, settings=values)

    @router.get(
        "/integrations",
        response_model=AdminPage,
        operation_id="listAdminIntegrations",
        responses=ERROR_RESPONSES,
    )
    async def integrations(
        session: SessionRecord = Depends(guarded("integration:read")),
    ) -> AdminPage:
        del session
        return page(_integrations(facade))

    @router.get(
        "/integrations/{integration_id}",
        response_model=dict[str, Any],
        operation_id="getAdminIntegration",
        responses=ERROR_RESPONSES,
    )
    async def integration(
        integration_id: str,
        session: SessionRecord = Depends(guarded("integration:read")),
    ) -> dict[str, Any]:
        del session
        value = next(
            (
                item
                for item in _integrations(facade)
                if item["integration_id"] == integration_id
            ),
            None,
        )
        if value is None:
            raise APIError(
                "resource_not_found", "请求的集成不存在。", status_code=404
            )
        return value

    @router.get(
        "/integrations/{integration_id}/events",
        response_model=AdminPage,
        operation_id="listAdminIntegrationEvents",
        responses=ERROR_RESPONSES,
    )
    async def integration_events(
        integration_id: str,
        session: SessionRecord = Depends(guarded("integration:read")),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> AdminPage:
        del session
        return page(facade.admin.integration_events(integration_id, limit=limit))

    @router.post(
        "/integrations/{integration_id}:test",
        response_model=dict[str, Any],
        operation_id="testAdminIntegration",
        responses=ERROR_RESPONSES,
    )
    async def test_integration(
        integration_id: str,
        body: AdminIntegrationTestRequest,
        request: Request,
        session: SessionRecord = Depends(guarded("integration:test")),
    ) -> dict[str, Any]:
        checker = facade.integration_tests.get(integration_id)
        if checker is None:
            raise APIError(
                "capability_disabled",
                "该集成没有登记安全测试 owner。",
                status_code=404,
            )
        result = checker(body.check)
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=5.0)
        safe = dict(result) if isinstance(result, dict) else {"ok": bool(result)}
        forbidden = {"secret", "token", "url", "path", "authorization"}
        safe = {key: value for key, value in safe.items() if key not in forbidden}
        facade.admin.append_integration_event(
            integration_id=integration_id,
            event_type="integration.test.completed",
            state="ready" if safe.get("ok", True) else "degraded",
        )
        facade.admin.append_audit(
            event_type="admin.integration.tested",
            actor_id=session.actor_id,
            target_type="integration",
            target_id=integration_id,
            outcome="succeeded",
            safe_detail=f"check:{body.check}",
            request_id=request.state.request_id,
        )
        return {
            "integration_id": integration_id,
            "check": body.check,
            "result": safe,
            "reconnect_performed": False,
        }

    @router.get(
        "/jobs",
        response_model=AdminPage,
        operation_id="listAdminJobs",
        responses=ERROR_RESPONSES,
    )
    async def jobs(
        session: SessionRecord = Depends(guarded("jobs:read")),
        status: CommandStatus | None = None,
        limit: int = Query(default=100, ge=1, le=200),
    ) -> AdminPage:
        del session
        values = facade.commands.list(actor_id=None, status=status, limit=limit)
        return page(tuple(_job(value) for value in values))

    @router.get(
        "/jobs/{job_id}",
        response_model=dict[str, Any],
        operation_id="getAdminJob",
        responses=ERROR_RESPONSES,
    )
    async def job(
        job_id: str,
        session: SessionRecord = Depends(guarded("jobs:read")),
    ) -> dict[str, Any]:
        del session
        try:
            return _job(facade.commands.get(job_id))
        except CommandNotFound as exc:
            raise APIError(
                "resource_not_found", "请求的作业不存在。", status_code=404
            ) from exc

    return router


def _credential(item: CredentialRecord) -> AdminCredential:
    return AdminCredential(
        credential_id=item.credential_id,
        actor_id=item.actor_id,
        audience=item.audience,
        role=item.role,
        scopes=item.scopes,
        resource_grants=item.resource_grants,
        created_at=item.created_at,
        revoked_at=item.revoked_at,
    )


def _session(item: ManagedSessionRecord) -> AdminSession:
    value = item.session
    return AdminSession(
        session_id=value.session_id,
        actor_id=value.actor_id,
        audience=value.audience,
        role=value.role,
        scopes=value.scopes,
        credential_id=value.credential_id,
        access_expires_at=value.access_expires_at,
        refresh_expires_at=value.refresh_expires_at,
        revoked_at=value.revoked_at,
        created_at=item.created_at,
    )


def _settings(facade: AdminFacade) -> AdminSettings:
    revision, values = facade.admin.settings()
    return AdminSettings(revision=revision, settings=values)


def _integrations(facade: AdminFacade) -> tuple[dict[str, Any], ...]:
    snapshot = facade.foundation.snapshot()
    return tuple(
        {
            "integration_id": item.provider,
            "component": item.component,
            "state": item.state,
            "enabled": item.enabled,
            "connected": item.connected,
            "degraded_reason": item.degraded_reason,
            "safe_tests": ["health", "capabilities", "permissions"]
            if item.provider in facade.integration_tests
            else [],
            "reconnect_available": False,
        }
        for item in snapshot.adapters
    )


def _incidents(components: tuple[Any, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "incident_id": f"incident:{item.component}",
            "component": item.component,
            "state": item.state,
            "summary": item.degraded_reason or "组件未就绪。",
            "active": True,
        }
        for item in components
        if item.state in {"failed", "degraded", "unavailable"}
    )


def _aggregate_state(items: tuple[Any, ...]) -> str:
    states = {item.state for item in items}
    if "failed" in states:
        return "failed"
    if states & {"degraded", "unavailable"}:
        return "degraded"
    return "ready"


def _job(command: Any) -> dict[str, Any]:
    result = _response(command).model_dump(mode="json")
    result.update(
        {
            "job_id": command.command_id,
            "owner": command.command_type.split(".", 1)[0],
            "retryable": command.status
            in {
                CommandStatus.FAILED,
                CommandStatus.REJECTED,
                CommandStatus.EXPIRED,
            },
        }
    )
    return result


__all__ = ["AdminFacade", "create_admin_router"]
