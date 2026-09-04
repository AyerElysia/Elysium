"""P3-02 基础发现、公共 capability 与只读 readiness 投影。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.core.config import CORE_VERSION

from .auth_store import SessionRecord
from .schemas import (
    AdapterStatus,
    BootstrapResponse,
    CallerIdentity,
    CapabilitiesResponse,
    CapabilityManifest,
    ComponentStatus,
    FeatureCapability,
    HealthResponse,
    ReadinessResponse,
)
from .schemas.common import API_VERSION, utc_now

_PUBLIC_FEATURES: dict[str, dict[str, str]] = {
    "system": {
        "system.bootstrap.read": "system:read",
        "system.readiness.read": "system:read",
        "system.health.read": "system:read",
    },
    "capabilities": {
        "capabilities.read": "capabilities:read",
    },
    "events": {
        "events.query": "events:read",
        "events.subscribe": "events:read",
    },
    "commands": {
        "commands.read": "jobs:read",
        "commands.submit": "jobs:operate",
        "commands.cancel": "jobs:operate",
    },
    "chat": {
        "message.read": "chat:read",
        "message.send.text": "chat:write",
        "message.send.media": "chat:write",
        "message.react": "chat:write",
        "message.recall": "chat:moderate",
    },
    "media": {
        "media.read": "media:read",
        "media.upload": "media:write",
        "media.recognize": "media:recognize",
    },
    "livestream": {
        "session.read": "livestream:read",
        "session.operate": "livestream:operate",
    },
    "voice_call": {
        "call.read": "voice_call:read",
        "call.operate": "voice_call:operate",
        "call.observe": "voice_call:observe",
    },
    "tabletop": {
        "werewolf.read": "tabletop:read",
        "werewolf.play": "tabletop:play",
    },
}

_MODULE_PLUGIN = {
    "events": "life_engine",
    "chat": "feishu_adapter",
    "media": "feishu_adapter",
    "livestream": "Livestream",
    "voice_call": "Voice-Live",
    "tabletop": "werewolf_game",
}
_MODULE_PROVIDER = {
    "chat": "feishu",
    "media": "feishu",
    "livestream": "bilibili",
    "voice_call": "realtime_provider",
    "tabletop": "werewolf",
}
_KNOWN_ADAPTER_PLUGINS = {
    "feishu_adapter": "feishu",
    "napcat_adapter": "qq",
    "ayla_adapter": "ayla",
}
_REQUIRED_LOCAL_COMPONENTS = {"api", "life_event_ledger"}


@dataclass(frozen=True, slots=True)
class FoundationSnapshot:
    """一次请求内一致的只读基础状态快照。"""

    generated_at: datetime
    node_id: str
    modules: tuple[ComponentStatus, ...]
    adapters: tuple[AdapterStatus, ...]
    migration_version: str


SnapshotProvider = Callable[[], FoundationSnapshot]


class FoundationProjection:
    """只读聚合外部注入的运行事实，不执行连接、写入或修复。"""

    def __init__(
        self,
        *,
        node_id: str,
        snapshot_provider: SnapshotProvider | None = None,
    ) -> None:
        self._node_id = node_id
        self._snapshot_provider = snapshot_provider

    def snapshot(self) -> FoundationSnapshot:
        """读取一份安全快照；未接入 Bot 时只声明可证明的状态。"""

        if self._snapshot_provider is not None:
            return self._snapshot_provider()
        return FoundationSnapshot(
            generated_at=utc_now(),
            node_id=self._node_id,
            modules=(
                ComponentStatus(
                    component="api",
                    state="ready",
                    enabled=True,
                    owner="app_api_v1",
                ),
                ComponentStatus(
                    component="life_event_ledger",
                    state="unavailable",
                    enabled=True,
                    owner="life_engine",
                    degraded_reason="运行状态源尚未接入。",
                ),
                ComponentStatus(
                    component="remote_sync",
                    state="disabled",
                    enabled=False,
                    owner="life_engine.shared_sync",
                ),
                ComponentStatus(
                    component="command_store",
                    state="unavailable",
                    enabled=True,
                    owner="kernel.commands",
                    degraded_reason="运行状态源尚未接入命令账本。",
                ),
            ),
            adapters=(),
            migration_version="api-v1-schema-1",
        )

    def bootstrap(
        self,
        session: SessionRecord,
        identity: CallerIdentity,
    ) -> BootstrapResponse:
        snapshot = self.snapshot()
        return BootstrapResponse(
            api_version=API_VERSION,
            elysium_version=CORE_VERSION,
            node_id=snapshot.node_id,
            identity=identity,
            modules=snapshot.modules,
            generated_at=snapshot.generated_at,
        )

    def capabilities(self, session: SessionRecord) -> CapabilitiesResponse:
        snapshot = self.snapshot()
        component_states = {item.component: item for item in snapshot.modules}
        provider_states = {item.provider: item for item in snapshot.adapters}
        manifests: list[CapabilityManifest] = []
        for module, features in _PUBLIC_FEATURES.items():
            plugin_name = _MODULE_PLUGIN.get(module)
            provider_name = _MODULE_PROVIDER.get(module)
            status = (
                component_states.get("life_event_ledger")
                if module == "events"
                else component_states.get(f"plugin:{plugin_name}")
                if plugin_name
                else None
            )
            adapter = provider_states.get(provider_name) if provider_name else None
            if module in {"system", "capabilities"}:
                state = "ready"
                reason = None
            elif adapter is not None:
                state = adapter.state
                reason = adapter.degraded_reason
            elif status is not None:
                state = status.state
                reason = status.degraded_reason
            else:
                state = "unavailable"
                reason = "运行状态源未声明该公共模块。"
            manifests.append(
                CapabilityManifest(
                    module=module,
                    available=state in {"ready", "degraded"},
                    state=state,
                    contract_version="1.0",
                    provider=provider_name,
                    features={
                        name: FeatureCapability(
                            supported=state in {"ready", "degraded"},
                            scope=scope,
                            authorized=scope in session.scopes,
                        )
                        for name, scope in features.items()
                    },
                    degraded_reason=reason,
                )
            )
        return CapabilitiesResponse(
            api_version=API_VERSION,
            node_id=snapshot.node_id,
            capabilities=tuple(manifests),
            generated_at=snapshot.generated_at,
        )

    def readiness(self) -> ReadinessResponse:
        snapshot = self.snapshot()
        required = {
            item.component: item
            for item in snapshot.modules
            if item.component in _REQUIRED_LOCAL_COMPONENTS
        }
        local_ready = all(
            required.get(component) is not None
            and required[component].state in {"ready", "degraded"}
            for component in _REQUIRED_LOCAL_COMPONENTS
        )
        if not local_ready:
            state = "unavailable"
        elif any(
            item.state in {"failed", "degraded", "unavailable"}
            for item in (*snapshot.modules, *snapshot.adapters)
        ):
            state = "degraded"
        else:
            state = "ready"
        return ReadinessResponse(
            api_version=API_VERSION,
            elysium_version=CORE_VERSION,
            node_id=snapshot.node_id,
            state=state,
            local_ready=local_ready,
            dependencies=snapshot.modules,
            adapters=snapshot.adapters,
            migration_version=snapshot.migration_version,
            generated_at=snapshot.generated_at,
        )

    def health(self) -> HealthResponse:
        generated_at = datetime.now(UTC)
        return HealthResponse(
            api_version=API_VERSION,
            node_id=self._node_id,
            state="ready",
            alive=True,
            generated_at=generated_at,
        )


def snapshot_from_bot(bot: Any) -> FoundationSnapshot:
    """从现役 Bot 的内存事实构建快照，不调用任何主动 health 方法。"""

    now = utc_now()
    load_results: Mapping[str, bool] = dict(getattr(bot, "load_results", {}) or {})
    manifests: Mapping[str, Any] = dict(getattr(bot, "manifests", {}) or {})
    plugin_manager = getattr(bot, "plugin_manager", None)
    loaded_plugins = (
        plugin_manager.get_all_plugins() if plugin_manager is not None else {}
    )
    modules: list[ComponentStatus] = [
        ComponentStatus(
            component="api",
            state="ready",
            enabled=True,
            owner="app_api_v1",
        )
    ]
    for name in sorted(manifests):
        manifest = manifests[name]
        enabled = bool(getattr(manifest, "enabled", True))
        if not enabled:
            state = "disabled"
            reason = None
        elif load_results.get(name) is True:
            state = "ready"
            reason = None
        elif name in load_results:
            state = "failed"
            reason = "插件加载失败。"
        else:
            state = "unavailable"
            reason = "插件尚未完成加载。"
        modules.append(
            ComponentStatus(
                component=f"plugin:{name}",
                state=state,
                enabled=enabled,
                owner=f"plugin:{name}",
                degraded_reason=reason,
            )
        )

    life_plugin = loaded_plugins.get("life_engine")
    life_service = _life_service(life_plugin)
    event_bus = getattr(life_service, "_event_bus", None)
    ledger_ready = event_bus is not None
    modules.append(
        ComponentStatus(
            component="life_event_ledger",
            state="ready" if ledger_ready else "unavailable",
            enabled=True,
            owner="life_engine",
            degraded_reason=None if ledger_ready else "Life Event ledger 尚未初始化。",
        )
    )

    shared_sync = getattr(life_service, "_shared_sync_bridge", None)
    sync_error = str(getattr(life_service, "_shared_sync_error", "") or "")
    config = getattr(life_plugin, "config", None) if life_plugin is not None else None
    shared_sync_config = getattr(config, "shared_sync", None)
    configured_sync_enabled = bool(getattr(shared_sync_config, "enabled", False))
    sync_enabled = bool(
        getattr(
            life_service,
            "_shared_sync_effective_enabled",
            configured_sync_enabled,
        )
    )
    if not sync_enabled:
        sync_state = "disabled"
        sync_reason = None
    elif shared_sync is None:
        sync_state = "degraded" if sync_error else "unavailable"
        sync_reason = _safe_reason(sync_error or "远程同步尚未就绪。")
    else:
        sync_state = "ready"
        sync_reason = None
    modules.append(
        ComponentStatus(
            component="remote_sync",
            state=sync_state,
            enabled=sync_enabled,
            owner="life_engine.shared_sync",
            degraded_reason=sync_reason,
        )
    )

    api_mount = getattr(bot, "app_api_mount", None)
    command_store = getattr(api_mount, "command_store", None)
    command_ready = command_store is not None and not bool(
        getattr(api_mount, "_closed", False)
    )
    modules.append(
        ComponentStatus(
            component="command_store",
            state="ready" if command_ready else "unavailable",
            enabled=True,
            owner="kernel.commands",
            degraded_reason=None if command_ready else "命令账本尚未挂载。",
        )
    )

    adapters: list[AdapterStatus] = []
    adapter_manager = None
    try:
        from src.core.managers.adapter_manager import get_adapter_manager

        adapter_manager = get_adapter_manager()
    except RuntimeError:
        adapter_manager = None
    active = adapter_manager.get_all_adapters() if adapter_manager is not None else {}
    represented_plugins: set[str] = set()
    for signature, adapter in sorted(active.items()):
        plugin_name = signature.split(":", 1)[0]
        represented_plugins.add(plugin_name)
        provider = str(getattr(adapter, "platform", "") or plugin_name)
        enabled = _plugin_enabled(adapter)
        connected = _adapter_connected(adapter) if enabled else False
        adapters.append(
            AdapterStatus(
                provider=provider,
                component=signature,
                state="ready" if connected else "degraded" if enabled else "disabled",
                enabled=enabled,
                connected=connected,
                degraded_reason=(
                    None
                    if connected or not enabled
                    else "Adapter 已加载但当前未连接。"
                ),
            )
        )
    for plugin_name, provider in _KNOWN_ADAPTER_PLUGINS.items():
        if plugin_name in represented_plugins or plugin_name not in manifests:
            continue
        manifest = manifests[plugin_name]
        plugin = loaded_plugins.get(plugin_name)
        enabled = _declared_adapter_enabled(manifest, plugin)
        if not enabled:
            state = "disabled"
            reason = None
        elif load_results.get(plugin_name) is False:
            state = "failed"
            reason = "Adapter 插件加载失败。"
        else:
            state = "unavailable"
            reason = "Adapter 未注册或尚未启动。"
        adapters.append(
            AdapterStatus(
                provider=provider,
                component=f"{plugin_name}:adapter",
                state=state,
                enabled=enabled,
                connected=False,
                degraded_reason=reason,
            )
        )
    return FoundationSnapshot(
        generated_at=now,
        node_id=str(getattr(bot, "bot_name", "Elysium") or "Elysium"),
        modules=tuple(modules),
        adapters=tuple(adapters),
        migration_version="api-v1-schema-1",
    )


def _life_service(plugin: Any) -> Any | None:
    """只读取已初始化服务，禁止访问会懒创建实例的 `service` 属性。"""

    if plugin is None:
        return None
    return getattr(plugin, "_service", None)


def _declared_adapter_enabled(manifest: Any, plugin: Any) -> bool:
    if not bool(getattr(manifest, "enabled", True)):
        return False
    config = getattr(plugin, "config", None) if plugin is not None else None
    plugin_section = getattr(config, "plugin", None)
    return bool(getattr(plugin_section, "enabled", True))


def _plugin_enabled(adapter: Any) -> bool:
    try:
        config = adapter._config()
    except (AttributeError, RuntimeError, TypeError):
        config = getattr(getattr(adapter, "plugin", None), "config", None)
    plugin = getattr(config, "plugin", None)
    return bool(getattr(plugin, "enabled", True))


def _adapter_connected(adapter: Any) -> bool:
    checker = getattr(adapter, "is_connected", None)
    if not callable(checker):
        return True
    try:
        return bool(checker())
    except Exception:  # noqa: BLE001 - health projection must stay safe and read-only
        return False


def _safe_reason(value: str) -> str:
    """仅暴露错误类型级别摘要，不回显连接串、凭据或完整异常。"""

    if not value:
        return ""
    error_type = value.split(":", 1)[0].strip()
    return f"{error_type or 'RuntimeError'}: 远程同步不可用。"


__all__ = [
    "FoundationProjection",
    "FoundationSnapshot",
    "snapshot_from_bot",
]
