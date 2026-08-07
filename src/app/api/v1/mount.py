"""API v1 的显式生产挂载入口。"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from plugins.life_engine.service.event_bus import RawEventStore
from src.kernel.commands import CommandDispatcher, CommandStore, HandlerRegistry
from src.kernel.concurrency import TaskManager

from .auth_store import AuthStore
from .chat import ChatQueryService, LedgerChatTargetResolver
from .events import EventQueryService
from .foundation import FoundationProjection
from .media_objects import (
    ManagedMediaService,
    MediaObjectStore,
    default_media_recognizer,
)
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
    media_store: MediaObjectStore
    command_store: CommandStore
    command_dispatcher: CommandDispatcher
    tabletop_provider: object | None = None
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
        self.media_store.close()
        if self.tabletop_provider is not None:
            close = getattr(self.tabletop_provider, "close", None)
            if callable(close):
                close()
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
    chat_command_service_factory: Callable[..., object] | None = None,
    livestream_provider: object | None = None,
    voice_call_provider: object | None = None,
    tabletop_provider: object | None = None,
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
    media_store: MediaObjectStore | None = None
    command_store: CommandStore | None = None
    try:
        media_store = MediaObjectStore(
            auth_path,
            workspace_root.resolve() / "runtime" / "media",
        )
        media_service = ManagedMediaService(
            media_store,
            recognizer=default_media_recognizer,
        )
        command_store = CommandStore(auth_path)
        codec = SignedValueCodec(signing_secret)
        chat_queries = ChatQueryService(
            codec=codec,
            store_provider=event_store_provider or (lambda: None),
        )
        if chat_command_service is not None and chat_command_service_factory is not None:
            raise ValueError(
                "chat command service and factory cannot be configured together"
            )
        if chat_command_service_factory is not None:
            chat_command_service = chat_command_service_factory(
                LedgerChatTargetResolver(queries=chat_queries, auth_store=store),
                media_resolver=media_service,
            )
        registry = command_registry or HandlerRegistry()
        if chat_command_service is not None:
            register = getattr(chat_command_service, "register", None)
            if not callable(register):
                raise TypeError("chat_command_service must provide register(registry)")
            register(registry)
        if livestream_provider is not None:
            from .livestream import LivestreamCommandService

            LivestreamCommandService(livestream_provider).register(registry)
        if voice_call_provider is not None:
            from .voice_calls import VoiceCallCommandService

            VoiceCallCommandService(voice_call_provider).register(registry)
        command_dispatcher = CommandDispatcher(
            command_store,
            registry=registry,
            task_manager=task_manager,
        )
        context = APIContext(
            store=store,
            codec=codec,
            installation_id=installation_id,
            allowed_origins=normalized_origins,
            max_concurrency=max_concurrency,
            max_websocket_connections=max_websocket_connections,
            foundation=foundation,
            events=EventQueryService(
                node_id=installation_id,
                codec=codec,
                store_provider=event_store_provider,
            ),
            chat=chat_queries,
            media=media_service,
            command_store=command_store,
            command_dispatcher=command_dispatcher,
            chat_commands_enabled=chat_command_service is not None,
            livestream=livestream_provider,
            voice_calls=voice_call_provider,
            tabletop=tabletop_provider,
        )
        app = create_api_app(context)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(normalized_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
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
        if command_store is not None:
            command_store.close()
        if media_store is not None:
            media_store.close()
        store.close()
        raise
    return APIV1Mount(
        parent=parent,
        store=store,
        media_store=media_store,
        command_store=command_store,
        command_dispatcher=command_dispatcher,
        tabletop_provider=tabletop_provider,
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
