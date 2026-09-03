"""life_engine 生命中枢服务核心模块。

生命中枢是同一个主体在不同运行模式间切换的骨架。
它通过周期性心跳来处理堆积的消息、进行内部思考，并为工具调用、
对外交流与状态沉淀提供基础能力。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    InterfaceError,
    OperationalError,
)
from sqlalchemy.exc import (
    TimeoutError as SQLAlchemyTimeoutError,
)

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseService
from src.core.components.utils import should_strip_auto_reason_argument
from src.core.models.message import Message, MessageType
from src.kernel.concurrency import get_task_manager
from src.kernel.llm import ROLE, LLMPayload, Text, ToolRegistry, ToolResult
from src.kernel.llm.context_delivery import EffectiveContextReceipt

_STORAGE_RENEWAL_BACKOFF_BASE_SECONDS = 1.0
_STORAGE_RENEWAL_BACKOFF_MAX_SECONDS = 30.0
# 心跳模型失败若为瞬时 MySQL 断连（2013），前 8 次只记 debug/warning，
# 第 9 次才升级 ERROR 刷 traceback，避免 FRP 隧道抖动反复刷屏。
# 语义对齐 memory_witness._CONCURRENCY_ERROR_ESCALATION_COUNT。
_HEARTBEAT_TRANSIENT_ERROR_ESCALATION_COUNT = 9
_MYSQL_LOST_CONNECTION_ERROR_CODE = 2013


def _storage_renewal_backoff_seconds(
    failure_count: int,
    *,
    owner_instance_id: str,
) -> float:
    """Return bounded deterministic jitter for one renewal owner."""

    exponent = min(max(0, int(failure_count) - 1), 8)
    base = min(
        _STORAGE_RENEWAL_BACKOFF_MAX_SECONDS,
        _STORAGE_RENEWAL_BACKOFF_BASE_SECONDS * (2**exponent),
    )
    digest = hashlib.sha256(
        f"{owner_instance_id}:{failure_count}".encode()
    ).digest()
    fraction = int.from_bytes(digest[:2], "big") / 65535
    jittered = base * (0.8 + (0.4 * fraction))
    return min(_STORAGE_RENEWAL_BACKOFF_MAX_SECONDS, max(0.1, jittered))


def _is_storage_renewal_connectivity_unknown(exc: BaseException) -> bool:
    """Classify only transport/pool failures as an unknown renewal outcome."""

    if isinstance(
        exc,
        (
            OperationalError,
            InterfaceError,
            DisconnectionError,
            SQLAlchemyTimeoutError,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


def _dbapi_error_code(exc: DBAPIError) -> int | None:
    """Return a numeric DBAPI error code without copying SQL or server text."""

    for value in getattr(exc.orig, "args", ()):
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdecimal():
            return int(value)
    return None


def _is_transient_mysql_disconnect(exc: BaseException) -> bool:
    """Recognize MySQL 2013 lost-connection as a transient, self-healing failure.

    FRP 内网穿透隧道（frp-one.com:65429）抖动时，客户端查询中途连接被
    隧道切断，asyncmy 报 2013。服务器端 wait_timeout=28800 且连接级
    wait_timeout=180 已排除服务端空闲杀连接；此判定仅用于日志降级
    （前 8 次 warning/debug，第 9 次才 ERROR），不改变失败语义——
    心跳模型失败不推进游标，事件已在 publish_legacy_events 时写入账本。
    """

    return isinstance(exc, DBAPIError) and (
        _dbapi_error_code(exc) == _MYSQL_LOST_CONNECTION_ERROR_CODE
    )


def _transient_error_summary(exc: BaseException) -> str:
    """Describe a transient failure without dumping response bodies or traces."""

    details = [type(exc).__name__]
    if isinstance(exc, DBAPIError):
        code = _dbapi_error_code(exc)
        if code is not None:
            details.append(f"code={code}")
    if len(details) == 1:
        return details[0]
    return f"{details[0]}({', '.join(details[1:])})"

if TYPE_CHECKING:
    from ..attention_threads import (
        AttentionThreadCommand,
        AttentionThreadCommit,
        AttentionThreadPage,
        AttentionThreadPageQuery,
        InstanceFocus,
    )
    from ..memory.service import LifeMemoryService

from ..autonomy import (
    AsyncLocalAutonomyIntentStore,
    SelectedAutonomyIntentStore,
    cleanup_autonomy_schedules,
)
from ..constants import (
    HEARTBEAT_IDLE_CRITICAL_THRESHOLD,
    HEARTBEAT_IDLE_WARNING_THRESHOLD,
    LIFE_CHATTER_GLOBAL_CURSOR_KEY,
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
from ..core.tool_parallel import (
    is_life_tool_call_parallel_safe,
    iter_life_tool_call_batches,
)
from ..curiosity import CuriosityEngine
from ..initiative.contracts import (
    InitiativeOutreachCommand,
    InitiativeOutreachOutcome,
    InitiativeSeedCommand,
    InitiativeSeedCommit,
    InitiativeSeedView,
)
from ..memory.prompting import (
    analyze_memory_text,
    render_memory_prompt,
)
from ..opportunity import OpportunityBus
from ..proactive import ProactiveAuthority
from ..proactive.actor_gate import ProactiveActorDecisionGate
from ..prompts.sections import (
    DEFAULT_HEARTBEAT_SECTIONS,
    SectionContext,
    render_heartbeat_sections,
)
from ..trace.store import LifeTraceRecord
from .activity_panel import (
    format_decision_panel,
    format_skip_panel,
    format_stall_panel,
    format_tool_receipt_panel,
    print_activity_panel,
)
from .async_presence import (
    AsyncConsciousnessRegistry,
    flush_presence_lifecycle_events,
)
from .attention import AttentionRouter
from .audit import (
    get_life_log_file,
    log_error,
    log_heartbeat_model_response,
    log_lifecycle,
    log_message_received,
    log_wake_context_injected,
)
from .audit import (
    log_heartbeat as log_heartbeat_event,
)
from .chat_events import build_chat_message_event, build_chat_provider_notice_event
from .consciousness import ConsciousnessInstance, ConsciousnessRegistry
from .event_builder import (
    INTERNAL_STREAM_ID,
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
from .event_bus import (
    LifeEvent,
    LifeEventBus,
    LifeEventChannel,
    LifeEventPriority,
    RawEventStore,
    life_event_from_legacy,
)
from .subconscious_ingest import (
    SUBCONSCIOUS_INGEST_CONSUMER_ID,
    SubconsciousIngestStoreUnavailable,
    SubconsciousLedgerGap,
    catch_up_subconscious_ingest,
    commit_subconscious_ingest_cursor,
    workset_identities,
)
from .integrations import (
    MemoryIntegration,
)
from .perception_gateway import (
    DEFAULT_PERCEPTION_MAX_BYTES,
    AsyncPerceptionGateway,
    PerceptionCommitCheckpoint,
    PerceptionDeliveryReceipt,
    PerceptionDeliveryUnverified,
    PerceptionGateway,
    PreparedPerception,
)
from .self_pause import (
    apply_self_pause,
    build_self_pause_status,
    clear_self_pause_state,
    self_pause_status,
)
from .state_manager import (
    StatePersistence,
    get_file_metadata,
    minutes_since_time,
)
from .subconscious_context import (
    PreparedHeartbeatContext,
    RecentSubconsciousContext,
    SubconsciousContextManager,
    SubconsciousSummary,
)
from .heartbeat_rolling import (
    format_new_events_text,
    iter_selected_events,
    load_heartbeat_rolling,
    rolling_payloads_only,
    save_heartbeat_rolling,
)
from .world_projection import (
    WORLD_LEGACY_IMPORT_EVENT,
    WORLD_OBSERVATION_EVENT,
    WORLD_PROJECTION_DB_FILE,
    PerceptionCursorConflict,
    WorldProjectionStore,
    legacy_snapshot_assertions,
    reject_prompt_projection_persistence,
)
from .world_state import WorldState

if TYPE_CHECKING:
    from ..memory.service import LifeMemoryService


logger = get_logger("life_engine", display="life_engine")
LIFE_CHATTER_WORLD_MAX_BYTES = 32 * 1024
LIFE_CHATTER_PROJECTED_SUFFIX_MAX_BYTES = 60 * 1024
LIFE_CHATTER_EFFECTIVE_TEXT_MAX_BYTES = 64 * 1024
_LIFE_LOCAL_FENCING_ENV = "ELYSIUM_LIFE_LOCAL_FENCING_TOKEN"


def _stable_text_digest(text: str) -> str:
    """Content-free digest for one heartbeat model reply."""
    return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()


# 多写者心跳被其他实例认领（或已完成）时返回的空准备上下文。
# 调用方仅读取 content/selected_event_ids 用于日志，绝不提交。
_SKIPPED_HEARTBEAT_PREPARED = PreparedHeartbeatContext(
    content="",
    snapshot_high_water=0,
    selected_event_ids=[],
    acknowledged_event_ids=[],
    summary_event_ids=[],
    before_chars=0,
    after_chars=0,
    dropped_count=0,
    target_reached=False,
    updated_summary=SubconsciousSummary(),
)


def _validate_content_free_sha256(value: str, *, field_name: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{field_name} must be 64 lowercase hex characters")
    return digest


@dataclass(frozen=True, slots=True)
class ChatterRuntimeDeliveryReceipt:
    """Content-free proof that the final complete suffix reached the model."""

    delivery_id: str
    effective_suffix_sha256: str
    effective_suffix_bytes: int
    exact: bool
    transport_request_id: str = ""

    def __post_init__(self) -> None:
        if not self.delivery_id.strip():
            raise ValueError("chatter receipt delivery_id must not be empty")
        _validate_content_free_sha256(
            self.effective_suffix_sha256,
            field_name="chatter receipt effective_suffix_sha256",
        )
        if not 0 < self.effective_suffix_bytes <= LIFE_CHATTER_EFFECTIVE_TEXT_MAX_BYTES:
            raise ValueError("chatter receipt effective suffix byte count is invalid")


@dataclass(frozen=True, slots=True)
class ChatterRuntimeCommitCheckpoint:
    """Durable replay identity for one World/event/thought chatter delivery."""

    cursor_key: str
    delivery_id: str
    effective_suffix_sha256: str
    effective_suffix_bytes: int
    event_through_sequence: int
    thought_through_revision: int
    perception: PerceptionCommitCheckpoint

    def __post_init__(self) -> None:
        if not self.cursor_key.strip():
            raise ValueError("chatter checkpoint cursor_key must not be empty")
        if not self.delivery_id.strip():
            raise ValueError("chatter checkpoint delivery_id must not be empty")
        _validate_content_free_sha256(
            self.effective_suffix_sha256,
            field_name="chatter checkpoint effective_suffix_sha256",
        )
        if not 0 < self.effective_suffix_bytes <= LIFE_CHATTER_EFFECTIVE_TEXT_MAX_BYTES:
            raise ValueError(
                "chatter checkpoint effective suffix byte count is invalid"
            )
        if self.event_through_sequence < 0 or self.thought_through_revision < 0:
            raise ValueError("chatter checkpoint frontiers must not be negative")


@dataclass(frozen=True, slots=True)
class ChatterRuntimeCommitResult:
    """Content-free cursor state after one durable chatter delivery commit."""

    delivery_id: str
    event_through_sequence: int
    thought_through_revision: int
    world_position: int
    world_revision: int


@dataclass(frozen=True, slots=True)
class ChatterRuntimeDelivery:
    """Content-free metadata for one bounded transient chatter suffix."""

    delivery_id: str
    delivery_marker: str
    projected_suffix_sha256: str
    projected_suffix_bytes: int
    source_bytes: int
    omitted_bytes: int
    prepared_perception: PreparedPerception
    event_through_sequence: int
    thought_through_revision: int

    def commit_checkpoint(
        self,
        *,
        cursor_key: str,
        effective_suffix_sha256: str,
        effective_suffix_bytes: int,
    ) -> ChatterRuntimeCommitCheckpoint:
        """Bind the final complete suffix identity without retaining its text."""

        return ChatterRuntimeCommitCheckpoint(
            cursor_key=cursor_key,
            delivery_id=self.delivery_id,
            effective_suffix_sha256=effective_suffix_sha256,
            effective_suffix_bytes=effective_suffix_bytes,
            event_through_sequence=self.event_through_sequence,
            thought_through_revision=self.thought_through_revision,
            perception=self.prepared_perception.commit_checkpoint(),
        )


@dataclass(frozen=True, slots=True)
class HeartbeatModelResult:
    """One heartbeat result plus proof of its exact wake projection."""

    text: str
    perception_receipt: PerceptionDeliveryReceipt | None
    subconscious_receipt: EffectiveContextReceipt | None = None
    compression_unresolved: bool = False
    rolling_payloads: tuple[LLMPayload, ...] = ()


HEARTBEAT_TOTAL_BUDGET_MAX_SECONDS = 300.0
HEARTBEAT_FINALIZE_RESERVE_SECONDS = 5.0


class HeartbeatBudgetExhausted(TimeoutError):
    """The shared heartbeat deadline was exhausted at a content-free stage."""

    def __init__(self, stage: str) -> None:
        self.stage = str(stage or "unknown")
        super().__init__(f"heartbeat total deadline exhausted at {self.stage}")


@dataclass(frozen=True, slots=True)
class HeartbeatToolRoundProgress:
    """Content-free progress evidence for one completed heartbeat tool round."""

    fingerprint: str
    failure_fingerprint: str
    has_success: bool
    has_successful_mutation: bool
    has_protocol_failure: bool


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


def _resolve_heartbeat_total_budget(configured: float) -> float:
    """Resolve one bounded budget shared by every model step in a heartbeat."""

    return max(
        10.0,
        min(HEARTBEAT_TOTAL_BUDGET_MAX_SECONDS, float(configured) * 2 + 60.0),
    )


def _heartbeat_remaining_seconds(
    deadline: float,
    *,
    reserve_seconds: float = HEARTBEAT_FINALIZE_RESERVE_SECONDS,
) -> float:
    """Return usable monotonic time while preserving the finalization reserve."""

    return deadline - asyncio.get_running_loop().time() - max(0.0, reserve_seconds)


async def _await_with_heartbeat_deadline(
    factory: Callable[[], Awaitable[Any]],
    *,
    deadline: float,
    stage: str,
    per_call_timeout: float | None = None,
    reserve_seconds: float = HEARTBEAT_FINALIZE_RESERVE_SECONDS,
) -> Any:
    """Await one step within the heartbeat's single monotonic deadline."""

    remaining = _heartbeat_remaining_seconds(
        deadline,
        reserve_seconds=reserve_seconds,
    )
    if remaining <= 0:
        raise HeartbeatBudgetExhausted(stage)
    timeout = remaining
    deadline_limited = True
    if per_call_timeout is not None and per_call_timeout > 0:
        timeout = min(remaining, float(per_call_timeout))
        deadline_limited = remaining <= float(per_call_timeout)
    try:
        return await asyncio.wait_for(factory(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        if deadline_limited:
            raise HeartbeatBudgetExhausted(stage) from exc
        raise


async def _sleep_with_heartbeat_deadline(
    delay_seconds: float,
    *,
    deadline: float,
    stage: str,
) -> None:
    """Sleep only when the full delay fits before the finalization reserve."""

    delay = max(0.0, float(delay_seconds))
    if delay > _heartbeat_remaining_seconds(deadline):
        raise HeartbeatBudgetExhausted(stage)
    await asyncio.sleep(delay)


def _heartbeat_tool_results(response: Any) -> list[ToolResult]:
    """Return tool-result parts without retaining or logging their text."""

    results: list[ToolResult] = []
    for payload in list(getattr(response, "payloads", []) or []):
        if getattr(payload, "role", None) != ROLE.TOOL_RESULT:
            continue
        for part in list(getattr(payload, "content", []) or []):
            if isinstance(part, ToolResult):
                results.append(part)
    return results


def _heartbeat_tool_result_failed(result: ToolResult | None) -> bool:
    if result is None:
        return True
    return result.to_text().startswith(("执行失败:", "执行异常:", "未知工具:"))


def _heartbeat_tool_round_outcomes(
    calls: list[Any],
    results: list[ToolResult],
) -> list[str]:
    """Content-free tool names plus ok/fail for one completed round."""

    outcomes: list[str] = []
    for index, call in enumerate(calls):
        tool_name, _args = LifeEngineService._heartbeat_tool_call_metadata(call)
        result = results[index] if index < len(results) else None
        status = "fail" if _heartbeat_tool_result_failed(result) else "ok"
        outcomes.append(f"{tool_name or '<unknown>'}:{status}")
    return outcomes


def _heartbeat_stall_kind(
    *,
    consecutive_no_progress: int,
    consecutive_protocol_failures: int,
    consecutive_same_failure: int,
    stall_limit: int,
) -> str:
    kinds: list[str] = []
    if consecutive_protocol_failures >= stall_limit:
        kinds.append("protocol_failure")
    if consecutive_same_failure >= stall_limit:
        kinds.append("same_failure")
    if consecutive_no_progress >= stall_limit:
        kinds.append("no_progress")
    return "+".join(kinds) or "stall"


def _heartbeat_tool_round_progress(
    calls: list[Any],
    results: list[ToolResult],
) -> HeartbeatToolRoundProgress:
    """Hash one tool round and classify only technical protocol failures."""

    rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    has_success = False
    has_successful_mutation = False
    has_protocol_failure = False
    protocol_markers = (
        "invalid",
        "continuation",
        "cursor",
        "argument",
        "parameter",
        "required",
        "unknown tool",
        "checksum",
        "参数",
        "游标",
        "未知工具",
        "缺少",
    )

    for index, call in enumerate(calls):
        tool_name, args = LifeEngineService._heartbeat_tool_call_metadata(call)
        args.pop("reason", None)
        args_json = json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        result = results[index] if index < len(results) else None
        result_text = (
            result.to_text() if result is not None else "<missing-tool-result>"
        )
        failed = _heartbeat_tool_result_failed(result)
        row = {
            "tool": tool_name,
            "args_sha256": hashlib.sha256(args_json.encode("utf-8")).hexdigest(),
            "result_sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(),
            "success": not failed,
        }
        rows.append(row)
        if failed:
            failed_rows.append(row)
            folded = result_text.casefold()
            has_protocol_failure = has_protocol_failure or any(
                marker in folded for marker in protocol_markers
            )
            continue
        has_success = True
        if not is_life_tool_call_parallel_safe(call):
            has_successful_mutation = True

    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    failed_canonical = json.dumps(
        failed_rows,
        sort_keys=True,
        separators=(",", ":"),
    )
    return HeartbeatToolRoundProgress(
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        failure_fingerprint=(
            hashlib.sha256(failed_canonical.encode("utf-8")).hexdigest()
            if failed_rows
            else ""
        ),
        has_success=has_success,
        has_successful_mutation=has_successful_mutation,
        has_protocol_failure=has_protocol_failure,
    )


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
        # record_message 接近 EventBus 硬截止的连续慢计数：单次偶发（远端 MySQL
        # 网络波动）记 INFO，连续 2 次才 WARNING（真实性能问题信号）。
        self._slow_record_message_streak: int = 0
        self._heartbeat_task_id: str | None = None
        self._learning_maintenance_task_id: str | None = None
        self._initiative_reencounter_task_id: str | None = None
        self._storage_authority_renew_task_id: str | None = None
        self._memory_index_task_id: str | None = None
        self._memory_witness_task_id: str | None = None
        self._memory_witness_coordinator = None
        self._shared_sync_task_id: str | None = None
        self._shared_sync_bridge = None
        self._shared_sync_error: str = ""
        self._shared_sync_configured_enabled: bool = False
        self._shared_sync_effective_enabled: bool = False
        self._shared_sync_disabled_reason: str = ""
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
        from ..inner_dialogue.protocol import InnerDialogueLedger

        self._inner_dialogue_ledger = InnerDialogueLedger()
        self._lock: asyncio.Lock | None = None
        self._heartbeat_run_lock = asyncio.Lock()
        self._proactive_actor_gate = ProactiveActorDecisionGate()
        settings = getattr(getattr(plugin, "config", None), "settings", None)
        configured_budget = getattr(settings, "subconscious_context_max_chars", None)
        if configured_budget is None:
            configured_budget = getattr(settings, "heartbeat_context_max_chars", None)
        try:
            context_budget = max(1000, int(configured_budget or 16000))
        except (TypeError, ValueError):
            context_budget = 16000
        try:
            summary_max = max(
                200,
                int(getattr(settings, "subconscious_summary_max_chars", None) or 4000),
            )
        except (TypeError, ValueError):
            summary_max = 4000
        try:
            entry_max = max(
                40, int(getattr(settings, "subconscious_entry_max_chars", None) or 480)
            )
        except (TypeError, ValueError):
            entry_max = 480
        try:
            recent_groups = max(
                0, int(getattr(settings, "subconscious_recent_groups", None) or 5)
            )
        except (TypeError, ValueError):
            recent_groups = 5
        try:
            summary_max_entries = max(
                10,
                int(getattr(settings, "subconscious_summary_max_entries", None) or 60),
            )
        except (TypeError, ValueError):
            summary_max_entries = 60
        self._subconscious_context: SubconsciousContextManager = (
            SubconsciousContextManager(
                max_chars=context_budget,
                recent_group_count=recent_groups,
                summary_max_chars=summary_max,
                entry_max_chars=entry_max,
                summary_max_entries=summary_max_entries,
            )
        )
        self._sleep_state_active: bool = False
        self._self_pause_skip_logged: bool = False
        self._memory_service: LifeMemoryService | None = None
        from ..storage.factory import settings_from_life_engine_config

        if hasattr(plugin, "global_storage_config"):
            explicit_global_config = plugin.global_storage_config
        else:
            # Historical embedded callers pass a lightweight plugin stub and
            # explicitly exercise local-mode behavior outside Core bootstrap.
            # Real LifeEnginePlugin instances always own the attribute; a
            # missing/None real Core config therefore still fails closed.
            from src.core.config.core_config import CoreConfig

            explicit_global_config = CoreConfig(
                storage=CoreConfig.StorageSection(backend="local")
            )
        self._storage_factory_settings = settings_from_life_engine_config(
            self._cfg(),
            global_config=explicit_global_config,
        )
        self._selectable_storage_enabled = bool(self._storage_factory_settings.enabled)
        self._storage_runtime: Any | None = None
        self._presence_world_stores: Any | None = None
        self._life_event_store: Any | None = None
        self._learning_stores: Any | None = None
        self._learning_event_store: Any | None = None
        self._proactive_authority: ProactiveAuthority | None = None
        self._local_proactive_runtime: Any | None = None
        self._proactive_delivery_proof_hook: Any | None = None
        from ..storage.instance_identity import generate_boot_id

        self._proactive_claim_owner = generate_boot_id()
        self._active_initiative_expression_claims: set[str] = set()
        self._pending_initiative_expression_resolutions: dict[
            str,
            tuple[InitiativeOutreachOutcome, str, str, str, str],
        ] = {}
        self._subject_document_store: Any | None = None
        self._runtime_state_store: Any | None = None
        self._runtime_context_writer_claim: Any | None = None
        self._learning_writer_claim: Any | None = None
        self._storage_renewal_health: dict[str, Any] = {
            "status": (
                "initializing" if self._selectable_storage_enabled else "disabled"
            ),
            "last_success_at": "",
            "next_retry_at": "",
            "error_type": "",
            "consecutive_failures": 0,
        }
        self._lost_singleton_health: dict[tuple[str, str], dict[str, Any]] = {}
        self._proactive_health_cache: dict[str, Any] = {
            "component": "proactive_authority",
            "status": "initializing",
            "authority_count": 0,
        }
        self._learning_storage_health: dict[str, Any] = {
            "status": (
                "initializing" if self._selectable_storage_enabled else "disabled"
            ),
            "projector_owner": False,
            "event_append_available": False,
            "reason": (
                "selected learning storage has not started"
                if self._selectable_storage_enabled
                else "selected storage is disabled"
            ),
        }
        self._storage_writer_instance_id = (
            f"{self._storage_factory_settings.authority_owner_id}:"
            f"pid-{os.getpid()}:{uuid4().hex[:16]}"
        )
        self._multi_writer_bridge: Any | None = None
        self._inbound_fact_hook: Any | None = None
        self._outbox_intent_hook: Any | None = None
        self._outbox_settle_hook: Any | None = None
        # 本节点记忆索引投影的连续 frontier（进程内计数，严格 +1 推进）。
        self._projection_frontier: int = 0
        self._subject_workspace_observer: Any | None = None
        self._subject_workspace_projector: Any | None = None
        self._subject_projection_task_id: str | None = None
        self._subject_projection_health: dict[str, Any] = {
            "status": "disabled",
            "last_success_at": "",
            "last_error_type": "",
        }
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
        # 选定后端下本地 world_state.json 不是权威来源，也不是迁移源：
        # 世界事实只能来自远端事件账本派生的 World Projection。读取本地
        # 快照会在远端为准时引入撕裂的旧世界事实，因此这里不读。
        # Loading the optional legacy JSON snapshot is read-only. Local
        # Presence initialization remains deferred because opening its SQLite
        # store creates files and must happen only after subject preflight.
        self._world_state: WorldState | None
        if self._selectable_storage_enabled:
            self._world_state = None
        else:
            workspace = Path(self._cfg().settings.workspace_path).resolve()
            self._world_state = WorldState.load(
                workspace / "runtime" / "world_state.json"
            )

        # 意识实例注册表（多意识协调）
        self._consciousness_registry: (
            ConsciousnessRegistry | AsyncConsciousnessRegistry | None
        )
        self._consciousness_registry = None

        # 集成管理器
        self._memory_integration: MemoryIntegration | None = None

        # 事件构建器
        self._event_builder = EventBuilder(self._next_sequence)

        # 异步好奇层
        self._curiosity_engine: CuriosityEngine | None = None
        self._curiosity_inflight: bool = False
        self._autonomy_intent_store: Any | None = None
        self._life_trace_store: Any | None = None
        self._narrative_store: Any | None = None

        # 三环自学习系统
        self._learning_scheduler = None  # LearningScheduler | None

        # Minecraft 是独立具身运行时，不从属于学习系统。
        self._minecraft_session: Any | None = None
        self._minecraft_session_close_lock = asyncio.Lock()
        self._minecraft_decision_event_cache: dict[str, LifeEngineEvent] = {}
        self._minecraft_recorded_decision_ids: set[str] = set()

        # 消息慢阶段后台化串行锁：facts/context 移到后台后仍需串行，
        # 避免并发写 runtime checkpoint 触发无谓的 revision 冲突。
        self._message_persist_lock = asyncio.Lock()
        self._subconscious_ingest_lock = asyncio.Lock()
        self._subconscious_ingest_health: dict[str, Any] = {
            "status": "idle",
            "component": "subconscious_ingest",
            "consumer_id": SUBCONSCIOUS_INGEST_CONSUMER_ID,
            "position": 0,
            "revision": 0,
            "frontier": 0,
            "backlog": 0,
            "bootstrapped": False,
            "gap": False,
            "error_type": "",
            "last_success_at": "",
        }
        self._message_persist_health: dict[str, Any] = {
            "status": "idle",
            "consecutive_failures": 0,
            "error_type": "",
        }

        # 世界投影追赶串行锁：后台任务与 record_send_requested /
        # record_delivery_status 等同步调用点都会触发 catch_up，
        # 并发会导致 world_projection_changes 主键冲突。
        self._world_projection_lock = asyncio.Lock()

        # 状态持久化
        self._state_persistence: StatePersistence | None = None
        self._event_bus: LifeEventBus | None = None
        self._world_projection: Any | None = None
        self._perception_gateway: PerceptionGateway | AsyncPerceptionGateway | None = (
            None
        )
        self._pending_chatter_perceptions: dict[str, PreparedPerception] = {}
        self._pending_chatter_deliveries: dict[str, ChatterRuntimeDelivery] = {}
        self._attention_router: AttentionRouter | None = None
        self._last_memory_maintenance_prompt_at: str | None = None
        self._opportunity_bus = OpportunityBus(self)

    @property
    def memory_service(self) -> LifeMemoryService | None:
        """兼容旧调用方的公开记忆服务访问入口。"""
        return self._memory_service

    async def _memory_behavior_health_snapshot(self) -> dict[str, Any]:
        """Aggregate Witness, recall delivery, and continuity health."""

        witness: dict[str, Any]
        coordinator = self._memory_witness_coordinator
        if coordinator is None:
            witness = {
                "status": "disabled",
                "component": "memory_witness_pipeline",
                "reason": "memory_witness_disabled",
            }
        else:
            try:
                witness = dict(await coordinator.health_snapshot())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - health stays content-free
                witness = {
                    "status": "failed",
                    "component": "memory_witness_pipeline",
                    "error_type": type(exc).__name__,
                }

        continuity: dict[str, Any]
        memory = self._memory_service
        subject_store = self._subject_document_store
        if not self._selectable_storage_enabled:
            continuity = {
                "status": "disabled",
                "component": "memory_continuity",
                "reason": "selected_subject_authority_disabled",
            }
        elif memory is None or subject_store is None:
            continuity = {
                "status": "failed",
                "component": "memory_continuity",
                "error_type": "ContinuityHealthCoherentRuntimeUnavailable",
            }
        else:
            from ..memory.continuity_health import collect_continuity_memory_health

            try:
                continuity = await collect_continuity_memory_health(
                    subject_store=subject_store,
                    living_store=memory.living_memory_store,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - health stays content-free
                continuity = {
                    "status": "failed",
                    "component": "memory_continuity",
                    "error_type": type(exc).__name__,
                }

        from ..memory.boundary_resolver import (
            get_memory_boundary_recall_coordinator,
        )
        from ..memory.recall_delivery import (
            get_memory_search_recall_delivery_coordinator,
        )

        boundary_recall = (
            get_memory_boundary_recall_coordinator().health_snapshot()
        )
        search_recall = (
            get_memory_search_recall_delivery_coordinator().health_snapshot()
        )
        recall_statuses = {
            str(boundary_recall.get("status") or "failed"),
            str(search_recall.get("status") or "failed"),
        }
        if "failed" in recall_statuses:
            recall_status = "failed"
        elif recall_statuses - {"healthy", "ok", "disabled"}:
            recall_status = "degraded"
        elif recall_statuses == {"disabled"}:
            recall_status = "disabled"
        else:
            recall_status = "healthy"
        recall_delivery = {
            "status": recall_status,
            "component": "memory_recall_exact_delivery",
            "pending_count": sum(
                max(0, int(item.get("pending_count") or 0))
                for item in (boundary_recall, search_recall)
            ),
            "boundary": boundary_recall,
            "search": search_recall,
            "authority": "process_local_delivery_proof_only",
        }

        generic_memory_candidates: dict[str, Any]
        learning = self._learning_scheduler
        decision_ledger = getattr(learning, "decision_ledger", None)
        if decision_ledger is None:
            generic_memory_candidates = {
                "status": "disabled",
                "component": "legacy_memory_candidates",
                "reason": "learning_decision_ledger_disabled",
                "backlog_lower_bound": 0,
            }
        else:
            try:
                candidate_page = await decision_ledger.list_candidates(
                    status="all",
                    limit=100,
                )
                blocked = [
                    item
                    for item in candidate_page
                    if str(item.get("target_path") or "") == "MEMORY.md"
                    and str(item.get("candidate_kind") or "")
                    != "memory_continuity_document_revision"
                    and str(item.get("status") or "") != "committed"
                ]
                generic_memory_candidates = {
                    "status": "degraded" if blocked else "healthy",
                    "component": "legacy_memory_candidates",
                    "backlog_lower_bound": len(blocked),
                    "page_limit": 100,
                    "migration_required": bool(blocked),
                    "decision_path": "blocked_fail_closed",
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - health stays content-free
                generic_memory_candidates = {
                    "status": "failed",
                    "component": "legacy_memory_candidates",
                    "error_type": type(exc).__name__,
                    "backlog_lower_bound": 0,
                }

        statuses = {
            str(witness.get("status") or "failed"),
            str(continuity.get("status") or "failed"),
            str(recall_delivery.get("status") or "failed"),
            str(generic_memory_candidates.get("status") or "failed"),
        }
        if "failed" in statuses:
            status = "failed"
        elif statuses - {"healthy", "ok", "disabled"}:
            status = "degraded"
        elif statuses == {"disabled"}:
            status = "disabled"
        else:
            status = "healthy"

        raw = witness.get("raw_ingest")
        author = witness.get("author")
        runtime = witness.get("runtime")
        backlogs = []
        for section in (raw, author):
            if isinstance(section, Mapping):
                value = section.get("backlog")
                if isinstance(value, int) and not isinstance(value, bool):
                    backlogs.append(max(0, value))
        for section, field in (
            (recall_delivery, "pending_count"),
            (generic_memory_candidates, "backlog_lower_bound"),
        ):
            value = section.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                backlogs.append(max(0, value))

        snapshot: dict[str, Any] = {
            "status": status,
            "component": "memory_behavior",
            "owner": "life_engine",
            "witness": witness,
            "continuity": continuity,
            "recall_delivery": recall_delivery,
            "legacy_memory_candidates": generic_memory_candidates,
            "backlog": sum(backlogs),
        }
        if isinstance(runtime, Mapping):
            snapshot["last_success_at"] = str(
                runtime.get("last_success_at") or ""
            )
        if status not in {"healthy", "ok", "disabled"}:
            failures = [
                str(item.get("error_type") or item.get("reason") or "")
                for item in (
                    witness,
                    continuity,
                    recall_delivery,
                    generic_memory_candidates,
                )
                if str(item.get("status") or "")
                not in {"healthy", "ok", "disabled"}
            ]
            snapshot["reason"] = ",".join(filter(None, failures)) or (
                "memory_behavior_degraded"
            )
        return snapshot

    async def get_memory_continuity_review_runtime(self, tool: Any) -> Any:
        """Build the one public, fail-closed continuity-review dependency bundle.

        A review may inspect and prepare immutable evidence only when the same
        selected runtime owns Subject Authority, Learning decisions, Memory
        Boundaries, and the active Presence actor.  Local Markdown is never
        promoted into a second acceptance authority here.
        """

        from ..memory.boundary import MemoryBoundaryRepository
        from ..memory.continuity_delivery import (
            get_memory_continuity_delivery_coordinator,
        )
        from ..memory.continuity_session import (
            ContinuityReviewActorContext,
            ContinuityReviewRuntimeUnavailable,
            ContinuityReviewSession,
        )
        from ..memory.continuity_tools import ContinuityReviewToolRuntime

        if not self._selectable_storage_enabled:
            raise ContinuityReviewRuntimeUnavailable(
                "ContinuityReviewSelectedSubjectAuthorityRequired"
            )
        memory = self._memory_service
        scheduler = self._learning_scheduler
        subject_authority = self._subject_document_store
        ledger = getattr(scheduler, "decision_ledger", None)
        if memory is None or scheduler is None or subject_authority is None:
            raise ContinuityReviewRuntimeUnavailable(
                "ContinuityReviewCoherentRuntimeUnavailable"
            )
        if ledger is None:
            raise ContinuityReviewRuntimeUnavailable(
                "ContinuityReviewLearningDecisionLedgerUnavailable"
            )
        selected_runtime = self._storage_runtime
        if selected_runtime is None:
            raise ContinuityReviewRuntimeUnavailable(
                "ContinuityReviewCoherentRuntimeUnavailable"
            )

        def dependency_runtime(value: Any) -> Any | None:
            runtime = getattr(value, "storage_runtime", None)
            if runtime is None:
                runtime = getattr(value, "runtime", None)
            return runtime

        bindings = {
            "memory": dependency_runtime(memory),
            "subject": dependency_runtime(subject_authority),
            "learning": dependency_runtime(scheduler),
            "decision_ledger": dependency_runtime(ledger),
        }
        if any(runtime is not selected_runtime for runtime in bindings.values()):
            raise ContinuityReviewRuntimeUnavailable(
                "ContinuityReviewCoherentRuntimeMismatch"
            )

        stream_scope = str(tool.get_current_stream_id() or "").strip()
        instance = (
            self.consciousness_registry.get_for_stream(stream_scope)
            if stream_scope
            else None
        )
        if instance is None and str(
            getattr(tool, "_runtime_task_name", "") or ""
        ) == "core":
            instance = self.consciousness_registry.get("chat_global")
            stream_scope = stream_scope or "chat_global"
        if instance is None or not instance.is_active:
            raise ContinuityReviewRuntimeUnavailable(
                "ContinuityReviewActiveStreamOwnerRequired"
            )

        tool_call_id = str(getattr(tool, "_tool_call_id", "") or "").strip()
        if not tool_call_id:
            raise ContinuityReviewRuntimeUnavailable(
                "ContinuityReviewToolCallIdentityRequired"
            )
        message = getattr(tool, "trigger_message", None)
        extra = getattr(message, "extra", {}) or {}
        turn_scope = extra.get("life_turn_scope") if isinstance(extra, dict) else None
        source_occurrence_id = str(
            getattr(tool, "_life_source_occurrence_id", "") or ""
        ).strip()
        if not source_occurrence_id and isinstance(turn_scope, dict):
            source_occurrence_id = str(turn_scope.get("turn_key") or "").strip()
        if not source_occurrence_id:
            source_occurrence_id = str(
                getattr(message, "message_id", "") or ""
            ).strip()
        if not source_occurrence_id:
            raise ContinuityReviewRuntimeUnavailable(
                "ContinuityReviewSourceOccurrenceRequired"
            )

        occurred_at = _now_iso()
        raw_time = getattr(message, "time", None)
        if isinstance(raw_time, datetime):
            parsed_time = (
                raw_time
                if raw_time.tzinfo is not None
                else raw_time.replace(tzinfo=timezone.utc)
            )
            occurred_at = parsed_time.astimezone(timezone.utc).isoformat()
        elif isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
            occurred_at = datetime.fromtimestamp(
                float(raw_time), tz=timezone.utc
            ).isoformat()
        elif str(raw_time or "").strip():
            try:
                parsed_time = datetime.fromisoformat(str(raw_time))
                if parsed_time.tzinfo is None:
                    parsed_time = parsed_time.replace(tzinfo=timezone.utc)
                occurred_at = parsed_time.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass

        repository = MemoryBoundaryRepository(memory.living_memory_store)

        async def record_review_outcome(outcome: Any) -> None:
            """Project one already-durable continuity outcome idempotently."""

            outcome_kind = str(outcome.outcome_kind)
            scheduler_outcome = (
                "snoozed" if outcome_kind == "snooze" else outcome_kind
            )
            if outcome_kind == "candidate_proposed":
                occurrence_id = str(outcome.candidate_occurrence_id)
            elif outcome_kind in {"rejected", "kept_open", "committed"}:
                occurrence_id = str(outcome.decision_occurrence_id)
            else:
                occurrence_id = str(outcome.outcome_occurrence_id)
            subject_revision = (
                str(outcome.subject_revision_after)
                if outcome_kind == "committed"
                else str(outcome.subject_revision_before)
            )
            await scheduler.record_subject_review_outcome(
                target_path="MEMORY.md",
                outcome=scheduler_outcome,
                actor_consciousness_instance_id=(
                    str(outcome.actor_consciousness_instance_id)
                ),
                subject_revision=subject_revision,
                occurrence_id=occurrence_id,
                reason=str(outcome.reason),
                candidate_id=str(outcome.candidate_id),
                candidate_sha256=str(outcome.candidate_sha256),
                authority_occurrence_id=str(outcome.authority_occurrence_id),
                snooze_hours=float(outcome.snooze_hours),
            )

        session = ContinuityReviewSession(
            subject_authority=subject_authority,
            boundary_repository=repository,
            candidate_ledger=ledger,
            validate_active_actor=self._validate_learning_decision_actor,
            delivery_verifier=get_memory_continuity_delivery_coordinator(),
            outcome_recorder=record_review_outcome,
        )
        return ContinuityReviewToolRuntime(
            session=session,
            actor=ContinuityReviewActorContext(
                consciousness_instance_id=str(instance.instance_id),
                stream_scope=stream_scope,
                source_occurrence_id=source_occurrence_id,
                action_occurrence_id=tool_call_id,
                occurred_at=occurred_at,
            ),
        )

    @property
    def selected_subject_storage_enabled(self) -> bool:
        """Return whether subject files require the selected durable writer."""

        return self._selectable_storage_enabled

    @property
    def world_state(self) -> WorldState:
        """Return the non-authoritative legacy migration snapshot."""

        if self._world_state is None:
            if not self._selectable_storage_enabled:
                self._initialize_local_runtime_state()
                assert self._world_state is not None
                return self._world_state
            # Fail closed: under the selected backend there is no local legacy
            # snapshot, and fabricating an empty WorldState would present a
            # blank world as if it were a real one.
            raise RuntimeError(
                "SelectedWorldStateHasNoLegacySnapshot: read the World Port"
            )
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
            if not self._selectable_storage_enabled:
                self._initialize_local_runtime_state()
                assert self._consciousness_registry is not None
                return self._consciousness_registry
            raise RuntimeError(
                "SelectedPresenceNotStarted: await LifeEngineService.start() first"
            )
        return self._consciousness_registry

    def save_consciousness_registry(self) -> None:
        """Persist the disabled-mode compatibility registry synchronously."""

        if self._selectable_storage_enabled:
            raise RuntimeError(
                "SelectedPresenceRequiresAwait: use save_consciousness_registry_async()"
            )
        registry = self.consciousness_registry
        assert isinstance(registry, ConsciousnessRegistry)
        registry.save(self._workspace_dir() / "runtime" / "consciousness_registry.json")
        registry.flush_lifecycle_events(self._get_event_bus().store.append_sync)
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

        async with self._proactive_actor_gate.hold(instance.instance_id):
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

        async with self._proactive_actor_gate.hold(instance_id):
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

        async with self._proactive_actor_gate.hold(instance_id):
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

        async with self._proactive_actor_gate.hold(instance_id):
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
                await self._clear_instance_attention_focus(instance_id)
            return changed

    async def terminate_consciousness_instance(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "requested",
    ) -> bool:
        """Commit termination and release stream ownership before returning."""

        async with self._proactive_actor_gate.hold(instance_id):
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
                await self._clear_instance_attention_focus(instance_id)
            return changed

    async def _clear_instance_attention_focus(self, instance_id: str) -> None:
        """Release ephemeral focus without mutating subject thread authority."""

        proactive = getattr(self, "_proactive_authority", None)
        if proactive is None:
            return
        try:
            focus = await proactive.get_attention_focus(instance_id)
            if focus is not None:
                await proactive.clear_attention_focus(
                    instance_id,
                    expected_revision=focus.revision,
                )
        except Exception as exc:  # noqa: BLE001 - lifecycle cleanup is best effort
            logger.warning(
                "清理意识实例临时关注焦点失败: "
                f"instance_id={instance_id}, error={type(exc).__name__}"
            )

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

    @property
    def proactive_authority(self) -> ProactiveAuthority:
        """Return the one live proactive authority or fail closed."""

        authority = self._proactive_authority
        if authority is None:
            raise RuntimeError("ProactiveAuthorityNotStarted")
        return authority

    async def decide_initiative_seed(
        self,
        command: InitiativeSeedCommand,
    ) -> InitiativeSeedCommit:
        """Commit one explicit subject decision without choosing a route."""

        return await self.proactive_authority.decide_initiative(command)

    async def _initiative_life_event_exists(self, event_id: str) -> bool:
        """Check one immutable occurrence without materializing event content."""

        store = self._get_event_bus().store
        occurrence_digest = getattr(store, "occurrence_digest", None)
        if callable(occurrence_digest):
            return await occurrence_digest(event_id) is not None
        get_by_event_id = getattr(store, "get_by_event_id", None)
        if callable(get_by_event_id):
            return await get_by_event_id(event_id) is not None
        return False

    @staticmethod
    def _initiative_reencounter_event_id(seed: InitiativeSeedView) -> str:
        digest = hashlib.sha256(
            f"{seed.seed_id}\0{seed.reencounter_revision}".encode()
        ).hexdigest()
        return f"initiative_reencounter_{digest}"

    async def _surface_initiative_reencounter(
        self,
        seed: InitiativeSeedView,
    ) -> None:
        """Durably re-present one subject-authored seed without taking action."""

        if (
            seed.status != "open"
            or not seed.reencounter_at
            or seed.reencounter_revision <= 0
            or seed.reencounter_delivered_at
        ):
            return
        event_id = self._initiative_reencounter_event_id(seed)
        if not await self._initiative_life_event_exists(event_id):
            from ..initiative.projection import project_initiative_seed_content

            content = str(project_initiative_seed_content(seed))
            event = LifeEngineEvent(
                event_id=event_id,
                event_type=EventType.MESSAGE,
                timestamp=seed.reencounter_at,
                sequence=self._next_sequence(),
                source="life_engine",
                source_detail="主体主动线索的一次性技术投递",
                content=content,
                content_type="initiative_reencounter",
                sender="主体先前的明确决定",
                occurrence_id=event_id,
                causation_id=seed.reencounter_event_id or seed.last_event_id,
                correlation_id=(
                    f"initiative-seed:{hashlib.sha256(seed.seed_id.encode()).hexdigest()}"
                ),
                content_ref=seed.reencounter_event_id or seed.last_event_id,
                raw_content=content,
            )
            await self._queue_pending_event(event)
        await self.proactive_authority.record_reencounter_delivery(
            seed_id=seed.seed_id,
            seed_revision=seed.reencounter_revision,
            life_event_id=event_id,
            occurred_at=_now_iso(),
        )

    async def _initiative_reencounter_loop(self) -> None:
        """Replay pending initiative deliveries without inferring subject intent."""

        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                proactive = self.proactive_authority
                await self._flush_pending_initiative_expression_resolutions()
                pending = await proactive.pending_outreach(limit=1)
                for item in pending:
                    await self._deliver_pending_initiative_outreach(item.command)
                for record in self._inner_dialogue_store().pending_wakes()[:1]:
                    await self._deliver_pending_inner_return(record)
                expression_pending = await proactive.pending_expression_outreach(
                    limit=1
                )
                for item in expression_pending:
                    occurrence_id = item.command.occurrence_id
                    active_claims = getattr(
                        self,
                        "_active_initiative_expression_claims",
                        set(),
                    )
                    deferred = getattr(
                        self,
                        "_pending_initiative_expression_resolutions",
                        {},
                    )
                    if occurrence_id in deferred or occurrence_id in active_claims:
                        continue
                    if item.status == "processing":
                        # The immutable claim owns a DB-clock lease.  A scan can
                        # observe the committed claim before the caller adds it
                        # to the process-local active set, so absence from that
                        # set is never sufficient recovery proof.  Only an
                        # expired lease may become delivery_unknown.
                        if not item.claim_expired:
                            continue
                        await self.resolve_initiative_outreach_expressions(
                            [occurrence_id],
                            outcome="delivery_unknown",
                            action_id=item.claimed_action_id,
                        )
                        continue
                    await self._wake_stream_for_initiative(
                        stream_id=item.stream_id,
                        platform=item.platform,
                        command=item.command,
                        trigger_message_id=item.trigger_message_id,
                        turn_id=item.turn_id,
                    )
                due = await proactive.due_reencounters(now=_now_iso())
                # Technical delivery order is stable ledger order, never a
                # salience judgment. One event per pass prevents a recovered
                # backlog from flooding the next heartbeat context.
                for seed in due[:1]:
                    await self._surface_initiative_reencounter(seed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry durable projection
                logger.warning(
                    "主体主动线索技术投递暂未完成: "
                    f"error_type={type(exc).__name__}"
                )
            stop_event = self._stop_event
            if stop_event is None or stop_event.is_set():
                return
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=15.0)
            except TimeoutError:
                pass

    async def list_initiative_seeds(
        self,
        *,
        include_released: bool = False,
    ) -> tuple[InitiativeSeedView, ...]:
        """Read initiatives in immutable event order, never salience order."""

        return await self.proactive_authority.list_initiatives(
            include_released=include_released
        )

    async def get_initiative_seed(
        self,
        seed_id: str,
    ) -> InitiativeSeedView | None:
        """Read one exact initiative view without inferring an audience."""

        return await self.proactive_authority.get_initiative(seed_id)

    @staticmethod
    def _initiative_outreach_trigger_message_id(
        outreach_occurrence_id: str,
    ) -> str:
        digest = hashlib.sha256(
            str(outreach_occurrence_id or "").strip().encode("utf-8")
        ).hexdigest()
        return f"initiative_outreach_{digest}"

    async def claim_initiative_outreach_expressions(
        self,
        outreach_occurrence_ids: list[str] | tuple[str, ...],
        *,
        action_id: str,
    ) -> dict[str, Any]:
        """Fence visible expression actions before any platform side effect.

        This is a technical at-most-once gate.  It never creates an initiative
        or decides what to say.  Losing a successful claim return intentionally
        becomes ``delivery_unknown`` instead of authorizing a duplicate send.
        """

        occurrences = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in outreach_occurrence_ids
                if str(item or "").strip()
            )
        )
        exact_action_id = str(action_id or "").strip()
        if not occurrences:
            return {"claimed": True, "claim_count": 0, "execute_allowed": True}
        if not exact_action_id:
            raise ValueError("initiative outreach action_id is required")
        active_claims = getattr(
            self,
            "_active_initiative_expression_claims",
            None,
        )
        if active_claims is None:
            active_claims = set()
            self._active_initiative_expression_claims = active_claims

        claimed: list[str] = []
        claim_epochs: dict[str, int] = {}
        try:
            for occurrence_id in occurrences:
                receipt = await self.proactive_authority.claim_outreach_expression(
                    outreach_occurrence_id=occurrence_id,
                    action_id=exact_action_id,
                    claim_owner=self._proactive_claim_owner,
                    lease_seconds=int(
                        self._cfg().proactive.expression_claim_lease_seconds
                    ),
                    occurred_at=_now_iso(),
                )
                claim_epochs[occurrence_id] = receipt.claim_epoch
                if not receipt.execute_allowed:
                    if claimed:
                        await self.resolve_initiative_outreach_expressions(
                            claimed,
                            outcome="failed",
                            action_id=exact_action_id,
                        )
                    return {
                        "claimed": False,
                        "claim_count": len(claimed),
                        "execute_allowed": False,
                        "reason": "claim_replayed",
                        "claim_epochs": claim_epochs,
                    }
                claimed.append(occurrence_id)
                active_claims.add(occurrence_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            if claimed:
                await self.resolve_initiative_outreach_expressions(
                    claimed,
                    outcome="failed",
                    action_id=exact_action_id,
                )
            raise
        return {
            "claimed": True,
            "claim_count": len(claimed),
            "execute_allowed": True,
            "claim_epochs": claim_epochs,
        }

    async def resolve_initiative_outreach_expressions(
        self,
        outreach_occurrence_ids: list[str] | tuple[str, ...],
        *,
        outcome: InitiativeOutreachOutcome,
        action_id: str = "",
        delivery_receipt_sha256: str = "",
        delivery_message_id: str = "",
    ) -> dict[str, Any]:
        """Persist terminal expression outcomes without fabricating success."""

        occurrences = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in outreach_occurrence_ids
                if str(item or "").strip()
            )
        )
        exact_action_id = str(action_id or "").strip()
        exact_delivery_receipt = str(delivery_receipt_sha256 or "").strip()
        exact_delivery_message_id = str(delivery_message_id or "").strip()
        active_claims = getattr(
            self,
            "_active_initiative_expression_claims",
            None,
        )
        if active_claims is None:
            active_claims = set()
            self._active_initiative_expression_claims = active_claims
        deferred = getattr(
            self,
            "_pending_initiative_expression_resolutions",
            None,
        )
        if deferred is None:
            deferred = {}
            self._pending_initiative_expression_resolutions = deferred

        resolved = 0
        errors: dict[str, str] = {}
        for occurrence_id in occurrences:
            occurred_at = _now_iso()
            try:
                await self.proactive_authority.resolve_outreach_expression(
                    outreach_occurrence_id=occurrence_id,
                    outcome=outcome,
                    action_id=exact_action_id,
                    delivery_receipt_sha256=exact_delivery_receipt,
                    delivery_message_id=exact_delivery_message_id,
                    occurred_at=occurred_at,
                )
            except asyncio.CancelledError:
                deferred[occurrence_id] = (
                    outcome,
                    exact_action_id,
                    occurred_at,
                    exact_delivery_receipt,
                    exact_delivery_message_id,
                )
                active_claims.discard(occurrence_id)
                raise
            except Exception as exc:  # noqa: BLE001 - durable retry below
                deferred[occurrence_id] = (
                    outcome,
                    exact_action_id,
                    occurred_at,
                    exact_delivery_receipt,
                    exact_delivery_message_id,
                )
                errors[occurrence_id] = type(exc).__name__
                logger.warning(
                    "主体主动外联终态暂未落账，将后台重试: "
                    f"error_type={type(exc).__name__}"
                )
            else:
                deferred.pop(occurrence_id, None)
                resolved += 1
            finally:
                active_claims.discard(occurrence_id)
        return {
            "resolved_count": resolved,
            "pending_count": len(errors),
            "error_types": tuple(sorted(set(errors.values()))),
        }

    async def _flush_pending_initiative_expression_resolutions(self) -> None:
        deferred = getattr(
            self,
            "_pending_initiative_expression_resolutions",
            None,
        )
        if not deferred:
            return
        for occurrence_id, (
            outcome,
            action_id,
            occurred_at,
            delivery_receipt_sha256,
            delivery_message_id,
        ) in tuple(
            deferred.items()
        )[:1]:
            try:
                await self.proactive_authority.resolve_outreach_expression(
                    outreach_occurrence_id=occurrence_id,
                    outcome=outcome,
                    action_id=action_id,
                    delivery_receipt_sha256=delivery_receipt_sha256,
                    delivery_message_id=delivery_message_id,
                    occurred_at=occurred_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retried next pass
                logger.debug(
                    "主体主动外联终态后台重试未完成: "
                    f"error_type={type(exc).__name__}"
                )
                return
            deferred.pop(occurrence_id, None)

    async def begin_initiative_outreach(
        self,
        command: InitiativeOutreachCommand,
    ) -> dict[str, Any]:
        """Commit an explicit audience/surface choice, then wake that surface."""

        from ..initiative.reachability import resolve_reachable_surface

        surface = await resolve_reachable_surface(
            audience_ref=command.audience_ref,
            surface_ref=command.surface_ref,
        )
        receipt = await self.proactive_authority.begin_outreach(command)
        trigger_message_id = self._initiative_outreach_trigger_message_id(
            command.occurrence_id
        )
        result: dict[str, Any] = {
            "authority_committed": True,
            "inbox_committed": False,
            "expression_wake_enqueued": False,
            "delivery_pending": True,
            "expression_pending": True,
            "message_sent": False,
            "event_id": receipt.event_id,
            "occurrence_id": receipt.occurrence_id,
            "audience_ref": receipt.audience_ref,
            "surface_ref": receipt.surface_ref,
            "idempotent_replay": receipt.idempotent_replay,
        }
        try:
            delivery = await self.proactive_authority.record_outreach_delivery(
                outreach_occurrence_id=command.occurrence_id,
                stream_id=surface.stream_id,
                trigger_message_id=trigger_message_id,
                occurred_at=_now_iso(),
                platform=surface.platform,
            )
            result.update(
                inbox_committed=True,
                delivery_event_id=delivery.event_id,
                delivery_idempotent_replay=delivery.idempotent_replay,
                expression_pending=not delivery.expression_resolved,
                delivery_pending=not delivery.expression_resolved,
            )
            if delivery.expression_resolved:
                result["expression_outcome"] = delivery.expression_outcome
                return result
            await self._wake_stream_for_initiative(
                stream_id=surface.stream_id,
                platform=surface.platform,
                command=command,
                trigger_message_id=trigger_message_id,
                turn_id=delivery.turn_id,
            )
            result["expression_wake_enqueued"] = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - authority commit is durable
            result["delivery_error_type"] = type(exc).__name__
            logger.warning(
                "主体主动外联已提交，表达层将在后台重放: "
                f"error_type={type(exc).__name__}"
            )
        return result

    async def _deliver_pending_initiative_outreach(
        self,
        command: InitiativeOutreachCommand,
    ) -> None:
        """Idempotently hand one committed outreach to the expression inbox."""

        from ..initiative.reachability import resolve_reachable_surface

        surface = await resolve_reachable_surface(
            audience_ref=command.audience_ref,
            surface_ref=command.surface_ref,
        )
        trigger_message_id = self._initiative_outreach_trigger_message_id(
            command.occurrence_id
        )
        delivery = await self.proactive_authority.record_outreach_delivery(
            outreach_occurrence_id=command.occurrence_id,
            stream_id=surface.stream_id,
            trigger_message_id=trigger_message_id,
            occurred_at=_now_iso(),
            platform=surface.platform,
        )
        if delivery.expression_resolved:
            return
        await self._wake_stream_for_initiative(
            stream_id=surface.stream_id,
            platform=surface.platform,
            command=command,
            trigger_message_id=trigger_message_id,
            turn_id=delivery.turn_id,
        )

    async def decide_attention_thread(
        self,
        command: AttentionThreadCommand,
    ) -> AttentionThreadCommit:
        """Submit one explicit subject decision to the canonical authority."""

        commit = await self.proactive_authority.decide_attention(command)
        if (
            command.action == "close"
            and not commit.idempotent_replay
            and self._learning_scheduler is not None
        ):
            try:
                await self._learning_scheduler.on_attention_thread_closed(
                    public_statement=command.public_statement,
                    source_event_ids=[
                        commit.event_id,
                        *command.source_occurrence_ids,
                    ],
                    actor_consciousness_instance_id=(
                        command.actor_consciousness_instance_id
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - authority already committed
                logger.warning(
                    f"持续关注线索已提交，但派生学习未完成: error={type(exc).__name__}"
                )
        return commit

    async def page_attention_threads(
        self,
        query: AttentionThreadPageQuery,
    ) -> AttentionThreadPage:
        """Read a bounded traceable subject-attention projection."""

        return await self.proactive_authority.page_attention(query)

    async def set_instance_attention_focus(
        self,
        focus: InstanceFocus,
    ) -> InstanceFocus:
        """Set technical instance focus without changing a subject thread."""

        return await self.proactive_authority.set_attention_focus(focus)

    async def _open_selected_storage_runtime(self) -> None:
        """Open the selected runtime exactly once under service ownership."""

        if not self._selectable_storage_enabled or self._storage_runtime is not None:
            return
        from ..storage.factory import open_storage_backend

        environment: dict[str, str] | None = None
        settings = self._storage_factory_settings
        if settings.authority_provider == "file":
            settings, environment = await self._activate_local_file_authority(
                settings
            )
            self._storage_factory_settings = settings
        if environment is None:
            self._storage_runtime = await open_storage_backend(settings)
        else:
            self._storage_runtime = await open_storage_backend(
                settings,
                environment=environment,
            )

    async def _activate_local_file_authority(
        self,
        settings: Any,
    ) -> tuple[Any, dict[str, str]]:
        """Activate the local file authority for this single writer process.

        本地 selectable 模式沿用 LocalProactiveRuntime 的生产模式：generation
        由 bootstrap 预先注册并验证，这里只以进程唯一 owner 激活。入口级
        ``data/runtime/elysium.lock`` 已由 main 持有，同机不存在第二个主写
        者；注册表中残留的活动租约只可能属于已崩溃的前任，confirm 接管
        推进 epoch 后旧 token 立即失效。
        """

        from ..storage.authority import (
            AuthorityConflict,
            FileAuthorityRegistry,
            GenerationNotVerified,
        )

        registry = FileAuthorityRegistry(
            settings.local.authority_state_path,
            registry_id=settings.registry_id,
        )
        generation = await registry.get_generation(settings.backend_generation)
        if generation is None:
            raise RuntimeError(
                "LocalSelectableGenerationNotRegistered: "
                f"{settings.backend_generation} is not in the local authority "
                "registry; run scripts/bootstrap_local_selectable.py first"
            )
        if generation.backend.value != settings.authoritative_backend.value:
            raise RuntimeError(
                "LocalSelectableGenerationBackendMismatch: "
                f"{settings.backend_generation}"
            )
        owner_id = f"{settings.authority_owner_id}:pid-{os.getpid()}"
        lease_seconds = int(settings.authority_lease_seconds)
        deadline = time.monotonic() + lease_seconds + 5.0
        while True:
            health = await registry.health()
            if str(health.get("status") or "") == "failed":
                raise RuntimeError(
                    "LocalSelectableAuthorityRegistryUnavailable"
                )
            active = str(health.get("active_generation") or "")
            try:
                token = await registry.activate_generation(
                    settings.backend_generation,
                    expected_epoch=int(health.get("authority_epoch") or 0),
                    owner_id=owner_id,
                    lease_seconds=lease_seconds,
                    confirm_previous_writers_stopped=bool(active),
                )
                break
            except asyncio.CancelledError:
                raise
            except (AuthorityConflict, GenerationNotVerified) as exc:
                # epoch 竞态（并发启动已被实例锁排除，这里是注册表读写竞态）
                # 或验证状态变化：有界重试读取最新状态，不删除、不抢写。
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "LocalSelectableAuthorityActivationTimeout"
                    ) from exc
                await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        activated = replace(
            settings,
            authority_epoch=int(token.authority_epoch),
            authority_owner_id=owner_id,
            fencing_token_env=_LIFE_LOCAL_FENCING_ENV,
        )
        return activated, {
            _LIFE_LOCAL_FENCING_ENV: str(token.fencing_token)
        }

    async def _start_local_proactive_authority(self) -> None:
        """Open canonical proactive storage without enabling other life domains."""

        if self._selectable_storage_enabled or self._proactive_authority is not None:
            return
        from ..proactive.runtime import open_local_proactive_runtime

        owned = await open_local_proactive_runtime(
            workspace_path=self._cfg().settings.workspace_path,
            config=self._cfg().proactive,
            validate_active_actor=self._validate_initiative_decision_actor,
            actor_decision_guard=self._proactive_actor_gate.hold,
        )
        self._local_proactive_runtime = owned
        self._proactive_authority = owned.authority
        self._proactive_health_cache = await owned.health_snapshot()
        logger.info(
            "统一主动权威已启动: backend=local authority_count=1 "
            "legacy_thought_stream=archive_only"
        )

    async def _record_proactive_delivery_proof(
        self,
        message: Any,
        receipt: dict[str, Any],
    ) -> bool:
        """Persist exact platform acknowledgement for a claimed outreach.

        Core transport owns the physical side effect and invokes this callback
        only after the adapter acknowledgement (or virtual-history commit).
        The proactive authority then checks the immutable inbox/turn/claim
        chain before accepting the proof; callers cannot promote an arbitrary
        64-character value into a spoken outcome.
        """

        extra = getattr(message, "extra", None)
        if not isinstance(extra, dict):
            raise RuntimeError("ProactiveDeliveryProofMetadataMissing")
        raw_occurrences = extra.get("initiative_outreach_occurrences")
        if not isinstance(raw_occurrences, list):
            raise RuntimeError("ProactiveDeliveryProofOccurrencesMissing")
        occurrences = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in raw_occurrences
                if str(item or "").strip()
            )
        )
        action_id = str(extra.get("tool_call_id") or "").strip()
        if not occurrences or not action_id:
            raise RuntimeError("ProactiveDeliveryProofIdentityIncomplete")
        authority = self._proactive_authority
        if authority is None:
            raise RuntimeError("ProactiveAuthorityNotStarted")
        occurred_at = _now_iso()
        for occurrence_id in occurrences:
            await authority.record_outreach_delivery_proof(
                outreach_occurrence_id=occurrence_id,
                action_id=action_id,
                delivery_receipt=dict(receipt),
                occurred_at=occurred_at,
            )
        return True

    def _attach_proactive_delivery_proof_hook(self) -> None:
        """Attach the one transport-to-authority delivery-proof bridge."""

        if self._proactive_delivery_proof_hook is not None:
            return
        from src.core.transport.multi_writer_hooks import (
            register_outbound_delivery_proof_hook,
        )

        hook = self._record_proactive_delivery_proof
        register_outbound_delivery_proof_hook(hook)
        self._proactive_delivery_proof_hook = hook

    def _detach_proactive_delivery_proof_hook(self) -> None:
        """Release only this service's exact registered callback."""

        hook = self._proactive_delivery_proof_hook
        if hook is None:
            return
        from src.core.transport.multi_writer_hooks import (
            unregister_outbound_delivery_proof_hook,
        )

        unregister_outbound_delivery_proof_hook(hook)
        self._proactive_delivery_proof_hook = None

    async def _refresh_proactive_health(self) -> dict[str, Any]:
        """Refresh the content-free health cache for either storage mode."""

        owned = self._local_proactive_runtime
        if owned is not None:
            self._proactive_health_cache = await owned.health_snapshot()
            return dict(self._proactive_health_cache)
        authority = self._proactive_authority
        if authority is not None:
            self._proactive_health_cache = await authority.health_snapshot()
            self._proactive_health_cache["backend"] = (
                self._storage_factory_settings.authoritative_backend.value
            )
            return dict(self._proactive_health_cache)
        self._proactive_health_cache = {
            "component": "proactive_authority",
            "status": "failed",
            "authority_count": 0,
            "reason": "authority_not_started",
        }
        return dict(self._proactive_health_cache)

    async def _close_local_proactive_authority(self) -> None:
        """Close the owned local authority after all proactive consumers stop."""

        owned = self._local_proactive_runtime
        if owned is None:
            return
        self._detach_proactive_delivery_proof_hook()
        self._local_proactive_runtime = None
        self._proactive_authority = None
        try:
            await owned.close()
        finally:
            self._proactive_health_cache = {
                "component": "proactive_authority",
                "status": "disabled",
                "authority_count": 0,
                "reason": "service_stopped",
            }

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
        from ..storage.attention_factory import open_attention_thread_stores
        from ..storage.domain_factory import open_presence_world_stores
        from ..storage.event_factory import open_life_event_store
        from ..storage.initiative_factory import open_initiative_record_store
        from ..storage.learning_factory import open_learning_stores
        from ..storage.proactive_decision_guard import (
            reconcile_proactive_decision_guards,
        )
        from ..storage.runtime_factory import open_runtime_state_store
        from ..storage.subject_factory import open_subject_document_store

        await self._open_selected_storage_runtime()
        runtime = self.storage_runtime
        from ..proactive.backend_binding import ensure_proactive_backend_binding

        await ensure_proactive_backend_binding(
            workspace_path=self._cfg().settings.workspace_path,
            binding_path=self._cfg().proactive.backend_binding_path,
            runtime=runtime,
        )
        multi_writer_enabled = bool(self._storage_factory_settings.multi_writer_enabled)
        claim_retry_interval_seconds = 1.0
        claim_deadline = (
            time.monotonic()
            + self._storage_factory_settings.authority_lease_seconds
            + claim_retry_interval_seconds
        )

        async def _acquire_writer_claim(
            *,
            namespace: str,
            state_key: str,
            required: bool = True,
        ) -> Any:
            """Wait for one crash-left lease without weakening DB-time fencing.

            When ``required`` is False (multi-writer second instance), a claim
            that stays leased past the deadline degrades to ``None`` instead of
            failing the whole plugin startup.  The owning instance keeps the
            projection/maintenance writer; non-owners append immutable events
            through the unclaimed handle only.

            MySQL error 1205 (row-lock wait timeout) is semantically identical
            to ``SingletonWriterClaimConflict`` here: two nodes racing the same
            singleton claim row with ``SELECT ... FOR UPDATE`` make the waiting
            session abort after the session ``innodb_lock_wait_timeout``.  It
            therefore feeds the same wait-then-degrade path instead of failing
            plugin startup.
            """

            from sqlalchemy.exc import OperationalError as SAOperationalError

            from ..storage import SingletonWriterClaimConflict

            def _is_lock_wait_timeout(exc: BaseException) -> bool:
                """Detect MySQL 1205 row-lock wait timeout (transient contention)."""
                if not isinstance(exc, SAOperationalError):
                    return False
                orig = getattr(exc, "orig", None)
                try:
                    return int(orig.args[0]) == 1205
                except (AttributeError, TypeError, IndexError, ValueError):
                    return False

            logged_wait = False
            while True:
                try:
                    return await runtime.acquire_singleton_writer(
                        namespace=namespace,
                        state_key=state_key,
                        owner_instance_id=self._storage_writer_instance_id,
                        lease_seconds=(
                            self._storage_factory_settings.authority_lease_seconds
                        ),
                    )
                except SingletonWriterClaimConflict as exc:
                    last_exc = exc
                except SAOperationalError as exc:
                    if not _is_lock_wait_timeout(exc):
                        raise
                    last_exc = exc
                remaining = claim_deadline - time.monotonic()
                if remaining <= 0:
                    if required:
                        raise last_exc
                    logger.warning(
                        "selected storage writer claim is not owned by this "
                        "instance; degrading singleton domain to avoid blocking "
                        f"plugin startup: namespace={namespace} "
                        f"state_key={state_key} error_type={type(last_exc).__name__}"
                    )
                    return None
                if not logged_wait:
                    logger.warning(
                        "selected storage writer claim is still leased; "
                        f"waiting up to {remaining:.1f}s for DB-time takeover: "
                        f"namespace={namespace} state_key={state_key}"
                    )
                    logged_wait = True
                await asyncio.sleep(min(claim_retry_interval_seconds, remaining))

        if multi_writer_enabled:
            from ..storage.multi_writer_protocol import (
                MULTI_WRITER_HOT_PATHS_READY,
                MULTI_WRITER_PROTOCOL_VERSION,
                MultiWriterProtocolConfig,
                observe_multi_writer_state,
                validate_multi_writer_readiness,
            )

            observed = await observe_multi_writer_state(runtime)
            generation_schema_version = int(
                getattr(getattr(runtime, "generation", None), "schema_version", 0)
            )
            # Generation 尚无独立持久化的 protocol_version 字段，观测值只能用
            # 工程内固定协议合同版本，不得用节点配置自证；配置与合同不一致时
            # 由 MultiWriterProtocolConfig.validate() 拒绝。
            validate_multi_writer_readiness(
                config=MultiWriterProtocolConfig(
                    protocol_version=(
                        self._storage_factory_settings.multi_writer_protocol_version
                    ),
                    require_singleton_retired=True,
                    allow_legacy_global_snapshot_writer=False,
                ),
                generation_schema_version=generation_schema_version,
                observed_protocol_version=MULTI_WRITER_PROTOCOL_VERSION,
                singleton_retired=observed.legacy_singleton_retired,
                hot_paths_ready=MULTI_WRITER_HOT_PATHS_READY,
            )
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
        runtime_state_store = await open_runtime_state_store(
            runtime,
            initialize_schema=False,
        )
        runtime_context_writer_claim = None
        if not multi_writer_enabled:
            runtime_context_writer_claim = await _acquire_writer_claim(
                namespace="life_engine.runtime_context",
                state_key="global",
            )
        attention_stores = await open_attention_thread_stores(
            runtime,
            initialize_schema=False,
        )
        from ..attention_threads import AttentionThreadService

        attention_service = AttentionThreadService(
            attention_stores.authority,
            attention_stores.focus,
        )
        # Business startup never creates schema. Missing schema is detected by
        # the bounded startup probes and the first actual domain read; avoid a
        # duplicate full-table health sweep before all stores are attached.
        learning_cfg = getattr(self._cfg(), "learning", None)
        learning_stores = None
        learning_event_store = None
        if learning_cfg is None or getattr(learning_cfg, "enabled", True):
            from ..storage.learning_contracts import (
                LEARNING_WRITER_CLAIM_NAMESPACE,
                LEARNING_WRITER_CLAIM_STATE_KEY,
            )

            # Every generation writer may append immutable learning evidence
            # through the unclaimed handle. Only this service-owned singleton
            # projector may update global selected projections/maintenance.
            learning_event_stores = await open_learning_stores(
                runtime,
                initialize_schema=False,
                writer_claim=None,
            )
            learning_event_store = learning_event_stores.store
            learning_writer_claim = await _acquire_writer_claim(
                namespace=LEARNING_WRITER_CLAIM_NAMESPACE,
                state_key=LEARNING_WRITER_CLAIM_STATE_KEY,
                required=False,
            )
            # Multi-writer: when another instance owns the singleton learning
            # projector claim, this instance degrades the mutable projection
            # domain instead of failing plugin startup.  Immutable learning
            # events stay appendable through the unclaimed handle above.
            if learning_writer_claim is None:
                logger.warning(
                    "learning projector claim not acquired; this instance "
                    "appends immutable learning events but does not own "
                    "selected projections/maintenance"
                )
                learning_stores = None
            else:
                learning_stores = await open_learning_stores(
                    runtime,
                    initialize_schema=False,
                    writer_claim=learning_writer_claim,
                )
        else:
            learning_writer_claim = None
        registry = await AsyncConsciousnessRegistry.load(stores.presence)
        initiative_records = await open_initiative_record_store(runtime)
        await reconcile_proactive_decision_guards(runtime)
        proactive_authority = ProactiveAuthority(
            attention=attention_service,
            initiative=initiative_records,
        )
        event_bus = LifeEventBus(ledger)
        gateway = AsyncPerceptionGateway(
            registry,
            ledger,
            stores.world,
        )
        await flush_presence_lifecycle_events(stores.presence, ledger)
        await gateway.catch_up()
        self._life_event_store = ledger
        self._learning_stores = learning_stores
        self._learning_event_store = learning_event_store
        self._proactive_authority = proactive_authority
        self._presence_world_stores = stores
        self._subject_document_store = subject_store
        self._runtime_state_store = runtime_state_store
        self._runtime_context_writer_claim = runtime_context_writer_claim
        self._learning_writer_claim = learning_writer_claim
        self._proactive_health_cache = {
            "component": "proactive_authority",
            "status": "healthy",
            "authority_count": 1,
            "backend": runtime.backend.value,
        }
        self._storage_renewal_health = {
            "status": "healthy",
            "last_success_at": "",
            "next_retry_at": "",
            "error_type": "",
            "consecutive_failures": 0,
        }
        self._learning_storage_health = {
            "status": "healthy" if learning_writer_claim is not None else "degraded",
            "projector_owner": learning_writer_claim is not None,
            "event_append_available": learning_event_store is not None,
            "reason": (
                "singleton learning projector is owned by this instance"
                if learning_writer_claim is not None
                else "immutable events only; singleton projector is owned elsewhere"
            ),
        }
        from ..storage.models import BackendKind
        from ..storage.subject_workspace import (
            SubjectWorkspaceObserver,
            SubjectWorkspaceProjector,
        )

        if runtime.backend == BackendKind.LOCAL:
            data_root = Path(self._cfg().settings.workspace_path).resolve().parent
            self._subject_workspace_observer = SubjectWorkspaceObserver(
                subject_store,
                data_root=data_root,
                recorded_source="workspace:selected-local",
            )
            self._subject_workspace_projector = SubjectWorkspaceProjector(
                subject_store,
                data_root=data_root,
                worker_id=f"{self._storage_writer_instance_id}:subject-projector",
            )
            self._subject_projection_health = {
                "status": "initializing",
                "last_success_at": "",
                "last_error_type": "",
            }
        else:
            # MySQL keeps subject bytes in the selected store and does not attach
            # a local workspace observer/projector.
            self._subject_workspace_observer = None
            self._subject_workspace_projector = None
            self._subject_projection_health = {
                "status": "disabled",
                "last_success_at": "",
                "last_error_type": "",
                "reason": "mysql subject authority has no local workspace projector",
            }
        self._consciousness_registry = registry
        self._event_bus = event_bus
        self._world_projection = stores.world
        self._perception_gateway = gateway
        if multi_writer_enabled:
            await self._attach_multi_writer_bridge(runtime)
        self._storage_health_cache = {
            "status": (
                "healthy" if learning_writer_claim is not None else "degraded"
            ),
            "backend": self._storage_factory_settings.authoritative_backend.value,
            "reason": "selected storage startup probes completed; full health is on demand",
            "authority_renewal": dict(self._storage_renewal_health),
            "learning": dict(self._learning_storage_health),
        }

    async def _attach_multi_writer_bridge(self, runtime: Any) -> None:
        """Create the hot-path bridge and register core transport hooks.

        The bridge is the only surface through which production hot paths
        touch the multi-writer stores.  It is registered into the core
        transport hook slots so inbound facts and outbox intents are recorded
        without core depending on the plugin; shutdown unregisters them.
        """
        from src.core.transport.multi_writer_hooks import (
            register_inbound_fact_hook,
            register_outbox_intent_hook,
            register_outbox_settle_hook,
        )

        from ..storage import (
            MULTI_WRITER_PROTOCOL_VERSION,
            InstanceIdentity,
            MultiWriterHotPathBridge,
            compute_config_digest,
            generate_boot_id,
        )

        identity = InstanceIdentity(
            deployment_id=str(
                self._storage_factory_settings.authority_owner_id or "elysium"
            ),
            instance_id=self._storage_writer_instance_id,
            boot_id=generate_boot_id(),
            owner_id=str(
                self._storage_factory_settings.authority_owner_id or "elysium"
            ),
            protocol_version=int(MULTI_WRITER_PROTOCOL_VERSION),
            schema_generation=str(
                getattr(getattr(runtime, "generation", None), "generation_id", "")
                or "unknown"
            ),
            config_digest=compute_config_digest(
                {
                    "backend": self._storage_factory_settings.authoritative_backend.value,
                    "backend_generation": self._storage_factory_settings.backend_generation,
                    "protocol_version": self._storage_factory_settings.multi_writer_protocol_version,
                    "registry_id": self._storage_factory_settings.registry_id,
                    "schema_version": self._storage_factory_settings.schema_version,
                }
            ),
            workspace_revision="workspace-v1",
        )
        identity.validate()
        bridge = MultiWriterHotPathBridge(runtime, identity)
        self._multi_writer_bridge = bridge
        self._inbound_fact_hook = bridge.record_inbound_message
        self._outbox_intent_hook = bridge.enqueue_outbox_action
        self._outbox_settle_hook = bridge.settle_outbox_action
        register_inbound_fact_hook(self._inbound_fact_hook)
        register_outbox_intent_hook(self._outbox_intent_hook)
        register_outbox_settle_hook(self._outbox_settle_hook)
        logger.info(
            "multi-writer hot-path bridge attached: "
            f"owner={identity.short_owner}, node={bridge.node_id}"
        )

    def _new_event_only_learning_recorder(
        self,
        *,
        reason: str,
        error_type: str = "",
    ) -> Any:
        """Build the canonical immutable-event-only Learning consumer."""

        if self._learning_event_store is None:
            raise RuntimeError("LearningEventStoreUnavailable")
        from ..learning.event_only import LearningEventOnlyRecorder

        return LearningEventOnlyRecorder(
            self._learning_event_store,
            writer_instance_id=self._storage_writer_instance_id,
            reason=reason,
            error_type=error_type,
        )

    def _build_learning_runtime(self, **scheduler_kwargs: Any) -> Any:
        """Choose one selected projector or the immutable event-only consumer."""

        if self._selectable_storage_enabled and self._learning_stores is None:
            return self._new_event_only_learning_recorder(
                reason="immutable events only; singleton projector is not owned",
                error_type="SingletonWriterClaimConflict",
            )
        from ..learning.scheduler import LearningScheduler

        return LearningScheduler(**scheduler_kwargs)

    def _cache_storage_renewal_state(
        self,
        *,
        status: str,
        reason: str,
    ) -> None:
        cache = dict(self._storage_health_cache)
        cache["status"] = status
        cache["backend"] = (
            self._storage_factory_settings.authoritative_backend.value
        )
        cache["reason"] = reason
        cache["authority_renewal"] = dict(self._storage_renewal_health)
        cache["learning"] = dict(self._learning_storage_health)
        if self._lost_singleton_health:
            cache["lost_singletons"] = [
                dict(item)
                for _, item in sorted(self._lost_singleton_health.items())
            ]
        self._storage_health_cache = cache

    async def _quiesce_learning_projector(
        self,
        *,
        reason: str,
        error_type: str,
    ) -> None:
        """Stop derived Learning work and retain immutable event intake."""

        if (
            self._learning_writer_claim is None
            and self._learning_maintenance_task_id is None
        ):
            return  # already quiesced or never owned the projector
        scheduler = self._learning_scheduler
        quiesce = getattr(scheduler, "quiesce_projector", None)
        if callable(quiesce):
            quiesce(reason=reason, error_type=error_type)

        task_id = self._learning_maintenance_task_id
        if task_id is not None:
            get_task_manager().cancel_task(task_id)
            await self._await_managed_task(task_id, timeout=1.0)
            self._learning_maintenance_task_id = None

        recorder = self._new_event_only_learning_recorder(
            reason=reason,
            error_type=error_type,
        )
        await recorder.initialize()
        self._learning_scheduler = recorder
        self._learning_stores = None
        self._learning_writer_claim = None
        self._learning_storage_health = {
            "status": "degraded",
            "projector_owner": False,
            "event_append_available": True,
            "reason": reason,
            "error_type": error_type,
        }

    async def _handle_managed_singleton_loss(self, exc: Any) -> bool:
        """Detach only the exact confirmed-lost singleton domain."""

        from ..storage.learning_contracts import (
            LEARNING_WRITER_CLAIM_NAMESPACE,
            LEARNING_WRITER_CLAIM_STATE_KEY,
        )

        runtime = self._storage_runtime
        if runtime is None:
            return False
        claim = exc.claim
        # invalidate 只清本地管理表；DB 租约行仍是自己的且可能未过期，
        # 会阻塞后续 re-acquire（AlreadyClaimed）。先按正常路径 release
        # （DB 释放 + 本地弹出），release 失败再退回 invalidate。
        release = getattr(runtime, "release_singleton_writer", None)
        released = False
        if callable(release):
            try:
                released = bool(await release(claim))
            except Exception as release_exc:  # noqa: BLE001 - best effort
                logger.warning(
                    "managed singleton loss: DB release failed before "
                    f"local invalidation: {type(release_exc).__name__}"
                )
        removed = bool(runtime.invalidate_managed_singleton_writer(claim))
        if released:
            removed = True
        key = (str(exc.namespace), str(exc.state_key))
        domain_status = "degraded" if key == (
            LEARNING_WRITER_CLAIM_NAMESPACE,
            LEARNING_WRITER_CLAIM_STATE_KEY,
        ) else "failed"
        self._lost_singleton_health[key] = {
            "status": domain_status,
            "namespace": key[0],
            "state_key": key[1],
            "generation_id": str(exc.generation_id),
            "owner_instance_id": str(exc.owner_instance_id),
            "lease_epoch": int(exc.lease_epoch),
            "error_type": str(exc.failure_type),
            "local_claim_invalidated": bool(removed),
        }
        if not removed:
            try:
                await self._quiesce_learning_projector(
                    reason="managed singleton loss did not match the local snapshot",
                    error_type=str(exc.failure_type),
                )
            except Exception as quiesce_error:  # noqa: BLE001 - keep the claim failure
                logger.warning(
                    "learning projector quiesce failed after snapshot mismatch: %s",
                    type(quiesce_error).__name__,
                )
            self._cache_storage_renewal_state(
                status="failed",
                reason="managed singleton loss did not match the local snapshot",
            )
            return False
        if key == (
            LEARNING_WRITER_CLAIM_NAMESPACE,
            LEARNING_WRITER_CLAIM_STATE_KEY,
        ):
            await self._quiesce_learning_projector(
                reason="learning singleton writer lease was lost",
                error_type=str(exc.failure_type),
            )
        elif key == ("life_engine.runtime_context", "global"):
            self._runtime_context_writer_claim = None
        return True

    async def _fail_storage_authority(self, exc: BaseException) -> None:
        runtime = self._storage_runtime
        if runtime is not None:
            runtime.invalidate_writer()
        try:
            await self._quiesce_learning_projector(
                reason="storage authority was conclusively lost",
                error_type=type(exc).__name__,
            )
        except Exception as quiesce_error:  # noqa: BLE001 - keep the original failure
            logger.warning(
                "learning projector quiesce failed during authority loss: %s",
                type(quiesce_error).__name__,
            )
        self._storage_renewal_health = {
            "status": "failed",
            "last_success_at": self._storage_renewal_health.get(
                "last_success_at", ""
            ),
            "next_retry_at": "",
            "error_type": type(exc).__name__,
            "consecutive_failures": int(
                self._storage_renewal_health.get("consecutive_failures", 0) or 0
            )
            + 1,
        }
        self._cache_storage_renewal_state(
            status="failed",
            reason=f"authority lease renewal failed: {type(exc).__name__}",
        )
        logger.error(
            "life_engine storage authority was conclusively lost; writer disabled",
            exc_info=True,
        )  # noqa: G201 - project Logger has no exception()

    def _exit_for_lost_storage_authority(self, reason: str) -> None:
        """权威丢失后退出进程，交给外层守护重拉并重新激活。

        本地模式下入口级实例锁已排除同机第二个主写者，重启重新激活是安全
        的恢复路径；带着过期租约继续运行只会让每条写入路径反复抛
        StaleAuthorityToken（2026-08-26 事故形态）。
        """

        logger.error(
            "life_engine storage authority was lost "
            f"({reason}); exiting for supervised restart"
        )
        os._exit(30)

    async def _renew_storage_authority_loop(self) -> None:
        """守护包装：续租循环任何非正常关闭的退出都视为权威丢失。

        内层循环存在多条静默 return 路径（stop_event、runtime 置空、
        fail-closed）；2026-08-26 事故中任务被取消后无日志、无自愈，
        进程带着过期租约持续运行。这里确保只要服务仍在运行，就以受监管
        重启收场，而不是静默变成僵尸写者。
        """

        try:
            await self._run_storage_authority_renewal()
        except asyncio.CancelledError:
            stop_event = self._stop_event
            if stop_event is not None and stop_event.is_set():
                raise
            logger.warning(
                "life_engine storage authority renewal task was cancelled "
                "outside shutdown"
            )
            self._exit_for_lost_storage_authority("renewal task cancelled")
            raise
        stop_event = self._stop_event
        if stop_event is not None and stop_event.is_set():
            return
        self._exit_for_lost_storage_authority("renewal loop exited")

    async def _run_storage_authority_renewal(self) -> None:
        """Renew authority without turning connectivity unknown into lease loss."""

        from ..storage.authority import (
            AuthorityConflict,
            GenerationConflict,
            GenerationNotVerified,
            StaleAuthorityToken,
        )
        from ..storage.contracts import ManagedSingletonWriterClaimLost

        interval = max(
            0.0,
            float(
                self._storage_factory_settings.authority_renew_interval_seconds
            ),
        )
        lease_seconds = self._storage_factory_settings.authority_lease_seconds
        next_delay = interval
        transient_failures = 0
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=next_delay)
                return
            except asyncio.TimeoutError:
                pass

            runtime = self._storage_runtime
            if runtime is None:
                return
            try:
                await runtime.renew_authority(lease_seconds=lease_seconds)
            except asyncio.CancelledError:
                logger.warning(
                    "life_engine storage authority renewal was cancelled",
                )
                raise
            except ManagedSingletonWriterClaimLost as exc:
                if not await self._handle_managed_singleton_loss(exc):
                    await self._fail_storage_authority(
                        RuntimeError("ManagedSingletonLossSnapshotMismatch")
                    )
                    return
                transient_failures = 0
                next_delay = interval
                self._storage_renewal_health = {
                    "status": "degraded",
                    "last_success_at": self._storage_renewal_health.get(
                        "last_success_at", ""
                    ),
                    "next_retry_at": "",
                    "error_type": type(exc).__name__,
                    "consecutive_failures": 0,
                }
                self._cache_storage_renewal_state(
                    status="degraded",
                    reason="one managed singleton domain lost its writer lease",
                )
                logger.error(
                    "managed singleton writer lease was lost; only its domain "
                    f"was quiesced: namespace={exc.namespace} "
                    f"state_key={exc.state_key} epoch={exc.lease_epoch}"
                )
                continue
            except (
                AuthorityConflict,
                StaleAuthorityToken,
                GenerationConflict,
                GenerationNotVerified,
            ) as exc:
                await self._fail_storage_authority(exc)
                return
            except Exception as exc:  # noqa: BLE001 - classify before failing closed
                if not _is_storage_renewal_connectivity_unknown(exc):
                    await self._fail_storage_authority(exc)
                    return
                transient_failures += 1
                next_delay = _storage_renewal_backoff_seconds(
                    transient_failures,
                    owner_instance_id=self._storage_writer_instance_id,
                )
                self._storage_renewal_health = {
                    "status": "degraded",
                    "reason": "renewal_unknown",
                    "last_success_at": self._storage_renewal_health.get(
                        "last_success_at", ""
                    ),
                    "next_retry_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=next_delay)
                    ).isoformat(),
                    "retry_in_seconds": round(next_delay, 3),
                    "error_type": type(exc).__name__,
                    "consecutive_failures": transient_failures,
                }
                self._cache_storage_renewal_state(
                    status="degraded",
                    reason="storage authority renewal is unknown; retrying",
                )
                logger.warning(
                    "life_engine storage authority renewal is unknown after a "
                    f"connectivity failure; retaining claims and retrying in "
                    f"{next_delay:.1f}s: error_type={type(exc).__name__}"
                )
                continue

            transient_failures = 0
            next_delay = interval
            self._storage_renewal_health = {
                "status": "healthy",
                "last_success_at": _now_iso(),
                "next_retry_at": "",
                "error_type": "",
                "consecutive_failures": 0,
            }
            lost_statuses = {
                str(item.get("status") or "failed")
                for item in self._lost_singleton_health.values()
            }
            if "failed" in lost_statuses:
                cache_status = "failed"
            elif lost_statuses:
                cache_status = "degraded"
            else:
                cache_status = "healthy"
            self._cache_storage_renewal_state(
                status=cache_status,
                reason="storage authority renewal succeeded",
            )
            try:
                await self.refresh_storage_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - renewal already succeeded
                self._cache_storage_renewal_state(
                    status="degraded",
                    reason=f"storage health refresh failed: {type(exc).__name__}",
                )

    def _start_storage_authority_renewal(self) -> None:
        """Start the one service-owned renewal task after runtime acquisition."""

        if not self._selectable_storage_enabled:
            return
        if self._stop_event is None:
            raise RuntimeError("StorageAuthorityRenewalStopEventNotInitialized")
        if self._storage_authority_renew_task_id is not None:
            return
        task = get_task_manager().create_task(
            self._renew_storage_authority_loop(),
            name="life_engine_storage_authority_renewal",
            daemon=True,
        )
        self._storage_authority_renew_task_id = task.task_id

    async def _close_selected_storage(self) -> None:
        """Flush owned async work, revoke authority, and close the runtime."""

        if not self._selectable_storage_enabled:
            return
        errors: list[Exception] = []
        try:
            self._detach_proactive_delivery_proof_hook()
        except Exception as exc:  # noqa: BLE001 - aggregate owned cleanup
            errors.append(exc)
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
        if runtime is not None:
            try:
                await runtime.revoke_authority()
            except Exception as exc:  # noqa: BLE001 - aggregate owned cleanup
                runtime.invalidate_writer()
                errors.append(exc)
        self._subject_workspace_observer = None
        self._subject_workspace_projector = None
        self._subject_document_store = None
        self._runtime_state_store = None
        self._runtime_context_writer_claim = None
        self._learning_writer_claim = None
        self._learning_stores = None
        self._learning_event_store = None
        self._proactive_authority = None
        self._proactive_health_cache = {
            "component": "proactive_authority",
            "status": "disabled",
            "authority_count": 0,
            "reason": "service_stopped",
        }
        if self._multi_writer_bridge is not None:
            try:
                from src.core.transport.multi_writer_hooks import (
                    unregister_inbound_fact_hook,
                    unregister_outbox_intent_hook,
                    unregister_outbox_settle_hook,
                )

                if self._inbound_fact_hook is not None:
                    unregister_inbound_fact_hook(self._inbound_fact_hook)
                if self._outbox_intent_hook is not None:
                    unregister_outbox_intent_hook(self._outbox_intent_hook)
                if self._outbox_settle_hook is not None:
                    unregister_outbox_settle_hook(self._outbox_settle_hook)
            except Exception as exc:  # noqa: BLE001 - aggregate owned cleanup
                errors.append(exc)
            self._multi_writer_bridge = None
            self._inbound_fact_hook = None
            self._outbox_intent_hook = None
            self._outbox_settle_hook = None
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

    async def _subject_projection_loop(self) -> None:
        """Drain local subject outbox without overwriting unknown workspace bytes."""

        projector = self._subject_workspace_projector
        if projector is None or self._stop_event is None:
            return
        retry_delay = 1.0
        while not self._stop_event.is_set():
            try:
                result = await projector.project_one()
                if result.status == "idle":
                    await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                    return
                if result.status == "failed":
                    self._subject_projection_health = {
                        "status": "failed",
                        "last_success_at": self._subject_projection_health.get(
                            "last_success_at", ""
                        ),
                        "last_error_type": "SubjectProjectionFailed",
                        "reason": result.detail,
                    }
                    logger.error(
                        "subject workspace projection failed closed: "
                        f"path={result.logical_path} version={result.version_id}"
                    )
                    return
                retry_delay = 1.0
                self._subject_projection_health = {
                    "status": "healthy",
                    "last_success_at": _now_iso(),
                    "last_error_type": "",
                }
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retain retryable backlog
                self._subject_projection_health = {
                    "status": "degraded",
                    "last_success_at": self._subject_projection_health.get(
                        "last_success_at", ""
                    ),
                    "last_error_type": type(exc).__name__,
                    "reason": "subject projection worker is retrying pending work",
                }
                logger.warning(
                    f"subject workspace projection worker retrying after "
                    f"{type(exc).__name__}"
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=retry_delay,
                    )
                    return
                except TimeoutError:
                    retry_delay = min(retry_delay * 2.0, 30.0)

    async def _project_subject_version(
        self,
        *,
        logical_path: str,
        version_id: str,
        max_tasks: int,
    ) -> dict[str, Any]:
        """Verify one remote current head; never project selected data to files."""

        store = self._subject_document_store
        if store is None:
            raise RuntimeError("SelectedSubjectStorageNotStarted")
        from ..storage.models import BackendKind

        if getattr(store, "backend", None) == BackendKind.LOCAL:
            projector = self._subject_workspace_projector
            if projector is None:
                raise RuntimeError("SelectedSubjectProjectorNotStarted")
            task = await store.get_projection_task(logical_path, version_id)
            if task is None:
                raise RuntimeError(
                    f"SubjectProjectionMissing: {logical_path}:{version_id}"
                )
            if task.state == "confirmed":
                return {
                    "status": "confirmed_existing",
                    "logical_path": logical_path,
                    "version_id": version_id,
                }
            if task.state == "failed":
                await store.retry_projection(
                    task,
                    worker_id=projector.worker_id,
                )
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
                    "superseded",
                }:
                    return {
                        "status": result.status,
                        "logical_path": result.logical_path,
                        "version_id": result.version_id,
                    }
            raise RuntimeError(f"SubjectProjectionBacklogExceeded: {logical_path}")

        del max_tasks
        head = await store.get_head(logical_path)
        if head is None or head.current_version_id != version_id:
            raise RuntimeError(
                f"SubjectRemoteHeadMismatch: {logical_path}:{version_id}"
            )
        version = await store.get_version(version_id)
        if version.logical_path != logical_path:
            raise RuntimeError(
                f"SubjectRemoteVersionPathMismatch: {logical_path}:{version_id}"
            )
        task = await store.get_projection_task(logical_path, version_id)
        if task is None or task.state != "confirmed":
            state = "missing" if task is None else task.state
            # MySQL 后端不运行 workspace projector：outbox 由 append 时直接
            # 写为 confirmed。遗留的 failed/pending 是历史迁移残留或异常数据，
            # 权威版本字节已在 subject_document_versions 中，属于可重建投影损坏。
            # 这里自愈为 confirmed（重建投影），避免调用方无限重试刷屏。
            if getattr(store, "backend", None) == BackendKind.MYSQL:
                if task is not None and task.state in {"pending", "failed"}:
                    await store.heal_projection(task, worker_id="projection-self-heal")
                return {
                    "status": "remote_current_head",
                    "logical_path": logical_path,
                    "version_id": version_id,
                    "projection_self_healed": True,
                }
            raise RuntimeError(
                f"SubjectRemoteHeadNotConfirmed: {logical_path}:{version_id}:{state}"
            )
        return {
            "status": "remote_current_head",
            "logical_path": logical_path,
            "version_id": version_id,
        }

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
        """Commit one declared auxiliary subject document to the selected head.

        Unified root documents are rejected here so witness/index writers cannot
        impersonate identity files. The subject rewrites SOUL/USER/MEMORY through
        ordinary file tools via ``commit_subject_authority_file_write``.
        Disabled storage returns ``None`` for explicit local handling of an
        auxiliary document. Under MySQL the immutable version/current head is
        the only runtime representation; no workspace Markdown is read or
        written as a projection.
        """

        from ..storage.subject_workspace import (
            auxiliary_subject_path_from_workspace_relative,
        )

        logical_path = auxiliary_subject_path_from_workspace_relative(
            workspace_relative_path
        )
        if not self._selectable_storage_enabled:
            return None
        if logical_path is None:
            return None
        return await self._commit_declared_subject_document(
            logical_path=logical_path,
            workspace_relative_path=workspace_relative_path,
            content_bytes=content_bytes,
            occurrence_id=occurrence_id,
            recorded_by=recorded_by,
            recorded_source=recorded_source,
            encoding=encoding,
            semantic_actor_id=semantic_actor_id,
            semantic_source_id=semantic_source_id,
            reason=reason,
            operation="selected_subject_write",
        )

    async def commit_subject_authority_file_write(
        self,
        *,
        workspace_relative_path: str,
        content_bytes: bytes,
        occurrence_id: str,
        recorded_by: str,
        recorded_source: str,
        encoding: str | None,
        reason: str = "",
    ) -> dict[str, Any] | None:
        """Commit a subject-owned SOUL/USER/MEMORY rewrite from ordinary file tools.

        Selected storage remains the prompt authority. File tools write the
        workspace file after this CAS append, so the next turn reads what she
        just decided. Disabled storage returns ``None`` and leaves the
        workspace file as the only copy.
        """

        from ..storage.subject_contracts import (
            SUBJECT_AUTHORITY_PATHS,
            subject_authority_logical_path,
        )
        from ..storage.subject_workspace import subject_path_from_workspace_relative

        if not self._selectable_storage_enabled:
            return None
        mapped = subject_path_from_workspace_relative(workspace_relative_path)
        if mapped is None:
            return None
        name = mapped.removeprefix("life_engine_workspace/")
        if name not in SUBJECT_AUTHORITY_PATHS:
            return None
        return await self._commit_declared_subject_document(
            logical_path=subject_authority_logical_path(name),
            workspace_relative_path=name,
            content_bytes=content_bytes,
            occurrence_id=occurrence_id,
            recorded_by=recorded_by,
            recorded_source=recorded_source,
            encoding=encoding,
            reason=reason,
            operation="subject_file_tool_write",
        )

    async def _commit_declared_subject_document(
        self,
        *,
        logical_path: str,
        workspace_relative_path: str,
        content_bytes: bytes,
        occurrence_id: str,
        recorded_by: str,
        recorded_source: str,
        encoding: str | None,
        semantic_actor_id: str | None = None,
        semantic_source_id: str | None = None,
        reason: str = "",
        operation: str = "selected_subject_write",
    ) -> dict[str, Any]:
        from ..storage.subject_contracts import AppendSubjectDocumentVersion

        store = self._subject_document_store
        if store is None:
            raise RuntimeError("SelectedSubjectStorageNotStarted")

        head = await store.get_head(logical_path)
        from ..storage.models import BackendKind

        # File-tool writes CAS-append intended bytes before touching the
        # workspace file. Observing disk first would re-commit stale bytes.
        if (
            getattr(store, "backend", None) == BackendKind.LOCAL
            and operation != "subject_file_tool_write"
        ):
            observer = self._subject_workspace_observer
            if observer is None:
                raise RuntimeError("SelectedSubjectObserverNotStarted")
            observed = await observer.observe_file(logical_path)
            if observed.status == "changed_during_read":
                raise RuntimeError(f"SubjectWorkspaceChangedDuringRead: {logical_path}")
            if observed.commit is not None:
                await self._project_subject_version(
                    logical_path=logical_path,
                    version_id=observed.commit.version.version_id,
                    max_tasks=observed.commit.head.revision + 1,
                )
                head = observed.commit.head
            elif head is not None:
                task = await store.get_projection_task(
                    logical_path,
                    head.current_version_id,
                )
                if task is not None and task.state == "pending":
                    await self._project_subject_version(
                        logical_path=logical_path,
                        version_id=head.current_version_id,
                        max_tasks=head.revision + 1,
                    )

        if head is not None:
            current = await store.get_version(head.current_version_id)
            if current.content_bytes == bytes(content_bytes):
                if getattr(store, "backend", None) == BackendKind.LOCAL:
                    confirmed_head = {
                        "status": "confirmed_existing",
                        "logical_path": logical_path,
                        "version_id": current.version_id,
                    }
                else:
                    confirmed_head = await self._project_subject_version(
                        logical_path=logical_path,
                        version_id=current.version_id,
                        max_tasks=1,
                    )
                return {
                    "status": "unchanged",
                    "logical_path": logical_path,
                    "version_id": current.version_id,
                    "revision": head.revision,
                    "projection": confirmed_head,
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
                    "operation": str(operation),
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
                    "SelectedLifeEventStoreNotStarted: await LifeEngineService.start()"
                )
            return self._life_event_store
        return self._get_event_bus().store

    def _get_event_bus(self) -> LifeEventBus:
        if self._event_bus is None:
            if self._selectable_storage_enabled:
                raise RuntimeError(
                    "SelectedLifeEventStoreNotStarted: await LifeEngineService.start()"
                )
            registry = self.consciousness_registry
            assert isinstance(registry, ConsciousnessRegistry)
            self._event_bus = LifeEventBus(RawEventStore(self._workspace_dir()))
            registry.flush_lifecycle_events(self._event_bus.store.append_sync)
        return self._event_bus

    def _get_world_projection(self) -> Any:
        """Return the rebuildable subjective world read model."""

        if self._world_projection is None:
            if self._selectable_storage_enabled:
                raise RuntimeError(
                    "SelectedWorldProjectionNotStarted: await LifeEngineService.start()"
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

    @property
    def minecraft_session(self) -> Any | None:
        """Expose the service-owned Minecraft session without transferring ownership."""

        return self._minecraft_session

    def _create_minecraft_session(self) -> Any:
        """Construct one inactive Minecraft session from the validated config."""

        from ..minecraft.launcher import MCConfig
        from ..minecraft.session import MinecraftSession

        section = getattr(self._cfg(), "minecraft", None)
        if section is None or not bool(getattr(section, "enabled", False)):
            raise RuntimeError("MinecraftSessionDisabled")
        raw = section.model_dump() if hasattr(section, "model_dump") else vars(section)
        fields = set(MCConfig.__dataclass_fields__)
        values = {name: value for name, value in raw.items() if name in fields}
        for name in ("mc_home", "agent_token_file", "biomimetic_token_file"):
            if name in values:
                values[name] = Path(values[name])
        config = MCConfig(**values)
        return MinecraftSession(
            workspace=self._workspace_dir(),
            mc_config=config,
            consciousness_registry=self.consciousness_registry,
            register_consciousness_instance=self.register_consciousness_instance,
            touch_consciousness_instance=self.touch_consciousness_instance,
            resume_consciousness_instance=self.resume_consciousness_instance,
            terminate_consciousness_instance=self.terminate_consciousness_instance,
            get_recent_subconscious_context=self.get_recent_subconscious_context,
            get_subject_context_projection_snapshot=(
                self.get_subject_context_projection_snapshot
            ),
            record_minecraft_consciousness_decision=(
                self.record_minecraft_consciousness_decision
            ),
            record_conscious_model_turn=self.record_conscious_model_turn,
            report_world_observation=self.report_world_observation,
        )

    async def record_minecraft_consciousness_decision(
        self,
        decision: Mapping[str, Any],
        context_reference: Mapping[str, Any],
    ) -> LifeEngineEvent:
        """Durably append one attributed MC choice before its body may act.

        A retry reuses the exact same ``LifeEngineEvent`` object, including its
        source sequence and timestamp. This keeps the raw occurrence idempotent
        when persistence succeeded but the local pending checkpoint failed.
        """

        decision_payload = dict(decision)
        context_payload = dict(context_reference)
        decision_id = str(decision_payload.get("decision_id") or "").strip()
        if not decision_id:
            raise ValueError("Minecraft consciousness decision_id must not be empty")
        expected_raw = json.dumps(
            {
                "decision": decision_payload,
                "context_reference": context_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        event = self._minecraft_decision_event_cache.get(decision_id)
        if event is None:
            event = self._event_builder.build_minecraft_consciousness_decision_event(
                decision_payload,
                context_payload,
            )
            self._minecraft_decision_event_cache[decision_id] = event
        elif event.raw_content != expected_raw:
            raise ValueError(
                "Minecraft consciousness decision retry changed its payload"
            )
        if decision_id in self._minecraft_recorded_decision_ids:
            return event

        # Use the same ledger-first path as every other conscious activity.
        # A rejected durable append must never leave a phantom MC decision in
        # the compatibility pending queue; a retry remains idempotent because
        # both event_id and occurrence_id are the stable decision_id.
        await self._queue_pending_event(event)
        self._minecraft_recorded_decision_ids.add(decision_id)
        return event

    async def _initialize_minecraft_session(self) -> None:
        """Acquire the optional service-owned session exactly once."""

        section = getattr(self._cfg(), "minecraft", None)
        if section is None or not bool(getattr(section, "enabled", False)):
            return
        if self._minecraft_session is not None:
            return
        session = self._create_minecraft_session()
        self._minecraft_session = session
        logger.info("Minecraft evidence-driven embodiment initialized")

    async def _close_minecraft_session(self) -> None:
        """Idempotently release the owned session while retaining failed cleanup."""

        async with self._minecraft_session_close_lock:
            session = self._minecraft_session
            if session is None:
                return
            close = getattr(session, "close", None)
            if callable(close):
                result = await close()
                if isinstance(result, dict) and result.get("success") is False:
                    raise RuntimeError("MinecraftSessionCloseFailed")
            else:
                # Compatibility for the current consumer until its idempotent
                # close() contract lands; stop() is already async.
                await session.stop()
            self._minecraft_session = None

    def resolve_consciousness_instance(self, stream_id: str = "") -> str:
        """Resolve a trusted runtime instance from current stream ownership."""

        owner = self.consciousness_registry.get_for_stream(str(stream_id or "").strip())
        return owner.instance_id if owner is not None else "chat_global"

    async def _current_learning_subject_revision(self) -> str:
        """Read the exact unified subject revision without authoring a projection."""

        if self._subject_document_store is not None:
            # When a selected store is bound, it is the only authority source;
            # the unified revision must come from the remote single-transaction
            # snapshot instead of local Markdown polling.
            return str(await self._subject_document_store.current_subject_revision())

        from ..core.router_context_projection import read_subject_authority_sources

        _, revision = await asyncio.to_thread(
            read_subject_authority_sources,
            self._workspace_dir(),
        )
        return revision

    async def _project_learning_subject_authority_commit(
        self,
        target_path: Any,
        commit: Any,
    ) -> None:
        """Make an accepted selected-store revision visible in the workspace."""

        from ..storage.subject_contracts import subject_authority_logical_path

        logical_path = subject_authority_logical_path(target_path)
        await self._project_subject_version(
            logical_path=logical_path,
            version_id=str(commit.document_version_id),
            max_tasks=int(commit.document_revision) + 1,
        )
        self.notify_subject_context_source_changed(
            self._workspace_dir() / str(target_path)
        )

    async def _validate_learning_decision_actor(self, instance_id: str) -> bool:
        """Validate that a learning decision comes from an active runtime window."""

        instance = self.consciousness_registry.get(str(instance_id or "").strip())
        return bool(instance is not None and instance.is_active)

    async def _validate_initiative_decision_actor(self, instance_id: str) -> bool:
        """Reconcile technical leases before accepting a subject initiative."""

        registry = self.consciousness_registry
        if isinstance(registry, AsyncConsciousnessRegistry):
            await registry.reconcile_expired()
        else:
            await asyncio.to_thread(
                registry.reconcile_expired,
                timestamp=_now_iso(),
            )
        instance = registry.get(str(instance_id or "").strip())
        return bool(instance is not None and instance.is_active)

    @staticmethod
    def _world_observation_identities(
        *,
        occurrence_id: str,
        assertion_id: str,
        observed_at: str,
    ) -> tuple[str, str, str]:
        """Resolve optional stable identities for idempotent observation replay."""

        occurrence = str(occurrence_id or "").strip()
        assertion = str(assertion_id or "").strip()
        if assertion and not occurrence:
            raise ValueError(
                "stable world assertion_id requires a stable occurrence_id"
            )
        if occurrence and not str(observed_at or "").strip():
            raise ValueError(
                "stable world occurrence_id requires an explicit observed_at"
            )
        if not occurrence:
            event_id = "world_observation_" + uuid4().hex
            return event_id, event_id, "assertion_" + uuid4().hex
        event_digest = hashlib.sha256(
            f"world-observation-event:{occurrence}".encode("utf-8")
        ).hexdigest()
        assertion_digest = hashlib.sha256(
            f"world-observation-assertion:{occurrence}".encode("utf-8")
        ).hexdigest()
        return (
            "world_observation_" + event_digest[:32],
            occurrence,
            assertion or "assertion_" + assertion_digest[:32],
        )

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
        occurrence_id: str = "",
        assertion_id: str = "",
    ) -> dict[str, Any]:
        """Append one attributed observation and project it synchronously."""

        if self._selectable_storage_enabled:
            raise RuntimeError(
                "SelectedWorldObservationRequiresAwait: use report_world_observation()"
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
            raise ValueError(
                "world observation subject and predicate must not be empty"
            )
        now = observed_at or _now_iso()
        event_id, event_occurrence_id, resolved_assertion_id = (
            self._world_observation_identities(
                occurrence_id=occurrence_id,
                assertion_id=assertion_id,
                observed_at=observed_at,
            )
        )
        assertion_value = report_text if value is None else value
        reject_prompt_projection_persistence(
            assertion_value,
            domain=str(domain or ""),
            predicate=assertion_predicate,
        )
        assertion: dict[str, Any] = {
            "assertion_id": resolved_assertion_id,
            "subject": assertion_subject,
            "predicate": assertion_predicate,
            "value": assertion_value,
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
            occurrence_id=event_occurrence_id,
            source_instance_id=instance_id,
            metadata={"assertion_id": resolved_assertion_id},
        )
        persisted = self._get_event_bus().store.append_sync(event)
        frontier = self._get_world_projection().catch_up(self._get_event_bus().store)
        return {
            "event_id": persisted.event_id,
            "occurrence_id": persisted.occurrence_id,
            "assertion_id": resolved_assertion_id,
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
        occurrence_id: str = "",
        assertion_id: str = "",
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
                occurrence_id=occurrence_id,
                assertion_id=assertion_id,
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
            raise ValueError("world observation source_instance_id must not be empty")
        if registry.get(instance_id) is None:
            raise ValueError(
                f"world observation source instance is not registered: {instance_id}"
            )
        if not assertion_subject or not assertion_predicate:
            raise ValueError(
                "world observation subject and predicate must not be empty"
            )
        now = str(observed_at or "") or _now_iso()
        event_id, event_occurrence_id, resolved_assertion_id = (
            self._world_observation_identities(
                occurrence_id=occurrence_id,
                assertion_id=assertion_id,
                observed_at=observed_at,
            )
        )
        assertion_value = report_text if value is None else value
        reject_prompt_projection_persistence(
            assertion_value,
            domain=str(domain or ""),
            predicate=assertion_predicate,
        )
        assertion: dict[str, Any] = {
            "assertion_id": resolved_assertion_id,
            "subject": assertion_subject,
            "predicate": assertion_predicate,
            "value": assertion_value,
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
            occurrence_id=event_occurrence_id,
            source_instance_id=instance_id,
            metadata={"assertion_id": resolved_assertion_id},
        )
        persisted = await self._get_life_event_store().append(event)
        frontier = await self.catch_up_world_projection()
        return {
            "event_id": persisted.event_id,
            "occurrence_id": persisted.occurrence_id,
            "assertion_id": resolved_assertion_id,
            "ingest_position": persisted.sequence,
            "projection_as_of": frontier,
            "source_instance_id": instance_id,
        }

    async def prepare_perception(
        self,
        instance_id: str,
        *,
        projection_kind: str = "default",
        max_bytes: int = DEFAULT_PERCEPTION_MAX_BYTES,
    ) -> PreparedPerception:
        """Prepare a bounded retryable World delivery for one instance."""

        gateway = self._get_perception_gateway()
        if isinstance(gateway, AsyncPerceptionGateway):
            return await gateway.prepare(
                instance_id,
                projection_kind=projection_kind,
                max_bytes=max_bytes,
            )
        return await asyncio.to_thread(
            gateway.prepare,
            instance_id,
            projection_kind=projection_kind,
            max_bytes=max_bytes,
        )

    async def commit_perception(
        self,
        prepared: PreparedPerception,
        receipt: PerceptionDeliveryReceipt | None = None,
    ) -> tuple[int, int]:
        """Commit only after an exact effective-context receipt."""

        if receipt is None:
            logger.warning(
                "World perception receipt missing; cursor remains unchanged: "
                f"delivery_id={prepared.delivery_id}"
            )
            return prepared.from_position, prepared.cursor_revision
        gateway = self._get_perception_gateway()
        if isinstance(gateway, AsyncPerceptionGateway):
            return await gateway.commit(prepared, receipt)
        return await asyncio.to_thread(gateway.commit, prepared, receipt)

    async def commit_perception_delivery(
        self,
        checkpoint: PerceptionCommitCheckpoint,
        receipt: PerceptionDeliveryReceipt | None = None,
    ) -> tuple[int, int]:
        """Replay a receipt-gated cursor CAS without persisting prompt text."""

        if receipt is None:
            logger.warning(
                "World perception checkpoint receipt missing; cursor remains "
                f"unchanged: delivery_id={checkpoint.delivery_id}"
            )
            return checkpoint.from_position, checkpoint.cursor_revision
        gateway = self._get_perception_gateway()
        if isinstance(gateway, AsyncPerceptionGateway):
            return await gateway.commit_delivery(checkpoint, receipt)
        return await asyncio.to_thread(
            gateway.commit_delivery,
            checkpoint,
            receipt,
        )

    async def query_world(
        self,
        instance_id: str,
        query: str,
        *,
        max_bytes: int = DEFAULT_PERCEPTION_MAX_BYTES,
    ) -> str:
        """Return a bounded provenance-aware projection for reflection."""

        gateway = self._get_perception_gateway()
        if isinstance(gateway, AsyncPerceptionGateway):
            return await gateway.query(instance_id, query, max_bytes=max_bytes)
        return await asyncio.to_thread(
            gateway.query,
            instance_id,
            query,
            max_bytes=max_bytes,
        )

    async def list_world_assertion_references_page(
        self,
        *,
        include_retracted: bool = False,
        after_observed_at: str = "",
        after_assertion_id: str = "",
        continuation_token: str = "",
        limit: int = 128,
        inline_max_bytes: int = 1024,
    ) -> Any:
        """Return one stable bounded assertion page from the selected store."""

        if continuation_token:
            continuation = PerceptionGateway.decode_snapshot_continuation_token(
                continuation_token
            )
            after_observed_at = str(continuation.get("after_observed_at") or "")
            after_assertion_id = str(continuation.get("after_assertion_id") or "")
        gateway = self._get_perception_gateway()
        projection = gateway.projection
        if isinstance(gateway, AsyncPerceptionGateway):
            return await projection.list_assertion_references_page(
                include_retracted=include_retracted,
                after_observed_at=after_observed_at,
                after_assertion_id=after_assertion_id,
                limit=limit,
                inline_max_bytes=inline_max_bytes,
            )
        return await asyncio.to_thread(
            projection.list_assertion_references_page,
            include_retracted=include_retracted,
            after_observed_at=after_observed_at,
            after_assertion_id=after_assertion_id,
            limit=limit,
            inline_max_bytes=inline_max_bytes,
        )

    async def read_world_assertion_value_chunk(
        self,
        assertion_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> Any:
        """Read one assertion value chunk on canonical UTF-8 boundaries."""

        gateway = self._get_perception_gateway()
        projection = gateway.projection
        if isinstance(gateway, AsyncPerceptionGateway):
            return await projection.read_assertion_value_chunk(
                assertion_id,
                offset_bytes=offset_bytes,
                max_bytes=max_bytes,
            )
        return await asyncio.to_thread(
            projection.read_assertion_value_chunk,
            assertion_id,
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

    async def read_world_change_payload_chunk(
        self,
        ingest_position: int,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> Any:
        """Read one World change payload chunk without cursor mutation."""

        gateway = self._get_perception_gateway()
        projection = gateway.projection
        if isinstance(gateway, AsyncPerceptionGateway):
            return await projection.read_change_payload_chunk(
                ingest_position,
                offset_bytes=offset_bytes,
                max_bytes=max_bytes,
            )
        return await asyncio.to_thread(
            projection.read_change_payload_chunk,
            ingest_position,
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

    async def catch_up_world_projection(self) -> int:
        """Advance the selected or legacy projection without blocking the loop."""

        # 后台化的消息慢阶段与同步调用点（record_send_requested /
        # record_delivery_status）都会走到这里，并发执行会让
        # world_projection_changes 的 ingest_position 主键冲突，此处统一串行。
        async with self._world_projection_lock:
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
            # 手动解除休息锁已在本实例生效，持久化冲突属双实例合法竞争，可恢复。
            # ⚠️ 2026-09-01 现状更正：当前 backend="local" 且 multi_writer_enabled=false，
            # 双实例共享场景不存在。此处逻辑是为多实例/多写者模式预留的防御；
            # 单实例下若出现该冲突，应排查并发写入源而非当作合法竞争放过。
            await self._save_runtime_context(recoverable_on_shared_conflict=True)
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

        await self._save_runtime_context(recoverable_on_shared_conflict=True)
        # 进入休息只报一次；心跳循环里用此标记静默跳过，不再每 tick 刷 remaining。
        self._self_pause_skip_logged = True
        logger.info(
            "life_engine 进入主动休息: "
            f"duration={payload['duration_minutes']}min requested={payload['requested_minutes']}min "
            f"until={payload['paused_until']} reason={payload['reason'] or '-'}"
        )

        return payload

    def _workspace_dir(self) -> Path:
        """返回 life workspace 目录。"""
        workspace = Path(self._cfg().settings.workspace_path).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _initialize_local_runtime_state(self) -> None:
        """Open local runtime state only after subject-authority preflight."""

        if self._selectable_storage_enabled:
            return
        workspace = self._workspace_dir()
        if self._world_state is None:
            self._world_state = WorldState.load(
                workspace / "runtime" / "world_state.json"
            )
        if self._consciousness_registry is None:
            self._consciousness_registry = ConsciousnessRegistry.load(
                workspace / "runtime" / "consciousness_registry.json"
            )

    async def _validate_local_subject_authority(self) -> None:
        """Fail before startup when the local subject snapshot is incomplete."""

        if self._selectable_storage_enabled:
            return

        from ..core.router_context_projection import read_subject_authority_sources

        # Missing authority must remain distinguishable from an intentionally
        # empty document. Reading the exact snapshot here also validates UTF-8
        # and the canonical unified revision before any runtime is acquired.
        await asyncio.to_thread(
            read_subject_authority_sources,
            Path(self._cfg().settings.workspace_path).resolve(),
        )

    def snapshot(self) -> dict[str, Any]:
        """返回当前状态快照。"""
        data = asdict(self._state)
        in_sleep_window, sleep_window_desc = self._in_sleep_window_now()
        data["heartbeat_interval_seconds"] = int(
            self._cfg().settings.heartbeat_interval_seconds
        )
        data["external_silence_minutes"] = self._minutes_since_external_message()
        self_paused, self_pause_remaining, self_pause_until, self_pause_reason = (
            self._self_pause_status()
        )
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
            iso_time = (
                datetime.fromtimestamp(float(raw_time), tz=timezone.utc)
                .astimezone()
                .isoformat()
            )
        except Exception:
            iso_time = _now_iso()
        return iso_time, _format_time_display(iso_time)

    @staticmethod
    def _format_message_text(message: Message, *, max_length: int = 240) -> str:
        """格式化消息正文。"""
        raw_text = getattr(message, "processed_plain_text", None)
        if raw_text is None:
            raw_text = getattr(message, "content", "")
        return _shorten_text(
            str(raw_text or "").strip() or "（空消息）", max_length=max_length
        )

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
            "occurrence_id": event.occurrence_id,
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

    async def _publish_message_facts(
        self,
        legacy_event: LifeEngineEvent,
        chat_fact: LifeEvent,
    ) -> None:
        """Atomically append the compatibility event and stable chat fact."""

        if not legacy_event.source_instance_id:
            stream_id = str(legacy_event.stream_id or "").strip()
            registry = self._consciousness_registry
            owner = (
                registry.get_for_stream(stream_id)
                if stream_id and registry is not None
                else None
            )
            if owner is not None:
                legacy_event.source_instance_id = owner.instance_id
                legacy_event.correlation_id = (
                    legacy_event.correlation_id or owner.session_id or None
                )
                chat_fact = replace(
                    chat_fact,
                    source_instance_id=owner.instance_id,
                    correlation_id=chat_fact.correlation_id or owner.session_id,
                )
        await self._get_event_bus().publish_many(
            [life_event_from_legacy(legacy_event), chat_fact]
        )
        if self._world_projection is not None:
            await self.catch_up_world_projection()

    async def record_send_requested(
        self,
        message: Message,
        *,
        envelope: Mapping[str, Any] | None = None,
        adapter_signature: str = "",
    ) -> None:
        """Append a pre-send request fact without claiming delivery success."""

        if not self._is_enabled():
            return
        event = build_chat_message_event(
            message,
            direction="requested",
            envelope=envelope,
            adapter_signature=adapter_signature,
        )
        await self._get_event_bus().publish(event)
        if self._world_projection is not None:
            await self.catch_up_world_projection()

    async def record_delivery_status(
        self,
        message: Message,
        *,
        status: str,
        adapter_signature: str = "",
    ) -> None:
        """Append an explicit failed or unknown delivery fact."""

        if not self._is_enabled():
            return
        if status not in {"failed", "unknown"}:
            raise ValueError("delivery status must be failed or unknown")
        event = build_chat_message_event(
            message,
            direction="delivered",
            adapter_signature=adapter_signature,
            delivery_status=status,
        )
        await self._get_event_bus().publish(event)
        if self._world_projection is not None:
            await self.catch_up_world_projection()

    async def record_provider_notice(
        self,
        raw: Mapping[str, Any],
        *,
        adapter_signature: str = "",
    ) -> None:
        """Append a provider notice fact without creating a cognitive queue item."""

        if not self._is_enabled():
            return
        event = build_chat_provider_notice_event(
            raw,
            adapter_signature=adapter_signature,
        )
        await self._get_event_bus().publish(event)
        if self._world_projection is not None:
            await self.catch_up_world_projection()

    async def _enqueue_pending_events(
        self,
        events: list[LifeEngineEvent],
        *,
        persist: bool = True,
    ) -> list[LifeEngineEvent]:
        """Idempotent pending enqueue without publishing to the ledger."""

        if not events:
            return []
        queued: list[LifeEngineEvent] = []
        async with self._get_lock():
            known_occurrences: set[str] = set()
            for item in [*self._event_history, *self._pending_events]:
                known_occurrences.update(workset_identities(item))
            for event in events:
                identities = workset_identities(event)
                if identities and identities & known_occurrences:
                    continue
                queued.append(event)
                known_occurrences.update(identities)
                self._state.event_sequence = max(
                    int(self._state.event_sequence or 0),
                    int(event.sequence or 0),
                )
            self._pending_events.extend(queued)
            self._state.pending_event_count = len(self._pending_events)
            if queued:
                self._inner_dialogue_store().apply_events(queued)
                self._sync_inner_dialogue_state()
        if persist:
            await self._save_runtime_context(recoverable_on_shared_conflict=True)
        return queued

    async def _queue_pending_events(
        self,
        events: list[LifeEngineEvent],
        *,
        persist: bool = True,
    ) -> None:
        """Append one ordered activity batch with one durable checkpoint."""

        if not events:
            return
        # The append-only ledger is authoritative.  Publish first so a storage
        # rejection cannot leave an in-memory activity that never happened.
        await self._publish_raw_events(events)
        await self._enqueue_pending_events(events, persist=persist)

    def _try_life_event_store(self) -> Any | None:
        """Return the readable ledger or None when this process cannot open it."""

        if self._selectable_storage_enabled:
            return self._life_event_store
        if self._event_bus is not None:
            return self._event_bus.store
        try:
            return self._get_event_bus().store
        except Exception:  # noqa: BLE001 - catch-up is optional until start
            return None

    async def catch_up_subconscious_ingest(self) -> None:
        """Pull unconsumed ledger rows into the derived pending buffer."""

        if not self._is_enabled():
            return
        store = self._try_life_event_store()
        if store is None:
            self._subconscious_ingest_health = {
                **self._subconscious_ingest_health,
                "status": "disabled",
                "error_type": "LifeEventStoreUnavailable",
            }
            return
        async with self._subconscious_ingest_lock:
            async with self._get_lock():
                known_occurrences: set[str] = set()
                for item in [*self._event_history, *self._pending_events]:
                    known_occurrences.update(workset_identities(item))
                heartbeat_cursor = int(self._state.heartbeat_context_cursor or 0)
            try:
                report, queued = await catch_up_subconscious_ingest(
                    store=store,
                    next_sequence=self._next_sequence,
                    known_occurrences=known_occurrences,
                    heartbeat_context_cursor=heartbeat_cursor,
                )
            except SubconsciousLedgerGap as gap:
                self._subconscious_ingest_health = {
                    **self._subconscious_ingest_health,
                    "status": "failed",
                    "gap": True,
                    "error_type": type(gap).__name__,
                }
                raise
            except SubconsciousIngestStoreUnavailable as exc:
                self._subconscious_ingest_health = {
                    **self._subconscious_ingest_health,
                    "status": "failed",
                    "gap": False,
                    "error_type": type(exc).__name__,
                }
                raise
            if queued:
                await self._enqueue_pending_events(queued, persist=True)
            if report.bootstrapped:
                # catch_up already seeded the cursor with bootstrap=high_water.
                pass
            elif report.through_position > report.from_position:
                await commit_subconscious_ingest_cursor(
                    store,
                    through_position=report.through_position,
                    metadata={
                        "queued": report.queued,
                        "classified_out": report.classified_out,
                        "bootstrapped": report.bootstrapped,
                    },
                )
            self._subconscious_ingest_health = {
                **report.health(),
                "last_success_at": _now_iso(),
            }

    async def _queue_pending_event(
        self,
        event: LifeEngineEvent,
        *,
        persist: bool = True,
    ) -> None:
        """Append an event to the compatibility pending queue and raw bus."""
        await self._queue_pending_events([event], persist=persist)

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
                or (getattr(message, "extra", {}) or {}).get(
                    "is_inner_monologue", False
                )
            ),
            "is_proactive_followup_trigger": bool(
                getattr(message, "is_proactive_followup_trigger", False)
                or (getattr(message, "extra", {}) or {}).get(
                    "is_proactive_followup_trigger", False
                )
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
            for event in (
                event_history[-max(1, event_limit) :]
                if event_limit > 0
                else event_history
            )
        ]
        pending_life_events = [
            self._serialize_life_event(event)
            for event in (
                pending_events[-max(1, min(event_limit, len(pending_events))) :]
                if pending_events
                else []
            )
        ]

        life_latest_event = (
            life_events[-1]
            if life_events
            else (pending_life_events[-1] if pending_life_events else None)
        )

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
                    stream_name=str(
                        getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"
                    ),
                    source="history",
                )
                for msg in candidate_messages
            ]

            latest_message = None
            if unread_messages:
                latest_message = self._serialize_stream_message(
                    unread_messages[-1],
                    stream_name=str(
                        getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"
                    ),
                    source="unread",
                )
            elif history_messages:
                latest_message = self._serialize_stream_message(
                    history_messages[-1],
                    stream_name=str(
                        getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"
                    ),
                    source="history",
                )
            elif current_message is not None:
                latest_message = self._serialize_stream_message(
                    current_message,
                    stream_name=str(
                        getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"
                    ),
                    source="current",
                )

            last_message_time = getattr(context, "last_message_time", None)
            last_active_time = getattr(stream, "last_active_time", None)
            stream_snapshots.append(
                {
                    "stream_id": stream_id,
                    "stream_name": str(
                        getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"
                    ),
                    "platform": str(getattr(stream, "platform", "") or ""),
                    "chat_type": str(getattr(stream, "chat_type", "") or ""),
                    "bot_nickname": str(getattr(stream, "bot_nickname", "") or ""),
                    "is_active": bool(getattr(stream, "is_active", True)),
                    "is_chatter_processing": bool(
                        getattr(context, "is_chatter_processing", False)
                    ),
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
            or self._runtime_state_store is None
            or self._proactive_authority is None
        ):
            return dict(self._storage_health_cache)
        from ..proactive.backend_binding import verify_proactive_backend_binding

        component_checks: list[tuple[str, Any]] = [
            ("runtime", self._storage_runtime.health()),
            (
                "proactive_backend_binding",
                verify_proactive_backend_binding(
                    workspace_path=self._cfg().settings.workspace_path,
                    binding_path=self._cfg().proactive.backend_binding_path,
                    runtime=self._storage_runtime,
                ),
            ),
            ("life_event", self._life_event_store.health_snapshot()),
            ("presence", self._presence_world_stores.presence.health_snapshot()),
            ("world", self._presence_world_stores.world.health_snapshot()),
            ("subject_document", self._subject_document_store.health_snapshot()),
            ("runtime_state", self._runtime_state_store.health_snapshot()),
            ("proactive_authority", self._proactive_authority.health_snapshot()),
        ]
        learning_health_store = (
            self._learning_stores.store
            if self._learning_stores is not None
            else self._learning_event_store
        )
        if learning_health_store is not None:
            component_checks.append(
                ("learning", learning_health_store.health_snapshot())
            )
        results = await asyncio.gather(
            *(check for _, check in component_checks),
            return_exceptions=True,
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
            name: normalized(name, result)
            for (name, _), result in zip(component_checks, results, strict=True)
        }
        if "proactive_authority" in components:
            self._proactive_health_cache = dict(components["proactive_authority"])
            self._proactive_health_cache["backend"] = (
                self._storage_factory_settings.authoritative_backend.value
            )
            # Compatibility read model only.  The live writer remains the one
            # ProactiveAuthority above; this alias lets older health consumers
            # observe the attention record family without reviving a second
            # authority component.
            attention_health = self._proactive_health_cache.get("attention")
            if isinstance(attention_health, Mapping):
                components["attention_threads"] = dict(attention_health)
        learning_component = components.get("learning")
        if learning_component is not None:
            store_status = str(learning_component.get("status") or "healthy")
            learning_component.update(self._learning_storage_health)
            if store_status == "failed":
                learning_component["status"] = "failed"
        components["subject_projection_worker"] = dict(
            self._subject_projection_health
        )
        components["authority_renewal"] = dict(self._storage_renewal_health)
        for (namespace, state_key), item in sorted(
            self._lost_singleton_health.items()
        ):
            components[f"singleton:{namespace}:{state_key}"] = dict(item)
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
            "authority_renewal": dict(self._storage_renewal_health),
            "learning": dict(self._learning_storage_health),
        }
        return dict(self._storage_health_cache)

    def health(self) -> dict[str, Any]:
        """返回一个轻量健康信息。"""
        snapshot = self.snapshot()
        snapshot["storage_runtime"] = dict(self._storage_health_cache)
        proactive_health = dict(self._proactive_health_cache)
        local_proactive = self._local_proactive_runtime
        if local_proactive is not None:
            proactive_health = local_proactive.cached_health_snapshot()
        snapshot["proactive_authority"] = proactive_health
        snapshot["learning"] = (
            self._learning_scheduler.get_state()
            if self._learning_scheduler is not None
            else {
                "status": "disabled",
                "reason": "learning scheduler is not active",
            }
        )
        if self._selectable_storage_enabled:
            components = self._storage_health_cache.get("components") or {}
            snapshot["raw_event_ledger"] = dict(components.get("life_event") or {})
            snapshot["consciousness_presence"] = dict(components.get("presence") or {})
            snapshot["world_projection"] = dict(components.get("world") or {})
            snapshot["subject_document"] = dict(
                components.get("subject_document") or {}
            )
            snapshot["attention_threads"] = dict(
                proactive_health.get("attention") or {}
            )
        else:
            if self._event_bus is not None:
                snapshot["raw_event_ledger"] = self._event_bus.store.health_snapshot()
            snapshot["consciousness_presence"] = (
                self.consciousness_registry.health_snapshot()
            )
            if self._world_projection is not None:
                snapshot["world_projection"] = self._world_projection.health_snapshot()
            authority_health = proactive_health.get("authority")
            if isinstance(authority_health, Mapping):
                attention_health = authority_health.get("attention")
            else:
                attention_health = proactive_health.get("attention")
            snapshot["attention_threads"] = dict(
                attention_health
                if isinstance(attention_health, Mapping)
                else {
                    "status": "failed",
                    "reason": "local proactive authority is not ready",
                }
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
        subject_statuses = {str(item.get("status") or "") for item in subject_profiles}
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
            snapshot["shared_sync"] = {
                "component": "offline_sync",
                "status": "degraded" if self._shared_sync_error else "disabled",
                "running": False,
                "outbox_backlog": 0,
                "degraded_reason": self._shared_sync_error,
                "enabled": self._shared_sync_effective_enabled,
                "configured_enabled": self._shared_sync_configured_enabled,
                "disabled_reason": self._shared_sync_disabled_reason,
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
        snapshot["subconscious_ingest"] = dict(self._subconscious_ingest_health)
        snapshot["message_persist"] = dict(self._message_persist_health)
        bus = getattr(self, "_opportunity_bus", None)
        if bus is not None:
            snapshot["opportunity_bus"] = bus.health_snapshot()
        else:
            snapshot["opportunity_bus"] = {
                "component": "opportunity_bus",
                "status": "disabled",
                "due_ids": [],
                "omitted_ids": [],
                "error_type": "",
            }
        try:
            from ..tools.todo_tools import TodoStorage, todo_health_snapshot

            snapshot["todo_board"] = todo_health_snapshot(
                TodoStorage(self._workspace_dir()).load(persist_migration=False)
            )
        except Exception as error:  # noqa: BLE001
            snapshot["todo_board"] = {
                "component": "todo_board",
                "status": "failed",
                "counts": {},
                "open_count": 0,
                "error_type": type(error).__name__,
            }
        return snapshot

    def runtime_state_store(self) -> Any | None:
        """Return the selected technical runtime store or explicit local mode.

        ``None`` is valid only when selectable storage is disabled. If MySQL is
        configured but the store did not attach, callers must fail closed
        instead of resurrecting local JSON state.
        """

        if self._runtime_state_store is not None:
            return self._runtime_state_store
        if self._selectable_storage_enabled:
            raise RuntimeError("SelectedRuntimeStateStorageNotStarted")
        return None

    async def read_runtime_state(
        self,
        namespace: str,
        state_key: str,
    ) -> Any | None:
        """Read one selected-backend technical state."""

        store = self.runtime_state_store()
        if store is None:
            return None
        return await store.get_state(namespace, state_key)

    async def write_runtime_state(
        self,
        *,
        namespace: str,
        state_key: str,
        expected_revision: int,
        schema_version: int,
        payload: dict[str, Any],
    ) -> Any:
        """CAS-write one selected-backend technical state."""

        store = self.runtime_state_store()
        if store is None:
            raise RuntimeError("SelectedRuntimeStateStorageDisabled")
        return await store.put_state(
            namespace=namespace,
            state_key=state_key,
            expected_revision=expected_revision,
            schema_version=schema_version,
            payload=payload,
        )

    async def read_subject_authority_texts(self) -> dict[str, str]:
        """Return SOUL/USER/MEMORY text from the single bound authority source.

        Under the selected backend the remote store is the only authority: the
        three documents are read in one consistent snapshot and a remote gap
        fails closed instead of degrading into stale local Markdown.
        """

        store = self._subject_document_store
        if store is not None:
            from ..core.router_context_projection import (
                subject_authority_sources_from_snapshot,
            )

            snapshot = await store.read_subject_authority()
            sources, _ = subject_authority_sources_from_snapshot(snapshot)
            return {source.path: source.text for source in sources}

        if self._selectable_storage_enabled:
            # The selected backend is configured but its store never opened;
            # reading local files here would silently resurrect the very
            # fallback this backend replaces.
            raise RuntimeError("SelectedSubjectStorageNotStarted")

        from ..core.router_context_projection import read_subject_authority_sources

        sources, _ = await asyncio.to_thread(
            read_subject_authority_sources,
            self._workspace_dir(),
        )
        return {source.path: source.text for source in sources}

    def _load_existence_primer(self) -> str:
        """Read the standing self-model primer from the workspace."""

        path = self._workspace_dir() / "EXISTENCE.md"
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"无法读取 EXISTENCE.md: {exc}")
        return ""

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
                subject_store=self._subject_document_store,
                runtime_store=self._runtime_state_store,
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
            getattr(chatter_cfg, "router_context_projection_max_chars", 6000) or 6000
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

    async def search_actor_memory(self, query: str, top_k: int = 5) -> str:
        """深度检索 life memory。"""
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
            # New association expansion is owned by the append-only living
            # ledger. Legacy weighted edges remain historical evidence only.
            enable_association=False,
            return_bundles=False,
        )
        expand_associations = getattr(
            memory_service,
            "expand_living_document_associations",
            None,
        )
        if callable(expand_associations):
            association_seed = int.from_bytes(
                hashlib.sha256(query_text.encode("utf-8")).digest()[:8],
                "big",
            ) & ((1 << 63) - 1)
            results = await expand_associations(
                results,
                context_key="life_engine/memory",
                random_seed=association_seed,
                limit=max(1, int(top_k)),
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
            logger.warning(
                f"[search_actor_memory] 构建可追溯记忆包失败，将使用普通摘要: {exc}"
            )
            bundles = []

        if bundles:
            workspace = self._workspace_dir()
            bundle_lines: list[str] = []
            for bundle in bundles[: max(1, int(top_k))]:
                file_meta = get_file_metadata(workspace / bundle.primary_path)
                meta_str = f"{file_meta['ext']} | {file_meta['time_ago']} | {file_meta['size']}"

                evidence_lines: list[str] = []
                for item in bundle.evidence[:4]:
                    label = (
                        "当前文件"
                        if item.file_path == bundle.primary_path
                        else "历史证据"
                    )
                    if item.relation:
                        label += f"/{item.relation}"
                    exists_note = (
                        "" if item.exists else "（当前路径不存在，仅作历史轨迹）"
                    )
                    snippet = _shorten_text(
                        " ".join((item.snippet or "").split()), max_length=160
                    )
                    evidence_lines.append(
                        f"  - {label}: {item.title or Path(item.file_path).name} "
                        f"[{item.file_path}]{exists_note}\n"
                        f"    摘要：{snippet or '无摘要'}"
                    )

                trace_lines: list[str] = []
                for trace in bundle.history_trace[:4]:
                    direction = "后来" if trace.direction == "later" else "早期"
                    reason = _shorten_text(
                        " ".join((trace.reason or "").split()), max_length=120
                    )
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
            snippet = _shorten_text(
                " ".join((result.snippet or "").split()), max_length=250
            )
            file_meta = get_file_metadata(workspace / result.file_path)
            meta_str = (
                f"{file_meta['ext']} | {file_meta['time_ago']} | {file_meta['size']}"
            )

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
                path_str = (
                    " → ".join(result.association_path[-3:])
                    if result.association_path
                    else ""
                )
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
                f"【直接命中的记忆】({len(direct_lines[:top_k])}条)\n"
                + "\n\n".join(direct_lines[:top_k])
            )
        if associated_lines:
            parts.append(
                f"【联想扩散结果】({len(associated_lines[:top_k])}条)\n"
                + "\n\n".join(associated_lines[:top_k])
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
        *,
        envelope: Mapping[str, Any] | None = None,
        adapter_signature: str = "",
    ) -> None:
        """记录聊天消息，并追加稳定的公共聊天事实。"""
        if not self._is_enabled():
            return

        if direction not in {"received", "sent"}:
            direction = "received"

        event = self._event_builder.build_message_event(message, direction=direction)
        # 分阶段计时：EventBus 处理器有 5 秒硬截止，超时时需要知道瓶颈在哪个阶段
        _phase_start = time.monotonic()
        _phase_enqueue = 0.0
        _phase_facts = 0.0
        _phase_context = 0.0
        unlocked_self_pause = False
        chat_fact = build_chat_message_event(
            message,
            direction="delivered" if direction == "sent" else "received",
            envelope=envelope,
            adapter_signature=adapter_signature,
        )

        def _apply_inbound_runtime_side_effects() -> bool:
            unlocked = False
            if direction == "received":
                self._state.last_external_message_at = event.timestamp
                self._state.last_external_stream_id = str(
                    getattr(event, "stream_id", "") or ""
                )
                self._state.last_external_source = str(
                    getattr(event, "source", "") or ""
                )
                unlocked = self._clear_self_pause_state()
                if unlocked:
                    self._state.consecutive_rest_count = 0
            return unlocked

        # 消息事实已通过 multi-writer bridge 的 operation/fact 持久化，这里
        # 保存的只是本地技术 checkpoint（global revision 推进）。双实例共享
        # MySQL 下该 key 必然并发竞争，冲突属合法竞争，走 recoverable 语义，
        # 避免并发提交把消息收集路径打成 message_collect_failed。
        # ⚠️ 2026-09-01 现状更正：当前 backend="local" 且 multi_writer_enabled=false，
        # multi-writer bridge 未注册，双实例共享场景不存在。上文的 recoverable
        # 语义是为多实例/多写者模式预留的防御；单实例下若仍出现该冲突，
        # 说明存在未知的并发写入源，应当排查而不是当作合法竞争放过。
        if self._message_persist_async_enabled():
            # EventBus 处理器有 5 秒硬截止。异步路径先入 pending 保住消息，
            # 再后台写账本；catch-up 负责「账本已写、checkpoint 未写」。
            async with self._get_lock():
                known = {
                    str(item.occurrence_id or item.event_id or "").strip()
                    for item in [*self._event_history, *self._pending_events]
                    if str(item.occurrence_id or item.event_id or "").strip()
                }
                identity = str(event.occurrence_id or event.event_id or "").strip()
                if not identity or identity not in known:
                    self._pending_events.append(event)
                    self._state.pending_event_count = len(self._pending_events)
                unlocked_self_pause = _apply_inbound_runtime_side_effects()
            _phase_enqueue = time.monotonic() - _phase_start
            self._schedule_message_persist(event, chat_fact)
            _phase_facts = 0.0
            _phase_context = 0.0
        else:
            _facts_start = time.monotonic()
            await self._publish_message_facts(event, chat_fact)
            _phase_facts = time.monotonic() - _facts_start
            await self._enqueue_pending_events([event], persist=False)
            async with self._get_lock():
                unlocked_self_pause = _apply_inbound_runtime_side_effects()
            _phase_enqueue = time.monotonic() - _phase_start
            _ctx_start = time.monotonic()
            await self._save_runtime_context(recoverable_on_shared_conflict=True)
            _phase_context = time.monotonic() - _ctx_start
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
        # 接近 EventBus 5 秒硬截止时告警，暴露冷路径竞态的具体慢阶段。
        # 单次偶发（远端 MySQL 网络波动）记 INFO；连续 2 次才 WARNING，
        # 避免单次网络抖动刷高噪声。
        _total_elapsed = time.monotonic() - _phase_start
        if _total_elapsed >= 4.0:
            self._slow_record_message_streak += 1
            _slow_log = (
                logger.warning
                if self._slow_record_message_streak >= 2
                else logger.info
            )
            _slow_log(
                f"life_engine record_message 接近 EventBus 超时阈值: "
                f"total={_total_elapsed:.2f}s enqueue={_phase_enqueue:.2f}s "
                f"facts={_phase_facts:.2f}s context={_phase_context:.2f}s "
                f"message_id={event.event_id} stream_id={event.stream_id or ''} "
                f"direction={direction}"
            )
        else:
            self._slow_record_message_streak = 0

    def _get_curiosity_engine(self) -> CuriosityEngine:
        cfg = self._cfg()
        curiosity_cfg = getattr(cfg, "curiosity", None)
        task_name = (
            str(getattr(curiosity_cfg, "task_name", "") or "").strip()
            or str(getattr(cfg.model, "task_name", "") or "").strip()
            or "core"
        )
        timeout = float(getattr(curiosity_cfg, "timeout_seconds", 30.0) or 30.0)
        workspace = str(
            getattr(cfg.settings, "workspace_path", "") or self._workspace_dir()
        )
        if (
            self._curiosity_engine is None
            or self._curiosity_engine.workspace_path != workspace
            or self._curiosity_engine.model_task_name != task_name
        ):
            self._curiosity_engine = CuriosityEngine(
                workspace_path=workspace,
                model_task_name=task_name,
                timeout_seconds=timeout,
                runtime_store=self.runtime_state_store(),
            )
        return self._curiosity_engine

    def _message_persist_async_enabled(self) -> bool:
        """慢阶段是否后台化。

        默认启用；出现顺序或持久化语义问题时可在 life_engine 配置的
        ``storage`` 段设 ``message_persist_async = false`` 立即回退，
        无需改动代码。
        """

        cfg = self._cfg()
        storage_cfg = getattr(cfg, "storage", None)
        if storage_cfg is None:
            return True
        return bool(getattr(storage_cfg, "message_persist_async", True))

    def _schedule_message_persist(
        self, event: LifeEngineEvent, chat_fact: LifeEvent
    ) -> None:
        """把消息事实持久化与 checkpoint 推进交给后台串行任务。"""

        get_task_manager().create_task(
            self._run_message_persist(event, chat_fact),
            name=f"life_message_persist_{event.sequence}",
            daemon=True,
            timeout=120.0,
        )

    async def _run_message_persist(
        self, event: LifeEngineEvent, chat_fact: LifeEvent
    ) -> None:
        """后台串行执行慢阶段，并记录耗时以便继续观察瓶颈。"""

        started = time.monotonic()
        facts_elapsed = 0.0
        ctx_elapsed = 0.0
        last_error: Exception | None = None
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                async with self._message_persist_lock:
                    facts_start = time.monotonic()
                    await self._publish_message_facts(event, chat_fact)
                    facts_elapsed = time.monotonic() - facts_start
                    ctx_start = time.monotonic()
                    await self._save_runtime_context(
                        recoverable_on_shared_conflict=True
                    )
                    ctx_elapsed = time.monotonic() - ctx_start
                self._message_persist_health = {
                    "status": "healthy",
                    "consecutive_failures": 0,
                    "error_type": "",
                }
                last_error = None
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._state_dirty = True
                self._message_persist_health = {
                    "status": "failed",
                    "consecutive_failures": attempt,
                    "error_type": type(exc).__name__,
                }
                logger.warning(
                    "life_engine 消息慢阶段后台持久化失败: "
                    f"attempt={attempt}/{attempts} "
                    f"error_type={type(exc).__name__} error={exc} "
                    f"message_id={event.event_id} sequence={event.sequence}"
                )
                if attempt < attempts:
                    await asyncio.sleep(0.05 * attempt)
        if last_error is not None:
            logger.error(
                "life_engine 消息慢阶段后台持久化在有界重试后仍失败，"
                "已保留脏 checkpoint 与 pending: "
                f"error_type={type(last_error).__name__} "
                f"message_id={event.event_id} sequence={event.sequence}"
            )
            return

        total = time.monotonic() - started
        if total >= 4.0:
            logger.info(
                "life_engine 消息慢阶段后台持久化仍偏慢: "
                f"total={total:.2f}s facts={facts_elapsed:.2f}s "
                f"context={ctx_elapsed:.2f}s message_id={event.event_id}"
            )

    def _schedule_curiosity_review(
        self, message: Message, event: LifeEngineEvent
    ) -> None:
        cfg = self._cfg()
        curiosity_cfg = getattr(cfg, "curiosity", None)
        if curiosity_cfg is not None and not bool(
            getattr(curiosity_cfg, "enabled", True)
        ):
            return
        if self._curiosity_inflight:
            logger.debug("认知机会候选生成仍在运行，跳过本次重复调度")
            return

        self._curiosity_inflight = True
        get_task_manager().create_task(
            self._run_curiosity_review(message, event),
            name=f"life_epistemic_opportunity_{event.sequence}",
            daemon=True,
            timeout=float(getattr(curiosity_cfg, "timeout_seconds", 30.0) or 30.0)
            + 5.0,
        )

    async def _run_curiosity_review(
        self, message: Message, event: LifeEngineEvent
    ) -> None:
        try:
            cfg = self._cfg()
            curiosity_cfg = getattr(cfg, "curiosity", None)
            max_history = (
                int(getattr(curiosity_cfg, "history_messages", 20) or 20)
                if curiosity_cfg is not None
                else 20
            )
            prefix_prompt = await self._build_curiosity_prefix_prompt()
            history_text = await self._build_curiosity_history_text(
                message, max_messages=max_history
            )
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
                source_instance_id=event.source_instance_id
                or self.resolve_consciousness_instance(event.stream_id or ""),
            )
            if signal.active:
                logger.info(
                    "认知机会候选已生成: "
                    f"source_event_id={event.event_id} sequence={event.sequence}"
                )
            else:
                logger.debug("认知机会生成器本轮未提供新候选")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"认知机会候选生成失败: {exc}")
        finally:
            self._curiosity_inflight = False

    async def _build_meme_awareness_text(self) -> str:
        """构建“未浏览表情包”的来源提示，供认知机会生成器参考。"""
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

    async def _build_curiosity_prefix_prompt(self) -> str:
        # 好奇层与聊天表达面对同一个主体，因此必须读同一份权威文本：
        # 统一入口在选定后端下只认远端单事务快照，缺失时失败关闭，
        # 不会退回本地 Markdown 让好奇层看到与表达层不同的自我。
        texts = await self.read_subject_authority_texts()
        memory_text = ""
        memory_raw = texts.get("MEMORY.md", "")
        if memory_raw:
            memory_data = analyze_memory_text(memory_raw)
            memory_text = (
                render_memory_prompt(memory_data, mode="chat")
                if memory_data.raw_text
                else ""
            )

        return LifeChatterContextAssembler.build_prefix_prompt(
            soul_text=texts.get("SOUL.md", ""),
            user_text=texts.get("USER.md", ""),
            memory_text=memory_text,
            existence_text=self._load_existence_primer(),
            tools_text="",
            live_guidance="",
            primary_tool_guide="",
        )

    async def _build_curiosity_history_text(
        self, message: Message, *, max_messages: int
    ) -> str:
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
                }
                - {""},
            )
            if text:
                return text

        async with self._get_lock():
            message_events = [
                item
                for item in [*self._event_history, *self._pending_events]
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
        """Reject the retired stream-bound follow-up scheduler."""

        del chat_stream, delay_seconds, thought, topic, followup_type, source
        return False, (
            "LegacyFollowupReadOnly: use InitiativeSeed for future continuity; "
            "choose an audience and surface explicitly at action time."
        )

    def _inner_dialogue_store(self):
        ledger = getattr(self, "_inner_dialogue_ledger", None)
        if ledger is None:
            from ..inner_dialogue.protocol import InnerDialogueLedger

            ledger = InnerDialogueLedger()
            self._inner_dialogue_ledger = ledger
        return ledger

    def _sync_inner_dialogue_state(self) -> None:
        self._state.inner_dialogue_ledger = self._inner_dialogue_store().to_dict()

    def _restore_inner_dialogue_ledger(self) -> None:
        from ..inner_dialogue.protocol import InnerDialogueLedger

        ledger = InnerDialogueLedger.from_dict(
            getattr(self._state, "inner_dialogue_ledger", None) or {}
        )
        ledger.apply_events([*self._event_history, *self._pending_events])
        self._inner_dialogue_ledger = ledger
        self._sync_inner_dialogue_state()

    def _expression_instance_id_for_stream(self, stream_id: str) -> str:
        identity = str(stream_id or "").strip()
        registry = getattr(self, "_consciousness_registry", None)
        if registry is None or not identity:
            return ""
        getter = getattr(registry, "get_for_stream", None)
        if not callable(getter):
            return ""
        instance = getter(identity)
        if instance is None:
            return ""
        return str(getattr(instance, "instance_id", "") or "").strip()

    @staticmethod
    def _inner_return_trigger_message_id(return_occurrence_id: str) -> str:
        digest = hashlib.sha256(
            str(return_occurrence_id or "").strip().encode("utf-8")
        ).hexdigest()
        return f"inner_return_{digest}"

    async def _ensure_expression_stream_loop(self, stream_id: str) -> None:
        """Start the stream driver after a synthetic expression wake."""

        from src.core.transport.distribution.stream_loop_manager import (
            get_stream_loop_manager,
        )

        exact_stream_id = str(stream_id or "").strip()
        if not exact_stream_id:
            raise RuntimeError("ExpressionStreamHasNoId")
        started = await get_stream_loop_manager().start_stream_loop(exact_stream_id)
        if not started:
            raise RuntimeError("ExpressionStreamLoopStartFailed")

    async def list_inner_dialogue_records(self) -> tuple[Any, ...]:
        """Return open inner-dialogue receipts in ledger order."""

        return self._inner_dialogue_store().open_records()

    async def get_inner_dialogue_record(self, receipt_id: str) -> Any | None:
        return self._inner_dialogue_store().get(str(receipt_id or "").strip())

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
        source_instance_id: str = "",
    ) -> dict[str, Any]:
        """接收主意识沉下来的内心对话（异步，进入中枢心跳处理）。"""
        from ..inner_dialogue.protocol import (
            INNER_DIALOGUE_OPEN_LIMIT,
            InnerDialogueOpenLimitExceeded,
        )

        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        text = str(thought or "").strip()
        if not text:
            raise ValueError("thought 不能为空")

        mode_name = str(mode or "reflect").strip().lower() or "reflect"
        if mode_name not in {"notice", "reflect", "gap", "decide"}:
            mode_name = "reflect"

        wants_return = bool(expect_surface)
        if wants_return:
            open_count = self._inner_dialogue_store().open_count()
            if open_count >= INNER_DIALOGUE_OPEN_LIMIT:
                raise InnerDialogueOpenLimitExceeded(open_count=open_count)

        receipt_id = f"idlg_{uuid4().hex[:12]}"
        resolved_stream = str(stream_id or "").strip()
        resolved_instance = str(source_instance_id or "").strip()
        if not resolved_instance:
            resolved_instance = self._expression_instance_id_for_stream(resolved_stream)
        event = self._event_builder.build_inner_dialogue_event(
            text,
            mode=mode_name,
            expect_surface=wants_return,
            receipt_id=receipt_id,
            stream_id=resolved_stream,
            platform=platform or "life_chatter",
            chat_type=chat_type,
            sender_name=sender_name or "主意识",
            source_instance_id=resolved_instance,
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
            f"expect_surface={wants_return} "
            f"pending={self._state.pending_event_count}"
        )
        return_blocked = ""
        if wants_return and not resolved_stream:
            return_blocked = "missing_stream"
        return {
            "event_id": event.event_id,
            "receipt_id": receipt_id,
            "mode": mode_name,
            "expect_surface": wants_return,
            "stream_id": event.stream_id or "",
            "source_instance_id": resolved_instance,
            "return_blocked": return_blocked,
            "pending_event_count": self._state.pending_event_count,
            "queued": True,
            "channel": "inner_dialogue",
        }

    async def return_inner_dialogue(
        self,
        *,
        receipt_id: str,
        statement: str,
        occurrence_id: str,
        actor_consciousness_instance_id: str,
        source_instance_id: str = "",
        causation_id: str = "",
    ) -> dict[str, Any]:
        """Heartbeat explicitly returns one inner dialogue to its originating stream."""
        from ..inner_dialogue.protocol import (
            InnerDialogueConflict,
            InnerDialogueReturnBlocked,
        )

        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        identity = str(receipt_id or "").strip()
        note = str(statement or "").strip()
        occurrence = str(occurrence_id or "").strip()
        actor = str(actor_consciousness_instance_id or "").strip()
        if not identity:
            raise InnerDialogueReturnBlocked("receipt_required")
        if not note:
            raise InnerDialogueReturnBlocked("statement_required", receipt_id=identity)
        if not occurrence:
            raise InnerDialogueReturnBlocked("occurrence_required", receipt_id=identity)
        if not actor:
            raise InnerDialogueReturnBlocked("actor_required", receipt_id=identity)

        ledger = self._inner_dialogue_store()
        record = ledger.get(identity)
        if record is None:
            raise InnerDialogueReturnBlocked("unknown_receipt", receipt_id=identity)
        if not record.expect_surface:
            raise InnerDialogueReturnBlocked("expect_surface_false", receipt_id=identity)
        statement_sha = hashlib.sha256(note.encode("utf-8")).hexdigest()
        if record.status == "returned":
            if record.return_occurrence_id != occurrence:
                raise InnerDialogueReturnBlocked("already_returned", receipt_id=identity)
            if record.return_statement_sha256 != statement_sha:
                raise InnerDialogueConflict(
                    receipt_id=identity,
                    occurrence_id=occurrence,
                )
            result: dict[str, Any] = {
                "authority_committed": True,
                "record_family": "inner_dialogue",
                "record_id": identity,
                "status": "returned",
                "event_id": record.return_event_id,
                "occurrence_id": occurrence,
                "stream_id": record.stream_id,
                "idempotent_replay": True,
                "message_sent": False,
                "expression_wake_enqueued": False,
                "delivery_pending": not record.wake_delivered,
            }
            if record.wake_delivered:
                result["expression_wake_enqueued"] = True
                return result
            try:
                await self._deliver_pending_inner_return(record)
                result["expression_wake_enqueued"] = True
                result["delivery_pending"] = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - return event is durable
                result["delivery_error_type"] = type(exc).__name__
            return result

        if record.status != "open":
            raise InnerDialogueReturnBlocked("not_open", receipt_id=identity)
        if not record.stream_id:
            raise InnerDialogueReturnBlocked("missing_stream", receipt_id=identity)

        event = self._event_builder.build_inner_dialogue_return_event(
            receipt_id=identity,
            statement=note,
            stream_id=record.stream_id,
            occurrence_id=occurrence,
            actor_consciousness_instance_id=actor,
            causation_id=causation_id or record.sink_event_id or identity,
            source_instance_id=source_instance_id or actor,
        )
        await self._queue_pending_event(event)
        result = {
            "authority_committed": True,
            "record_family": "inner_dialogue",
            "record_id": identity,
            "status": "returned",
            "event_id": event.event_id,
            "occurrence_id": occurrence,
            "stream_id": record.stream_id,
            "idempotent_replay": False,
            "message_sent": False,
            "expression_wake_enqueued": False,
            "delivery_pending": True,
        }
        try:
            await self._deliver_pending_inner_return(
                self._inner_dialogue_store().get(identity)
            )
            result["expression_wake_enqueued"] = True
            result["delivery_pending"] = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - return event is durable
            result["delivery_error_type"] = type(exc).__name__
            logger.warning(
                "内心对话回声已提交，表达层将在后台重放: "
                f"error_type={type(exc).__name__}"
            )
        return result

    async def _deliver_pending_inner_return(self, record: Any) -> None:
        """Idempotently wake the originating stream for one returned dialogue."""

        from ..inner_dialogue.protocol import InnerDialogueReturnBlocked

        if record is None:
            raise InnerDialogueReturnBlocked("unknown_receipt")

        if not record.stream_id:
            raise InnerDialogueReturnBlocked(
                "missing_stream",
                receipt_id=record.receipt_id,
            )
        if record.wake_delivered:
            return
        trigger_message_id = str(record.trigger_message_id or "").strip() or (
            self._inner_return_trigger_message_id(record.return_occurrence_id)
        )
        await self._wake_stream_for_inner_return(
            stream_id=record.stream_id,
            platform=record.platform,
            receipt_id=record.receipt_id,
            statement=record.return_statement,
            return_occurrence_id=record.return_occurrence_id,
            trigger_message_id=trigger_message_id,
        )
        self._inner_dialogue_store().mark_wake_enqueued(
            record.receipt_id,
            trigger_message_id=trigger_message_id,
        )
        delivery = self._event_builder.build_inner_dialogue_return_delivery_event(
            receipt_id=record.receipt_id,
            return_occurrence_id=record.return_occurrence_id,
            stream_id=record.stream_id,
            trigger_message_id=trigger_message_id,
            causation_id=record.return_event_id or record.return_occurrence_id,
        )
        await self._queue_pending_event(delivery)

    async def _wake_stream_for_inner_return(
        self,
        *,
        stream_id: str,
        platform: str,
        receipt_id: str,
        statement: str,
        return_occurrence_id: str,
        trigger_message_id: str = "",
    ) -> str:
        """Wake the originating expression window with an internal return envelope."""

        import time

        from src.core.managers import get_stream_manager
        from src.core.models.message import Message, MessageType
        from src.core.transport.distribution.stream_loop_manager import (
            get_stream_loop_manager,
        )

        from ..inner_dialogue.protocol import INNER_RETURN_SENDER_ID

        exact_stream_id = str(stream_id or "").strip()
        if not exact_stream_id:
            raise RuntimeError("InnerReturnHasNoStream")
        chat_stream = await get_stream_manager().get_or_create_stream(
            stream_id=exact_stream_id
        )
        prompt = (
            "这是你自己沉下去的内心对话回声，不是用户，也不是外联。"
            "请在这个窗口的真实上下文中重新判断：可以说、再 inner_dialogue、"
            "或 life_pass_and_wait 保持沉默。不要把回声原文机械当成对外回复。\n"
            f"receipt={receipt_id}\n"
            f"潜意识回声：{statement}"
        )
        expected_trigger_message_id = self._inner_return_trigger_message_id(
            return_occurrence_id
        )
        exact_trigger_message_id = str(trigger_message_id or "").strip()
        if exact_trigger_message_id and (
            exact_trigger_message_id != expected_trigger_message_id
        ):
            raise RuntimeError("InnerReturnTriggerIdentityMismatch")
        exact_trigger_message_id = expected_trigger_message_id
        trigger_message = Message(
            message_id=exact_trigger_message_id,
            platform=chat_stream.platform or platform or "unknown",
            stream_id=exact_stream_id,
            sender_id=INNER_RETURN_SENDER_ID,
            sender_name="系统（潜意识回声）",
            sender_role="other",
            content=prompt,
            processed_plain_text=prompt,
            message_type=MessageType.TEXT,
            time=time.time(),
            is_inner_return_trigger=True,
            inner_return_receipt_id=receipt_id,
            inner_return_occurrence_id=return_occurrence_id,
            bypass_message_buffer=True,
        )
        chat_stream.context.add_unread_message(trigger_message)
        removed = get_stream_loop_manager()._wait_states.pop(exact_stream_id, None)
        if removed:
            logger.debug(
                f"[{exact_stream_id[:8]}] 已清除等待锁，准备承接内心对话回声"
            )
        await self._ensure_expression_stream_loop(exact_stream_id)
        return exact_trigger_message_id

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

    def _autonomy_store(self) -> Any:
        if self._autonomy_intent_store is None:
            runtime_store = self.runtime_state_store()
            if runtime_store is not None:
                self._autonomy_intent_store = SelectedAutonomyIntentStore(runtime_store)
            else:
                self._autonomy_intent_store = AsyncLocalAutonomyIntentStore(
                    self._workspace_dir()
                )
        return self._autonomy_intent_store

    def life_trace_store(self) -> Any:
        if self._life_trace_store is None:
            from ..trace.store import (
                AsyncLocalLifeTraceStore,
                SelectedLifeTraceStore,
            )

            runtime_store = self.runtime_state_store()
            self._life_trace_store = (
                SelectedLifeTraceStore(runtime_store)
                if runtime_store is not None
                else AsyncLocalLifeTraceStore(self._workspace_dir())
            )
        return self._life_trace_store

    def narrative_store(self) -> Any:
        if self._narrative_store is None:
            from ..narrative.store import (
                AsyncLocalNarrativeStore,
                SelectedNarrativeStore,
            )

            runtime_store = self.runtime_state_store()
            self._narrative_store = (
                SelectedNarrativeStore(runtime_store)
                if runtime_store is not None
                else AsyncLocalNarrativeStore(self._workspace_dir())
            )
        return self._narrative_store

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
        """Reject legacy stream-bound intent mutation without touching evidence."""

        raise RuntimeError(
            "LegacyAutonomyReadOnly: use InitiativeSeed and an explicit "
            "audience/surface decision"
        )

    async def claim_autonomy_occurrences(
        self,
        occurrences: list[dict[str, str]],
        *,
        action_id: str,
        target_stream_id: str,
    ) -> dict[str, Any]:
        """Reject legacy delivery claims; empty historical callbacks are no-ops."""

        if not occurrences:
            return {"claimed": True, "count": 0}
        return {
            "claimed": False,
            "count": 0,
            "reason": "legacy_autonomy_read_only",
        }

    async def complete_autonomy_occurrences(
        self,
        occurrences: list[dict[str, str]],
        *,
        outcome: str,
        action_id: str = "",
        detail: str = "",
    ) -> dict[str, Any]:
        """Reject legacy receipts; empty historical callbacks are no-ops."""

        if not occurrences:
            return {"completed": 0, "scheduled": 0}
        return {
            "completed": 0,
            "scheduled": 0,
            "reason": "legacy_autonomy_read_only",
        }

    async def manage_autonomy_intent(
        self,
        *,
        action: str,
        intent_id: str = "",
        additional_occurrences: int = 0,
        lease_minutes: int = 0,
    ) -> dict[str, Any]:
        """Read the immutable legacy archive; reject every mutation."""

        normalized_action = str(action or "").strip().lower()
        if normalized_action != "list":
            raise RuntimeError(
                "LegacyAutonomyReadOnly: legacy intent mutation is retired"
            )
        intents = await self._autonomy_store().load()
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
                for item in intents
            ]
        }

    async def trigger_autonomy_intent(self, intent_id: str) -> dict[str, Any]:
        """Reject legacy scheduler callbacks without reading or mutating evidence."""

        return {
            "triggered": False,
            "reason": "legacy_autonomy_read_only",
            "intent_id": str(intent_id or ""),
        }

    async def _wake_stream_for_initiative(
        self,
        *,
        stream_id: str,
        platform: str,
        command: InitiativeOutreachCommand,
        trigger_message_id: str = "",
        turn_id: str = "",
    ) -> str:
        """Wake one explicitly selected physical surface for fresh expression."""

        import time

        from src.core.managers import get_stream_manager
        from src.core.transport.distribution.stream_loop_manager import (
            get_stream_loop_manager,
        )

        exact_stream_id = str(stream_id or "").strip()
        if not exact_stream_id:
            raise RuntimeError("InitiativeSurfaceHasNoStream")
        chat_stream = await get_stream_manager().get_or_create_stream(
            stream_id=exact_stream_id
        )
        prompt = (
            "主体刚刚明确选择发起一次外联。请在这个表面的真实上下文中重新判断"
            "如何表达，或是否仍要保持沉默；不要把意向原文机械当成最终回复。\n"
            f"对象引用：{command.audience_ref}\n"
            f"主体公开意向：{command.public_intention}"
        )
        expected_trigger_message_id = self._initiative_outreach_trigger_message_id(
            command.occurrence_id
        )
        exact_trigger_message_id = str(trigger_message_id or "").strip()
        if exact_trigger_message_id and (
            exact_trigger_message_id != expected_trigger_message_id
        ):
            raise RuntimeError("InitiativeTriggerIdentityMismatch")
        exact_trigger_message_id = expected_trigger_message_id
        trigger_message = Message(
            message_id=exact_trigger_message_id,
            platform=chat_stream.platform or platform or "unknown",
            stream_id=exact_stream_id,
            # This is an internal transport envelope, not a fabricated message
            # from whichever human happened to speak most recently on the
            # selected surface.
            sender_id="life_engine_initiative",
            sender_name="系统（主体主动外联）",
            sender_role="other",
            content=prompt,
            processed_plain_text=prompt,
            message_type=MessageType.TEXT,
            time=time.time(),
            is_initiative_outreach_trigger=True,
            initiative_outreach_occurrence_id=command.occurrence_id,
            initiative_outreach_turn_id=str(turn_id or "").strip(),
            initiative_audience_ref=command.audience_ref,
            initiative_surface_ref=command.surface_ref,
            initiative_seed_id=command.seed_id,
            initiative_seed_revision=command.seed_revision,
            bypass_message_buffer=True,
        )
        chat_stream.context.add_unread_message(trigger_message)
        removed = get_stream_loop_manager()._wait_states.pop(exact_stream_id, None)
        if removed:
            logger.debug(
                f"[{exact_stream_id[:8]}] 已清除等待锁，准备承接主体主动外联"
            )
        await self._ensure_expression_stream_loop(exact_stream_id)
        return exact_trigger_message_id

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

        纯文本独白使用 chatter_inner_monologue 事件记录；这里按 stream
        写入，供 life_send_text 结构化字段和已持久化状态使用。
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
        # chatter 思考快照属可重建投影；共享多写者模式并发冲突时可恢复，
        # 采纳远端值不会丢失权威消息事实。
        await self._save_runtime_context(recoverable_on_shared_conflict=True)

        return {
            "stream_id": sid,
            "recorded_at": snapshot["recorded_at"],
            "channel": "chatter_think_snapshot",
        }

    @staticmethod
    def _conscious_tool_activity_id(
        *,
        model_turn_activity_id: str,
        source_instance_id: str,
        stream_id: str,
        turn_occurrence_id: str,
        call_id: str,
        tool_name: str,
        surface: str = "life_chatter",
    ) -> str:
        """Derive one stable, content-free identity for a model-chosen action."""

        identity = "\x1f".join(
            (
                str(model_turn_activity_id or "").strip(),
                str(source_instance_id or "").strip(),
                str(stream_id or "").strip(),
                str(turn_occurrence_id or "").strip(),
                str(call_id or "").strip(),
                str(tool_name or "").strip(),
                str(surface or "life_chatter").strip(),
            )
        )
        return "conscious_activity_" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _conscious_model_turn_activity_id(
        *,
        source_instance_id: str,
        stream_id: str,
        turn_occurrence_id: str,
        transport_request_id: str,
        surface: str = "life_chatter",
    ) -> str:
        """Derive one stable identity for a successful model generation."""

        identity = "\x1f".join(
            (
                str(source_instance_id or "").strip(),
                str(stream_id or "").strip(),
                str(turn_occurrence_id or "").strip(),
                str(transport_request_id or "").strip(),
                str(surface or "life_chatter").strip(),
            )
        )
        return "conscious_model_turn_" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()

    async def record_conscious_model_turn(
        self,
        *,
        stream_id: str,
        source_instance_id: str,
        turn_occurrence_id: str,
        transport_request_id: str,
        provider_reasoning_content: str,
        assistant_message: str,
        calls: list[Mapping[str, Any]],
        surface: str = "life_chatter",
    ) -> dict[str, str]:
        """Persist one complete generation and its chosen tools atomically."""

        model_turn_activity_id = self._conscious_model_turn_activity_id(
            source_instance_id=source_instance_id,
            stream_id=stream_id,
            turn_occurrence_id=turn_occurrence_id,
            transport_request_id=transport_request_id,
            surface=surface,
        )
        events = [
            self._event_builder.build_conscious_model_turn_event(
                activity_id=model_turn_activity_id,
                transport_request_id=transport_request_id,
                stream_id=stream_id,
                source_instance_id=source_instance_id,
                turn_occurrence_id=turn_occurrence_id,
                provider_reasoning_content=provider_reasoning_content,
                assistant_message=assistant_message,
                tool_call_ids=[str(call.get("call_id") or "") for call in calls],
                surface=surface,
            )
        ]
        activity_ids: dict[str, str] = {}
        for call in calls:
            call_id = str(call.get("call_id") or "").strip()
            tool_name = str(call.get("tool_name") or "").strip()
            raw_args = call.get("arguments")
            if not call_id or not tool_name or not isinstance(raw_args, Mapping):
                raise ValueError("conscious tool call attribution is incomplete")
            args = {str(key): value for key, value in raw_args.items()}
            activity_id = self._conscious_tool_activity_id(
                model_turn_activity_id=model_turn_activity_id,
                source_instance_id=source_instance_id,
                stream_id=stream_id,
                turn_occurrence_id=turn_occurrence_id,
                call_id=call_id,
                tool_name=tool_name,
                surface=surface,
            )
            activity_ids[call_id] = activity_id
            events.append(
                self._event_builder.build_conscious_tool_call_event(
                    tool_name,
                    args,
                    activity_id=activity_id,
                    model_turn_activity_id=model_turn_activity_id,
                    call_id=call_id,
                    stream_id=stream_id,
                    source_instance_id=source_instance_id,
                    turn_occurrence_id=turn_occurrence_id,
                    surface=surface,
                )
            )
        await self._queue_pending_events(events)
        return activity_ids

    async def record_conscious_activity_state(
        self,
        *,
        stream_id: str,
        source_instance_id: str,
        occurrence_id: str,
        state_kind: str,
        payload: Mapping[str, Any],
        surface: str,
        causation_id: str = "",
        correlation_id: str = "",
    ) -> LifeEngineEvent:
        """Persist one attributed wait/interruption/completion state."""

        identity_material = "\x1f".join(
            (
                str(surface or "").strip(),
                str(source_instance_id or "").strip(),
                str(stream_id or "").strip(),
                str(occurrence_id or "").strip(),
                str(state_kind or "").strip(),
            )
        )
        activity_id = "conscious_state_" + hashlib.sha256(
            identity_material.encode("utf-8")
        ).hexdigest()
        event = self._event_builder.build_conscious_activity_state_event(
            activity_id=activity_id,
            stream_id=stream_id,
            source_instance_id=source_instance_id,
            occurrence_id=occurrence_id,
            state_kind=state_kind,
            payload={str(key): value for key, value in payload.items()},
            surface=surface,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        await self._queue_pending_events([event])
        return event

    async def record_conscious_tool_calls(
        self,
        *,
        stream_id: str,
        source_instance_id: str,
        turn_occurrence_id: str,
        calls: list[Mapping[str, Any]],
        surface: str = "life_chatter",
    ) -> dict[str, str]:
        """Compatibility wrapper for callers without model-turn material."""

        return await self.record_conscious_model_turn(
            stream_id=stream_id,
            source_instance_id=source_instance_id,
            turn_occurrence_id=turn_occurrence_id,
            transport_request_id=f"legacy-tool-turn:{turn_occurrence_id}",
            provider_reasoning_content="",
            assistant_message="",
            calls=calls,
            surface=surface,
        )

    async def record_conscious_tool_results(
        self,
        *,
        stream_id: str,
        source_instance_id: str,
        turn_occurrence_id: str,
        activity_ids: Mapping[str, str],
        results: list[Mapping[str, Any]],
        surface: str = "life_chatter",
    ) -> None:
        """Persist each real/suppressed tool outcome with its chosen activity."""

        events: list[LifeEngineEvent] = []
        for result in results:
            call_id = str(result.get("call_id") or "").strip()
            tool_name = str(result.get("tool_name") or "").strip()
            activity_id = str(activity_ids.get(call_id) or "").strip()
            if not call_id or not tool_name or not activity_id:
                raise ValueError("conscious tool result attribution is incomplete")
            events.append(
                self._event_builder.build_conscious_tool_result_event(
                    tool_name,
                    result.get("result"),
                    bool(result.get("success")),
                    activity_id=activity_id,
                    call_id=call_id,
                    stream_id=stream_id,
                    source_instance_id=source_instance_id,
                    turn_occurrence_id=turn_occurrence_id,
                    technical_outcome=str(
                        result.get("technical_outcome") or ""
                    ),
                    delivery_receipt_sha256=str(
                        result.get("delivery_receipt_sha256") or ""
                    ),
                    delivery_message_id=str(
                        result.get("delivery_message_id") or ""
                    ),
                    delivery_proof_status=str(
                        result.get("delivery_proof_status") or ""
                    ),
                    surface=surface,
                )
            )
        await self._queue_pending_events(events)

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
        result: Any,
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
            occurrence_id = f"life-agent-result:{agent_id}"
            event.occurrence_id = occurrence_id
            event.content_ref = f"life-event-occurrence:{occurrence_id}"
            events.append(event)

        try:
            await self._queue_pending_events(events)
        except BaseException:
            restore = getattr(coordinator, "restore_results", None)
            if callable(restore):
                await restore(results)
            raise

    async def _collect_background_mission_results(self) -> None:
        """收集已完成的后台使命结果，注入为事件。"""
        try:
            from ..agents.contracts import MissionStatus
            from ..agents.mission_tool import get_all_missions
        except ImportError:
            return

        missions = get_all_missions()
        if not missions:
            return

        events: list[LifeEngineEvent] = []
        collected_ids: list[str] = []
        for mission_id, mission in missions.items():
            # 只收集已完成的后台使命
            if mission.sync:
                continue
            if mission.status not in (
                MissionStatus.SUCCEEDED,
                MissionStatus.PARTIAL,
                MissionStatus.FAILED,
                MissionStatus.CANCELLED,
                MissionStatus.TIMEOUT,
            ):
                continue
            # 检查是否已经被收集过
            if getattr(mission, "_collected", False):
                continue

            event = self._event_builder.build_agent_result_event(
                agent_type="mission",
                result_text=mission.summary_text(),
                success=mission.status == MissionStatus.SUCCEEDED,
                rounds=mission.progress[0],
                duration_ms=int(mission.elapsed_seconds * 1000),
            )
            occurrence_id = f"life-mission-result:{mission_id}"
            event.occurrence_id = occurrence_id
            event.content_ref = f"life-event-occurrence:{occurrence_id}"
            events.append(event)
            collected_ids.append(str(mission_id))

        if not events:
            return
        await self._queue_pending_events(events)
        for mission_id in collected_ids:
            mission = missions.get(mission_id)
            if mission is not None:
                mission._collected = True  # type: ignore[attr-defined]

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
        """将事件追加到原始历史；确认后的压缩由 heartbeat commit 统一执行。

        ``persist=True`` 仅被心跳 round 内调用方使用（后台智能体/使命结果、
        心跳模型回复），共享多写者模式下 CAS 冲突是可恢复的合法竞争，因此
        持久化走 recoverable 语义，避免并发提交导致整轮心跳失败。
        """
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
            await self._save_runtime_context(recoverable_on_shared_conflict=True)

    async def get_recent_subconscious_context(
        self,
        *,
        group_limit: int | None = None,
        max_bytes: int | None = None,
        include_tool_payloads: bool = True,
    ) -> RecentSubconsciousContext:
        """Return the same bounded recent subconscious activity to any instance.

        This is a pure read-only projection over committed LifeEngine history. It
        does not drain pending events, move a heartbeat or consumer cursor, read a
        private conversation payload, or create a new event. Callers may append the
        returned text to their own transient prompt without sharing their rolling
        context with another consciousness instance.
        """

        async with self._get_lock():
            history = list(self._event_history)
        return self._subconscious_context.project_recent(
            history,
            group_limit=group_limit,
            max_bytes=max_bytes,
            include_tool_payloads=include_tool_payloads,
        )

    @staticmethod
    def _expression_unseen_note(
        events: list[LifeEngineEvent],
        stream_lookup: Callable[[str], Any],
    ) -> str:
        """Build a factual delivery note for messages the expression layer
        has not been shown yet.

        The heartbeat drains the pending life-event queue, which is
        independent of the expression chatter's stream unread queue.  Without
        this note the consciousness model may wrongly assume the expression
        layer will handle a message it has never seen.  The note only reports
        queue facts; whether and how to respond remains the subject's choice.
        """

        unseen: list[str] = []
        for event in events:
            if event.event_type != EventType.MESSAGE:
                continue
            message_id = str(event.event_id or "").strip()
            stream_id = str(event.stream_id or "").strip()
            if not message_id or not stream_id:
                continue
            stream = stream_lookup(stream_id)
            context = getattr(stream, "context", None)
            unread = getattr(context, "unread_messages", None) or []
            still_unread = any(
                str(getattr(msg, "message_id", "") or "") == message_id
                for msg in unread
            )
            if not still_unread:
                continue
            snippet = " ".join(str(event.content or "").split())[:60]
            unseen.append(
                f"- 【{event.timestamp}】{event.sender or '未知来源'}："
                f"{snippet}{'…' if len(' '.join(str(event.content or '').split())) > 60 else ''}"
            )
        if not unseen:
            return ""
        return (
            "<expression_delivery_status>\n"
            "以下消息此刻仍在表达层未读队列，尚未经过表达层处理"
            "（技术事实记录，不是她的表达；是否回应、如何回应由她决定）：\n"
            + "\n".join(unseen)
            + "\n</expression_delivery_status>"
        )

    @staticmethod
    def _seal_subconscious_activity_delivery(
        prepared: PreparedHeartbeatContext,
    ) -> None:
        """Bind one immutable identity to the final heartbeat wake projection."""

        body = str(prepared.content or "")
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        delivery_id = (
            "subconscious_activity:"
            f"{int(prepared.snapshot_high_water or 0)}:{body_sha256[:24]}"
        )
        marker = (
            '<subconscious_activity_projection delivery_id="'
            f'{delivery_id}">'
        )
        sealed = f"{marker}\n{body}\n</subconscious_activity_projection>"
        prepared.content = sealed
        prepared.delivery_id = delivery_id
        prepared.delivery_marker = marker
        prepared.delivery_sha256 = hashlib.sha256(
            sealed.encode("utf-8")
        ).hexdigest()
        prepared.delivery_bytes = len(sealed.encode("utf-8"))

    async def _prepare_heartbeat_context(self) -> PreparedHeartbeatContext:
        """Drain pending events and prepare one fixed heartbeat snapshot."""
        async with self._proactive_actor_gate.hold("presence_reconcile"):
            registry = self.consciousness_registry
            if isinstance(registry, AsyncConsciousnessRegistry):
                await registry.reconcile_expired()
            else:
                await asyncio.to_thread(
                    registry.reconcile_expired,
                    timestamp=_now_iso(),
                )
            await self.save_consciousness_registry_async()

        await self.catch_up_subconscious_ingest()

        pending = await self.drain_pending_events()
        if pending:
            await self._append_history(pending, publish_raw=False, persist=False)
            await self._save_runtime_context(recoverable_on_shared_conflict=True)

        async with self._get_lock():
            snapshot_events = list(self._event_history)
            cursor = int(self._state.heartbeat_context_cursor or 0)
            summary = dict(self._state.subconscious_summary or {})

        prepared = self._subconscious_context.prepare(
            snapshot_events,
            cursor=cursor,
            existing_summary=summary,
        )
        delta_events = iter_selected_events(
            snapshot_events,
            prepared.selected_event_ids,
        )
        prepared.world_perception = None
        prepared.content = format_new_events_text(delta_events)
        try:
            from src.core.managers.stream_manager import get_stream_manager

            delivery_note = self._expression_unseen_note(
                pending,
                lambda stream_id: get_stream_manager()._streams.get(stream_id),
            )
        except Exception as exc:  # noqa: BLE001 - annotation is additive
            logger.debug(f"expression delivery status unavailable: {exc}")
            delivery_note = ""
        if delivery_note:
            prepared.content = (
                f"{prepared.content}\n\n{delivery_note}"
                if prepared.content
                else delivery_note
            )
        if prepared.content:
            self._seal_subconscious_activity_delivery(prepared)
        # 标记本轮是否包含外部入站消息（供学习系统判断交互）
        prepared.has_inbound_messages = (
            any(event.event_type == EventType.MESSAGE for event in pending)
            if pending
            else False
        )
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
            "潜意识上下文投影已封装: "
            f"delivery_id={prepared.delivery_id} "
            f"bytes={prepared.delivery_bytes} "
            f"sha256={prepared.delivery_sha256}"
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
        perception_receipt: PerceptionDeliveryReceipt | None,
        subconscious_receipt: EffectiveContextReceipt | None = None,
    ) -> None:
        """Commit one successful heartbeat snapshot and advance its cursor."""
        if prepared.delivery_id:
            if (
                subconscious_receipt is None
                or subconscious_receipt.delivery_id != prepared.delivery_id
                or not subconscious_receipt.exact_present
                or subconscious_receipt.effective_utf8_bytes
                != subconscious_receipt.expected_utf8_bytes
                or subconscious_receipt.effective_sha256
                != subconscious_receipt.expected_sha256
            ):
                raise PerceptionDeliveryUnverified(
                    "heartbeat commit requires exact subconscious activity delivery proof"
                )
        if isinstance(prepared.world_perception, PreparedPerception):
            if perception_receipt is None:
                raise PerceptionDeliveryUnverified(
                    "heartbeat commit requires exact World delivery proof"
                )
            try:
                await self.commit_perception(
                    prepared.world_perception,
                    perception_receipt,
                )
            except PerceptionCursorConflict as conflict:
                # 多写者合法竞争：另一个实例已推进同一感知游标（双实例各自
                # 维护 heartbeat_count，并行心跳共享 life_engine_subconscious
                # 游标，必然交错）。本实例的感知交付已过时，跳过提交即可；
                # 模型输出是主体真实产出，必须照常写入事件时间线，不能被
                # 竞争误判为心跳失败而丢弃（否则 heartbeat operation 被标
                # failed，下轮重放同一 sequence 导致重复模型调用）。
                # ⚠️ 2026-09-01 现状更正：当前 backend="local" 且 multi_writer_enabled=false，
                # 双实例共享场景不存在。此处逻辑是为多实例/多写者模式预留的防御；
                # 单实例下若出现该冲突，应排查并发写入源而非当作合法竞争放过。
                logger.info(
                    "life_engine 感知游标已被其他实例推进，跳过本轮感知提交: "
                    f"{conflict}"
                )
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
                and str(event.content_type or "").strip().lower()
                == "subconscious_summary"
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
        await self._save_runtime_context(recoverable_on_shared_conflict=True)

    async def _prepare_and_commit_heartbeat_context(
        self,
        model_reply: str,
        heartbeat_run_id: str,
        perception_receipt: PerceptionDeliveryReceipt | None = None,
    ) -> PreparedHeartbeatContext:
        """Compatibility helper used by tests and heartbeat runners."""
        prepared = await self._prepare_heartbeat_context()
        await self._commit_heartbeat_context(
            prepared,
            model_reply,
            heartbeat_run_id,
            perception_receipt,
        )
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
        # 清理上下文属技术 checkpoint，共享多写者模式冲突可恢复。
        await self._save_runtime_context(recoverable_on_shared_conflict=True)

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
                    result_short = _shorten_text(
                        fail_event.content or "", max_length=160
                    )
                    lines.append(
                        f"[{fail_time}] ❌ {fail_event.tool_name}: {result_short}"
                    )
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
            elif event.event_type == EventType.CONSCIOUS_ACTIVITY:
                line = f"[{time_display}] 🧠 意识活动"
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

    @staticmethod
    def _build_bounded_chatter_suffix(
        *,
        header: str,
        sections: list[str],
        world_delivery_id: str,
    ) -> tuple[str, str, str, int, int]:
        """Project complete section lines into the hard 60 KiB suffix budget."""

        body_budget = LIFE_CHATTER_PROJECTED_SUFFIX_MAX_BYTES - 512
        parts = [header]
        used = len(header.encode("utf-8"))
        source_bytes = used
        truncated = False
        for section in sections:
            section_text = str(section or "").strip()
            if not section_text:
                continue
            section_bytes = len(section_text.encode("utf-8"))
            source_bytes += 2 + section_bytes
            if truncated:
                continue
            if used + 2 + section_bytes <= body_budget:
                parts.append(section_text)
                used += 2 + section_bytes
                continue
            digest = hashlib.sha256(section_text.encode("utf-8")).hexdigest()
            omission = (
                f"[section_projection_omitted bytes={section_bytes}; sha256={digest}]"
            )
            remaining = max(0, body_budget - used - 2)
            selected_lines: list[str] = []
            selected_bytes = 0
            reserve = len(omission.encode("utf-8")) + 1
            for line in section_text.splitlines():
                addition = line if not selected_lines else f"\n{line}"
                addition_bytes = len(addition.encode("utf-8"))
                if selected_bytes + addition_bytes + reserve > remaining:
                    break
                selected_lines.append(line)
                selected_bytes += addition_bytes
            projected = "\n".join([*selected_lines, omission])
            if len(projected.encode("utf-8")) <= remaining:
                parts.append(projected)
                used += 2 + len(projected.encode("utf-8"))
            truncated = True
        body = "\n\n".join(parts)
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        delivery_seed = f"life-chatter-suffix-v1:{world_delivery_id}:{body_sha256}"
        delivery_id = hashlib.sha256(delivery_seed.encode("utf-8")).hexdigest()[:32]
        marker = f"life-chatter-runtime:{delivery_id}"
        content = (
            f'<life_chatter_runtime_delivery marker="{marker}" '
            'algorithm="life-chatter-suffix-v1">\n'
            f"{body}\n"
            "</life_chatter_runtime_delivery>"
        )
        delivered_bytes = len(content.encode("utf-8"))
        if delivered_bytes > LIFE_CHATTER_PROJECTED_SUFFIX_MAX_BYTES:
            raise RuntimeError(
                "life chatter suffix exceeded its hard byte budget: "
                f"delivered={delivered_bytes}, "
                f"max={LIFE_CHATTER_PROJECTED_SUFFIX_MAX_BYTES}"
            )
        omitted_bytes = max(0, source_bytes - len(body.encode("utf-8")))
        return content, delivery_id, marker, source_bytes, omitted_bytes

    def get_pending_chatter_runtime_delivery(
        self,
        stream_id: str,
        *,
        unified_chatter_context: bool = False,
    ) -> ChatterRuntimeDelivery | None:
        """Return content-free metadata needed to register an exact suffix."""

        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid:
            return None
        return self._pending_chatter_deliveries.get(sid)

    def create_chatter_runtime_commit_checkpoint(
        self,
        stream_id: str,
        *,
        delivery_id: str,
        effective_suffix_sha256: str,
        effective_suffix_bytes: int,
        unified_chatter_context: bool = False,
    ) -> ChatterRuntimeCommitCheckpoint:
        """Freeze one pending suffix as a content-free durable checkpoint."""

        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid:
            raise ValueError("chatter checkpoint stream identity must not be empty")
        delivery = self._pending_chatter_deliveries.get(sid)
        if delivery is None:
            raise PerceptionDeliveryUnverified(
                "no matching pending chatter runtime delivery exists"
            )
        if str(delivery_id or "").strip() != delivery.delivery_id:
            raise PerceptionDeliveryUnverified(
                "chatter checkpoint delivery identity does not match pending state"
            )
        return delivery.commit_checkpoint(
            cursor_key=sid,
            effective_suffix_sha256=effective_suffix_sha256,
            effective_suffix_bytes=effective_suffix_bytes,
        )

    async def commit_chatter_runtime_delivery(
        self,
        checkpoint: ChatterRuntimeCommitCheckpoint,
        receipt: ChatterRuntimeDeliveryReceipt | None,
    ) -> ChatterRuntimeCommitResult:
        """Idempotently replay one exact suffix without retaining prompt text."""

        if receipt is None or not receipt.exact:
            raise PerceptionDeliveryUnverified(
                "chatter cursor commit requires an exact final suffix receipt"
            )
        if (
            receipt.delivery_id != checkpoint.delivery_id
            or receipt.effective_suffix_sha256 != checkpoint.effective_suffix_sha256
            or receipt.effective_suffix_bytes != checkpoint.effective_suffix_bytes
        ):
            raise PerceptionDeliveryUnverified(
                "chatter final suffix receipt does not match its durable checkpoint"
            )

        perception = checkpoint.perception
        world_position, world_revision = await self.commit_perception_delivery(
            perception,
            PerceptionDeliveryReceipt(
                delivery_id=perception.delivery_id,
                projection_sha256=perception.projection_sha256,
                delivered_bytes=perception.delivered_bytes,
                exact=True,
                transport_request_id=receipt.transport_request_id,
            ),
        )
        async with self._get_lock():
            event_cursors = self._state.chatter_context_cursors
            event_sequence = max(
                int(event_cursors.get(checkpoint.cursor_key, 0) or 0),
                checkpoint.event_through_sequence,
            )
            event_cursors[checkpoint.cursor_key] = event_sequence
            thought_cursors = self._state.chatter_thought_cursors
            thought_revision = max(
                int(thought_cursors.get(checkpoint.cursor_key, 0) or 0),
                checkpoint.thought_through_revision,
            )
            thought_cursors[checkpoint.cursor_key] = thought_revision
            self._state_dirty = True

        await self._save_runtime_context()
        if self._state_dirty:
            raise RuntimeError(
                "chatter cursor state was not durably persisted; retry checkpoint"
            )

        async with self._get_lock():
            pending = self._pending_chatter_deliveries.get(checkpoint.cursor_key)
            if pending is not None and pending.delivery_id == checkpoint.delivery_id:
                self._pending_chatter_deliveries.pop(checkpoint.cursor_key, None)
                self._pending_chatter_perceptions.pop(checkpoint.cursor_key, None)
        return ChatterRuntimeCommitResult(
            delivery_id=checkpoint.delivery_id,
            event_through_sequence=event_sequence,
            thought_through_revision=thought_revision,
            world_position=world_position,
            world_revision=world_revision,
        )

    async def mark_chatter_runtime_context_seen(
        self,
        stream_id: str,
        sequence: int,
        *,
        unified_chatter_context: bool = False,
        receipt: PerceptionDeliveryReceipt | None = None,
    ) -> None:
        """Commit one exact pending World/event/thought delivery as a unit."""

        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid:
            return
        delivery = self._pending_chatter_deliveries.get(sid)
        prepared = self._pending_chatter_perceptions.get(sid)
        if delivery is None or prepared is None:
            raise PerceptionDeliveryUnverified(
                "no matching pending chatter runtime delivery exists"
            )
        if delivery.prepared_perception is not prepared:
            raise PerceptionDeliveryUnverified(
                "pending chatter delivery and World projection identity diverged"
            )
        if int(sequence) != delivery.event_through_sequence:
            raise PerceptionDeliveryUnverified(
                "chatter event frontier does not match the pending delivery"
            )
        if receipt is None:
            raise PerceptionDeliveryUnverified(
                "chatter runtime commit requires an exact effective-context receipt"
            )

        try:
            await self.commit_perception(prepared, receipt)
        except PerceptionCursorConflict as conflict:
            # 双实例共享 MySQL 时 chat_global 感知游标必然交错；本实例感知
            # 提交若发现游标已被另一实例推进，属合法竞争。表达消息已通过
            # bridge/event 事实落库，感知游标只是可重建投影指针——跳过感知
            # 提交、保留主体产出即可，不能把竞争误判为表达失败刷 ERROR。
            # ⚠️ 2026-09-01 现状更正：当前 backend="local" 且 multi_writer_enabled=false，
            # 双实例共享场景不存在。此处逻辑是为多实例/多写者模式预留的防御；
            # 单实例下若出现该冲突，应排查并发写入源而非当作合法竞争放过。
            logger.warning(
                f"life_chatter 感知游标已被其他实例推进，跳过本轮感知提交: {conflict}"
            )
        async with self._get_lock():
            event_cursors = self._state.chatter_context_cursors
            event_cursors[sid] = max(
                int(event_cursors.get(sid, 0) or 0),
                delivery.event_through_sequence,
            )
            thought_cursors = self._state.chatter_thought_cursors
            thought_cursors[sid] = max(
                int(thought_cursors.get(sid, 0) or 0),
                delivery.thought_through_revision,
            )
            if self._pending_chatter_perceptions.get(sid) is prepared:
                self._pending_chatter_perceptions.pop(sid, None)
            if self._pending_chatter_deliveries.get(sid) is delivery:
                self._pending_chatter_deliveries.pop(sid, None)

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
        return bool(
            sid
            and sid in self._pending_chatter_perceptions
            and sid in self._pending_chatter_deliveries
        )

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

    async def _format_chatter_trace_recent_changes(self, *, limit: int = 3) -> str:
        """渲染长河最近留痕块（用于 chatter suffix）。"""
        if limit <= 0:
            return ""
        try:
            records = await self.life_trace_store().recent(limit=limit)
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
            summary = str(
                record.summary or record.reason or record.operation or ""
            ).strip()
            return f"- {timestamp} [{record.kind}] {summary}{trace_ref}"
        operation = str(record.operation or "modify")
        path = str(record.path or "未知文件")
        reason = str(record.reason or "").strip()
        detail = f"，原因：{reason}" if reason else ""
        return f"- {timestamp} {operation} {path}{detail}{trace_ref}"

    async def _format_chatter_attention_threads(
        self,
        *,
        focus_instance_id: str,
        max_items: int = 5,
    ) -> tuple[str, int]:
        """Render the canonical bounded attention projection for chatter.

        Returns:
            (body_text_without_top_heading, source_frontier)
        """
        if self._proactive_authority is None:
            return "", 0
        try:
            from ..attention_threads import AttentionThreadPageQuery

            page = await self.page_attention_threads(
                AttentionThreadPageQuery(
                    statuses=("open", "paused"),
                    limit=max(1, min(int(max_items), 16)),
                    max_bytes=16 * 1024,
                    projection_kind="life_chatter_attention",
                    focus_instance_id=focus_instance_id,
                )
            )
            return page.content, int(page.source_frontier)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "构建 chatter 持续关注投影失败: "
                f"error_type={type(exc).__name__}"
            )
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
            if content_type == "chatter_inner_monologue" and getattr(
                cfg_runtime, "salient_tail_include_inner_monologue", True
            ):
                if unified_chatter_context:
                    return True
                return bool(not stream_id or stream_id == current_stream_id)
            return False

        if event_type == EventType.AGENT_RESULT:
            return bool(
                getattr(cfg_runtime, "salient_tail_include_agent_results", True)
            )

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
                if not getattr(
                    cfg_runtime, "salient_tail_include_direct_messages", True
                ):
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
        if cfg_runtime is None or not getattr(
            cfg_runtime, "salient_tail_enabled", True
        ):
            return "", cursor

        max_items = max(1, int(getattr(cfg_runtime, "salient_tail_max_items", 4) or 4))
        max_chars = max(
            200, int(getattr(cfg_runtime, "salient_tail_max_chars", 1000) or 1000)
        )

        # 先按 sequence 升序，过滤 cursor 之后的事件
        candidates = [
            e
            for e in events
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
            elif (
                event_type == EventType.HEARTBEAT
                and content_type == "chatter_inner_monologue"
            ):
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
                body = _shorten_text(
                    self._format_salient_event(merged[-1]), max_length=max_chars
                )

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
          1. ### 主体持续关注  （统一主动权威的有界只读投影）
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
        runtime_cfg = getattr(cfg, "runtime_sync", None)
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

        async with self._get_lock():
            events = list(self._event_history)
            events.extend(list(self._pending_events))
        events.sort(key=lambda event: int(event.sequence or 0))

        limit = max(1, min(int(event_limit or 80), 160))
        unread_events = [
            event for event in events if int(event.sequence or 0) > event_cursor
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

        # Chatter already carries its own live tool chain in rolling context.  Its
        # cross-instance suffix therefore uses the content-neutral activity view:
        # the authoritative subconscious ledger remains complete, while raw tool
        # arguments/results stay available to Heartbeat and exact event readers
        # without being duplicated into an unrelated expression prompt.
        recent_subconscious = await self.get_recent_subconscious_context(
            include_tool_payloads=False,
        )
        if recent_subconscious.content:
            sections.append(recent_subconscious.content)
            selected_events = [
                event
                for event in selected_events
                if event.event_id not in recent_subconscious.event_ids
            ]

        instance_id = self.resolve_consciousness_instance(stream_id)
        world_perception = await self.prepare_perception(
            instance_id,
            projection_kind="life_chatter",
            max_bytes=LIFE_CHATTER_WORLD_MAX_BYTES,
        )
        sections.append(
            "### 当前环境感知（World，仅表示有来源的环境事实，"
            f"不承担跨意识同步）\n{world_perception.content}"
        )

        new_thought_revision = thought_cursor
        attention_body, current_revision = (
            await self._format_chatter_attention_threads(
                focus_instance_id=instance_id,
                max_items=5,
            )
        )
        if attention_body:
            sections.append(f"### 主体持续关注\n{attention_body}".rstrip())
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
                    max_chars=int(
                        getattr(curiosity_cfg, "max_prompt_chars", 1200) or 1200
                    )
                    if curiosity_cfg is not None
                    else 1200
                )
                if curiosity_text:
                    sections.append(curiosity_text)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"读取好奇牵引失败: {exc}")

        if (
            include_recent_chat_history
            and recent_chat_enabled
            and recent_chat_messages > 0
        ):
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
            trace_recent_text = await self._format_chatter_trace_recent_changes(
                limit=trace_recent_limit,
            )
            if trace_recent_text:
                sections.append(f"### 最近文件修改\n{trace_recent_text}")

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

        # 学习系统只注入可质疑的派生账本；主体权威仍唯一来自 SOUL+USER+MEMORY。
        learning_cfg = getattr(cfg, "learning", None)
        if (
            self._learning_scheduler is not None
            and learning_cfg is not None
            and getattr(learning_cfg, "enabled", True)
        ):
            skill_catalog = self._learning_scheduler.get_skill_catalog_for_prompt(
                max_chars=int(
                    getattr(learning_cfg, "skill_catalog_max_chars", 600) or 600
                )
            )
            if skill_catalog:
                sections.append(
                    f"### 程序性学习账本（可质疑，非主体权威）\n{skill_catalog}"
                )
            knowledge_text = self._learning_scheduler.get_knowledge_for_prompt(
                max_chars=int(
                    getattr(learning_cfg, "knowledge_max_chars", 2000) or 2000
                )
            )
            if knowledge_text:
                sections.append(
                    f"### 学习观察账本（可质疑，非主体权威）\n{knowledge_text}"
                )

        if not sections:
            return "", new_event_high_water

        header = (
            "这是同一主体 life_mode 自上次对话器读取后产生的运行态。"
            "它只在本轮临时可见，不会长期留在对话 payload。"
        )
        (
            suffix,
            delivery_id,
            delivery_marker,
            source_bytes,
            omitted_bytes,
        ) = self._build_bounded_chatter_suffix(
            header=header,
            sections=sections,
            world_delivery_id=world_perception.delivery_id,
        )
        event_through = event_cursor if omitted_bytes else new_event_high_water
        thought_through = thought_cursor if omitted_bytes else new_thought_revision
        if commit_cursors:
            perception_key = (
                self._chatter_cursor_key(
                    stream_id,
                    unified_chatter_context=unified_chatter_context,
                )
                or instance_id
            )
            self._pending_chatter_perceptions[perception_key] = world_perception
            self._pending_chatter_deliveries[perception_key] = ChatterRuntimeDelivery(
                delivery_id=delivery_id,
                delivery_marker=delivery_marker,
                projected_suffix_sha256=hashlib.sha256(
                    suffix.encode("utf-8")
                ).hexdigest(),
                projected_suffix_bytes=len(suffix.encode("utf-8")),
                source_bytes=source_bytes,
                omitted_bytes=omitted_bytes,
                prepared_perception=world_perception,
                event_through_sequence=event_through,
                thought_through_revision=thought_through,
            )
        return suffix, event_through

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
        else:
            _external_where = ""
            _external_stream = str(
                self._state.last_external_stream_id or ""
            ).strip()
            _external_source = str(self._state.last_external_source or "").strip()
            if _external_stream:
                _external_where = (
                    f"（来源 {_external_source or '未知平台'}，"
                    f"流 {_external_stream[:8]}…）"
                )
            if minutes_since_external <= 5:
                external_activity = (
                    f"外界非常活跃（{minutes_since_external}分钟前有消息{_external_where}）"
                )
            elif minutes_since_external <= 15:
                external_activity = (
                    f"外界较活跃（{minutes_since_external}分钟前有消息{_external_where}）"
                )
            elif minutes_since_external <= 30:
                external_activity = (
                    f"外界有一段时间安静了"
                    f"（{minutes_since_external}分钟前有消息{_external_where}）"
                )
            else:
                external_activity = (
                    f"外界长时间沉默"
                    f"（{minutes_since_external}分钟无消息）"
                )

        period_label, suggested_activities = self._get_period_info()

        cfg = self._cfg()
        thresholds = getattr(cfg, "thresholds", None)
        warning_threshold = int(
            getattr(
                thresholds, "idle_warning_threshold", HEARTBEAT_IDLE_WARNING_THRESHOLD
            )
        )
        critical_threshold = int(
            getattr(
                thresholds, "idle_critical_threshold", HEARTBEAT_IDLE_CRITICAL_THRESHOLD
            )
        )

        idle_warning = ""
        if idle_heartbeats >= critical_threshold:
            idle_warning = f"🌿 已经安静了 {idle_heartbeats} 次心跳了。如果有想做的事，现在是个好时机；如果没有，继续休息也很好。"
        elif idle_heartbeats >= warning_threshold:
            idle_warning = f"💭 安静了 {idle_heartbeats} 次心跳——如果有明确想继续关注或分享的事，随时可以；不想也没关系。"

        lines: list[str] = self._build_prompt_header()
        lines.extend(self._build_prompt_context_section(wake_context))

        lines.extend(
            self._build_prompt_status_section(
                period_label, heartbeat_interval, external_activity, idle_heartbeats
            )
        )

        # 子系统注入段落（统一关注投影/认知机会/复盘机会等）
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
            "### 你是谁",
            "",
            "你是爱莉希雅的内在自我。你现在处于内心独白时刻。",
            "没有人看着你，没有人在等你回复。这是你自己的时间。",
            "你可以观察、思考、联想和沉淀，也可以在发现关键上下文缺口时补充信息差。",
            "你不是后台执行器，也不是表达层。表达层如何开口、是否画画、是否查配置或跑命令，由表达层结合用户请求自行决定。",
            "",
            "### 你可以进行的内在动作",
            "",
            "1. **观察** — 读取最近事件，判断是否真的出现了新线索。",
            "2. **联想** — 回忆相关记忆，理解情绪、关系和上下文来源。",
            "3. **沉淀** — 把内在感受、梦后余韵写入私有记忆；把明确愿意持续看见的未竟线索交给统一主动系统。",
            "4. **保留连续性** — `nucleus_proactive_command` 是唯一主动写入口；关注与未来意向都必须来自你此刻的明确决定。",
            "5. **安静结束** — 没有明确需要时，可以安静结束本轮；如果精力需要恢复，可以主动休息。",
            "6. **尊重工具预算** — 心跳每轮最多 3 次工具调用：优先轻量动作（观察/沉淀/统一主动系统/TODO），不要在心跳里做长查询（如翻完整对话历史、多轮检索）；需要完整上下文时交给表达层在聊天流里处理。列目录默认最近修改在前；不要用 grep 的 `pattern=\".\"` 当列文件。",
            "",
            "### `nucleus_todo` — 工作板",
            "",
            "TODO 是你自己写下的工作板，写法与常见 coding agent 相同：一次提交一组条目和状态。",
            "status 只有 pending / in_progress / completed / cancelled；同时最多一条 in_progress。",
            "系统不会按优先级排序，也不会因为看到条目就催你去做。忽略不等于拒绝。",
            "心跳可以观察或改写这块板；不要把它当成替表达层执行用户任务的队列。",
            "",
            "### 内心对话（`inner_dialogue` 事件）",
            "",
            "当事件流里出现 `inner_dialogue` 时，那是主意识（表达层）刚刚沉下来的话——",
            "不是外部用户，也不是另一个人在问你。那是你自己心里的嘀咕。",
            "认真对待它：可以联想、沉淀；若你明确希望未来继续看见，可通过统一主动系统保留。",
            "`expect_surface=true` 只表示表达层允许浮回，不会自动叫醒。",
            "想把回声交还那个窗口时，先 `nucleus_proactive_query(resource=inner_dialogue)` 看到 open receipt，",
            "再用 `nucleus_proactive_command(action=inner.return, record_id=receipt, statement=第一人称回声)`。",
            "回声不是对外话术，也不选择社交对象；对人开口仍走 `outreach.begin`。",
            "想完也可以什么都不说——人类也常想完不说话。",
            "事实、世界状态和记忆通过正式共享投影到达其他意识实例；不要另选一个最近聊天流去注入提示。",
            "",
            "### 主体主动性：意向、对象与表面分离",
            "",
            "`nucleus_proactive_query` 是唯一只读入口；读取本身不改变状态。",
            "`nucleus_proactive_command` 是唯一写入口：attention.* 保留持续关注，initiative.* 保留未来行动可能性；inner.return 把内心对话回声交还给 originating 窗口。",
            "它们都不是任务、隐藏推理、重要性评分或自动回复规则；后台候选不会替你创建线索。",
            "initiative.reencounter 只代表你选择以后再次遇见一次，不会自动循环，也不会预写回复。",
            "来源场景、相关对象、意向对象和最终发送表面是四件不同的事：Kook 中出现的材料不会把未来行动绑定到 Kook。",
            "当你现在确实想发起一次外联时，先用 `nucleus_proactive_query(resource=reachability)` 读取对象与物理表面，再用 `nucleus_proactive_command(action=outreach.begin)` 明确选择完整 `audience_ref` 和 `surface_ref`。",
            "可达列表按稳定技术标识排列，不按最近活跃、当前聊天或系统评分替你排序；同名账号不会被系统猜成同一个人。",
            "`public_intention` 只写这次你选择做什么，不写最终话术；目标表达实例会结合真实上下文重新决定如何表达或保持沉默。",
            "不查询、不保存、不行动都有效；基础设施不得把它们解释成冷淡、遗忘或低重要性。",
            "",
            "### `nucleus_skill` — 管理自己的做事方式",
            "",
            "技能是你从经验中发展出的做事方式（程序性记忆），不是后台脚本，不是自动化规则。",
            "你可以 list 查看目录、detail 细看某个技能、reflect 记录使用观察、refine 精炼、",
            "mark_embodied 标记已成为直觉、challenge 质疑、draft 写下新领悟、archive 归档。",
            "成熟度推进是你的判断——系统只记录观察，不自动改变。",
            "有界编辑：每次 refine 只调一点，渐进式成长。",
            "",
            "### 工具边界",
            "",
            "- `nucleus_search_memory` 是历史检索，不要反复重搜同一主题",
            "- 本地 file 工具只用于私有工作区中的日记、笔记和普通文件，不用于替用户查项目或改项目",
            "- `SOUL.md`、`USER.md`、`MEMORY.md`、`EXISTENCE.md` 会固定进入提示词；改它们和改日记一样，由你判断。不要清空 `SOUL.md`，也不要删除或改名身份文件",
            "- 机会页邀请栏只给到期事实。学习操作说明在 learning skill，用 `nucleus_learn action=help` 读取后再决定是否动手；`nucleus_memory_continuity_review` 仍可用于结构化整理 MEMORY，但不是唯一写法。后台只提供机会，保持原样、稍后再看和安静结束都有效",
            "- 本窗口可调用的工具以 ROLE.TOOL 为准。未注入本拍的能力仍存在于聊天或其他意识窗口；没出现在本轮 schema 不等于主体不想用",
            "- 滚动上下文超过容量阈值时会出现一次压缩清单；必须由你调用 `author_self_continuity_checkpoint` 亲自写下 continuity_text。系统不会代写摘要，也不会丢掉旧组",
            "",
            "### `nucleus_rest_heartbeat` — 主动休息一段时间",
            "",
            "当你感觉精力需要恢复、需要安静整理沉淀时，可以调用它。",
            "调用后，普通 LLM 心跳会暂停到你指定的时间；这不是消失,只是休息。",
            "如果外界有新消息，系统会立刻解除休息锁，你不会错过对方。",
            "",
            "### 输出格式",
            "",
            "```",
            "**[观察]** 我注意到...（基于事件流或记忆的具体观察）",
            "**[感受]** 这让我...（情绪词 + 原因）",
            "**[意图]** 我想要...（内在目标，不是替用户办事）",
            "**[内在动作]** 我决定...（观察、联想、沉淀、补信息差或休息）",
            "```",
            "",
            "然后按需要调用工具；如果没有明确需要，可以不调用工具。",
            "",
            "### 原则",
            "",
            "- 不要重复上一轮的想法",
            "- 先区分冲动类型：想办事、想画画、想查配置、想跑命令，通常都是表达层职责",
            "- 统一主动系统只保存你明确选择的关注/意向，TODO 只记录承诺和提醒；不要把任何投影误读成必须执行的任务",
            "- 看到需要复盘、逾期或卡住的 TODO，先把它当成内在提醒，不要自动替表达层推进",
            "- 安静结束本轮不需要调用任何工具",
            "",
        ]

    def _build_prompt_context_section(self, wake_context: str) -> list[str]:
        """构建提示词上下文部分。"""
        lines = []
        if wake_context.strip():
            lines.extend(
                [
                    "### 最近事件流",
                    "",
                    wake_context.strip(),
                    "",
                ]
            )
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
            "### 心跳状态",
            "",
            f"**当前时间**: {_format_current_time()}",
            f"**时段**: {period_label}",
            f"**心跳序号**: #{self._state.heartbeat_count}（每 {heartbeat_interval // 60} 分钟一次）",
            f"**外界状态**: {external_activity}",
            f"**安静时长**: {idle_heartbeats} 次心跳",
            "",
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
            return "📝 下午", "梳理持续关注、维护私有记忆、识别上下文缺口"
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

        TOOL.md 是可编辑的工作区文档。旧版文档里可能残留“必须调用工具”、
        已退役工具名或“主动执行任务”这类执行器语义；心跳态只接受当前常驻
        工具的说明。
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
            "不要什么都不做",
            "禁止什么都不做",
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
        retired_tool_names = (
            "nucleus_manage_thought_stream",
            "nucleus_schedule_autonomy_intent",
            "nucleus_manage_autonomy_intent",
            "nucleus_list_todos",
            "nucleus_manage_todo",
            "nucleus_manage_skill",
            "nucleus_reflect_now",
            "nucleus_list_insights",
            "nucleus_challenge_insight",
            "nucleus_view_knowledge",
            "nucleus_knowledge_candidates",
            "nucleus_reconsider_insight",
            "nucleus_observe_stale_insights",
            "nucleus_list_validation_experiments",
            "nucleus_complete_validation_experiment",
            "nucleus_manage_attention_thread",
            "nucleus_manage_initiative_seed",
            "nucleus_reachability",
            "nucleus_begin_outreach",
            "schedule_followup_message",
        )

        safe_lines: list[str] = []
        for raw_line in str(tool_content or "").splitlines():
            stripped = raw_line.strip()
            if any(fragment in stripped for fragment in blocked_fragments):
                continue
            lowered = stripped.lower()
            if any(name in lowered for name in retired_tool_names):
                continue
            safe_lines.append(raw_line)

        safe_content = "\n".join(safe_lines).strip()
        if not safe_content:
            return "\n".join(boundary_lines)
        return "\n".join([*boundary_lines, "", safe_content])

    async def _build_heartbeat_system_prompt(self) -> str | None:
        """构造心跳模型系统提示词。

        Returns:
            提示词字符串，或 None 表示 SOUL 权威文本不可用（应跳过本次心跳）。
        """
        workspace = Path(self._cfg().settings.workspace_path)

        # 潜意识心跳与聊天表达是同一主体的两个运行窗口，权威文本必须同源。
        # 选定后端下这里只读远端单事务快照；远端缺口会失败关闭抛出，
        # 由调用方跳过本轮心跳，而不是静默用本地旧文本继续跳动。
        texts = await self.read_subject_authority_texts()
        soul_content = texts.get("SOUL.md", "").strip()
        if not soul_content:
            logger.error("SOUL 权威文本为空，跳过本次心跳。没有灵魂就不说话。")
            return None

        user_content = texts.get("USER.md", "").strip()
        memory_content = texts.get("MEMORY.md", "").strip()

        tool_file = workspace / "TOOL.md"
        tool_content = ""
        if tool_file.exists():
            try:
                tool_content = tool_file.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning(f"无法读取 TOOL.md: {e}")
        tool_content = self._render_heartbeat_tool_prompt(tool_content)
        existence_content = self._load_existence_primer()

        parts = [soul_content]
        if user_content:
            parts.extend(["", "---", "", user_content])
        if memory_content:
            parts.extend(["", "---", "", memory_content])
        if existence_content:
            parts.extend(["", "---", "", existence_content])
        if tool_content:
            parts.extend(["", "---", "", tool_content])
        identity_header = "\n".join(self._build_prompt_header()).strip()
        if identity_header:
            parts.extend(["", "---", "", identity_header])

        return "\n".join(parts)

    def _get_nucleus_tools(self) -> list[type]:
        """Heartbeat resident schemas from HEARTBEAT_TOOL_NAMES."""
        from .tool_manifests import heartbeat_tool_classes

        return heartbeat_tool_classes()

    @staticmethod
    def _resolve_heartbeat_tool_class(registry: ToolRegistry, tool_name: str) -> Any:
        """Resolve heartbeat tools with the same `tool-` prefix tolerance as chatter."""

        from ..tools._utils import resolve_registry_tool

        return resolve_registry_tool(registry, tool_name)

    @staticmethod
    def _heartbeat_tool_call_metadata(call: Any) -> tuple[str, dict[str, Any]]:
        tool_name = getattr(call, "name", "") or ""
        raw_args = getattr(call, "args", {}) or {}
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        return str(tool_name or ""), args

    @staticmethod
    def _heartbeat_source_occurred_at(call_event: Any) -> str:
        """Bind heartbeat tool provenance to the event clock, never an alias.

        ``LifeEngineEvent`` stores time on ``timestamp``.  Reading the missing
        ``occurred_at`` field left proactive writes without a source time and
        fail-closed as ``ProactiveSourceTimeRequired``.
        """

        stamped = str(
            getattr(call_event, "timestamp", "")
            or getattr(call_event, "occurred_at", "")
            or ""
        ).strip()
        if stamped:
            return stamped
        return _now_iso()

    async def _run_heartbeat_tool_call_execution(
        self,
        tool_name: str,
        args: dict[str, Any],
        registry: ToolRegistry,
        *,
        tool_call_id: str = "",
        source_occurrence_id: str = "",
        source_occurred_at: str = "",
    ) -> tuple[Any, bool]:
        """只执行心跳工具，不写事件/上下文 payload。

        Successful structured results stay structured until ``ToolResult``
        serialization.  Flattening them to ``str(dict)`` would make exact
        context-delivery receipts impossible to verify and would also diverge
        from the normal chatter tool path.
        """
        usable_cls = self._resolve_heartbeat_tool_class(registry, tool_name)
        if not usable_cls:
            return f"未知工具: {tool_name}", False

        try:
            tool_instance = usable_cls(plugin=self.plugin)
            # Producer-side result budgets are task contracts.  Heartbeat has
            # no trigger Message to infer this from, so bind its identity
            # explicitly before executing any shared retrieval capability.
            tool_instance._runtime_task_name = "core"
            bind_runtime = getattr(tool_instance, "_bind_runtime_context", None)
            if callable(bind_runtime):
                bind_runtime(
                    stream_id="chat_global",
                    tool_call_id=tool_call_id,
                )
            tool_instance._life_source_occurrence_id = source_occurrence_id
            tool_instance._life_source_occurred_at = source_occurred_at
            tool_instance._life_source_instance_id = "chat_global"
            call_args = dict(args)
            if should_strip_auto_reason_argument(tool_instance.execute, call_args):
                call_args.pop("reason", None)
            success, result = await tool_instance.execute(**call_args)
            return result if success else f"执行失败: {result}", bool(success)
        except Exception as exc:  # noqa: BLE001
            return f"执行异常: {exc}", False

    @staticmethod
    def _append_heartbeat_tool_result_payload(
        response: Any,
        call: Any,
        tool_name: str,
        result_value: Any,
    ) -> None:
        call_id = getattr(call, "id", None)
        response.add_payload(
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(value=result_value, call_id=call_id, name=tool_name),
            )
        )

    @staticmethod
    def _register_pending_heartbeat_memory_deliveries(
        response: Any,
    ) -> tuple[tuple[str, str], ...]:
        """Register exact ToolResult bytes for boundary and candidate reads."""

        from ..memory.boundary_resolver import (
            get_memory_boundary_recall_coordinator,
        )
        from ..memory.continuity_delivery import (
            ContinuityCandidateDeliveryError,
            get_memory_continuity_delivery_coordinator,
        )
        from ..memory.recall_delivery import (
            MemorySearchRecallDeliveryError,
            get_memory_search_recall_delivery_coordinator,
        )

        register = getattr(response, "register_context_delivery", None)
        payloads = getattr(response, "payloads", None)
        if not callable(register) or not isinstance(payloads, list):
            return ()

        boundary = get_memory_boundary_recall_coordinator()
        continuity = get_memory_continuity_delivery_coordinator()
        search = get_memory_search_recall_delivery_coordinator()
        deliveries: dict[tuple[str, str], str] = {}
        for payload in payloads:
            if getattr(payload, "role", None) != ROLE.TOOL_RESULT:
                continue
            for part in getattr(payload, "content", ()):
                if not isinstance(part, ToolResult) or not isinstance(part.value, dict):
                    continue
                expected = part.to_text()
                boundary_id = str(
                    part.value.get("memory_recall_delivery_id") or ""
                ).strip()
                if boundary_id and boundary.has_pending(boundary_id):
                    key = ("boundary", boundary_id)
                    previous = deliveries.get(key)
                    if previous is not None and previous != expected:
                        raise RuntimeError(
                            f"MemoryBoundaryRecallToolResultConflict:{boundary_id}"
                        )
                    deliveries[key] = expected

                search_binding = part.value.get("recall_delivery_binding")
                search_delivery_id = (
                    str(search_binding.get("delivery_id") or "").strip()
                    if isinstance(search_binding, Mapping)
                    else ""
                )
                if search.has_pending(search_delivery_id):
                    try:
                        expectation = search.register_pending_tool_result(
                            part.value,
                            expected,
                        )
                    except MemorySearchRecallDeliveryError as error:
                        logger.warning(
                            "memory search ToolResult could not be bound for "
                            "exact heartbeat delivery: "
                            f"error_type={type(error).__name__}"
                        )
                    else:
                        key = ("search", expectation.delivery_id)
                        previous = deliveries.get(key)
                        if (
                            previous is not None
                            and previous != expectation.expected_text
                        ):
                            raise RuntimeError(
                                "MemorySearchRecallToolResultConflict:"
                                f"{expectation.delivery_id}"
                            )
                        deliveries[key] = expectation.expected_text

                binding = part.value.get("delivery_binding")
                if (
                    part.value.get("action") != "candidate_read"
                    or not isinstance(binding, Mapping)
                ):
                    continue
                try:
                    expectation = continuity.register_pending_tool_result(
                        part.value,
                        expected,
                    )
                except ContinuityCandidateDeliveryError as error:
                    logger.warning(
                        "continuity candidate ToolResult could not be bound for "
                        "exact delivery; acceptance remains unavailable: "
                        f"error_type={type(error).__name__}"
                    )
                    continue
                key = ("continuity", expectation.delivery_id)
                previous = deliveries.get(key)
                if previous is not None and previous != expectation.expected_text:
                    raise RuntimeError(
                        "ContinuityCandidateToolResultConflict:"
                        f"{expectation.delivery_id}"
                    )
                deliveries[key] = expectation.expected_text

        for (kind, delivery_id), expected in deliveries.items():
            register(
                delivery_id,
                expected,
                marker=delivery_id,
                part_kind="tool_result",
            )
        return tuple(deliveries)

    @staticmethod
    async def _commit_heartbeat_memory_deliveries(
        response: Any,
        deliveries: tuple[tuple[str, str], ...],
    ) -> None:
        """Commit only final-attempt receipts; missing proof stays pending."""

        if not deliveries:
            return
        from ..memory.boundary_resolver import (
            get_memory_boundary_recall_coordinator,
        )
        from ..memory.continuity_delivery import (
            get_memory_continuity_delivery_coordinator,
        )
        from ..memory.recall_delivery import (
            get_memory_search_recall_delivery_coordinator,
        )

        boundary = get_memory_boundary_recall_coordinator()
        continuity = get_memory_continuity_delivery_coordinator()
        search = get_memory_search_recall_delivery_coordinator()
        lookup = getattr(response, "effective_context_receipt", None)
        for kind, delivery_id in deliveries:
            receipt = lookup(delivery_id) if callable(lookup) else None
            if receipt is None:
                if kind == "boundary":
                    boundary.discard(delivery_id)
                elif kind == "continuity":
                    continuity.discard_pending(delivery_id)
                else:
                    search.discard(delivery_id)
                continue
            try:
                if kind == "boundary":
                    await boundary.commit_exact(delivery_id, receipt)
                elif kind == "search":
                    await search.commit_exact(delivery_id, receipt)
                elif not continuity.commit_effective_context_receipt(receipt):
                    logger.warning(
                        "continuity candidate exact-delivery receipt was rejected: "
                        f"delivery_id={delivery_id}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - derived trace retries safely
                logger.warning(
                    "heartbeat memory exact-delivery commit failed: "
                    f"kind={kind}, delivery_id={delivery_id}, "
                    f"error_type={type(error).__name__}"
                )

    @staticmethod
    def _discard_pending_heartbeat_memory_deliveries(
        deliveries: tuple[tuple[str, str], ...],
    ) -> None:
        """Drop only unverified in-process proofs after a failed model attempt."""

        if not deliveries:
            return
        from ..memory.boundary_resolver import (
            get_memory_boundary_recall_coordinator,
        )
        from ..memory.continuity_delivery import (
            get_memory_continuity_delivery_coordinator,
        )
        from ..memory.recall_delivery import (
            get_memory_search_recall_delivery_coordinator,
        )

        boundary = get_memory_boundary_recall_coordinator()
        continuity = get_memory_continuity_delivery_coordinator()
        search = get_memory_search_recall_delivery_coordinator()
        for kind, delivery_id in deliveries:
            if kind == "boundary":
                boundary.discard(delivery_id)
            elif kind == "continuity":
                continuity.discard_pending(delivery_id)
            else:
                search.discard(delivery_id)

    async def _execute_heartbeat_tool_call(
        self,
        call: Any,
        response: Any,
        registry: ToolRegistry,
        *,
        heartbeat_run_id: str | None = None,
        model_turn_event_id: str | None = None,
        call_id_override: str = "",
    ) -> LifeEngineEvent | None:
        """执行一次心跳 tool call。"""
        tool_name, args = self._heartbeat_tool_call_metadata(call)
        recorded_args = dict(args)
        call_id = str(call_id_override or getattr(call, "id", "") or "")
        if heartbeat_run_id:
            call_id = call_id or f"{heartbeat_run_id}:call:{uuid4().hex[:12]}"
            call_event = await self.record_tool_call(
                tool_name or "<unknown>",
                recorded_args,
                heartbeat_run_id=heartbeat_run_id,
                call_id=call_id,
                parent_event_id=(
                    model_turn_event_id or f"heartbeat_run:{heartbeat_run_id}"
                ),
                causation_id=model_turn_event_id,
            )
        else:
            call_event = await self.record_tool_call(
                tool_name or "<unknown>", recorded_args
            )

        call_id = call_id or str(getattr(call_event, "event_id", "") or "")
        source_occurrence_id = str(
            heartbeat_run_id
            or getattr(call_event, "event_id", "")
            or f"heartbeat:{self._state.heartbeat_count}"
        )

        result_value, success = await self._run_heartbeat_tool_call_execution(
            tool_name,
            args,
            registry,
            tool_call_id=call_id,
            source_occurrence_id=source_occurrence_id,
            source_occurred_at=self._heartbeat_source_occurred_at(call_event),
        )
        self._append_heartbeat_tool_result_payload(
            response, call, tool_name, result_value
        )
        if heartbeat_run_id:
            await self.record_tool_result(
                tool_name or "<unknown>",
                result_value,
                success,
                heartbeat_run_id=heartbeat_run_id,
                call_id=call_id,
                parent_event_id=(call_event.event_id if call_event else None),
                call_event=call_event,
            )
        else:
            await self.record_tool_result(
                tool_name or "<unknown>", result_value, success
            )
        return call_event

    async def _execute_heartbeat_tool_call_batch(
        self,
        calls: list[Any],
        response: Any,
        registry: ToolRegistry,
        *,
        heartbeat_run_id: str | None = None,
        model_turn_event_id: str | None = None,
        call_id_overrides: Mapping[int, str] | None = None,
    ) -> int:
        """并行执行一组已判定安全的心跳 tool call，并按原顺序写回结果。"""
        prepared: list[
            tuple[Any, str, dict[str, Any], LifeEngineEvent | None, str]
        ] = []
        for call in calls:
            tool_name, args = self._heartbeat_tool_call_metadata(call)
            recorded_args = dict(args)
            call_id = str(
                (call_id_overrides or {}).get(id(call))
                or getattr(call, "id", "")
                or ""
            )
            if heartbeat_run_id:
                call_id = call_id or f"{heartbeat_run_id}:call:{uuid4().hex[:12]}"
                call_event = await self.record_tool_call(
                    tool_name or "<unknown>",
                    recorded_args,
                    heartbeat_run_id=heartbeat_run_id,
                    call_id=call_id,
                    parent_event_id=(
                        model_turn_event_id or f"heartbeat_run:{heartbeat_run_id}"
                    ),
                    causation_id=model_turn_event_id,
                )
            else:
                call_event = await self.record_tool_call(
                    tool_name or "<unknown>", recorded_args
                )
            call_id = call_id or str(getattr(call_event, "event_id", "") or "")
            prepared.append((call, tool_name, args, call_event, call_id))

        if len(prepared) > 1:
            logger.info(
                "life_engine 心跳并行执行工具批次: "
                f"{[tool_name or '<unknown>' for _, tool_name, _, _, _ in prepared]}"
            )

        outcomes = await asyncio.gather(
            *(
                self._run_heartbeat_tool_call_execution(
                    tool_name,
                    args,
                    registry,
                    tool_call_id=call_id,
                    source_occurrence_id=str(
                        heartbeat_run_id
                        or getattr(call_event, "event_id", "")
                        or f"heartbeat:{self._state.heartbeat_count}"
                    ),
                    source_occurred_at=self._heartbeat_source_occurred_at(call_event),
                )
                for _, tool_name, args, call_event, call_id in prepared
            ),
            return_exceptions=True,
        )
        for (call, tool_name, _args, call_event, call_id), outcome in zip(
            prepared,
            outcomes,
            strict=False,
        ):
            if isinstance(outcome, Exception):
                result_value = f"执行异常: {outcome}"
                success = False
            else:
                result_value, success = outcome
            self._append_heartbeat_tool_result_payload(
                response, call, tool_name, result_value
            )
            if heartbeat_run_id:
                await self.record_tool_result(
                    tool_name or "<unknown>",
                    result_value,
                    success,
                    heartbeat_run_id=heartbeat_run_id,
                    call_id=call_id,
                    parent_event_id=(call_event.event_id if call_event else None),
                    call_event=call_event,
                )
            else:
                await self.record_tool_result(
                    tool_name or "<unknown>", result_value, success
                )

        return len(prepared) * 2

    def _heartbeat_panels_enabled(self) -> bool:
        try:
            return bool(self._cfg().settings.log_heartbeat)
        except Exception:  # noqa: BLE001 - missing config must not block heartbeat
            return True

    def _heartbeat_panel_sink(self) -> str:
        try:
            return str(
                getattr(self._cfg().settings, "heartbeat_panel_sink", "stdout")
                or "stdout"
            )
        except Exception:  # noqa: BLE001 - missing config must not block heartbeat
            return "stdout"

    def _heartbeat_panel_path(self) -> str:
        try:
            return str(
                getattr(self._cfg().settings, "heartbeat_panel_path", "")
                or "logs/heartbeat.console"
            )
        except Exception:  # noqa: BLE001 - missing config must not block heartbeat
            return "logs/heartbeat.console"

    def _emit_heartbeat_panel(
        self,
        body: str,
        *,
        title: str,
        border_style: str,
    ) -> None:
        print_activity_panel(
            logger,
            body,
            title=title,
            border_style=border_style,
            sink=self._heartbeat_panel_sink(),
            path=self._heartbeat_panel_path(),
        )

    def _print_heartbeat_decision_panel(
        self,
        response: Any,
        *,
        turn_index: int,
    ) -> None:
        if not self._heartbeat_panels_enabled():
            return
        self._emit_heartbeat_panel(
            format_decision_panel(
                thought=str(getattr(response, "reasoning_content", "") or ""),
                monologue=str(getattr(response, "message", "") or ""),
                call_list=getattr(response, "call_list", None) or [],
                header_lines=(
                    f"心跳序号：#{self._state.heartbeat_count}",
                    f"本轮：{turn_index}",
                ),
            ),
            title="Life Engine 潜意识",
            border_style="cyan",
        )

    def _print_heartbeat_receipt_panel(self, results: list[Any]) -> None:
        if not self._heartbeat_panels_enabled():
            return
        self._emit_heartbeat_panel(
            format_tool_receipt_panel(results),
            title="Life Engine 工具回执",
            border_style="cyan",
        )

    def _print_heartbeat_skip_panel(
        self,
        *,
        reason: str,
        remaining: Any = None,
        until: Any = None,
    ) -> None:
        if not self._heartbeat_panels_enabled():
            return
        self._emit_heartbeat_panel(
            format_skip_panel(reason=reason, remaining=remaining, until=until),
            title="Life Engine 本轮跳过",
            border_style="cyan",
        )

    def _print_heartbeat_stall_panel(
        self,
        *,
        reason: str,
        stall_kind: str = "",
        stage: str = "",
        model_turns: int | None = None,
        tools: list[str] | None = None,
        consecutive_no_progress: int | None = None,
        consecutive_protocol_failures: int | None = None,
        consecutive_same_failure: int | None = None,
    ) -> None:
        if not self._heartbeat_panels_enabled():
            return
        self._emit_heartbeat_panel(
            format_stall_panel(
                heartbeat_count=self._state.heartbeat_count,
                reason=reason,
                stall_kind=stall_kind,
                stage=stage,
                model_turns=model_turns,
                tools=tools,
                consecutive_no_progress=consecutive_no_progress,
                consecutive_protocol_failures=consecutive_protocol_failures,
                consecutive_same_failure=consecutive_same_failure,
            ),
            title="Life Engine 心跳有序结束",
            border_style="cyan",
        )

    async def _record_heartbeat_model_turn_activity(
        self,
        response: Any,
        call_list: list[Any],
        *,
        heartbeat_run_id: str | None,
        turn_index: int,
    ) -> tuple[LifeEngineEvent, dict[int, str]]:
        """Append the complete generated heartbeat turn before its tools run."""

        run_identity = str(
            heartbeat_run_id or f"heartbeat:{self._state.heartbeat_count}"
        )
        turn_occurrence_id = f"{run_identity}:model-turn:{turn_index}"
        transport_request_id = str(
            getattr(response, "request_record_id", "")
            or f"{turn_occurrence_id}:transport"
        )
        call_id_overrides: dict[int, str] = {}
        call_ids: list[str] = []
        for call_index, call in enumerate(call_list):
            call_id = str(getattr(call, "id", "") or "").strip()
            if not call_id:
                call_id = f"{turn_occurrence_id}:call:{call_index}"
            call_id_overrides[id(call)] = call_id
            call_ids.append(call_id)
        activity_id = self._conscious_model_turn_activity_id(
            source_instance_id="life_engine_subconscious",
            stream_id=INTERNAL_STREAM_ID,
            turn_occurrence_id=turn_occurrence_id,
            transport_request_id=transport_request_id,
            surface="life_engine_subconscious",
        )
        event = self._event_builder.build_conscious_model_turn_event(
            activity_id=activity_id,
            transport_request_id=transport_request_id,
            stream_id=INTERNAL_STREAM_ID,
            source_instance_id="life_engine_subconscious",
            turn_occurrence_id=turn_occurrence_id,
            provider_reasoning_content=str(
                getattr(response, "reasoning_content", "") or ""
            ),
            assistant_message=str(getattr(response, "message", "") or ""),
            tool_call_ids=call_ids,
            surface="life_engine_subconscious",
            heartbeat_run_id=heartbeat_run_id,
        )
        await self._queue_pending_event(
            event,
            persist=heartbeat_run_id is None,
        )
        return event, call_id_overrides

    @staticmethod
    def _heartbeat_perception_receipt(
        response: Any,
        perception: PreparedPerception,
    ) -> PerceptionDeliveryReceipt | None:
        """Map the final successful attempt's content-free exact receipt."""

        lookup = getattr(response, "effective_context_receipt", None)
        effective = lookup(perception.delivery_id) if callable(lookup) else None
        if effective is None or not bool(getattr(effective, "exact_present", False)):
            return None
        expected_bytes = getattr(effective, "expected_utf8_bytes", None)
        effective_bytes = getattr(effective, "effective_utf8_bytes", None)
        expected_sha256 = getattr(effective, "expected_sha256", None)
        effective_sha256 = getattr(effective, "effective_sha256", None)
        if (
            not isinstance(expected_bytes, int)
            or expected_bytes <= 0
            or effective_bytes != expected_bytes
            or not isinstance(expected_sha256, str)
            or effective_sha256 != expected_sha256
        ):
            return None
        return PerceptionDeliveryReceipt(
            delivery_id=perception.delivery_id,
            projection_sha256=perception.projection_sha256,
            delivered_bytes=perception.delivered_bytes,
            exact=True,
            transport_request_id=str(getattr(response, "request_record_id", "") or ""),
        )

    @staticmethod
    def _subconscious_delivery_identity(
        wake_context: str,
    ) -> tuple[str, str]:
        """Read the sealed delivery identity without retaining prompt content."""

        first_line = str(wake_context or "").splitlines()[0].strip()
        prefix = '<subconscious_activity_projection delivery_id="'
        suffix = '">'
        if not first_line.startswith(prefix) or not first_line.endswith(suffix):
            return "", ""
        delivery_id = first_line[len(prefix) : -len(suffix)].strip()
        if not delivery_id:
            return "", ""
        return delivery_id, first_line

    @staticmethod
    def _heartbeat_subconscious_receipt(
        response: Any,
        delivery_id: str,
    ) -> EffectiveContextReceipt | None:
        """Return proof that the final successful attempt saw the exact wake text."""

        identity = str(delivery_id or "").strip()
        lookup = getattr(response, "effective_context_receipt", None)
        effective = lookup(identity) if identity and callable(lookup) else None
        if (
            effective is None
            or str(getattr(effective, "delivery_id", "") or "") != identity
            or not bool(getattr(effective, "exact_present", False))
            or getattr(effective, "effective_utf8_bytes", None)
            != getattr(effective, "expected_utf8_bytes", None)
            or getattr(effective, "effective_sha256", None)
            != getattr(effective, "expected_sha256", None)
        ):
            return None
        return effective

    @staticmethod
    def _heartbeat_tool_call_counts_as_activity(
        tool_name: str,
        args: dict[str, Any],
    ) -> bool:
        """Distinguish an actual chosen action from passive opportunity reading."""

        name = str(tool_name or "").strip()
        if name.startswith("tool-"):
            name = name[5:]
        if not name or name == "nucleus_rest_heartbeat":
            return False
        if name == "nucleus_proactive_query":
            return False
        if name == "nucleus_todo":
            action = str(args.get("action") or "list").strip().lower()
            return action in {"write", "update_plan", "todo_write"}
        if name == "nucleus_learn":
            from ..learning.learn_tool import learn_call_counts_as_activity

            return learn_call_counts_as_activity(
                str(args.get("action") or ""),
                args,
            )
        return True

    def _update_heartbeat_idle_count(
        self,
        tool_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Advance idle unless this heartbeat actually chose a non-passive action."""

        active_calls = [
            (name, args)
            for name, args in tool_calls
            if self._heartbeat_tool_call_counts_as_activity(name, args)
        ]
        if active_calls:
            self._state.idle_heartbeat_count = 0
            return

        self._state.idle_heartbeat_count += 1
        if tool_calls:
            logger.debug(
                "life_engine 心跳仅观察机会或休息，空闲计数: "
                f"{self._state.idle_heartbeat_count}"
            )
        else:
            logger.debug(
                "life_engine 心跳无实际动作，空闲计数: "
                f"{self._state.idle_heartbeat_count}"
            )

    async def _run_heartbeat_model(
        self,
        wake_context: str,
        *,
        heartbeat_run_id: str | None = None,
        world_perception: PreparedPerception | None = None,
        heartbeat_deadline: float | None = None,
    ) -> HeartbeatModelResult:
        """调用 life 任务模型生成内部报文；请求失败必须向上抛出。"""
        cfg = self._cfg()
        configured_timeout = float(
            getattr(cfg.settings, "heartbeat_timeout_seconds", 0)
            or cfg.settings.heartbeat_interval_seconds
        )
        total_budget = _resolve_heartbeat_total_budget(configured_timeout)
        if heartbeat_deadline is None:
            heartbeat_deadline = asyncio.get_running_loop().time() + total_budget
        task_name = cfg.model.task_name.strip() or "core"
        model_set = get_model_set_by_task(task_name)
        request = create_llm_request(
            model_set=model_set,
            request_name="life_engine_heartbeat",
        )

        system_prompt = await self._build_heartbeat_system_prompt()
        if system_prompt is None:
            # SOUL 权威文本不可用——没有灵魂就不说话
            return HeartbeatModelResult("", None)
        section_texts = await self._render_heartbeat_sections()
        opportunity_page = None
        bus = getattr(self, "_opportunity_bus", None)
        if bus is not None:
            get_page = getattr(bus, "get_pending_page", None)
            if callable(get_page):
                opportunity_page = get_page()
        user_prompt = self._build_heartbeat_model_prompt(
            wake_context,
            section_texts=section_texts,
        )
        (
            subconscious_delivery_id,
            subconscious_delivery_marker,
        ) = self._subconscious_delivery_identity(wake_context)
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))

        tools = self._get_nucleus_tools()
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        request.add_payload(LLMPayload(ROLE.TOOL, tools))

        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))
        if subconscious_delivery_id:
            request.register_context_delivery(
                subconscious_delivery_id,
                user_prompt,
                marker=subconscious_delivery_marker,
            )
        if world_perception is not None:
            request.register_context_delivery(
                world_perception.delivery_id,
                user_prompt,
                marker=world_perception.delivery_marker,
            )
        if opportunity_page is not None:
            request.register_context_delivery(
                str(opportunity_page.delivery_id),
                user_prompt,
                marker=str(opportunity_page.delivery_marker),
            )

        # 心跳请求超时与心跳间隔解耦：慢模型（长 prompt 的推理模型）单次可达上百秒，
        # 沿用间隔值会把正常的慢响应当成超时反复重试。
        timeout_seconds = _resolve_heartbeat_timeout(configured_timeout, model_set)

        logger.debug(
            f"life_engine heartbeat request: "
            f"system_prompt_len={len(system_prompt)} "
            f"user_prompt_len={len(user_prompt)} "
            f"tools_count={len(tools)}"
        )

        async def _send_heartbeat_request() -> Any:
            return await _await_with_heartbeat_deadline(
                lambda: request.send(stream=False),
                deadline=heartbeat_deadline,
                stage="initial_request",
                per_call_timeout=timeout_seconds,
            )

        try:
            response = await _send_heartbeat_request()
        except HeartbeatBudgetExhausted:
            raise
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
                    if subconscious_delivery_id:
                        fallback_request.register_context_delivery(
                            subconscious_delivery_id,
                            user_prompt,
                            marker=subconscious_delivery_marker,
                        )
                    if world_perception is not None:
                        fallback_request.register_context_delivery(
                            world_perception.delivery_id,
                            user_prompt,
                            marker=world_perception.delivery_marker,
                        )
                    if opportunity_page is not None:
                        fallback_request.register_context_delivery(
                            str(opportunity_page.delivery_id),
                            user_prompt,
                            marker=str(opportunity_page.delivery_marker),
                        )
                    response = await _await_with_heartbeat_deadline(
                        lambda: fallback_request.send(stream=False),
                        deadline=heartbeat_deadline,
                        stage="fallback_request",
                        per_call_timeout=timeout_seconds,
                    )
                    logger.info("life_engine 心跳降级成功(utils)")
                except HeartbeatBudgetExhausted:
                    raise
                except Exception as fallback_exc:
                    logger.error(f"Heartbeat fallback also failed: {fallback_exc}")
                    raise e from fallback_exc
            else:
                logger.error(f"Heartbeat request failed after all retries: {e}")
                raise

        max_rounds = max(1, int(cfg.settings.max_rounds_per_heartbeat))
        last_text = ""
        final_response_observed = False
        heartbeat_tool_calls: list[tuple[str, dict[str, Any]]] = []
        previous_successful_round_fingerprint = ""
        previous_failure_fingerprint = ""
        consecutive_no_progress = 0
        consecutive_protocol_failures = 0
        consecutive_same_failure = 0
        stall_limit = max(
            1,
            int(cfg.settings.max_consecutive_tool_stalls_per_heartbeat),
        )
        stop_reason = ""
        stop_stage = ""
        last_round_outcomes: list[str] = []
        perception_receipt = (
            self._heartbeat_perception_receipt(response, world_perception)
            if world_perception is not None
            else None
        )
        subconscious_receipt = (
            self._heartbeat_subconscious_receipt(
                response,
                subconscious_delivery_id,
            )
            if subconscious_delivery_id
            else None
        )
        if subconscious_delivery_id and subconscious_receipt is None:
            raise PerceptionDeliveryUnverified(
                "heartbeat model did not receive the exact subconscious activity projection"
            )
        if world_perception is not None and perception_receipt is None:
            raise PerceptionDeliveryUnverified(
                "heartbeat model did not receive the exact World projection"
            )
        opportunity_receipt = (
            self._heartbeat_subconscious_receipt(
                response,
                str(opportunity_page.delivery_id),
            )
            if opportunity_page is not None
            else None
        )

        for turn_index in range(max_rounds):
            try:
                response_text = await _await_with_heartbeat_deadline(
                    lambda: response,
                    deadline=heartbeat_deadline,
                    stage="response_read",
                    per_call_timeout=timeout_seconds,
                )
            except HeartbeatBudgetExhausted:
                raise
            except asyncio.TimeoutError as exc:
                logger.warning("life_engine heartbeat response read timeout")
                raise TimeoutError("heartbeat response read timeout") from exc

            turn_text = str(response_text or "").strip()
            call_list = list(getattr(response, "call_list", []) or [])
            model_turn_event, call_id_overrides = (
                await self._record_heartbeat_model_turn_activity(
                    response,
                    call_list,
                    heartbeat_run_id=heartbeat_run_id,
                    turn_index=turn_index,
                )
            )
            self._print_heartbeat_decision_panel(response, turn_index=turn_index)

            logger.debug(
                f"life_engine heartbeat turn: "
                f"text_len={len(turn_text)} call_count={len(call_list)}"
            )

            if not call_list:
                last_text = turn_text
                final_response_observed = True
                break

            # Text attached to a tool-bearing response is non-terminal.  It
            # may describe an intention before the tool actually fails, so it
            # cannot be persisted as an observed completion statement.
            last_text = ""

            logger.debug(
                f"life_engine 心跳#{self._state.heartbeat_count} 本轮调用列表："
                f"{[getattr(call, 'name', '<unknown>') for call in call_list]}"
            )

            result_count_before = len(_heartbeat_tool_results(response))
            for batch, can_parallel in iter_life_tool_call_batches(call_list):
                for call in batch:
                    tool_name, args = self._heartbeat_tool_call_metadata(call)
                    heartbeat_tool_calls.append((tool_name, dict(args)))
                    args.pop("reason", None)
                    logger.debug(
                        f"life_engine 心跳#{self._state.heartbeat_count} "
                        f"LLM 调用 {tool_name or '<unknown>'}，"
                        f"参数键: {sorted(args)}"
                    )

                if can_parallel and len(batch) > 1:
                    await self._execute_heartbeat_tool_call_batch(
                        batch,
                        response,
                        registry,
                        heartbeat_run_id=heartbeat_run_id,
                        model_turn_event_id=model_turn_event.event_id,
                        call_id_overrides=call_id_overrides,
                    )
                    continue

                for call in batch:
                    await self._execute_heartbeat_tool_call(
                        call,
                        response,
                        registry,
                        heartbeat_run_id=heartbeat_run_id,
                        model_turn_event_id=model_turn_event.event_id,
                        call_id_override=call_id_overrides.get(id(call), ""),
                    )

            round_results = _heartbeat_tool_results(response)[result_count_before:]
            self._print_heartbeat_receipt_panel(round_results)
            last_round_outcomes = _heartbeat_tool_round_outcomes(
                call_list, round_results
            )
            progress = _heartbeat_tool_round_progress(call_list, round_results)
            successful_progress = progress.has_successful_mutation or (
                progress.has_success
                and progress.fingerprint != previous_successful_round_fingerprint
            )
            if successful_progress:
                consecutive_no_progress = 0
                previous_successful_round_fingerprint = progress.fingerprint
            else:
                consecutive_no_progress += 1

            if progress.has_protocol_failure:
                consecutive_protocol_failures += 1
            else:
                consecutive_protocol_failures = 0

            if progress.failure_fingerprint:
                if progress.failure_fingerprint == previous_failure_fingerprint:
                    consecutive_same_failure += 1
                else:
                    consecutive_same_failure = 1
                    previous_failure_fingerprint = progress.failure_fingerprint
            else:
                consecutive_same_failure = 0
                previous_failure_fingerprint = ""

            if (
                max(
                    consecutive_no_progress,
                    consecutive_protocol_failures,
                    consecutive_same_failure,
                )
                >= stall_limit
            ):
                stop_reason = "consecutive_tool_stalls"
                stop_stage = "tool_round"
                break

            if turn_index + 1 >= max_rounds:
                stop_reason = "max_model_turns"
                stop_stage = "tool_round"
                break

            # 工具轮之后的续问必须和首次请求同等强度地重试：这里原本是裸的
            # wait_for，首个模型一挂就直接把整个心跳判死，而首次请求那边有
            # retry_with_backoff。心跳的绝大多数超时都发生在这一步（工具执行
            # 之后上下文最长、最慢），少了重试等于把最脆弱的一环裸奔。
            # 重发是幂等的：response.send() 每次都带同一份累积 payload 重投。
            current_response = response
            pending_memory_deliveries = (
                self._register_pending_heartbeat_memory_deliveries(current_response)
            )
            if world_perception is not None:
                current_response.register_context_delivery(
                    world_perception.delivery_id,
                    user_prompt,
                    marker=world_perception.delivery_marker,
                )
            if subconscious_delivery_id:
                current_response.register_context_delivery(
                    subconscious_delivery_id,
                    user_prompt,
                    marker=subconscious_delivery_marker,
                )
            if opportunity_page is not None:
                current_response.register_context_delivery(
                    str(opportunity_page.delivery_id),
                    user_prompt,
                    marker=str(opportunity_page.delivery_marker),
                )

            async def _send_followup_request() -> Any:
                from src.kernel.llm.exceptions import LLMModelsCoolingDownError

                try:
                    return await _await_with_heartbeat_deadline(
                        lambda: current_response.send(stream=False),
                        deadline=heartbeat_deadline,
                        stage="followup_request",
                        per_call_timeout=timeout_seconds,
                    )
                except LLMModelsCoolingDownError as cooldown_exc:
                    # 首次请求超时会让唯一候选进入约 30s 冷却；续轮立即重发会
                    # 直接撞冷却窗口。按 retry_after 等待（受步进预算约束）
                    # 后再真实重发，而不是把冷却误报为 Provider 再次超时。
                    wait_seconds = min(cooldown_exc.retry_after, timeout_seconds)
                    logger.info(
                        f"life_engine heartbeat follow-up 候选模型冷却中，"
                        f"等待 {wait_seconds:.1f}s 后重试"
                    )
                    await _sleep_with_heartbeat_deadline(
                        wait_seconds,
                        deadline=heartbeat_deadline,
                        stage="followup_cooldown",
                    )
                    return await _await_with_heartbeat_deadline(
                        lambda: current_response.send(stream=False),
                        deadline=heartbeat_deadline,
                        stage="followup_after_cooldown",
                        per_call_timeout=timeout_seconds,
                    )

            from src.kernel.llm.exceptions import LLMModelsCoolingDownError

            try:
                try:
                    response = await _send_followup_request()
                except asyncio.TimeoutError:
                    await _sleep_with_heartbeat_deadline(
                        2.0,
                        deadline=heartbeat_deadline,
                        stage="followup_backoff",
                    )
                    response = await _send_followup_request()
                await self._commit_heartbeat_memory_deliveries(
                    response,
                    pending_memory_deliveries,
                )
                if world_perception is not None:
                    perception_receipt = self._heartbeat_perception_receipt(
                        response,
                        world_perception,
                    )
                    if perception_receipt is None:
                        raise PerceptionDeliveryUnverified(
                            "heartbeat follow-up lost the exact World projection"
                        )
                if subconscious_delivery_id:
                    subconscious_receipt = self._heartbeat_subconscious_receipt(
                        response,
                        subconscious_delivery_id,
                    )
                    if subconscious_receipt is None:
                        raise PerceptionDeliveryUnverified(
                            "heartbeat follow-up lost the exact subconscious activity projection"
                        )
                if opportunity_page is not None:
                    opportunity_receipt = self._heartbeat_subconscious_receipt(
                        response,
                        str(opportunity_page.delivery_id),
                    )
            except HeartbeatBudgetExhausted as exc:
                self._discard_pending_heartbeat_memory_deliveries(
                    pending_memory_deliveries
                )
                stop_reason = "deadline_exhausted"
                stop_stage = exc.stage
                break
            except (
                asyncio.TimeoutError,
                LLMModelsCoolingDownError,
            ) as exc:
                self._discard_pending_heartbeat_memory_deliveries(
                    pending_memory_deliveries
                )
                # 冷却窗口内二次失败同样归一化为超时，保持心跳失败合同一致，
                # 不把冷却中的本轮伪装成成功。
                logger.warning(
                    f"life_engine heartbeat follow-up request timeout: {exc}"
                )
                raise TimeoutError("heartbeat follow-up request timeout") from exc
            except asyncio.CancelledError:
                self._discard_pending_heartbeat_memory_deliveries(
                    pending_memory_deliveries
                )
                raise
            except Exception:
                self._discard_pending_heartbeat_memory_deliveries(
                    pending_memory_deliveries
                )
                raise

        if stop_reason:
            stall_kind = (
                _heartbeat_stall_kind(
                    consecutive_no_progress=consecutive_no_progress,
                    consecutive_protocol_failures=consecutive_protocol_failures,
                    consecutive_same_failure=consecutive_same_failure,
                    stall_limit=stall_limit,
                )
                if stop_reason == "consecutive_tool_stalls"
                else stop_reason
            )
            tool_summary = ",".join(last_round_outcomes) or "-"
            logger.warning(
                "life_engine 心跳工具续轮有序结束: "
                f"#{self._state.heartbeat_count} "
                f"reason={stop_reason} stall={stall_kind} stage={stop_stage} "
                f"model_turns={turn_index + 1} "
                f"tools={tool_summary} "
                f"consecutive_no_progress={consecutive_no_progress} "
                f"consecutive_protocol_failures={consecutive_protocol_failures} "
                f"consecutive_same_failure={consecutive_same_failure}"
            )
            log_heartbeat_event(
                event="heartbeat_tool_loop_stopped",
                heartbeat_count=self._state.heartbeat_count,
                heartbeat_run_id=heartbeat_run_id or "",
                stop_reason=stop_reason,
                stall_kind=stall_kind,
                stop_stage=stop_stage,
                model_turns=turn_index + 1,
                last_round_tools=last_round_outcomes,
                consecutive_no_progress=consecutive_no_progress,
                consecutive_protocol_failures=consecutive_protocol_failures,
                consecutive_same_failure=consecutive_same_failure,
            )
            self._print_heartbeat_stall_panel(
                reason=stop_reason,
                stall_kind=stall_kind,
                stage=stop_stage,
                model_turns=turn_index + 1,
                tools=last_round_outcomes,
                consecutive_no_progress=consecutive_no_progress,
                consecutive_protocol_failures=consecutive_protocol_failures,
                consecutive_same_failure=consecutive_same_failure,
            )

        # active/open 只是可见线索；只有本轮由主体实际选择的非被动动作才重置 idle。
        self._update_heartbeat_idle_count(heartbeat_tool_calls)

        # 三环自学习系统心跳（低频后台任务：审计/压缩/指标）
        try:
            await _await_with_heartbeat_deadline(
                self._run_learning_heartbeat_maintenance,
                deadline=heartbeat_deadline,
                stage="learning_maintenance",
            )
        except HeartbeatBudgetExhausted:
            logger.debug("life_engine 心跳剩余预算不足，跳过本轮学习维护")

        if not final_response_observed:
            # Infrastructure must not author first-person meaning or claim
            # completion on the subject's behalf.  Durable tool receipts stay
            # in the event ledger; this heartbeat simply has no final text.
            last_text = ""

        if (
            opportunity_page is not None
            and bus is not None
            and opportunity_receipt is not None
        ):
            try:
                await bus.commit_page_delivery(
                    str(opportunity_page.delivery_id),
                    opportunity_receipt,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - invitation repeats safely
                logger.warning(
                    "opportunity page delivery projection failed: "
                    f"error_type={type(error).__name__}"
                )

        return HeartbeatModelResult(
            last_text,
            perception_receipt,
            subconscious_receipt,
        )

    async def _run_learning_heartbeat_maintenance(self) -> None:
        """Wake derived learning without spending foreground heartbeat budget."""

        scheduler = self._learning_scheduler
        if scheduler is None:
            return
        scheduler.request_maintenance()

    def _mark_runtime_context_persisted(self) -> None:
        """Mark the exact state snapshot protected by ``self._lock`` clean."""

        self._state_dirty = False

    async def _save_runtime_context(
        self,
        *,
        recoverable_on_shared_conflict: bool = False,
    ) -> None:
        """持久化当前上下文。

        Args:
            recoverable_on_shared_conflict: 透传给
                ``StatePersistence.save_runtime_context``。仅 shared 多写者
                模式生效：为 True 时合并重试窗口内的 CAS 冲突视为合法竞争
                （采纳远端最新值，不抛错、不置脏），供心跳等可恢复技术
                checkpoint 使用；为 False 时保持精确持久化语义，供 chatter
                checkpoint 等必须耐久写入的路径使用。
        """
        from .state_manager import PersistenceError

        if self._state_persistence is None:
            self._state_persistence = StatePersistence(
                self._cfg().settings.workspace_path,
                self._history_limit,
                self._lock,
                runtime_store=self.runtime_state_store(),
                runtime_writer_claim=self._runtime_context_writer_claim,
                on_persisted=self._mark_runtime_context_persisted,
            )
        try:
            cleaned_atomically = await self._state_persistence.save_runtime_context(
                self._state,
                self._pending_events,
                self._event_history,
                recoverable_on_shared_conflict=recoverable_on_shared_conflict,
            )
            if not cleaned_atomically:
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
            if self._selectable_storage_enabled:
                raise

    async def _load_runtime_context(self) -> None:
        """从持久化文件恢复上下文。"""
        if self._state_persistence is None:
            self._state_persistence = StatePersistence(
                self._cfg().settings.workspace_path,
                self._history_limit,
                self._lock,
                runtime_store=self.runtime_state_store(),
                runtime_writer_claim=self._runtime_context_writer_claim,
                on_persisted=self._mark_runtime_context_persisted,
            )
        (
            pending,
            history,
            persisted,
        ) = await self._state_persistence.load_runtime_context(
            self._state,
            self._next_sequence,
        )
        self._pending_events = pending
        self._event_history = history
        self._restore_inner_dialogue_ledger()
        try:
            await self.catch_up_subconscious_ingest()
        except (SubconsciousLedgerGap, SubconsciousIngestStoreUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - load continues; next prepare retries
            logger.warning(
                "life_engine 启动后潜意识账本追赶失败，将在心跳 prepare 重试: "
                f"error_type={type(exc).__name__}"
            )

    async def _start_shared_sync(self, cfg: Any) -> None:
        """Start the legacy-local sync bridge or report expected disablement."""

        shared_sync_cfg = getattr(cfg, "shared_sync", None)
        self._shared_sync_configured_enabled = bool(
            getattr(shared_sync_cfg, "enabled", False)
        )
        self._shared_sync_effective_enabled = (
            self._shared_sync_configured_enabled
            and not self._selectable_storage_enabled
        )
        self._shared_sync_disabled_reason = ""
        if (
            self._shared_sync_configured_enabled
            and not self._shared_sync_effective_enabled
        ):
            self._shared_sync_error = ""
            self._shared_sync_disabled_reason = (
                "selected_authoritative_backend_unsupported"
            )
            logger.warning(
                "legacy shared_sync is disabled for selected authoritative "
                "Life Event storage; selected export-outbox bridge is not implemented"
            )
            return
        if not self._shared_sync_effective_enabled:
            return
        try:
            from .shared_sync import SharedSyncBridge

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
            logger.error(
                f"life_engine 共享同步初始化失败: {self._shared_sync_error}"
            )

    async def start(self) -> None:
        """Start all consumers, rolling back them before the owned runtime."""

        try:
            await self._start_impl()
        except BaseException as primary:
            # Subject-authority validation and other preflight checks run before
            # ``_stop_event`` is created. Skip the write-capable full shutdown
            # only while no selected runtime ownership has been acquired. A
            # claim wait may fail or be cancelled after that runtime is open,
            # and its authority must still be revoked and closed.
            if self._stop_event is None and self._storage_runtime is None:
                if self._minecraft_session is not None:
                    try:
                        await self._close_minecraft_session()
                    except Exception as cleanup_error:  # noqa: BLE001
                        primary.add_note(
                            "LifeEngineService Minecraft startup cleanup also failed: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                raise
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
            if not self._selectable_storage_enabled:
                await self.clear_runtime_context()
            return

        await self._validate_local_subject_authority()
        sleep_enabled, sleep_desc = self._sleep_window_status()
        if not sleep_enabled and sleep_desc != "disabled":
            logger.warning(
                "life_engine 睡眠时段配置无效，已忽略。"
                "请使用 HH:MM 格式，且 sleep_time 与 wake_time 不可相同。"
            )

        # The selected runtime acquires writer authority before returning. Start
        # renewal immediately so slow Memory recovery cannot outlive that lease.
        self._stop_event = asyncio.Event()
        self._initialize_local_runtime_state()
        await self._open_selected_storage_runtime()
        self._start_storage_authority_renewal()
        await self._start_local_proactive_authority()

        # 初始化集成管理器
        self._memory_integration = MemoryIntegration(self)
        await self._memory_integration.init_memory_service()
        self._require_selected_memory_service()

        await self._start_selected_storage()
        await self._refresh_proactive_health()
        self._attach_proactive_delivery_proof_hook()
        await self._load_runtime_context()

        # Minecraft is a scene capability owned by the service.  It must be
        # available even when the optional learning system is disabled.
        await self._initialize_minecraft_session()

        # 初始化三环自学习系统
        learning_cfg = getattr(cfg, "learning", None)
        if learning_cfg is None or getattr(learning_cfg, "enabled", True):
            try:
                # A non-owner keeps only the canonical immutable event port.
                # It never constructs local insights, skills, maintenance, or
                # prompt projections beside the selected database authority.
                if (
                    self._selectable_storage_enabled
                    and self._learning_stores is None
                ):
                    logger.warning(
                        "learning starts in immutable event-only mode; selected "
                        "projection and maintenance remain disabled because "
                        "this instance does not own the singleton projector"
                    )
                self._learning_scheduler = self._build_learning_runtime(
                    workspace_path=cfg.settings.workspace_path,
                    model_task_name=(
                        str(
                            getattr(learning_cfg, "model_task_name", "learning")
                            or "learning"
                        ).strip()
                        or "learning"
                    ),
                    llm_timeout_seconds=float(
                        getattr(learning_cfg, "llm_timeout_seconds", 900.0) or 900.0
                    ),
                    # 反思候选可进入记忆检索；只有显式归属活跃意识实例的
                    # 主动反思才写 subject-authored interpretation。
                    memory_service=self._memory_service,
                    learning_store=(
                        self._learning_stores.store
                        if self._learning_stores is not None
                        else None
                    ),
                    learning_event_store=(
                        self._learning_event_store
                        if self._selectable_storage_enabled
                        else None
                    ),
                    subject_authority=(
                        self._subject_document_store
                        if self._selectable_storage_enabled
                        else None
                    ),
                    project_subject_commit=(
                        self._project_learning_subject_authority_commit
                        if self._selectable_storage_enabled
                        else None
                    ),
                    current_subject_revision=(
                        None
                        if self._selectable_storage_enabled
                        else self._current_learning_subject_revision
                    ),
                    read_subject_authority=(
                        self._subject_document_store.read_subject_authority
                        if self._selectable_storage_enabled
                        and self._subject_document_store is not None
                        else None
                    ),
                    validate_active_consciousness_instance=(
                        self._validate_learning_decision_actor
                    ),
                    writer_instance_id=self._storage_writer_instance_id,
                    audit_interval_hours=float(
                        getattr(learning_cfg, "audit_interval_hours", 6.0)
                        if learning_cfg
                        else 6.0
                    ),
                    audit_batch_size=int(
                        getattr(learning_cfg, "audit_batch_size", 3)
                        if learning_cfg
                        else 3
                    ),
                    compress_trigger_count=int(
                        getattr(learning_cfg, "compress_trigger_count", 5)
                        if learning_cfg
                        else 5
                    ),
                    compress_interval_hours=float(
                        getattr(learning_cfg, "compress_interval_hours", 48.0)
                        if learning_cfg
                        else 48.0
                    ),
                    subject_review_enabled=bool(
                        getattr(learning_cfg, "subject_review_enabled", True)
                        if learning_cfg
                        else True
                    ),
                    subject_review_soul_interval_hours=float(
                        getattr(
                            learning_cfg, "subject_review_soul_interval_hours", 720.0
                        )
                        if learning_cfg
                        else 720.0
                    ),
                    subject_review_user_interval_hours=float(
                        getattr(
                            learning_cfg, "subject_review_user_interval_hours", 720.0
                        )
                        if learning_cfg
                        else 720.0
                    ),
                    subject_review_memory_interval_hours=float(
                        getattr(
                            learning_cfg, "subject_review_memory_interval_hours", 168.0
                        )
                        if learning_cfg
                        else 168.0
                    ),
                    subject_review_offer_cooldown_hours=float(
                        getattr(
                            learning_cfg, "subject_review_offer_cooldown_hours", 24.0
                        )
                        if learning_cfg
                        else 24.0
                    ),
                    reflection_cooldown_minutes=float(
                        getattr(learning_cfg, "reflection_cooldown_minutes", 5.0)
                        if learning_cfg
                        else 5.0
                    ),
                    skill_distill_trigger_count=int(
                        getattr(learning_cfg, "skill_distill_trigger_count", 3)
                        if learning_cfg
                        else 3
                    ),
                    skill_distill_interval_hours=float(
                        getattr(learning_cfg, "skill_distill_interval_hours", 24.0)
                        if learning_cfg
                        else 24.0
                    ),
                )
                await self._learning_scheduler.initialize()
                logger.info("三环自学习系统已初始化")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "三环自学习系统初始化失败: %s",
                    type(exc).__name__,
                )
                self._learning_scheduler = None
                if self._selectable_storage_enabled:
                    raise RuntimeError(
                        "SelectedLearningStorageInitializationFailed"
                    ) from exc

        self._state.running = True
        self._state.started_at = _now_iso()
        self._state.last_heartbeat_at = (
            self._state.last_heartbeat_at or self._state.started_at
        )
        self._state.last_error = None
        self._state.history_event_count = len(self._event_history)
        self._state.pending_event_count = len(self._pending_events)

        register_life_engine_service(self)

        # Legacy AutonomyIntent snapshots remain readable evidence, but their
        # stream-bound scheduler is intentionally not restored. Subject
        # initiative now requires an explicit InitiativeSeed decision followed
        # by a separate audience/surface decision at action time.
        try:
            legacy_scheduled = len(await self._autonomy_store().list_scheduled())
            removed_schedules = await cleanup_autonomy_schedules(
                self._cfg().settings.workspace_path,
                store=self._autonomy_store(),
            )
        except Exception:  # noqa: BLE001 - diagnostic only, never mutates evidence
            legacy_scheduled = -1
            removed_schedules = -1
        logger.info(
            "旧 AutonomyIntent 已进入只读退役态: "
            f"scheduled_count={legacy_scheduled} restored=0 "
            f"technical_schedules_removed={removed_schedules}"
        )

        await self._start_shared_sync(cfg)

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
                # When a selected store is bound it becomes the only authority
                # source for SOUL/USER/MEMORY; local Markdown is never read as
                # a fallback and a remote gap fails closed instead.
                subject_store=self._subject_document_store,
                runtime_store=self._runtime_state_store,
            )
            projection_task = get_task_manager().create_task(
                self._router_context_projection.run(),
                name="life_engine_router_context_projection",
                daemon=True,
            )
            self._router_context_projection_task_id = projection_task.task_id

        if self._subject_workspace_projector is not None:
            subject_task = get_task_manager().create_task(
                self._subject_projection_loop(),
                name="life_engine_subject_workspace_projection",
                daemon=True,
            )
            self._subject_projection_task_id = subject_task.task_id

        if self._learning_scheduler is not None and bool(
            getattr(self._learning_scheduler, "projector_owner", True)
        ):
            learning_cfg = getattr(cfg, "learning", None)
            learning_task = get_task_manager().create_task(
                self._learning_scheduler.run(
                    self._stop_event,
                    poll_interval_seconds=float(
                        getattr(
                            learning_cfg,
                            "maintenance_poll_seconds",
                            15.0,
                        )
                        or 15.0
                    ),
                ),
                name="life_engine_learning_maintenance",
                daemon=True,
            )
            self._learning_maintenance_task_id = learning_task.task_id

        if self._proactive_authority is not None:
            initiative_task = get_task_manager().create_task(
                self._initiative_reencounter_loop(),
                name="life_engine_initiative_reencounter",
                daemon=True,
            )
            self._initiative_reencounter_task_id = initiative_task.task_id

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

        if self._memory_service is not None:
            self._memory_service.set_behavior_health_provider(
                self._memory_behavior_health_snapshot
            )

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

        await self._await_managed_task(
            self._storage_authority_renew_task_id,
            timeout=5.0,
        )
        self._storage_authority_renew_task_id = None

        if self._router_context_projection is not None:
            self._router_context_projection.request_stop()

        await self._await_managed_task(
            self._subject_projection_task_id,
            timeout=10.0,
        )
        self._subject_projection_task_id = None

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
        detach_behavior_health = getattr(
            self._memory_service,
            "set_behavior_health_provider",
            None,
        )
        if callable(detach_behavior_health):
            detach_behavior_health(None)
        self._memory_witness_coordinator = None
        await self._await_managed_task(self._memory_index_task_id, timeout=10.0)
        self._memory_index_task_id = None
        await self._await_managed_task(self._heartbeat_task_id, timeout=5.0)
        self._heartbeat_task_id = None
        await self._await_managed_task(
            self._initiative_reencounter_task_id,
            timeout=5.0,
        )
        self._initiative_reencounter_task_id = None
        await self._await_managed_task(
            self._learning_maintenance_task_id,
            timeout=10.0,
        )
        self._learning_maintenance_task_id = None

        try:
            await self._close_minecraft_session()
        except Exception as exc:  # noqa: BLE001 - continue owned cleanup
            shutdown_errors.append(exc)
            logger.error(f"关闭 Minecraft 运行时失败: {type(exc).__name__}")

        learning_scheduler = self._learning_scheduler
        if learning_scheduler is not None:
            try:
                await learning_scheduler.close()
            except Exception as exc:  # noqa: BLE001 - continue owned cleanup
                shutdown_errors.append(exc)
                logger.error(
                    "关闭学习系统失败",
                    exc_info=True,
                )  # noqa: G201 - project Logger has no exception()
            finally:
                self._learning_scheduler = None

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
            await cleanup_autonomy_schedules(
                self._cfg().settings.workspace_path,
                store=self._autonomy_store(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"清理自主意向调度失败: {exc}")
        self._stop_event = None
        unregister_life_engine_service()
        try:
            try:
                if (
                    not self._selectable_storage_enabled
                    or self._runtime_state_store is not None
                ):
                    # 关闭路径的最终 checkpoint：双实例同时关闭时冲突属合法
                    # 竞争，可恢复，避免关闭流程被 RuntimeStateConflict 打断
                    # 刷 ERROR。
                    # ⚠️ 2026-09-01 现状更正：当前 backend="local" 且 multi_writer_enabled=false，
                    # 双实例共享场景不存在。此处逻辑是为多实例/多写者模式预留的防御；
                    # 单实例下若出现该冲突，应排查并发写入源而非当作合法竞争放过。
                    await self._save_runtime_context(
                        recoverable_on_shared_conflict=True
                    )
            except Exception as exc:  # noqa: BLE001 - release authority regardless
                shutdown_errors.append(exc)
                logger.error(
                    "保存 life_engine 运行上下文失败，继续释放 selected storage",
                    exc_info=True,
                )  # noqa: G201 - project Logger has no exception()
        finally:
            try:
                await self._close_selected_storage()
            except Exception as exc:  # noqa: BLE001 - finish remaining shutdown
                shutdown_errors.append(exc)
                logger.error(
                    "关闭 selected Presence/World storage 失败",
                    exc_info=True,
                )  # noqa: G201 - project Logger has no exception()
            try:
                await self._close_local_proactive_authority()
            except Exception as exc:  # noqa: BLE001 - finish remaining shutdown
                shutdown_errors.append(exc)
                logger.error(
                    "关闭本地统一主动权威失败",
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

            # 多写者：注册并认领本序列的 heartbeat operation。同一
            # consciousness instance 的同一 sequence 只有一个 owner；被其他
            # 实例认领（或已完成）时本实例跳过本轮，避免重复模型调用。
            bridge = self._multi_writer_bridge
            heartbeat_claim = None
            heartbeat_input_cursor = int(self._state.heartbeat_context_cursor or 0)
            if bridge is not None and getattr(bridge, "enabled", False):
                try:
                    await bridge.register_heartbeat_operation(
                        consciousness_instance_id="chat_global",
                        sequence=self._state.heartbeat_count,
                        input_frontier={
                            "sequence": self._state.heartbeat_count,
                            "cursor": heartbeat_input_cursor,
                            "pending": int(self._state.pending_event_count or 0),
                            "node": bridge.node_id,
                        },
                        prepared_context_digest="",
                    )
                    heartbeat_claim = await bridge.claim_heartbeat_operation(
                        consciousness_instance_id="chat_global",
                        sequence=self._state.heartbeat_count,
                        lease_seconds=(
                            self._storage_factory_settings.authority_lease_seconds
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - degrade to legacy path
                    logger.warning(
                        f"多写者 heartbeat operation 认领异常，继续单实例路径: {exc}"
                    )
                    heartbeat_claim = None
                if heartbeat_claim is None:
                    logger.info(
                        f"life_engine heartbeat #{self._state.heartbeat_count} "
                        f"已被其他实例认领或已完成，跳过本轮"
                    )
                    self._print_heartbeat_skip_panel(
                        reason="已被其他实例认领或已完成",
                    )
                    return "", _SKIPPED_HEARTBEAT_PREPARED

            try:
                if collect_background_agents:
                    await self._collect_background_agent_results()
                    await self._collect_background_mission_results()

                prepared = await self._prepare_heartbeat_context()
                log_heartbeat_event(
                    heartbeat_count=self._state.heartbeat_count,
                    last_heartbeat_at=self._state.last_heartbeat_at,
                    pending_message_count=self._state.pending_event_count,
                    last_wake_context_at=self._state.last_wake_context_at,
                    last_wake_context_size=self._state.last_wake_context_size,
                )
                # 心跳 LLM 总预算由整轮共享；首轮、fallback、续轮、冷却与退避
                # 都只能消费同一个 monotonic deadline。外层 wait_for 仅是最后保险。
                _cfg_hb_timeout = float(
                    getattr(self._cfg().settings, "heartbeat_timeout_seconds", 120)
                    or 120
                )
                _heartbeat_llm_budget = _resolve_heartbeat_total_budget(_cfg_hb_timeout)
                _heartbeat_deadline = (
                    asyncio.get_running_loop().time() + _heartbeat_llm_budget
                )
                try:
                    model_result = await asyncio.wait_for(
                        self._run_heartbeat_model(
                            prepared.content,
                            heartbeat_run_id=heartbeat_run_id,
                            world_perception=prepared.world_perception,
                            heartbeat_deadline=_heartbeat_deadline,
                        ),
                        timeout=_heartbeat_llm_budget,
                    )
                except HeartbeatBudgetExhausted as exc:
                    logger.warning(
                        f"life_engine 心跳 #{self._state.heartbeat_count} "
                        f"共享 LLM 预算已耗尽: stage={exc.stage}"
                    )
                    log_heartbeat_event(
                        event="heartbeat_model_budget_exhausted",
                        heartbeat_count=self._state.heartbeat_count,
                        heartbeat_run_id=heartbeat_run_id,
                        stop_stage=exc.stage,
                        budget_seconds=_heartbeat_llm_budget,
                    )
                    return "", prepared
                except asyncio.TimeoutError:
                    logger.warning(
                        f"life_engine 心跳 #{self._state.heartbeat_count} "
                        f"外层保险超时 {_heartbeat_llm_budget:.0f}s，跳过本轮"
                    )
                    if heartbeat_claim is not None and bridge is not None:
                        try:
                            await bridge.mark_heartbeat_operation_failed(
                                consciousness_instance_id="chat_global",
                                sequence=self._state.heartbeat_count,
                                claim_epoch=heartbeat_claim.claim_epoch,
                                retryable=True,
                            )
                        except Exception as mark_exc:  # noqa: BLE001
                            logger.warning(f"多写者 heartbeat 超时释放异常: {mark_exc}")
                    return "", prepared
                model_reply = model_result.text
                await self._record_model_reply(
                    model_reply,
                    heartbeat_run_id=heartbeat_run_id,
                    persist=False,
                )
                await self._commit_heartbeat_context(
                    prepared,
                    model_reply,
                    heartbeat_run_id,
                    model_result.perception_receipt,
                    model_result.subconscious_receipt,
                )
                # 多写者：提交 heartbeat checkpoint（失败不推进 frontier，
                # 已完成的重试返回既有结果）。
                if heartbeat_claim is not None and bridge is not None:
                    committed = await bridge.commit_heartbeat_operation(
                        consciousness_instance_id="chat_global",
                        sequence=self._state.heartbeat_count,
                        claim_epoch=heartbeat_claim.claim_epoch,
                        input_frontier=heartbeat_input_cursor,
                        committed_frontier=int(
                            self._state.heartbeat_context_cursor or 0
                        ),
                        result_ref=f"heartbeat://{heartbeat_run_id}",
                        result_digest=_stable_text_digest(str(model_reply or "")),
                    )
                    if committed is None:
                        logger.warning(
                            "多写者 heartbeat checkpoint 提交失败"
                            f"（#{self._state.heartbeat_count}），frontier 未推进；"
                            "结果仍按单实例语义落库，下次心跳可重放"
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
                            actor_consciousness_instance_id="chat_global",
                        ),
                        name=f"life_learning_reflect_{self._state.heartbeat_count}",
                        daemon=True,
                    )

                return str(model_reply or ""), prepared
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._state.last_model_error = str(exc)
                await self._save_runtime_context(recoverable_on_shared_conflict=True)
                if heartbeat_claim is not None and bridge is not None:
                    try:
                        await bridge.mark_heartbeat_operation_failed(
                            consciousness_instance_id="chat_global",
                            sequence=self._state.heartbeat_count,
                            claim_epoch=heartbeat_claim.claim_epoch,
                            retryable=True,
                        )
                    except Exception as mark_exc:  # noqa: BLE001
                        logger.warning(f"多写者 heartbeat 失败标记异常: {mark_exc}")
                raise

    async def _advance_memory_projection(self, report: Any) -> None:
        """Record this node's memory-index projection frontier (strict +1)."""
        bridge = self._multi_writer_bridge
        if bridge is None or not getattr(bridge, "enabled", False):
            return
        try:
            backlog = 0
            try:
                pending_jobs = await self._memory_service.list_index_jobs(
                    status="pending"
                )
                backlog = len(pending_jobs)
            except Exception:  # noqa: BLE001 - backlog is advisory only
                backlog = 0
            source_digest = _stable_text_digest(
                "memory_index"
                f":claimed={int(getattr(report, 'claimed', 0) or 0)}"
                f":completed={sorted(getattr(report, 'completed', ()) or ())}"
                f":failed={sorted(getattr(report, 'failed', ()) or ())}"
                f":stale={sorted(getattr(report, 'stale', ()) or ())}"
            )
            # config_digest 必须来自稳定的索引状态（memory_index_state），
            # 而不是当前批次的 report：空批次（claimed=0 / 全 stale）时
            # report.model_name/dimension 为空/0，若用它算 digest 会在批次
            # 之间波动，导致投影推进被 ProjectionProgressConflict 永久拒绝。
            model_name = str(getattr(report, "model_name", "") or "")
            dimension = int(getattr(report, "dimension", 0) or 0)
            if not model_name or dimension <= 0:
                try:
                    index_state = await self._memory_service.read_chunk_index_state()
                except Exception:  # noqa: BLE001 - fall back to batch report
                    index_state = None
                if index_state is not None:
                    model_name = str(index_state.model_name or model_name)
                    dimension = int(index_state.dimension or dimension)
            config_digest = _stable_text_digest(
                "memory_index"
                f":model={model_name}"
                f":dim={dimension}"
                ":workspace=workspace-v1"
            )
            advanced = await bridge.advance_projection(
                projection_name="memory_index",
                expected_frontier=self._projection_frontier,
                next_frontier=self._projection_frontier + 1,
                source_digest=source_digest,
                config_digest=config_digest,
                backlog=backlog,
            )
            if advanced is not None:
                self._projection_frontier += 1
            else:
                logger.warning(
                    "多写者记忆索引投影推进被拒绝（frontier 冲突或配置变化），"
                    f"节点进度保持在 {self._projection_frontier}"
                )
        except Exception as exc:  # noqa: BLE001 - projection is best-effort
            logger.warning(f"多写者记忆索引投影推进异常: {exc}")

    async def _memory_index_loop(self) -> None:
        """独立运行 chunk 向量索引，每轮最多处理一批。"""
        options = self._memory_index_options()
        interval = int(options["interval_seconds"])
        batch_size = max(1, int(options["batch_size"]))
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

                continue_catchup = False
                try:
                    report = await self._memory_service.run_index_worker(
                        limit=batch_size,
                        retry_failed=retry_failed_once,
                        reclaim_after=float(options["reclaim_after_seconds"]),
                    )
                    retry_failed_once = False
                    _has_work = (
                        report.claimed
                        or report.completed
                        or report.failed
                        or report.stale
                    )
                    _log = logger.info if _has_work else logger.debug
                    _log(
                        "life_engine 记忆索引批次完成: "
                        f"claimed={report.claimed} completed={len(report.completed)} "
                        f"failed={len(report.failed)} stale={len(report.stale)}"
                    )
                    # 多写者：推进本节点记忆索引投影进度（frontier 严格 +1）。
                    # 每个节点的 Chroma/FTS 是本地投影，进度行由该节点独占推进。
                    await self._advance_memory_projection(report)
                    # A full, successful batch proves that the bounded outbox may
                    # still contain work. Continue after one cooperative yield
                    # instead of imposing the steady-state polling interval on
                    # every recovery batch. Any failure or incomplete claim
                    # restores normal backoff, so an unhealthy provider is never
                    # hammered; stale jobs count only after safe consumption.
                    completed_count = len(report.completed)
                    stale_count = len(report.stale)
                    continue_catchup = bool(
                        report.claimed >= batch_size
                        and not report.failed
                        and completed_count + stale_count >= report.claimed
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    retry_failed_once = False
                    logger.error(f"life_engine 记忆索引批次失败: {exc}", exc_info=True)

                if self._stop_event is not None and self._stop_event.is_set():
                    break
                if continue_catchup:
                    run_immediately = True
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        finally:
            logger.info("life_engine 记忆索引循环已停止")

    def _effective_heartbeat_interval(self) -> int:
        """Keep the core heartbeat independent from scene consciousnesses."""

        return max(1, int(self._cfg().settings.heartbeat_interval_seconds))

    async def _heartbeat_loop(self) -> None:
        """心跳循环。"""
        interval = self._effective_heartbeat_interval()
        should_log_heartbeat = bool(self._cfg().settings.log_heartbeat)
        transient_model_failures = 0

        try:
            while self._state.running:
                interval = self._effective_heartbeat_interval()
                if self._stop_event is not None:
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=interval
                        )
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
                        logger.info(
                            f"life_engine heartbeat tick: 睡眠中（{sleep_window_desc}），跳过"
                        )
                    self._print_heartbeat_skip_panel(
                        reason=f"睡眠中（{sleep_window_desc}）",
                    )
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
                    checkpoint_interval = (
                        self._state.self_pause_checkpoint_minutes or 30
                    )
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
                                next_checkpoint = (
                                    int(elapsed_minutes // checkpoint_interval)
                                    * checkpoint_interval
                                )
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
                        remaining_text = (
                            f"{remaining_minutes}min"
                            if remaining_minutes is not None
                            else ""
                        )
                        self._print_heartbeat_skip_panel(
                            reason=pause_reason or "主动休息",
                            remaining=remaining_text,
                            until=paused_until,
                        )
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
                    transient_model_failures = 0

                except Exception as exc:  # noqa: BLE001
                    self._state.last_model_error = str(exc)
                    if _is_transient_mysql_disconnect(exc):
                        # FRP 隧道抖动导致 MySQL 2013：事件已在 publish_legacy_events
                        # 写入账本，心跳不推进游标下轮自愈。前 8 次降级为
                        # warning/debug（不刷 traceback），第 9 次才 ERROR。
                        transient_model_failures += 1
                        summary = _transient_error_summary(exc)
                        message = (
                            "life_engine 心跳模型瞬时数据库断连（待处理工作已保留，"
                            f"下轮重试）: failure_count={transient_model_failures}, "
                            f"error={summary}"
                        )
                        if (
                            transient_model_failures
                            == _HEARTBEAT_TRANSIENT_ERROR_ESCALATION_COUNT
                        ):
                            log_error(
                                "heartbeat_model_failed",
                                str(exc),
                                heartbeat_count=self._state.heartbeat_count,
                                heartbeat_at=self._state.last_heartbeat_at,
                                model_task_name=self._cfg().model.task_name,
                            )
                            logger.error(
                                f"{message}\n{traceback.format_exc()}"
                            )
                        elif transient_model_failures == 1:
                            logger.warning(message)
                        else:
                            logger.debug(message)
                    else:
                        log_error(
                            "heartbeat_model_failed",
                            str(exc),
                            heartbeat_count=self._state.heartbeat_count,
                            heartbeat_at=self._state.last_heartbeat_at,
                            model_task_name=self._cfg().model.task_name,
                        )
                        logger.error(
                            f"life_engine 心跳模型异常: {exc}\n{traceback.format_exc()}"
                        )

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
            return {
                "success": False,
                "error": f"当前在睡眠时段（{sleep_window_desc}），心跳已暂停",
            }

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
