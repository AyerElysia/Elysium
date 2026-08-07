"""Late-bound production owner for the P3-10 durable Werewolf domain."""

from __future__ import annotations

from typing import Any

from plugins.werewolf_game.domain import WerewolfDomainService
from src.core.managers.plugin_manager import get_plugin_manager


class MountedTabletopProvider:
    """Resolve the plugin-owned domain at call time without starting it."""

    def domain(self) -> WerewolfDomainService | None:
        plugin = get_plugin_manager().get_plugin("werewolf_game")
        domain = getattr(plugin, "_werewolf_domain", None) if plugin is not None else None
        return domain if isinstance(domain, WerewolfDomainService) else None

    def __getattr__(self, name: str) -> Any:
        domain = self.domain()
        if domain is None:
            async def unavailable(*args: Any, **kwargs: Any) -> Any:
                del args, kwargs
                raise RuntimeError("tabletop capability is unavailable")

            return unavailable
        return getattr(domain, name)

    def close(self) -> None:
        """The plugin owns the ledger lifecycle; the API mount owns no resource."""


__all__ = ["MountedTabletopProvider"]
