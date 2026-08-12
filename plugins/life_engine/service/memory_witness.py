"""Timed first-person witness consciousness for Life Engine memory.

The witness reads only the append-only raw event store. It does not enter or
copy another consciousness instance's rolling context. Its diary is subjective
testimony linked to immutable source events, never an objective-truth override.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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

from ..memory.experience import EpistemicKind, ExperienceRecord, WitnessMemory
from .consciousness import ConsciousnessInstance
from .event_bus import LifeEvent, RawEventGapError
from .perception_gateway import PerceptionDeliveryReceipt
from plugins.life_engine.service.presence_store import PresenceRevisionConflict
from .world_state import PerceptionFilter

if TYPE_CHECKING:
    from .core import LifeEngineService

logger = get_logger("life_engine.memory_witness")
MEMORY_WITNESS_INSTANCE_ID = "memory_witness"
_NO_WITNESS = "<no_witness>"
_TRANSIENT_ERROR_ESCALATION_COUNT = 3
_CONCURRENCY_ERROR_ESCALATION_COUNT = 3
_MYSQL_LOST_CONNECTION_ERROR_CODE = 2013

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


def _exact_perception_receipt(
    response: Any,
    perception: Any,
) -> PerceptionDeliveryReceipt | None:
    """Map the final successful LLM attempt receipt to one World delivery."""

    lookup = getattr(response, "effective_context_receipt", None)
    effective = lookup(str(perception.delivery_id)) if callable(lookup) else None
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
        delivery_id=str(perception.delivery_id),
        projection_sha256=str(perception.projection_sha256),
        delivered_bytes=int(perception.delivered_bytes),
        exact=True,
        transport_request_id=str(getattr(response, "request_record_id", "") or ""),
    )


class MemoryWitnessCoordinator:
    """Coordinate a periodic consciousness instance over immutable evidence."""

    def __init__(self, service: LifeEngineService) -> None:
        self._service = service
        self._run_lock = asyncio.Lock()

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
            live = self._service.consciousness_registry.get(
                MEMORY_WITNESS_INSTANCE_ID
            )
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
                if isinstance(exc, (PresenceRevisionConflict, CursorConflict)):
                    transient_failures = 0
                    concurrency_failures += 1
                    next_delay = retry_delay
                    if isinstance(exc, PresenceRevisionConflict):
                        await self._refresh_presence_snapshot_safely()
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
            instance = await self.ensure_instance()
            memory = self._service.memory_service
            cfg = self.config
            if memory is None or cfg is None:
                return WitnessRunReport()

            await self._migrate_legacy_diaries()
            await self._retry_pending_projections()
            state = await memory.get_witness_state(instance.instance_id)
            limit = max(1, int(getattr(cfg, "max_events_per_run", 80)))
            store = self._service._get_life_event_store()
            get_offset = getattr(store, "get_consumer_offset", None)
            if callable(get_offset):
                cursor = int(await get_offset(instance.instance_id))
            else:
                cursor = int(state.get("last_sequence", 0) or 0)
            try:
                raw_events = await store.read_since(cursor, limit=limit)
            except RawEventGapError as gap:
                raise RuntimeError(
                    "MemoryWitnessRawLedgerGap: refusing to skip missing life "
                    f"history after={gap.requested_sequence} "
                    f"earliest={gap.earliest_available}"
                ) from gap
            if not raw_events:
                await memory.update_witness_state(
                    instance.instance_id,
                    last_sequence=cursor,
                    last_run_at=_now_iso(),
                    last_error="",
                    expected_sequence=int(state.get("last_sequence", 0) or 0),
                    expected_revision=int(state.get("revision", 0) or 0),
                )
                return WitnessRunReport(last_sequence=cursor)

            # 游标推进：无论事件是否有心理意义，游标都必须前进，
            # 否则见证意识会被操作噪音永远困在原地。
            max_sequence = max(event.sequence for event in raw_events)

            candidates = [self._to_experience(event) for event in raw_events]
            append_detailed = getattr(memory, "append_experiences_detailed", None)
            if callable(append_detailed):
                append_report = await append_detailed(candidates)
                # Author from the canonical ledger rows, including rows that
                # were inserted by an earlier failed attempt.  Advancing only
                # from ``inserted`` would lose the subjective witness when the
                # experience append succeeded but the model/projection failed:
                # the retry would see every row as existing and silently move
                # the durable cursor past an unwitnessed window.
                experiences = [
                    *append_report.inserted,
                    *append_report.existing,
                ]
                synced = int(append_report.inserted_count)
            else:
                synced = await memory.append_experiences(candidates)
                experiences = candidates

            witnessable_experiences = [
                item
                for item in experiences
                if not self._is_self_presence_side_effect(
                    item,
                    instance_id=instance.instance_id,
                )
            ]
            suppressed_self_echo_events = len(experiences) - len(
                witnessable_experiences
            )

            written: list[str] = []
            skipped: list[str] = []
            if witnessable_experiences:
                for scope, items in self._group_by_stream(witnessable_experiences):
                    projection_path = self._projection_path(items)
                    existing = await memory.get_witness_by_projection_path(
                        projection_path
                    )
                    if existing is not None:
                        await self._project_witness(existing)
                        written.append(existing.witness_id)
                        continue
                    text = await self._author_witness(instance, items)
                    if not text:
                        skipped.append(scope)
                        continue
                    witness = await memory.record_witness_memory(
                        content=text,
                        consciousness_instance_id=instance.instance_id,
                        perspective_subject_id="elysia",
                        epistemic_kind=EpistemicKind.SUBJECTIVE_WITNESS.value,
                        source_kind="experience_window",
                        stream_scope=scope,
                        visibility="private",
                        valid_from=items[0].occurred_at,
                        valid_to=items[-1].occurred_at,
                        source_event_ids=[item.event_id for item in items],
                        source_sequence_start=items[0].sequence,
                        source_sequence_end=items[-1].sequence,
                        model_task_name=str(
                            getattr(cfg, "model_task_name", "witness") or "witness"
                        ),
                        projection_path=projection_path,
                        metadata={
                            "author_kind": "consciousness_instance",
                            "factual_anchor": "memory_experiences",
                            "subjective": True,
                        },
                    )
                    await self._project_witness(witness)
                    written.append(witness.witness_id)

            now = _now_iso()
            commit_offset = getattr(store, "commit_consumer_offset", None)
            if callable(commit_offset):
                await commit_offset(
                    instance.instance_id,
                    max_sequence,
                    metadata={"witness_state_mirror": True},
                )
            await memory.update_witness_state(
                instance.instance_id,
                last_sequence=max_sequence,
                last_run_at=now,
                last_success_at=now,
                last_error="",
                expected_sequence=int(state.get("last_sequence", 0) or 0),
                expected_revision=int(state.get("revision", 0) or 0),
            )
            await self._touch_presence_after_commit(
                instance.instance_id,
                timestamp=now,
            )
            return WitnessRunReport(
                synced_experiences=synced,
                considered_events=len(raw_events),
                suppressed_self_echo_events=suppressed_self_echo_events,
                written_witnesses=tuple(written),
                skipped_scopes=tuple(skipped),
                last_sequence=max_sequence,
            )

    async def _migrate_legacy_diaries(self) -> None:
        cfg = self.config
        memory = self._service.memory_service
        if memory is None or not bool(getattr(cfg, "migrate_legacy_diaries", True)):
            return
        from .legacy_diary import migrate_legacy_diaries

        source = Path(
            str(getattr(cfg, "legacy_diary_path", "data/diaries") or "data/diaries")
        )
        if not source.is_absolute():
            source = Path.cwd() / source
        migrated = await migrate_legacy_diaries(memory, source)
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

    async def _retry_pending_projections(self) -> None:
        memory = self._service.memory_service
        if memory is None:
            return
        pending = await memory.list_pending_witness_projections(limit=20)
        for witness in pending:
            await self._project_witness(witness)

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
    def _group_by_stream(
        records: Sequence[ExperienceRecord],
    ) -> list[tuple[str, list[ExperienceRecord]]]:
        buckets: dict[str, list[ExperienceRecord]] = {}
        for record in records:
            buckets.setdefault(str(record.stream_id or ""), []).append(record)
        return [
            (scope, sorted(items, key=lambda item: (item.sequence, item.event_id)))
            for scope, items in sorted(buckets.items())
        ]

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
        records: Sequence[ExperienceRecord],
    ) -> str:
        cfg = self.config
        task_name = str(getattr(cfg, "model_task_name", "witness") or "witness")
        model_set = get_model_set_by_task(task_name)
        if not model_set:
            raise RuntimeError(f"MemoryWitnessModelUnavailable:{task_name}")
        perception = await self._service.prepare_perception(instance.instance_id)
        request = LLMRequest(model_set, "life_memory_witness")
        request.add_payload(
            LLMPayload(ROLE.SYSTEM, Text(await self._build_system_prompt(instance)))
        )
        user_text = (
            "请回望下面这段已经发生并被保存的经历，写下你此刻愿意留下的"
            "第一人称见证。如果没有值得留下的主观感受，只输出 "
            f"{_NO_WITNESS}。\n\n"
            "<transient_world_perception>\n"
            f"{perception.content}\n"
            "</transient_world_perception>\n\n"
            f"{self._format_experience_window(records)}"
        )
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(user_text),
            )
        )
        request.register_context_delivery(
            str(perception.delivery_id),
            user_text,
            marker=str(perception.delivery_marker),
        )
        timeout = max(10.0, float(getattr(cfg, "timeout_seconds", 600.0)))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        timeout_scope = asyncio.timeout_at(deadline)
        try:
            async with timeout_scope:
                response = await request.send()
                result = await response if not response.message else response.message
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
        await self._service.commit_perception(perception, receipt)
        text = str(result or "").strip().replace("**", "").replace("```", "")
        if not text or _NO_WITNESS in text.lower():
            return ""
        return text

    async def _build_system_prompt(self, instance: ConsciousnessInstance) -> str:
        # 见证实例也是同一主体的运行窗口，权威文本必须与表达层同源。
        # 选定后端下只读远端单事务快照，远端缺口失败关闭而不是退回本地。
        texts = await self._service.read_subject_authority_texts()
        soul = texts.get("SOUL.md", "").strip()
        user = texts.get("USER.md", "").strip()
        if not soul:
            raise RuntimeError("MemoryWitnessSoulUnavailable")
        return f"""{soul}

---

{user}

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
    def _format_experience_window(records: Sequence[ExperienceRecord]) -> str:
        lines = []
        for item in records:
            content = " ".join(str(item.content or "").split())
            lines.append(
                f"[{item.occurred_at}] occurrence_id={item.event_id} "
                f"source_event_id={item.source_event_id or item.event_id} "
                f"channel={item.channel} type={item.event_type} "
                f"actor={item.actor or '-'}\n{content}"
            )
        return "\n\n".join(lines)

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
