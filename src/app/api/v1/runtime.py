"""API v1 应用工厂、公共错误和认证依赖。"""

import asyncio
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from src.kernel.commands import CommandDispatcher, CommandStore
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .auth_store import AuthStore, SessionRecord
from .foundation import FoundationProjection
from .policy import ALL_EXPORTED_SCOPES
from .schemas import (
    BootstrapResponse,
    CallerIdentity,
    CapabilitiesResponse,
    ErrorBody,
    ErrorResponse,
    HealthResponse,
    LogoutResponse,
    ReadinessResponse,
    SessionCreateRequest,
    SessionRefreshRequest,
    SessionResponse,
    WSTicketRequest,
    WSTicketResponse,
)
from .tokens import SignedValueCodec, SignedValueError

REQUEST_ID_HEADER = "X-Request-ID"
AUTH_HEADER = "Authorization"
DEFAULT_ACCESS_TTL = timedelta(minutes=15)
DEFAULT_REFRESH_TTL = timedelta(days=7)
DEFAULT_TICKET_TTL = timedelta(minutes=1)
MAX_BODY_BYTES = 1 * 1024 * 1024
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "认证失败"},
    403: {"model": ErrorResponse, "description": "权限不足"},
    404: {"model": ErrorResponse, "description": "能力或资源不存在"},
    409: {"model": ErrorResponse, "description": "幂等键或资源状态冲突"},
    413: {"model": ErrorResponse, "description": "请求体超过上限"},
    415: {"model": ErrorResponse, "description": "媒体类型不受支持"},
    422: {"model": ErrorResponse, "description": "请求不符合协议"},
    429: {"model": ErrorResponse, "description": "资源预算已耗尽"},
    500: {"model": ErrorResponse, "description": "内部错误"},
}
WS_RESOURCE_SCOPES = {
    "/api/v1/events/ws": "events:read",
    "/api/v1/livestream/stage/ws": "livestream:read",
    "/api/v1/voice-calls/{call_id}/ws": "voice_call:operate",
    "/api/v1/voice-calls/{call_id}/observe": "voice_call:observe",
    "/api/v1/tabletop/rooms/{room_id}/ws": "tabletop:play",
    "/api/v1/admin/voice-calls/{call_id}/observe": "voice_call:admin",
    "/api/v1/surfaces/{surface_id}/ws": "surface:connect",
}


class APIError(Exception):
    """可安全暴露给调用方的协议错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class APIContext:
    """API 应用的可注入运行依赖。"""

    store: AuthStore
    codec: SignedValueCodec
    installation_id: str
    access_ttl: timedelta = DEFAULT_ACCESS_TTL
    refresh_ttl: timedelta = DEFAULT_REFRESH_TTL
    ticket_ttl: timedelta = DEFAULT_TICKET_TTL
    allowed_origins: tuple[str, ...] = ()
    max_concurrency: int = 32
    max_websocket_connections: int = 64
    foundation: FoundationProjection | None = None
    command_store: CommandStore | None = None
    command_dispatcher: CommandDispatcher | None = None


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每次请求生成或保留安全的 request id。"""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER, "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,100}", request_id):
            request_id = f"req_{secrets.token_urlsafe(16)}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class WebSocketConnectionBudget:
    """后续 WS 领域路由共享的严格有界连接预算。"""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("WebSocket connection limit must be positive")
        self._limit = limit
        self._active = 0
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """返回当前是否仍有连接槽位。"""

        return self._active < self._limit

    async def acquire(self) -> None:
        """原子取得连接槽位；无槽位时立即显式失败。"""

        async with self._lock:
            if self._active >= self._limit:
                raise APIError(
                    "connection_limit_reached",
                    "实时连接数已达到上限。",
                    status_code=429,
                    retryable=True,
                )
            self._active += 1

    def release(self) -> None:
        """归还连接槽位；重复释放属于调用方协议错误。"""

        if self._active <= 0:
            raise RuntimeError("WebSocket connection budget released without ownership")
        self._active -= 1


class ConcurrencyLimitMiddleware(BaseHTTPMiddleware):
    """严格限制应用内并发请求，不建立无界等待队列。"""

    def __init__(self, app: Any, max_concurrency: int) -> None:
        super().__init__(app)
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._limit = max_concurrency
        self._active = 0
        self._lock = asyncio.Lock()

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        async with self._lock:
            if self._active >= self._limit:
                return _error_response(
                    request,
                    APIError(
                        "request_overloaded",
                        "服务当前请求过多。",
                        status_code=429,
                        retryable=True,
                    ),
                )
            self._active += 1
        try:
            return await call_next(request)
        finally:
            async with self._lock:
                self._active -= 1


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """在读取 body 前拒绝声明超过上限的请求。"""

    def __init__(
        self,
        app: Any,
        max_bytes: int = MAX_BODY_BYTES,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
    ) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes
        self._max_upload_bytes = max_upload_bytes

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        limit = (
            self._max_upload_bytes
            if request.url.path.startswith("/media/uploads")
            else self._max_bytes
        )
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    return self._reject(request)
            except ValueError:
                return self._reject(request)
        if request.method in {"POST", "PUT", "PATCH"}:
            if not request.url.path.startswith("/media/uploads"):
                media_type = request.headers.get("content-type", "").split(";", 1)[0]
                if media_type != "application/json":
                    return _error_response(
                        request,
                        APIError(
                            "unsupported_media_type",
                            "该接口只接受 application/json。",
                            status_code=415,
                        ),
                    )
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    return self._reject(request)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        return await call_next(request)

    @staticmethod
    def _reject(request: Request) -> JSONResponse:
        request_id = getattr(
            request.state,
            "request_id",
            f"req_{secrets.token_urlsafe(16)}",
        )
        request.state.request_id = request_id
        return _error_response(
            request,
            APIError(
                "payload_too_large",
                "请求体超过允许大小。",
                status_code=413,
            ),
        )


def _error_response(request: Request, error: APIError) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=error.code,
            message=error.message,
            request_id=request.state.request_id,
            retryable=error.retryable,
        )
    )
    return JSONResponse(
        status_code=error.status_code,
        content=body.model_dump(mode="json"),
        headers={REQUEST_ID_HEADER: request.state.request_id},
    )


def _identity(session: SessionRecord) -> CallerIdentity:
    role = session.role
    if role not in {"user", "administrator", "platform_service"}:
        raise APIError("unauthenticated", "调用身份无效。", status_code=401)
    return CallerIdentity(
        actor_id=session.actor_id,
        credential_id=session.credential_id,
        audience=session.audience,
        role=role,  # type: ignore[arg-type]
        scopes=session.scopes,
        resource_grants=session.resource_grants,
        session_id=session.session_id,
        expires_at=session.access_expires_at,
    )


def _session_response(
    session: SessionRecord,
    access_token: str,
    refresh_token: str,
) -> SessionResponse:
    return SessionResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=session.access_expires_at,
        refresh_expires_at=session.refresh_expires_at,
        identity=_identity(session),
    )


def create_api_app(context: APIContext) -> FastAPI:
    """创建可独立测试且可显式挂载的 `/api/v1` 应用。"""

    app = FastAPI(
        title="Elysium Application API",
        version="1.0.0",
        openapi_url="/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.api_context = context
    app.state.websocket_connection_budget = WebSocketConnectionBudget(
        context.max_websocket_connections
    )
    app.add_middleware(
        ConcurrencyLimitMiddleware,
        max_concurrency=context.max_concurrency,
    )
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(RequestIDMiddleware)

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        return _error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            APIError(
                "validation_failed",
                "请求参数不符合接口协议。",
                status_code=422,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        if exc.status_code == 404:
            error = APIError("resource_not_found", "请求的资源不存在。", status_code=404)
        elif exc.status_code == 405:
            error = APIError("method_not_allowed", "请求方法不受支持。", status_code=405)
        else:
            error = APIError("request_rejected", "请求无法处理。", status_code=exc.status_code)
        return _error_response(request, error)

    @app.exception_handler(Exception)
    async def handle_internal_error(request: Request, exc: Exception) -> JSONResponse:
        del exc
        return _error_response(
            request,
            APIError("internal_error", "服务内部错误。", status_code=500, retryable=True),
        )

    auth_router = APIRouter(prefix="/auth")
    foundation_router = APIRouter()
    bearer = HTTPBearer(auto_error=False)
    foundation = context.foundation or FoundationProjection(
        node_id=context.installation_id,
    )

    def current_session(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer),
        ],
    ) -> SessionRecord:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise APIError("unauthenticated", "需要有效的 Bearer 会话。", status_code=401)
        token = credentials.credentials.strip()
        if not token:
            raise APIError("unauthenticated", "需要有效的 Bearer 会话。", status_code=401)
        try:
            return context.store.authenticate_access(
                access_token=token,
                codec=context.codec,
            )
        except (SignedValueError, TypeError, ValueError) as exc:
            code = str(exc)
            if code in {"session_revoked", "credential_revoked"}:
                raise APIError("unauthenticated", "会话已失效。", status_code=401) from exc
            raise APIError("unauthenticated", "需要有效的 Bearer 会话。", status_code=401) from exc

    def revocable_session(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer),
        ],
    ) -> SessionRecord:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise APIError("unauthenticated", "需要有效的 Bearer 会话。", status_code=401)
        token = credentials.credentials.strip()
        try:
            return context.store.authenticate_access(
                access_token=token,
                codec=context.codec,
                allow_revoked=True,
            )
        except (SignedValueError, TypeError, ValueError) as exc:
            raise APIError("unauthenticated", "需要有效的 Bearer 会话。", status_code=401) from exc

    def require_scope(*required: str) -> Callable[[SessionRecord], SessionRecord]:
        if not set(required).issubset(ALL_EXPORTED_SCOPES):
            raise ValueError("unknown required scope")

        def dependency(
            session: Annotated[SessionRecord, Depends(current_session)],
        ) -> SessionRecord:
            if not set(required).issubset(session.scopes):
                raise APIError(
                    "scope_required",
                    "当前会话缺少所需权限。",
                    status_code=403,
                )
            return session

        return dependency

    @auth_router.post(
        "/sessions",
        response_model=SessionResponse,
        operation_id="createAuthSession",
        responses=ERROR_RESPONSES,
    )
    def create_session(
        payload: SessionCreateRequest,
        request: Request,
    ) -> SessionResponse:
        try:
            if payload.grant_type == "bootstrap_challenge":
                request_origin = request.headers.get("origin")
                if (
                    request_origin != payload.origin
                    or request_origin not in context.allowed_origins
                ):
                    raise ValueError("bootstrap_origin_forbidden")
                session, access, refresh = context.store.issue_session_from_bootstrap(
                    challenge=payload.bootstrap_challenge or "",
                    audience=payload.audience,
                    origin=payload.origin or "",
                    codec=context.codec,
                    access_ttl=context.access_ttl,
                    refresh_ttl=context.refresh_ttl,
                )
            else:
                session, access, refresh = context.store.issue_session_from_credential(
                    credential=payload.service_credential or "",
                    audience=payload.audience,
                    codec=context.codec,
                    access_ttl=context.access_ttl,
                    refresh_ttl=context.refresh_ttl,
                )
        except (SignedValueError, TypeError, ValueError) as exc:
            raise APIError("unauthenticated", "无法建立认证会话。", status_code=401) from exc
        return _session_response(session, access, refresh)

    @auth_router.get(
        "/me",
        response_model=CallerIdentity,
        operation_id="getAuthMe",
        responses=ERROR_RESPONSES,
    )
    def get_me(
        session: Annotated[SessionRecord, Depends(current_session)],
    ) -> CallerIdentity:
        return _identity(session)

    @auth_router.post(
        "/sessions/current:refresh",
        response_model=SessionResponse,
        operation_id="refreshAuthSession",
        responses=ERROR_RESPONSES,
    )
    def refresh_session(payload: SessionRefreshRequest) -> SessionResponse:
        try:
            session, access, refresh = context.store.refresh_session(
                refresh_token=payload.refresh_token,
                codec=context.codec,
                access_ttl=context.access_ttl,
                refresh_ttl=context.refresh_ttl,
            )
        except (SignedValueError, TypeError, ValueError) as exc:
            raise APIError("unauthenticated", "刷新凭据无效或已失效。", status_code=401) from exc
        return _session_response(session, access, refresh)

    @auth_router.delete(
        "/sessions/current",
        response_model=LogoutResponse,
        operation_id="logoutAuthSession",
        responses=ERROR_RESPONSES,
    )
    def logout_session(
        session: Annotated[SessionRecord, Depends(revocable_session)],
    ) -> LogoutResponse:
        context.store.revoke_session(session.session_id)
        return LogoutResponse(revoked=True)

    @auth_router.post(
        "/ws-tickets",
        response_model=WSTicketResponse,
        operation_id="createAuthWebsocketTicket",
        responses=ERROR_RESPONSES,
    )
    def create_ws_ticket(
        payload: WSTicketRequest,
        session: Annotated[
            SessionRecord,
            Depends(require_scope("auth:ticket")),
        ],
    ) -> WSTicketResponse:
        required_scope = _resolve_ws_scope(payload.resource)
        if required_scope not in session.scopes:
            raise APIError("scope_required", "当前会话缺少目标资源权限。", status_code=403)
        try:
            ticket, token = context.store.issue_ws_ticket(
                session=session,
                codec=context.codec,
                resource=payload.resource,
                subprotocol=payload.subprotocol,
                scopes=tuple(dict.fromkeys((*payload.scopes, required_scope))),
                origin=payload.origin,
                ttl=context.ticket_ttl,
            )
        except ValueError as exc:
            if str(exc) in {"session_revoked", "credential_revoked", "access_expired"}:
                raise APIError("unauthenticated", "会话已失效。", status_code=401) from exc
            raise APIError("forbidden", "无法为目标资源创建 ticket。", status_code=403) from exc
        return WSTicketResponse(
            ticket=token,
            expires_at=ticket.expires_at,
            resource=ticket.resource,
            subprotocol=ticket.subprotocol,
            scopes=ticket.scopes,
        )

    @foundation_router.get(
        "/bootstrap",
        response_model=BootstrapResponse,
        operation_id="getBootstrap",
        responses=ERROR_RESPONSES,
    )
    def get_bootstrap(
        session: Annotated[
            SessionRecord,
            Depends(require_scope("system:read")),
        ],
    ) -> BootstrapResponse:
        return foundation.bootstrap(session, _identity(session))

    @foundation_router.get(
        "/capabilities",
        response_model=CapabilitiesResponse,
        operation_id="getCapabilities",
        responses=ERROR_RESPONSES,
    )
    def get_capabilities(
        session: Annotated[
            SessionRecord,
            Depends(require_scope("capabilities:read")),
        ],
    ) -> CapabilitiesResponse:
        return foundation.capabilities(session)

    @foundation_router.get(
        "/readiness",
        response_model=ReadinessResponse,
        operation_id="getReadiness",
        responses=ERROR_RESPONSES,
    )
    def get_readiness(
        _session: Annotated[
            SessionRecord,
            Depends(require_scope("system:read")),
        ],
    ) -> ReadinessResponse:
        return foundation.readiness()

    @foundation_router.get(
        "/health",
        response_model=HealthResponse,
        operation_id="getHealth",
        responses=ERROR_RESPONSES,
    )
    def get_health() -> HealthResponse:
        return foundation.health()

    app.include_router(auth_router)
    app.include_router(foundation_router)
    if (context.command_store is None) != (context.command_dispatcher is None):
        raise ValueError("command store and dispatcher must be configured together")
    if context.command_store is not None and context.command_dispatcher is not None:
        from .commands import create_commands_router

        app.include_router(
            create_commands_router(
                store=context.command_store,
                dispatcher=context.command_dispatcher,
                require_scope=require_scope,
            )
        )
    return app


def _resolve_ws_scope(resource: str) -> str:
    """将具体资源路径映射到 inventory 允许的 WS scope。"""

    for template, scope in WS_RESOURCE_SCOPES.items():
        pattern = re.sub(r"\{[^}]+\}", r"[^/]+", template)
        if re.fullmatch(pattern, resource):
            return scope
    raise APIError("capability_disabled", "目标实时资源未开放。", status_code=404)


__all__ = [
    "MAX_BODY_BYTES",
    "APIContext",
    "APIError",
    "BodyLimitMiddleware",
    "RequestIDMiddleware",
    "WebSocketConnectionBudget",
    "create_api_app",
]
