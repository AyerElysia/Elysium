"""Late-bound production provider for the P3-08 livestream API."""

from __future__ import annotations

from pathlib import Path

from plugins.livestream.router import LivestreamRouter
from plugins.livestream.runtime import LivestreamRuntime
from src.core.managers.router_manager import get_router_manager


class MountedLivestreamProvider:
    """Resolve the plugin-owned router without creating or starting livestream resources."""

    def runtime(self) -> LivestreamRuntime | None:
        router = self._router()
        return router.runtime if router is not None else None

    def ledger_path(self) -> Path | None:
        runtime = self.runtime()
        if runtime is None:
            return None
        return Path(runtime.config.storage.ledger_path)

    @staticmethod
    def _router() -> LivestreamRouter | None:
        for router in get_router_manager().get_all_mounted_routers().values():
            if isinstance(router, LivestreamRouter):
                return router
        return None


__all__ = ["MountedLivestreamProvider"]
