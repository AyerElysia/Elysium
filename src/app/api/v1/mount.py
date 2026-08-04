"""API v1 的显式生产挂载入口。"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.kernel.commands import CommandDispatcher, CommandStore, HandlerRegistry

from plugins.life_engine.service.event_bus import RawEventStore
from src.kernel.concurrency import TaskManager

from .auth_store import AuthStore
from .events import EventQueryService
from .foundation import FoundationProjection
from .runtime import APIContext, create_api_app
from .tokens import SignedValueCodec

SIGNING_SECRET_ENV = "ELYSIUM_APP_API_V1_SIGNING_SECRET"
INSTALLATION_ID_ENV = "ELYSIUM_INSTALLATION_ID"
MOUNT_NAME = "elysium_app_api_v1"


@dataclass(slots=True)
class APIV1Mount:
    """同时拥有 FastAPI Mount 与认证 store 的生命周期句柄。"""

    parent: FastAPI
    store: AuthStore
    command_store: CommandStore
    command_dispatcher: CommandDispatcher
    _closed: bool = field(default=False, init=False)

    async def start(self) -> None:
        """Recover durable accepted commands after all handlers are registered."""

        if self._closed:
            raise RuntimeError("API v1 mount is closed")
        await self.command_dispatcher.recover()

    async def aclose(self) -> None:
        """Idempotently stop commands, unmount routes, and close both stores."""

        if self._closed:
            return
        await self.command_dispatcher.close()
        self._close_resources()

    def close(self) -> None:
        """Synchronously close an unstarted mount used by setup and tests."""

        if self._closed:
            return
        if self.command_dispatcher.has_active_tasks:
            raise RuntimeError("active command dispatcher requires await mount.aclose()")
        self._close_resources()

    def _close_resources(self) -> None:
        self.parent.router.routes[:] = [
            route
            for route in self.parent.router.routes
            if getattr(route, "name", None) != MOUNT_NAME
        ]
        self.command_store.close()
        self.store.close()
        self._closed = True


def mount_api_v1(
    parent: FastAPI,
    *,
    workspace_root: Path,
    database_path: str,
    allowed_origins: tuple[str, ...],
    max_concurrency: int,
    max_websocket_connections: int = 64,
    foundation: FoundationProjection | None = None,
    event_store_provider: Callable[[], RawEventStore | None] | None = None,
    command_registry: HandlerRegistry | None = None,
    chat_command_service: object | None = None,
    task_manager: TaskManager | None = None,
    environ: Mapping[str, str] | None = None,
) -> APIV1Mount:
    """校验生产配置，创建耐久认证 store，并挂载 `/api/v1`。"""

    if any(getattr(route, "name", None) == MOUNT_NAME for route in parent.routes):
        raise RuntimeError("/api/v1 is already mounted")
    environment = environ or os.environ
    signing_secret = environment.get(SIGNING_SECRET_ENV, "")
    installation_id = environment.get(INSTALLATION_ID_ENV, "")
    if len(signing_secret.encode("utf-8")) < 32:
        raise RuntimeError(f"{SIGNING_SECRET_ENV} must contain at least 32 bytes")
    if not installation_id.strip():
        raise RuntimeError(f"{INSTALLATION_ID_ENV} must not be empty")
    normalized_origins = _validate_origins(allowed_origins)
    auth_path = _resolve_auth_path(workspace_root, database_path)
    store = AuthStore(auth_path, installation_id=installation_id)
    command_store = CommandStore(auth_path)
    registry = command_registry or HandlerRegistry()
    if chat_command_service is not None:
        register = getattr(chat_command_service, "register", None)
        if not callable(register):
            raise TypeError("chat_command_service must provide register(registry)")
        register(registry)
    command_dispatcher = CommandDispatcher(
        command_store,
        registry=registry,
        task_manager=task_manager,
    )
    try:
        context = APIContext(
            store=store,
            codec=SignedValueCodec(signing_secret),
            installation_id=installation_id,
            allowed_origins=normalized_origins,
            max_concurrency=max_concurrency,
            max_websocket_connections=max_websocket_connections,
            foundation=foundation,
            events=EventQueryService(
                node_id=installation_id,
                codec=SignedValueCodec(signing_secret),
                store_provider=event_store_provider,
            ),
            command_store=command_store,
            command_dispatcher=command_dispatcher,
            chat_commands_enabled=chat_command_service is not None,
        )
        app = create_api_app(context)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(normalized_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "X-Request-ID",
            ],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )
        parent.mount("/api/v1", app, name=MOUNT_NAME)
    except BaseException:
        command_store.close()
        store.close()
        raise
    return APIV1Mount(
        parent=parent,
        store=store,
        command_store=command_store,
        command_dispatcher=command_dispatcher,
    )


def _resolve_auth_path(workspace_root: Path, configured: str) -> Path:
    root = workspace_root.resolve()
    runtime_root = (root / "runtime").resolve()
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(runtime_root):
        raise RuntimeError("app_api_v1_database_path must stay under workspace runtime/")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _validate_origins(origins: tuple[str, ...]) -> tuple[str, ...]:
    if not origins:
        raise RuntimeError("app_api_v1_allowed_origins must not be empty")
    normalized: list[str] = []
    for origin in origins:
        parsed = urlparse(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or "*" in origin
        ):
            raise RuntimeError(f"invalid exact Origin: {origin}")
        normalized.append(origin.rstrip("/"))
    return tuple(dict.fromkeys(normalized))


__all__ = [
    "INSTALLATION_ID_ENV",
    "MOUNT_NAME",
    "SIGNING_SECRET_ENV",
    "APIV1Mount",
    "mount_api_v1",
]
