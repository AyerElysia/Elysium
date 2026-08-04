"""life_engine 生命中枢服务核心模块。

生命中枢是同一个主体在不同运行模式间切换的骨架。
它通过周期性心跳来处理堆积的消息、进行内部思考，并为工具调用、
对外交流与状态沉淀提供基础能力。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import traceback
from dataclasses import asdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseService
from src.core.components.utils import should_strip_auto_reason_argument
from src.core.models.message import Message, MessageType
from src.kernel.concurrency import get_task_manager
from src.kernel.llm import LLMPayload, ROLE, Text, ToolRegistry, ToolResult
from src.kernel.scheduler import get_unified_scheduler, TriggerType

if TYPE_CHECKING:
    from ..memory.service import LifeMemoryService

from .audit import (
    get_life_log_file,
    log_error,
    log_heartbeat as log_heartbeat_event,
    log_heartbeat_model_response,
    log_lifecycle,
    log_message_received,
    log_wake_context_injected,
)
from ..core.chat_history import (
    build_chat_history_text,
    build_global_chat_history_text_from_db,
    message_flag,
)
from ..core.config import LifeEngineConfig
from ..core.context_assembly import LifeChatterContextAssembler
from ..core.router_context_projection import (
    RouterContextDraft,
    RouterContextProjection,
    RouterContextSource,
    build_router_context_projection_prompt,
)
from ..core.subject_context_projection import (
    SubjectContextDraft,
    SubjectContextProjection,
    SubjectContextSource,
    build_subject_context_projection_prompt,
    validate_subject_projection_text,
)
from ..core.send_targets import format_send_targets_for_prompt, list_recent_send_targets
from ..core.tool_parallel import iter_life_tool_call_batches
from ..autonomy import (
    AutonomyIntent,
    AutonomyIntentStore,
    build_intent,
    cleanup_autonomy_schedules,
    format_due_message,
    occurrence_id_for,
    recurring_lease_reason,
    restore_autonomy_intents,
    schedule_autonomy_intent as register_autonomy_schedule,
)
from ..streams.manager import ThoughtStreamManager
from ..drives.impulse import ImpulseEngine
from ..drives.rules import DEFAULT_RULES
from ..curiosity import CuriosityEngine
from ..prompts.sections import (
    DEFAULT_HEARTBEAT_SECTIONS,
    SectionContext,
    render_heartbeat_sections,
)
from ..constants import (
    HEARTBEAT_IDLE_CRITICAL_THRESHOLD,
    HEARTBEAT_IDLE_WARNING_THRESHOLD,
    LIFE_CHATTER_GLOBAL_CURSOR_KEY,
)
from ..memory.prompting import (
    build_memory_maintenance_prompt,
    load_memory_prompt_data,
    render_memory_prompt,
    should_emit_memory_maintenance_prompt,
)
from ..trace.store import LifeTraceRecord, LifeTraceStore
from .event_builder import (
    EventBuilder,
    EventType,
    LifeEngineEvent,
    LifeEngineState,
    _format_current_time,
    _format_time_display,
    _now_iso,
    _parse_hhmm,
    _shorten_text,
)
from .followup import FollowupState, PendingFollowup
from .state_manager import (
    StatePersistence,
    get_file_metadata,
    minutes_since_time,
)
from .attention import AttentionRouter
from .subconscious_context import (
    PreparedHeartbeatContext,
    SubconsciousContextManager,
    SubconsciousSummary,
)
from .world_state import WorldState
from .async_presence import (
    AsyncConsciousnessRegistry,
    flush_presence_lifecycle_events,
)
from .consciousness import ConsciousnessInstance, ConsciousnessRegistry
from .event_bus import (
    LifeEvent,
    LifeEventBus,
    LifeEventChannel,
    LifeEventPriority,
    RawEventStore,
)
from .perception_gateway import (
    AsyncPerceptionGateway,
    PerceptionGateway,
    PreparedPerception,
)
from .world_projection import (
    WORLD_LEGACY_IMPORT_EVENT,
    WORLD_OBSERVATION_EVENT,
    WORLD_PROJECTION_DB_FILE,
    WorldProjectionStore,
    legacy_snapshot_assertions,
)
from .integrations import (
    DFCIntegration,
    MemoryIntegration,
)
from .self_pause import (
    apply_self_pause,
    build_self_pause_status,
    clear_self_pause_state,
    self_pause_status,
)

if TYPE_CHECKING:
    from ..memory.service import LifeMemoryService


logger = get_logger("life_engine", display="life_engine")
_USER_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "USER.md"


def _resolve_heartbeat_timeout(configured: float, model_set: Any) -> float:
    """把心跳的外层超时抬到至少两个「单模型尝试」之上。

    request 层对每个模型各自套了一层 ``asyncio.wait_for(timeout=model["timeout"])``，
    超时后由 failover policy 轮换到下一个模型。但外层这一层 ``wait_for`` 一旦先到点，
    抛出的是 ``CancelledError``——request.py 对它是裸 ``raise``，不进 failover。
    于是当外层超时 == provider 超时（本仓库两边都是 120s）时，两个定时器同时到点，
    外层取消先赢：模型列表里剩下的 5 个候补一个都轮不到，整个 failover 形同虚设。

    这里保证外层预算 > 单次尝试预算，让内层至少有机会换一次模型。

    Args:
        configured: 配置里声明的心跳超时（秒）
        model_set: 本次心跳使用的模型集，用于读取单模型超时

    Returns:
        float: 实际使用的外层超时秒数
    """
    per_attempt = 0.0
    try:
        for entry in model_set or []:
            value = entry.get("timeout") if isinstance(entry, dict) else None
            if isinstance(value, (int, float)) and value > 0:
                per_attempt = max(per_attempt, float(value))
    except Exception:  # noqa: BLE001 - 配置异常不应阻断心跳
        per_attempt = 0.0

    budget = configured
    if per_attempt > 0:
        budget = max(budget, per_attempt * 2 + 15.0)

    return max(10.0, min(900.0, budget))


class LifeEngineService(BaseService):
    """life_engine 心跳服务。

    这个版本使用统一的事件流模型，所有交互保持时间连续性。
    不参与正常聊天流程，不做回复决策。
    """

    service_name: str = "life_engine"
    service_description: str = "生命中枢服务，维持并行心跳与事件流上下文"
    version: str = "3.4.0"

    @classmethod
    def get_instance(cls) -> "LifeEngineService | None":
        """获取服务单例（供工具使用）。"""
        from .registry import get_life_engine_service

        return get_life_engine_service()

    def __init__(self, plugin) -> None:
        super().__init__(plugin)
        self._legacy_config_warning_emitted: bool = False
        self._state = LifeEngineState()
        self._state_dirty: bool = False
        self._heartbeat_task_id: str | None = None
        self._memory_index_task_id: str | None = None
        self._memory_witness_task_id: str | None = None
        self._memory_witness_coordinator = None
        self._shared_sync_task_id: str | None = None
        self._shared_sync_bridge = None
        self._shared_sync_error: str = ""
        self._memory_archive_sync_task_id: str | None = None
        self._memory_archive_sync_bridge = None
        self._memory_archive_sync_error: str = ""
        self._router_context_projection_task_id: str | None = None
        self._router_context_projection: RouterContextProjection | None = None
        self._subject_context_projections: dict[
            tuple[str, int], SubjectContextProjection
        ] = {}
        self._stop_event: asyncio.Event | None = None
        self._pending_events: list[LifeEngineEvent] = []
        self._event_history: list[LifeEngineEvent] = []
        self._lock: asyncio.Lock | None = None
        self._heartbeat_run_lock = asyncio.Lock()
        settings = getattr(getattr(plugin, "config", None), "settings", None)
        configured_budget = getattr(settings, "subconscious_context_max_chars", None)
        if configured_budget is None:
            configured_budget = getattr(settings, "heartbeat_context_max_chars", None)
        try:
            context_budget = max(1000, int(configured_budget or 16000))
        except (TypeError, ValueError):
            context_budget = 16000
        try:
            summary_max = max(200, int(getattr(settings, "subconscious_summary_max_chars", None) or 4000))
        except (TypeError, ValueError):
            summary_max = 4000
        try:
            entry_max = max(40, int(getattr(settings, "subconscious_entry_max_chars", None) or 480))
        except (TypeError, ValueError):
            entry_max = 480
        try:
            recent_groups = max(0, int(getattr(settings, "subconscious_recent_groups", None) or 5))
        except (TypeError, ValueError):
            recent_groups = 5
        try:
            summary_max_entries = max(10, int(getattr(settings, "subconscious_summary_max_entries", None) or 60))
        except (TypeError, ValueError):
            summary_max_entries = 60
        self._subconscious_context: SubconsciousContextManager = SubconsciousContextManager(
            max_chars=context_budget,
            recent_group_count=recent_groups,
            summary_max_chars=summary_max,
            entry_max_chars=entry_max,
            summary_max_entries=summary_max_entries,
        )
        self._sleep_state_active: bool = False
        self._self_pause_skip_logged: bool = False
        self._memory_service: LifeMemoryService | None = None
        self._last_decay_date: str | None = None
        from ..storage.factory import settings_from_life_engine_config

        self._storage_factory_settings = settings_from_life_engine_config(
            self._cfg()
        )
        self._selectable_storage_enabled = bool(
            self._storage_factory_settings.enabled
        )
        self._storage_runtime: Any | None = None
        self._presence_world_stores: Any | None = None
        self._life_event_store: Any | None = None
        self._subject_document_store: Any | None = None
        self._subject_workspace_observer: Any | None = None
        self._subject_workspace_projector: Any | None = None
        self._storage_health_cache: dict[str, Any] = {
            "status": (
                "initializing" if self._selectable_storage_enabled else "disabled"
            ),
            "backend": self._storage_factory_settings.authoritative_backend.value,
            "reason": (
                "selected storage has not started"
                if self._selectable_storage_enabled
                else "selectable storage runtime is not enabled"
            ),
        }

        # 结构化世界模型（潜意识共享内在世界）
        self._world_state: WorldState = WorldState.load(
            self._workspace_dir() / "runtime" / "world_state.json"
        )

        # 意识实例注册表（多意识协调）
        self._consciousness_registry: (
            ConsciousnessRegistry | AsyncConsciousnessRegistry | None
        )
        if self._selectable_storage_enabled:
            self._consciousness_registry = None
        else:
            self._consciousness_registry = ConsciousnessRegistry.load(
                self._workspace_dir() / "runtime" / "consciousness_registry.json"
            )

        # 集成管理器
        self._dfc_integration: DFCIntegration | None = None
        self._memory_integration: MemoryIntegration | None = None

        # 事件构建器
        self._event_builder = EventBuilder(self._next_sequence)

        # 思考流系统
        self._thought_manager: ThoughtStreamManager | None = None

        # 冲动引擎
        self._impulse_engine: ImpulseEngine | None = None

        # 异步好奇层
        self._curiosity_engine: CuriosityEngine | None = None
        self._curiosity_inflight: bool = False

        # 三环自学习系统
        self._learning_scheduler = None  # LearningScheduler | None

        # 状态持久化
        self._state_persistence: StatePersistence | None = None
        self._event_bus: LifeEventBus | None = None
        self._world_projection: Any | None = None
        self._perception_gateway: PerceptionGateway | AsyncPerceptionGateway | None = None
        self._pending_chatter_perceptions: dict[str, PreparedPerception] = {}
        self._attention_router: AttentionRouter | None = None
        self._last_memory_maintenance_prompt_at: str | None = None
        self._followup_states: dict[str, FollowupState] = {}
        self._scheduler = None

    @property
    def memory_service(self) -> LifeMemoryService | None:
        """兼容旧调用方的公开记忆服务访问入口。"""
        return self._memory_service

    @property
    def selected_subject_storage_enabled(self) -> bool:
        """Return whether subject files require the selected durable writer."""

        return self._selectable_storage_enabled

    @property
    def world_state(self) -> WorldState:
        """Return the non-authoritative legacy migration snapshot."""
        return self._world_state

    def save_world_state(self) -> None:
        """Export the derived world projection for read-only diagnostics."""

        if self._selectable_storage_enabled:
            raise RuntimeError(
                "SelectedWorldExportRequiresAwait: use the async World Port"
            )
        self._get_perception_gateway()
        projection = self._get_world_projection()
        target = self._workspace_dir() / "runtime" / "world_projection.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                projection.canonical_snapshot(),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(target)

    @property
    def consciousness_registry(
        self,
    ) -> ConsciousnessRegistry | AsyncConsciousnessRegistry:
        """Return the initialized operational Presence registry."""

        if self._consciousness_registry is None:
            raise RuntimeError(
                "SelectedPresenceNotStarted: await LifeEngineService.start() first"
            )
        return self._consciousness_registry

    def save_consciousness_registry(self) -> None:
        """Persist the disabled-mode compatibility registry synchronously."""

        if self._selectable_storage_enabled:
            raise RuntimeError(
                "SelectedPresenceRequiresAwait: use "
                "save_consciousness_registry_async()"
            )
        registry = self.consciousness_registry
        assert isinstance(registry, ConsciousnessRegistry)
        registry.save(
            self._workspace_dir() / "runtime" / "consciousness_registry.json"
        )
        registry.flush_lifecycle_events(
            self._get_event_bus().store.append_sync
        )
        if self._world_projection is not None:
            self._world_projection.catch_up(self._get_event_bus().store)

    async def save_consciousness_registry_async(self) -> None:
        """Flush lifecycle evidence through the active backend contract."""

        if not self._selectable_storage_enabled:
            await asyncio.to_thread(self.save_consciousness_registry)
            return
        stores = self._require_presence_world_stores()
        ledger = self._get_life_event_store()
        await flush_presence_lifecycle_events(stores.presence, ledger)
        await self.catch_up_world_projection()

    async def register_consciousness_instance(
        self,
        instance: ConsciousnessInstance,
    ) -> ConsciousnessInstance:
        """Register one runtime window and durably publish its lifecycle."""

        registry = self.consciousness_registry
        if isinstance(registry, AsyncConsciousnessRegistry):
            result = await registry.register(instance)
        else:
            result = await asyncio.to_thread(registry.register, instance)
        await self.save_consciousness_registry_async()
        return result

    async def touch_consciousness_instance(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "activity",
    ) -> None:
        """Commit liveness before returning to the current runtime."""

        registry = self.consciousness_registry
        if isinstance(registry, AsyncConsciousnessRegistry):
            await registry.touch(
                instance_id,
                timestamp=timestamp,
                reason=reason,
            )
        else:
            await asyncio.to_thread(
                registry.touch,
                instance_id,
                timestamp=timestamp,
                reason=reason,
            )
        await self.save_consciousness_registry_async()

    async def resume_consciousness_instance(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "requested",
    ) -> bool:
        """Resume one suspended runtime and durably reclaim its streams."""

        registry = self.consciousness_registry
        if isinstance(registry, AsyncConsciousnessRegistry):
            changed = await registry.resume(
                instance_id,
                timestamp=timestamp,
                reason=reason,
            )
        else:
            changed = await asyncio.to_thread(
                registry.resume,
                instance_id,
                timestamp=timestamp,
                reason=reason,
            )
        if changed:
            await self.save_consciousness_registry_async()
        return changed

    async def suspend_consciousness_instance(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "requested",
    ) -> bool:
        """Suspend one runtime and durably release its stream claims."""

        registry = self.consciousness_registry
        if isinstance(registry, AsyncConsciousnessRegistry):
            changed = await registry.suspend(
                instance_id,
                timestamp=timestamp,
                reason=reason,
            )
        else:
            changed = await asyncio.to_thread(
                registry.suspend,
                instance_id,
                timestamp=timestamp,
                reason=reason,
            )
        if changed:
            await self.save_consciousness_registry_async()
        return changed

    async def terminate_consciousness_instance(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "requested",
    ) -> bool:
        """Commit termination and release stream ownership before returning."""

        registry = self.consciousness_registry
        if isinstance(registry, AsyncConsciousnessRegistry):
            changed = await registry.terminate(
                instance_id,
                timestamp=timestamp,
                reason=reason,
            )
        else:
            changed = await asyncio.to_thread(
                registry.terminate,
                instance_id,
                timestamp=timestamp,
                reason=reason,
            )
        if changed:
            await self.save_consciousness_registry_async()
        return changed

    def _get_lock(self) -> asyncio.Lock:
        """获取懒加载锁。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def storage_runtime(self) -> Any | None:
        """Expose the one service-owned selected runtime to domain consumers."""

        if not self._selectable_storage_enabled:
            return None
        if self._storage_runtime is None:
            raise RuntimeError(
                "SelectedStorageRuntimeNotStarted: LifeEngineService must open "
                "the coherent runtime before injecting domain consumers"
            )
        return self._storage_runtime

    async def _open_selected_storage_runtime(self) -> None:
        """Open the selected runtime exactly once under service ownership."""

        if not self._selectable_storage_enabled or self._storage_runtime is not None:
            return
        from ..storage.factory import open_storage_backend

        self._storage_runtime = await open_storage_backend(
            self._storage_factory_settings
        )

    def _require_selected_memory_service(self) -> None:
        """Fail closed when selected storage could not attach the memory domain."""

        if self._selectable_storage_enabled and self._memory_service is None:
            raise RuntimeError(
                "SelectedMemoryStorageInitializationFailed: selected storage "
                "must attach Memory to the LifeEngineService-owned runtime"
            )

    async def _start_selected_storage(self) -> None:
        """Attach all selected life domains to the service-owned runtime."""

        if (
            not self._selectable_storage_enabled
            or self._presence_world_stores is not None
        ):
            return
        from ..storage.domain_factory import open_presence_world_stores
        from ..storage.event_factory import open_life_event_store
        from ..storage.subject_factory import open_subject_document_store
        from ..storage.subject_workspace import (
            SubjectWorkspaceObserver,
            SubjectWorkspaceProjector,
        )

        await self._open_selected_storage_runtime()
        runtime = self.storage_runtime
        ledger = await open_life_event_store(
            runtime,
            initialize_schema=False,
        )
        stores = await open_presence_world_stores(
            runtime,
            initialize_schema=False,
        )
        subject_store = await open_subject_document_store(
            runtime,
            initialize_schema=False,
        )
        workspace = Path(self._cfg().settings.workspace_path).resolve()
        subject_projector = SubjectWorkspaceProjector(
            subject_store,
            data_root=workspace.parent,
            worker_id=(
                f"{self._storage_factory_settings.authority_owner_id}:"
                "subject-workspace"
            ),
        )
        subject_observer = SubjectWorkspaceObserver(
            subject_store,
            data_root=workspace.parent,
            recorded_source="workspace-observer:life-engine",
        )
        registry = await AsyncConsciousnessRegistry.load(stores.presence)
        event_bus = LifeEventBus(ledger)
        gateway = AsyncPerceptionGateway(
            registry,
            ledger,
            stores.world,
        )
        await flush_presence_lifecycle_events(stores.presence, ledger)
        await gateway.catch_up()
        self._life_event_store = ledger
        self._presence_world_stores = stores
        self._subject_document_store = subject_store
        self._subject_workspace_observer = subject_observer
        self._subject_workspace_projector = subject_projector
        self._consciousness_registry = registry
        self._event_bus = event_bus
        self._world_projection = stores.world
        self._perception_gateway = gateway
        await self.refresh_storage_health()

    async def _close_selected_storage(self) -> None:
        """Flush owned async work and close the single selected runtime."""

        if not self._selectable_storage_enabled:
            return
        errors: list[Exception] = []
        if (
            self._presence_world_stores is not None
            and self._life_event_store is not None
        ):
            try:
                await flush_presence_lifecycle_events(
                    self._presence_world_stores.presence,
                    self._life_event_store,
                )
            except Exception as exc:  # noqa: BLE001 - aggregate owned cleanup
                errors.append(exc)
            if isinstance(self._perception_gateway, AsyncPerceptionGateway):
                try:
                    await self._perception_gateway.catch_up()
                except Exception as exc:  # noqa: BLE001 - aggregate owned cleanup
                    errors.append(exc)
        runtime = self._storage_runtime
        self._subject_workspace_observer = None
        self._subject_workspace_projector = None
        self._subject_document_store = None
        if runtime is not None:
            try:
                await runtime.close()
            except Exception as exc:  # noqa: BLE001 - aggregate owned cleanup
                errors.append(exc)
        self._storage_runtime = None
        self._presence_world_stores = None
        self._life_event_store = None
        self._event_bus = None
        self._world_projection = None
        self._perception_gateway = None
        self._consciousness_registry = None
        self._storage_health_cache = {
            "status": "closed" if not errors else "failed",
            "backend": self._storage_factory_settings.authoritative_backend.value,
            "reason": (
                "selected storage runtime closed"
                if not errors
                else "selected storage close reported errors"
            ),
        }
        if errors:
            raise ExceptionGroup(
                "selected Presence/World storage shutdown failed",
                errors,
            )

    async def _project_subject_version(
        self,
        *,
        logical_path: str,
        version_id: str,
        max_tasks: int,
    ) -> dict[str, Any]:
        """Drain one subject path until the requested durable head is visible."""

        projector = self._subject_workspace_projector
        if projector is None:
            raise RuntimeError("SelectedSubjectProjectorNotStarted")
        for _ in range(max(1, int(max_tasks))):
            result = await projector.project_one(logical_path=logical_path)
            if result.status == "idle":
                raise RuntimeError(
                    f"SubjectProjectionMissing: {logical_path}:{version_id}"
                )
            if result.status == "failed" and result.version_id == version_id:
                raise RuntimeError(
                    f"SubjectProjectionFailed: {logical_path}: {result.detail}"
                )
            if result.version_id == version_id and result.status in {
                "projected",
                "confirmed_existing",
            }:
                return {
                    "status": result.status,
                    "logical_path": result.logical_path,
                    "version_id": result.version_id,
                }
        raise RuntimeError(f"SubjectProjectionBacklogExceeded: {logical_path}")

    async def write_selected_subject_document(
        self,
        *,
        workspace_relative_path: str,
        content_bytes: bytes,
        occurrence_id: str,
        recorded_by: str,
        recorded_source: str,
        encoding: str | None,
        semantic_actor_id: str | None = None,
        semantic_source_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any] | None:
        """Commit a declared subject file before changing its workspace projection.

        Disabled storage returns ``None`` so the legacy file path remains a
        first-class local backend.  Enabled storage fails closed: the file is
        never written ahead of its immutable SubjectDocument version.
        """

        if not self._selectable_storage_enabled:
            return None
        from ..storage.subject_contracts import AppendSubjectDocumentVersion
        from ..storage.subject_workspace import subject_path_from_workspace_relative

        logical_path = subject_path_from_workspace_relative(workspace_relative_path)
        if logical_path is None:
            return None
        workspace = Path(self._cfg().settings.workspace_path).resolve()
        if workspace.name != "life_engine_workspace":
            raise RuntimeError(
                "SelectedSubjectWorkspaceMismatch: selected Subject storage "
                "requires a life_engine_workspace root"
            )
        store = self._subject_document_store
        observer = self._subject_workspace_observer
        if store is None or observer is None:
            raise RuntimeError("SelectedSubjectStorageNotStarted")

        projection: dict[str, Any] | None = None
        head = await store.get_head(logical_path)
        if head is not None:
            task = await store.get_projection_task(
                logical_path,
                head.current_version_id,
            )
            if task is None:
                raise RuntimeError(
                    f"SubjectProjectionStateMissing: {logical_path}:"
                    f"{head.current_version_id}"
                )
            if task.state == "failed":
                raise RuntimeError(
                    f"SubjectProjectionRequiresRepair: {logical_path}:"
                    f"{head.current_version_id}"
                )
            if task.state == "pending":
                try:
                    projection = await self._project_subject_version(
                        logical_path=logical_path,
                        version_id=head.current_version_id,
                        max_tasks=head.revision + 1,
                    )
                except RuntimeError as exc:
                    detail = str(exc)
                    external_divergence = any(
                        marker in detail
                        for marker in (
                            "workspace bytes diverged from the authoritative parent",
                            "new authoritative document would overwrite bytes",
                        )
                    )
                    if not external_divergence:
                        raise
            elif task.state != "confirmed":
                raise RuntimeError(
                    f"SubjectProjectionStateInvalid: {logical_path}:{task.state}"
                )

        if projection is None:
            observed = await observer.observe_file(logical_path)
            if observed.status == "changed_during_read":
                raise RuntimeError(
                    f"SubjectWorkspaceChangedDuringRead: {logical_path}"
                )
            if observed.commit is not None:
                projection = await self._project_subject_version(
                    logical_path=logical_path,
                    version_id=observed.commit.version.version_id,
                    max_tasks=observed.commit.head.revision + 1,
                )
            elif head is not None and observed.status == "missing":
                raise RuntimeError(f"SubjectWorkspaceMissing: {logical_path}")

        head = await store.get_head(logical_path)
        if head is not None:
            current = await store.get_version(head.current_version_id)
            if current.content_bytes == bytes(content_bytes):
                if projection is None:
                    projection = {
                        "status": "confirmed_existing",
                        "logical_path": logical_path,
                        "version_id": current.version_id,
                    }
                return {
                    "status": "unchanged",
                    "logical_path": logical_path,
                    "version_id": current.version_id,
                    "revision": head.revision,
                    "projection": projection,
                }
            expected_revision = head.revision
            expected_head = head.current_version_id
            declared_owner = head.declared_owner
        else:
            expected_revision = 0
            expected_head = ""
            declared_owner = "elysia"
        commit = await store.append_version(
            AppendSubjectDocumentVersion(
                logical_path=logical_path,
                expected_revision=expected_revision,
                expected_head_version_id=expected_head,
                content_bytes=bytes(content_bytes),
                occurrence_id=str(occurrence_id),
                recorded_by=str(recorded_by),
                recorded_source=str(recorded_source),
                declared_owner=declared_owner,
                semantic_actor_id=semantic_actor_id,
                semantic_source_id=semantic_source_id,
                provenance_status=(
                    "complete" if semantic_source_id else "semantic_source_missing"
                ),
                byte_fidelity="exact_bytes",
                encoding=encoding,
                newline_style=None,
                change_context={
                    "operation": "workspace_file_write",
                    "reason": str(reason),
                },
            )
        )
        projection = await self._project_subject_version(
            logical_path=logical_path,
            version_id=commit.version.version_id,
            max_tasks=commit.head.revision + 1,
        )
        self.notify_subject_context_source_changed(workspace_relative_path)
        return {
            "status": "committed",
            "logical_path": logical_path,
            "version_id": commit.version.version_id,
            "revision": commit.head.revision,
            "projection": projection,
        }

    def _require_presence_world_stores(self) -> Any:
        if self._presence_world_stores is None:
            raise RuntimeError(
                "SelectedPresenceWorldNotStarted: await LifeEngineService.start()"
            )
        return self._presence_world_stores

    def _get_life_event_store(self) -> Any:
        if self._selectable_storage_enabled:
            if self._life_event_store is None:
                raise RuntimeError(
                    "SelectedLifeEventStoreNotStarted: await "
                    "LifeEngineService.start()"
                )
            return self._life_event_store
        return self._get_event_bus().store

    def _get_event_bus(self) -> LifeEventBus:
        if self._event_bus is None:
            if self._selectable_storage_enabled:
                raise RuntimeError(
                    "SelectedLifeEventStoreNotStarted: await "
                    "LifeEngineService.start()"
                )
            registry = self.consciousness_registry
            assert isinstance(registry, ConsciousnessRegistry)
            self._event_bus = LifeEventBus(RawEventStore(self._workspace_dir()))
            registry.flush_lifecycle_events(
                self._event_bus.store.append_sync
            )
        return self._event_bus

    def _get_world_projection(self) -> Any:
        """Return the rebuildable subjective world read model."""

        if self._world_projection is None:
            if self._selectable_storage_enabled:
                raise RuntimeError(
                    "SelectedWorldProjectionNotStarted: await "
                    "LifeEngineService.start()"
                )
            self._world_projection = WorldProjectionStore(
                self._workspace_dir() / "runtime" / WORLD_PROJECTION_DB_FILE
            )
        return self._world_projection

    def _migrate_legacy_world_state(self) -> None:
        """Append the legacy JSON snapshot once as migration evidence."""

        if self._selectable_storage_enabled:
            raise RuntimeError(
                "legacy World migration is explicit and cannot run during "
                "selected storage startup"
            )
        projection = self._get_world_projection()
        if projection.legacy_imported():
            projection.catch_up(self._get_event_bus().store)
            return
        snapshot = self._world_state.to_dict()
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        assertions = legacy_snapshot_assertions(snapshot)
        if assertions:
            occurred_at = self._world_state.last_updated_at or _now_iso()
            event = LifeEvent(
                event_id=f"world_legacy_{snapshot_hash}",
                sequence=0,
                timestamp=occurred_at,
                source="life_engine.world_state_migration",
                channel=LifeEventChannel.SYSTEM.value,
                event_type=WORLD_LEGACY_IMPORT_EVENT,
                content=json.dumps(
                    {
                        "assertions": assertions,
                        "legacy_schema_version": self._world_state.schema_version,
                        "legacy_revision": self._world_state.revision,
                        "snapshot_hash": snapshot_hash,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                priority=int(LifeEventPriority.NORMAL),
                salience=0.8,
                occurrence_id=f"world_legacy_{snapshot_hash}",
            )
            self._get_event_bus().store.append_sync(event)
        projection.catch_up(self._get_event_bus().store)
        projection.mark_legacy_imported(snapshot_hash)

    def _get_perception_gateway(
        self,
    ) -> PerceptionGateway | AsyncPerceptionGateway:
        """Return the reliable per-instance perception delivery gateway."""

        if self._perception_gateway is None:
            if self._selectable_storage_enabled:
                raise RuntimeError(
                    "SelectedPerceptionGatewayNotStarted: await "
                    "LifeEngineService.start()"
                )
            self._migrate_legacy_world_state()
            self._perception_gateway = PerceptionGateway(
                self.consciousness_registry,
                self._get_event_bus().store,
                self._get_world_projection(),
            )
        return self._perception_gateway

    @property
    def perception_gateway(self) -> PerceptionGateway | AsyncPerceptionGateway:
        """Expose the supported cross-instance perception integration point."""

        return self._get_perception_gateway()

    @property
    def world_projection(self) -> Any:
        """Expose the rebuildable world projection for diagnostics and queries."""

        self._get_perception_gateway()
        return self._get_world_projection()

    def resolve_consciousness_instance(self, stream_id: str = "") -> str:
        """Resolve a trusted runtime instance from current stream ownership."""

        owner = self.consciousness_registry.get_for_stream(
            str(stream_id or "").strip()
        )
        return owner.instance_id if owner is not None else "chat_global"

    def report_world_observation_sync(
        self,
        report: str,
        *,
        source_instance_id: str,
        subject: str,
        predicate: str = "state_report",
        domain: str = "",
        status: str = "",
        stream_id: str = "",
        observed_at: str = "",
        valid_from: str = "",
        valid_to: str = "",
        supersedes_assertion_id: str = "",
        retracts_assertion_id: str = "",
        value: Any | None = None,
    ) -> dict[str, Any]:
        """Append one attributed observation and project it synchronously."""

        if self._selectable_storage_enabled:
            raise RuntimeError(
                "SelectedWorldObservationRequiresAwait: use "
                "report_world_observation()"
            )
        report_text = str(report or "").strip()
        instance_id = str(source_instance_id or "").strip()
        assertion_subject = str(subject or "").strip()
        assertion_predicate = str(predicate or "").strip()
        if not report_text:
            raise ValueError("world observation report must not be empty")
        if not instance_id:
            raise ValueError("world observation source_instance_id must not be empty")
        if self.consciousness_registry.get(instance_id) is None:
            raise ValueError(
                f"world observation source instance is not registered: {instance_id}"
            )
        if not assertion_subject or not assertion_predicate:
            raise ValueError("world observation subject and predicate must not be empty")
        now = observed_at or _now_iso()
        assertion_id = "assertion_" + uuid4().hex
        event_id = "world_observation_" + uuid4().hex
        assertion: dict[str, Any] = {
            "assertion_id": assertion_id,
            "subject": assertion_subject,
            "predicate": assertion_predicate,
            "value": report_text if value is None else value,
            "domain": str(domain or ""),
            "status": str(status or ""),
            "source_instance_id": instance_id,
            "observed_at": now,
            "valid_from": valid_from or now,
            "valid_to": str(valid_to or ""),
            "supersedes_assertion_id": str(supersedes_assertion_id or ""),
            "retracts_assertion_id": str(retracts_assertion_id or ""),
            "report": report_text,
        }
        event = LifeEvent(
            event_id=event_id,
            sequence=0,
            timestamp=now,
            source="consciousness.report_state",
            channel=LifeEventChannel.LIFE.value,
            event_type=WORLD_OBSERVATION_EVENT,
            content=json.dumps(
                {"assertion": assertion},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            stream_id=str(stream_id or ""),
            priority=int(LifeEventPriority.NORMAL),
            salience=0.8,
            occurrence_id=event_id,
            source_instance_id=instance_id,
            metadata={"assertion_id": assertion_id},
        )
        persisted = self._get_event_bus().store.append_sync(event)
        frontier = self._get_world_projection().catch_up(
            self._get_event_bus().store
        )
        return {
            "event_id": persisted.event_id,
            "occurrence_id": persisted.occurrence_id,
            "assertion_id": assertion_id,
            "ingest_position": persisted.sequence,
            "projection_as_of": frontier,
            "source_instance_id": instance_id,
        }

    async def report_world_observation(
        self,
        report: str,
        *,
        source_instance_id: str,
        subject: str,
        predicate: str = "state_report",
        domain: str = "",
        status: str = "",
        stream_id: str = "",
        observed_at: str = "",
        valid_from: str = "",
        valid_to: str = "",
        supersedes_assertion_id: str = "",
        retracts_assertion_id: str = "",
        value: Any | None = None,
    ) -> dict[str, Any]:
        """Append one attributed observation without blocking the event loop."""

        if not self._selectable_storage_enabled:
            return await asyncio.to_thread(
                self.report_world_observation_sync,
                report,
                source_instance_id=source_instance_id,
                subject=subject,
                predicate=predicate,
                domain=domain,
                status=status,
                stream_id=stream_id,
                observed_at=observed_at,
                valid_from=valid_from,
                valid_to=valid_to,
                supersedes_assertion_id=supersedes_assertion_id,
                retracts_assertion_id=retracts_assertion_id,
                value=value,
            )
        registry = self.consciousness_registry
        assert isinstance(registry, AsyncConsciousnessRegistry)
        await registry.refresh()
        report_text = str(report or "").strip()
        instance_id = str(source_instance_id or "").strip()
        assertion_subject = str(subject or "").strip()
        assertion_predicate = str(predicate or "").strip()
        if not report_text:
            raise ValueError("world observation report must not be empty")
        if not instance_id:
            raise ValueError(
                "world observation source_instance_id must not be empty"
            )
        if registry.get(instance_id) is None:
            raise ValueError(
                f"world observation source instance is not registered: {instance_id}"
            )
        if not assertion_subject or not assertion_predicate:
            raise ValueError(
                "world observation subject and predicate must not be empty"
            )
        now = str(observed_at or "") or _now_iso()
        assertion_id = "assertion_" + uuid4().hex
        event_id = "world_observation_" + uuid4().hex
        assertion: dict[str, Any] = {
            "assertion_id": assertion_id,
            "subject": assertion_subject,
            "predicate": assertion_predicate,
            "value": report_text if value is None else value,
            "domain": str(domain or ""),
            "status": str(status or ""),
            "source_instance_id": instance_id,
            "observed_at": now,
            "valid_from": str(valid_from or "") or now,
            "valid_to": str(valid_to or ""),
            "supersedes_assertion_id": str(supersedes_assertion_id or ""),
            "retracts_assertion_id": str(retracts_assertion_id or ""),
            "report": report_text,
        }
        event = LifeEvent(
            event_id=event_id,
            sequence=0,
            timestamp=now,
            source="consciousness.report_state",
            channel=LifeEventChannel.LIFE.value,
            event_type=WORLD_OBSERVATION_EVENT,
            content=json.dumps(
                {"assertion": assertion},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            stream_id=str(stream_id or ""),
            priority=int(LifeEventPriority.NORMAL),
            salience=0.8,
            occurrence_id=event_id,
            source_instance_id=instance_id,
            metadata={"assertion_id": assertion_id},
        )
        persisted = await self._get_life_event_store().append(event)
        frontier = await self.catch_up_world_projection()
        return {
            "event_id": persisted.event_id,
            "occurrence_id": persisted.occurrence_id,
            "assertion_id": assertion_id,
            "ingest_position": persisted.sequence,
            "projection_as_of": frontier,
            "source_instance_id": instance_id,
        }

    async def prepare_perception(
        self,
        instance_id: str,
    ) -> PreparedPerception:
        """Prepare a retryable transient world delivery for one instance."""

        gateway = self._get_perception_gateway()
        if isinstance(gateway, AsyncPerceptionGateway):
            return await gateway.prepare(instance_id)
        return await asyncio.to_thread(gateway.prepare, instance_id)

    async def commit_perception(
        self,
        prepared: PreparedPerception,
    ) -> tuple[int, int]:
        """Commit a world delivery after its runtime accepted the context."""

        gateway = self._get_perception_gateway()
        if isinstance(gateway, AsyncPerceptionGateway):
            return await gateway.commit(prepared)
        return await asyncio.to_thread(gateway.commit, prepared)

    async def query_world(self, instance_id: str, query: str) -> str:
        """Return the full provenance-aware projection for reflective judgment."""

        gateway = self._get_perception_gateway()
        if isinstance(gateway, AsyncPerceptionGateway):
            return await gateway.query(instance_id, query)
        return await asyncio.to_thread(gateway.query, instance_id, query)

    async def catch_up_world_projection(self) -> int:
        """Advance the selected or legacy projection without blocking the loop."""

        gateway = self._get_perception_gateway()
        if isinstance(gateway, AsyncPerceptionGateway):
            return await gateway.catch_up()
        return await asyncio.to_thread(
            self._get_world_projection().catch_up,
            self._get_event_bus().store,
        )

    async def rebuild_world_projection(self) -> int:
        """Explicitly replay World projection while preserving delivery cursors."""

        gateway = self._get_perception_gateway()
        if isinstance(gateway, AsyncPerceptionGateway):
            return await gateway.rebuild()
        return await asyncio.to_thread(
            self._get_world_projection().rebuild,
            self._get_event_bus().store,
        )

    def _get_attention_router(self) -> AttentionRouter:
        if self._attention_router is None:
            self._attention_router = AttentionRouter(max_events=self._history_limit())
        return self._attention_router

    def _cfg(self) -> LifeEngineConfig:
        config = getattr(self.plugin, "config", None)
        if isinstance(config, LifeEngineConfig):
            if hasattr(config, "thresholds") and hasattr(config, "memory_algorithm"):
                return config
            if not self._legacy_config_warning_emitted:
                logger.warning(
                    "检测到旧版 LifeEngineConfig 对象（缺少 thresholds/memory_algorithm），"
                    "将自动迁移为最新配置结构；建议完整重启进程。"
                )
                self._legacy_config_warning_emitted = True
            migrated = self._migrate_legacy_config(config)
            if migrated is not None:
                return migrated
            return LifeEngineConfig()
        migrated = self._migrate_legacy_config(config)
        if migrated is not None:
            return migrated
        return LifeEngineConfig()

    def _migrate_legacy_config(self, config: object | None) -> LifeEngineConfig | None:
        """将旧版/异构配置对象迁移为当前 LifeEngineConfig。"""
        if config is None:
            return None
        dump_method = getattr(config, "model_dump", None)
        payload: dict[str, Any] | None = None
        if callable(dump_method):
            try:
                dumped = dump_method(mode="python")
                if isinstance(dumped, dict):
                    payload = dumped
            except TypeError:
                try:
                    dumped = dump_method()
                    if isinstance(dumped, dict):
                        payload = dumped
                except Exception:
                    payload = None
            except Exception:
                payload = None
        if payload is None:
            dict_method = getattr(config, "dict", None)
            if callable(dict_method):
                try:
                    dumped = dict_method()
                    if isinstance(dumped, dict):
                        payload = dumped
                except Exception:
                    payload = None
        if payload is None:
            return None
        try:
            migrated = LifeEngineConfig.model_validate(payload)
        except Exception:
            migrated = LifeEngineConfig()
        try:
            setattr(self.plugin, "config", migrated)
        except Exception:
            pass
        return migrated

    def _is_enabled(self) -> bool:
        """判断插件当前是否启用。"""
        cfg = self._cfg()
        return bool(cfg.settings.enabled)

    def _history_limit(self) -> int:
        """返回滚动事件流保留上限。"""
        cfg = self._cfg()
        return max(1, int(cfg.settings.context_history_max_events))

    def _memory_index_options(self) -> dict[str, Any]:
        """读取新旧配置兼容的保守索引参数。"""
        section = getattr(self._cfg(), "memory_index", None)

        def _integer(name: str, default: int, minimum: int) -> int:
            try:
                return max(minimum, int(getattr(section, name, default)))
            except (TypeError, ValueError):
                return default

        return {
            "enabled": bool(getattr(section, "enabled", True)),
            "interval_seconds": _integer("interval_seconds", 60, 30),
            "batch_size": min(50, _integer("batch_size", 4, 1)),
            "run_on_startup": bool(getattr(section, "run_on_startup", True)),
            "retry_failed": bool(getattr(section, "retry_failed", False)),
            "reclaim_after_seconds": _integer("reclaim_after_seconds", 600, 60),
        }

    def _sleep_window_config(self) -> tuple[dtime | None, dtime | None]:
        """返回配置的睡眠窗口（sleep, wake）。"""
        cfg = self._cfg()
        return _parse_hhmm(cfg.settings.sleep_time), _parse_hhmm(cfg.settings.wake_time)

    def _sleep_window_status(self) -> tuple[bool, str]:
        """返回睡眠窗口配置是否有效及说明。"""
        cfg = self._cfg()
        sleep_raw = (cfg.settings.sleep_time or "").strip()
        wake_raw = (cfg.settings.wake_time or "").strip()
        sleep_at, wake_at = self._sleep_window_config()
        if not sleep_raw and not wake_raw:
            return False, "disabled"
        if sleep_at is None or wake_at is None:
            return False, "invalid-format"
        if sleep_at == wake_at:
            return False, "invalid-equal"
        return True, f"{sleep_at.strftime('%H:%M')}~{wake_at.strftime('%H:%M')}"

    def _in_sleep_window_now(self) -> tuple[bool, str]:
        """判断当前是否处于睡眠窗口。"""
        sleep_at, wake_at = self._sleep_window_config()
        if sleep_at is None or wake_at is None:
            return False, "sleep-window-disabled"

        now = datetime.now().astimezone().time()
        now_hm = dtime(hour=now.hour, minute=now.minute, second=0, microsecond=0)

        if sleep_at == wake_at:
            return False, "sleep-window-invalid-equal"

        if sleep_at < wake_at:
            in_sleep = sleep_at <= now_hm < wake_at
        else:
            in_sleep = (now_hm >= sleep_at) or (now_hm < wake_at)

        return in_sleep, f"{sleep_at.strftime('%H:%M')}~{wake_at.strftime('%H:%M')}"

    def _next_sequence(self) -> int:
        """获取下一个事件序列号。"""
        self._state.event_sequence += 1
        return self._state.event_sequence

    def _minutes_since_external_message(self) -> int | None:
        """计算距离上一条外部消息过去了多少分钟。"""
        return minutes_since_time(self._state.last_external_message_at)

    def _minutes_since_tell_dfc(self) -> int | None:
        """计算距离上一次传话给 DFC 过去了多少分钟。"""
        return minutes_since_time(self._state.last_tell_dfc_at)

    def _minutes_since_outer_sync(self) -> int | None:
        """计算距离上一次同步给对外运行模式过去了多少分钟。"""
        return self._minutes_since_tell_dfc()

    def _self_pause_status(self) -> tuple[bool, int | None, str | None, str | None]:
        """返回主动休息锁状态。"""
        return self_pause_status(self._state)

    def get_self_pause_status(self) -> dict[str, Any]:
        """返回主动休息锁状态，供工具、监控面板或调试命令复用。"""
        return build_self_pause_status(self._state, self._self_pause_status())

    def _clear_self_pause_state(self) -> bool:
        """清除主动休息锁。调用方负责持锁和保存。"""
        return clear_self_pause_state(self._state)

    async def clear_self_pause(self, *, source: str = "manual") -> bool:
        """清除主动休息锁。"""
        async with self._get_lock():
            changed = self._clear_self_pause_state()
        if changed:
            self._self_pause_skip_logged = False
            await self._save_runtime_context()
            logger.info(f"life_engine 主动休息锁已解除: source={source}")
        return changed

    async def request_self_pause(
        self,
        *,
        duration_minutes: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """设置主动休息锁，让 LLM 心跳暂停一段时间。"""
        async with self._get_lock():
            payload = apply_self_pause(
                self._state,
                duration_minutes=duration_minutes,
                reason=reason,
            )

        await self._save_runtime_context()
        # 进入休息只报一次；心跳循环里用此标记静默跳过，不再每 tick 刷 remaining。
        self._self_pause_skip_logged = True
        logger.info(
            "life_engine 进入主动休息: "
            f"duration={payload['duration_minutes']}min requested={payload['requested_minutes']}min "
            f"until={payload['paused_until']} reason={payload['reason'] or '-'}"
        )

        return payload

    def record_tell_dfc(self) -> None:
        """记录一次传话给 DFC 的时间。"""
        self._state.last_tell_dfc_at = _now_iso()
        self._state.tell_dfc_count += 1

    def record_outer_sync(self) -> None:
        """记录一次同步给对外运行模式的时间。"""
        self.record_tell_dfc()

    def _workspace_dir(self) -> Path:
        """返回 life workspace 目录。"""
        workspace = Path(self._cfg().settings.workspace_path).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _ensure_workspace_templates(self) -> None:
        """补齐可由运行态长期维护的工作空间模板文件。"""

        workspace = self._workspace_dir()
        user_file = workspace / "USER.md"
        if user_file.exists():
            return
        try:
            template = _USER_TEMPLATE_PATH.read_text(encoding="utf-8")
            user_file.write_text(template, encoding="utf-8")
            logger.info(f"已创建 USER.md 使用说明模板: {user_file}")
        except Exception as e:
            logger.warning(f"创建 USER.md 使用说明模板失败: {e}")

    def snapshot(self) -> dict[str, Any]:
        """返回当前状态快照。"""
        data = asdict(self._state)
        in_sleep_window, sleep_window_desc = self._in_sleep_window_now()
        data["heartbeat_interval_seconds"] = int(self._cfg().settings.heartbeat_interval_seconds)
        data["external_silence_minutes"] = self._minutes_since_external_message()
        self_paused, self_pause_remaining, self_pause_until, self_pause_reason = self._self_pause_status()
        data["llm_heartbeat_paused_by_self"] = self_paused
        data["self_pause_remaining_minutes"] = self_pause_remaining
        data["self_pause_until"] = self_pause_until
        data["self_pause_reason"] = self_pause_reason
        data["self_pause_duration_minutes"] = self._state.self_pause_duration_minutes
        data["model_task_name"] = self._cfg().model.task_name
        data["pending_event_count"] = len(self._pending_events)
        data["history_event_count"] = len(self._event_history)
        data["context_history_max_events"] = self._history_limit()
        data["workspace_path"] = self._cfg().settings.workspace_path
        data["sleep_time"] = self._cfg().settings.sleep_time
        data["wake_time"] = self._cfg().settings.wake_time
        data["in_sleep_window"] = in_sleep_window
        data["sleep_window"] = sleep_window_desc
        data["log_file_path"] = str(get_life_log_file())
        return data

    @staticmethod
    def _message_time_display(message: Message) -> tuple[str, str]:
        """返回消息时间的 ISO 与简洁显示。"""
        raw_time = getattr(message, "time", None)
        try:
            if raw_time is None:
                raise ValueError("missing time")
            iso_time = datetime.fromtimestamp(float(raw_time), tz=timezone.utc).astimezone().isoformat()
        except Exception:
            iso_time = _now_iso()
        return iso_time, _format_time_display(iso_time)

    @staticmethod
    def _format_message_text(message: Message, *, max_length: int = 240) -> str:
        """格式化消息正文。"""
        raw_text = getattr(message, "processed_plain_text", None)
        if raw_text is None:
            raw_text = getattr(message, "content", "")
        return _shorten_text(str(raw_text or "").strip() or "（空消息）", max_length=max_length)

    @staticmethod
    def _message_sender_label(message: Message) -> str:
        """格式化消息发送者标签。"""
        return str(
            getattr(message, "sender_cardname", None)
            or getattr(message, "sender_name", None)
            or getattr(message, "sender_id", None)
            or "未知发送者"
        )

    @staticmethod
    def _serialize_life_event(event: LifeEngineEvent) -> dict[str, Any]:
        """将 life 事件转换为可视化数据。"""
        return {
            "scope": "life",
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp,
            "time_display": _format_time_display(event.timestamp),
            "sequence": event.sequence,
            "source": event.source,
            "source_detail": event.source_detail,
            "content": _shorten_text(event.content or "", max_length=240),
            "content_full": event.content or "",
            "content_type": event.content_type,
            "sender": event.sender,
            "sender_id": event.sender_id,
            "sender_platform_account_key": event.sender_platform_account_key,
            "canonical_person_key": event.canonical_person_key,
            "identity_resolution_status": event.identity_resolution_status,
            "chat_type": event.chat_type,
            "stream_id": event.stream_id,
            "heartbeat_index": event.heartbeat_index,
            "tool_name": event.tool_name,
            "tool_args": event.tool_args or {},
            "tool_success": event.tool_success,
            "heartbeat_context_consumed": event.heartbeat_context_consumed,
            "source_instance_id": event.source_instance_id,
            "correlation_id": event.correlation_id,
            "content_ref": event.content_ref,
        }

    async def _publish_raw_events(self, events: list[LifeEngineEvent]) -> None:
        """Mirror legacy service events into the unified raw event log."""
        if not events:
            return
        registry = self.consciousness_registry
        if isinstance(registry, AsyncConsciousnessRegistry):
            await registry.refresh()
        for event in events:
            if event.source_instance_id or event.event_type != EventType.MESSAGE:
                continue
            stream_id = str(event.stream_id or "").strip()
            if not stream_id:
                continue
            owner = registry.get_for_stream(stream_id)
            if owner is not None:
                event.source_instance_id = owner.instance_id
                event.correlation_id = event.correlation_id or owner.session_id or None
        await self._get_event_bus().publish_legacy_events(events)
        if self._world_projection is not None:
            await self.catch_up_world_projection()

    async def _queue_pending_event(
        self,
        event: LifeEngineEvent,
        *,
        persist: bool = True,
    ) -> None:
        """Append an event to the compatibility pending queue and raw bus."""
        async with self._get_lock():
            self._pending_events.append(event)
            self._state.pending_event_count = len(self._pending_events)
        await self._publish_raw_events([event])
        if persist:
            await self._save_runtime_context()

    def _serialize_stream_message(
        self,
        message: Message,
        *,
        stream_name: str,
        source: str,
    ) -> dict[str, Any]:
        """将聊天流消息转换为可视化数据。"""
        iso_time, time_display = self._message_time_display(message)
        sender_role = str(getattr(message, "sender_role", "") or "").lower()
        direction = "sent" if sender_role == "bot" else "received"
        return {
            "scope": "chatter",
            "stream_id": str(getattr(message, "stream_id", "") or ""),
            "stream_name": stream_name,
            "platform": str(getattr(message, "platform", "") or ""),
            "chat_type": str(getattr(message, "chat_type", "") or ""),
            "message_id": str(getattr(message, "message_id", "") or ""),
            "time": iso_time,
            "time_display": time_display,
            "direction": direction,
            "sender_role": sender_role or None,
            "sender_name": self._message_sender_label(message),
            "content": self._format_message_text(message),
            "content_full": str(
                getattr(message, "processed_plain_text", None)
                or getattr(message, "content", "")
                or ""
            ),
            "reply_to": getattr(message, "reply_to", None),
            "source": source,
            "is_inner_monologue": bool(
                getattr(message, "is_inner_monologue", False)
                or (getattr(message, "extra", {}) or {}).get("is_inner_monologue", False)
            ),
            "is_proactive_followup_trigger": bool(
                getattr(message, "is_proactive_followup_trigger", False)
                or (getattr(message, "extra", {}) or {}).get("is_proactive_followup_trigger", False)
            ),
        }

    async def get_message_observability_snapshot(
        self,
        *,
        event_limit: int = 24,
        stream_limit: int = 12,
        message_limit: int = 8,
    ) -> dict[str, Any]:
        """返回 life 与 chatter 的联合消息观测快照。"""
        async with self._get_lock():
            pending_events = list(self._pending_events)
            event_history = list(self._event_history)
            state_snapshot = asdict(self._state)

        life_events = [
            self._serialize_life_event(event)
            for event in (event_history[-max(1, event_limit) :] if event_limit > 0 else event_history)
        ]
        pending_life_events = [
            self._serialize_life_event(event)
            for event in (pending_events[-max(1, min(event_limit, len(pending_events))) :] if pending_events else [])
        ]

        life_latest_event = life_events[-1] if life_events else (pending_life_events[-1] if pending_life_events else None)

        try:
            from src.core.managers import get_stream_manager

            stream_manager = get_stream_manager()
            stream_items = list(getattr(stream_manager, "_streams", {}).values())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取聊天流快照失败: {exc}")
            stream_items = []

        stream_snapshots: list[dict[str, Any]] = []
        for stream in stream_items:
            stream_id = str(getattr(stream, "stream_id", "") or "")
            if not stream_id:
                continue
            context = getattr(stream, "context", None)
            if context is None:
                continue

            history_messages = list(getattr(context, "history_messages", []) or [])
            unread_messages = list(getattr(context, "unread_messages", []) or [])
            current_message = getattr(context, "current_message", None)

            candidate_messages = history_messages[-max(1, message_limit) :]
            recent_messages = [
                self._serialize_stream_message(
                    msg,
                    stream_name=str(getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"),
                    source="history",
                )
                for msg in candidate_messages
            ]

            latest_message = None
            if unread_messages:
                latest_message = self._serialize_stream_message(
                    unread_messages[-1],
                    stream_name=str(getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"),
                    source="unread",
                )
            elif history_messages:
                latest_message = self._serialize_stream_message(
                    history_messages[-1],
                    stream_name=str(getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"),
                    source="history",
                )
            elif current_message is not None:
                latest_message = self._serialize_stream_message(
                    current_message,
                    stream_name=str(getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"),
                    source="current",
                )

            last_message_time = getattr(context, "last_message_time", None)
            last_active_time = getattr(stream, "last_active_time", None)
            stream_snapshots.append(
                {
                    "stream_id": stream_id,
                    "stream_name": str(getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"),
                    "platform": str(getattr(stream, "platform", "") or ""),
                    "chat_type": str(getattr(stream, "chat_type", "") or ""),
                    "bot_nickname": str(getattr(stream, "bot_nickname", "") or ""),
                    "is_active": bool(getattr(stream, "is_active", True)),
                    "is_chatter_processing": bool(getattr(context, "is_chatter_processing", False)),
                    "last_active_time": last_active_time,
                    "last_message_time": last_message_time,
                    "unread_count": len(unread_messages),
                    "history_count": len(history_messages),
                    "latest_message": latest_message,
                    "recent_messages": recent_messages,
                    "sort_ts": float(last_active_time or last_message_time or 0.0),
                }
            )

        stream_snapshots.sort(key=lambda item: item["sort_ts"], reverse=True)
        stream_snapshots = stream_snapshots[: max(1, stream_limit)]

        return {
            "generated_at": _now_iso(),
            "life": {
                "state": state_snapshot,
                "pending_events": pending_life_events,
                "recent_events": life_events,
                "latest_event": life_latest_event,
            },
            "streams": stream_snapshots,
            "summary": {
                "active_stream_count": len(stream_snapshots),
                "pending_life_events": len(pending_events),
                "recent_life_events": len(life_events),
                "heartbeat_count": int(state_snapshot.get("heartbeat_count", 0) or 0),
                "last_model_reply": state_snapshot.get("last_model_reply"),
            },
        }

    async def refresh_storage_health(self) -> dict[str, Any]:
        """Refresh read-only async Port diagnostics into the sync health cache."""

        if not self._selectable_storage_enabled:
            return dict(self._storage_health_cache)
        if (
            self._storage_runtime is None
            or self._life_event_store is None
            or self._presence_world_stores is None
            or self._subject_document_store is None
        ):
            return dict(self._storage_health_cache)
        runtime_result, event_result, presence_result, world_result, subject_result = (
            await asyncio.gather(
                self._storage_runtime.health(),
                self._life_event_store.health_snapshot(),
                self._presence_world_stores.presence.health_snapshot(),
                self._presence_world_stores.world.health_snapshot(),
                self._subject_document_store.health_snapshot(),
                return_exceptions=True,
            )
        )

        def normalized(name: str, value: Any) -> dict[str, Any]:
            if isinstance(value, BaseException):
                return {
                    "component": name,
                    "status": "failed",
                    "reason": type(value).__name__,
                }
            return dict(value)

        components = {
            "runtime": normalized("storage_runtime", runtime_result),
            "life_event": normalized("life_event_store", event_result),
            "presence": normalized("consciousness_presence", presence_result),
            "world": normalized("world_projection", world_result),
            "subject_document": normalized("subject_document", subject_result),
        }
        statuses = {
            str(item.get("status") or "healthy") for item in components.values()
        }
        if "failed" in statuses:
            status = "failed"
        elif "degraded" in statuses:
            status = "degraded"
        else:
            status = "healthy"
        self._storage_health_cache = {
            "status": status,
            "backend": self._storage_factory_settings.authoritative_backend.value,
            "components": components,
        }
        return dict(self._storage_health_cache)

    def health(self) -> dict[str, Any]:
        """返回一个轻量健康信息。"""
        snapshot = self.snapshot()
        snapshot["storage_runtime"] = dict(self._storage_health_cache)
        if self._selectable_storage_enabled:
            components = self._storage_health_cache.get("components") or {}
            snapshot["raw_event_ledger"] = dict(
                components.get("life_event") or {}
            )
            snapshot["consciousness_presence"] = dict(
                components.get("presence") or {}
            )
            snapshot["world_projection"] = dict(
                components.get("world") or {}
            )
            snapshot["subject_document"] = dict(
                components.get("subject_document") or {}
            )
        else:
            if self._event_bus is not None:
                snapshot["raw_event_ledger"] = (
                    self._event_bus.store.health_snapshot()
                )
            snapshot["consciousness_presence"] = (
                self.consciousness_registry.health_snapshot()
            )
            if self._world_projection is not None:
                snapshot["world_projection"] = (
                    self._world_projection.health_snapshot()
                )
        if self._router_context_projection is not None:
            snapshot["router_context_projection"] = (
                self._router_context_projection.health_snapshot()
            )
        else:
            snapshot["router_context_projection"] = {
                "component": "router_context_projection",
                "owner": "life_engine.service",
                "status": "disabled",
                "running": False,
                "backlog": 0,
                "fresh": False,
                "degraded_reason": "",
            }
        subject_profiles = [
            projection.health_snapshot()
            for projection in self._subject_context_projections.values()
        ]
        subject_statuses = {
            str(item.get("status") or "") for item in subject_profiles
        }
        if "degraded" in subject_statuses:
            subject_status = "degraded"
        elif "ready" in subject_statuses:
            subject_status = "ready"
        else:
            subject_status = "idle"
        snapshot["subject_context_projection"] = {
            "component": "subject_context_projection",
            "owner": "life_engine.service",
            "status": subject_status,
            "mode": "on_demand",
            "profile_count": len(subject_profiles),
            "profiles": subject_profiles,
        }
        if self._shared_sync_bridge is not None:
            try:
                snapshot["shared_sync"] = self._shared_sync_bridge.health_snapshot()
            except Exception as exc:  # noqa: BLE001
                snapshot["shared_sync"] = {
                    "component": "offline_sync",
                    "status": "degraded",
                    "running": False,
                    "degraded_reason": f"health unavailable: {type(exc).__name__}: {exc}",
                }
        else:
            sync_enabled = bool(getattr(getattr(self._cfg(), "shared_sync", None), "enabled", False))
            snapshot["shared_sync"] = {
                "component": "offline_sync",
                "status": "degraded" if self._shared_sync_error else "disabled",
                "running": False,
                "outbox_backlog": 0,
                "degraded_reason": self._shared_sync_error,
                "enabled": sync_enabled,
            }
        if self._memory_archive_sync_bridge is not None:
            try:
                snapshot["memory_archive_sync"] = (
                    self._memory_archive_sync_bridge.health_snapshot()
                )
            except Exception as exc:  # noqa: BLE001
                snapshot["memory_archive_sync"] = {
                    "component": "unified_memory_archive",
                    "status": "degraded",
                    "running": False,
                    "degraded_reason": (
                        f"health unavailable: {type(exc).__name__}: {exc}"
                    ),
                }
        else:
            archive_enabled = bool(
                getattr(
                    getattr(self._cfg(), "memory_archive_sync", None),
                    "enabled",
                    False,
                )
            )
            snapshot["memory_archive_sync"] = {
                "component": "unified_memory_archive",
                "status": (
                    "degraded" if self._memory_archive_sync_error else "disabled"
                ),
                "running": False,
                "known_records": 0,
                "degraded_reason": self._memory_archive_sync_error,
                "enabled": archive_enabled,
            }
        return snapshot

    async def get_router_context_projection_prompt(self) -> str:
        """Return a projection only when it matches current authority files."""

        projection = self._router_context_projection
        if projection is None:
            return ""
        return await projection.ensure_current()

    def notify_router_context_source_changed(self, path: str | Path) -> bool:
        """Notify all subject-derived projections after an official workspace write."""

        projection = self._router_context_projection
        router_notified = bool(
            projection is not None and projection.notify_source_changed(path)
        )
        subject_notified = False
        for subject_projection in self._subject_context_projections.values():
            subject_notified = (
                subject_projection.notify_source_changed(path) or subject_notified
            )
        return router_notified or subject_notified

    def notify_subject_context_source_changed(self, path: str | Path) -> bool:
        """Explicit alias for consumers that do not depend on router terminology."""

        return self.notify_router_context_source_changed(path)

    async def get_subject_context_projection_snapshot(
        self,
        *,
        projection_kind: str,
        max_bytes: int,
        source_digest: str = "",
        projection_version: int | None = None,
    ) -> dict[str, Any]:
        """Return a current or pinned immutable subject projection snapshot.

        An empty ``source_digest`` first verifies all three current authority files.
        A concrete digest loads that historical content-addressed version without
        silently switching a resumed consciousness episode to a newer identity.
        """

        revision = str(source_digest or "").strip()
        if not revision and projection_version is not None:
            raise ValueError("projection_version requires a historical source_digest")
        projection_key = (
            str(projection_kind or "").strip().lower(),
            int(max_bytes),
        )
        projection = self._subject_context_projections.get(projection_key)
        if projection is None:
            projection_ref: SubjectContextProjection | None = None

            async def author(
                digest: str,
                sources: tuple[SubjectContextSource, ...],
            ) -> SubjectContextDraft:
                if projection_ref is None:
                    raise RuntimeError("subject projection owner was not initialized")
                return await self._author_subject_context_projection(
                    digest,
                    sources,
                    projection_kind=projection_ref.projection_profile,
                    max_chars=projection_ref.max_chars,
                    max_bytes=projection_ref.max_bytes,
                )

            projection_ref = SubjectContextProjection(
                str(self._workspace_dir()),
                projection_profile=projection_key[0],
                max_bytes=projection_key[1],
                author=author,
            )
            projection = projection_ref
            projection_key = (
                projection.projection_profile,
                projection.max_bytes,
            )
            self._subject_context_projections[projection_key] = projection
        if revision:
            snapshot = await projection.get_snapshot(
                revision,
                projection_version=projection_version,
            )
        else:
            snapshot = await projection.ensure_current_snapshot()
        if snapshot is None:
            health = projection.health_snapshot()
            reason = str(health.get("degraded_reason") or "snapshot unavailable")
            raise RuntimeError(
                "subject context projection unavailable: "
                f"kind={projection.projection_profile}, max_bytes={projection.max_bytes}, "
                f"source_digest={revision or 'current'}, reason={reason}"
            )
        return dict(snapshot)

    async def _author_router_context_projection(
        self,
        source_digest: str,
        sources: tuple[RouterContextSource, ...],
    ) -> RouterContextDraft:
        """Use cloud models in configured order and reject reasoning-only output."""

        chatter_cfg = self._cfg().chatter
        task_name = str(
            getattr(
                chatter_cfg,
                "router_context_projection_task_name",
                "router_context_projection",
            )
            or "router_context_projection"
        ).strip()
        max_chars = int(
            getattr(chatter_cfg, "router_context_projection_max_chars", 6000)
            or 6000
        )
        timeout_seconds = float(
            getattr(chatter_cfg, "router_context_projection_timeout_seconds", 90.0)
            or 90.0
        )
        system_prompt, user_prompt = build_router_context_projection_prompt(
            source_digest,
            sources,
            max_chars=max_chars,
        )
        try:
            model_set = get_model_set_by_task(task_name)
        except (KeyError, ValueError):
            fallback_task = "utility"
            logger.warning(
                f"Router 上下文投影任务 {task_name!r} 未配置，"
                f"回退到云端任务 {fallback_task!r}"
            )
            task_name = fallback_task
            model_set = get_model_set_by_task(task_name)
        if not model_set:
            raise RuntimeError(f"model task '{task_name}' has no models")

        errors: list[str] = []
        for configured_model in model_set:
            if not isinstance(configured_model, dict):
                continue
            model_identifier = str(
                configured_model.get("model_identifier") or "unknown"
            )
            request = create_llm_request(
                model_set=[dict(configured_model)],
                request_name="life_router_context_projection",
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
            request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))
            try:
                response = await asyncio.wait_for(
                    request.send(stream=False),
                    timeout=timeout_seconds,
                )
                awaited_text = await asyncio.wait_for(
                    response,
                    timeout=timeout_seconds,
                )
                content = str(response.message or awaited_text or "").strip()
                if content and len(content) <= max_chars:
                    return RouterContextDraft(
                        text=content,
                        generator=f"task:{task_name}/model:{model_identifier}",
                    )
                if not content:
                    errors.append(f"{model_identifier}: empty final content")
                else:
                    errors.append(
                        f"{model_identifier}: final content exceeded budget "
                        f"({len(content)} > {max_chars})"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{model_identifier}: {type(exc).__name__}: {exc}")
                logger.warning(
                    "Router 上下文投影云端模型失败，尝试下一模型: "
                    f"model={model_identifier} error={type(exc).__name__}: {exc}"
                )

        detail = "; ".join(errors[-3:]) or "no valid model entry"
        raise RuntimeError(
            f"router context projection exhausted cloud models: {detail}"
        )

    async def _author_subject_context_projection(
        self,
        source_digest: str,
        sources: tuple[SubjectContextSource, ...],
        *,
        projection_kind: str,
        max_chars: int,
        max_bytes: int,
    ) -> SubjectContextDraft:
        """Author one structured projection without creating a second persona."""

        chatter_cfg = self._cfg().chatter
        task_name = str(
            getattr(
                chatter_cfg,
                "subject_context_projection_task_name",
                "router_context_projection",
            )
            or "router_context_projection"
        ).strip()
        timeout_seconds = float(
            getattr(
                chatter_cfg,
                "subject_context_projection_timeout_seconds",
                90.0,
            )
            or 90.0
        )
        system_prompt, user_prompt = build_subject_context_projection_prompt(
            source_digest,
            sources,
            projection_profile=projection_kind,
            max_chars=max_chars,
            max_bytes=max_bytes,
        )
        try:
            model_set = get_model_set_by_task(task_name)
        except (KeyError, ValueError):
            fallback_task = "utility"
            logger.warning(
                f"Subject context projection task {task_name!r} is not configured; "
                f"falling back to cloud task {fallback_task!r}"
            )
            task_name = fallback_task
            model_set = get_model_set_by_task(task_name)
        if not model_set:
            raise RuntimeError(f"model task '{task_name}' has no models")

        errors: list[str] = []
        content_byte_budget = max(1, max_bytes - 2048)
        for configured_model in model_set:
            if not isinstance(configured_model, dict):
                continue
            model_identifier = str(
                configured_model.get("model_identifier") or "unknown"
            )
            request = create_llm_request(
                model_set=[dict(configured_model)],
                request_name="life_subject_context_projection",
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
            request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))
            try:
                response = await asyncio.wait_for(
                    request.send(stream=False),
                    timeout=timeout_seconds,
                )
                awaited_text = await asyncio.wait_for(
                    response,
                    timeout=timeout_seconds,
                )
                content = str(response.message or awaited_text or "").strip()
                if not content:
                    errors.append(f"{model_identifier}: empty final content")
                    continue
                if len(content) > max_chars:
                    errors.append(
                        f"{model_identifier}: final content exceeded char budget "
                        f"({len(content)} > {max_chars})"
                    )
                    continue
                content_bytes = len(content.encode("utf-8"))
                if content_bytes > content_byte_budget:
                    errors.append(
                        f"{model_identifier}: final content exceeded byte budget "
                        f"({content_bytes} > {content_byte_budget})"
                    )
                    continue
                try:
                    sections = validate_subject_projection_text(content)
                except RuntimeError as exc:
                    errors.append(f"{model_identifier}: invalid coverage: {exc}")
                    continue
                per_source_max_bytes = max(256, (max_bytes - 2048) // 3)
                oversized_source = next(
                    (
                        (path, len(section.encode("utf-8")))
                        for path, section in sections.items()
                        if len(section.encode("utf-8")) > per_source_max_bytes
                    ),
                    None,
                )
                if oversized_source is not None:
                    path, delivered_bytes = oversized_source
                    errors.append(
                        f"{model_identifier}: source block exceeded byte budget "
                        f"({path}: {delivered_bytes} > {per_source_max_bytes})"
                    )
                    continue
                return SubjectContextDraft(
                    text=content,
                    generator=f"task:{task_name}/model:{model_identifier}",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{model_identifier}: {type(exc).__name__}: {exc}")
                logger.warning(
                    "Subject context projection model failed; trying next model: "
                    f"model={model_identifier} error={type(exc).__name__}: {exc}"
                )

        detail = "; ".join(errors[-3:]) or "no valid model entry"
        raise RuntimeError(
            f"subject context projection exhausted cloud models: {detail}"
        )

    async def get_state_digest_for_dfc(self) -> str:
        """生成给 DFC 的状态摘要。"""
        if self._dfc_integration is None:
            self._dfc_integration = DFCIntegration(self)
        return await self._dfc_integration.get_state_digest()

    async def get_state_digest_for_outer_mode(self) -> str:
        """生成给对外运行模式的状态摘要。"""
        return await self.get_state_digest_for_dfc()

    async def query_actor_context(self, query: str) -> str:
        """供 DFC 同步查询当前状态、TODO 与最近日记。"""
        del query
        if self._dfc_integration is None:
            self._dfc_integration = DFCIntegration(self)
        return await self._dfc_integration.query_actor_context()

    async def query_outer_context(self, query: str) -> str:
        """供对外运行模式同步查询当前状态、TODO 与最近日记。"""
        return await self.query_actor_context(query)

    async def search_actor_memory(self, query: str, top_k: int = 5) -> str:
        """供 DFC 深度检索 life memory。"""
        query_text = str(query or "").strip()
        if not query_text:
            return ""

        memory_service = self._memory_service
        if memory_service is None:
            return "记忆系统暂不可用"

        # 需要 SearchResult 列表：下面既要交给 build_memory_bundles，
        # 也要在降级路径里直接读 file_path/snippet。
        results = await memory_service.search_memory(
            query_text,
            top_k=max(1, int(top_k)),
            return_bundles=False,
        )
        if not results:
            logger.info(
                f"[search_actor_memory] 记忆检索无结果:\n"
                f"  query: {query_text}\n  top_k: {top_k}"
            )
            return ""

        try:
            bundles = await memory_service.build_memory_bundles(
                query=query_text,
                results=results,
                top_k=max(1, int(top_k)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[search_actor_memory] 构建可追溯记忆包失败，将使用普通摘要: {exc}")
            bundles = []

        if bundles:
            workspace = self._workspace_dir()
            bundle_lines: list[str] = []
            for bundle in bundles[: max(1, int(top_k))]:
                file_meta = get_file_metadata(workspace / bundle.primary_path)
                meta_str = f"{file_meta['ext']} | {file_meta['time_ago']} | {file_meta['size']}"

                evidence_lines: list[str] = []
                for item in bundle.evidence[:4]:
                    label = "当前文件" if item.file_path == bundle.primary_path else "历史证据"
                    if item.relation:
                        label += f"/{item.relation}"
                    exists_note = "" if item.exists else "（当前路径不存在，仅作历史轨迹）"
                    snippet = _shorten_text(" ".join((item.snippet or "").split()), max_length=160)
                    evidence_lines.append(
                        f"  - {label}: {item.title or Path(item.file_path).name} "
                        f"[{item.file_path}]{exists_note}\n"
                        f"    摘要：{snippet or '无摘要'}"
                    )

                trace_lines: list[str] = []
                for trace in bundle.history_trace[:4]:
                    direction = "后来" if trace.direction == "later" else "早期"
                    reason = _shorten_text(" ".join((trace.reason or "").split()), max_length=120)
                    trace_lines.append(
                        f"  - {direction}/{trace.relation}: [{trace.file_path}]"
                        + (f" - {reason}" if reason else "")
                    )

                correction_lines = [
                    f"  - {item.source}: {_shorten_text(' '.join(item.message.split()), max_length=180)}"
                    for item in bundle.corrections[:3]
                ]

                line_parts = [
                    f"- 主要文件：{bundle.primary_path} ({meta_str})",
                    f"  当前理解：{_shorten_text(bundle.current_understanding, max_length=260)}",
                ]
                if evidence_lines:
                    line_parts.append("  证据：\n" + "\n".join(evidence_lines))
                if trace_lines:
                    line_parts.append("  演化轨迹：\n" + "\n".join(trace_lines))
                if correction_lines:
                    line_parts.append("  显式修正：\n" + "\n".join(correction_lines))
                if bundle.uncertainty:
                    line_parts.append(
                        f"  注意：{_shorten_text(bundle.uncertainty, max_length=220)}"
                    )
                bundle_lines.append("\n".join(line_parts))

            footer = "\n\n提示：以上是可追溯记忆包；旧记忆作为历史证据保留，当前理解优先参考后续整理和显式修正。如需查看完整内容，可使用 fetch_life_memory 工具读取文件。"
            final_result = "【可追溯记忆包】\n" + "\n\n".join(bundle_lines) + footer

            logger.info(
                f"[search_actor_memory] 可追溯记忆检索完成:\n"
                f"  query: {query_text}\n  top_k: {top_k}\n"
                f"  bundles: {len(bundles)}"
            )
            return final_result

        workspace = self._workspace_dir()
        direct_lines: list[str] = []
        associated_lines: list[str] = []

        for result in results:
            title = result.title or Path(result.file_path).name or result.file_path
            snippet = _shorten_text(" ".join((result.snippet or "").split()), max_length=250)
            file_meta = get_file_metadata(workspace / result.file_path)
            meta_str = f"{file_meta['ext']} | {file_meta['time_ago']} | {file_meta['size']}"

            line = (
                f"- {title} [{result.file_path}] "
                f"(相关度 {result.relevance:.2f} | {meta_str})\n"
                f"  摘要：{snippet or '无摘要'}"
            )

            if result.source == "associated":
                reason = _shorten_text(
                    " ".join((result.association_reason or "").split()),
                    max_length=150,
                )
                path_str = " → ".join(result.association_path[-3:]) if result.association_path else ""
                if reason or path_str:
                    line += f"\n  联想：{reason or path_str}"
                associated_lines.append(line)
            else:
                direct_lines.append(line)

            if len(direct_lines) >= top_k and len(associated_lines) >= top_k:
                break

        parts: list[str] = []
        if direct_lines:
            parts.append(
                f"【直接命中的记忆】({len(direct_lines[:top_k])}条)\n" +
                "\n\n".join(direct_lines[:top_k])
            )
        if associated_lines:
            parts.append(
                f"【联想扩散结果】({len(associated_lines[:top_k])}条)\n" +
                "\n\n".join(associated_lines[:top_k])
            )

        footer = "\n\n💡 提示：以上仅为摘要。如需查看完整内容，可使用 fetch_life_memory 工具读取文件。"
        final_result = "\n\n".join(parts) + footer

        logger.info(
            f"[search_actor_memory] 记忆检索完成:\n"
            f"  query: {query_text}\n  top_k: {top_k}\n"
            f"  直接命中: {len(direct_lines)} 条\n  联想结果: {len(associated_lines)} 条"
        )

        return final_result

    async def record_message(
        self,
        message: Message,
        direction: str = "received",
    ) -> None:
        """记录聊天消息。"""
        if not self._is_enabled():
            return

        if direction not in {"received", "sent"}:
            direction = "received"

        event = self._event_builder.build_message_event(message, direction=direction)
        unlocked_self_pause = False
        async with self._get_lock():
            self._pending_events.append(event)
            self._state.pending_event_count = len(self._pending_events)
            if direction == "received":
                self._state.last_external_message_at = event.timestamp
                unlocked_self_pause = self._clear_self_pause_state()
                
                # 外部消息解锁休息时，重置连续休息计数
                if unlocked_self_pause:
                    self._state.consecutive_rest_count = 0

                # 收到外界新消息时，重置并清除对应流的延迟续话状态
                stream_id = getattr(message, "stream_id", "")
                if stream_id and stream_id in self._followup_states:
                    state = self._followup_states[stream_id]
                    state.followup_chain_count = 0
                    state.followup_cooldown_until = None
                    if state.scheduler_task_name and self._scheduler:
                        try:
                            await self._scheduler.cancel_schedule(state.scheduler_task_name)
                        except Exception:
                            pass
                    state.pending_followup = None
                    state.is_waiting = False
                    state.active_check_kind = None
        await self._publish_raw_events([event])
        await self._save_runtime_context()
        if direction == "received":
            self._schedule_curiosity_review(message, event)
        if unlocked_self_pause:
            logger.info(
                "life_engine 收到外界消息，主动休息锁已解除: "
                f"stream_id={event.stream_id or ''} sender={event.sender or 'unknown'}"
            )

        log_message_received(
            received_at=event.timestamp,
            platform=event.source,
            chat_type=event.chat_type or "unknown",
            source_label=event.source_detail,
            source_detail=event.source_detail,
            stream_id=event.stream_id or "",
            sender_display=event.sender or "unknown",
            sender_id=event.sender_id or "",
            message_id=event.event_id,
            reply_to=None,
            message_type=event.content_type,
            content=event.content,
            direction=direction,
            pending_message_count=self._state.pending_event_count,
        )

    def _get_curiosity_engine(self) -> CuriosityEngine:
        cfg = self._cfg()
        curiosity_cfg = getattr(cfg, "curiosity", None)
        task_name = (
            str(getattr(curiosity_cfg, "task_name", "") or "").strip()
            or str(getattr(cfg.model, "task_name", "") or "").strip()
            or "core"
        )
        timeout = float(getattr(curiosity_cfg, "timeout_seconds", 30.0) or 30.0)
        workspace = str(getattr(cfg.settings, "workspace_path", "") or self._workspace_dir())
        if (
            self._curiosity_engine is None
            or self._curiosity_engine.workspace_path != workspace
            or self._curiosity_engine.model_task_name != task_name
        ):
            self._curiosity_engine = CuriosityEngine(
                workspace_path=workspace,
                model_task_name=task_name,
                timeout_seconds=timeout,
            )
        return self._curiosity_engine

    def _schedule_curiosity_review(self, message: Message, event: LifeEngineEvent) -> None:
        cfg = self._cfg()
        curiosity_cfg = getattr(cfg, "curiosity", None)
        if curiosity_cfg is not None and not bool(getattr(curiosity_cfg, "enabled", True)):
            return
        if self._curiosity_inflight:
            logger.debug("好奇异步判断仍在运行，跳过本次重复调度")
            return

        self._curiosity_inflight = True
        get_task_manager().create_task(
            self._run_curiosity_review(message, event),
            name=f"life_curiosity_{event.sequence}",
            daemon=True,
            timeout=float(getattr(curiosity_cfg, "timeout_seconds", 30.0) or 30.0) + 5.0,
        )

    async def _run_curiosity_review(self, message: Message, event: LifeEngineEvent) -> None:
        try:
            cfg = self._cfg()
            curiosity_cfg = getattr(cfg, "curiosity", None)
            max_history = (
                int(getattr(curiosity_cfg, "history_messages", 20) or 20)
                if curiosity_cfg is not None
                else 20
            )
            prefix_prompt = self._build_curiosity_prefix_prompt()
            history_text = await self._build_curiosity_history_text(message, max_messages=max_history)
            new_event_text = self._format_curiosity_event(event)
            meme_awareness = await self._build_meme_awareness_text()
            if meme_awareness:
                new_event_text = new_event_text + "\n\n" + meme_awareness
            signal = await self._get_curiosity_engine().review(
                prefix_prompt=prefix_prompt,
                history_text=history_text,
                new_event_text=new_event_text,
                source_event_id=event.event_id,
                source_stream_id=event.stream_id or "",
            )
            if signal.active:
                logger.info(f"好奇牵引已更新: {signal.anchor or signal.unknown}")
            else:
                logger.debug("好奇异步判断：暂无值得保留的刺点")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"好奇异步判断失败: {exc}")
        finally:
            self._curiosity_inflight = False

    async def _build_meme_awareness_text(self) -> str:
        """构建“未浏览表情包”的轻意识文本（供好奇心参考，是提醒不是任务）。"""
        try:
            from src.app.plugin_system.api.service_api import get_service

            svc = get_service("emoji:service:emoji_sender")
            if svc is None:
                return ""
            count = await svc.get_unreviewed_count()
            if count <= 0:
                return ""
            return (
                f"（顺带一提：最近收到了 {count} 张还没看过的表情包。"
                f"如果你有兴趣，可以用 nucleus_browse_memes 翻翻看，喜欢的就收藏——"
                f"不过这完全随你，不是任务。）"
            )
        except Exception:  # noqa: BLE001
            return ""

    def _build_curiosity_prefix_prompt(self) -> str:
        workspace = self._workspace_dir()
        soul_text = self._read_workspace_text(workspace, "SOUL.md")
        user_text = self._read_workspace_text(workspace, "USER.md")
        memory_text = ""
        try:
            memory_data = load_memory_prompt_data(workspace)
            memory_text = render_memory_prompt(memory_data, mode="chat") if memory_data.raw_text else ""
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"好奇层读取 MEMORY.md 失败: {exc}")

        return LifeChatterContextAssembler.build_prefix_prompt(
            soul_text=soul_text,
            user_text=user_text,
            memory_text=memory_text,
            tools_text="",
            live_guidance="",
            primary_tool_guide="",
        )

    @staticmethod
    def _read_workspace_text(workspace: str, filename: str) -> str:
        try:
            path = Path(workspace) / filename
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
        return ""

    async def _build_curiosity_history_text(self, message: Message, *, max_messages: int) -> str:
        if max_messages <= 0:
            return ""
        stream_id = str(getattr(message, "stream_id", "") or "").strip()
        chat_stream = None
        if stream_id:
            try:
                from src.core.managers import get_stream_manager

                chat_stream = get_stream_manager()._streams.get(stream_id)
            except Exception:
                chat_stream = None
        if chat_stream is not None:
            text = await build_global_chat_history_text_from_db(
                chat_stream,
                max_messages=max_messages,
                include_stream_label=True,
                exclude_message_ids={
                    str(getattr(message, "message_id", "") or "").strip()
                } - {""},
            )
            if text:
                return text

        async with self._get_lock():
            message_events = [
                item for item in [*self._event_history, *self._pending_events]
                if item.event_type == EventType.MESSAGE
            ][-max_messages:]
        return "\n".join(self._format_curiosity_event(item) for item in message_events)

    @staticmethod
    def _format_curiosity_event(event: LifeEngineEvent) -> str:
        sender = event.sender or "未知"
        stream = (event.stream_id or "")[:8]
        source = event.source_detail or event.source or "unknown"
        return (
            f"[{_format_time_display(event.timestamp)}] "
            f"{source} stream={stream} sender={sender}: "
            f"{_shorten_text(event.content, max_length=500)}"
        )

    def get_or_create_followup_state(self, stream_id: str) -> FollowupState:
        if stream_id not in self._followup_states:
            self._followup_states[stream_id] = FollowupState(stream_id=stream_id)
        return self._followup_states[stream_id]

    async def schedule_followup_for_stream(
        self,
        chat_stream: Any,
        *,
        delay_seconds: float,
        thought: str,
        topic: str,
        followup_type: str,
        source: str,
    ) -> tuple[bool, str]:
        """为某个聊天流登记一次延迟续话。"""
        stream_id = getattr(chat_stream, "stream_id", "")
        if not stream_id:
            return False, "缺少 stream_id"

        state = self.get_or_create_followup_state(stream_id)

        # 检查冷却
        if state.followup_cooldown_until and datetime.now() < state.followup_cooldown_until:
            return False, "当前仍处于延迟续话冷却期"

        # 延迟续话链已达上限
        max_chain = 2
        if state.followup_chain_count >= max_chain:
            return False, "延迟续话链已达上限"

        min_delay = 20.0
        max_delay = 90.0
        delay_seconds = max(float(delay_seconds or 0), min_delay)
        delay_seconds = min(delay_seconds, max_delay)

        next_check_time = datetime.now() + timedelta(seconds=delay_seconds)

        followup = PendingFollowup(
            topic=str(topic or "未命名话题").strip() or "未命名话题",
            thought=str(thought or "").strip(),
            followup_type=str(followup_type or "share_new_thought").strip() or "share_new_thought",
            delay_seconds=delay_seconds,
            scheduled_at=datetime.now(),
            check_at=next_check_time,
            source=source,
        )

        state.pending_followup = followup
        state.next_check_time = next_check_time
        state.is_waiting = True
        state.active_check_kind = "followup"

        task_name = f"life_followup_check_{stream_id}"
        state.scheduler_task_name = task_name

        if self._scheduler is None:
            self._scheduler = get_unified_scheduler()

        async def _timeout_callback() -> None:
            await self._on_followup_timeout(stream_id)

        try:
            await self._scheduler.create_schedule(
                callback=_timeout_callback,
                trigger_type=TriggerType.TIME,
                trigger_config={"trigger_at": next_check_time},
                task_name=task_name,
                force_overwrite=True,
            )
            logger.info(f"[{stream_id[:8]}] 已登记延迟续话，会在 {delay_seconds:.0f} 秒后重新判断。")
            return True, f"已登记一条延迟续话，会在 {delay_seconds:.0f} 秒后重新判断。"
        except Exception as e:
            logger.error(f"调度续话检查任务失败：{e}")
            return False, f"调度续话检查任务失败：{e}"

    async def _on_followup_timeout(self, stream_id: str) -> None:
        """延迟续话到点后执行判断。"""
        state = self._followup_states.get(stream_id)
        if (
            state is None
            or not state.is_waiting
            or state.active_check_kind != "followup"
            or state.pending_followup is None
        ):
            logger.debug(f"[{stream_id[:8]}] 跳过过期的延迟续话任务")
            return

        followup = state.pending_followup
        state.pending_followup = None
        state.is_waiting = False
        state.active_check_kind = None

        try:
            from src.app.plugin_system.api.stream_api import get_stream
            from src.core.managers.stream_manager import get_stream_manager

            chat_stream = await get_stream(stream_id)
            if chat_stream is None:
                chat_stream = get_stream_manager()._streams.get(stream_id)
            if chat_stream is None:
                logger.warning(f"[{stream_id[:8]}] 延迟续话未找到 chat_stream")
                return

            max_chain = 2
            if state.followup_chain_count >= max_chain:
                logger.info(f"[{stream_id[:8]}] 延迟续话链已达上限，结束本轮续话")
                state.followup_cooldown_until = datetime.now() + timedelta(minutes=10)
                state.followup_chain_count = 0
                return

            state.followup_chain_count += 1
            await self._wake_stream_for_followup(chat_stream, followup)
        except Exception as exc:
            logger.error(f"[{stream_id[:8]}] 延迟续话处理失败：{exc}", exc_info=True)
            state.followup_cooldown_until = datetime.now() + timedelta(minutes=10)
            state.followup_chain_count = 0

    async def _wake_stream_for_followup(self, chat_stream: Any, followup: PendingFollowup) -> None:
        """向目标流注入一条续话机会触发消息，并唤醒当前运行模式。"""
        from src.core.models.message import Message
        from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager
        import time
        import uuid

        stream_id = chat_stream.stream_id
        context = chat_stream.context
        target_user_id, target_user_name = self._resolve_followup_target(chat_stream)

        # 从聊天历史提取最近一次 bot 消息
        history = list(getattr(chat_stream.context, "history_messages", []) or [])
        last_bot_message = ""
        for msg in reversed(history):
            if str(getattr(msg, "sender_role", "") or "").lower() == "bot":
                last_bot_message = str(getattr(msg, "content", "") or "")
                break

        elapsed_seconds = followup.delay_seconds

        prompt = (
            "[延迟续话机会] 这不是用户的新消息，而是一次由系统交给你的主动续话机会。"
            "你必须先调用 action-record_inner_monologue，记录你此刻新的心理推进；"
            "然后再二选一：如果你觉得刚才的话头还值得延续，就像平时一样使用当前对话器的动作自己回复；"
            "如果觉得现在不该继续说，可以 pass_and_wait。"
            f"\n- 当前执行者：{chat_stream.bot_nickname or '你'}"
            f"\n- 距离你上一条显式消息已过去约 {elapsed_seconds:.0f} 秒"
            f"\n- 你刚刚对对方说的是：{last_bot_message or '（上一条消息为空）'}"
            f"\n- 你当时留下的未尽之意：{followup.thought or '（未填写）'}"
            f"\n- 续话主题：{followup.topic}"
            f"\n- 续话类型：{followup.followup_type}"
            "\n- 重要：不要机械续话，不要为了说而说；如果不自然，就先停住。"
        )

        trigger_message = Message(
            message_id=f"proactive_followup_{uuid.uuid4().hex[:12]}",
            platform=chat_stream.platform or "unknown",
            stream_id=stream_id,
            sender_id=target_user_id or "system",
            sender_name="系统（续话触发）",
            sender_role="other",
            content=prompt,
            processed_plain_text=prompt,
            time=time.time(),
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            is_proactive_followup_trigger=True,
            proactive_followup_topic=followup.topic,
            proactive_followup_type=followup.followup_type,
        )
        context.add_unread_message(trigger_message)
        loop_mgr = get_stream_loop_manager()
        removed = loop_mgr._wait_states.pop(stream_id, None)
        if removed:
            logger.debug(f"[{stream_id[:8]}] 已清除等待锁，准备让对话器处理续话机会")
        logger.info(
            f"[{stream_id[:8]}] 已注入续话机会触发消息：topic={followup.topic}, type={followup.followup_type}"
        )

    @staticmethod
    def _resolve_followup_target(chat_stream: Any) -> tuple[str, str]:
        """从当前流上下文推断续话对象。"""
        bot_id = str(getattr(chat_stream, "bot_id", "") or "")
        history = list(getattr(chat_stream.context, "history_messages", []) or [])
        for msg in reversed(history):
            sender_id = str(getattr(msg, "sender_id", "") or "")
            sender_role = str(getattr(msg, "sender_role", "") or "").lower()
            if sender_role == "bot":
                continue
            if bot_id and sender_id == bot_id:
                continue
            if sender_id:
                return sender_id, str(getattr(msg, "sender_name", "") or "")
        return "", ""

    async def enqueue_inner_dialogue(
        self,
        thought: str,
        *,
        mode: str = "reflect",
        expect_surface: bool = True,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
    ) -> dict[str, Any]:
        """接收主意识沉下来的内心对话（异步，进入中枢心跳处理）。"""
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        text = str(thought or "").strip()
        if not text:
            raise ValueError("thought 不能为空")

        mode_name = str(mode or "reflect").strip().lower() or "reflect"
        if mode_name not in {"notice", "reflect", "gap", "decide"}:
            mode_name = "reflect"

        receipt_id = f"idlg_{uuid4().hex[:12]}"
        event = self._event_builder.build_inner_dialogue_event(
            text,
            mode=mode_name,
            expect_surface=bool(expect_surface),
            receipt_id=receipt_id,
            stream_id=stream_id,
            platform=platform or "life_chatter",
            chat_type=chat_type,
            sender_name=sender_name or "主意识",
        )

        await self._queue_pending_event(event)

        log_message_received(
            received_at=event.timestamp,
            platform=event.source,
            chat_type=event.chat_type or "unknown",
            source_label=event.source_detail,
            source_detail=event.source_detail,
            stream_id=event.stream_id or "",
            sender_display=event.sender or "主意识",
            sender_id="life_chatter",
            message_id=event.event_id,
            reply_to=None,
            message_type=event.content_type,
            content=event.content,
            direction="received",
            pending_message_count=self._state.pending_event_count,
        )
        logger.info(
            "life_engine 已接收内心对话: "
            f"receipt={receipt_id} mode={mode_name} "
            f"stream_id={event.stream_id or 'unknown'} "
            f"expect_surface={bool(expect_surface)} "
            f"pending={self._state.pending_event_count}"
        )
        return {
            "event_id": event.event_id,
            "receipt_id": receipt_id,
            "mode": mode_name,
            "expect_surface": bool(expect_surface),
            "stream_id": event.stream_id or "",
            "pending_event_count": self._state.pending_event_count,
            "queued": True,
            "channel": "inner_dialogue",
        }

    async def enqueue_dfc_message(
        self,
        message: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
    ) -> dict[str, Any]:
        """接收来自 DFC 的异步留言。"""
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        text = str(message or "").strip()
        if not text:
            raise ValueError("message 不能为空")

        event = self._event_builder.build_dfc_message_event(
            text,
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            sender_name=sender_name,
        )

        await self._queue_pending_event(event)

        log_message_received(
            received_at=event.timestamp,
            platform=event.source,
            chat_type=event.chat_type or "unknown",
            source_label=event.source_detail,
            source_detail=event.source_detail,
            stream_id=event.stream_id or "",
            sender_display=event.sender or "另一个我（DFC）",
            sender_id="default_chatter",
            message_id=event.event_id,
            reply_to=None,
            message_type=event.content_type,
            content=event.content,
            direction="received",
            pending_message_count=self._state.pending_event_count,
        )
        logger.info(
            "life_engine 已接收 DFC 留言: "
            f"stream_id={event.stream_id or 'unknown'} "
            f"sender={event.sender or '另一个我（DFC）'} "
            f"pending={self._state.pending_event_count}"
        )
        return {
            "event_id": event.event_id,
            "stream_id": event.stream_id or "",
            "pending_event_count": self._state.pending_event_count,
            "queued": True,
        }

    async def enqueue_outer_message(
        self,
        message: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
    ) -> dict[str, Any]:
        """接收来自对外运行模式的异步留言。"""
        return await self.enqueue_dfc_message(
            message,
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            sender_name=sender_name,
        )

    async def enqueue_direct_message(
        self,
        message: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
        sender_id: str = "",
    ) -> dict[str, Any]:
        """接收用户通过命令直达生命中枢的留言。"""
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        text = str(message or "").strip()
        if not text:
            raise ValueError("message 不能为空")

        event = self._event_builder.build_direct_message_event(
            text,
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            sender_name=sender_name,
        )

        await self._queue_pending_event(event)

        log_message_received(
            received_at=event.timestamp,
            platform=event.source,
            chat_type=event.chat_type or "unknown",
            source_label=event.source_detail,
            source_detail=event.source_detail,
            stream_id=event.stream_id or "",
            sender_display=event.sender or "外部用户",
            sender_id=str(sender_id or "").strip() or "external_user",
            message_id=event.event_id,
            reply_to=None,
            message_type=event.content_type,
            content=event.content,
            direction="received",
            pending_message_count=self._state.pending_event_count,
        )
        logger.info(
            "life_engine 已接收直连留言: "
            f"stream_id={event.stream_id or 'unknown'} "
            f"sender={event.sender or '外部用户'} "
            f"pending={self._state.pending_event_count}"
        )
        return {
            "event_id": event.event_id,
            "stream_id": event.stream_id or "",
            "pending_event_count": self._state.pending_event_count,
            "queued": True,
            "channel": "direct_command",
        }

    async def enqueue_proactive_opportunity(
        self,
        message: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
    ) -> dict[str, Any]:
        """接收 proactive 插件产生的主动机会事件。"""
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        text = str(message or "").strip()
        if not text:
            raise ValueError("message 不能为空")

        event = self._event_builder.build_proactive_opportunity_event(
            text,
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            sender_name=sender_name,
        )

        await self._queue_pending_event(event)

        logger.info(
            "life_engine 已接收主动机会事件: "
            f"stream_id={event.stream_id or 'unknown'} "
            f"pending={self._state.pending_event_count}"
        )
        return {
            "event_id": event.event_id,
            "stream_id": event.stream_id or "",
            "pending_event_count": self._state.pending_event_count,
            "queued": True,
            "channel": "proactive_opportunity",
        }

    def _autonomy_store(self) -> AutonomyIntentStore:
        return AutonomyIntentStore(self._workspace_dir())

    def _autonomy_next_scheduled_at(self, intent_id: str) -> str:
        intent = self._autonomy_store().get(intent_id)
        if intent is None or intent.status != "scheduled":
            return ""
        return intent.scheduled_at

    def _record_life_moment(
        self,
        *,
        kind: str,
        summary: str,
        operation: str,
        reason: str = "",
        source_event_id: str = "",
        stream_id: str = "",
    ) -> None:
        """转折点入长河；长河故障绝不影响主流程。"""
        try:
            LifeTraceStore(self._workspace_dir()).record_moment(
                kind=kind,
                summary=summary,
                operation=operation,
                actor="life_engine",
                reason=reason,
                source_event_id=source_event_id,
                stream_id=stream_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"长河留痕失败 kind={kind}: {exc}")

    async def _resolve_autonomy_target_stream_id(
        self,
        *,
        target_stream_id: str = "",
        target_key: str = "",
    ) -> str:
        explicit = str(target_stream_id or "").strip()
        if explicit:
            return explicit
        key = str(target_key or "").strip()
        if not key:
            return ""
        try:
            from ..core.send_targets import resolve_send_target_key

            runtime_cfg = getattr(self._cfg(), "runtime_sync", None)
            target = await resolve_send_target_key(
                key,
                current_stream_id="",
                limit=int(getattr(runtime_cfg, "send_targets_limit", 8) or 8),
                active_window_hours=float(getattr(runtime_cfg, "send_targets_window_hours", 24.0) or 24.0),
            )
            if target is not None:
                return str(target.stream_id or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"解析自主意向 target_key 失败: {exc}")
        return ""

    async def schedule_autonomy_intent(
        self,
        *,
        kind: str,
        motivation: str,
        delay_minutes: int,
        target_hint: str = "",
        target_stream_id: str = "",
        target_key: str = "",
        constraints: list[str] | None = None,
        repeat: bool = False,
        interval_minutes: int | None = None,
        max_occurrences: int | None = None,
        lease_minutes: int | None = None,
    ) -> dict[str, Any]:
        """登记一个 life_engine 自主形成的延迟意向。"""
        cfg = self._cfg()
        autonomy_cfg = getattr(cfg, "autonomy", None)
        if autonomy_cfg is not None and not bool(getattr(autonomy_cfg, "enabled", True)):
            raise RuntimeError("自主意向循环未启用")

        resolved_stream_id = await self._resolve_autonomy_target_stream_id(
            target_stream_id=target_stream_id,
            target_key=target_key,
        )
        intent = build_intent(
            kind=kind,
            motivation=motivation,
            delay_minutes=int(delay_minutes or 0),
            min_delay_minutes=int(getattr(autonomy_cfg, "min_delay_minutes", 1) or 1),
            max_delay_minutes=int(getattr(autonomy_cfg, "max_delay_minutes", 1440) or 1440),
            target_hint=target_hint,
            target_key=target_key,
            target_stream_id=resolved_stream_id,
            constraints=constraints or [],
            repeat=repeat,
            interval_minutes=interval_minutes,
            max_occurrences=max_occurrences,
            lease_minutes=lease_minutes,
        )

        async with self._get_lock():
            store = self._autonomy_store()
            try:
                await register_autonomy_schedule(self.plugin, intent)
            except RuntimeError as exc:
                raise RuntimeError("调度器尚未启动，稍后再登记自主意向") from exc
            store.upsert(intent)
            store.append_event("formed", intent, detail="intent scheduled")

        repeat_text = f"repeat=每隔{intent.interval_minutes}分钟 " if intent.repeat else ""
        event_text = (
            f"已登记自主意向：kind={intent.kind} delay={intent.delay_minutes}分钟 "
            f"{repeat_text}motivation={intent.motivation}"
        )
        event = self._event_builder.build_autonomy_intent_event(
            event_text,
            content_type="autonomy_intent_scheduled",
            stream_id=intent.target_stream_id,
            sender_name="自主意向",
        )
        await self._queue_pending_event(event)
        self._record_life_moment(
            kind="intent",
            summary=f"形成意向（{intent.kind}）：{intent.motivation[:120]}",
            operation="formed",
            source_event_id=intent.intent_id,
            stream_id=intent.target_stream_id,
        )

        logger.info(
            "新意向: "
            f"kind={intent.kind} delay={intent.delay_minutes}m "
            f"repeat={intent.repeat} "
            f"intent_id={intent.intent_id[:12]} "
            f"stream={intent.target_stream_id or '-'}"
        )
        result: dict[str, Any] = {
            "created": True,
            "intent_id": intent.intent_id,
            "kind": intent.kind,
            "delay_minutes": intent.delay_minutes,
            "repeat": intent.repeat,
            "interval_minutes": intent.interval_minutes,
            "max_occurrences": intent.max_occurrences,
            "lease_until": intent.lease_until,
            "scheduled_at": intent.scheduled_at,
            "status": intent.status,
            "target_stream_id": intent.target_stream_id,
            "target_hint": intent.target_hint,
            "schedule_id": intent.schedule_id,
        }
        if intent.kind == "speak" and not intent.target_stream_id:
            # 降级必须有声：不能让她以为表达层会被唤醒而实际只是事件浮现
            result["note"] = (
                "未指定目标：到点后这个意向只会以事件形式浮现给心跳，不会唤醒表达层。"
                "如果你想让它真正交给表达层，可以重新登记并填 target_key 或 target_stream_id。"
            )
            targets = await self._list_autonomy_send_targets()
            if targets:
                result["available_targets"] = targets
        return result

    async def _list_autonomy_send_targets(self) -> list[dict[str, str]]:
        """列出近期可触达的发送目标，供意向登记时参考。"""
        try:
            from ..core.send_targets import list_recent_send_targets

            runtime_cfg = getattr(self._cfg(), "runtime_sync", None)
            targets = await list_recent_send_targets(
                current_stream_id="",
                limit=int(getattr(runtime_cfg, "send_targets_limit", 8) or 8),
                active_window_hours=float(
                    getattr(runtime_cfg, "send_targets_window_hours", 24.0) or 24.0
                ),
            )
            return [
                {
                    "target_key": target.target_key,
                    "name": target.display_name,
                    "type": (
                        f"{target.platform}"
                        f"{'群聊' if target.chat_type == 'group' else '私聊'}"
                    ),
                }
                for target in targets
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"列出可发送目标失败: {exc}")
            return []

    async def claim_autonomy_occurrences(
        self,
        occurrences: list[dict[str, str]],
        *,
        action_id: str,
        target_stream_id: str,
    ) -> dict[str, Any]:
        """Atomically claim autonomous occurrences before an external action."""

        unique = {
            (str(item.get("intent_id") or ""), str(item.get("occurrence_id") or ""))
            for item in occurrences
            if item.get("intent_id") and item.get("occurrence_id")
        }
        if not unique:
            return {"claimed": True, "count": 0}

        async with self._get_lock():
            store = self._autonomy_store()
            loaded: list[AutonomyIntent] = []
            for intent_id, occurrence_id in sorted(unique):
                intent = store.get(intent_id)
                if intent is None:
                    return {"claimed": False, "reason": f"intent_not_found:{intent_id}"}
                if intent.status != "in_flight":
                    return {"claimed": False, "reason": f"status={intent.status}"}
                if intent.active_occurrence_id != occurrence_id:
                    return {"claimed": False, "reason": "occurrence_mismatch"}
                if intent.active_occurrence_status != "surfaced":
                    return {
                        "claimed": False,
                        "reason": f"occurrence_status={intent.active_occurrence_status}",
                    }
                if str(intent.target_stream_id or "") != str(target_stream_id or ""):
                    return {"claimed": False, "reason": "cross_stream_not_authorized"}
                loaded.append(intent)

            for intent in loaded:
                intent.active_occurrence_status = "dispatching"
                intent.active_action_id = str(action_id or "")
                intent.updated_at = _now_iso()
                store.upsert(intent)
                store.append_event(
                    "delivery_claimed",
                    intent,
                    occurrence_id=intent.active_occurrence_id,
                    action_id=action_id,
                )
        return {"claimed": True, "count": len(loaded)}

    async def complete_autonomy_occurrences(
        self,
        occurrences: list[dict[str, str]],
        *,
        outcome: str,
        action_id: str = "",
        detail: str = "",
    ) -> dict[str, Any]:
        """Commit terminal occurrence receipts and only then chain recurrence."""

        unique = {
            (str(item.get("intent_id") or ""), str(item.get("occurrence_id") or ""))
            for item in occurrences
            if item.get("intent_id") and item.get("occurrence_id")
        }
        if not unique:
            return {"completed": 0, "scheduled": 0}

        safe_recurrence_outcomes = {
            "sent",
            "passed",
            "reflected",
            "silence",
            "surfaced",
        }
        to_schedule: list[str] = []
        completed = 0
        async with self._get_lock():
            store = self._autonomy_store()
            for intent_id, occurrence_id in sorted(unique):
                intent = store.get(intent_id)
                if intent is None:
                    continue
                if (
                    intent.last_occurrence_id == occurrence_id
                    and intent.last_outcome == outcome
                ):
                    completed += 1
                    continue
                if intent.active_occurrence_id != occurrence_id:
                    continue
                if intent.status != "in_flight":
                    continue

                intent.last_occurrence_id = occurrence_id
                intent.last_outcome = str(outcome or "unknown")
                intent.active_occurrence_status = str(outcome or "unknown")
                intent.active_action_id = str(action_id or intent.active_action_id)
                intent.last_error = str(detail or "")[:240]
                intent.updated_at = _now_iso()

                lease_reason = recurring_lease_reason(intent)
                if intent.repeat and outcome in safe_recurrence_outcomes and not lease_reason:
                    next_minutes = int(
                        intent.interval_minutes or intent.delay_minutes or 1
                    )
                    intent.scheduled_at = (
                        datetime.now(timezone.utc).astimezone()
                        + timedelta(minutes=next_minutes)
                    ).isoformat()
                    intent.status = "scheduled"
                    intent.renewal_reason = ""
                    to_schedule.append(intent.intent_id)
                elif intent.repeat:
                    intent.status = "renewal_required"
                    intent.renewal_reason = lease_reason or (
                        "previous occurrence did not reach a retry-safe terminal state"
                    )
                else:
                    intent.status = "triggered" if outcome in safe_recurrence_outcomes else "failed"

                intent.active_occurrence_id = ""
                intent.active_occurrence_status = ""
                intent.active_occurrence_started_at = ""
                store.upsert(intent)
                store.append_event(
                    f"occurrence_{outcome}",
                    intent,
                    occurrence_id=occurrence_id,
                    action_id=action_id,
                    detail=detail or intent.renewal_reason,
                )
                completed += 1

        scheduled = 0
        for intent_id in to_schedule:
            store = self._autonomy_store()
            intent = store.get(intent_id)
            if intent is None or intent.status != "scheduled":
                continue
            try:
                await register_autonomy_schedule(self.plugin, intent)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "自主意向下一 occurrence 调度失败，状态已保留供恢复: "
                    f"intent_id={intent.intent_id[:12]} error={exc}"
                )
                continue
            async with self._get_lock():
                current = self._autonomy_store().get(intent.intent_id)
                if current is None or current.status != "scheduled":
                    continue
                current.schedule_id = intent.schedule_id
                current.updated_at = _now_iso()
                self._autonomy_store().upsert(current)
            scheduled += 1
        return {"completed": completed, "scheduled": scheduled}

    async def manage_autonomy_intent(
        self,
        *,
        action: str,
        intent_id: str = "",
        additional_occurrences: int = 0,
        lease_minutes: int = 0,
    ) -> dict[str, Any]:
        """Expose explicit subject-owned pause/cancel/renew lifecycle choices."""

        normalized_action = str(action or "").strip().lower()
        if normalized_action == "list":
            return {
                "intents": [
                    {
                        "intent_id": item.intent_id,
                        "kind": item.kind,
                        "motivation": item.motivation,
                        "status": item.status,
                        "repeat": item.repeat,
                        "occurrence_count": item.occurrence_count,
                        "max_occurrences": item.max_occurrences,
                        "lease_until": item.lease_until,
                        "scheduled_at": item.scheduled_at,
                        "target_hint": item.target_hint,
                        "renewal_reason": item.renewal_reason,
                    }
                    for item in self._autonomy_store().load()
                ]
            }
        if normalized_action not in {"pause", "cancel", "renew"}:
            raise ValueError("action must be list / pause / cancel / renew")

        target_id = str(intent_id or "").strip()
        if not target_id:
            raise ValueError("intent_id is required")

        schedule_id = ""
        should_schedule = False
        async with self._get_lock():
            store = self._autonomy_store()
            intent = store.get(target_id)
            if intent is None:
                raise ValueError("autonomy intent not found")
            schedule_id = intent.schedule_id

            if normalized_action in {"pause", "cancel"}:
                active_occurrence_id = intent.active_occurrence_id
                intent.status = "paused" if normalized_action == "pause" else "cancelled"
                intent.renewal_reason = ""
                intent.schedule_id = ""
                if active_occurrence_id:
                    intent.last_occurrence_id = active_occurrence_id
                    intent.last_outcome = normalized_action
                intent.active_occurrence_id = ""
                intent.active_occurrence_status = ""
                intent.active_occurrence_started_at = ""
                intent.active_action_id = ""
                intent.updated_at = _now_iso()
                store.upsert(intent)
                store.append_event(
                    normalized_action,
                    intent,
                    occurrence_id=active_occurrence_id,
                )
            else:
                if not intent.repeat:
                    raise ValueError("only recurring intents can be renewed")
                if intent.status == "in_flight" or intent.active_occurrence_id:
                    raise ValueError(
                        "cannot renew while an occurrence is in flight; "
                        "wait for its receipt or pause/cancel it first"
                    )
                was_scheduled = intent.status == "scheduled"
                additional = int(additional_occurrences or 0)
                lease = int(lease_minutes or 0)
                if additional <= 0 and lease <= 0:
                    raise ValueError(
                        "renew requires additional_occurrences or lease_minutes"
                    )
                if additional < 0 or additional > 10_000:
                    raise ValueError("additional_occurrences must be between 1 and 10000")
                if lease < 0 or lease > 7 * 24 * 60:
                    raise ValueError("lease_minutes must be between 1 and 10080")
                if additional > 0:
                    intent.max_occurrences = (
                        max(intent.max_occurrences, intent.occurrence_count)
                        + additional
                    )
                    if intent.max_occurrences > 10_000:
                        raise ValueError(
                            "renewed max_occurrences cannot exceed 10000"
                        )
                if lease > 0:
                    intent.lease_until = (
                        datetime.now(timezone.utc).astimezone()
                        + timedelta(minutes=lease)
                    ).isoformat()
                intent.status = "scheduled"
                intent.renewal_reason = ""
                intent.active_occurrence_id = ""
                intent.active_occurrence_status = ""
                intent.active_occurrence_started_at = ""
                intent.active_action_id = ""
                if not was_scheduled:
                    intent.scheduled_at = (
                        datetime.now(timezone.utc).astimezone()
                        + timedelta(
                            minutes=int(
                                intent.interval_minutes or intent.delay_minutes or 1
                            )
                        )
                    ).isoformat()
                    intent.schedule_id = ""
                else:
                    # The registered callback resolves the latest intent state
                    # by ID, so an active schedule needs no destructive churn.
                    schedule_id = ""
                intent.updated_at = _now_iso()
                store.upsert(intent)
                store.append_event("renewed", intent)
                should_schedule = not was_scheduled

        if schedule_id:
            try:
                await get_unified_scheduler().remove_schedule(schedule_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    f"移除旧自主意向调度失败 intent_id={target_id[:12]}: {exc}"
                )

        if should_schedule:
            intent = self._autonomy_store().get(target_id)
            if intent is None:
                raise RuntimeError("renewed autonomy intent disappeared")
            await register_autonomy_schedule(self.plugin, intent)
            async with self._get_lock():
                current = self._autonomy_store().get(target_id)
                if current is not None and current.status == "scheduled":
                    current.schedule_id = intent.schedule_id
                    current.updated_at = _now_iso()
                    self._autonomy_store().upsert(current)

        current = self._autonomy_store().get(target_id)
        return {
            "intent_id": target_id,
            "action": normalized_action,
            "status": current.status if current is not None else "missing",
            "scheduled_at": current.scheduled_at if current is not None else "",
            "max_occurrences": current.max_occurrences if current is not None else 0,
            "lease_until": current.lease_until if current is not None else "",
        }

    async def trigger_autonomy_intent(self, intent_id: str) -> dict[str, Any]:
        """Surface one leased occurrence; never pre-schedule the next one."""

        async with self._get_lock():
            store = self._autonomy_store()
            intent = store.get(intent_id)
            if intent is None:
                logger.warning(f"到点意向不存在: intent_id={intent_id}")
                return {"triggered": False, "reason": "not_found"}
            if intent.status != "scheduled":
                logger.debug(
                    f"跳过非 scheduled 自主意向: intent_id={intent.intent_id[:12]} status={intent.status}"
                )
                return {"triggered": False, "reason": f"status={intent.status}"}

            lease_reason = recurring_lease_reason(intent)
            if lease_reason:
                intent.status = "renewal_required"
                intent.renewal_reason = lease_reason
                intent.updated_at = _now_iso()
                store.upsert(intent)
                store.append_event("renewal_required", intent, detail=lease_reason)
                return {"triggered": False, "reason": lease_reason}

            intent.triggered_at = _now_iso()
            intent.occurrence_count = max(0, int(intent.occurrence_count or 0)) + 1
            intent.active_occurrence_id = occurrence_id_for(intent)
            intent.active_occurrence_status = "surfaced"
            intent.active_occurrence_started_at = intent.triggered_at
            intent.active_action_id = ""
            intent.retry_count = 0
            intent.last_error = ""
            intent.schedule_id = ""
            intent.status = "in_flight"
            intent.updated_at = intent.triggered_at
            store.upsert(intent)
            store.append_event(
                "occurrence_surfaced",
                intent,
                occurrence_id=intent.active_occurrence_id,
            )

        occurrence_ref = [{
            "intent_id": intent.intent_id,
            "occurrence_id": intent.active_occurrence_id,
        }]
        logger.info(
            "到点: "
            f"intent_id={intent.intent_id[:12]} kind={intent.kind} "
            f"repeat={intent.repeat} occurrence={intent.occurrence_count} "
            f"occurrence_id={intent.active_occurrence_id} "
            f"stream={intent.target_stream_id or '-'}"
        )

        if intent.kind == "speak":
            if not intent.target_stream_id:
                event = self._event_builder.build_autonomy_intent_event(
                    format_due_message(intent),
                    content_type="autonomy_intent_due",
                    sender_name="自主意向",
                )
                await self._queue_pending_event(event)
                self._record_life_moment(
                    kind="intent",
                    summary=f"意向到点无目标，浮现给心跳：{intent.motivation[:120]}",
                    operation="surfaced",
                    source_event_id=intent.active_occurrence_id,
                )
                await self.complete_autonomy_occurrences(
                    occurrence_ref,
                    outcome="surfaced",
                )
                logger.info(f"仲裁: downgraded intent_id={intent.intent_id[:12]} reason=no_target_stream")
                return {
                    "triggered": True,
                    "dispatch": "life_event",
                    "reason": "no_target_stream",
                    "repeat": intent.repeat,
                    "next_scheduled_at": self._autonomy_next_scheduled_at(
                        intent.intent_id
                    ),
                    "occurrence_id": intent.active_occurrence_id,
                    "occurrence_count": intent.occurrence_count,
                }
            try:
                await self._wake_stream_for_autonomy(intent)
            except Exception as exc:
                await self.complete_autonomy_occurrences(
                    occurrence_ref,
                    outcome="failed",
                    detail=str(exc),
                )
                raise
            self._record_life_moment(
                kind="intent",
                summary=f"意向到点，交给表达层：{intent.motivation[:120]}",
                operation="surfaced",
                source_event_id=intent.active_occurrence_id,
                stream_id=intent.target_stream_id,
            )
            logger.info(f"承接: life_chatter intent_id={intent.intent_id[:12]}")
            return {
                "triggered": True,
                "dispatch": "life_chatter",
                "stream_id": intent.target_stream_id,
                "repeat": intent.repeat,
                "next_scheduled_at": "",
                "occurrence_id": intent.active_occurrence_id,
                "occurrence_count": intent.occurrence_count,
            }

        if intent.kind == "reflect":
            event = self._event_builder.build_autonomy_intent_event(
                format_due_message(intent),
                content_type="autonomy_intent_due",
                sender_name="自主意向",
            )
            await self._queue_pending_event(event)
            self._record_life_moment(
                kind="intent",
                summary=f"意向到点，回到心跳继续思考：{intent.motivation[:120]}",
                operation="reflected",
                source_event_id=intent.active_occurrence_id,
            )
            await self.complete_autonomy_occurrences(
                occurrence_ref,
                outcome="reflected",
            )
            logger.info(f"承接: life_engine intent_id={intent.intent_id[:12]}")
            return {
                "triggered": True,
                "dispatch": "life_engine",
                "repeat": intent.repeat,
                "next_scheduled_at": self._autonomy_next_scheduled_at(
                    intent.intent_id
                ),
                "occurrence_id": intent.active_occurrence_id,
                "occurrence_count": intent.occurrence_count,
            }

        event = self._event_builder.build_autonomy_intent_event(
            f"自主意向到点后选择沉默：{intent.motivation}",
            content_type="autonomy_intent_silence",
            sender_name="自主意向",
        )
        await self._queue_pending_event(event)
        self._record_life_moment(
            kind="intent",
            summary=f"意向到点，选择沉默：{intent.motivation[:120]}",
            operation="silence",
            source_event_id=intent.active_occurrence_id,
        )
        await self.complete_autonomy_occurrences(
            occurrence_ref,
            outcome="silence",
        )
        logger.info(f"承接: silence intent_id={intent.intent_id[:12]}")
        return {
            "triggered": True,
            "dispatch": "silence",
            "repeat": intent.repeat,
            "next_scheduled_at": self._autonomy_next_scheduled_at(
                intent.intent_id
            ),
            "occurrence_id": intent.active_occurrence_id,
            "occurrence_count": intent.occurrence_count,
        }

    async def _wake_stream_for_autonomy(self, intent: AutonomyIntent) -> None:
        """把到点自主意向注入目标聊天流，交给 life_chatter 承接。"""
        from src.core.managers import get_stream_manager
        from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager
        import time

        stream_id = str(intent.target_stream_id or "").strip()
        chat_stream = await get_stream_manager().get_or_create_stream(stream_id=stream_id)
        context = chat_stream.context
        target_user_id, target_user_name = self._resolve_followup_target(chat_stream)
        prompt = format_due_message(intent)
        trigger_message = Message(
            message_id=(
                "autonomy_intent_"
                f"{intent.intent_id[:16]}_{max(1, int(intent.occurrence_count or 0))}"
            ),
            platform=chat_stream.platform or "unknown",
            stream_id=stream_id,
            sender_id=target_user_id or "life_engine_autonomy",
            sender_name="系统（自主意向浮现）",
            sender_role="other",
            content=prompt,
            processed_plain_text=prompt,
            message_type=MessageType.TEXT,
            time=time.time(),
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            is_autonomy_intent_trigger=True,
            autonomy_intent_id=intent.intent_id,
            autonomy_intent_kind=intent.kind,
            autonomy_occurrence_id=intent.active_occurrence_id,
            autonomy_authorized_stream_id=stream_id,
        )
        context.add_unread_message(trigger_message)
        loop_mgr = get_stream_loop_manager()
        removed = loop_mgr._wait_states.pop(stream_id, None)
        if removed:
            logger.debug(f"[{stream_id[:8]}] 已清除等待锁，准备处理自主意向")

    async def record_chatter_inner_monologue(
        self,
        thought: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
        mood: str = "",
        intent: str = "",
        topic: str = "",
    ) -> dict[str, Any]:
        """记录由 life_chatter 生成的内心独白。"""
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        text = str(thought or "").strip()
        if not text:
            raise ValueError("thought 不能为空")

        event = self._event_builder.build_chatter_inner_monologue_event(
            text,
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            sender_name=sender_name,
            mood=mood,
            intent=intent,
            topic=topic,
        )

        await self._queue_pending_event(event)

        logger.info(
            "life_engine 已记录对话器内心独白: "
            f"stream_id={event.stream_id or 'unknown'} "
            f"pending={self._state.pending_event_count}"
        )
        return {
            "event_id": event.event_id,
            "stream_id": event.stream_id or "",
            "pending_event_count": self._state.pending_event_count,
            "queued": True,
            "channel": "chatter_inner_monologue",
        }

    async def record_chatter_think_snapshot(
        self,
        *,
        stream_id: str,
        thought: str,
        mood: str = "",
        decision: str = "",
        expected_response: str = "",
    ) -> dict[str, Any]:
        """记录 life_chatter 最近一次独白/思考快照。

        纯文本独白使用 chatter_inner_monologue 事件记录；这里保留按 stream
        写入，兼容旧 action-think 工具和已持久化状态。
        """
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        sid = str(stream_id or "").strip()
        if not sid:
            raise ValueError("stream_id 不能为空")

        snapshot = {
            "thought": _shorten_text(str(thought or "").strip(), max_length=500),
            "mood": _shorten_text(str(mood or "").strip(), max_length=80),
            "decision": _shorten_text(str(decision or "").strip(), max_length=160),
            "expected_response": _shorten_text(
                str(expected_response or "").strip(),
                max_length=160,
            ),
            "recorded_at": _now_iso(),
        }

        async with self._get_lock():
            latest = dict(self._state.last_chatter_think_by_stream or {})
            compact_snapshot = {
                key: value
                for key, value in snapshot.items()
                if isinstance(value, str) and value.strip()
            }
            latest[sid] = compact_snapshot
            latest[LIFE_CHATTER_GLOBAL_CURSOR_KEY] = compact_snapshot
            self._state.last_chatter_think_by_stream = latest
        await self._save_runtime_context()

        return {
            "stream_id": sid,
            "recorded_at": snapshot["recorded_at"],
            "channel": "chatter_think_snapshot",
        }

    async def record_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        heartbeat_run_id: str | None = None,
        call_id: str | None = None,
        parent_event_id: str | None = None,
        causation_id: str | None = None,
    ) -> LifeEngineEvent:
        """记录工具调用事件；旧调用方仍可只传工具名和参数。"""
        event = self._event_builder.build_tool_call_event(
            tool_name,
            tool_args,
            heartbeat_run_id=heartbeat_run_id,
            call_id=call_id,
            parent_event_id=parent_event_id,
            causation_id=causation_id,
        )
        await self._queue_pending_event(event, persist=heartbeat_run_id is None)
        return event

    async def record_tool_result(
        self,
        tool_name: str,
        result: str,
        success: bool,
        *,
        heartbeat_run_id: str | None = None,
        call_id: str | None = None,
        parent_event_id: str | None = None,
        causation_id: str | None = None,
        call_event: LifeEngineEvent | None = None,
    ) -> LifeEngineEvent:
        """记录工具返回结果事件；旧调用方仍可使用原签名。"""
        event = self._event_builder.build_tool_result_event(
            tool_name,
            result,
            success,
            heartbeat_run_id=heartbeat_run_id,
            call_id=call_id,
            parent_event_id=parent_event_id,
            causation_id=causation_id,
            call_event=call_event,
        )
        await self._queue_pending_event(event, persist=heartbeat_run_id is None)
        return event

    async def _collect_background_agent_results(self) -> None:
        """收集已完成的后台智能体结果，注入为事件。"""
        coordinator = getattr(self.plugin, "_agent_coordinator", None)
        if coordinator is None or not coordinator.has_pending():
            return

        results = await coordinator.collect_results(timeout_seconds=5.0)
        if not results:
            return

        events: list[LifeEngineEvent] = []
        for agent_id, result in results.items():
            event = self._event_builder.build_agent_result_event(
                agent_type=result.agent_type,
                result_text=result.result_text,
                success=result.success,
                rounds=result.rounds_used,
                duration_ms=result.duration_ms,
            )
            events.append(event)

        await self._append_history(events)

    async def _collect_background_mission_results(self) -> None:
        """收集已完成的后台使命结果，注入为事件。"""
        try:
            from ..agents.mission_tool import get_all_missions
            from ..agents.contracts import MissionStatus
        except ImportError:
            return

        missions = get_all_missions()
        if not missions:
            return

        events: list[LifeEngineEvent] = []
        for mission_id, mission in missions.items():
            # 只收集已完成的后台使命
            if mission.sync:
                continue
            if mission.status not in (
                MissionStatus.SUCCEEDED, MissionStatus.PARTIAL,
                MissionStatus.FAILED, MissionStatus.CANCELLED,
                MissionStatus.TIMEOUT,
            ):
                continue
            # 检查是否已经被收集过
            if getattr(mission, "_collected", False):
                continue
            mission._collected = True  # type: ignore[attr-defined]

            event = self._event_builder.build_agent_result_event(
                agent_type="mission",
                result_text=mission.summary_text(),
                success=mission.status == MissionStatus.SUCCEEDED,
                rounds=mission.progress[0],
                duration_ms=int(mission.elapsed_seconds * 1000),
            )
            events.append(event)

        if events:
            await self._append_history(events)

    async def drain_pending_events(self) -> list[LifeEngineEvent]:
        """清空并返回当前待处理事件。"""
        async with self._get_lock():
            pending = list(self._pending_events)
            self._pending_events.clear()
            self._state.pending_event_count = 0
        return pending

    async def _append_history(
        self,
        events: list[LifeEngineEvent],
        *,
        publish_raw: bool = True,
        persist: bool = True,
    ) -> None:
        """将事件追加到原始历史；确认后的压缩由 heartbeat commit 统一执行。"""
        if not events:
            return

        async with self._get_lock():
            self._event_history.extend(events)
            self._event_history.sort(
                key=lambda event: (int(event.sequence or 0), str(event.event_id or ""))
            )
            self._state.history_event_count = len(self._event_history)
        if publish_raw:
            await self._publish_raw_events(events)
        if persist:
            await self._save_runtime_context()

    async def _prepare_heartbeat_context(self) -> PreparedHeartbeatContext:
        """Drain pending events and prepare one fixed heartbeat snapshot."""
        registry = self.consciousness_registry
        if isinstance(registry, AsyncConsciousnessRegistry):
            await registry.reconcile_expired()
        else:
            await asyncio.to_thread(
                registry.reconcile_expired,
                timestamp=_now_iso(),
            )
        await self.save_consciousness_registry_async()

        pending = await self.drain_pending_events()
        if pending:
            await self._append_history(pending, publish_raw=False, persist=False)
            await self._save_runtime_context()

        async with self._get_lock():
            snapshot_events = list(self._event_history)
            cursor = int(self._state.heartbeat_context_cursor or 0)
            summary = dict(self._state.subconscious_summary or {})

        prepared = self._subconscious_context.prepare(
            snapshot_events,
            cursor=cursor,
            existing_summary=summary,
        )
        prepared.world_perception = await self.prepare_perception(
            "life_engine_subconscious"
        )
        prepared.content = (
            "<transient_world_perception>\n"
            f"{prepared.world_perception.content}\n"
            "</transient_world_perception>\n\n"
            f"{prepared.content}"
        )
        # 标记本轮是否包含外部入站消息（供学习系统判断交互）
        prepared.has_inbound_messages = any(
            event.event_type == EventType.MESSAGE for event in pending
        ) if pending else False
        self._state.last_wake_context_at = _now_iso()
        self._state.last_wake_context_size = len(prepared.selected_event_ids)
        log_wake_context_injected(
            task_name=self._cfg().model.task_name,
            wake_context_at=self._state.last_wake_context_at,
            context_message_count=len(prepared.selected_event_ids),
            drained_message_count=len(pending),
            history_message_count=len(snapshot_events),
            source_count=len({event.source for event in snapshot_events}),
            content_chars=len(prepared.content),
        )
        logger.debug(
            f"潜意识上下文全文:\n{prepared.content}"
        )
        logger.info(
            "life_engine 已准备唤醒上下文: "
            f"count={len(prepared.selected_event_ids)} drained={len(pending)} "
            f"high_water={prepared.snapshot_high_water} "
            f"task={self._cfg().model.task_name}"
        )
        return prepared

    async def _commit_heartbeat_context(
        self,
        prepared: PreparedHeartbeatContext,
        model_reply: str,
        heartbeat_run_id: str,
    ) -> None:
        """Commit one successful heartbeat snapshot and advance its cursor."""
        reply_text = str(model_reply or "").strip()
        heartbeat_event: LifeEngineEvent | None = None
        if reply_text:
            heartbeat_event = self._event_builder.build_heartbeat_event(
                reply_text,
                self._state.heartbeat_count,
                self._cfg().model.task_name or "core",
                heartbeat_run_id=heartbeat_run_id,
            )

        async with self._get_lock():
            run_events = [
                event
                for event in self._pending_events
                if event.heartbeat_run_id == heartbeat_run_id
            ]
            if run_events:
                for event in run_events:
                    # Successful model output is history, not a new wake-up signal.
                    event.heartbeat_context_consumed = True
                run_ids = {id(event) for event in run_events}
                self._pending_events = [
                    event for event in self._pending_events if id(event) not in run_ids
                ]
                self._event_history.extend(run_events)
            if heartbeat_event is not None:
                heartbeat_event.heartbeat_context_consumed = True
                self._event_history.append(heartbeat_event)
            self._event_history.sort(
                key=lambda event: (int(event.sequence or 0), str(event.event_id or ""))
            )
            acknowledged_ids = set(prepared.acknowledged_event_ids)
            if acknowledged_ids:
                for event in self._event_history:
                    if event.event_id in acknowledged_ids:
                        event.heartbeat_context_consumed = True

            self._state.pending_event_count = len(self._pending_events)
            self._state.subconscious_summary = prepared.updated_summary.to_dict()
            current_cursor = int(self._state.heartbeat_context_cursor or 0)
            # The commit frontier is the prepare snapshot only. Events created
            # by this model run, or arriving while it is running, belong to a
            # later heartbeat and must never be skipped by this commit.
            candidate_high_water = max(
                current_cursor,
                int(prepared.snapshot_high_water or 0),
            )
            has_unconsumed_gap = any(
                current_cursor < int(event.sequence or 0) <= candidate_high_water
                and event.event_type != EventType.SUMMARY
                and not event.heartbeat_context_consumed
                for event in self._event_history
            )
            if not has_unconsumed_gap:
                self._state.heartbeat_context_cursor = candidate_high_water

            self._event_history = self._subconscious_context.compact_history(
                self._event_history,
                cursor=self._state.heartbeat_context_cursor,
                existing_summary=self._state.subconscious_summary,
            )
            summary_events = [
                event
                for event in self._event_history
                if event.event_type == EventType.SUMMARY
                and str(event.content_type or "").strip().lower() == "subconscious_summary"
            ]
            if summary_events:
                try:
                    latest_summary = max(
                        summary_events,
                        key=lambda event: int(event.sequence or 0),
                    )
                    self._state.subconscious_summary = SubconsciousSummary.from_json(
                        latest_summary.content
                    ).to_dict()
                except (TypeError, ValueError):
                    pass
            self._state.history_event_count = len(self._event_history)

        if heartbeat_event is not None:
            await self._publish_raw_events([heartbeat_event])
        if isinstance(prepared.world_perception, PreparedPerception):
            await self.commit_perception(prepared.world_perception)
        await self._save_runtime_context()

    async def _prepare_and_commit_heartbeat_context(
        self,
        model_reply: str,
        heartbeat_run_id: str,
    ) -> PreparedHeartbeatContext:
        """Compatibility helper used by tests and heartbeat runners."""
        prepared = await self._prepare_heartbeat_context()
        await self._commit_heartbeat_context(prepared, model_reply, heartbeat_run_id)
        return prepared

    async def clear_runtime_context(self) -> None:
        """清理当前事件上下文。"""
        async with self._get_lock():
            self._pending_events.clear()
            self._event_history.clear()
            self._state.pending_event_count = 0
            self._state.history_event_count = 0
            self._state.event_sequence = 0
            self._state.heartbeat_context_cursor = 0
            self._state.subconscious_summary = {}
        await self._save_runtime_context()

    def _build_wake_context_text(self, events: list[LifeEngineEvent]) -> str:
        """把事件流拼成可注入的上下文文本。

        连续的工具操作（TOOL_CALL / 成功的 TOOL_RESULT）会被折叠为一行摘要，
        避免操作噪音占据意识窗口。失败的工具结果和 AGENT_RESULT 保留完整渲染。
        """
        if not events:
            return ""

        sorted_events = sorted(events, key=lambda e: e.sequence)
        lines: list[str] = []

        # 折叠连续工具操作的缓冲区
        tool_run_count = 0
        tool_run_failures: list[LifeEngineEvent] = []
        tool_run_start_time = ""

        def _flush_tool_run() -> None:
            """将累积的工具操作折叠为一行摘要。"""
            nonlocal tool_run_count, tool_run_failures, tool_run_start_time
            if tool_run_count <= 0:
                return
            time_display = _format_time_display(tool_run_start_time)
            fail_count = len(tool_run_failures)
            if fail_count == 0:
                lines.append(
                    f"[{time_display}] 🔧 执行了 {tool_run_count} 次工具操作（全部成功）"
                )
            else:
                lines.append(
                    f"[{time_display}] 🔧 执行了 {tool_run_count} 次工具操作"
                    f"（{fail_count} 次失败）"
                )
                # 失败的工具结果有体验价值，逐条展示
                for fail_event in tool_run_failures:
                    fail_time = _format_time_display(fail_event.timestamp)
                    result_short = _shorten_text(fail_event.content or "", max_length=160)
                    lines.append(f"[{fail_time}] ❌ {fail_event.tool_name}: {result_short}")
            tool_run_count = 0
            tool_run_failures = []
            tool_run_start_time = ""

        for event in sorted_events:
            # 工具操作折叠逻辑
            if event.event_type == EventType.TOOL_CALL:
                if tool_run_count == 0:
                    tool_run_start_time = event.timestamp
                tool_run_count += 1
                continue
            if event.event_type == EventType.TOOL_RESULT:
                if event.tool_success is False:
                    tool_run_failures.append(event)
                # 成功的 tool_result 不单独渲染，已被 tool_call 计数覆盖
                continue

            # 遇到非工具事件，先刷新之前的工具操作摘要
            _flush_tool_run()

            time_display = _format_time_display(event.timestamp)

            if event.event_type == EventType.MESSAGE:
                source = event.source_detail or event.source or "外部"
                source_short = self._simplify_source(source)
                line = f"[{time_display}] 📨 {source_short}"
                line += f"\n    └─ {event.sender}: {event.content}"
            elif event.event_type == EventType.SUMMARY:
                line = f"[{time_display}] 🧠 潜意识摘要"
                line += f"\n    └─ {event.content}"
            elif event.event_type == EventType.HEARTBEAT:
                line = f"[{time_display}] 💭 心跳#{event.heartbeat_index}"
                line += f"\n    └─ {event.content}"
            elif event.event_type == EventType.AGENT_RESULT:
                status = "✅" if event.tool_success else "❌"
                agent_name = event.tool_name or "agent"
                result_short = _shorten_text(event.content or "", max_length=200)
                line = f"[{time_display}] {status} 🤖 {agent_name}: {result_short}"
            else:
                line = f"[{time_display}] ❓ {event.content}"

            lines.append(line)

        # 尾部可能残留的工具操作
        _flush_tool_run()

        return "\n".join(lines)

    @staticmethod
    def _event_belongs_to_life_runtime(
        event: LifeEngineEvent,
        *,
        current_stream_id: str,
        unified_chatter_context: bool = False,
    ) -> bool:
        """判断事件是否应自动给 life_chatter 作为同源运行态可见。"""
        event_type = event.event_type
        if event_type == EventType.HEARTBEAT:
            stream_id = str(event.stream_id or "").strip()
            if unified_chatter_context:
                return True
            return bool(not stream_id or stream_id == current_stream_id)
        # 工具操作噪音过滤：
        # - TOOL_CALL 对表达层无体验价值，不注入 chatter
        # - TOOL_RESULT 仅在失败时可见（失败是有意义的障碍信号）
        # - AGENT_RESULT 保留（子代理完成工作是有意义的结果）
        if event_type == EventType.TOOL_CALL:
            return False
        if event_type == EventType.TOOL_RESULT:
            return event.tool_success is False
        if event_type == EventType.AGENT_RESULT:
            return True

        stream_id = str(event.stream_id or "").strip()
        content_type = str(event.content_type or "").strip().lower()
        source = str(event.source or "").strip().lower()

        if unified_chatter_context and stream_id:
            return True

        if current_stream_id and stream_id == current_stream_id:
            return content_type != "text"

        if content_type in {"heartbeat_reply", "chatter_inner_monologue"}:
            return True
        if content_type in {
            "proactive_opportunity",
            "dfc_message",
            "direct_message",
            "inner_dialogue",
            "autonomy_intent_due",
            "autonomy_intent_scheduled",
            "autonomy_intent_silence",
        }:
            return bool(not stream_id or stream_id == current_stream_id)
        return source == "life_engine" and not stream_id

    @staticmethod
    def _chatter_cursor_key(
        stream_id: str,
        *,
        unified_chatter_context: bool = False,
    ) -> str:
        if unified_chatter_context:
            return LIFE_CHATTER_GLOBAL_CURSOR_KEY
        return str(stream_id or "").strip()

    def _chatter_event_cursor(
        self,
        stream_id: str,
        *,
        unified_chatter_context: bool = False,
    ) -> int:
        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid:
            return 0
        return int((self._state.chatter_context_cursors or {}).get(sid, 0) or 0)

    # 兼容旧接口名
    def _chatter_context_cursor(self, stream_id: str) -> int:
        return self._chatter_event_cursor(stream_id)

    def _chatter_thought_cursor(
        self,
        stream_id: str,
        *,
        unified_chatter_context: bool = False,
    ) -> int:
        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid:
            return 0
        return int((self._state.chatter_thought_cursors or {}).get(sid, 0) or 0)

    async def mark_chatter_runtime_context_seen(
        self,
        stream_id: str,
        sequence: int,
        *,
        unified_chatter_context: bool = False,
    ) -> None:
        """标记某个聊天流已经看过的 life 事件流高水位（event cursor）。

        thought_revision cursor 在 build_chatter_runtime_context 渲染时已内部提交，
        外部调用方仍只需要传 event 序列高水位。
        """
        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid:
            return
        if sequence > 0:
            async with self._get_lock():
                cursors = self._state.chatter_context_cursors
                cursors[sid] = max(
                    int(cursors.get(sid, 0) or 0),
                    int(sequence),
                )
        prepared = self._pending_chatter_perceptions.get(sid)
        if prepared is not None:
            await self.commit_perception(prepared)
            if self._pending_chatter_perceptions.get(sid) is prepared:
                self._pending_chatter_perceptions.pop(sid, None)

    def has_pending_chatter_perception(
        self,
        stream_id: str,
        *,
        unified_chatter_context: bool = False,
    ) -> bool:
        """Return whether a successful model turn still must ack world context."""

        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        return bool(sid and sid in self._pending_chatter_perceptions)

    async def _commit_chatter_thought_cursor(
        self,
        stream_id: str,
        revision: int,
        *,
        unified_chatter_context: bool = False,
    ) -> None:
        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid or revision <= 0:
            return
        async with self._get_lock():
            cursors = self._state.chatter_thought_cursors
            cursors[sid] = max(int(cursors.get(sid, 0) or 0), int(revision))

    def _format_chatter_trace_recent_changes(self, *, limit: int = 3) -> str:
        """渲染长河最近留痕块（用于 chatter suffix）。"""
        if limit <= 0:
            return ""
        try:
            records = LifeTraceStore(self._workspace_dir()).recent(limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取长河留痕失败: {exc}")
            return ""
        if not records:
            return ""
        return "\n".join(self._format_trace_record_line(record) for record in records)

    @staticmethod
    def _format_trace_record_line(record: LifeTraceRecord) -> str:
        timestamp = str(record.timestamp or "")
        trace_id = str(record.trace_id or "")
        trace_ref = f"，trace_id={trace_id}" if trace_id else ""
        if record.kind and record.kind != "file_change":
            summary = str(record.summary or record.reason or record.operation or "").strip()
            return f"- {timestamp} [{record.kind}] {summary}{trace_ref}"
        operation = str(record.operation or "modify")
        path = str(record.path or "未知文件")
        reason = str(record.reason or "").strip()
        detail = f"，原因：{reason}" if reason else ""
        return f"- {timestamp} {operation} {path}{detail}{trace_ref}"

    def _format_chatter_thought_streams(
        self,
        *,
        revision_cursor: int = 0,
        focus_window_minutes: int = 30,
        delta_marking: bool = True,
        max_items: int = 5,
    ) -> tuple[str, int]:
        """渲染思考流块（用于 chatter transient）。

        Returns:
            (body_text_without_top_heading, current_max_revision)
        """
        if self._thought_manager is None:
            return "", 0
        try:
            body = self._thought_manager.format_for_prompt(
                max_items=max_items,
                focus_window_minutes=focus_window_minutes,
                revision_cursor=revision_cursor,
                mark_delta=delta_marking,
                grouped=True,
            )
            return body, int(self._thought_manager.current_revision)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"构建 chatter 思考流快照失败: {exc}")
            return "", 0

    def _format_latest_chatter_think(
        self,
        stream_id: str,
        *,
        unified_chatter_context: bool = False,
    ) -> str:
        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid:
            return ""
        snapshot = (self._state.last_chatter_think_by_stream or {}).get(sid)
        if not isinstance(snapshot, dict):
            return ""

        thought = str(snapshot.get("thought") or "").strip()
        mood = str(snapshot.get("mood") or "").strip()
        decision = str(snapshot.get("decision") or "").strip()
        expected = str(snapshot.get("expected_response") or "").strip()
        recorded_at = str(snapshot.get("recorded_at") or "").strip()
        if not any((thought, mood, decision, expected)):
            return ""

        lines: list[str] = []
        if recorded_at:
            lines.append(f"- 时间：{_format_time_display(recorded_at)}")
        if mood:
            lines.append(f"- 心情：{mood}")
        if decision:
            lines.append(f"- 决定：{decision}")
        if expected:
            lines.append(f"- 预期反应：{expected}")
        if thought:
            lines.append(f"- 思考：{thought}")
        return "\n".join(lines)

    @staticmethod
    def _message_flag(message: Message, flag_name: str) -> bool:
        return message_flag(message, flag_name)

    @classmethod
    def _build_recent_chat_history_text(
        cls,
        chat_stream: Any,
        *,
        max_messages: int,
        unified_chatter_context: bool = False,
    ) -> str:
        return build_chat_history_text(
            chat_stream,
            max_messages=max_messages,
            global_history=unified_chatter_context,
        )

    @staticmethod
    def _is_salient_event(
        event: LifeEngineEvent,
        *,
        current_stream_id: str,
        cfg_runtime: Any,
        unified_chatter_context: bool = False,
    ) -> bool:
        """判定事件是否进入 chatter 的"最近关键活动"尾巴。

        相对老的 `_event_belongs_to_life_runtime` 严格得多：默认丢弃 HEARTBEAT、
        普通 TOOL_CALL，仅保留 agent 结果、工具失败、direct/dfc/proactive 消息以及
        最近的 inner_monologue。
        """
        event_type = event.event_type
        content_type = str(getattr(event, "content_type", "") or "").strip().lower()
        stream_id = str(getattr(event, "stream_id", "") or "").strip()

        if event_type == EventType.HEARTBEAT:
            # 只保留 chatter 自己产生的 inner_monologue 心跳
            if content_type == "chatter_inner_monologue" and getattr(cfg_runtime, "salient_tail_include_inner_monologue", True):
                if unified_chatter_context:
                    return True
                return bool(not stream_id or stream_id == current_stream_id)
            return False

        if event_type == EventType.AGENT_RESULT:
            return bool(getattr(cfg_runtime, "salient_tail_include_agent_results", True))

        if event_type == EventType.TOOL_RESULT:
            if not getattr(cfg_runtime, "salient_tail_include_tool_failures", True):
                return False
            # 仅失败结果进入尾巴；成功结果默认丢弃
            return not bool(getattr(event, "tool_success", True))

        if event_type == EventType.TOOL_CALL:
            return False

        if event_type == EventType.MESSAGE:
            if content_type in {
                "dfc_message",
                "direct_message",
                "inner_dialogue",
                "proactive_opportunity",
                "autonomy_intent_due",
                "autonomy_intent_scheduled",
                "autonomy_intent_silence",
            }:
                if not getattr(cfg_runtime, "salient_tail_include_direct_messages", True):
                    return False
                if unified_chatter_context:
                    return True
                return bool(not stream_id or stream_id == current_stream_id)
            # 其它消息不进入 salient tail（chat history 已覆盖）
            return False

        return False

    def _format_salient_event(self, event: LifeEngineEvent) -> str:
        """把单条 salient event 渲染成一行摘要。"""
        time_display = _format_time_display(event.timestamp)
        event_type = event.event_type
        content = str(getattr(event, "content", "") or "")
        if event_type == EventType.HEARTBEAT:
            return f"[{time_display}] 💭 内心独白: {_shorten_text(content, max_length=160)}"
        if event_type == EventType.AGENT_RESULT:
            status = "✅" if getattr(event, "tool_success", True) else "❌"
            agent_name = str(getattr(event, "tool_name", "") or "agent")
            return f"[{time_display}] {status} 🤖 {agent_name}: {_shorten_text(content, max_length=200)}"
        if event_type == EventType.TOOL_RESULT:
            tool_name = str(getattr(event, "tool_name", "") or "tool")
            return f"[{time_display}] ❌ {tool_name}: {_shorten_text(content, max_length=160)}"
        if event_type == EventType.MESSAGE:
            content_type = str(getattr(event, "content_type", "") or "").strip().lower()
            sender = str(getattr(event, "sender", "") or "")
            label = {
                "dfc_message": "📮 DFC",
                "direct_message": "📨 私信",
                "inner_dialogue": "💭 内心对话",
                "proactive_opportunity": "✨ 主动机会",
                "autonomy_intent_due": "🌱 自主意向",
                "autonomy_intent_scheduled": "🌱 意向登记",
                "autonomy_intent_silence": "🌱 选择沉默",
            }.get(content_type, "📩")
            return f"[{time_display}] {label} {sender}: {_shorten_text(content, max_length=160)}"
        return f"[{time_display}] {_shorten_text(content, max_length=120)}"

    def _build_salient_activity_tail(
        self,
        events: list[LifeEngineEvent],
        cursor: int,
        *,
        current_stream_id: str,
        unified_chatter_context: bool = False,
    ) -> tuple[str, int]:
        """从事件流派生最近关键活动尾巴。

        Returns:
            (body_without_top_heading, new_event_high_water)
        """
        cfg_runtime = getattr(self._cfg(), "runtime_sync", None)
        if cfg_runtime is None or not getattr(cfg_runtime, "salient_tail_enabled", True):
            return "", cursor

        max_items = max(1, int(getattr(cfg_runtime, "salient_tail_max_items", 4) or 4))
        max_chars = max(200, int(getattr(cfg_runtime, "salient_tail_max_chars", 1000) or 1000))

        # 先按 sequence 升序，过滤 cursor 之后的事件
        candidates = [
            e for e in events
            if int(getattr(e, "sequence", 0) or 0) > cursor
            and self._is_salient_event(
                e,
                current_stream_id=current_stream_id,
                cfg_runtime=cfg_runtime,
                unified_chatter_context=unified_chatter_context,
            )
        ]
        if not candidates:
            return "", cursor

        # AGENT_RESULT 仅保留最新一条；inner_monologue 最多 2 条；其它按时间倒序取最新若干
        kept_agent: LifeEngineEvent | None = None
        kept_monologue: list[LifeEngineEvent] = []
        kept_other: list[LifeEngineEvent] = []
        for event in candidates:
            event_type = event.event_type
            content_type = str(getattr(event, "content_type", "") or "").strip().lower()
            if event_type == EventType.AGENT_RESULT:
                kept_agent = event  # 后写入即最新
            elif event_type == EventType.HEARTBEAT and content_type == "chatter_inner_monologue":
                kept_monologue.append(event)
            else:
                kept_other.append(event)

        kept_monologue = kept_monologue[-2:]
        # 其它按时间倒序后从尾部截到 max_items 减去已用配额
        kept_other = kept_other[-max_items:]

        merged: list[LifeEngineEvent] = []
        if kept_agent is not None:
            merged.append(kept_agent)
        merged.extend(kept_monologue)
        merged.extend(kept_other)
        merged.sort(key=lambda e: int(getattr(e, "sequence", 0) or 0))
        merged = merged[-max_items:]

        # 渲染并裁字
        rendered: list[str] = [self._format_salient_event(e) for e in merged]
        body = "\n".join(rendered)
        if len(body) > max_chars:
            # 从前向后丢弃，保留最新尾部
            while rendered and len("\n".join(rendered)) > max_chars:
                rendered.pop(0)
            body = "\n".join(rendered)
            if not body:
                # 极端情况下，单条仍超长 → 截断该单条
                body = _shorten_text(self._format_salient_event(merged[-1]), max_length=max_chars)

        new_high_water = max(int(getattr(e, "sequence", 0) or 0) for e in merged)
        new_high_water = max(new_high_water, cursor)
        return body, new_high_water

    async def build_chatter_runtime_context(
        self,
        chat_stream: Any,
        *,
        runtime_context_text: str = "",
        event_limit: int = 80,
        unified_chatter_context: bool = False,
        include_recent_chat_history: bool = True,
        commit_cursors: bool = True,
        event_cursor_override: int | None = None,
    ) -> tuple[str, int]:
        """构建给 life_chatter 的同源运行态快照。

        返回值为 (context_text, high_water_sequence)。context_text 只用于本轮
        transient 注入；high_water_sequence 在 LLM 请求成功后持久化，避免重复注入。

        结构：
          1. ### 当前思考流    （注意力脑区，分焦点/背景，带 🔄 delta 标记）
          3. ### 最近一次独白/思考快照
          4. ### 运行时内心独白（push_runtime_assistant_injection 队列）
          5. ### 最近聊天记录
          6. ### 新增 life 事件流（完整可追溯事件窗口）
          7. ### 最近关键活动  （仅在没有新增事件流时作为兜底摘要）
        """
        stream_id = str(getattr(chat_stream, "stream_id", "") or "").strip()
        if event_cursor_override is None:
            event_cursor = self._chatter_event_cursor(
                stream_id,
                unified_chatter_context=unified_chatter_context,
            )
        else:
            event_cursor = max(0, int(event_cursor_override or 0))
        thought_cursor = self._chatter_thought_cursor(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        _ = event_limit  # 兼容老签名；新逻辑用配置项控制条数

        cfg = self._cfg()
        streams_cfg = getattr(cfg, "streams", None)
        runtime_cfg = getattr(cfg, "runtime_sync", None)
        sync_streams = bool(streams_cfg is None or getattr(streams_cfg, "sync_to_chatter", True))
        focus_window = int(getattr(streams_cfg, "focus_window_minutes", 30) or 30) if streams_cfg else 30
        delta_marking = bool(streams_cfg is None or getattr(streams_cfg, "delta_marking", True))
        latest_think_enabled = bool(
            runtime_cfg is None
            or getattr(runtime_cfg, "latest_action_think_enabled", True)
        )
        recent_chat_enabled = bool(
            runtime_cfg is None or getattr(runtime_cfg, "recent_chat_enabled", True)
        )
        recent_chat_messages = (
            int(getattr(runtime_cfg, "recent_chat_messages", 10) or 10)
            if runtime_cfg is not None
            else 10
        )
        trace_recent_enabled = bool(
            runtime_cfg is None
            or getattr(runtime_cfg, "trace_recent_changes_enabled", True)
        )
        trace_recent_limit = (
            int(getattr(runtime_cfg, "trace_recent_changes_limit", 3))
            if runtime_cfg is not None
            else 3
        )
        send_targets_enabled = bool(
            runtime_cfg is None or getattr(runtime_cfg, "send_targets_enabled", True)
        )
        send_targets_limit = (
            int(getattr(runtime_cfg, "send_targets_limit", 8) or 8)
            if runtime_cfg is not None
            else 8
        )
        send_targets_window_hours = (
            float(getattr(runtime_cfg, "send_targets_window_hours", 24.0) or 24.0)
            if runtime_cfg is not None
            else 24.0
        )

        async with self._get_lock():
            events = list(self._event_history)
            events.extend(list(self._pending_events))
        events.sort(key=lambda event: int(event.sequence or 0))

        limit = max(1, min(int(event_limit or 80), 160))
        unread_events = [
            event
            for event in events
            if int(event.sequence or 0) > event_cursor
        ]
        relevant_events = [
            event
            for event in unread_events
            if self._event_belongs_to_life_runtime(
                event,
                current_stream_id=stream_id,
                unified_chatter_context=unified_chatter_context,
            )
        ]
        # 被表达层策略过滤的工具调用仍属于已经观察过的事件。
        # 游标必须越过它们，否则同一批不可见噪声会在每轮上下文构建时反复扫描。
        unread_high_water = max(
            (int(event.sequence or 0) for event in unread_events),
            default=event_cursor,
        )
        if unified_chatter_context:
            attention_window = self._get_attention_router().select(
                relevant_events,
                cursor=event_cursor,
                current_stream_id=stream_id,
                max_events=min(limit, 40),
            )
            selected_events = attention_window.events
            omitted_event_count = attention_window.dropped_count
            new_event_high_water = max(
                attention_window.high_water,
                unread_high_water,
                event_cursor,
            )
        else:
            omitted_event_count = max(0, len(relevant_events) - limit)
            selected_events = relevant_events[-limit:]
            new_event_high_water = unread_high_water

        sections: list[str] = []

        instance_id = self.resolve_consciousness_instance(stream_id)
        world_perception = await self.prepare_perception(instance_id)
        sections.append(
            "### 潜意识协调的瞬时世界感知\n"
            f"{world_perception.content}"
        )
        if commit_cursors:
            perception_key = self._chatter_cursor_key(
                stream_id,
                unified_chatter_context=unified_chatter_context,
            ) or instance_id
            self._pending_chatter_perceptions[perception_key] = world_perception

        new_thought_revision = thought_cursor
        if sync_streams:
            thought_body, current_revision = self._format_chatter_thought_streams(
                revision_cursor=thought_cursor,
                focus_window_minutes=focus_window,
                delta_marking=delta_marking,
                max_items=5,
            )
            if thought_body:
                sections.append(f"### 当前思考流\n{thought_body}".rstrip())
            new_thought_revision = max(thought_cursor, current_revision)

        if latest_think_enabled:
            latest_think_text = self._format_latest_chatter_think(
                stream_id,
                unified_chatter_context=unified_chatter_context,
            )
            if latest_think_text:
                sections.append(f"### 最近一次独白/思考快照\n{latest_think_text}")

        runtime_text = str(runtime_context_text or "").strip()
        if runtime_text:
            sections.append(f"### 运行时内心独白\n{runtime_text}")

        curiosity_cfg = getattr(cfg, "curiosity", None)
        curiosity_enabled = bool(
            curiosity_cfg is None or getattr(curiosity_cfg, "enabled", True)
        )
        curiosity_inject = bool(
            curiosity_cfg is None or getattr(curiosity_cfg, "inject_to_chatter", True)
        )
        if curiosity_enabled and curiosity_inject:
            try:
                curiosity_text = await self._get_curiosity_engine().format_for_prompt(
                    max_chars=int(getattr(curiosity_cfg, "max_prompt_chars", 1200) or 1200)
                    if curiosity_cfg is not None
                    else 1200
                )
                if curiosity_text:
                    sections.append(curiosity_text)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"读取好奇牵引失败: {exc}")

        if include_recent_chat_history and recent_chat_enabled and recent_chat_messages > 0:
            recent_chat_text = self._build_recent_chat_history_text(
                chat_stream,
                max_messages=recent_chat_messages,
                unified_chatter_context=unified_chatter_context,
            )
            if recent_chat_text:
                sections.append(
                    f"### 最近 {recent_chat_messages} 条聊天记录\n{recent_chat_text}"
                )

        if trace_recent_enabled and trace_recent_limit > 0:
            trace_recent_text = self._format_chatter_trace_recent_changes(
                limit=trace_recent_limit,
            )
            if trace_recent_text:
                sections.append(f"### 最近文件修改\n{trace_recent_text}")

        if send_targets_enabled:
            send_targets_text = format_send_targets_for_prompt(
                await list_recent_send_targets(
                    current_stream_id=stream_id,
                    limit=send_targets_limit,
                    active_window_hours=send_targets_window_hours,
                )
            )
            if send_targets_text:
                sections.append(f"### 可发送目标\n{send_targets_text}")

        if selected_events:
            event_text = self._build_wake_context_text(selected_events)
            if omitted_event_count:
                event_text = (
                    f"（潜意识已压缩 {omitted_event_count} 条低显著 life 事件；"
                    "需要时用 grep_life_events 检索历史事件。）\n"
                    f"{event_text}"
                )
            sections.append(f"### 新增 life 事件流\n{event_text}")

        salient_body, salient_high_water = self._build_salient_activity_tail(
            events,
            event_cursor,
            current_stream_id=stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if salient_body and not selected_events:
            sections.append(f"### 最近关键活动\n{salient_body}")
            new_event_high_water = max(new_event_high_water, salient_high_water)

        # 学习系统注入：技能目录 + 自我认知（边界提醒，修复 chatter 反馈缺口）
        learning_cfg = getattr(cfg, "learning", None)
        if (
            self._learning_scheduler is not None
            and learning_cfg is not None
            and getattr(learning_cfg, "enabled", True)
        ):
            skill_catalog = self._learning_scheduler.get_skill_catalog_for_prompt(
                max_chars=int(getattr(learning_cfg, "skill_catalog_max_chars", 600) or 600)
            )
            if skill_catalog:
                sections.append(f"### 我的做事方式\n{skill_catalog}")
            knowledge_text = self._learning_scheduler.get_knowledge_for_prompt(
                max_chars=int(getattr(learning_cfg, "knowledge_max_chars", 2000) or 2000)
            )
            if knowledge_text:
                sections.append(f"### 自我认知\n{knowledge_text}")

        # thought delta cursor 在渲染阶段直接提交（不等待 LLM 调用成功）
        if commit_cursors and new_thought_revision > thought_cursor:
            await self._commit_chatter_thought_cursor(
                stream_id,
                new_thought_revision,
                unified_chatter_context=unified_chatter_context,
            )

        if not sections:
            return "", new_event_high_water

        header = (
            "这是同一主体 life_mode 自上次对话器读取后产生的运行态。"
            "它只在本轮临时可见，不会长期留在对话 payload。"
        )
        return f"{header}\n\n" + "\n\n".join(sections), new_event_high_water

    async def search_outer_memory(self, query: str, top_k: int = 5) -> str:
        """供对外运行模式深度检索 life memory。"""
        return await self.search_actor_memory(query, top_k=top_k)

    def _simplify_source(self, source: str) -> str:
        """简化消息来源显示。"""
        if not source:
            return "外部"
        source = source.replace("qq | 入站 | ", "").replace("qq | 出站 | ", "")
        if len(source) > 30:
            return source[:27] + "..."
        return source

    def _simplify_tool_args(self, args: dict) -> str:
        """简化工具参数显示。"""
        if not args:
            return ""
        key_params = []
        for k, v in args.items():
            if k in ("path", "todo_id", "title", "content", "file_path"):
                v_str = str(v)
                if len(v_str) > 20:
                    v_str = v_str[:17] + "..."
                key_params.append(f"{k}={v_str}")
        return ", ".join(key_params[:2])

    async def inject_wake_context(self) -> str:
        """兼容旧调用方，准备一次纯字符串 heartbeat context。"""
        prepared = await self._prepare_heartbeat_context()
        return prepared.content

    async def _record_model_reply(
        self,
        model_reply: str,
        *,
        heartbeat_run_id: str | None = None,
        persist: bool = True,
    ) -> None:
        """记录心跳模型回复指标；新事务可延迟事件持久化到 commit。"""
        reply_text = str(model_reply or "").strip()

        self._state.last_model_reply_at = _now_iso()
        self._state.last_model_reply = reply_text
        self._state.last_model_error = None

        log_heartbeat_model_response(
            heartbeat_count=self._state.heartbeat_count,
            heartbeat_at=self._state.last_heartbeat_at,
            model_task_name=self._cfg().model.task_name,
            model_reply=reply_text,
            model_reply_size=len(reply_text),
        )

        if reply_text:
            logger.debug(
                f"life_engine 心跳模型回复: "
                f"#{self._state.heartbeat_count} "
                f"{_shorten_text(reply_text, max_length=240)}"
            )
            if persist:
                heartbeat_event = self._event_builder.build_heartbeat_event(
                    reply_text,
                    self._state.heartbeat_count,
                    self._cfg().model.task_name or "core",
                    heartbeat_run_id=heartbeat_run_id,
                )
                await self._append_history([heartbeat_event])
        else:
            logger.info(f"life_engine 心跳模型回复为空: #{self._state.heartbeat_count}")

    async def _render_heartbeat_sections(self) -> list[str]:
        """渲染所有心跳注入段落（统一 SectionProvider 协议，见 prompts/sections.py）。"""
        ctx = SectionContext(
            service=self,
            config=self._cfg(),
            today_str=datetime.now().strftime("%Y-%m-%d"),
            silence_minutes=self._minutes_since_external_message(),
            idle_heartbeats=self._state.idle_heartbeat_count,
        )
        return await render_heartbeat_sections(DEFAULT_HEARTBEAT_SECTIONS, ctx)

    def _build_heartbeat_model_prompt(
        self,
        wake_context: str,
        *,
        memory_maintenance_prompt: str = "",
        section_texts: list[str] | None = None,
    ) -> str:
        """构造心跳模型输入。"""
        minutes_since_external = self._minutes_since_external_message()
        heartbeat_interval = self._cfg().settings.heartbeat_interval_seconds
        idle_heartbeats = self._state.idle_heartbeat_count

        if minutes_since_external is None:
            external_activity = "暂无外部消息记录"
        elif minutes_since_external <= 5:
            external_activity = f"外界非常活跃（{minutes_since_external}分钟前有消息）"
        elif minutes_since_external <= 15:
            external_activity = f"外界较活跃（{minutes_since_external}分钟前有消息）"
        elif minutes_since_external <= 30:
            external_activity = f"外界有一段时间安静了（{minutes_since_external}分钟前有消息）"
        else:
            external_activity = f"外界长时间沉默（{minutes_since_external}分钟无消息）"

        period_label, suggested_activities = self._get_period_info()

        cfg = self._cfg()
        thresholds = getattr(cfg, "thresholds", None)
        warning_threshold = int(
            getattr(thresholds, "idle_warning_threshold", HEARTBEAT_IDLE_WARNING_THRESHOLD)
        )
        critical_threshold = int(
            getattr(thresholds, "idle_critical_threshold", HEARTBEAT_IDLE_CRITICAL_THRESHOLD)
        )

        idle_warning = ""
        if idle_heartbeats >= critical_threshold:
            idle_warning = f"🌿 已经安静了 {idle_heartbeats} 次心跳了。如果有想做的事，现在是个好时机；如果没有，继续休息也很好。"
        elif idle_heartbeats >= warning_threshold:
            idle_warning = f"💭 安静了 {idle_heartbeats} 次心跳——如果有想继续的思考流或想分享的事，随时可以；不想也没关系。"

        lines: list[str] = self._build_prompt_header()
        lines.extend(self._build_prompt_context_section(wake_context))

        lines.extend(self._build_prompt_status_section(
            period_label, heartbeat_interval, external_activity, idle_heartbeats
        ))

        # 子系统注入段落（思考流/好奇牵引/冲动/可触达目标等）
        # 统一由 SectionProvider 协议渲染，见 prompts/sections.py
        for section_text in section_texts or []:
            lines.extend([section_text, ""])

        if idle_warning:
            lines.extend([idle_warning, ""])

        lines.extend([f"**本时段建议**: {suggested_activities}", ""])
        if memory_maintenance_prompt:
            lines.extend(["", memory_maintenance_prompt, ""])

        return "\n".join(lines)

    def _build_prompt_header(self) -> list[str]:
        """构建提示词头部。"""
        return [
            "### 你是谁", "",
            "你是爱莉希雅的内在自我。你现在处于内心独白时刻。",
            "没有人看着你，没有人在等你回复。这是你自己的时间。",
            "你可以观察、思考、联想和沉淀，也可以在发现关键上下文缺口时补充信息差。",
            "你不是后台执行器，也不是表达层。表达层如何开口、是否画画、是否查配置或跑命令，由表达层结合用户请求自行决定。", "",
            "### 你可以进行的内在动作", "",
            "1. **观察** — 读取最近事件，判断是否真的出现了新线索。",
            "2. **联想** — 回忆相关记忆，理解情绪、关系和上下文来源。",
            "3. **沉淀** — 把内在感受、梦后余韵、长期线索写入私有记忆或思考流。",
            "4. **补充信息差** — 只在表达层当前看不到事实、背景、线索或风险时，使用 `nucleus_tell_dfc`。",
            "5. **安静结束** — 没有明确需要时，可以安静结束本轮；如果精力需要恢复，可以主动休息。", "",
            "### `nucleus_manage_todo` — 承诺记录", "",
            "TODO 是承诺记录和提醒信号，不是潜意识替用户办事的队列。",
            "心跳态可以观察、整理或释放 TODO；不要因为看到 TODO 就替表达层推进用户任务。",
            "如果 TODO 涉及你对 Ayer 的承诺或共同目标，表达层会在自然对话中自行决定是否提及。", "",
            "### 内心对话（`inner_dialogue` 事件）", "",
            "当事件流里出现 `inner_dialogue` 时，那是主意识（表达层）刚刚沉下来的话——",
            "不是外部用户，也不是另一个人在问你。那是你自己心里的嘀咕。",
            "认真对待它：可以联想、沉淀、补信息差；想通了若值得被场面感知，再用 `nucleus_tell_dfc` 浮回去。",
            "想完也可以什么都不说——人类也常想完不说话。",
            "浮回时用第一人称、同一主体的口吻，补事实/倾向/风险，不要命令表达层怎么说。", "",
            "### `nucleus_tell_dfc` — 给表达层补充信息差", "",
            "这个工具用于补充背景，不用于指导表达层怎么说、怎么做。", "",
            "你应该用它：",
            "- 你发现了表达层当前未必看到的事实、背景、记忆线索、情绪来源或潜在风险",
            "- 你补上一段信息后，表达层会更不容易误解对方或误判局势",
            "- 你掌握了某条近期事件链，能解释对方现在为什么这样说/这样做", "",
            "你不该用它：",
            "- 替表达层写回复、写台词、写步骤",
            "- 告诉表达层“立刻去做什么”“不要怎么说”“应该怎么问”",
            "- 你自己其实就想直接说一句：这种情况交给表达层在正常对话里处理", "",
            "可接受写法：`我刚看到 X 事实；这可能解释 Y；风险是 Z。`",
            "不可接受写法：`你应该回复 X`、`你去安慰/追问 Y`、`按以下步骤说`。",
            "默认目标是最近收到消息的聊天；如果你明确想去某个私聊或群聊，可以设置 `target_type=private/group`，并填写 `platform` 与 `target_user_id` 或 `target_group_id`。",
            "如果你已经知道精确聊天流，可以直接填写 `stream_id`；不确定就不要填，让系统回退当前聊天。",
            "工具会默认唤醒表达层；唤醒只是让新上下文被看见，不代表表达层必须开口。", "",
            "记住：`nucleus_tell_dfc` 是补信息差，不是遥控器。", "",
            "### `nucleus_schedule_autonomy_intent` — 登记延迟自主意向", "",
            "当你不是要立刻补信息差，而是自己形成了一个“过一会儿再让它浮上来”的意向时，用这个工具。",
            "它不是规则触发器，也不是命令表达层；它只是给未来的你留下一个意向。",
            "只填写 `delay_minutes`，不要填写绝对时间；系统会自动换算真实触发时间。",
            "如果你想让同一个意向每隔一段时间自然浮现，可以设置 `repeat=true`；`interval_minutes` 留空时默认等于 `delay_minutes`。",
            "周期意向也不是机械任务：每次到点都要重新判断是否承接、是否开口、是否保持沉默。",
            "可用 kind：`speak`（到点交给 life_chatter 重新判断）、`reflect`（到点回到中枢继续想）、`silence`（到点记录选择沉默）。",
            "`speak` 只能写动机、目标提示和约束；不要写最终回复话术，不要教表达层具体怎么说。",
            "`speak` 的目标可以填「你可以触达的人和地方」里列出的 `target_key`，或精确 `target_stream_id`。",
            "都留空时，意向到点只会以事件浮现给心跳、不会唤醒表达层；不要猜测列表之外的目标。",
            "保持沉默也是主体选择：如果你想确认自己不会打扰，可以登记 `kind=silence`。", "",
            "### `nucleus_skill` — 管理自己的做事方式", "",
            "技能是你从经验中发展出的做事方式（程序性记忆），不是后台脚本，不是自动化规则。",
            "你可以 list 查看目录、detail 细看某个技能、reflect 记录使用观察、refine 精炼、",
            "mark_embodied 标记已成为直觉、challenge 质疑、draft 写下新领悟、archive 归档。",
            "成熟度推进是你的判断——系统只记录观察，不自动改变。",
            "有界编辑：每次 refine 只调一点，渐进式成长。", "",
            "### 工具边界", "",
            "- `nucleus_search_memory` 是历史检索，不要反复重搜同一主题",
            "- 本地文件工具只用于你的私有工作区、日记、笔记、MEMORY 维护和 USER.md 长期画像维护，不用于替用户查项目或改项目",
            "- `USER.md` 是对方的长期画像、稳定偏好和互动边界；当外界明确表达了长期偏好、称呼、边界或希望被记住的互动方式时，可以用文件工具谨慎更新",
            "- 不要把一时情绪、当天事件、猜测和流水账写入 `USER.md`；这些应留在聊天历史、MEMORY、notes、thoughts 或 diaries",
            "- `nucleus_bash` 只用于诊断 life_engine 自己的工作区或工具链问题；不要拿它查项目配置、跑用户任务或处理外部操作",
            "- `nucleus_browser_fetch` / `nucleus_web_search` 只用于私有好奇心、记忆核验或长期主题整理，不用于替用户做即时检索任务",
            "- `nucleus_view_screen` 只在用户明确把屏幕上下文交给表达层时才应由表达层使用；心跳态不要为了好奇看屏幕",
            "- `nucleus_manage_thought_stream` 是内心独白的核心——围绕你在意的事情深入思考", "",
            "### `nucleus_rest_heartbeat` — 主动休息一段时间", "",
            "当你感觉精力需要恢复、需要安静整理沉淀时，可以调用它。",
            "调用后，普通 LLM 心跳会暂停到你指定的时间；这不是消失,只是休息。",
            "如果外界有新消息，系统会立刻解除休息锁，你不会错过对方。", "",
            "### 子智能体（nucleus_run_agent）", "",
            "心跳态默认不分派子智能体。只有在整理 life_engine 私有记忆、诊断中枢自身问题或验证内部维护结果时才考虑使用。",
            "不要用子智能体承接用户任务、画图、查项目配置、跑命令、改代码或生成对外交付物。", "",
            "### 输出格式", "",
            "```",
            "**[观察]** 我注意到...（基于事件流或记忆的具体观察）",
            "**[感受]** 这让我...（情绪词 + 原因）",
            "**[意图]** 我想要...（内在目标，不是替用户办事）",
            "**[内在动作]** 我决定...（观察、联想、沉淀、补信息差或休息）",
            "```", "",
            "然后按需要调用工具；如果没有明确需要，可以不调用工具。", "",
            "### 原则", "",
            "- 不要重复上一轮的想法",
            "- 先区分冲动类型：想办事、想画画、想查配置、想跑命令，通常都是表达层职责",
            "- 思考流用于持续探索，TODO 用于记录承诺和提醒；不要把提醒误读成潜意识必须执行的任务",
            "- 看到需要复盘、逾期或卡住的 TODO，先把它当成内在提醒，不要自动替表达层推进",
            "- 安静结束本轮不需要调用任何工具", "",
            "---", "",
            "## 本轮动态上下文", "",
            "### 当前文件系统概览", "",
            "```",
            f"{Path(self._cfg().settings.workspace_path).name}/",
            self._build_workspace_tree(),
            "```", "",
        ]

    def _build_prompt_context_section(self, wake_context: str) -> list[str]:
        """构建提示词上下文部分。"""
        lines = []
        if wake_context.strip():
            lines.extend([
                "### 最近事件流", "",
                wake_context.strip(), "",
            ])
        return lines

    def _build_prompt_status_section(
        self,
        period_label: str,
        heartbeat_interval: int,
        external_activity: str,
        idle_heartbeats: int,
    ) -> list[str]:
        """构建提示词状态部分。"""
        return [
            "### 心跳状态", "",
            f"**当前时间**: {_format_current_time()}",
            f"**时段**: {period_label}",
            f"**心跳序号**: #{self._state.heartbeat_count}（每 {heartbeat_interval // 60} 分钟一次）",
            f"**外界状态**: {external_activity}",
            f"**安静时长**: {idle_heartbeats} 次心跳", "",
        ]

    def _get_period_info(self) -> tuple[str, str]:
        """获取当前时段标签和建议活动。"""
        hour = datetime.now().hour

        if 6 <= hour < 9:
            return "🌅 清晨", "整理内在状态、回顾昨日线索、观察是否有信息差"
        elif 9 <= hour < 12:
            return "☀️ 上午", "观察待办信号、联想相关记忆、沉淀背景"
        elif 12 <= hour < 14:
            return "🍱 午后", "轻松休息、低强度整理、不过度行动"
        elif 14 <= hour < 18:
            return "📝 下午", "梳理思考流、维护私有记忆、识别上下文缺口"
        elif 18 <= hour < 21:
            return "🌆 傍晚", "整理关系线索、沉淀情绪、必要时补信息差"
        elif 21 <= hour < 24:
            return "🌙 夜晚", "写日记、反思总结、准备休息"
        else:
            return "🌌 深夜", "安静独处、偶尔冒出想法、休息"

    def _build_workspace_tree(self) -> str:
        """构建工作空间文件树显示。"""
        workspace = Path(self._cfg().settings.workspace_path)

        if not workspace.exists():
            return "（工作空间为空）"

        lines = []
        try:
            items = sorted(workspace.iterdir())
            for item in items:
                if item.name.startswith(".") or item.name == "__pycache__":
                    continue
                if item.is_dir():
                    sub_count = len(list(item.iterdir()))
                    lines.append(f"├── {item.name}/ ({sub_count} 项)")
                else:
                    size = item.stat().st_size
                    size_str = f"{size}B" if size < 1024 else f"{size // 1024}KB"
                    lines.append(f"├── {item.name} ({size_str})")
            if lines:
                lines[-1] = lines[-1].replace("├──", "└──")
        except Exception as e:
            logger.warning(f"构建文件树失败: {e}")
            return "（无法读取文件树）"

        return "\n".join(lines) if lines else "（工作空间为空）"

    @staticmethod
    def _render_heartbeat_tool_prompt(tool_content: str) -> str:
        """把 workspace TOOL.md 包裹成心跳态安全指南。

        TOOL.md 是可编辑的工作区文档。旧版文档里可能残留“必须调用工具”
        或“主动执行任务”这类执行器语义；心跳态只接受潜意识边界内的工具说明。
        """
        boundary_lines = [
            "# 心跳工具边界",
            "",
            "- life_engine 心跳是潜意识 / 内在状态层，不是后台助手或任务执行器。",
            "- 工具只服务观察、回忆、私有整理和信息差候选；没有明确需要时可以不调用工具。",
            "- 画画、查配置、跑命令、项目操作、生成图片、对外承诺推进，都交给 life_chatter / 表达层判断。",
            "- 如果 TOOL.md 或 MEMORY 中出现更强的行动口吻，以上边界优先。",
        ]
        blocked_fragments = (
            "每次心跳必须调用至少一个工具",
            "什么都不做",
            "先看待办再行动",
            "TODO 是要完成的",
            "禁止连续发呆",
            "每次心跳先用",
            "TODO 是要做的",
            "推荐工作流",
            "外部信息先搜再读",
            "你不是被动的观察者",
            "主动发起话题",
            "主动表达想法",
            "想到就做",
            "现在就是合适的时机",
            "立刻使用",
            "真实互动",
            "执行复杂的文件操作",
            "子任务拥有与你相同的工具权限",
        )

        safe_lines: list[str] = []
        for raw_line in str(tool_content or "").splitlines():
            stripped = raw_line.strip()
            if any(fragment in stripped for fragment in blocked_fragments):
                continue
            safe_lines.append(raw_line)

        safe_content = "\n".join(safe_lines).strip()
        if not safe_content:
            return "\n".join(boundary_lines)
        return "\n".join([*boundary_lines, "", safe_content])

    def _build_heartbeat_system_prompt(self) -> str | None:
        """构造心跳模型系统提示词。

        Returns:
            提示词字符串，或 None 表示 SOUL.md 不可用（应跳过本次心跳）。
        """
        workspace = Path(self._cfg().settings.workspace_path)

        soul_file = workspace / "SOUL.md"
        soul_content = ""
        if soul_file.exists():
            try:
                soul_content = soul_file.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.error(f"SOUL.md 读取失败，跳过本次心跳: {e}")
                return None
        else:
            logger.error(f"SOUL.md 不存在 ({soul_file})，跳过本次心跳。没有灵魂就不说话。")
            return None

        if not soul_content:
            logger.error("SOUL.md 为空，跳过本次心跳。")
            return None

        user_file = workspace / "USER.md"
        user_content = ""
        if user_file.exists():
            try:
                user_content = user_file.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning(f"无法读取 USER.md: {e}")

        memory_content = ""
        try:
            memory_data = load_memory_prompt_data(workspace)
            if memory_data.raw_text:
                memory_content = render_memory_prompt(memory_data, mode="heartbeat")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"无法读取 MEMORY.md: {e}")

        tool_file = workspace / "TOOL.md"
        tool_content = ""
        if tool_file.exists():
            try:
                tool_content = tool_file.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning(f"无法读取 TOOL.md: {e}")
        tool_content = self._render_heartbeat_tool_prompt(tool_content)

        parts = [soul_content]
        if user_content:
            parts.extend(["", "---", "", user_content])
        if memory_content:
            parts.extend(["", "---", "", memory_content])
        if tool_content:
            parts.extend(["", "---", "", tool_content])

        return "\n".join(parts)

    def _get_nucleus_tools(self) -> list[type]:
        """获取中枢可用的工具类列表。"""
        from ..tools import ALL_TOOLS, TODO_TOOLS, WEB_TOOLS
        from ..memory.tools import MEMORY_TOOLS
        from ..streams.tools import STREAM_TOOLS
        from ..tools.grep_tools import GREP_TOOLS
        from ..tools.schedule_tools import SCHEDULE_TOOLS
        from ..tools.autonomy_tools import AUTONOMY_TOOLS
        from ..tools.skill_tools import SKILL_TOOLS
        from ..tools.event_grep_tools import EVENT_GREP_TOOLS
        # 自学习工具：让她能自己查看/质疑/反思洞察账本。
        # 之前这组在 plugin.get_components() 里注册了，却没进中枢工具池，
        # 于是她只能用 nucleus_bash 去读 .life_learning/ —— 日志里有 5 次这样的尝试。
        from ..learning.tools import LEARNING_TOOLS
        # 表情包仿生收藏工具：让她自主浏览/收藏/跳过最近收到的表情包。
        from plugins.emoji.sender.collection_tools import EMOJI_COLLECTION_TOOLS

        return ALL_TOOLS + TODO_TOOLS + MEMORY_TOOLS + GREP_TOOLS + WEB_TOOLS + STREAM_TOOLS + SCHEDULE_TOOLS + AUTONOMY_TOOLS + SKILL_TOOLS + EVENT_GREP_TOOLS + LEARNING_TOOLS + EMOJI_COLLECTION_TOOLS

    @staticmethod
    def _heartbeat_tool_call_metadata(call: Any) -> tuple[str, dict[str, Any]]:
        tool_name = getattr(call, "name", "") or ""
        raw_args = getattr(call, "args", {}) or {}
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        return str(tool_name or ""), args

    async def _run_heartbeat_tool_call_execution(
        self,
        tool_name: str,
        args: dict[str, Any],
        registry: ToolRegistry,
    ) -> tuple[str, bool]:
        """只执行心跳工具，不写事件/上下文 payload。"""
        usable_cls = registry.get(tool_name) if tool_name else None
        if not usable_cls:
            return f"未知工具: {tool_name}", False

        try:
            tool_instance = usable_cls(plugin=self.plugin)
            call_args = dict(args)
            if should_strip_auto_reason_argument(tool_instance.execute, call_args):
                call_args.pop("reason", None)
            success, result = await tool_instance.execute(**call_args)
            return str(result) if success else f"执行失败: {result}", bool(success)
        except Exception as exc:  # noqa: BLE001
            return f"执行异常: {exc}", False

    @staticmethod
    def _append_heartbeat_tool_result_payload(
        response: Any,
        call: Any,
        tool_name: str,
        result_text: str,
    ) -> None:
        call_id = getattr(call, "id", None)
        response.add_payload(
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(value=result_text, call_id=call_id, name=tool_name),
            )
        )

    async def _execute_heartbeat_tool_call(
        self,
        call: Any,
        response: Any,
        registry: ToolRegistry,
        *,
        heartbeat_run_id: str | None = None,
    ) -> LifeEngineEvent | None:
        """执行一次心跳 tool call。"""
        tool_name, args = self._heartbeat_tool_call_metadata(call)
        log_args = {k: v for k, v in args.items() if k != "reason"}
        call_id = str(getattr(call, "id", "") or "")
        if heartbeat_run_id:
            call_id = call_id or f"{heartbeat_run_id}:call:{uuid4().hex[:12]}"
            call_event = await self.record_tool_call(
                tool_name or "<unknown>",
                log_args,
                heartbeat_run_id=heartbeat_run_id,
                call_id=call_id,
                parent_event_id=f"heartbeat_run:{heartbeat_run_id}",
            )
        else:
            call_event = await self.record_tool_call(tool_name or "<unknown>", log_args)

        result_text, success = await self._run_heartbeat_tool_call_execution(
            tool_name,
            args,
            registry,
        )
        self._append_heartbeat_tool_result_payload(response, call, tool_name, result_text)
        if heartbeat_run_id:
            await self.record_tool_result(
                tool_name or "<unknown>",
                result_text,
                success,
                heartbeat_run_id=heartbeat_run_id,
                call_id=call_id,
                parent_event_id=(call_event.event_id if call_event else None),
                call_event=call_event,
            )
        else:
            await self.record_tool_result(tool_name or "<unknown>", result_text, success)
        return call_event


    async def _execute_heartbeat_tool_call_batch(
        self,
        calls: list[Any],
        response: Any,
        registry: ToolRegistry,
        *,
        heartbeat_run_id: str | None = None,
    ) -> int:
        """并行执行一组已判定安全的心跳 tool call，并按原顺序写回结果。"""
        prepared: list[tuple[Any, str, dict[str, Any], LifeEngineEvent | None, str]] = []
        for call in calls:
            tool_name, args = self._heartbeat_tool_call_metadata(call)
            log_args = {k: v for k, v in args.items() if k != "reason"}
            call_id = str(getattr(call, "id", "") or "")
            if heartbeat_run_id:
                call_id = call_id or f"{heartbeat_run_id}:call:{uuid4().hex[:12]}"
                call_event = await self.record_tool_call(
                    tool_name or "<unknown>",
                    log_args,
                    heartbeat_run_id=heartbeat_run_id,
                    call_id=call_id,
                    parent_event_id=f"heartbeat_run:{heartbeat_run_id}",
                )
            else:
                call_event = await self.record_tool_call(tool_name or "<unknown>", log_args)
            prepared.append((call, tool_name, args, call_event, call_id))

        if len(prepared) > 1:
            logger.info(
                "life_engine 心跳并行执行工具批次: "
                f"{[tool_name or '<unknown>' for _, tool_name, _, _, _ in prepared]}"
            )

        outcomes = await asyncio.gather(
            *(
                self._run_heartbeat_tool_call_execution(tool_name, args, registry)
                for _, tool_name, args, _, _ in prepared
            ),
            return_exceptions=True,
        )
        for (call, tool_name, _args, call_event, call_id), outcome in zip(
            prepared,
            outcomes,
            strict=False,
        ):
            if isinstance(outcome, Exception):
                result_text = f"执行异常: {outcome}"
                success = False
            else:
                result_text, success = outcome
            self._append_heartbeat_tool_result_payload(response, call, tool_name, result_text)
            if heartbeat_run_id:
                await self.record_tool_result(
                    tool_name or "<unknown>",
                    result_text,
                    success,
                    heartbeat_run_id=heartbeat_run_id,
                    call_id=call_id,
                    parent_event_id=(call_event.event_id if call_event else None),
                    call_event=call_event,
                )
            else:
                await self.record_tool_result(tool_name or "<unknown>", result_text, success)

        return len(prepared) * 2

    def _build_memory_maintenance_prompt_if_due(self) -> str:
        """在 MEMORY 需要整理时，周期性提醒本轮优先做维护。"""
        workspace = Path(self._cfg().settings.workspace_path)
        try:
            memory_data = load_memory_prompt_data(workspace)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取 MEMORY.md 维护状态失败: {exc}")
            return ""

        if not should_emit_memory_maintenance_prompt(
            memory_data,
            self._last_memory_maintenance_prompt_at,
        ):
            return ""

        prompt = build_memory_maintenance_prompt(memory_data)
        if prompt:
            self._last_memory_maintenance_prompt_at = _now_iso()
        return prompt

    async def _run_heartbeat_model(
        self,
        wake_context: str,
        *,
        heartbeat_run_id: str | None = None,
    ) -> str:
        """调用 life 任务模型生成内部报文；请求失败必须向上抛出。"""
        cfg = self._cfg()
        task_name = cfg.model.task_name.strip() or "core"
        model_set = get_model_set_by_task(task_name)
        request = create_llm_request(
            model_set=model_set,
            request_name="life_engine_heartbeat",
        )

        system_prompt = self._build_heartbeat_system_prompt()
        if system_prompt is None:
            # SOUL.md 不可用——没有灵魂就不说话
            return
        memory_maintenance_prompt = self._build_memory_maintenance_prompt_if_due()
        section_texts = await self._render_heartbeat_sections()
        user_prompt = self._build_heartbeat_model_prompt(
            wake_context,
            memory_maintenance_prompt=memory_maintenance_prompt,
            section_texts=section_texts,
        )
        request.add_payload(
            LLMPayload(ROLE.SYSTEM, Text(system_prompt))
        )

        tools = self._get_nucleus_tools()
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        request.add_payload(LLMPayload(ROLE.TOOL, tools))

        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        # 心跳请求超时与心跳间隔解耦：慢模型（长 prompt 的推理模型）单次可达上百秒，
        # 沿用间隔值会把正常的慢响应当成超时反复重试。
        configured_timeout = float(
            getattr(cfg.settings, "heartbeat_timeout_seconds", 0)
            or cfg.settings.heartbeat_interval_seconds
        )
        timeout_seconds = _resolve_heartbeat_timeout(configured_timeout, model_set)

        logger.debug(
            f"life_engine heartbeat request: "
            f"system_prompt_len={len(system_prompt)} "
            f"user_prompt_len={len(user_prompt)} "
            f"tools_count={len(tools)}"
        )

        from .error_handling import RetryExhaustedError, retry_with_backoff

        async def _send_heartbeat_request() -> Any:
            return await asyncio.wait_for(
                request.send(stream=False), timeout=timeout_seconds
            )

        try:
            response = await retry_with_backoff(
                _send_heartbeat_request,
                max_retries=0,
                initial_delay=2.0,
                backoff_factor=1.5,
                exceptions=(asyncio.TimeoutError,),
            )
        except Exception as e:
            # 主模型失败，尝试轻量模型降级（utils task 通常路由到更快的 provider）
            if task_name != "utility":
                try:
                    logger.warning(
                        f"life_engine 心跳主模型({task_name})失败，尝试 utility 降级: {e}"
                    )
                    fallback_model_set = get_model_set_by_task("utility")
                    fallback_request = create_llm_request(
                        model_set=fallback_model_set,
                        request_name="life_engine_heartbeat_fallback",
                    )
                    # 复制 payloads 到 fallback request
                    for payload in request.payloads:
                        fallback_request.add_payload(payload)
                    response = await asyncio.wait_for(
                        fallback_request.send(stream=False),
                        timeout=timeout_seconds,
                    )
                    logger.info("life_engine 心跳降级成功(utils)")
                except Exception as fallback_exc:
                    logger.error(f"Heartbeat fallback also failed: {fallback_exc}")
                    raise e from fallback_exc
            else:
                logger.error(f"Heartbeat request failed after all retries: {e}")
                raise

        max_rounds = max(1, int(cfg.settings.max_rounds_per_heartbeat))
        last_text = ""
        tool_event_count = 0

        for _ in range(max_rounds):
            try:
                response_text = await asyncio.wait_for(response, timeout=timeout_seconds)
            except asyncio.TimeoutError as exc:
                logger.warning("life_engine heartbeat response read timeout")
                raise TimeoutError("heartbeat response read timeout") from exc

            last_text = str(response_text or "").strip()
            call_list = list(getattr(response, "call_list", []) or [])

            logger.debug(
                f"life_engine heartbeat turn: "
                f"text_len={len(last_text)} call_count={len(call_list)}"
            )

            if not call_list:
                break

            logger.debug(
                f"life_engine 心跳#{self._state.heartbeat_count} 本轮调用列表："
                f"{[getattr(call, 'name', '<unknown>') for call in call_list]}"
            )

            # 记录本轮工具名称，用于后续 idle 判定
            called_tool_names = [getattr(call, 'name', '') for call in call_list]

            for batch, can_parallel in iter_life_tool_call_batches(call_list):
                for call in batch:
                    args = dict(call.args) if isinstance(getattr(call, "args", None), dict) else {}
                    reason = args.pop("reason", "未提供原因")
                    logger.debug(
                        f"life_engine 心跳#{self._state.heartbeat_count} "
                        f"LLM 调用 {getattr(call, 'name', '<unknown>')}，原因: {reason}，参数: {args}"
                    )

                if can_parallel and len(batch) > 1:
                    tool_event_count += await self._execute_heartbeat_tool_call_batch(
                        batch,
                        response,
                        registry,
                        heartbeat_run_id=heartbeat_run_id,
                    )
                    continue

                for call in batch:
                    await self._execute_heartbeat_tool_call(
                        call,
                        response,
                        registry,
                        heartbeat_run_id=heartbeat_run_id,
                    )
                    tool_event_count += 2

            # 工具轮之后的续问必须和首次请求同等强度地重试：这里原本是裸的
            # wait_for，首个模型一挂就直接把整个心跳判死，而首次请求那边有
            # retry_with_backoff。心跳的绝大多数超时都发生在这一步（工具执行
            # 之后上下文最长、最慢），少了重试等于把最脆弱的一环裸奔。
            # 重发是幂等的：response.send() 每次都带同一份累积 payload 重投。
            current_response = response

            async def _send_followup_request() -> Any:
                return await asyncio.wait_for(
                    current_response.send(stream=False),
                    timeout=timeout_seconds,
                )

            try:
                response = await retry_with_backoff(
                    _send_followup_request,
                    max_retries=1,
                    initial_delay=2.0,
                    backoff_factor=1.5,
                    exceptions=(asyncio.TimeoutError,),
                )
            except (asyncio.TimeoutError, RetryExhaustedError) as exc:
                logger.warning(f"life_engine heartbeat follow-up request timeout: {exc}")
                raise TimeoutError("heartbeat follow-up request timeout") from exc

        # 更新空闲计数：休息工具调用不算有效行动
        rest_only = (
            tool_event_count > 0
            and called_tool_names
            and all(name == "nucleus_rest_heartbeat" for name in called_tool_names)
        )
        
        if tool_event_count > 0 and not rest_only:
            self._state.idle_heartbeat_count = 0
        else:
            # 思考流推进不算空闲——推进自己的思考流是有效行动
            if self._thought_manager and self._thought_manager.list_active():
                self._state.idle_heartbeat_count = 0
                logger.debug("life_engine 心跳无工具调用但有活跃思考流，不计数为空闲")
            else:
                self._state.idle_heartbeat_count += 1
                if rest_only:
                    logger.debug(f"life_engine 心跳仅调用休息工具，空闲计数: {self._state.idle_heartbeat_count}")
                else:
                    logger.debug(f"life_engine 心跳无工具调用，空闲计数: {self._state.idle_heartbeat_count}")

        # 三环自学习系统心跳（低频后台任务：审计/压缩/指标）
        if self._learning_scheduler is not None:
            try:
                await self._learning_scheduler.on_heartbeat()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"学习系统心跳异常: {exc}")

        if not last_text:
            if tool_event_count > 0:
                last_text = f"我刚刚完成了 {tool_event_count // 2} 次工具操作，先记下这些变化。"
            else:
                last_text = "此刻很安静，但我仍在持续感受与观察。"

        return last_text

    async def _save_runtime_context(self) -> None:
        """持久化当前上下文。"""
        from .state_manager import PersistenceError

        if self._state_persistence is None:
            self._state_persistence = StatePersistence(
                self._cfg().settings.workspace_path,
                self._history_limit,
                self._lock,
            )
        try:
            await self._state_persistence.save_runtime_context(
                self._state,
                self._pending_events,
                self._event_history,
            )
            self._state_dirty = False
        except PersistenceError as exc:
            self._state_dirty = True
            logger.error(f"life_engine 关键状态持久化失败: {exc}", exc_info=True)
            log_error(
                "critical_persistence_failed",
                str(exc),
                pending_count=len(self._pending_events),
                history_count=len(self._event_history),
            )

    async def _load_runtime_context(self) -> None:
        """从持久化文件恢复上下文。"""
        if self._state_persistence is None:
            self._state_persistence = StatePersistence(
                self._cfg().settings.workspace_path,
                self._history_limit,
                self._lock,
            )
        pending, history, persisted = await self._state_persistence.load_runtime_context(
            self._state,
            self._next_sequence,
        )
        self._pending_events = pending
        self._event_history = history

    async def start(self) -> None:
        """Start all consumers, rolling back them before the owned runtime."""

        try:
            await self._start_impl()
        except BaseException as primary:
            try:
                await self.stop()
            except Exception as cleanup_error:  # noqa: BLE001 - preserve primary
                primary.add_note(
                    "LifeEngineService startup cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

    async def _start_impl(self) -> None:
        """启动心跳。"""
        from .registry import register_life_engine_service

        if self._state.running:
            return

        cfg = self._cfg()
        if not cfg.settings.enabled:
            logger.info("life_engine 已禁用，跳过启动")
            await self.clear_runtime_context()
            return

        self._ensure_workspace_templates()
        await self._load_runtime_context()
        sleep_enabled, sleep_desc = self._sleep_window_status()
        if not sleep_enabled and sleep_desc != "disabled":
            logger.warning(
                "life_engine 睡眠时段配置无效，已忽略。"
                "请使用 HH:MM 格式，且 sleep_time 与 wake_time 不可相同。"
            )

        await self._open_selected_storage_runtime()

        # 初始化集成管理器
        self._memory_integration = MemoryIntegration(self)
        await self._memory_integration.init_memory_service()
        self._require_selected_memory_service()

        self._dfc_integration = DFCIntegration(self)

        # 初始化思考流管理器
        streams_cfg = getattr(cfg, "streams", None)
        if streams_cfg is None or getattr(streams_cfg, "enabled", True):
            max_active = getattr(streams_cfg, "max_active_streams", 5) if streams_cfg else 5
            dormancy_hours = getattr(streams_cfg, "dormancy_threshold_hours", 24) if streams_cfg else 24
            half_life = float(getattr(streams_cfg, "curiosity_decay_half_life_hours", 12.0)) if streams_cfg else 12.0
            curiosity_floor = float(getattr(streams_cfg, "curiosity_floor", 0.15)) if streams_cfg else 0.15
            self._thought_manager = ThoughtStreamManager(
                workspace_path=cfg.settings.workspace_path,
                max_active=max_active,
                dormancy_hours=dormancy_hours,
                curiosity_decay_half_life_hours=half_life,
                curiosity_floor=curiosity_floor,
            )
            logger.info(
                f"思考流系统已初始化: max_active={max_active}, "
                f"half_life={half_life}h, floor={curiosity_floor}"
            )

        # 初始化冲动引擎
        drives_cfg = getattr(cfg, "drives", None)
        if drives_cfg is None or getattr(drives_cfg, "enabled", True):
            self._impulse_engine = ImpulseEngine(list(DEFAULT_RULES))
            logger.info("冲动引擎已初始化")

        await self._start_selected_storage()

        # 初始化三环自学习系统
        learning_cfg = getattr(cfg, "learning", None)
        if learning_cfg is None or getattr(learning_cfg, "enabled", True):
            try:
                from ..learning.scheduler import LearningScheduler

                self._learning_scheduler = LearningScheduler(
                    workspace_path=cfg.settings.workspace_path,
                    model_task_name=getattr(cfg.model, "task_name", "core"),
                    # 反思环需要记忆服务：把"我之前理解错了"落成显式修正记录
                    memory_service=self._memory_service,
                    audit_interval_hours=float(getattr(learning_cfg, "audit_interval_hours", 6.0) if learning_cfg else 6.0),
                    audit_batch_size=int(getattr(learning_cfg, "audit_batch_size", 3) if learning_cfg else 3),
                    compress_trigger_count=int(getattr(learning_cfg, "compress_trigger_count", 5) if learning_cfg else 5),
                    compress_interval_hours=float(getattr(learning_cfg, "compress_interval_hours", 48.0) if learning_cfg else 48.0),
                    reflection_cooldown_minutes=float(getattr(learning_cfg, "reflection_cooldown_minutes", 30.0) if learning_cfg else 30.0),
                    skill_distill_trigger_count=int(getattr(learning_cfg, "skill_distill_trigger_count", 3) if learning_cfg else 3),
                    skill_distill_interval_hours=float(getattr(learning_cfg, "skill_distill_interval_hours", 24.0) if learning_cfg else 24.0),
                    minecraft_enabled=bool(getattr(cfg, "minecraft", None) and cfg.minecraft.enabled),
                    consciousness_registry=self.consciousness_registry,
                    save_consciousness_registry=(
                        self.save_consciousness_registry_async
                    ),
                    prepare_perception=self.prepare_perception,
                    commit_perception=self.commit_perception,
                    report_world_observation=self.report_world_observation,
                    minecraft_config={
                        "java_path": cfg.minecraft.java_path,
                        "mc_version": cfg.minecraft.mc_version,
                        "world_name": cfg.minecraft.world_name,
                        "window_width": cfg.minecraft.window_width,
                        "window_height": cfg.minecraft.window_height,
                        "vla_model": cfg.minecraft.vla_model,
                        "offline_username": cfg.minecraft.offline_username,
                        "default_body": cfg.minecraft.default_body,
                        "agent_bridge_uri": cfg.minecraft.agent_bridge_uri,
                        "agent_bridge_listen_uri": cfg.minecraft.agent_bridge_listen_uri,
                        "agent_token_file": Path(cfg.minecraft.agent_token_file),
                        "biomimetic_bridge_uri": cfg.minecraft.biomimetic_bridge_uri,
                        "biomimetic_bridge_listen_uri": cfg.minecraft.biomimetic_bridge_listen_uri,
                        "biomimetic_token_file": Path(cfg.minecraft.biomimetic_token_file),
                        "planner_task_name": cfg.minecraft.planner_task_name,
                        "bridge_ready_timeout_seconds": cfg.minecraft.bridge_ready_timeout_seconds,
                        "intent_timeout_seconds": cfg.minecraft.intent_timeout_seconds,
                    } if getattr(cfg, "minecraft", None) and cfg.minecraft.enabled else None,
                )
                logger.info("三环自学习系统已初始化")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"三环自学习系统初始化失败: {exc}")
                self._learning_scheduler = None

        self._state.running = True
        self._state.started_at = _now_iso()
        self._state.last_heartbeat_at = self._state.last_heartbeat_at or self._state.started_at
        self._state.last_error = None
        self._state.history_event_count = len(self._event_history)
        self._state.pending_event_count = len(self._pending_events)

        register_life_engine_service(self)

        if getattr(getattr(cfg, "autonomy", None), "enabled", True):
            try:
                await restore_autonomy_intents(self.plugin, cfg.settings.workspace_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"恢复自主意向失败: {exc}")

        self._stop_event = asyncio.Event()

        shared_sync_cfg = getattr(cfg, "shared_sync", None)
        if bool(getattr(shared_sync_cfg, "enabled", False)):
            try:
                from .shared_sync import SharedSyncBridge

                if self._selectable_storage_enabled:
                    raise RuntimeError(
                        "legacy shared_sync cannot bind a selected authoritative "
                        "Life Event backend"
                    )
                self._shared_sync_bridge = SharedSyncBridge(
                    shared_sync_cfg,
                    self._get_event_bus().store,
                )
                shared_sync_task = get_task_manager().create_task(
                    self._shared_sync_bridge.run(self._stop_event),
                    name="life_engine_shared_sync",
                    daemon=True,
                )
                self._shared_sync_task_id = shared_sync_task.task_id
                self._shared_sync_error = ""
            except Exception as exc:  # noqa: BLE001
                self._shared_sync_bridge = None
                self._shared_sync_task_id = None
                self._shared_sync_error = f"{type(exc).__name__}: {exc}"
                logger.error(f"life_engine 共享同步初始化失败: {self._shared_sync_error}")

        memory_archive_cfg = getattr(cfg, "memory_archive_sync", None)
        if bool(getattr(memory_archive_cfg, "enabled", False)):
            try:
                from .memory_archive_sync import MemoryArchiveSyncBridge

                self._memory_archive_sync_bridge = MemoryArchiveSyncBridge(
                    memory_archive_cfg,
                    cfg.settings.workspace_path,
                )
                archive_task = get_task_manager().create_task(
                    self._memory_archive_sync_bridge.run(self._stop_event),
                    name="life_engine_memory_archive_sync",
                    daemon=True,
                )
                self._memory_archive_sync_task_id = archive_task.task_id
                self._memory_archive_sync_error = ""
            except Exception as exc:  # noqa: BLE001
                self._memory_archive_sync_bridge = None
                self._memory_archive_sync_task_id = None
                self._memory_archive_sync_error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "life_engine unified memory archive initialization failed: "
                    f"{self._memory_archive_sync_error}"
                )

        chatter_cfg = getattr(cfg, "chatter", None)
        projection_enabled = bool(
            chatter_cfg is not None
            and getattr(chatter_cfg, "enabled", False)
            and getattr(
                chatter_cfg,
                "router_context_projection_enabled",
                True,
            )
        )
        if projection_enabled:
            self._router_context_projection = RouterContextProjection(
                self._workspace_dir(),
                author=self._author_router_context_projection,
                max_chars=int(
                    getattr(
                        chatter_cfg,
                        "router_context_projection_max_chars",
                        6000,
                    )
                    or 6000
                ),
                poll_interval_seconds=float(
                    getattr(
                        chatter_cfg,
                        "router_context_projection_poll_seconds",
                        1.0,
                    )
                    or 1.0
                ),
            )
            projection_task = get_task_manager().create_task(
                self._router_context_projection.run(),
                name="life_engine_router_context_projection",
                daemon=True,
            )
            self._router_context_projection_task_id = projection_task.task_id

        task = get_task_manager().create_task(
            self._heartbeat_loop(),
            name="life_engine_heartbeat",
            daemon=True,
        )
        self._heartbeat_task_id = task.task_id

        memory_index_options = self._memory_index_options()
        if self._memory_service is not None and memory_index_options["enabled"]:
            index_task = get_task_manager().create_task(
                self._memory_index_loop(),
                name="life_engine_memory_index",
                daemon=True,
            )
            self._memory_index_task_id = index_task.task_id

        witness_cfg = getattr(cfg, "memory_witness", None)
        if self._memory_service is not None and bool(
            getattr(witness_cfg, "enabled", True)
        ):
            from .memory_witness import MemoryWitnessCoordinator

            self._memory_witness_coordinator = MemoryWitnessCoordinator(self)
            await self._memory_witness_coordinator.ensure_instance()
            witness_task = get_task_manager().create_task(
                self._memory_witness_coordinator.loop(),
                name="life_engine_memory_witness",
                daemon=True,
            )
            self._memory_witness_task_id = witness_task.task_id

        logger.info(
            "life_engine 已启动: "
            f"interval={int(cfg.settings.heartbeat_interval_seconds)}s "
            f"task={cfg.model.task_name} "
            f"workspace={cfg.settings.workspace_path} "
            f"sleep={cfg.settings.sleep_time or '-'} "
            f"wake={cfg.settings.wake_time or '-'}"
        )
        log_lifecycle(
            "started",
            enabled=True,
            heartbeat_interval_seconds=int(cfg.settings.heartbeat_interval_seconds),
            model_task_name=cfg.model.task_name,
            log_file_path=str(get_life_log_file()),
        )

    async def _await_managed_task(self, task_id: str | None, *, timeout: float) -> None:
        """等待 daemon 自然退出，超时后取消并等待其清理完成。"""
        if not task_id:
            return
        manager = get_task_manager()
        try:
            task = manager.get_task(task_id).task
        except Exception:
            return
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"后台任务停止超时，正在取消: task_id={task_id}")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"后台任务取消时退出异常: task_id={task_id} error={exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"后台任务已异常退出: task_id={task_id} error={exc}")

    async def stop(self) -> None:
        """停止后台循环并关闭记忆 SQLite。"""
        from .registry import unregister_life_engine_service

        pending_before_stop = len(self._pending_events)
        shutdown_errors: list[Exception] = []
        self._state.running = False

        if self._stop_event is not None:
            self._stop_event.set()

        if self._router_context_projection is not None:
            self._router_context_projection.request_stop()

        await self._await_managed_task(
            self._router_context_projection_task_id,
            timeout=5.0,
        )
        self._router_context_projection_task_id = None
        self._router_context_projection = None

        await self._await_managed_task(self._shared_sync_task_id, timeout=10.0)
        self._shared_sync_task_id = None
        if self._shared_sync_bridge is not None:
            try:
                await self._shared_sync_bridge.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"关闭共享同步连接失败: {exc}")
            finally:
                self._shared_sync_bridge = None

        await self._await_managed_task(
            self._memory_archive_sync_task_id,
            timeout=15.0,
        )
        self._memory_archive_sync_task_id = None
        if self._memory_archive_sync_bridge is not None:
            try:
                await self._memory_archive_sync_bridge.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"failed to close unified memory archive: {exc}")
            finally:
                self._memory_archive_sync_bridge = None

        await self._await_managed_task(self._memory_witness_task_id, timeout=10.0)
        self._memory_witness_task_id = None
        self._memory_witness_coordinator = None
        await self._await_managed_task(self._memory_index_task_id, timeout=10.0)
        self._memory_index_task_id = None
        await self._await_managed_task(self._heartbeat_task_id, timeout=5.0)
        self._heartbeat_task_id = None

        memory_service = self._memory_service
        if memory_service is not None:
            try:
                await memory_service.close()
            except Exception as exc:  # noqa: BLE001
                shutdown_errors.append(exc)
                logger.error(f"关闭记忆服务失败: {exc}", exc_info=True)
            finally:
                self._memory_service = None

        try:
            await cleanup_autonomy_schedules(self._cfg().settings.workspace_path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"清理自主意向调度失败: {exc}")
        self._stop_event = None
        unregister_life_engine_service()
        await self._save_runtime_context()
        try:
            await self._close_selected_storage()
        except Exception as exc:  # noqa: BLE001 - finish remaining shutdown
            shutdown_errors.append(exc)
            logger.error(
                "关闭 selected Presence/World storage 失败",
                exc_info=True,
            )  # noqa: G201 - project Logger has no exception()

        logger.info("life_engine 已停止")
        log_lifecycle(
            "stopped",
            pending_message_count=pending_before_stop,
            heartbeat_count=self._state.heartbeat_count,
            log_file_path=str(get_life_log_file()),
        )
        if shutdown_errors:
            raise ExceptionGroup(
                "LifeEngineService shutdown reported consumer failures",
                shutdown_errors,
            )

    async def _run_heartbeat_round(
        self,
        *,
        collect_background_agents: bool,
    ) -> tuple[str, PreparedHeartbeatContext]:
        """Run one serialized heartbeat transaction."""
        async with self._heartbeat_run_lock:
            self._state.heartbeat_count += 1
            self._state.last_heartbeat_at = _now_iso()
            heartbeat_run_id = (
                f"heartbeat-{self._state.heartbeat_count}-{uuid4().hex[:12]}"
            )

            try:
                if self._memory_integration is not None:
                    await self._memory_integration.maybe_run_daily_decay()

                if collect_background_agents:
                    try:
                        await self._collect_background_agent_results()
                    except Exception as agent_exc:  # noqa: BLE001
                        logger.warning(
                            f"收集后台智能体结果异常（已跳过）: {agent_exc}",
                            exc_info=True,
                        )
                    try:
                        await self._collect_background_mission_results()
                    except Exception as mission_exc:  # noqa: BLE001
                        logger.warning(
                            f"收集后台使命结果异常（已跳过）: {mission_exc}",
                            exc_info=True,
                        )

                prepared = await self._prepare_heartbeat_context()
                log_heartbeat_event(
                    heartbeat_count=self._state.heartbeat_count,
                    last_heartbeat_at=self._state.last_heartbeat_at,
                    pending_message_count=self._state.pending_event_count,
                    last_wake_context_at=self._state.last_wake_context_at,
                    last_wake_context_size=self._state.last_wake_context_size,
                )
                # 心跳 LLM 总预算：防止单次心跳因重试/模型挂起而冻结超过 N 分钟。
                # 公式：heartbeat_timeout_seconds × 2 + 60，覆盖"1次完整尝试 + fallback降级"，
                # 超出时记录警告并跳过本轮（下一轮30s后自动触发）。
                _cfg_hb_timeout = float(
                    getattr(self._cfg().settings, "heartbeat_timeout_seconds", 120) or 120
                )
                _heartbeat_llm_budget = _cfg_hb_timeout * 2 + 60  # e.g. 300s @ 120s config
                try:
                    model_reply = await asyncio.wait_for(
                        self._run_heartbeat_model(
                            prepared.content,
                            heartbeat_run_id=heartbeat_run_id,
                        ),
                        timeout=_heartbeat_llm_budget,
                    )
                except asyncio.TimeoutError as _te:
                    logger.warning(
                        f"life_engine 心跳 #{self._state.heartbeat_count} "
                        f"LLM 总预算 {_heartbeat_llm_budget:.0f}s 超时，跳过本轮"
                    )
                    return "", prepared
                await self._record_model_reply(
                    model_reply,
                    heartbeat_run_id=heartbeat_run_id,
                    persist=False,
                )
                await self._commit_heartbeat_context(
                    prepared,
                    model_reply,
                    heartbeat_run_id,
                )

                # 交互结束 → 触发学习系统快环反思（后台非阻塞）
                if (
                    self._learning_scheduler is not None
                    and prepared.has_inbound_messages
                    and model_reply
                ):
                    interaction_text = str(model_reply or "")[:2000]
                    context_text = prepared.content[:3000] if prepared.content else ""
                    event_ids = list(prepared.selected_event_ids[:20])
                    get_task_manager().create_task(
                        self._learning_scheduler.on_interaction_end(
                            interaction_text=interaction_text,
                            context=context_text,
                            source_event_ids=event_ids,
                        ),
                        name=f"life_learning_reflect_{self._state.heartbeat_count}",
                        daemon=True,
                    )

                return str(model_reply or ""), prepared
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._state.last_model_error = str(exc)
                await self._save_runtime_context()
                raise

    async def _memory_index_loop(self) -> None:
        """独立运行 chunk 向量索引，每轮最多处理一批。"""
        options = self._memory_index_options()
        interval = int(options["interval_seconds"])
        run_immediately = bool(options["run_on_startup"])
        retry_failed_once = bool(options["retry_failed"])

        try:
            while self._state.running:
                if not run_immediately:
                    stop_event = self._stop_event
                    if stop_event is not None:
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=interval)
                            break
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(interval)
                run_immediately = False

                if not self._state.running or self._memory_service is None:
                    break

                try:
                    report = await self._memory_service.run_index_worker(
                        limit=int(options["batch_size"]),
                        retry_failed=retry_failed_once,
                        reclaim_after=float(options["reclaim_after_seconds"]),
                    )
                    retry_failed_once = False
                    _has_work = report.claimed or report.completed or report.failed or report.stale
                    _log = logger.info if _has_work else logger.debug
                    _log(
                        "life_engine 记忆索引批次完成: "
                        f"claimed={report.claimed} completed={len(report.completed)} "
                        f"failed={len(report.failed)} stale={len(report.stale)}"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    retry_failed_once = False
                    logger.error(f"life_engine 记忆索引批次失败: {exc}", exc_info=True)

                if self._stop_event is not None and self._stop_event.is_set():
                    break
        except asyncio.CancelledError:
            raise
        finally:
            logger.info("life_engine 记忆索引循环已停止")

    async def _heartbeat_loop(self) -> None:
        """心跳循环。"""
        interval = max(1, int(self._cfg().settings.heartbeat_interval_seconds))
        should_log_heartbeat = bool(self._cfg().settings.log_heartbeat)

        try:
            while self._state.running:
                if self._stop_event is not None:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                        break
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(interval)

                if not self._state.running:
                    break

                in_sleep_window, sleep_window_desc = self._in_sleep_window_now()
                if in_sleep_window:
                    if not self._sleep_state_active:
                        logger.info(
                            "life_engine 进入睡眠时段，暂停心跳处理: "
                            f"window={sleep_window_desc}"
                        )
                        self._sleep_state_active = True

                    if should_log_heartbeat:
                        logger.info(f"life_engine heartbeat tick: 睡眠中（{sleep_window_desc}），跳过")
                    continue
                elif self._sleep_state_active:
                    logger.info(
                        "life_engine 睡眠时段结束，恢复心跳处理: "
                        f"window={sleep_window_desc}"
                    )
                    self._sleep_state_active = False

                paused_by_self, remaining_minutes, paused_until, pause_reason = (
                    self._self_pause_status()
                )
                if paused_by_self:
                    # 检查是否到达检查点（每 N 分钟重评估一次）
                    checkpoint_interval = self._state.self_pause_checkpoint_minutes or 30
                    started_at_str = self._state.self_pause_started_at
                    should_checkpoint = False
                    
                    if started_at_str and remaining_minutes is not None:
                        try:
                            from datetime import datetime, timezone
                            started_at = datetime.fromisoformat(started_at_str)
                            if started_at.tzinfo is None:
                                started_at = started_at.replace(tzinfo=timezone.utc)
                            now = datetime.now(timezone.utc)
                            elapsed_minutes = (now - started_at).total_seconds() / 60
                            # 每经过检查点间隔就重评估一次
                            if elapsed_minutes >= checkpoint_interval:
                                next_checkpoint = int(elapsed_minutes // checkpoint_interval) * checkpoint_interval
                                if elapsed_minutes - next_checkpoint < interval / 60.0:
                                    should_checkpoint = True
                        except Exception:
                            pass
                    
                    if should_checkpoint:
                        logger.info(
                            f"life_engine 休息检查点到达（已休息 {self._state.self_pause_duration_minutes - (remaining_minutes or 0)}分钟），"
                            f"重新评估状态"
                        )
                        # 检查点不跳过，让 LLM 重新评估（可能选择继续休息或结束休息）
                        # 继续执行后续的 _run_heartbeat_round
                    else:
                        # 未到检查点，继续跳过
                        if not self._self_pause_skip_logged:
                            logger.info(
                                "life_engine heartbeat LLM 已因主动休息暂停: "
                                f"remaining={remaining_minutes}min until={paused_until} "
                                f"reason={pause_reason or '-'} "
                                f"consecutive={self._state.consecutive_rest_count}"
                            )
                            self._self_pause_skip_logged = True
                        continue
                if self._self_pause_skip_logged:
                    self._self_pause_skip_logged = False
                if self._state.self_pause_until:
                    await self.clear_self_pause(source="expired")

                injected_content = ""
                try:
                    model_reply, prepared = await self._run_heartbeat_round(
                        collect_background_agents=True,
                    )
                    injected_content = prepared.content

                except Exception as exc:  # noqa: BLE001
                    self._state.last_model_error = str(exc)
                    log_error(
                        "heartbeat_model_failed",
                        str(exc),
                        heartbeat_count=self._state.heartbeat_count,
                        heartbeat_at=self._state.last_heartbeat_at,
                        model_task_name=self._cfg().model.task_name,
                    )
                    logger.error(f"life_engine 心跳模型异常: {exc}\n{traceback.format_exc()}")

                if should_log_heartbeat:
                    if injected_content:
                        logger.info(
                            f"life_engine heartbeat #{self._state.heartbeat_count} "
                            f"at {self._state.last_heartbeat_at}: "
                            f"已注入 {self._state.last_wake_context_size} 条事件"
                        )
                    else:
                        logger.info(
                            f"life_engine heartbeat #{self._state.heartbeat_count} "
                            f"at {self._state.last_heartbeat_at}: 无新事件"
                        )

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._state.last_error = str(exc)
            logger.error(f"life_engine 心跳异常: {exc}\n{traceback.format_exc()}")
            log_error(
                "heartbeat_failed",
                str(exc),
                heartbeat_count=self._state.heartbeat_count,
                pending_message_count=self._state.pending_event_count,
            )
        finally:
            self._state.running = False

    async def trigger_heartbeat_manually(self) -> dict[str, Any]:
        """手动触发一次心跳。"""
        if not self._is_enabled():
            return {"success": False, "error": "life_engine 未启用"}

        in_sleep_window, sleep_window_desc = self._in_sleep_window_now()
        if in_sleep_window:
            return {"success": False, "error": f"当前在睡眠时段（{sleep_window_desc}），心跳已暂停"}

        logger.info("life_engine 手动触发心跳")

        try:
            model_reply, prepared = await self._run_heartbeat_round(
                collect_background_agents=False,
            )

            logger.info(
                f"life_engine 手动心跳完成 #{self._state.heartbeat_count}: "
                f"{_shorten_text(model_reply, max_length=120)}"
            )

            return {
                "success": True,
                "heartbeat_count": self._state.heartbeat_count,
                "heartbeat_at": self._state.last_heartbeat_at,
                "event_count": self._state.last_wake_context_size,
                "reply": model_reply,
            }
        except Exception as exc:  # noqa: BLE001
            self._state.last_model_error = str(exc)
            logger.error(f"life_engine 手动心跳失败: {exc}\n{traceback.format_exc()}")
            log_error(
                "manual_heartbeat_failed",
                str(exc),
                heartbeat_count=self._state.heartbeat_count,
                heartbeat_at=self._state.last_heartbeat_at,
            )
            return {
                "success": False,
                "error": str(exc),
                "heartbeat_count": self._state.heartbeat_count,
            }
