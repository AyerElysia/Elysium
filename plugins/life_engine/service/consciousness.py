"""Consciousness instance model and registry for multi-consciousness coordination.

Each main consciousness (chat, livestream, creation, etc.) is represented as a
ConsciousnessInstance. The ConsciousnessRegistry manages their lifecycle and
provides lookup for the subconscious coordination protocol.

Design principles:
- The existing global chatter is always registered as "chat_global"
- Instances can be active, suspended, or terminated
- Each instance has a PerceptionFilter controlling its WorldState slice
- The registry is persisted to runtime/consciousness_registry.json
- Thread-safe via asyncio lock in the service layer (not here)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .world_state import PerceptionFilter

logger = logging.getLogger(__name__)

REGISTRY_SCHEMA_VERSION = 1

# 默认全局聊天意识实例 ID
CHAT_GLOBAL_INSTANCE_ID = "chat_global"


@dataclass(slots=True)
class ConsciousnessInstance:
    """One main consciousness instance (e.g., chat, livestream, creation)."""

    instance_id: str
    kind: str = "chat"  # chat | livestream | creation | custom
    display_name: str = ""
    # 绑定的聊天流 ID（这个意识负责哪些对话）
    stream_ids: list[str] = field(default_factory=list)
    # 生命周期状态
    status: str = "active"  # active | suspended | terminated
    created_at: str = ""
    last_active_at: str = ""
    suspended_at: str = ""
    # 感知配置：这个意识需要世界状态的哪些切片
    perception_filter: PerceptionFilter = field(default_factory=PerceptionFilter.full)
    # 元数据（自由扩展）
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_suspended(self) -> bool:
        return self.status == "suspended"

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "stream_ids": list(self.stream_ids),
            "status": self.status,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "suspended_at": self.suspended_at,
            "perception_filter": self.perception_filter.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConsciousnessInstance":
        pf_data = data.get("perception_filter")
        pf = PerceptionFilter.from_dict(pf_data) if isinstance(pf_data, dict) else PerceptionFilter.full()
        return cls(
            instance_id=str(data.get("instance_id") or ""),
            kind=str(data.get("kind") or "chat"),
            display_name=str(data.get("display_name") or ""),
            stream_ids=[str(v) for v in (data.get("stream_ids") or []) if v],
            status=str(data.get("status") or "active"),
            created_at=str(data.get("created_at") or ""),
            last_active_at=str(data.get("last_active_at") or ""),
            suspended_at=str(data.get("suspended_at") or ""),
            perception_filter=pf,
            metadata=dict(data.get("metadata") or {}),
        )


class ConsciousnessRegistry:
    """Manages the lifecycle of all consciousness instances.

    The registry ensures that:
    - A default 'chat_global' instance always exists
    - Stream-to-instance mapping is consistent (one stream → one active instance)
    - Suspended instances release their streams (can be reclaimed)
    """

    def __init__(self) -> None:
        self._instances: dict[str, ConsciousnessInstance] = {}
        self._ensure_chat_global()

    def _ensure_chat_global(self) -> None:
        """Ensure the default global chat consciousness always exists."""
        if CHAT_GLOBAL_INSTANCE_ID not in self._instances:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            self._instances[CHAT_GLOBAL_INSTANCE_ID] = ConsciousnessInstance(
                instance_id=CHAT_GLOBAL_INSTANCE_ID,
                kind="chat",
                display_name="全局聊天意识",
                status="active",
                created_at=now,
                last_active_at=now,
                perception_filter=PerceptionFilter.full(),
            )

    # ------------------------------------------------------------------
    # Registration & Lifecycle
    # ------------------------------------------------------------------

    def register(self, instance: ConsciousnessInstance) -> ConsciousnessInstance:
        """Register a new consciousness instance.

        If an instance with the same ID exists and is terminated, it is replaced.
        If it exists and is active/suspended, raises ValueError.
        """
        existing = self._instances.get(instance.instance_id)
        if existing and existing.status != "terminated":
            raise ValueError(
                f"意识实例 '{instance.instance_id}' 已存在且状态为 {existing.status}"
            )
        self._instances[instance.instance_id] = instance
        logger.info(
            f"注册意识实例: {instance.instance_id} "
            f"(kind={instance.kind}, streams={instance.stream_ids})"
        )
        return instance

    def suspend(self, instance_id: str, *, timestamp: str = "") -> bool:
        """Suspend an active instance (e.g., livestream ends)."""
        instance = self._instances.get(instance_id)
        if instance is None or not instance.is_active:
            return False
        instance.status = "suspended"
        instance.suspended_at = timestamp
        logger.info(f"挂起意识实例: {instance_id}")
        return True

    def resume(self, instance_id: str, *, timestamp: str = "") -> bool:
        """Resume a suspended instance."""
        instance = self._instances.get(instance_id)
        if instance is None or not instance.is_suspended:
            return False
        instance.status = "active"
        instance.suspended_at = ""
        if timestamp:
            instance.last_active_at = timestamp
        logger.info(f"恢复意识实例: {instance_id}")
        return True

    def terminate(self, instance_id: str) -> bool:
        """Terminate an instance (cannot be resumed)."""
        if instance_id == CHAT_GLOBAL_INSTANCE_ID:
            logger.warning("不允许终止全局聊天意识")
            return False
        instance = self._instances.get(instance_id)
        if instance is None:
            return False
        instance.status = "terminated"
        logger.info(f"终止意识实例: {instance_id}")
        return True

    def touch(self, instance_id: str, *, timestamp: str = "") -> None:
        """Update last_active_at for an instance."""
        instance = self._instances.get(instance_id)
        if instance and instance.is_active and timestamp:
            instance.last_active_at = timestamp

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, instance_id: str) -> ConsciousnessInstance | None:
        return self._instances.get(instance_id)

    def get_active(self) -> list[ConsciousnessInstance]:
        """Get all active consciousness instances."""
        return [inst for inst in self._instances.values() if inst.is_active]

    def get_all(self) -> list[ConsciousnessInstance]:
        """Get all instances regardless of status."""
        return list(self._instances.values())

    def get_for_stream(self, stream_id: str) -> ConsciousnessInstance | None:
        """Find the active instance responsible for a given stream."""
        for inst in self._instances.values():
            if inst.is_active and stream_id in inst.stream_ids:
                return inst
        # Fallback: chat_global handles unassigned streams
        return self._instances.get(CHAT_GLOBAL_INSTANCE_ID)

    def get_by_kind(self, kind: str) -> list[ConsciousnessInstance]:
        """Get all active instances of a specific kind."""
        return [
            inst
            for inst in self._instances.values()
            if inst.is_active and inst.kind == kind
        ]

    @property
    def active_count(self) -> int:
        return sum(1 for inst in self._instances.values() if inst.is_active)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "instances": {
                inst_id: inst.to_dict()
                for inst_id, inst in self._instances.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConsciousnessRegistry":
        registry = cls()
        raw_instances = data.get("instances")
        if isinstance(raw_instances, dict):
            for inst_id, inst_data in raw_instances.items():
                if isinstance(inst_data, dict):
                    instance = ConsciousnessInstance.from_dict(inst_data)
                    registry._instances[str(inst_id)] = instance
        registry._ensure_chat_global()
        return registry

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=None, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "ConsciousnessRegistry":
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise ValueError("registry JSON must be an object")
        return cls.from_dict(raw)

    @classmethod
    def load(cls, path: Path) -> "ConsciousnessRegistry":
        """Load from disk; returns fresh registry if missing or corrupt."""
        try:
            if path.exists():
                return cls.from_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"加载意识注册表失败，使用默认: {exc}")
        return cls()

    def save(self, path: Path) -> None:
        """Atomically persist to disk."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(self.to_json(), encoding="utf-8")
            import os
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning(f"保存意识注册表失败: {exc}")
