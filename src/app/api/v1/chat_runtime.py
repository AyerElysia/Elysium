"""Runtime assembly for P3-06 chat commands without exposing provider clients."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.transport.message_send import get_message_sender

from .chat_commands import (
    CapabilityError,
    ChatCommandService,
    ChatTargetResolver,
    ProviderFacadeRegistry,
)
from .chat_platforms import AylaChatFacade, FeishuChatFacade, NapCatChatFacade
from .media_contracts import ManagedMediaResolver


def create_chat_command_service(
    targets: ChatTargetResolver,
    *,
    media_resolver: ManagedMediaResolver | None = None,
    message_sender: object | None = None,
    feishu_provider: Callable[[], object | None] | None = None,
    adapter_manager_provider: Callable[[], object] | None = None,
) -> ChatCommandService:
    """Create the domain service with late-bound adapters and no media fallback."""

    return ChatCommandService(
        sender=message_sender or get_message_sender(),
        targets=targets,
        media=media_resolver,
        providers=ProviderFacadeRegistry(
            {
                "feishu": FeishuChatFacade(
                    _LateBoundFeishuClient(feishu_provider or _feishu_adapter)
                ),
                "qq": NapCatChatFacade(
                    _LateBoundNapCatClient(adapter_manager_provider or _adapter_manager)
                ),
                "ayla": AylaChatFacade(),
            }
        ),
    )


class _LateBoundFeishuClient:
    def __init__(self, provider: Callable[[], object | None]) -> None:
        self._provider = provider

    async def execute_action(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        adapter = self._provider()
        execute = getattr(adapter, "execute_action", None)
        if not callable(execute):
            raise CapabilityError("Feishu adapter is unavailable")
        return await execute(action, params)


class _LateBoundNapCatClient:
    def __init__(self, manager_provider: Callable[[], object]) -> None:
        self._manager_provider = manager_provider

    def _client(self) -> object:
        manager = self._manager_provider()
        get_all = getattr(manager, "get_all_adapters", None)
        if not callable(get_all):
            raise CapabilityError("NapCat adapter manager is unavailable")
        adapters = get_all()
        for adapter in adapters.values():
            if getattr(adapter, "platform", None) != "qq":
                continue
            client = getattr(adapter, "client", None)
            if client is not None:
                return client
        raise CapabilityError("NapCat adapter is unavailable")

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client(), name, None)
        if not callable(attribute):
            raise CapabilityError(f"NapCat capability {name!r} is unavailable")
        return attribute


def _feishu_adapter() -> object | None:
    from plugins.feishu_adapter.adapter import get_feishu_adapter

    return get_feishu_adapter()


def _adapter_manager() -> object:
    from src.core.managers.adapter_manager import get_adapter_manager

    return get_adapter_manager()


__all__ = ["create_chat_command_service"]
