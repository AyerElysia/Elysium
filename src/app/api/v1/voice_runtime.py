"""Late-bound production provider for the P3-09 voice-call API."""

from __future__ import annotations

from pathlib import Path

from plugins.voice_live.router import VoiceLiveRouter
from plugins.voice_live.runtime_store import VoiceEpisodeStore
from src.core.managers.router_manager import get_router_manager


class MountedVoiceCallProvider:
    """Resolve plugin-owned voice resources without starting a call."""

    def router(self) -> VoiceLiveRouter | None:
        for router in get_router_manager().get_all_mounted_routers().values():
            if isinstance(router, VoiceLiveRouter):
                return router
        return None

    def trace_root(self) -> Path | None:
        router = self.router()
        if router is None:
            return None
        return Path(router.plugin.config.observability.trace_root)

    def instance_id(self, call_id: str) -> str | None:
        router = self.router()
        if router is None:
            return None
        prefix = router.plugin.config.session.instance_id_prefix
        return f"{prefix}_{call_id}"

    def store(self, call_id: str) -> VoiceEpisodeStore | None:
        root = self.trace_root()
        instance_id = self.instance_id(call_id)
        if root is None or instance_id is None:
            return None
        return VoiceEpisodeStore(root, instance_id, call_id)


__all__ = ["MountedVoiceCallProvider"]
