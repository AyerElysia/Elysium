"""Recoverable Experience-to-Witness coordination for Life Engine memory.

The coordinator has four deliberately separate stages:

* raw Life Events are appended to the immutable Experience occurrence ledger;
* deterministic immutable occurrence windows are offered to the witness;
* the witness decision and content-free delivery outboxes are committed;
* World cursor delivery and Markdown projection are replayed independently.

No downstream LLM, World, Presence, or filesystem failure can roll back a
successfully appended Experience occurrence.  Witness testimony remains a
subjective first-person record linked to immutable evidence, never an
objective-truth override.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import os
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import DBAPIError

from src.app.plugin_system.api.llm_api import get_model_set_by_task
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import ROLE, LLMPayload, LLMRequest, Text
from src.kernel.llm.exceptions import (
    LLMAPIError,
    is_transient_llm_error,
)
from src.kernel.storage import CursorConflict

from ..memory.experience import (
    EpistemicKind,
    ExperienceOccurrenceCursor,
    ExperienceOccurrenceRef,
    ExperienceRecord,
    WitnessMemory,
)
from ..memory.witness_pipeline import (
    WitnessDecision,
    WitnessDeliveryJob,
    WitnessWindow,
    witness_window_source_digest,
)
from ..storage import (
    SingletonWriterClaimConflict,
    SingletonWriterClaimLost,
)
from ..storage.contracts import StorageRuntimeError
from ..storage.memory.contracts import (
    StableLedgerCursor,
    StableLedgerPage,
    WitnessReconciliationState,
)
from .consciousness import ConsciousnessInstance
from .event_bus import LifeEvent, RawEventGapError
from .perception_gateway import (
    PerceptionCommitCheckpoint,
    PerceptionDeliveryReceipt,
)
from .presence_store import PresenceRevisionConflict
from .world_state import PerceptionFilter

# 双路径加载身份兼容。plugin_manager 以顶层包身份（life_engine.*）加载插件，
# 而 main.py/src 可能先以 plugins 前缀导入同一子模块 → 同一源码出现两份类
# （__module__ 不同）。捕获方必须同时匹配两个身份，否则 except/isinstance
# 漏捕并把常态并发竞争刷成 ERROR（2026-08-13 线上两次：ClaimConflict 漏捕、
# PresenceRevisionConflict 漏捕均为此因）。
try:
    from plugins.life_engine.service.presence_store import (  # type: ignore[import-not-found]
        PresenceRevisionConflict as _PluginsPresenceRevisionConflict,
    )
except ImportError:  # pragma: no cover - 单一路径环境不会触发
    _PluginsPresenceRevisionConflict = PresenceRevisionConflict
try:
    from plugins.life_engine.storage import (  # type: ignore[import-not-found]
        SingletonWriterClaimConflict as _PluginsWriterClaimConflict,
    )
    from plugins.life_engine.storage import (
        SingletonWriterClaimLost as _PluginsWriterClaimLost,
    )
except ImportError:  # pragma: no cover
    _PluginsWriterClaimConflict = SingletonWriterClaimConflict
    _PluginsWriterClaimLost = SingletonWriterClaimLost

_PRESENCE_CONFLICT_TYPES = (
    PresenceRevisionConflict,
    _PluginsPresenceRevisionConflict,
)
_WRITER_CLAIM_TYPES = (
    SingletonWriterClaimConflict,
    _PluginsWriterClaimConflict,
    SingletonWriterClaimLost,
    _PluginsWriterClaimLost,
)

if TYPE_CHECKING:
    from .core import LifeEngineService

logger = get_logger("life_engine.memory_witness")
MEMORY_WITNESS_INSTANCE_ID = "memory_witness"
MEMORY_EXPERIENCE_CONSUMER_ID = "memory_experience_ingest:v1"
_NO_WITNESS = "<no_witness>"
_WINDOW_PLANNER_VERSION = "memory-witness-window-v1"
_DECISION_VERSION = "memory-witness-decision-v1"
_DELIVERY_WORKER_ID = "memory-witness-delivery:v1"
_WINDOW_LOOKAHEAD_LIMIT = 1000
_CURSOR_RECOVERY_LIMIT = 64
_SUBJECT_CONTEXT_MAX_BYTES = 24 * 1024
_RECENT_SUBCONSCIOUS_CONTEXT_MAX_BYTES = 8 * 1024
_RECENT_SUBCONSCIOUS_PREFIX = """<recent_subconscious_context>
用途：同一主体的近期连续性背景；不是本次 Witness 的 Experience 证据。
边界：不得仅依据本段声称本次经历发生过任何内容；与目标 Experience 无关时可以完全不提及。
"""
_RECENT_SUBCONSCIOUS_SUFFIX = "\n</recent_subconscious_context>"
_RECONCILIATION_SCAN_LIMIT = 1000
_FILESYSTEM_RECONCILIATION_SCAN_NAME = "projection_filesystem:v1"
_FILESYSTEM_RECONCILIATION_MAX_FILES = 100_000
_FILESYSTEM_RECONCILIATION_MAX_BYTES = 1024 * 1024 * 1024
_FILESYSTEM_HASH_CHUNK_BYTES = 64 * 1024
_AUTHOR_CLAIM_NAMESPACE = "life_engine.memory_witness"
_UNMANAGED_SINGLETON_WRITER_PREFIX = (
    "singleton writer is not managed locally:"
)
_TRANSIENT_ERROR_ESCALATION_COUNT = 3
# 双实例共享同一 witness presence 行时，PresenceRevisionConflict 是常态合法竞争；
# 前 8 次只记 debug/warning，第 9 次才升级 ERROR，避免常态竞争刷 ERROR。
_CONCURRENCY_ERROR_ESCALATION_COUNT = 9
_MYSQL_LOST_CONNECTION_ERROR_CODE = 2013
_MYSQL_LOCK_WAIT_TIMEOUT_ERROR_CODE = 1205
_LEGACY_MIGRATION_GUARD = Lock()
_LEGACY_MIGRATION_COMPLETED: set[str] = set()
_LEGACY_MIGRATION_IN_PROGRESS: set[str] = set()

_SELF_PRESENCE_SIDE_EFFECT_EVENT_TYPES = frozenset(
    {
        "consciousness.instance_imported",
        "consciousness.instance_imported_suspended",
        "consciousness.instance_lease_expired",
        "consciousness.instance_registered",
        "consciousness.instance_resumed",
        "consciousness.instance_seen",
        "consciousness.instance_suspended",
        "consciousness.instance_taken_over",
        "consciousness.instance_terminated",
    }
)


class MemoryExperienceRawLedgerGap(RuntimeError):
    """The independent raw-to-Experience consumer cannot prove continuity."""


class MemoryWitnessWindowTooLarge(RuntimeError):
    """The first complete occurrence position cannot fit the authoring budget."""


class MemoryWitnessDeliveryPayloadInvalid(RuntimeError):
    """A durable delivery payload cannot be replayed safely."""


class MemoryWitnessWorldReceiptUnavailable(RuntimeError):
    """A World outbox has no exact final-attempt delivery proof."""


class MemoryWitnessWorldCommitMismatch(RuntimeError):
    """World cursor commit returned a state other than the durable checkpoint."""


class MemoryWitnessProjectionRecordMissing(RuntimeError):
    """A projection outbox no longer resolves to its immutable witness."""


class MemoryWitnessCrossProcessClaimUnavailable(RuntimeError):
    """Selected storage cannot prove a unique cross-process witness author."""


class MemoryWitnessOccurrencePaginationUnavailable(RuntimeError):
    """The Port cannot prove that one occurrence position was read completely."""


class MemoryWitnessAuthoringEmptyResponse(RuntimeError):
    """An empty model response is not an explicit no-witness decision."""


class MemoryWitnessAuthoringProjectionUnavailable(RuntimeError):
    """Subject projection could not be produced for this authoring round
    (shared-projection contention). Retryable: the window stays pending and
    the authoring cursor must not advance."""


class MemoryWitnessProjectionFilesystemChanged(RuntimeError):
    """The projection source changed before one durable scan cycle completed."""


class MemoryWitnessProjectionFilesystemBoundExceeded(RuntimeError):
    """A projection source cannot be inventoried inside the declared hard bound."""


class MemoryWitnessProjectionFilesystemUnsafe(RuntimeError):
    """A projection source contains an unsafe indirection such as a symlink."""


# Every retained life event remains available as experience evidence. Exact
# Presence protocol events caused by the witness maintaining its own lease are
# fenced only from that same witness's authoring window: this breaks a causal
# feedback loop without classifying the event's semantic importance.
def _transient_error_summary(exc: BaseException) -> str:
    """Describe an upstream failure without dumping response bodies or traces."""

    details = [type(exc).__name__]
    if isinstance(exc, LLMAPIError):
        if exc.status_code is not None:
            details.append(f"status={exc.status_code}")
        if exc.error_code:
            details.append(f"code={exc.error_code}")
    elif isinstance(exc, DBAPIError):
        code = _dbapi_error_code(exc)
        if code is not None:
            details.append(f"code={code}")
    if len(details) == 1:
        return details[0]
    return f"{details[0]}({', '.join(details[1:])})"


def _dbapi_error_code(exc: DBAPIError) -> int | None:
    """Return a numeric DBAPI code without copying SQL or server text."""

    for value in getattr(exc.orig, "args", ()):
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdecimal():
            return int(value)
    return None


def _is_unmanaged_author_claim_error(exc: BaseException) -> bool:
    """Return whether the runtime explicitly rejected a detached local claim.

    A renewal can fail for many reasons while the runtime still manages the
    exact claim.  Only the runtime's structured error for a missing managed
    claim permits this coordinator to forget its snapshot and acquire again.
    """

    return isinstance(exc, StorageRuntimeError) and str(exc).startswith(
        _UNMANAGED_SINGLETON_WRITER_PREFIX
    )


def _is_transient_mysql_disconnect(exc: BaseException) -> bool:
    """Recognize the observed selected-MySQL connection-loss condition."""

    return isinstance(exc, DBAPIError) and (
        _dbapi_error_code(exc) == _MYSQL_LOST_CONNECTION_ERROR_CODE
    )


def _safe_log(
    level: str,
    message: str,
    *,
    exc_info: BaseException | None = None,
) -> None:
    """Emit one diagnostic without letting a logging failure kill the worker."""

    log_method = getattr(logger, level, None)
    if not callable(log_method):
        return
    try:
        if exc_info is None:
            log_method(message)
        else:
            log_method(message, exc_info=exc_info)
    except Exception:  # noqa: BLE001 - observability must not own worker life
        # The witness owns durable work, while the logger is an observability
        # projection.  A broken sink must not terminate the only consumer.
        return


@dataclass(frozen=True, slots=True)
class WitnessRunReport:
    synced_experiences: int = 0
    considered_events: int = 0
    suppressed_self_echo_events: int = 0
    written_witnesses: tuple[str, ...] = ()
    skipped_scopes: tuple[str, ...] = ()
    last_sequence: int = 0
    raw_ingest_cursor: int = 0
    occurrence_count: int = 0
    decisions_committed: int = 0
    deliveries_succeeded: int = 0
    deliveries_failed: int = 0
    projections_rebuilt: int = 0
    author_claim_conflict: bool = False


@dataclass(frozen=True, slots=True)
class _IngestReport:
    inserted_count: int = 0
    occurrence_count: int = 0
    raw_event_count: int = 0
    raw_cursor: int = 0
    batches: int = 0


@dataclass(frozen=True, slots=True)
class _AuthoringResult:
    text: str
    model_task_name: str
    model_request_id: str
    response_sha256: str
    response_bytes: int
    world_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _DeliveryReport:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class _ProjectionFilesystemEntry:
    path: Path
    identity: str
    descriptor_sha256: str


@dataclass(frozen=True, slots=True)
class _ReconciliationReport:
    rebuilt: int = 0
    missing: int = 0
    orphan: int = 0
    legacy_actionable: int = 0
    legacy_failed: int = 0
    filesystem_scan_truncated: bool = False
    ledger_scan_complete: bool = True
    ledger_scan_blocker: str = ""
    legacy_scan_state: WitnessReconciliationState | None = None
    projection_scan_state: WitnessReconciliationState | None = None
    filesystem_scan_state: WitnessReconciliationState | None = None
    filesystem_cursor_scope: str = "durable_store"
    filesystem_scan_blocker: str = ""


def _exact_perception_receipt(
    response: Any,
    perception: Any,
) -> PerceptionDeliveryReceipt | None:
    """Map the final successful LLM attempt receipt to one World delivery."""

    delivery_id = str(perception.delivery_id)
    projection_sha256 = str(perception.projection_sha256)
    delivered_bytes = int(perception.delivered_bytes)
    lookup = getattr(response, "effective_context_receipt", None)
    effective = lookup(delivery_id) if callable(lookup) else None
    if effective is None or not bool(getattr(effective, "exact_present", False)):
        return None
    effective_delivery_id = str(getattr(effective, "delivery_id", "") or "")
    part_kind = str(getattr(effective, "part_kind", "") or "")
    expected_bytes = getattr(effective, "expected_utf8_bytes", None)
    effective_bytes = getattr(effective, "effective_utf8_bytes", None)
    expected_sha256 = getattr(effective, "expected_sha256", None)
    effective_sha256 = getattr(effective, "effective_sha256", None)
    if (
        effective_delivery_id != delivery_id
        or part_kind != "text"
        or not isinstance(expected_bytes, int)
        or expected_bytes != delivered_bytes
        or effective_bytes != delivered_bytes
        or not isinstance(expected_sha256, str)
        or expected_sha256 != projection_sha256
        or effective_sha256 != projection_sha256
    ):
        return None
    return PerceptionDeliveryReceipt(
        delivery_id=delivery_id,
        projection_sha256=projection_sha256,
        delivered_bytes=delivered_bytes,
        exact=True,
        transport_request_id=str(getattr(response, "request_record_id", "") or ""),
    )


class MemoryWitnessCoordinator:
    """Coordinate a periodic consciousness instance over immutable evidence."""

    def __init__(self, service: LifeEngineService) -> None:
        self._service = service
        self._run_lock = asyncio.Lock()
        self._legacy_migration_complete = False
        self._last_run_at = ""
        self._last_success_at = ""
        self._last_error_type = ""
        self._last_ingest = _IngestReport()
        self._last_delivery = _DeliveryReport()
        self._last_reconciliation = _ReconciliationReport()
        self._last_subject_projection: dict[str, Any] = {}
        self._oversized_window: dict[str, Any] = {}
        self._occurrence_pagination_blocker: dict[str, Any] = {}
        self._last_occurrence_frontier: ExperienceOccurrenceCursor | None = None
        self._author_claim: Any | None = None
        self._author_claim_mode = "unchecked"
        self._legacy_projection_cursor = ""
        self._projection_job_cursor = ""

    @property
    def config(self) -> Any:
        return getattr(self._service._cfg(), "memory_witness", None)

    async def ensure_instance(self) -> ConsciousnessInstance:
        registry = self._service.consciousness_registry
        existing = registry.get(MEMORY_WITNESS_INSTANCE_ID)
        now = _now_iso()
        if existing is not None and existing.status != "terminated":
            await self._ensure_presence_liveness(existing, timestamp=now)
            return existing
        instance = ConsciousnessInstance(
            instance_id=MEMORY_WITNESS_INSTANCE_ID,
            kind="memory_witness",
            display_name="爱莉的记忆见证意识",
            status="active",
            created_at=now,
            last_active_at=now,
            perception_filter=PerceptionFilter.full(),
            metadata={
                "role": "first_person_experience_witness",
                "epistemic_boundary": "subjective_witness_not_objective_truth",
                "reads": "immutable_experience_ledger",
            },
        )
        try:
            await self._service.register_consciousness_instance(instance)
        except PresenceRevisionConflict:
            # A concurrent node registered the same witness between our local
            # snapshot and the durable commit.  Refresh and treat it as the
            # existing live instance instead of failing plugin startup.
            await self._refresh_presence_snapshot_safely()
            live = self._service.consciousness_registry.get(MEMORY_WITNESS_INSTANCE_ID)
            if live is not None and live.status != "terminated":
                await self._ensure_presence_liveness(live, timestamp=now)
                return live
            logger.warning(
                "memory witness presence could not be registered under "
                "concurrent ownership; continuing with a local read-only "
                "instance handle"
            )
        return instance

    async def _ensure_presence_liveness(
        self,
        instance: ConsciousnessInstance,
        *,
        timestamp: str,
    ) -> None:
        """Boundedly renew the witness presence, degrading on contention.

        In a multi-writer deployment the resident Linux node keeps touching the
        shared ``memory_witness`` presence row, so an occasional Windows guest
        frequently races a ``PresenceRevisionConflict`` on its startup touch.
        That is transient ownership competition, not a storage failure: refresh
        the snapshot and retry a bounded number of times, then degrade to a
        local read-only handle instead of failing plugin startup.
        """

        for attempt in range(3):
            try:
                if instance.status == "suspended":
                    await self._service.resume_consciousness_instance(
                        MEMORY_WITNESS_INSTANCE_ID,
                        timestamp=timestamp,
                    )
                await self._service.touch_consciousness_instance(
                    MEMORY_WITNESS_INSTANCE_ID,
                    timestamp=timestamp,
                )
                return
            except PresenceRevisionConflict:
                if attempt >= 2:
                    logger.warning(
                        "memory witness presence remains contended after retries; "
                        "continuing with a local read-only instance handle: "
                        f"attempts={attempt + 1}"
                    )
                    return
                await self._refresh_presence_snapshot_safely()
                refreshed = self._service.consciousness_registry.get(
                    MEMORY_WITNESS_INSTANCE_ID
                )
                if refreshed is None or refreshed.status == "terminated":
                    # The concurrent owner retired the instance; re-register.
                    return
                instance = refreshed
            except DBAPIError as db_exc:
                if _dbapi_error_code(db_exc) != _MYSQL_LOCK_WAIT_TIMEOUT_ERROR_CODE:
                    raise
                if attempt >= 2:
                    logger.warning(
                        "memory witness presence world-projection lock "
                        "contention after retries; continuing with a local "
                        "read-only instance handle: "
                        f"attempts={attempt + 1}"
                    )
                    return

    async def loop(self) -> None:
        cfg = self.config
        if cfg is None or not bool(getattr(cfg, "enabled", True)):
            return
        run_immediately = bool(getattr(cfg, "run_on_startup", True))
        interval = max(60, int(getattr(cfg, "interval_seconds", 1800)))
        retry_delay = max(
            10,
            min(interval, int(getattr(cfg, "retry_delay_seconds", 60))),
        )
        next_delay = 0 if run_immediately else interval
        transient_failures = 0
        concurrency_failures = 0
        while self._service._state.running:
            if next_delay > 0:
                stop_event = self._service._stop_event
                if stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=next_delay)
                        break
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(next_delay)
            if not self._service._state.running:
                break
            next_delay = interval
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - managed worker boundary
                await self._record_error_safely(exc)
                if isinstance(
                    exc,
                    (
                        *_PRESENCE_CONFLICT_TYPES,
                        CursorConflict,
                        MemoryWitnessProjectionFilesystemChanged,
                        *_WRITER_CLAIM_TYPES,
                    ),
                ):
                    transient_failures = 0
                    concurrency_failures += 1
                    next_delay = retry_delay
                    if isinstance(exc, _PRESENCE_CONFLICT_TYPES):
                        await self._refresh_presence_snapshot_safely()
                    elif isinstance(exc, SingletonWriterClaimLost):
                        self._author_claim = None
                    message = (
                        "记忆见证遇到可恢复并发冲突，待处理经历已保留: "
                        f"failure_count={concurrency_failures}, "
                        f"retry_in={next_delay}s, error={type(exc).__name__}"
                    )
                    if concurrency_failures == 1:
                        _safe_log("warning", message, exc_info=exc)
                    elif concurrency_failures == _CONCURRENCY_ERROR_ESCALATION_COUNT:
                        _safe_log("error", message, exc_info=exc)
                    else:
                        _safe_log("debug", message)
                    continue

                concurrency_failures = 0
                if not (
                    is_transient_llm_error(exc) or _is_transient_mysql_disconnect(exc)
                ):
                    transient_failures = 0
                    _safe_log("error", "记忆见证意识运行失败", exc_info=exc)
                    continue

                transient_failures += 1
                retry_after = getattr(exc, "retry_after", 0.0)
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    next_delay = max(retry_delay, math.ceil(retry_after))
                else:
                    next_delay = retry_delay
                summary = _transient_error_summary(exc)
                message = (
                    "记忆见证上游暂时不可用，待处理经历已保留: "
                    f"failure_count={transient_failures}, "
                    f"retry_in={next_delay}s, error={summary}"
                )
                if transient_failures == _TRANSIENT_ERROR_ESCALATION_COUNT:
                    _safe_log("error", message)
                elif transient_failures == 1:
                    _safe_log("warning", message)
                else:
                    _safe_log("debug", message)
            else:
                if transient_failures:
                    _safe_log(
                        "info",
                        f"记忆见证上游已恢复: previous_failures={transient_failures}",
                    )
                if concurrency_failures:
                    _safe_log(
                        "info",
                        "记忆见证并发冲突已恢复: "
                        f"previous_failures={concurrency_failures}",
                    )
                transient_failures = 0
                concurrency_failures = 0

    async def run_once(self) -> WitnessRunReport:
        async with self._run_lock:
            self._last_run_at = _now_iso()
            self._last_error_type = ""
            memory = self._service.memory_service
            cfg = self.config
            if memory is None or cfg is None:
                return WitnessRunReport()
            storage = memory._require_memory_storage()
            try:
                # Stage A owns a separate raw-ledger cursor and is deliberately
                # completed before Presence, LLM, World, or projection work.
                ingest = await self._ingest_raw_experiences(storage.experiences)
                self._last_ingest = ingest

                instance = await self.ensure_instance()
                try:
                    await self._migrate_legacy_diaries()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - independent legacy stage
                    self._last_error_type = type(exc).__name__
                    _safe_log(
                        "warning",
                        "旧日记迁移阶段暂不可用，Experience/Witness 主链继续: "
                        f"error={type(exc).__name__}",
                    )

                # Replay old outboxes before asking the model for new work.
                delivery = await self._run_delivery_worker(storage.witnesses)
                self._last_delivery = delivery
                reconciliation = await self._reconcile_projections(storage.witnesses)
                self._last_reconciliation = reconciliation

                try:
                    await self._ensure_authoring_claim()
                    window, state = await self._next_authoring_window(
                        instance,
                        storage.experiences,
                        storage.witnesses,
                    )
                    written: list[str] = []
                    skipped: list[str] = []
                    suppressed = 0
                    decisions_committed = 0
                    if window is not None:
                        witnessable = tuple(
                            item
                            for item in window.occurrences
                            if not self._is_self_presence_side_effect(
                                item.experience,
                                instance_id=instance.instance_id,
                            )
                        )
                        suppressed = len(window.occurrences) - len(witnessable)
                        decision, witness = await self._decide_window(
                            instance,
                            window,
                            witnessable,
                            storage.witnesses,
                        )
                        decisions_committed = 1
                        if witness is None:
                            skipped.append(window.stream_scope)
                        else:
                            written.append(witness.witness_id)
                        state = await self._advance_author_cursor(
                            storage.witnesses,
                            state,
                            window,
                            success_at=decision.decided_at,
                        )
                    else:
                        now = _now_iso()
                        state = (
                            await storage.witnesses.compare_and_advance_state(
                                instance.instance_id,
                                expected_sequence=int(
                                    state.get("last_sequence", 0) or 0
                                ),
                                expected_revision=int(
                                    state.get("revision", 0) or 0
                                ),
                                next_sequence=int(
                                    state.get("last_sequence", 0) or 0
                                ),
                                last_run_at=now,
                                last_success_at=now,
                                last_error="",
                            )
                        )
                except _WRITER_CLAIM_TYPES as claim_issue:
                    # Another live instance (the resident writer) holds the
                    # durable author claim.  This is the expected multi-writer
                    # guest role, not a failure: ingest/delivery/reconciliation
                    # already completed and must not be rolled back, but this
                    # node must not author witnesses without the singleton.
                    # ClaimLost additionally means our own lease was taken over
                    # or expired; the stale claim was already dropped so the
                    # next cycle re-acquires instead of renewing a dead lease.
                    # Surface it as a bounded skip instead of a fatal ERROR.
                    self._author_claim_mode = "not_authoring_writer"
                    self._last_error_type = type(claim_issue).__name__
                    return WitnessRunReport(
                        synced_experiences=ingest.inserted_count,
                        considered_events=ingest.raw_event_count,
                        raw_ingest_cursor=ingest.raw_cursor,
                        occurrence_count=ingest.occurrence_count,
                        deliveries_succeeded=delivery.succeeded,
                        deliveries_failed=delivery.failed,
                        projections_rebuilt=reconciliation.rebuilt,
                        author_claim_conflict=True,
                    )
                except MemoryWitnessAuthoringProjectionUnavailable:
                    # 共享投影竞争下本轮无法产出 subject projection：跳过
                    # authoring 但不推进游标（窗口保持 pending，下轮重试），
                    # 不当作 fatal ERROR 刷屏。
                    self._last_error_type = (
                        "MemoryWitnessAuthoringProjectionUnavailable"
                    )
                    return WitnessRunReport(
                        synced_experiences=ingest.inserted_count,
                        considered_events=ingest.raw_event_count,
                        raw_ingest_cursor=ingest.raw_cursor,
                        occurrence_count=ingest.occurrence_count,
                        deliveries_succeeded=delivery.succeeded,
                        deliveries_failed=delivery.failed,
                        projections_rebuilt=reconciliation.rebuilt,
                    )

                now = _now_iso()
                self._last_success_at = now
                if delivery.failed == 0 and reconciliation.legacy_failed == 0:
                    self._last_error_type = ""
                await self._touch_presence_after_commit(
                    instance.instance_id,
                    timestamp=now,
                )
                return WitnessRunReport(
                    synced_experiences=ingest.inserted_count,
                    considered_events=ingest.raw_event_count,
                    suppressed_self_echo_events=suppressed,
                    written_witnesses=tuple(written),
                    skipped_scopes=tuple(skipped),
                    last_sequence=int(state.get("last_sequence", 0) or 0),
                    raw_ingest_cursor=ingest.raw_cursor,
                    occurrence_count=ingest.occurrence_count,
                    decisions_committed=decisions_committed,
                    deliveries_succeeded=delivery.succeeded,
                    deliveries_failed=delivery.failed,
                    projections_rebuilt=reconciliation.rebuilt,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error_type = type(exc).__name__
                raise

    async def _ingest_raw_experiences(self, experience_store: Any) -> _IngestReport:
        """Append raw events independently, committing only covered occurrences."""

        cfg = self.config
        raw_store = self._service._get_life_event_store()
        get_offset = getattr(raw_store, "get_consumer_offset", None)
        commit_offset = getattr(raw_store, "commit_consumer_offset", None)
        if not callable(get_offset) or not callable(commit_offset):
            raise RuntimeError("MemoryExperienceRawConsumerCursorUnavailable")
        cursor = int(await get_offset(MEMORY_EXPERIENCE_CONSUMER_ID))
        batch_limit = max(1, int(getattr(cfg, "max_events_per_run", 500)))
        max_batches = max(
            1,
            int(getattr(cfg, "max_ingest_batches_per_run", 8)),
        )
        inserted_count = 0
        occurrence_count = 0
        raw_event_count = 0
        batches = 0
        for _ in range(max_batches):
            try:
                raw_events = await raw_store.read_since(cursor, limit=batch_limit)
            except RawEventGapError as gap:
                raise MemoryExperienceRawLedgerGap(
                    "MemoryExperienceRawLedgerGap: refusing to skip missing life "
                    f"history after={gap.requested_sequence} "
                    f"earliest={gap.earliest_available}"
                ) from gap
            if not raw_events:
                break
            positions = tuple(int(event.sequence) for event in raw_events)
            if positions != tuple(sorted(positions)) or positions[0] <= cursor:
                raise RuntimeError("MemoryExperienceRawBatchOrderingInvalid")
            candidates = tuple(self._to_experience(event) for event in raw_events)
            append_report = await experience_store.append(candidates)
            occurrences = tuple(append_report.occurrences)
            if len(occurrences) != len(raw_events):
                raise RuntimeError("MemoryExperienceOccurrenceCoverageMismatch")
            for event, occurrence in zip(raw_events, occurrences, strict=True):
                expected_occurrence_id = str(
                    event.occurrence_id or f"occ_position_{event.sequence}"
                )
                if occurrence.occurrence_id != expected_occurrence_id or int(
                    occurrence.ingest_position
                ) != int(event.sequence):
                    raise RuntimeError("MemoryExperienceOccurrenceIdentityMismatch")
            next_cursor = max(positions)
            committed = await commit_offset(
                MEMORY_EXPERIENCE_CONSUMER_ID,
                next_cursor,
                metadata={
                    "stage": "experience_ingest",
                    "schema": "v1",
                },
            )
            cursor = next_cursor if committed is None else int(committed)
            if cursor < next_cursor:
                raise RuntimeError("MemoryExperienceRawCursorCommitRegressed")
            inserted_count += int(append_report.inserted_count)
            occurrence_count += len(occurrences)
            raw_event_count += len(raw_events)
            batches += 1
            if len(raw_events) < batch_limit:
                break
        return _IngestReport(
            inserted_count=inserted_count,
            occurrence_count=occurrence_count,
            raw_event_count=raw_event_count,
            raw_cursor=cursor,
            batches=batches,
        )

    def _author_owner_instance_id(self, runtime: Any) -> str:
        """Build the durable claim owner id (authority + witness pid)."""

        authority = getattr(runtime, "authority_token", None)
        owner = str(getattr(authority, "owner_id", "runtime") or "runtime")
        return (
            f"{owner}:{MEMORY_WITNESS_INSTANCE_ID}:pid-{os.getpid()}"
        )[:255]

    async def _ensure_authoring_claim(self) -> None:
        """Prove one selected-backend author or state process-only isolation.

        The coordinator lock serializes tasks only inside this Python process.
        A selected shared runtime therefore must supply its durable singleton
        writer claim; absence or loss fails closed before any LLM decision.  The
        legacy disabled/local mode has no cross-process claim registry and is
        reported honestly as process-only rather than as cross-process safe.
        """

        runtime = getattr(self._service, "storage_runtime", None)
        if runtime is None or not bool(getattr(runtime, "enabled", False)):
            self._author_claim_mode = "process_lock_only"
            return
        validate_writer = getattr(runtime, "validate_writer", None)
        acquire = getattr(runtime, "acquire_singleton_writer", None)
        renew = getattr(runtime, "renew_singleton_writer", None)
        if not all(callable(item) for item in (validate_writer, acquire, renew)):
            self._author_claim_mode = "selected_runtime_claim_unavailable"
            raise MemoryWitnessCrossProcessClaimUnavailable(
                "MemoryWitnessCrossProcessClaimUnavailable"
            )
        cfg = self.config
        interval = max(60, int(getattr(cfg, "interval_seconds", 300)))
        timeout = max(10, int(float(getattr(cfg, "timeout_seconds", 600.0))))
        lease_seconds = max(120, min(3600, max(interval * 3, timeout * 2)))
        try:
            await validate_writer()
            if self._author_claim is None:
                self._author_claim = await acquire(
                    namespace=_AUTHOR_CLAIM_NAMESPACE,
                    state_key=MEMORY_WITNESS_INSTANCE_ID,
                    owner_instance_id=self._author_owner_instance_id(runtime),
                    lease_seconds=lease_seconds,
                )
            else:
                try:
                    self._author_claim = await renew(
                        self._author_claim,
                        lease_seconds=lease_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as renew_exc:
                    claim_was_lost = isinstance(
                        renew_exc,
                        (SingletonWriterClaimLost, _PluginsWriterClaimLost),
                    )
                    if not claim_was_lost and not _is_unmanaged_author_claim_error(
                        renew_exc
                    ):
                        # A DB/network failure or any other unclassified renewal
                        # error does not prove lease loss.  Keep the exact local
                        # snapshot so the managed loop can retry it without
                        # colliding with the runtime's still-owned claim entry.
                        raise
                    # An explicit claim-loss or runtime detachment proves this
                    # snapshot is stale. Re-acquire in the same authoring round;
                    # a competing live writer will surface the normal conflict
                    # path and no witness cursor will advance.
                    self._author_claim = None
                    self._author_claim_mode = "selected_runtime_claim_failed"
                    self._author_claim = await acquire(
                        namespace=_AUTHOR_CLAIM_NAMESPACE,
                        state_key=MEMORY_WITNESS_INSTANCE_ID,
                        owner_instance_id=self._author_owner_instance_id(runtime),
                        lease_seconds=lease_seconds,
                    )
        except asyncio.CancelledError:
            raise
        except SingletonWriterClaimLost:
            # The lease we held was taken over or expired while renewing.
            # Drop the stale local claim so the next attempt re-acquires
            # instead of renewing a dead lease forever; the caller decides
            # whether to skip authoring (guest) or retry (writer).
            self._author_claim = None
            self._author_claim_mode = "selected_runtime_claim_failed"
            raise
        except _PluginsWriterClaimLost:
            # plugins 前缀身份的 SingletonWriterClaimLost（双路径加载的另一份
            # 类）；与同身份分支完全同语义，仅类身份不同。
            self._author_claim = None
            self._author_claim_mode = "selected_runtime_claim_failed"
            raise
        except Exception:
            self._author_claim_mode = "selected_runtime_claim_failed"
            raise
        self._author_claim_mode = "durable_singleton_claim"

    async def _next_authoring_window(
        self,
        instance: ConsciousnessInstance,
        experience_store: Any,
        witness_store: Any,
    ) -> tuple[WitnessWindow | None, dict[str, Any]]:
        """Recover decided windows, then return one durable pending window."""

        for _ in range(_CURSOR_RECOVERY_LIMIT):
            state = await witness_store.get_state(instance.instance_id)
            cursor = int(state.get("last_sequence", 0) or 0)
            occurrences = await self._list_plannable_occurrences(
                instance.instance_id,
                experience_store,
                cursor,
            )
            if not occurrences:
                return None, state
            first = occurrences[0]
            window_id = self._window_id(instance.instance_id, first)
            window = await witness_store.get_window(window_id)
            if window is None:
                planned = self._plan_window(instance.instance_id, occurrences)
                self._occurrence_pagination_blocker = {}
                window = await witness_store.append_window(planned)
            if window.start_position <= cursor:
                raise RuntimeError("MemoryWitnessWindowCursorOverlap")
            decision = await witness_store.get_decision(
                self._decision_id(window.window_id)
            )
            if decision is None:
                return window, state
            await self._advance_author_cursor(
                witness_store,
                state,
                window,
                success_at=decision.decided_at,
            )
        raise RuntimeError("MemoryWitnessCursorRecoveryLimitExceeded")

    async def _list_plannable_occurrences(
        self,
        instance_id: str,
        experience_store: Any,
        cursor: int,
    ) -> tuple[ExperienceOccurrenceRef, ...]:
        """Read enough complete positions through one immutable page frontier."""

        page_reader = getattr(experience_store, "list_occurrence_page", None)
        if not callable(page_reader):
            occurrences = tuple(
                await experience_store.list_occurrences_after(
                    cursor,
                    _WINDOW_LOOKAHEAD_LIMIT,
                )
            )
            if len(occurrences) < _WINDOW_LOOKAHEAD_LIMIT:
                self._occurrence_pagination_blocker = {}
                self._last_occurrence_frontier = None
                return occurrences
            tail_position = int(occurrences[-1].ingest_position)
            if (
                occurrences
                and int(occurrences[0].ingest_position) == tail_position
            ):
                self._occurrence_pagination_blocker = {
                    "status": "blocked",
                    "author_cursor": cursor,
                    "returned_occurrences": len(occurrences),
                    "lookahead_limit": _WINDOW_LOOKAHEAD_LIMIT,
                    "first_position": tail_position,
                    "last_position": tail_position,
                    "cursor_advanced": False,
                    "reason": "ExperienceOccurrenceStoreNoCompositePagination",
                }
                raise MemoryWitnessOccurrencePaginationUnavailable(
                    "MemoryWitnessOccurrencePaginationUnavailable:"
                    f"cursor={cursor}:limit={_WINDOW_LOOKAHEAD_LIMIT}:"
                    f"last_position={tail_position}"
                )
            planned = self._plan_window(instance_id, occurrences)
            if planned.end_position < tail_position:
                self._occurrence_pagination_blocker = {}
                self._last_occurrence_frontier = None
                return occurrences
            self._occurrence_pagination_blocker = {
                "status": "blocked",
                "author_cursor": cursor,
                "returned_occurrences": len(occurrences),
                "lookahead_limit": _WINDOW_LOOKAHEAD_LIMIT,
                "first_position": int(occurrences[0].ingest_position),
                "last_position": tail_position,
                "cursor_advanced": False,
                "reason": "ExperienceOccurrenceStoreNoCompositePagination",
            }
            raise MemoryWitnessOccurrencePaginationUnavailable(
                "MemoryWitnessOccurrencePaginationUnavailable:"
                f"cursor={cursor}:limit={_WINDOW_LOOKAHEAD_LIMIT}:"
                f"last_position={tail_position}"
            )

        count_limit = max(
            1,
            int(getattr(self.config, "max_witness_events_per_run", 40)),
        )
        page_limit = min(_WINDOW_LOOKAHEAD_LIMIT, count_limit + 1)
        page = await page_reader(position_after=cursor, limit=page_limit)
        frontier = page.frontier
        self._last_occurrence_frontier = frontier
        occurrences = list(page.items)
        while occurrences:
            planned = self._plan_window(instance_id, occurrences)
            if not page.has_more or (
                planned.end_position < int(occurrences[-1].ingest_position)
            ):
                self._occurrence_pagination_blocker = {}
                return tuple(occurrences)
            previous_cursor = page.next_cursor
            if previous_cursor is None or frontier is None:
                break
            page = await page_reader(
                position_after=cursor,
                after=previous_cursor,
                through=frontier,
                limit=page_limit,
            )
            if page.frontier != frontier or (
                page.next_cursor is not None
                and page.next_cursor <= previous_cursor
            ):
                break
            occurrences.extend(page.items)
        if not occurrences and not page.has_more:
            self._occurrence_pagination_blocker = {}
            return ()
        self._occurrence_pagination_blocker = {
            "status": "blocked",
            "author_cursor": cursor,
            "returned_occurrences": len(occurrences),
            "lookahead_limit": page_limit,
            "cursor_advanced": False,
            "reason": "ExperienceOccurrenceCompositePageDidNotAdvance",
        }
        raise MemoryWitnessOccurrencePaginationUnavailable(
            "MemoryWitnessOccurrenceCompositePageDidNotAdvance"
        )

    def _plan_window(
        self,
        instance_id: str,
        occurrences: Sequence[ExperienceOccurrenceRef],
    ) -> WitnessWindow:
        """Select a complete-position prefix under count and UTF-8 budgets."""

        if not occurrences:
            raise ValueError("MemoryWitnessOccurrencesRequired")
        cfg = self.config
        count_limit = max(
            1,
            int(getattr(cfg, "max_witness_events_per_run", 40)),
        )
        byte_limit = max(
            1,
            int(getattr(cfg, "max_witness_context_bytes", 64 * 1024)),
        )
        selected: list[ExperienceOccurrenceRef] = []
        delivered_bytes = 0
        index = 0
        while index < len(occurrences):
            position = int(occurrences[index].ingest_position)
            group: list[ExperienceOccurrenceRef] = []
            while (
                index < len(occurrences)
                and int(occurrences[index].ingest_position) == position
            ):
                group.append(occurrences[index])
                index += 1
            rendered_group = tuple(self._format_occurrence(item) for item in group)
            group_bytes = sum(
                len(rendered.encode("utf-8")) for rendered in rendered_group
            )
            separator_bytes = 2 * max(0, len(group) - 1)
            if selected:
                separator_bytes += 2
            next_count = len(selected) + len(group)
            next_bytes = delivered_bytes + group_bytes + separator_bytes
            if next_count > count_limit or next_bytes > byte_limit:
                if not selected:
                    complete_group_bytes = group_bytes + 2 * max(0, len(group) - 1)
                    self._oversized_window = {
                        "status": "blocked",
                        "position": position,
                        "occurrence_count": len(group),
                        "utf8_bytes": complete_group_bytes,
                        "count_limit": count_limit,
                        "byte_limit": byte_limit,
                        "content_truncated": False,
                        "cursor_advanced": False,
                        "remediation": (
                            "increase a hard window budget or introduce an explicit "
                            "authority-approved oversized occurrence policy"
                        ),
                    }
                    raise MemoryWitnessWindowTooLarge(
                        "MemoryWitnessOccurrenceWindowTooLarge:"
                        f"position={position}:occurrences={len(group)}:"
                        f"utf8_bytes={complete_group_bytes}:"
                        f"count_limit={count_limit}:byte_limit={byte_limit}"
                    )
                break
            selected.extend(group)
            delivered_bytes = next_bytes
            if len(selected) >= count_limit:
                break
        if not selected:
            raise RuntimeError("MemoryWitnessWindowPlannerProducedEmptyWindow")
        self._oversized_window = {}
        stream_scope = self._window_stream_scope(selected)
        first = selected[0]
        return WitnessWindow(
            window_id=self._window_id(instance_id, first),
            consciousness_instance_id=instance_id,
            stream_scope=stream_scope,
            start_position=int(first.ingest_position),
            end_position=int(selected[-1].ingest_position),
            occurrences=tuple(selected),
            created_at=first.recorded_at,
            planner_version=_WINDOW_PLANNER_VERSION,
            source_digest=witness_window_source_digest(selected),
            metadata={
                "selection": "contiguous_occurrence_prefix",
                "occurrence_count": len(selected),
                "delivered_utf8_bytes": delivered_bytes,
                "count_limit": count_limit,
                "byte_limit": byte_limit,
            },
        )

    async def _decide_window(
        self,
        instance: ConsciousnessInstance,
        window: WitnessWindow,
        witnessable: Sequence[ExperienceOccurrenceRef],
        witness_store: Any,
    ) -> tuple[WitnessDecision, WitnessMemory | None]:
        """Author or recover one decision, then atomically create outboxes."""

        projection_path = self._projection_path_for_window(window)
        existing = await witness_store.get_by_projection_path(projection_path)
        if existing is not None:
            return await self._recover_witness_decision(
                instance,
                window,
                existing,
                witness_store,
            )

        if not witnessable:
            decided_at = _now_iso()
            decision = WitnessDecision(
                decision_id=self._decision_id(window.window_id),
                window_id=window.window_id,
                consciousness_instance_id=instance.instance_id,
                decision_kind="no_witness",
                decided_at=decided_at,
                response_sha256=hashlib.sha256(_NO_WITNESS.encode()).hexdigest(),
                metadata={
                    "decision_version": _DECISION_VERSION,
                    "reason": "self_presence_side_effect_window",
                    "source_digest": window.source_digest,
                    "world_delivery_order": "not_prepared",
                },
            )
            persisted = await witness_store.append_decision(
                decision,
                delivery_payloads={},
            )
            return persisted, None

        authored = await self._author_witness(instance, witnessable)
        if not authored.text:
            decision = WitnessDecision(
                decision_id=self._decision_id(window.window_id),
                window_id=window.window_id,
                consciousness_instance_id=instance.instance_id,
                decision_kind="no_witness",
                decided_at=_now_iso(),
                model_task_name=authored.model_task_name,
                model_request_id=authored.model_request_id,
                response_sha256=authored.response_sha256,
                metadata={
                    "decision_version": _DECISION_VERSION,
                    "source_digest": window.source_digest,
                    "response_utf8_bytes": authored.response_bytes,
                    "world_delivery_order": "decision_outbox_before_commit",
                },
            )
            persisted = await witness_store.append_decision(
                decision,
                delivery_payloads={"world": authored.world_payload},
            )
            return persisted, None

        witness_id = (
            "wit_"
            + hashlib.sha256(
                f"{_DECISION_VERSION}:{window.window_id}".encode("utf-8")
            ).hexdigest()
        )
        source_records = tuple(item.experience for item in witnessable)
        source_ids = tuple(
            dict.fromkeys(item.canonical_event_id for item in witnessable)
        )
        recorded_at = _now_iso()
        witness = await witness_store.append(
            content=authored.text,
            consciousness_instance_id=instance.instance_id,
            perspective_subject_id="elysia",
            epistemic_kind=EpistemicKind.SUBJECTIVE_WITNESS.value,
            source_kind="experience_window",
            stream_scope=window.stream_scope,
            visibility="private",
            valid_from=source_records[0].occurred_at,
            valid_to=source_records[-1].occurred_at,
            source_event_ids=source_ids,
            source_sequence_start=int(witnessable[0].ingest_position),
            source_sequence_end=int(witnessable[-1].ingest_position),
            model_task_name=authored.model_task_name,
            projection_path=projection_path,
            witness_id=witness_id,
            recorded_at=recorded_at,
            metadata={
                "author_kind": "consciousness_instance",
                "factual_anchor": "memory_experience_occurrences",
                "subjective": True,
                "pipeline_version": _DECISION_VERSION,
                "witness_window_id": window.window_id,
                "window_source_digest": window.source_digest,
                "window_start_position": window.start_position,
                "window_end_position": window.end_position,
                "source_occurrence_ids": [item.occurrence_id for item in witnessable],
                "model_request_id": authored.model_request_id,
                "response_sha256": authored.response_sha256,
                "response_utf8_bytes": authored.response_bytes,
                "world_delivery": authored.world_payload,
            },
        )
        decision = WitnessDecision(
            decision_id=self._decision_id(window.window_id),
            window_id=window.window_id,
            consciousness_instance_id=instance.instance_id,
            decision_kind="witness",
            witness_id=witness.witness_id,
            model_task_name=authored.model_task_name,
            model_request_id=authored.model_request_id,
            response_sha256=authored.response_sha256,
            decided_at=recorded_at,
            metadata={
                "decision_version": _DECISION_VERSION,
                "source_digest": window.source_digest,
                "response_utf8_bytes": authored.response_bytes,
                "world_delivery_order": "decision_outbox_before_commit",
            },
        )
        persisted = await witness_store.append_decision(
            decision,
            delivery_payloads={
                "world": authored.world_payload,
                "projection": {
                    "witness_id": witness.witness_id,
                    "projection_path": witness.projection_path,
                    "window_id": window.window_id,
                },
            },
        )
        return persisted, witness

    async def _recover_witness_decision(
        self,
        instance: ConsciousnessInstance,
        window: WitnessWindow,
        witness: WitnessMemory,
        witness_store: Any,
    ) -> tuple[WitnessDecision, WitnessMemory]:
        """Recover the narrow witness-append/decision crash window."""

        metadata = dict(witness.metadata or {})
        if (
            witness.consciousness_instance_id != instance.instance_id
            or metadata.get("witness_window_id") != window.window_id
            or metadata.get("window_source_digest") != window.source_digest
        ):
            raise RuntimeError("MemoryWitnessProjectionIdentityConflict")
        world_payload = metadata.get("world_delivery")
        if not isinstance(world_payload, dict):
            world_payload = {
                "proof_state": "unavailable_after_legacy_crash",
                "window_id": window.window_id,
            }
        response_sha256 = str(metadata.get("response_sha256") or "")
        if not response_sha256:
            response_sha256 = hashlib.sha256(
                witness.content.encode("utf-8")
            ).hexdigest()
        decision = WitnessDecision(
            decision_id=self._decision_id(window.window_id),
            window_id=window.window_id,
            consciousness_instance_id=instance.instance_id,
            decision_kind="witness",
            witness_id=witness.witness_id,
            model_task_name=witness.model_task_name,
            model_request_id=str(metadata.get("model_request_id") or ""),
            response_sha256=response_sha256,
            decided_at=witness.recorded_at,
            metadata={
                "decision_version": _DECISION_VERSION,
                "source_digest": window.source_digest,
                "recovered_after_witness_append": True,
                "world_delivery_order": "decision_outbox_before_commit",
            },
        )
        persisted = await witness_store.append_decision(
            decision,
            delivery_payloads={
                "world": world_payload,
                "projection": {
                    "witness_id": witness.witness_id,
                    "projection_path": witness.projection_path,
                    "window_id": window.window_id,
                },
            },
        )
        return persisted, witness

    async def _advance_author_cursor(
        self,
        witness_store: Any,
        state: dict[str, Any],
        window: WitnessWindow,
        *,
        success_at: str,
    ) -> dict[str, Any]:
        current = int(state.get("last_sequence", 0) or 0)
        if current >= window.end_position:
            return state
        return await witness_store.compare_and_advance_state(
            window.consciousness_instance_id,
            expected_sequence=current,
            expected_revision=int(state.get("revision", 0) or 0),
            next_sequence=window.end_position,
            last_run_at=_now_iso(),
            last_success_at=success_at,
            last_error="",
        )

    async def _migrate_legacy_diaries(self) -> None:
        if self._legacy_migration_complete:
            return
        cfg = self.config
        memory = self._service.memory_service
        if memory is None:
            return
        if not bool(getattr(cfg, "migrate_legacy_diaries", True)):
            self._legacy_migration_complete = True
            return
        from .legacy_diary import migrate_legacy_diaries

        source = Path(
            str(getattr(cfg, "legacy_diary_path", "data/diaries") or "data/diaries")
        )
        if not source.is_absolute():
            source = Path.cwd() / source
        migration_key = str(source.resolve(strict=False))
        with _LEGACY_MIGRATION_GUARD:
            if migration_key in _LEGACY_MIGRATION_COMPLETED:
                self._legacy_migration_complete = True
                return
            if migration_key in _LEGACY_MIGRATION_IN_PROGRESS:
                return
            _LEGACY_MIGRATION_IN_PROGRESS.add(migration_key)
        try:
            migrated = await migrate_legacy_diaries(memory, source)
        except BaseException:
            with _LEGACY_MIGRATION_GUARD:
                _LEGACY_MIGRATION_IN_PROGRESS.discard(migration_key)
            raise
        with _LEGACY_MIGRATION_GUARD:
            _LEGACY_MIGRATION_IN_PROGRESS.discard(migration_key)
            _LEGACY_MIGRATION_COMPLETED.add(migration_key)
        self._legacy_migration_complete = True
        if migrated:
            logger.info(f"旧日记已幂等迁移为 legacy witness: {migrated} 条")

    async def _record_error(self, exc: Exception) -> None:
        memory = self._service.memory_service
        if memory is None:
            return
        await memory.update_witness_state(
            MEMORY_WITNESS_INSTANCE_ID,
            last_run_at=_now_iso(),
            last_error=type(exc).__name__,
        )

    async def _record_error_safely(self, exc: Exception) -> None:
        """Best-effort diagnostics must never replace the primary failure."""

        try:
            await self._record_error(exc)
        except asyncio.CancelledError:
            raise
        except Exception as record_error:  # noqa: BLE001 - diagnostic boundary
            _safe_log(
                "error",
                "记忆见证错误状态记录失败: "
                f"primary_error={type(exc).__name__}, "
                f"record_error={type(record_error).__name__}",
                exc_info=record_error,
            )

    async def _refresh_presence_snapshot_safely(self) -> None:
        """Refresh stale Presence state after CAS conflict, without looping."""

        refresh = getattr(self._service.consciousness_registry, "refresh", None)
        if not callable(refresh):
            return
        try:
            await refresh()
        except asyncio.CancelledError:
            raise
        except Exception as refresh_error:  # noqa: BLE001 - recovery boundary
            _safe_log(
                "error",
                f"记忆见证 Presence 快照刷新失败: error={type(refresh_error).__name__}",
                exc_info=refresh_error,
            )

    async def _touch_presence_after_commit(
        self,
        instance_id: str,
        *,
        timestamp: str,
    ) -> None:
        """Refresh one stale Presence snapshot without undoing committed work.

        The Life Event offset, witness ledger/projection, and witness-state mirror
        are already committed before this auxiliary activity touch.  A Presence
        CAS race therefore cannot turn that durable success into a failed witness
        run.  One refresh/retry is attempted; a repeated conflict is retained as
        a content-free warning and the latest read snapshot is refreshed again.
        """

        try:
            await self._service.touch_consciousness_instance(
                instance_id,
                timestamp=timestamp,
            )
            return
        except asyncio.CancelledError:
            raise
        except PresenceRevisionConflict:
            await self._refresh_presence_snapshot_safely()
        except Exception as exc:  # noqa: BLE001 - durable work is already committed
            _safe_log(
                "warning",
                "记忆见证已提交，Presence 尾触摸失败不反转本轮工作: "
                f"error={type(exc).__name__}",
            )
            return

        try:
            await self._service.touch_consciousness_instance(
                instance_id,
                timestamp=timestamp,
            )
        except asyncio.CancelledError:
            raise
        except PresenceRevisionConflict:
            await self._refresh_presence_snapshot_safely()
            _safe_log(
                "warning",
                "记忆见证已提交，Presence 尾触摸仍有 CAS 冲突，"
                "本轮成功状态保持不变: retry_count=1, "
                "error=PresenceRevisionConflict",
            )
        except Exception as exc:  # noqa: BLE001 - durable work is already committed
            _safe_log(
                "warning",
                "记忆见证已提交，Presence 尾触摸失败不反转本轮工作: "
                f"error={type(exc).__name__}",
            )

    async def _run_delivery_worker(self, witness_store: Any) -> _DeliveryReport:
        """Replay independently bounded World and projection outbox workers."""

        cfg = self.config
        limit = max(1, int(getattr(cfg, "delivery_batch_size", 50)))
        reports = [
            await self._run_delivery_kind(witness_store, delivery_kind, limit=limit)
            for delivery_kind in ("world", "projection")
        ]
        return _DeliveryReport(
            processed=sum(report.processed for report in reports),
            succeeded=sum(report.succeeded for report in reports),
            failed=sum(report.failed for report in reports),
        )

    async def _run_delivery_kind(
        self,
        witness_store: Any,
        delivery_kind: str,
        *,
        limit: int,
    ) -> _DeliveryReport:
        """Replay due jobs through one immutable frontier without head blocking."""

        cfg = self.config
        scan_limit = min(1000, max(limit, limit * 4))
        processed = 0
        succeeded = 0
        failed = 0
        after: StableLedgerCursor | None = None
        frontier: StableLedgerCursor | None = None
        exhausted = False
        while processed < limit and not exhausted:
            page = await witness_store.list_delivery_jobs_page(
                delivery_kind=delivery_kind,
                statuses=("pending", "failed", "processing"),
                after=after,
                through=frontier,
                limit=scan_limit,
            )
            if frontier is None:
                frontier = page.frontier
            for raw_job in page.items:
                if processed >= limit:
                    break
                job = raw_job
                if job.status == "processing":
                    if not self._time_due(job.lease_expires_at):
                        continue
                    try:
                        job = await witness_store.mark_delivery_job(
                            job.job_id,
                            expected_revision=job.revision,
                            status="failed",
                            error_type="WitnessDeliveryLeaseExpired",
                        )
                    except CursorConflict:
                        continue
                if not self._time_due(job.available_at):
                    continue
                lease_seconds = max(
                    60,
                    min(
                        1800,
                        int(getattr(cfg, "retry_delay_seconds", 60)) * 2,
                    ),
                )
                lease_expires_at = (
                    datetime.now(UTC).astimezone()
                    + timedelta(seconds=lease_seconds)
                ).isoformat()
                try:
                    claimed = await witness_store.mark_delivery_job(
                        job.job_id,
                        expected_revision=job.revision,
                        status="processing",
                        lease_owner=_DELIVERY_WORKER_ID,
                        lease_expires_at=lease_expires_at,
                    )
                except CursorConflict:
                    continue
                processed += 1
                try:
                    await self._deliver_job(claimed, witness_store)
                    await witness_store.mark_delivery_job(
                        claimed.job_id,
                        expected_revision=claimed.revision,
                        status="succeeded",
                    )
                    succeeded += 1
                except asyncio.CancelledError:
                    # The durable processing lease is intentionally left intact;
                    # another worker can recover it after the bounded lease expiry.
                    raise
                except CursorConflict:
                    # A concurrent owner completed or recovered the same CAS job.
                    continue
                except Exception as exc:  # noqa: BLE001 - per-job failure isolation
                    failed += 1
                    self._last_error_type = type(exc).__name__
                    retry_at = (
                        datetime.now(UTC).astimezone()
                        + timedelta(
                            seconds=max(
                                10,
                                int(getattr(cfg, "retry_delay_seconds", 60)),
                            )
                        )
                    ).isoformat()
                    try:
                        await witness_store.mark_delivery_job(
                            claimed.job_id,
                            expected_revision=claimed.revision,
                            status="failed",
                            error_type=type(exc).__name__,
                            available_at=retry_at,
                        )
                    except asyncio.CancelledError:
                        raise
                    except CursorConflict:
                        pass
                    _safe_log(
                        "warning",
                        "记忆见证耐久投递失败，任务保持可重放: "
                        f"kind={claimed.delivery_kind}, error={type(exc).__name__}",
                    )
            exhausted = not page.has_more
            if not exhausted:
                if page.next_cursor is None or page.next_cursor == after:
                    raise RuntimeError("WitnessDeliveryPaginationDidNotAdvance")
                after = page.next_cursor
                await asyncio.sleep(0)
        return _DeliveryReport(
            processed=processed,
            succeeded=succeeded,
            failed=failed,
        )

    async def _deliver_job(
        self,
        job: WitnessDeliveryJob,
        witness_store: Any,
    ) -> None:
        if job.delivery_kind == "world":
            await self._deliver_world(job.payload)
            return
        if job.delivery_kind != "projection":
            raise MemoryWitnessDeliveryPayloadInvalid(
                "MemoryWitnessDeliveryKindUnsupported"
            )
        witness_id = str(job.payload.get("witness_id") or "")
        projection_path = str(job.payload.get("projection_path") or "")
        if not witness_id or not projection_path:
            raise MemoryWitnessDeliveryPayloadInvalid(
                "MemoryWitnessProjectionPayloadIncomplete"
            )
        witness = await witness_store.get_by_projection_path(projection_path)
        if witness is None or witness.witness_id != witness_id:
            raise MemoryWitnessProjectionRecordMissing(
                "MemoryWitnessProjectionRecordMissing"
            )
        await self._project_witness(witness)

    async def _deliver_world(self, payload: dict[str, Any]) -> None:
        if payload.get("proof_state") != "exact_final_attempt":
            raise MemoryWitnessWorldReceiptUnavailable(
                "MemoryWitnessWorldReceiptUnavailable"
            )
        checkpoint_body = payload.get("checkpoint")
        receipt_body = payload.get("receipt")
        if not isinstance(checkpoint_body, dict) or not isinstance(receipt_body, dict):
            raise MemoryWitnessDeliveryPayloadInvalid(
                "MemoryWitnessWorldPayloadIncomplete"
            )
        try:
            checkpoint = PerceptionCommitCheckpoint(
                instance_id=str(checkpoint_body["instance_id"]),
                from_position=int(checkpoint_body["from_position"]),
                through_position=int(checkpoint_body["through_position"]),
                cursor_revision=int(checkpoint_body["cursor_revision"]),
                delivery_id=str(checkpoint_body["delivery_id"]),
                projection_sha256=str(checkpoint_body["projection_sha256"]),
                delivered_bytes=int(checkpoint_body["delivered_bytes"]),
            )
            receipt = PerceptionDeliveryReceipt(
                delivery_id=str(receipt_body["delivery_id"]),
                projection_sha256=str(receipt_body["projection_sha256"]),
                delivered_bytes=int(receipt_body["delivered_bytes"]),
                exact=bool(receipt_body["exact"]),
                transport_request_id=str(
                    receipt_body.get("transport_request_id") or ""
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryWitnessDeliveryPayloadInvalid(
                "MemoryWitnessWorldPayloadInvalid"
            ) from exc
        if (
            not receipt.exact
            or receipt.delivery_id != checkpoint.delivery_id
            or receipt.projection_sha256 != checkpoint.projection_sha256
            or receipt.delivered_bytes != checkpoint.delivered_bytes
        ):
            raise MemoryWitnessDeliveryPayloadInvalid(
                "MemoryWitnessWorldReceiptMismatch"
            )
        commit = getattr(self._service, "commit_perception_delivery", None)
        if not callable(commit):
            raise MemoryWitnessWorldReceiptUnavailable(
                "MemoryWitnessWorldReplayUnavailable"
            )
        committed = await commit(checkpoint, receipt)
        expected = (
            checkpoint.through_position,
            checkpoint.cursor_revision
            + int(checkpoint.through_position > checkpoint.from_position),
        )
        if tuple(committed) != expected:
            raise MemoryWitnessWorldCommitMismatch("MemoryWitnessWorldCommitMismatch")

    async def _reconcile_projections(
        self,
        witness_store: Any,
    ) -> _ReconciliationReport:
        """Rebuild known projections and diagnose bounded filesystem orphans."""

        cfg = self.config
        limit = max(
            1,
            int(getattr(cfg, "projection_reconcile_batch_size", 100)),
        )
        rebuilt = 0
        missing = 0
        orphan = 0
        legacy_actionable = 0
        legacy_failed = 0

        get_scan_state = getattr(witness_store, "get_reconciliation_state", None)
        advance_scan_state = getattr(
            witness_store,
            "compare_and_advance_reconciliation_state",
            None,
        )
        list_pending_page = getattr(witness_store, "list_pending_page", None)
        list_projection_page = getattr(
            witness_store,
            "list_projection_records_page",
            None,
        )
        durable_pagination = all(
            callable(item)
            for item in (
                get_scan_state,
                advance_scan_state,
                list_pending_page,
                list_projection_page,
            )
        )
        durable_reconciliation_state = bool(
            callable(get_scan_state) and callable(advance_scan_state)
        )
        legacy_state: WitnessReconciliationState | None = None
        projection_state: WitnessReconciliationState | None = None
        filesystem_state: WitnessReconciliationState | None = None
        legacy_page: Any | None = None
        projection_page: Any | None = None
        legacy_candidates: Sequence[Any] = ()
        complete_candidates: Sequence[Any] = ()
        if durable_pagination:
            legacy_state = await get_scan_state("legacy_pending_projection:v1")
            legacy_page = await list_pending_page(
                after=legacy_state.cursor,
                through=legacy_state.frontier,
                limit=limit,
            )
            legacy_pending = tuple(legacy_page.items)
        else:
            legacy_candidates = await witness_store.list_pending(
                limit=_RECONCILIATION_SCAN_LIMIT
            )
            legacy_pending, self._legacy_projection_cursor = self._rotating_page(
                legacy_candidates,
                key=lambda item: str(item.witness_id),
                after=self._legacy_projection_cursor,
                limit=limit,
            )
        for witness in legacy_pending:
            metadata = dict(witness.metadata or {})
            if metadata.get("pipeline_version") == _DECISION_VERSION:
                continue
            if not witness.projection_path:
                continue
            legacy_actionable += 1
            try:
                await self._project_witness(witness)
                rebuilt += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one projection is isolated
                legacy_failed += 1
                self._last_error_type = type(exc).__name__

        legacy_completed = (
            len(legacy_candidates) < _RECONCILIATION_SCAN_LIMIT
            if not durable_pagination
            else True
        )
        if durable_pagination:
            assert legacy_state is not None and legacy_page is not None
            legacy_completed = not bool(legacy_page.has_more)
            legacy_state = await advance_scan_state(
                "legacy_pending_projection:v1",
                expected_revision=legacy_state.revision,
                next_cursor=(
                    None if legacy_completed else legacy_page.next_cursor
                ),
                frontier=None if legacy_completed else legacy_page.frontier,
                completed=legacy_completed,
            )

        if durable_pagination:
            projection_state = await get_scan_state("completed_projection:v1")
            projection_page = await list_projection_page(
                statuses=("succeeded",),
                after=projection_state.cursor,
                through=projection_state.frontier,
                limit=limit,
            )
            complete_jobs = tuple(projection_page.items)
        else:
            complete_candidates = await witness_store.list_projection_records(
                statuses=("succeeded",),
                limit=_RECONCILIATION_SCAN_LIMIT,
            )
            complete_jobs, self._projection_job_cursor = self._rotating_page(
                complete_candidates,
                key=lambda item: str(item.job_id),
                after=self._projection_job_cursor,
                limit=limit,
            )
        workspace = self._service._workspace_dir()
        for job in complete_jobs:
            path = str(job.payload.get("projection_path") or "")
            witness_id = str(job.payload.get("witness_id") or "")
            if not path or not witness_id:
                missing += 1
                continue
            absolute = workspace / path
            exists = await asyncio.to_thread(absolute.is_file)
            if exists:
                continue
            missing += 1
            witness = await witness_store.get_by_projection_path(path)
            if witness is None or witness.witness_id != witness_id:
                continue
            try:
                await self._project_witness(witness)
                rebuilt += 1
                missing -= 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconciliation remains bounded
                self._last_error_type = type(exc).__name__

        projection_completed = (
            len(complete_candidates) < _RECONCILIATION_SCAN_LIMIT
            if not durable_pagination
            else True
        )
        if durable_pagination:
            assert projection_state is not None and projection_page is not None
            projection_completed = not bool(projection_page.has_more)
            projection_state = await advance_scan_state(
                "completed_projection:v1",
                expected_revision=projection_state.revision,
                next_cursor=(
                    None if projection_completed else projection_page.next_cursor
                ),
                frontier=None if projection_completed else projection_page.frontier,
                completed=projection_completed,
            )

        projection_root = workspace / "diaries" / "witness"
        files: tuple[Path, ...] = ()
        truncated = False
        filesystem_scan_blocker = ""
        filesystem_page: Any | None = None
        if durable_reconciliation_state:
            filesystem_state = await get_scan_state(
                _FILESYSTEM_RECONCILIATION_SCAN_NAME
            )
            try:
                filesystem_page = await asyncio.to_thread(
                    self._projection_filesystem_page,
                    projection_root,
                    after=filesystem_state.cursor,
                    through=filesystem_state.frontier,
                    limit=limit,
                )
                files = tuple(filesystem_page.items)
                truncated = bool(filesystem_page.has_more)
                for absolute in files:
                    try:
                        relative = absolute.relative_to(workspace).as_posix()
                    except ValueError:
                        orphan += 1
                        continue
                    if (
                        await witness_store.get_by_projection_path(relative)
                        is None
                    ):
                        orphan += 1
                # The filesystem has no transaction.  Recompute the content-free
                # manifest immediately before cursor CAS so a mid-page add/
                # remove/rewrite cannot be mistaken for a completed stable scan.
                await asyncio.to_thread(
                    self._projection_filesystem_page,
                    projection_root,
                    after=filesystem_page.next_cursor,
                    through=filesystem_page.frontier,
                    limit=1,
                )
                filesystem_completed = not bool(filesystem_page.has_more)
                filesystem_state = await advance_scan_state(
                    _FILESYSTEM_RECONCILIATION_SCAN_NAME,
                    expected_revision=filesystem_state.revision,
                    next_cursor=(
                        None
                        if filesystem_completed
                        else filesystem_page.next_cursor
                    ),
                    frontier=(
                        None
                        if filesystem_completed
                        else filesystem_page.frontier
                    ),
                    completed=filesystem_completed,
                )
            except MemoryWitnessProjectionFilesystemChanged:
                # The subject is still writing its own witness diary while this
                # scan runs.  The filesystem has no transaction, so a mid-page
                # rewrite is normal contention, not corruption.  Skip this scan
                # page and retry on the next cycle instead of aborting the whole
                # reconciliation (or spamming ERROR/traceback through the loop).
                filesystem_scan_blocker = (
                    "WitnessProjectionFilesystemSourceChangedRetryNextCycle"
                )
                files = ()
                truncated = False
        else:
            filesystem_scan_blocker = (
                "WitnessProjectionFilesystemReconciliationStateUnavailable"
            )
        ledger_scan_complete = bool(legacy_completed and projection_completed)
        ledger_scan_blocker = (
            ""
            if durable_pagination or ledger_scan_complete
            else "WitnessLedgerStoreNoProjectionPagination"
        )
        return _ReconciliationReport(
            rebuilt=rebuilt,
            missing=missing,
            orphan=orphan,
            legacy_actionable=legacy_actionable,
            legacy_failed=legacy_failed,
            filesystem_scan_truncated=truncated,
            ledger_scan_complete=ledger_scan_complete,
            ledger_scan_blocker=ledger_scan_blocker,
            legacy_scan_state=legacy_state,
            projection_scan_state=projection_state,
            filesystem_scan_state=filesystem_state,
            filesystem_cursor_scope=(
                "durable_store"
                if durable_reconciliation_state
                else "unavailable"
            ),
            filesystem_scan_blocker=filesystem_scan_blocker,
        )

    @staticmethod
    def _rotating_page(
        items: Sequence[Any],
        *,
        key: Any,
        after: str,
        limit: int,
    ) -> tuple[tuple[Any, ...], str]:
        """Return a stable process-lifetime page without repeating the head."""

        ordered = tuple(sorted(items, key=key))
        if not ordered:
            return (), ""
        start = 0
        if after:
            for index, item in enumerate(ordered):
                if key(item) > after:
                    start = index
                    break
            else:
                start = 0
        page = ordered[start : start + max(1, int(limit))]
        if not page:
            return (), ""
        reached_end = start + len(page) >= len(ordered)
        return page, "" if reached_end else str(key(page[-1]))

    @staticmethod
    def _projection_filesystem_page(
        root: Path,
        *,
        after: StableLedgerCursor | None,
        through: StableLedgerCursor | None,
        limit: int,
    ) -> Any:
        """Return one path-only page bound to an immutable source manifest."""

        page_limit = max(1, min(1000, int(limit)))
        entries = MemoryWitnessCoordinator._projection_filesystem_inventory(root)
        if not entries:
            if through is not None or after is not None:
                raise MemoryWitnessProjectionFilesystemChanged(
                    "WitnessProjectionFilesystemSourceChanged"
                )
            return StableLedgerPage((), None, None, False)
        manifest = hashlib.sha256()
        for entry in entries:
            manifest.update(entry.descriptor_sha256.encode("ascii"))
            manifest.update(b"\n")
        order_value = manifest.hexdigest()
        current_frontier = StableLedgerCursor(
            order_value=order_value,
            identity=entries[-1].identity,
        )
        if through is not None and through != current_frontier:
            raise MemoryWitnessProjectionFilesystemChanged(
                "WitnessProjectionFilesystemSourceChanged"
            )
        start = 0
        if after is not None:
            if through is None or after.order_value != order_value:
                raise MemoryWitnessProjectionFilesystemChanged(
                    "WitnessProjectionFilesystemCursorSourceMismatch"
                )
            for index, entry in enumerate(entries):
                if entry.identity == after.identity:
                    start = index + 1
                    break
            else:
                raise MemoryWitnessProjectionFilesystemChanged(
                    "WitnessProjectionFilesystemCursorMissing"
                )
        selected = entries[start : start + page_limit]
        has_more = start + len(selected) < len(entries)
        next_cursor = (
            StableLedgerCursor(order_value, selected[-1].identity)
            if selected
            else after
        )
        return StableLedgerPage(
            items=tuple(entry.path for entry in selected),
            next_cursor=next_cursor,
            frontier=current_frontier,
            has_more=has_more,
        )

    @staticmethod
    def _projection_filesystem_inventory(
        root: Path,
    ) -> tuple[_ProjectionFilesystemEntry, ...]:
        """Build a bounded content-free manifest without retaining file bodies."""

        if not root.exists():
            return ()
        if root.is_symlink() or not root.is_dir():
            raise MemoryWitnessProjectionFilesystemUnsafe(
                "WitnessProjectionFilesystemRootUnsafe"
            )
        entries: list[_ProjectionFilesystemEntry] = []
        seen_identities: set[str] = set()
        total_bytes = 0
        for path in MemoryWitnessCoordinator._iter_projection_files(root):
            if path.suffix != ".md":
                continue
            if len(entries) >= _FILESYSTEM_RECONCILIATION_MAX_FILES:
                raise MemoryWitnessProjectionFilesystemBoundExceeded(
                    "WitnessProjectionFilesystemFileBoundExceeded"
                )
            if path.is_symlink():
                raise MemoryWitnessProjectionFilesystemUnsafe(
                    "WitnessProjectionFilesystemSymlinkUnsupported"
                )
            try:
                before = path.stat()
            except FileNotFoundError as exc:
                raise MemoryWitnessProjectionFilesystemChanged(
                    "WitnessProjectionFilesystemSourceChanged"
                ) from exc
            total_bytes += max(0, int(before.st_size))
            if total_bytes > _FILESYSTEM_RECONCILIATION_MAX_BYTES:
                raise MemoryWitnessProjectionFilesystemBoundExceeded(
                    "WitnessProjectionFilesystemByteBoundExceeded"
                )
            content_sha256 = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    while True:
                        chunk = handle.read(_FILESYSTEM_HASH_CHUNK_BYTES)
                        if not chunk:
                            break
                        content_sha256.update(chunk)
                after_stat = path.stat()
            except FileNotFoundError as exc:
                raise MemoryWitnessProjectionFilesystemChanged(
                    "WitnessProjectionFilesystemSourceChanged"
                ) from exc
            if (
                before.st_size != after_stat.st_size
                or before.st_mtime_ns != after_stat.st_mtime_ns
                or getattr(before, "st_ino", 0) != getattr(after_stat, "st_ino", 0)
            ):
                raise MemoryWitnessProjectionFilesystemChanged(
                    "WitnessProjectionFilesystemSourceChanged"
                )
            relative = path.relative_to(root).as_posix()
            relative_bytes = relative.encode("utf-8")
            identity = hashlib.sha256(relative_bytes).hexdigest()
            if identity in seen_identities:
                raise MemoryWitnessProjectionFilesystemUnsafe(
                    "WitnessProjectionFilesystemIdentityCollision"
                )
            seen_identities.add(identity)
            descriptor = hashlib.sha256()
            descriptor.update(b"memory-witness-projection-file-v1\0")
            descriptor.update(relative_bytes)
            descriptor.update(b"\0")
            descriptor.update(str(before.st_size).encode("ascii"))
            descriptor.update(b"\0")
            descriptor.update(str(before.st_mtime_ns).encode("ascii"))
            descriptor.update(b"\0")
            descriptor.update(content_sha256.hexdigest().encode("ascii"))
            entries.append(
                _ProjectionFilesystemEntry(
                    path=path,
                    identity=identity,
                    descriptor_sha256=descriptor.hexdigest(),
                )
            )
        return tuple(sorted(entries, key=lambda item: item.identity))

    @staticmethod
    def _iter_projection_files(root: Path) -> Any:
        def fail_scan(error: OSError) -> None:
            raise error

        for directory, directories, filenames in os.walk(root, onerror=fail_scan):
            for name in tuple(directories):
                if (Path(directory) / name).is_symlink():
                    raise MemoryWitnessProjectionFilesystemUnsafe(
                        "WitnessProjectionFilesystemSymlinkUnsupported"
                    )
            directories.sort()
            for filename in sorted(filenames):
                yield Path(directory) / filename

    async def health_snapshot(self) -> dict[str, Any]:
        """Return a bounded content-free staged-pipeline health snapshot."""

        memory = self._service.memory_service
        if memory is None:
            return {
                "status": "disabled",
                "component": "memory_witness_pipeline",
                "reason": "memory_service_unavailable",
            }
        try:
            storage = memory._require_memory_storage()
        except Exception as exc:  # noqa: BLE001 - health must stay available
            return {
                "status": "failed",
                "component": "memory_witness_pipeline",
                "error_type": type(exc).__name__,
            }

        raw = await self._raw_ingest_health()
        try:
            experience = self._content_free_experience_health(
                await storage.experiences.health_snapshot()
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - content-free health boundary
            experience = {
                "status": "unavailable",
                "error_type": type(exc).__name__,
            }
        try:
            state = dict(await storage.witnesses.get_state(MEMORY_WITNESS_INSTANCE_ID))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - content-free health boundary
            state = {
                "last_sequence": 0,
                "last_run_at": "",
                "last_success_at": "",
                "last_error": type(exc).__name__,
            }
        try:
            pending = await storage.witnesses.next_pending_window(
                MEMORY_WITNESS_INSTANCE_ID
            )
            pending_windows = {
                "count": int(pending is not None),
                "exact": pending is None,
                "head": (
                    {
                        "window_id_sha256": hashlib.sha256(
                            str(pending.window_id).encode("utf-8")
                        ).hexdigest(),
                        "start_position": int(pending.start_position),
                        "end_position": int(pending.end_position),
                        "occurrence_count": len(pending.occurrences),
                    }
                    if pending is not None
                    else {}
                ),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - content-free health boundary
            pending_windows = {
                "count": None,
                "exact": False,
                "error_type": type(exc).__name__,
            }
        try:
            delivery_health = getattr(storage.witnesses, "delivery_health", None)
            if not callable(delivery_health):
                raise RuntimeError("WitnessLedgerStoreNoDeliveryHealth")
            raw_outbox = await delivery_health()
            if not isinstance(raw_outbox, dict):
                raise TypeError("WitnessDeliveryHealthInvalid")
            outbox_counts = {
                str(kind): {
                    str(status): max(0, int(count))
                    for status, count in dict(statuses).items()
                }
                for kind, statuses in dict(raw_outbox.get("counts") or {}).items()
            }
            actionable = {
                str(kind): max(0, int(count))
                for kind, count in dict(
                    raw_outbox.get("actionable") or {}
                ).items()
            }
            actionable_projection = actionable.get("projection", 0)
            outbox = {
                "counts": outbox_counts,
                "actionable": actionable,
                "total": self._content_free_nonnegative_int(
                    raw_outbox.get("total")
                ),
                "exact": bool(raw_outbox.get("exact")),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - content-free health boundary
            actionable_projection = 0
            outbox = {
                "counts": {},
                "exact": False,
                "error_type": type(exc).__name__,
            }

        raw_frontier = raw.get("frontier")
        raw_cursor = raw.get("cursor")
        experience_frontier = experience.get("frontier")
        author_cursor = self._content_free_nonnegative_int(state.get("last_sequence"))
        if author_cursor is None:
            author_cursor = 0
        author_backlog = (
            max(0, int(experience_frontier) - author_cursor)
            if isinstance(experience_frontier, int)
            else None
        )
        reconciliation_states: dict[str, Any] = {}
        try:
            get_reconciliation_state = getattr(
                storage.witnesses,
                "get_reconciliation_state",
                None,
            )
            if callable(get_reconciliation_state):
                for label, scan_name in (
                    ("legacy_pending", "legacy_pending_projection:v1"),
                    ("completed_projection", "completed_projection:v1"),
                    (
                        "projection_filesystem",
                        _FILESYSTEM_RECONCILIATION_SCAN_NAME,
                    ),
                ):
                    reconciliation_states[label] = (
                        self._content_free_reconciliation_state(
                            await get_reconciliation_state(scan_name)
                        )
                    )
            else:
                reconciliation_states["status"] = "unsupported"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - content-free health boundary
            reconciliation_states = {
                "status": "unavailable",
                "error_type": type(exc).__name__,
            }
        filesystem_reconciliation = reconciliation_states.get(
            "projection_filesystem"
        )
        filesystem_cursor = (
            dict(filesystem_reconciliation.get("cursor") or {})
            if isinstance(filesystem_reconciliation, dict)
            else self._content_free_stable_cursor(
                (
                    self._last_reconciliation.filesystem_scan_state.cursor
                    if self._last_reconciliation.filesystem_scan_state is not None
                    else None
                )
            )
        )
        filesystem_frontier = (
            dict(filesystem_reconciliation.get("frontier") or {})
            if isinstance(filesystem_reconciliation, dict)
            else self._content_free_stable_cursor(
                (
                    self._last_reconciliation.filesystem_scan_state.frontier
                    if self._last_reconciliation.filesystem_scan_state is not None
                    else None
                )
            )
        )
        projection = {
            "actionable": actionable_projection,
            "legacy_no_path": {
                "count": None,
                "status": "unavailable",
                "reason": "WitnessLedgerStoreNoLegacyNoPathCount",
                "actionable": False,
            },
            "missing": self._last_reconciliation.missing,
            "orphan": self._last_reconciliation.orphan,
            "rebuilt": self._last_reconciliation.rebuilt,
            "legacy_actionable": self._last_reconciliation.legacy_actionable,
            "legacy_failed": self._last_reconciliation.legacy_failed,
            "filesystem_scan_truncated": (
                self._last_reconciliation.filesystem_scan_truncated
            ),
            "ledger_scan_complete": self._last_reconciliation.ledger_scan_complete,
            "ledger_scan_blocker": self._last_reconciliation.ledger_scan_blocker,
            "ledger_rotation_cursor_scope": (
                "durable_store"
                if "status" not in reconciliation_states
                else "process_lifetime_compatibility"
            ),
            "ledger_rotation_cursors": reconciliation_states,
            "filesystem_cursor_scope": (
                self._last_reconciliation.filesystem_cursor_scope
            ),
            "filesystem_cursor": filesystem_cursor,
            "filesystem_frontier": filesystem_frontier,
            "filesystem_scan_blocker": (
                self._last_reconciliation.filesystem_scan_blocker
            ),
            "complete_legacy_missing_scan": {
                "status": "unavailable",
                "reason": "WitnessLedgerStoreNoCompleteWitnessListing",
            },
        }
        error_type = self._content_free_error_type(state.get("last_error"))
        if not error_type:
            error_type = self._content_free_error_type(self._last_error_type)
        failed_delivery_jobs = sum(
            int(counts.get("failed", 0))
            for counts in outbox.get("counts", {}).values()
            if isinstance(counts, dict)
        )
        degraded = bool(
            error_type
            or raw.get("status") != "healthy"
            or experience.get("status") != "healthy"
            or outbox.get("error_type")
            or failed_delivery_jobs
            or self._last_delivery.failed
            or self._last_reconciliation.legacy_failed
            or self._last_reconciliation.missing
            or self._last_reconciliation.filesystem_scan_blocker
            or self._oversized_window
            or self._occurrence_pagination_blocker
            or self._last_reconciliation.ledger_scan_blocker
            or self._author_claim_mode
            in {
                "selected_runtime_claim_unavailable",
                "selected_runtime_claim_failed",
            }
        )
        claim = self._author_claim
        claim_health = {
            "status": self._author_claim_mode,
            "cross_process_safe": self._author_claim_mode == "durable_singleton_claim",
            "lease_epoch": (
                self._content_free_nonnegative_int(getattr(claim, "lease_epoch", 0))
                if claim is not None
                else 0
            ),
            "lease_until": (
                self._content_free_timestamp(getattr(claim, "lease_until", ""))
                if claim is not None
                else ""
            ),
        }
        if self._author_claim_mode == "process_lock_only":
            claim_health["reason"] = "StorageRuntimeHasNoDurableSingletonClaim"
        elif self._author_claim_mode == "selected_runtime_claim_unavailable":
            claim_health["reason"] = "SelectedRuntimeClaimUnavailable"
        elif self._author_claim_mode == "selected_runtime_claim_failed":
            claim_health["reason"] = "SelectedRuntimeClaimValidationFailed"
        continuity_delivery = self._continuity_delivery_health()
        return {
            "status": "degraded" if degraded else "healthy",
            "component": "memory_witness_pipeline",
            "raw_ingest": {
                **raw,
                "consumer_id": MEMORY_EXPERIENCE_CONSUMER_ID,
                "backlog": (
                    max(0, int(raw_frontier) - int(raw_cursor))
                    if isinstance(raw_frontier, int) and isinstance(raw_cursor, int)
                    else None
                ),
            },
            "experience": experience,
            "author": {
                "cursor": author_cursor,
                "frontier": experience_frontier,
                "backlog": author_backlog,
                "pending_windows": pending_windows,
                "planner_version": _WINDOW_PLANNER_VERSION,
                "cross_process_window_claim": claim_health,
                "oversized_window": dict(self._oversized_window),
                "occurrence_pagination_blocker": dict(
                    self._occurrence_pagination_blocker
                ),
                "occurrence_pagination": {
                    "contract": "(ingest_position, occurrence_id)",
                    "cursor_scope": "durable_complete_position",
                    "durable_frontier": dict(
                        experience.get("frontier_cursor") or {}
                    ),
                    "last_scan_frontier": self._content_free_experience_cursor(
                        self._last_occurrence_frontier
                    ),
                },
            },
            "outbox": outbox,
            "projection": projection,
            "continuity_delivery_verifier": continuity_delivery,
            "legacy_writer": {
                "retired": True,
                "writes_enabled": False,
                "authoritative": False,
                "replacement": "staged_experience_witness_pipeline",
            },
            "runtime": {
                "last_run_at": self._content_free_timestamp(
                    state.get("last_run_at") or self._last_run_at
                ),
                "last_success_at": self._content_free_timestamp(
                    state.get("last_success_at") or self._last_success_at
                ),
                "last_error_type": error_type,
                "last_ingest_batches": self._last_ingest.batches,
                "last_ingest_occurrences": self._last_ingest.occurrence_count,
                "last_delivery_processed": self._last_delivery.processed,
                "world_commit_order": "decision_outbox_before_world_commit",
                "subject_projection": dict(self._last_subject_projection),
            },
        }

    @staticmethod
    def _content_free_nonnegative_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _content_free_timestamp(value: Any) -> str:
        candidate = str(value or "").strip()
        if not candidate or len(candidate) > 64:
            return ""
        try:
            datetime.fromisoformat(candidate)
        except (TypeError, ValueError):
            return ""
        return candidate

    @staticmethod
    def _content_free_cursor(value: Any) -> dict[str, Any]:
        candidate = str(value or "")
        return {
            "present": bool(candidate),
            "sha256": (
                hashlib.sha256(candidate.encode("utf-8")).hexdigest()
                if candidate
                else ""
            ),
        }

    @staticmethod
    def _content_free_stable_cursor(
        cursor: StableLedgerCursor | None,
    ) -> dict[str, Any]:
        if cursor is None:
            return {
                "present": False,
                "order_value": "",
                "order_value_sha256": "",
                "identity_sha256": "",
            }
        order_value = str(cursor.order_value)
        return {
            "present": True,
            "order_value": MemoryWitnessCoordinator._content_free_timestamp(
                order_value
            ),
            "order_value_sha256": hashlib.sha256(
                order_value.encode("utf-8")
            ).hexdigest(),
            "identity_sha256": hashlib.sha256(
                cursor.identity.encode("utf-8")
            ).hexdigest(),
        }

    @classmethod
    def _content_free_reconciliation_state(
        cls,
        state: WitnessReconciliationState,
    ) -> dict[str, Any]:
        return {
            "revision": cls._content_free_nonnegative_int(state.revision),
            "cursor": cls._content_free_stable_cursor(state.cursor),
            "frontier": cls._content_free_stable_cursor(state.frontier),
            "cycle_started_at": cls._content_free_timestamp(
                state.cycle_started_at
            ),
            "last_completed_at": cls._content_free_timestamp(
                state.last_completed_at
            ),
            "updated_at": cls._content_free_timestamp(state.updated_at),
        }

    @staticmethod
    def _content_free_experience_cursor(
        cursor: ExperienceOccurrenceCursor | None,
    ) -> dict[str, Any]:
        if cursor is None:
            return {
                "present": False,
                "ingest_position": None,
                "occurrence_id_sha256": "",
            }
        return {
            "present": True,
            "ingest_position": max(0, int(cursor.ingest_position)),
            "occurrence_id_sha256": hashlib.sha256(
                cursor.occurrence_id.encode("utf-8")
            ).hexdigest(),
        }

    @staticmethod
    def _content_free_error_type(value: Any) -> str:
        candidate = str(value or "").strip()
        if (
            not candidate
            or len(candidate) > 128
            or not candidate.isascii()
            or not (candidate[0].isalpha() or candidate[0] == "_")
            or any(not (char.isalnum() or char in "._") for char in candidate)
        ):
            return "RecordedWitnessError" if candidate else ""
        return candidate

    @classmethod
    def _content_free_experience_health(cls, snapshot: Any) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise TypeError("MemoryExperienceHealthInvalid")
        raw_status = str(snapshot.get("status") or "")
        status = (
            raw_status
            if raw_status in {"healthy", "degraded", "unavailable", "failed"}
            else "unknown"
        )
        projected: dict[str, Any] = {"status": status}
        for key in (
            "canonical_count",
            "alias_count",
            "occurrence_count",
            "frontier",
        ):
            projected[key] = cls._content_free_nonnegative_int(snapshot.get(key))
        latest = cls._content_free_timestamp(snapshot.get("latest_recorded_at"))
        if latest:
            projected["latest_recorded_at"] = latest
        raw_frontier_cursor = snapshot.get("frontier_cursor")
        if isinstance(raw_frontier_cursor, dict):
            position = cls._content_free_nonnegative_int(
                raw_frontier_cursor.get("ingest_position")
            )
            identity_sha256 = str(
                raw_frontier_cursor.get("occurrence_id_sha256") or ""
            )
            projected["frontier_cursor"] = {
                "ingest_position": position,
                "occurrence_id_sha256": (
                    identity_sha256
                    if len(identity_sha256) == 64
                    and all(char in "0123456789abcdef" for char in identity_sha256)
                    else ""
                ),
            }
        return projected

    @staticmethod
    def _continuity_delivery_health() -> dict[str, Any]:
        """Read the existing content-free continuity verifier diagnostics."""

        try:
            from ..memory.continuity_delivery import (
                get_memory_continuity_delivery_coordinator,
            )

            snapshot = get_memory_continuity_delivery_coordinator().snapshot()
            return {
                "status": "healthy",
                "pending_pages": int(snapshot.pending_pages),
                "committed_pages": int(snapshot.committed_pages),
                "candidate_coverages": int(snapshot.candidate_coverages),
                "limits": {
                    "max_pending": int(snapshot.max_pending),
                    "max_committed_pages": int(snapshot.max_committed_pages),
                },
            }
        except Exception as exc:  # noqa: BLE001 - optional health aggregation
            return {
                "status": "unavailable",
                "error_type": type(exc).__name__,
            }

    async def _raw_ingest_health(self) -> dict[str, Any]:
        try:
            store = self._service._get_life_event_store()
            get_offset = getattr(store, "get_consumer_offset", None)
            if not callable(get_offset):
                raise RuntimeError("RawConsumerOffsetUnavailable")
            cursor = self._content_free_nonnegative_int(
                await get_offset(MEMORY_EXPERIENCE_CONSUMER_ID)
            )
            if cursor is None:
                raise RuntimeError("RawConsumerOffsetInvalid")
            provider = getattr(store, "health", None)
            if not callable(provider):
                provider = getattr(store, "health_snapshot", None)
            if not callable(provider):
                return {
                    "status": "unavailable",
                    "cursor": cursor,
                    "frontier": None,
                    "reason": "RawEventHealthUnavailable",
                }
            snapshot = provider()
            if inspect.isawaitable(snapshot):
                snapshot = await snapshot
            if not isinstance(snapshot, dict):
                raise TypeError("RawEventHealthInvalid")
            raw_frontier = snapshot.get("latest_position", snapshot.get("frontier"))
            frontier = self._content_free_nonnegative_int(raw_frontier)
            return {
                "status": "healthy" if frontier is not None else "unavailable",
                "cursor": cursor,
                "frontier": frontier,
                "earliest": self._content_free_nonnegative_int(
                    snapshot.get(
                        "earliest_position",
                        snapshot.get("earliest"),
                    )
                ),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - content-free health boundary
            return {
                "status": "unavailable",
                "cursor": None,
                "frontier": None,
                "error_type": type(exc).__name__,
            }

    @staticmethod
    def _time_due(value: str) -> bool:
        if not str(value or "").strip():
            return True
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed <= datetime.now(UTC).astimezone()
        except (TypeError, ValueError):
            return True

    @staticmethod
    def _to_experience(event: LifeEvent) -> ExperienceRecord:
        metadata = dict(event.metadata or {})
        return ExperienceRecord(
            event_id=event.occurrence_id or f"occ_position_{event.sequence}",
            source_event_id=event.event_id,
            sequence=event.sequence,
            occurred_at=event.timestamp,
            recorded_at=_now_iso(),
            source=event.source,
            channel=event.channel,
            event_type=event.event_type,
            content=event.content,
            stream_id=event.stream_id,
            consciousness_instance_id=str(
                event.source_instance_id
                or metadata.get("consciousness_instance_id")
                or ""
            ),
            actor=str(metadata.get("sender") or event.source or ""),
            visibility="private",
            valid_from=event.timestamp,
            metadata=metadata,
        )

    @staticmethod
    def _window_id(
        instance_id: str,
        first: ExperienceOccurrenceRef,
    ) -> str:
        # The identity intentionally excludes the configurable end boundary.
        # A crash after decision but before cursor CAS can therefore recover the
        # already persisted window even if budgets change before restart.
        material = (
            f"{_WINDOW_PLANNER_VERSION}:{instance_id}:"
            f"{first.ingest_position}:{first.occurrence_id}"
        )
        return "window-" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _decision_id(window_id: str) -> str:
        return (
            "decision-"
            + hashlib.sha256(
                f"{_DECISION_VERSION}:{window_id}".encode("utf-8")
            ).hexdigest()
        )

    @staticmethod
    def _window_stream_scope(
        occurrences: Sequence[ExperienceOccurrenceRef],
    ) -> str:
        scopes = tuple(
            dict.fromkeys(str(item.experience.stream_id or "") for item in occurrences)
        )
        if len(scopes) == 1:
            return scopes[0]
        digest = hashlib.sha256("\n".join(scopes).encode("utf-8")).hexdigest()[:16]
        return f"mixed:{digest}"

    @staticmethod
    def _is_self_presence_side_effect(
        record: ExperienceRecord,
        *,
        instance_id: str,
    ) -> bool:
        """Fence only the witness's own Presence protocol feedback events."""

        return (
            record.channel == "system"
            and record.source == "life_engine.presence"
            and record.consciousness_instance_id == instance_id
            and record.event_type in _SELF_PRESENCE_SIDE_EFFECT_EVENT_TYPES
        )

    async def _author_witness(
        self,
        instance: ConsciousnessInstance,
        records: Sequence[ExperienceOccurrenceRef],
    ) -> _AuthoringResult:
        cfg = self.config
        # Resolve the unified subject projection before allocating a model
        # request or preparing transient World delivery.  A projection
        # manifest conflict is a recoverable authority precondition failure;
        # model configuration must not mask it or advance any cursor.
        system_prompt = await self._build_system_prompt(instance)
        task_name = str(getattr(cfg, "model_task_name", "witness") or "witness")
        model_set = get_model_set_by_task(task_name)
        if not model_set:
            raise RuntimeError(f"MemoryWitnessModelUnavailable:{task_name}")
        perception = await self._service.prepare_perception(instance.instance_id)
        request = LLMRequest(model_set, "life_memory_witness")
        request.add_payload(
            LLMPayload(ROLE.SYSTEM, Text(system_prompt))
        )
        recent_subconscious = await self._build_recent_subconscious_background()
        if recent_subconscious:
            request.add_payload(
                LLMPayload(ROLE.USER, Text(recent_subconscious))
            )
        instruction_text = (
            "请回望下面这段已经发生并被保存的经历，写下你此刻愿意留下的"
            "第一人称见证。如果没有值得留下的主观感受，只输出 "
            f"{_NO_WITNESS}。只有 Experience 窗口定义本次见证的经历范围；"
            "World 投影是当前意识的有来源环境背景，潜意识近期上下文（若有）"
            "只维持主体连续性，二者都不能替代 Experience 充当本次经历证据。"
        )
        request.add_payload(LLMPayload(ROLE.USER, Text(instruction_text)))
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(self._format_experience_window(records)),
            )
        )
        # World must remain the final exact Text part.  The context manager may
        # trim earlier USER parts under pressure, while its recency policy keeps
        # the tail.  Registering only this part prevents Experience text from
        # satisfying the receipt and keeps exact delivery fail-closed.
        request.add_payload(LLMPayload(ROLE.USER, Text(str(perception.content))))
        request.register_context_delivery(
            str(perception.delivery_id),
            str(perception.content),
            marker=str(perception.delivery_marker),
        )
        timeout = max(10.0, float(getattr(cfg, "timeout_seconds", 600.0)))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        timeout_scope = asyncio.timeout_at(deadline)
        try:
            async with timeout_scope:
                # Non-stream mode keeps forced-stream collection inside the
                # request policy, so a stream failure can fail over and the
                # returned receipts belong to the final successful attempt.
                response = await request.send(stream=False)
                result = await response
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            if not timeout_scope.expired():
                raise
            raise TimeoutError(
                "MemoryWitnessAuthoringDeadlineExceeded:"
                f"configured_timeout={timeout:.3f}:task_name={task_name}"
            ) from exc
        receipt = _exact_perception_receipt(response, perception)
        if receipt is None:
            raise RuntimeError("MemoryWitnessPerceptionDeliveryUnverified")
        raw_text = str(result or "")
        response_bytes = len(raw_text.encode("utf-8"))
        response_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        normalized_decision = unicodedata.normalize("NFKC", raw_text).strip().casefold()
        if not normalized_decision:
            raise MemoryWitnessAuthoringEmptyResponse(
                "MemoryWitnessAuthoringEmptyResponse"
            )
        text = "" if normalized_decision == _NO_WITNESS.casefold() else raw_text
        return _AuthoringResult(
            text=text,
            model_task_name=task_name,
            model_request_id=str(getattr(response, "request_record_id", "") or ""),
            response_sha256=response_sha256,
            response_bytes=response_bytes,
            world_payload=self._world_delivery_payload(perception, receipt),
        )

    async def _build_recent_subconscious_background(self) -> str:
        """Return optional bounded continuity context, never witness evidence."""

        getter = getattr(
            self._service,
            "get_recent_subconscious_context",
            None,
        )
        if not callable(getter):
            return ""
        wrapper_bytes = len(
            (_RECENT_SUBCONSCIOUS_PREFIX + _RECENT_SUBCONSCIOUS_SUFFIX).encode(
                "utf-8"
            )
        )
        content_budget = _RECENT_SUBCONSCIOUS_CONTEXT_MAX_BYTES - wrapper_bytes
        try:
            projection = getter(max_bytes=content_budget)
            if inspect.isawaitable(projection):
                projection = await projection
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - optional read-only context
            logger.warning(
                "Memory Witness recent subconscious context unavailable: "
                f"error_type={type(exc).__name__}"
            )
            return ""
        if projection is None:
            return ""

        content = str(getattr(projection, "content", "") or "")
        if not content:
            return ""
        encoded = content.encode("utf-8")
        declared_sha256 = str(
            getattr(projection, "projection_sha256", "") or ""
        )
        try:
            declared_bytes = int(
                getattr(projection, "delivered_bytes", -1)
            )
        except (TypeError, ValueError):
            declared_bytes = -1
        valid = bool(
            declared_bytes == len(encoded)
            and len(encoded) <= content_budget
            and declared_sha256 == hashlib.sha256(encoded).hexdigest()
            and str(getattr(projection, "algorithm_version", "") or "")
            and tuple(getattr(projection, "event_ids", ()) or ())
        )
        if not valid:
            logger.warning(
                "Memory Witness recent subconscious context rejected: "
                "error_type=RecentSubconsciousProjectionInvalid"
            )
            return ""
        wrapped = (
            _RECENT_SUBCONSCIOUS_PREFIX
            + content
            + _RECENT_SUBCONSCIOUS_SUFFIX
        )
        if len(wrapped.encode("utf-8")) > _RECENT_SUBCONSCIOUS_CONTEXT_MAX_BYTES:
            raise RuntimeError("MemoryWitnessRecentSubconsciousBudgetExceeded")
        return wrapped

    @staticmethod
    def _world_delivery_payload(
        perception: Any,
        receipt: PerceptionDeliveryReceipt,
    ) -> dict[str, Any]:
        return {
            "proof_state": "exact_final_attempt",
            "checkpoint": {
                "instance_id": str(perception.instance_id),
                "from_position": int(perception.from_position),
                "through_position": int(perception.through_position),
                "cursor_revision": int(perception.cursor_revision),
                "delivery_id": str(perception.delivery_id),
                "projection_sha256": str(perception.projection_sha256),
                "delivered_bytes": int(perception.delivered_bytes),
            },
            "receipt": {
                "delivery_id": receipt.delivery_id,
                "projection_sha256": receipt.projection_sha256,
                "delivered_bytes": receipt.delivered_bytes,
                "exact": bool(receipt.exact),
                "transport_request_id": receipt.transport_request_id,
            },
        }

    async def _build_system_prompt(self, instance: ConsciousnessInstance) -> str:
        """Use the shared traceable SOUL+USER+MEMORY projection only."""

        getter = getattr(
            self._service,
            "get_subject_context_projection_snapshot",
            None,
        )
        if not callable(getter):
            raise RuntimeError("MemoryWitnessSubjectProjectionUnavailable")
        try:
            snapshot = await getter(
                projection_kind="memory_witness",
                max_bytes=_SUBJECT_CONTEXT_MAX_BYTES,
            )
        except RuntimeError as projection_error:
            # 双实例共享投影时，只读节点从远端恢复版本可能失败（manifest
            # profile/digest 竞争，如 "projection manifest profile is
            # incompatible"）。这是可恢复的外部依赖失败：本轮跳过 authoring、
            # 不推进游标，由上层按可恢复路径静默重试。
            raise MemoryWitnessAuthoringProjectionUnavailable(
                f"MemoryWitnessAuthoringProjectionUnavailable: "
                f"{type(projection_error).__name__}: {projection_error}"
            ) from projection_error
        if not isinstance(snapshot, dict):
            raise RuntimeError("MemoryWitnessSubjectProjectionInvalid")
        text = str(snapshot.get("text") or "")
        source_digest = str(snapshot.get("source_digest") or "").lower()
        projection_sha256 = str(snapshot.get("projection_sha256") or "").lower()
        projection_version = snapshot.get("projection_version")
        if (
            len(source_digest) != 64
            or any(char not in "0123456789abcdef" for char in source_digest)
            or not isinstance(projection_version, int)
            or isinstance(projection_version, bool)
            or projection_version < 1
            or len(projection_sha256) != 64
            or projection_sha256 != hashlib.sha256(text.encode("utf-8")).hexdigest()
            or f"source_digest: `{source_digest}`" not in text
            or f"projection_version: `{projection_version}`" not in text
        ):
            raise RuntimeError("MemoryWitnessSubjectProjectionTraceInvalid")
        source_cursor = 0
        for path in ("SOUL.md", "USER.md", "MEMORY.md"):
            opening = '<subject-source path="' + path + '">'
            closing = "</subject-source>"
            if text.count(opening) != 1:
                raise RuntimeError("MemoryWitnessSubjectProjectionCoverageInvalid")
            start = text.find(opening, source_cursor)
            end = text.find(closing, start + len(opening))
            if start < source_cursor or end < 0:
                raise RuntimeError("MemoryWitnessSubjectProjectionCoverageInvalid")
            source_cursor = end + len(closing)
        self._last_subject_projection = {
            "source_digest": source_digest,
            "projection_version": projection_version,
            "projection_sha256": projection_sha256,
            "max_bytes": _SUBJECT_CONTEXT_MAX_BYTES,
        }
        return f"""{text}

# 当前意识实例
- instance_id: {instance.instance_id}
- 你是爱莉在异步时刻回望经历的一个意识实例，不是外部总结器，也不是另一个人格。

# 见证边界
1. 用第一人称写你如何经历、感受和理解，不要伪装成客观全知记录。
2. 只依据给出的经历事件，不补造未出现的对话、动机、关系或结果。
3. 可以保留犹豫、不确定、矛盾和未完成感，不必强行得出结论。
4. 区分“发生了什么”“我当时如何感受”“我现在如何理解”。
5. 后续理解不会删除这篇见证，而会成为可追溯的认识历史。
6. 只输出自然的日记正文，不要标题、标签、JSON 或说明文字。
""".strip()

    @staticmethod
    def _format_occurrence(item: ExperienceOccurrenceRef) -> str:
        record = item.experience
        return (
            f"[{record.occurred_at}] occurrence_id={item.occurrence_id} "
            f"canonical_event_id={item.canonical_event_id} "
            f"source_event_id={item.source_event_id or item.canonical_event_id} "
            f"ingest_position={item.ingest_position} channel={record.channel} "
            f"type={record.event_type} actor={record.actor or '-'}\n"
            f"{record.content}"
        )

    @classmethod
    def _format_experience_window(
        cls,
        records: Sequence[ExperienceOccurrenceRef],
    ) -> str:
        return "\n\n".join(cls._format_occurrence(item) for item in records)

    @staticmethod
    def _projection_path_for_window(window: WitnessWindow) -> str:
        occurred = _parse_time(window.occurrences[-1].experience.occurred_at)
        identity = window.window_id.removeprefix("window-")[:16]
        return (
            f"diaries/witness/{occurred:%Y-%m}/{occurred:%Y-%m-%d}/"
            f"{window.start_position:012d}-{window.end_position:012d}-{identity}.md"
        )

    @staticmethod
    def _projection_path(records: Sequence[ExperienceRecord]) -> str:
        occurred = _parse_time(records[-1].occurred_at)
        stream = str(records[0].stream_id or "global")
        scope_hash = hashlib.sha256(stream.encode("utf-8")).hexdigest()[:10]
        start = int(records[0].sequence)
        end = int(records[-1].sequence)
        return (
            f"diaries/witness/{occurred:%Y-%m}/{occurred:%Y-%m-%d}/"
            f"{start:012d}-{end:012d}-{scope_hash}.md"
        )

    async def _project_witness(self, witness: WitnessMemory) -> None:
        memory = self._service.memory_service
        if memory is None:
            raise RuntimeError("MemoryServiceUnavailable")
        path = witness.projection_path
        if not path:
            raise ValueError("WitnessProjectionPathMissing")
        body = self._render_projection(witness)
        absolute = self._service._workspace_dir() / path
        try:
            if bool(
                getattr(
                    self._service,
                    "selected_subject_storage_enabled",
                    False,
                )
            ):
                subject_commit = await self._service.write_selected_subject_document(
                    workspace_relative_path=path,
                    content_bytes=body.encode("utf-8"),
                    occurrence_id=witness.witness_id,
                    recorded_by=witness.consciousness_instance_id,
                    recorded_source="memory-witness",
                    encoding="utf-8",
                    semantic_actor_id=witness.consciousness_instance_id,
                    semantic_source_id=witness.witness_id,
                    reason="project immutable first-person witness",
                )
                if subject_commit is None:
                    raise RuntimeError("SelectedWitnessSubjectWriteNotHandled")
                source_mtime = None
            else:
                await asyncio.to_thread(_atomic_write_text, absolute, body)
                source_mtime = await asyncio.to_thread(lambda: absolute.stat().st_mtime)
            await memory.upsert_document(
                path,
                body,
                title=f"第一人称经历见证 {witness.recorded_at[:16]}",
                source_mtime=source_mtime,
            )
            await memory.mark_witness_projection(
                witness.witness_id,
                projection_path=path,
                status="complete",
            )
        except Exception as exc:
            await memory.mark_witness_projection(
                witness.witness_id,
                projection_path=path,
                status="failed",
                error=type(exc).__name__,
            )
            raise

    @staticmethod
    def _render_projection(witness: WitnessMemory) -> str:
        source_ids = ", ".join(witness.source_event_ids)
        return f"""---
witness_id: {witness.witness_id}
author_consciousness: {witness.consciousness_instance_id}
epistemic_kind: {witness.epistemic_kind}
status: {witness.status}
recorded_at: {witness.recorded_at}
valid_from: {witness.valid_from}
valid_to: {witness.valid_to}
stream_scope: {witness.stream_scope}
visibility: {witness.visibility}
source_event_ids: [{source_ids}]
---

{witness.content}
"""


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value).astimezone()
    except (TypeError, ValueError):
        return datetime.now(UTC).astimezone()


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


__all__ = [
    "MEMORY_WITNESS_INSTANCE_ID",
    "MemoryWitnessCoordinator",
    "WitnessRunReport",
]
