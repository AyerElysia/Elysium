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

from src.app.plugin_system.api.llm_api import get_model_set_by_task
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import ROLE, LLMPayload, LLMRequest, Text
from src.kernel.llm.exceptions import (
    LLMAPIError,
    is_transient_llm_error,
)

from ..memory.experience import EpistemicKind, ExperienceRecord, WitnessMemory
from .consciousness import ConsciousnessInstance
from .event_bus import LifeEvent, RawEventGapError
from .world_state import PerceptionFilter

if TYPE_CHECKING:
    from .core import LifeEngineService

logger = get_logger("life_engine.memory_witness")
MEMORY_WITNESS_INSTANCE_ID = "memory_witness"
_NO_WITNESS = "<no_witness>"
_TRANSIENT_ERROR_ESCALATION_COUNT = 3

# Every retained life event remains available as experience evidence.  The
# witness consciousness, not a code whitelist, decides what deserves a diary.
def _transient_error_summary(exc: BaseException) -> str:
    """Describe an upstream failure without dumping response bodies or traces."""

    details = [type(exc).__name__]
    if isinstance(exc, LLMAPIError):
        if exc.status_code is not None:
            details.append(f"status={exc.status_code}")
        if exc.error_code:
            details.append(f"code={exc.error_code}")
    if len(details) == 1:
        return details[0]
    return f"{details[0]}({', '.join(details[1:])})"


@dataclass(frozen=True, slots=True)
class WitnessRunReport:
    synced_experiences: int = 0
    considered_events: int = 0
    written_witnesses: tuple[str, ...] = ()
    skipped_scopes: tuple[str, ...] = ()
    last_sequence: int = 0


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
            if existing.status == "suspended":
                await self._service.resume_consciousness_instance(
                    MEMORY_WITNESS_INSTANCE_ID,
                    timestamp=now,
                )
            await self._service.touch_consciousness_instance(
                MEMORY_WITNESS_INSTANCE_ID,
                timestamp=now,
            )
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
        await self._service.register_consciousness_instance(instance)
        return instance

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
            except Exception as exc:
                await self._record_error(exc)
                if not is_transient_llm_error(exc):
                    transient_failures = 0
                    logger.exception("记忆见证意识运行失败")
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
                    logger.error(message)
                elif transient_failures == 1:
                    logger.warning(message)
                else:
                    logger.debug(message)
            else:
                if transient_failures:
                    logger.info(
                        "记忆见证上游已恢复: "
                        f"previous_failures={transient_failures}"
                    )
                transient_failures = 0

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

            written: list[str] = []
            skipped: list[str] = []
            if experiences:
                for scope, items in self._group_by_stream(experiences):
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
            await self._service.touch_consciousness_instance(
                instance.instance_id,
                timestamp=now,
            )
            return WitnessRunReport(
                synced_experiences=synced,
                considered_events=len(raw_events),
                written_witnesses=tuple(written),
                skipped_scopes=tuple(skipped),
                last_sequence=max_sequence,
            )

    async def _migrate_legacy_diaries(self) -> None:
        cfg = self.config
        memory = self._service.memory_service
        if memory is None or not bool(
            getattr(cfg, "migrate_legacy_diaries", True)
        ):
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
            LLMPayload(ROLE.SYSTEM, Text(self._build_system_prompt(instance)))
        )
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(
                    "请回望下面这段已经发生并被保存的经历，写下你此刻愿意留下的"
                    "第一人称见证。如果没有值得留下的主观感受，只输出 "
                    f"{_NO_WITNESS}。\n\n"
                    "<transient_world_perception>\n"
                    f"{perception.content}\n"
                    "</transient_world_perception>\n\n"
                    f"{self._format_experience_window(records)}"
                ),
            )
        )
        timeout = max(10.0, float(getattr(cfg, "timeout_seconds", 120.0)))
        response = await asyncio.wait_for(request.send(), timeout=timeout)
        result = await response if not response.message else response.message
        await self._service.commit_perception(perception)
        text = str(result or "").strip().replace("**", "").replace("```", "")
        if not text or _NO_WITNESS in text.lower():
            return ""
        return text

    def _build_system_prompt(self, instance: ConsciousnessInstance) -> str:
        workspace = self._service._workspace_dir()
        soul = self._service._read_workspace_text(workspace, "SOUL.md")
        user = self._service._read_workspace_text(workspace, "USER.md")
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
