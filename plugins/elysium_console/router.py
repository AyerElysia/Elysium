"""Loopback-only HTTP surface for the Elysium data console."""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Query, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from src.core.components.base.router import BaseRouter
from src.kernel.logger import get_logger

from .catalog import (
    ConsoleDataInvalid,
    ConsoleDataNotFound,
    ConsoleDataUnavailable,
    ElysiumDataCatalog,
)

logger = get_logger("elysium_console")

SESSION_COOKIE = "elysium_console_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_MAX_ENTRIES = 8

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
ATTENTION_STATUSES_QUERY = Query(default=[])


def _is_loopback_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(str(host or "").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def _request_origin(request: Request) -> str:
    host = request.headers.get("host", "")
    return f"{request.url.scheme}://{host}".rstrip("/")


def _same_origin(request: Request) -> bool:
    supplied = request.headers.get("origin", "").strip()
    if not supplied:
        return True
    try:
        actual = urlsplit(_request_origin(request))
        claimed = urlsplit(supplied)
    except ValueError:
        return False
    return (
        claimed.scheme.casefold(),
        claimed.netloc.casefold(),
    ) == (
        actual.scheme.casefold(),
        actual.netloc.casefold(),
    )


class LocalConsoleSessions:
    """Small in-memory session set; only token digests are retained."""

    def __init__(
        self,
        *,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        max_entries: int = SESSION_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        for digest, expires_at in list(self._sessions.items()):
            if expires_at <= now:
                self._sessions.pop(digest, None)
        while len(self._sessions) >= self._max_entries:
            oldest = min(self._sessions, key=self._sessions.__getitem__)
            self._sessions.pop(oldest, None)

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._sessions[self._digest(token)] = now + self._ttl_seconds
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        now = self._clock()
        digest = self._digest(token)
        with self._lock:
            expires_at = self._sessions.get(digest, 0.0)
            if expires_at <= now:
                self._sessions.pop(digest, None)
                return False
            return True


class _ConsoleSecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.client is None or not _is_loopback_host(request.client.host):
            return Response("local access only", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response


class ElysiumConsoleRouter(BaseRouter):
    """Read-only local observatory mounted below ``/console``."""

    router_name = "elysium_console"
    router_description = "Local read-only observatory for Elysium life data"
    custom_route_path = "/console"
    cors_origins = None

    def __init__(
        self,
        plugin: Any,
        *,
        catalog: ElysiumDataCatalog | None = None,
        sessions: LocalConsoleSessions | None = None,
    ) -> None:
        self._catalog = catalog or ElysiumDataCatalog()
        self._sessions = sessions or LocalConsoleSessions()
        self._static_root = Path(__file__).with_name("static")
        super().__init__(plugin)
        self.app.routes[:] = [
            route
            for route in self.app.routes
            if getattr(route, "path", "")
            not in {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
        ]
        self.app.add_middleware(_ConsoleSecurityHeadersMiddleware)

    def _authorize(self, request: Request) -> None:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not self._sessions.valid(token):
            raise HTTPException(status_code=401, detail="console session required")
        if not _same_origin(request):
            raise HTTPException(status_code=403, detail="cross-origin request rejected")
        fetch_site = request.headers.get("sec-fetch-site", "").casefold()
        if fetch_site and fetch_site not in {"none", "same-origin"}:
            raise HTTPException(status_code=403, detail="cross-site request rejected")

    async def _read(self, operation: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        try:
            return await operation
        except ConsoleDataUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ConsoleDataNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConsoleDataInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"Console read failed: error_type={type(exc).__name__}")
            raise HTTPException(status_code=500, detail="console read failed") from exc

    def register_endpoints(self) -> None:
        @self.app.get("/", include_in_schema=False)
        async def index() -> Response:
            try:
                content = (self._static_root / "index.html").read_text(encoding="utf-8")
            except OSError as exc:
                logger.error(
                    f"Console shell unavailable: error_type={type(exc).__name__}"
                )
                raise HTTPException(
                    status_code=503, detail="console shell unavailable"
                ) from exc
            response = Response(content, media_type="text/html; charset=utf-8")
            response.set_cookie(
                SESSION_COOKIE,
                self._sessions.issue(),
                httponly=True,
                max_age=SESSION_TTL_SECONDS,
                path="/console",
                samesite="strict",
            )
            return response

        @self.app.get("/assets/{asset_name}", include_in_schema=False)
        async def asset(asset_name: str) -> Response:
            allowed = {
                "app.js": "text/javascript; charset=utf-8",
                "styles.css": "text/css; charset=utf-8",
            }
            media_type = allowed.get(asset_name)
            if media_type is None:
                raise HTTPException(status_code=404, detail="asset not found")
            try:
                content = (self._static_root / asset_name).read_bytes()
            except OSError as exc:
                raise HTTPException(status_code=404, detail="asset not found") from exc
            return Response(content, media_type=media_type)

        @self.app.get("/api/v1/overview")
        async def overview(request: Request) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(self._catalog.overview())

        @self.app.get("/api/v1/timeline")
        async def timeline(request: Request, limit: int = 80) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(self._catalog.timeline(limit=limit))

        @self.app.get("/api/v1/subject")
        async def subject(request: Request) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(self._catalog.subject_documents())

        @self.app.get("/api/v1/memory")
        async def memory(request: Request) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(self._catalog.memory_summary())

        @self.app.get("/api/v1/memory/experiences")
        async def memory_experiences(
            request: Request,
            limit: int = 40,
            after_position: int = 0,
            after_occurrence_id: str = "",
            through_position: int | None = None,
            through_occurrence_id: str = "",
        ) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(
                self._catalog.memory_experiences(
                    limit=limit,
                    after_position=after_position,
                    after_occurrence_id=after_occurrence_id,
                    through_position=through_position,
                    through_occurrence_id=through_occurrence_id,
                )
            )

        @self.app.get("/api/v1/world")
        async def world(
            request: Request,
            limit: int = 50,
            after_observed_at: str = "",
            after_assertion_id: str = "",
            include_retracted: bool = False,
        ) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(
                self._catalog.world_page(
                    limit=limit,
                    after_observed_at=after_observed_at,
                    after_assertion_id=after_assertion_id,
                    include_retracted=include_retracted,
                )
            )

        @self.app.get("/api/v1/world/assertions/{assertion_id}/value")
        async def world_value(
            request: Request,
            assertion_id: str,
            offset_bytes: int = 0,
            max_bytes: int = 64 * 1024,
        ) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(
                self._catalog.world_assertion_value(
                    assertion_id,
                    offset_bytes=offset_bytes,
                    max_bytes=max_bytes,
                )
            )

        @self.app.get("/api/v1/attention")
        async def attention(
            request: Request,
            statuses: list[str] = ATTENTION_STATUSES_QUERY,
            continuation: str = "",
            limit: int = 32,
        ) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(
                self._catalog.attention_page(
                    statuses=statuses,
                    continuation=continuation,
                    limit=limit,
                )
            )

        @self.app.get("/api/v1/workspace")
        async def workspace(
            request: Request,
            path: str = "",
            offset: int = 0,
            limit: int = 100,
        ) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(
                self._catalog.workspace_page(path=path, offset=offset, limit=limit)
            )

        @self.app.get("/api/v1/workspace/text")
        async def workspace_text(
            request: Request,
            path: str,
            offset_bytes: int = 0,
            max_bytes: int = 64 * 1024,
        ) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(
                self._catalog.workspace_text(
                    path=path,
                    offset_bytes=offset_bytes,
                    max_bytes=max_bytes,
                )
            )

        @self.app.get("/api/v1/catalog")
        async def catalog(request: Request) -> dict[str, Any]:
            self._authorize(request)
            return await self._read(self._catalog.data_map())


__all__ = [
    "SESSION_COOKIE",
    "ElysiumConsoleRouter",
    "LocalConsoleSessions",
    "_is_loopback_host",
]
