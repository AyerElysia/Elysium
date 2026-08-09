"""P3-12 生产 Provider：只包装现有领域 owner，不创建平行状态。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from plugins.life_engine.service.registry import get_life_engine_service
from plugins.life_engine.tools.schedule_tools import ScheduleStore
from plugins.life_engine.tools.todo_tools import TodoStorage
from src.core.managers import get_plugin_manager
from src.kernel.scheduler import get_unified_scheduler

from .auth_store import SessionRecord
from .p312 import P312Providers


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return {"value": value}


def _life_service() -> Any:
    service = get_life_engine_service()
    if service is None:
        raise RuntimeError("life_engine capability is unavailable")
    return service


def _life_plugin() -> Any:
    plugin = get_plugin_manager().get_plugin("life_engine")
    if plugin is None:
        raise RuntimeError("life_engine capability is unavailable")
    return plugin


class RuntimeConsciousnessProvider:
    @staticmethod
    def _registry() -> Any:
        return _life_service().consciousness_registry

    async def _refresh(self) -> Any:
        registry = self._registry()
        refresh = getattr(registry, "refresh", None)
        if callable(refresh):
            value = refresh()
            if asyncio.iscoroutine(value):
                await value
        return registry

    @staticmethod
    def _public(instance: Any) -> dict[str, Any]:
        raw = _dict(instance)
        raw.pop("process_epoch", None)
        metadata = raw.get("metadata")
        raw["metadata"] = {
            key: value
            for key, value in dict(metadata or {}).items()
            if key in {"episode_id", "session_id", "provider", "scene", "owner"}
        }
        raw["presence_only"] = True
        return raw

    async def list_instances(self) -> list[dict[str, Any]]:
        registry = await self._refresh()
        return [self._public(item) for item in registry.get_all()]

    async def get_instance(self, instance_id: str) -> dict[str, Any]:
        registry = await self._refresh()
        item = registry.get(instance_id)
        if item is None:
            raise KeyError(instance_id)
        return self._public(item)

    async def get_stream_owner(self, stream_id: str) -> dict[str, Any]:
        registry = await self._refresh()
        owners = [
            item
            for item in registry.get_all()
            if item.is_active and stream_id in item.stream_ids
        ]
        if not owners:
            raise KeyError(stream_id)
        if len(owners) != 1:
            raise RuntimeError("multiple active stream owners")
        return self._public(owners[0])

    async def health(self) -> dict[str, Any]:
        registry = await self._refresh()
        raw = dict(registry.health_snapshot())
        raw.pop("database_path", None)
        raw.pop("process_epoch", None)
        raw["presence_only"] = True
        return raw

    async def _action(
        self,
        action: str,
        instance_id: str,
        *,
        expected_revision: int | None,
        reason: str,
    ) -> dict[str, Any]:
        registry = await self._refresh()
        item = registry.get(instance_id)
        if item is None:
            raise KeyError(instance_id)
        if expected_revision is not None and int(item.revision) != expected_revision:
            raise ValueError("presence revision conflict")
        function = getattr(registry, action)
        value = function(instance_id, reason=reason)
        if asyncio.iscoroutine(value):
            value = await value
        if not value:
            raise ValueError(f"consciousness instance cannot {action} from current status")
        save = getattr(_life_service(), "save_consciousness_registry_async", None)
        if callable(save):
            await save()
        return await self.get_instance(instance_id)

    async def suspend(self, instance_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self._action("suspend", instance_id, **kwargs)

    async def resume(self, instance_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self._action("resume", instance_id, **kwargs)

    async def drain(
        self,
        instance_id: str,
        *,
        expected_revision: int | None,
        reason: str,
    ) -> dict[str, Any]:
        # 当前 Registry 尚无独立 draining 状态；安全 drain 等价为先停止新工作
        # 的 suspend，不冒充实例已终止或任务已全部消费。
        result = await self._action(
            "suspend",
            instance_id,
            expected_revision=expected_revision,
            reason=f"drain:{reason}",
        )
        result["drain_state"] = "new_work_stopped"
        return result


class RuntimeWorldProvider:
    @staticmethod
    def _projection() -> Any:
        return _life_service().world_projection

    async def list_assertions(self, *, session: SessionRecord) -> list[dict[str, Any]]:
        del session
        projection = self._projection()
        result = projection.list_assertions(include_retracted=True)
        if asyncio.iscoroutine(result):
            result = await result
        return [_dict(item) for item in result]

    async def changes_since(self, after: int, *, session: SessionRecord) -> list[dict[str, Any]]:
        del session
        result = self._projection().changes_since(after)
        if asyncio.iscoroutine(result):
            result = await result
        return [_dict(item) for item in result]

    async def health(self) -> dict[str, Any]:
        value = self._projection().health_snapshot()
        if asyncio.iscoroutine(value):
            value = await value
        result = dict(value)
        result.pop("database_path", None)
        return result

    async def report_observation(self, **payload: Any) -> dict[str, Any]:
        return dict(await _life_service().report_world_observation(**payload))

    async def rebuild(self, *, batch_size: int = 500) -> dict[str, Any]:
        del batch_size
        frontier = await _life_service().rebuild_world_projection()
        return {"projection": "world", "as_of_ingest_position": frontier}


class RuntimeMemoryProvider:
    @staticmethod
    def _service() -> Any:
        memory = _life_service().memory_service
        if memory is None:
            raise RuntimeError("memory capability is unavailable")
        return memory

    async def search(self, query: str, *, top_k: int, session: SessionRecord) -> list[dict[str, Any]]:
        del session
        values = await self._service().search_memory(
            query,
            top_k=top_k,
            enable_association=True,
            return_bundles=True,
        )
        return [_dict(item) for item in values]

    async def get_experience(self, experience_id: str, *, session: SessionRecord) -> dict[str, Any]:
        del session
        # Experience 没有按 id 的独立 port；分页读取时保留权威顺序，不直连 DB。
        after = 0
        for _ in range(10000):
            batch = await self._service().list_experiences_after(after, limit=500)
            if not batch:
                break
            for item in batch:
                raw = _dict(item)
                if raw.get("experience_id") == experience_id or raw.get("event_id") == experience_id:
                    return raw
            after = max(int(getattr(item, "sequence", 0)) for item in batch)
        raise KeyError(experience_id)

    async def artifact_versions(self, artifact_id: str, *, session: SessionRecord) -> list[dict[str, Any]]:
        del session
        return [_dict(item) for item in await self._service().get_memory_artifact_history(artifact_id)]

    async def artifact_version(self, artifact_id: str, version: int, *, session: SessionRecord) -> dict[str, Any]:
        values = await self.artifact_versions(artifact_id, session=session)
        if version < 1 or version > len(values):
            raise KeyError(f"{artifact_id}:{version}")
        return values[version - 1]

    async def graph(self, *, session: SessionRecord) -> list[dict[str, Any]]:
        del session
        stats = await self._service().get_stats()
        return [{"projection": "memory_graph", "stats": stats}]

    async def stats(self, *, session: SessionRecord) -> dict[str, Any]:
        del session
        return dict(await self._service().get_stats())

    async def health(self, *, session: SessionRecord) -> dict[str, Any]:
        del session
        result = dict(await self._service().health_snapshot())
        result.pop("database_path", None)
        return result

    async def rebuild_projection(self, projection: str, *, session: SessionRecord) -> dict[str, Any]:
        del session
        if projection != "associations":
            raise ValueError("only the associations memory projection is rebuildable")
        count = await self._service().rebuild_memory_association_projection()
        return {"projection": projection, "rebuilt_count": count}


class RuntimeCommitmentsProvider:
    @staticmethod
    def _workspace() -> Path:
        return _life_service()._workspace_dir()

    def _todo_store(self) -> TodoStorage:
        return TodoStorage(self._workspace())

    def _schedule_store(self) -> ScheduleStore:
        return ScheduleStore(_life_plugin())

    async def list_todos(self, *, session: SessionRecord) -> list[dict[str, Any]]:
        del session
        return [item.to_dict() for item in await asyncio.to_thread(self._todo_store().load)]

    async def get_todo(self, todo_id: str, *, session: SessionRecord) -> dict[str, Any]:
        del session
        item = await asyncio.to_thread(self._todo_store().get, todo_id)
        if item is None:
            raise KeyError(todo_id)
        return item.to_dict()

    async def todo_events(self, todo_id: str, *, session: SessionRecord) -> list[dict[str, Any]]:
        todo = await self.get_todo(todo_id, session=session)
        events = [dict(item) for item in todo.get("progress_log", [])]
        events.extend(dict(item) for item in todo.get("completion_log", []))
        return sorted(events, key=lambda item: str(item.get("at") or ""))

    async def _schedule_summary(self, record: Any) -> dict[str, Any]:
        result = record.to_dict()
        info = None
        if record.schedule_id:
            info = await get_unified_scheduler().get_task_info(record.schedule_id)
        result["runtime"] = info or {"status": "missing"}
        return result

    async def list_schedules(self, *, session: SessionRecord) -> list[dict[str, Any]]:
        del session
        records = await asyncio.to_thread(self._schedule_store().list_records)
        return [await self._schedule_summary(item) for item in records]

    async def get_schedule(self, record_id: str, *, session: SessionRecord) -> dict[str, Any]:
        del session
        matches = await asyncio.to_thread(self._schedule_store().find_matches, record_id)
        if len(matches) != 1:
            raise KeyError(record_id)
        return await self._schedule_summary(matches[0])

    async def suggest(
        self,
        *,
        suggestion: str,
        source: str,
        target_hint: str,
        notes: str,
        occurrence_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        service = _life_service()
        stable = occurrence_id.strip() or f"commitment_suggestion_{uuid4().hex}"
        occurred_at = datetime.now(UTC).isoformat()
        payload = {
            "suggestion_id": stable,
            "suggestion": suggestion,
            "source": source,
            "target_hint": target_hint,
            "notes": notes,
            "actor_id": actor_id,
            "status": "external_suggestion",
            "occurred_at": occurred_at,
        }
        from plugins.life_engine.service.event_bus import LifeEvent

        event = LifeEvent(
            event_id=stable,
            sequence=0,
            timestamp=occurred_at,
            source="app_api_v1.commitment_suggestion",
            channel="system",
            event_type="commitment.suggestion.reported",
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            priority=5,
            salience=0.0,
            metadata={"external_suggestion": True},
            occurrence_id=stable,
            source_instance_id="external_administrator",
        )
        store = service._get_life_event_store()
        persisted = await store.append(event)
        return {
            "suggestion_id": stable,
            "event_id": persisted.event_id,
            "ingest_position": persisted.sequence,
            "status": "external_suggestion",
        }

    async def _operate_schedule(
        self,
        record_id: str,
        *,
        action: str,
        expected_revision: int | None,
        reason: str,
    ) -> dict[str, Any]:
        del reason
        matches = await asyncio.to_thread(self._schedule_store().find_matches, record_id)
        if len(matches) != 1:
            raise KeyError(record_id)
        record = matches[0]
        revision = int(datetime.fromisoformat(record.updated_at).timestamp()) if record.updated_at else 0
        if expected_revision is not None and expected_revision != revision:
            raise ValueError("schedule revision conflict")
        if not record.schedule_id:
            raise ValueError("schedule runtime is missing")
        scheduler = get_unified_scheduler()
        changed = (
            await scheduler.pause_schedule(record.schedule_id)
            if action == "pause"
            else await scheduler.resume_schedule(record.schedule_id)
        )
        if not changed:
            raise ValueError(f"schedule cannot {action} from current status")
        result = await self._schedule_summary(record)
        result["revision"] = revision
        result["technical_control_only"] = True
        return result

    async def pause(self, record_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self._operate_schedule(record_id, action="pause", **kwargs)

    async def resume(self, record_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self._operate_schedule(record_id, action="resume", **kwargs)


class RuntimeAutonomyProvider:
    @staticmethod
    def _store() -> Any:
        return _life_service()._autonomy_store()

    async def list_intents(self, *, session: SessionRecord) -> list[dict[str, Any]]:
        del session
        return [item.to_dict() for item in await self._store().load()]

    async def get_intent(self, intent_id: str, *, session: SessionRecord) -> dict[str, Any]:
        del session
        item = await self._store().get(intent_id)
        if item is None:
            raise KeyError(intent_id)
        return item.to_dict()

    async def occurrences(self, intent_id: str, *, session: SessionRecord) -> list[dict[str, Any]]:
        intent = await self.get_intent(intent_id, session=session)
        result: list[dict[str, Any]] = []
        if intent.get("last_occurrence_id"):
            result.append({
                "occurrence_id": intent["last_occurrence_id"],
                "status": intent.get("last_outcome") or "completed",
                "active": False,
            })
        if intent.get("active_occurrence_id"):
            result.append({
                "occurrence_id": intent["active_occurrence_id"],
                "status": intent.get("active_occurrence_status") or "in_flight",
                "started_at": intent.get("active_occurrence_started_at") or "",
                "active": True,
            })
        return result

    async def cancel_occurrence(
        self,
        occurrence_id: str,
        *,
        reason: str,
        actor_id: str,
    ) -> dict[str, Any]:
        del actor_id
        intents = await self._store().load()
        target = next(
            (item for item in intents if item.active_occurrence_id == occurrence_id),
            None,
        )
        if target is None:
            raise KeyError(occurrence_id)
        result = await _life_service().manage_autonomy_intent(
            action="cancel",
            intent_id=target.intent_id,
        )
        result["occurrence_id"] = occurrence_id
        result["reason"] = reason
        result["technical_control_only"] = True
        return result


class RuntimeAbilitiesProvider:
    _ABILITIES = (
        {
            "ability_id": "chat.messaging",
            "name": "聊天与消息",
            "description": "读取会话并通过受管消息合同发送文本或媒体。",
            "module": "chat",
            "required_scopes": ("chat:read", "chat:write"),
        },
        {
            "ability_id": "memory.observation",
            "name": "记忆观察",
            "description": "读取正式记忆投影、来源和版本，不修改主体语义。",
            "module": "memory",
            "required_scopes": ("memory:read",),
        },
        {
            "ability_id": "surface.presentation",
            "name": "Neko Surface",
            "description": "连接受管展示面并使用版本化 Surface 协议。",
            "module": "surface",
            "required_scopes": ("surface:read", "surface:connect"),
        },
        {
            "ability_id": "world.observation",
            "name": "世界观察",
            "description": "查看来源保留的世界断言，或追加明确外部观察。",
            "module": "world",
            "required_scopes": ("world:read", "world:observe"),
        },
    )

    async def list_abilities(self, *, session: SessionRecord) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in self._ABILITIES:
            required = tuple(item["required_scopes"])
            result.append({
                **item,
                "state": "available" if set(required) & set(session.scopes) else "unauthorized",
            })
        return result

    async def get_ability(self, ability_id: str, *, session: SessionRecord) -> dict[str, Any]:
        for item in await self.list_abilities(session=session):
            if item["ability_id"] == ability_id:
                return item
        raise KeyError(ability_id)


class RuntimeSurfaceProvider:
    @staticmethod
    def _gateway() -> Any:
        plugin = get_plugin_manager().get_plugin("neko_surface")
        gateway = getattr(plugin, "gateway", None) if plugin is not None else None
        if gateway is None:
            raise RuntimeError("surface capability is unavailable")
        return gateway

    async def list_surfaces(self, *, session: SessionRecord) -> list[dict[str, Any]]:
        del session
        snapshot = await self._gateway().snapshot()
        surface_ids = sorted({str(item.get("surface_id") or "") for item in snapshot.get("clients", []) if item.get("surface_id")})
        if not surface_ids:
            surface_ids = ["neko-default"]
        return [
            {
                "surface_id": surface_id,
                "protocol": "elysia.surface.v1",
                "connected": any(item.get("surface_id") == surface_id for item in snapshot.get("clients", [])),
                "owns_identity": False,
            }
            for surface_id in surface_ids
        ]

    async def status(self, surface_id: str, *, session: SessionRecord) -> dict[str, Any]:
        del session
        snapshot = await self._gateway().snapshot()
        clients = [item for item in snapshot.get("clients", []) if item.get("surface_id") == surface_id]
        return {
            "surface_id": surface_id,
            "protocol": "elysia.surface.v1",
            "connected": bool(clients),
            "connection_count": len(clients),
            "owns_identity": False,
        }

    async def serve(self, websocket: Any, *, surface_id: str, grant: Any) -> None:
        # Gateway 的 hello 仍校验 surface_id；统一 ticket 已替代旧静态 token。
        await self._gateway().serve_authorized(
            websocket,
            expected_surface_id=surface_id,
            input_enabled="surface:input" in grant.scopes,
            actor_id=grant.actor_id,
        )

    async def connections(self, surface_id: str, *, session: SessionRecord) -> list[dict[str, Any]]:
        del session
        return await self._gateway().connection_summaries(surface_id)

    async def disconnect(
        self,
        surface_id: str,
        connection_id: str,
        *,
        reason: str,
        session: SessionRecord,
    ) -> dict[str, Any]:
        del session
        changed = await self._gateway().disconnect_connection(
            surface_id,
            connection_id,
            reason=reason,
        )
        if not changed:
            raise KeyError(connection_id)
        return {
            "surface_id": surface_id,
            "connection_id": connection_id,
            "disconnected": True,
        }


def create_runtime_p312_providers() -> P312Providers:
    """创建只持有无状态 wrapper 的 P3-12 生产依赖。"""

    return P312Providers(
        consciousness=RuntimeConsciousnessProvider(),
        world=RuntimeWorldProvider(),
        memory=RuntimeMemoryProvider(),
        commitments=RuntimeCommitmentsProvider(),
        autonomy=RuntimeAutonomyProvider(),
        abilities=RuntimeAbilitiesProvider(),
        surfaces=RuntimeSurfaceProvider(),
    )


__all__ = ["create_runtime_p312_providers"]
