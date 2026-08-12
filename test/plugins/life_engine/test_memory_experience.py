"""Life Engine 生命记忆本体与见证意识回归测试。"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.memory import tools as memory_tools
from plugins.life_engine.memory.experience import (
    EpistemicKind,
    EvidenceAwareMemoryResult,
    ExperienceOccurrenceRef,
    ExperienceRecord,
    MemorySearchMode,
    WitnessMemory,
    create_life_memory_schema,
    get_witness_state,
    insert_experiences,
    insert_witness_memory,
    migrate_legacy_witness,
    search_witness_memories,
    update_witness_state,
)
from plugins.life_engine.memory.lineage import MemoryBundle, MemoryEvidence
from plugins.life_engine.memory.recall_delivery import (
    get_memory_search_recall_delivery_coordinator,
)
from plugins.life_engine.memory.tools import (
    MEMORY_SEARCH_CORE_MAX_BYTES,
    MEMORY_SEARCH_EXPRESSION_MAX_BYTES,
    MEMORY_SEARCH_PROJECTION_VERSION,
    LifeEngineSearchMemoryTool,
)
from plugins.life_engine.service.consciousness import (
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from plugins.life_engine.service.legacy_diary import parse_legacy_diary_file
from plugins.life_engine.service.memory_witness import (
    MEMORY_WITNESS_INSTANCE_ID,
    MemoryWitnessCoordinator,
)
from plugins.life_engine.service.tool_manifests import get_tool_manifest
from src.kernel.llm.context_delivery import EffectiveContextReceipt
from src.kernel.llm.exceptions import LLMAPIError, LLMModelsCoolingDownError
from src.kernel.llm.payload import ToolResult
from src.kernel.storage import CursorConflict


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    create_life_memory_schema(db)
    return db


def _experience(event_id: str = "event-1", sequence: int = 1) -> ExperienceRecord:
    return ExperienceRecord(
        event_id=event_id,
        sequence=sequence,
        occurred_at="2026-07-29T08:00:00+08:00",
        recorded_at="2026-07-29T08:01:00+08:00",
        source="chat",
        channel="chat",
        event_type="message",
        content="我记得这段真实经历。",
        stream_id="stream-1",
    )


def _occurrence(record: ExperienceRecord | None = None) -> ExperienceOccurrenceRef:
    experience = record or _experience()
    payload_sha256 = hashlib.sha256(experience.content.encode("utf-8")).hexdigest()
    return ExperienceOccurrenceRef(
        occurrence_id=experience.event_id,
        source_event_id=experience.source_event_id or experience.event_id,
        ingest_position=experience.sequence,
        canonical_event_id=experience.event_id,
        canonical_payload_sha256=payload_sha256,
        recorded_at=experience.recorded_at,
        experience=experience,
    )


def test_experience_ledger_is_idempotent_and_immutable() -> None:
    db = _db()
    record = _experience()

    assert insert_experiences(db, [record]) == 1
    assert insert_experiences(db, [record]) == 0
    with pytest.raises(ValueError, match="ExperienceIdentityConflict:event-1"):
        insert_experiences(db, [replace(record, content="冲突的事件内容")])

    with pytest.raises(sqlite3.IntegrityError, match="ExperienceLedgerImmutable"):
        db.execute(
            "UPDATE memory_experiences SET content = 'changed' WHERE event_id = ?",
            (record.event_id,),
        )
    db.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="ExperienceLedgerImmutable"):
        db.execute(
            "DELETE FROM memory_experiences WHERE event_id = ?",
            (record.event_id,),
        )


def test_witness_requires_existing_source_event() -> None:
    db = _db()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert_witness_memory(
            db,
            content="我记得。",
            consciousness_instance_id=MEMORY_WITNESS_INSTANCE_ID,
            perspective_subject_id="elysia",
            epistemic_kind=EpistemicKind.SUBJECTIVE_WITNESS.value,
            source_kind="experience_window",
            stream_scope="stream-1",
            visibility="private",
            valid_from="2026-07-29T08:00:00+08:00",
            valid_to="2026-07-29T08:00:00+08:00",
            source_event_ids=["missing"],
        )


def test_witness_state_mirror_uses_monotonic_position_revision_cas() -> None:
    db = _db()
    state = update_witness_state(
        db,
        "memory_witness",
        last_sequence=5,
        expected_sequence=0,
        expected_revision=0,
    )
    assert state["last_sequence"] == 5
    assert state["revision"] == 1

    with pytest.raises(CursorConflict, match="expected 0, actual 5"):
        update_witness_state(
            db,
            "memory_witness",
            last_sequence=6,
            expected_sequence=0,
            expected_revision=0,
        )
    with pytest.raises(CursorConflict, match="cannot regress"):
        update_witness_state(
            db,
            "memory_witness",
            last_sequence=4,
            expected_sequence=5,
            expected_revision=1,
        )

    metadata_only = update_witness_state(
        db,
        "memory_witness",
        last_run_at="2026-08-04T12:00:00+08:00",
        expected_sequence=5,
        expected_revision=1,
    )
    assert metadata_only["last_sequence"] == 5
    assert metadata_only["revision"] == 1
    assert get_witness_state(db, "memory_witness") == metadata_only


def test_witness_search_keeps_rank_separate_from_truth() -> None:
    db = _db()
    record = _experience()
    insert_experiences(db, [record])
    witness = insert_witness_memory(
        db,
        content="我记得这段真实经历。",
        consciousness_instance_id=MEMORY_WITNESS_INSTANCE_ID,
        perspective_subject_id="elysia",
        epistemic_kind=EpistemicKind.SUBJECTIVE_WITNESS.value,
        source_kind="experience_window",
        stream_scope="stream-1",
        visibility="private",
        valid_from=record.occurred_at,
        valid_to=record.occurred_at,
        source_event_ids=[record.event_id],
    )

    results = search_witness_memories(
        db,
        "真实经历",
        mode=MemorySearchMode.CURRENT_FACT,
        stream_scope="stream-1",
    )

    assert results[0].witness.witness_id == witness.witness_id
    assert results[0].rank_score > 0
    assert "not objective truth" in results[0].epistemic_note
    assert "corroboration required" in results[0].epistemic_note


def test_private_witness_requires_matching_stream_scope() -> None:
    db = _db()
    record = _experience()
    insert_experiences(db, [record])
    insert_witness_memory(
        db,
        content="我记得这段真实经历。",
        consciousness_instance_id=MEMORY_WITNESS_INSTANCE_ID,
        perspective_subject_id="elysia",
        epistemic_kind=EpistemicKind.SUBJECTIVE_WITNESS.value,
        source_kind="experience_window",
        stream_scope="stream-1",
        visibility="private",
        valid_from=record.occurred_at,
        valid_to=record.occurred_at,
        source_event_ids=[record.event_id],
    )

    assert search_witness_memories(
        db,
        "我记得这段真实经历",
        visibility=("private",),
    ) == []
    assert search_witness_memories(
        db,
        "我记得这段真实经历",
        stream_scope="stream-2",
        visibility=("private",),
    ) == []


def test_legacy_migration_is_atomic_and_idempotent() -> None:
    db = _db()
    kwargs = {
        "migration_key": "legacy-key",
        "source_path": "2026-03/2026-03-18.md",
        "source_hash": "source-hash",
        "content": "旧日记里的主观见证。",
        "valid_from": "2026-03-18T01:39:00+08:00",
        "recorded_at": "2026-03-18T01:39:00+08:00",
    }

    first = migrate_legacy_witness(db, **kwargs)
    second = migrate_legacy_witness(db, **kwargs)

    assert first is not None
    assert first.epistemic_kind == EpistemicKind.LEGACY_WITNESS.value
    assert first.source_event_ids == ()
    assert second is None
    assert db.execute("SELECT COUNT(*) FROM memory_witnesses").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM memory_witness_migrations").fetchone()[0] == 1


def test_legacy_parser_handles_adjacent_and_multiline_entries(tmp_path: Path) -> None:
    root = tmp_path / "diaries"
    path = root / "2026-03" / "2026-03-18.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "**[01:39]** 第一条。**[01:45]** 第二条第一行。\n第二行。\n",
        encoding="utf-8",
    )

    entries = parse_legacy_diary_file(path, root=root)

    assert [item.content for item in entries] == ["第一条。", "第二条第一行。\n第二行。"]
    assert entries[0].valid_from == "2026-03-18T01:39:00"
    assert entries[0].migration_key != entries[1].migration_key


def test_legacy_diary_entry_identity_survives_unrelated_file_edits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "diaries"
    root.mkdir()
    path = root / "2026-07-29.md"
    path.write_text(
        "**[08:00]** first memory\n\n**[09:00]** second memory\n",
        encoding="utf-8",
    )
    before = parse_legacy_diary_file(path, root=root)

    path.write_text(
        "header added later\n\n**[08:00]** first memory\n\n"
        "**[09:00]** second memory\n",
        encoding="utf-8",
    )
    after = parse_legacy_diary_file(path, root=root)

    assert [item.migration_key for item in before] == [
        item.migration_key for item in after
    ]
    assert before[0].source_hash != after[0].source_hash


@pytest.mark.asyncio
async def test_memory_witness_is_registered_as_consciousness_without_tools(
    tmp_path: Path,
) -> None:
    registry = ConsciousnessRegistry()

    async def register(instance: ConsciousnessInstance) -> ConsciousnessInstance:
        return registry.register(instance)

    service = SimpleNamespace(
        consciousness_registry=registry,
        register_consciousness_instance=register,
        _cfg=lambda: SimpleNamespace(memory_witness=SimpleNamespace(enabled=True)),
    )

    instance = await MemoryWitnessCoordinator(service).ensure_instance()

    assert instance.instance_id == MEMORY_WITNESS_INSTANCE_ID
    assert instance.kind == "memory_witness"
    assert instance.metadata["epistemic_boundary"] == (
        "subjective_witness_not_objective_truth"
    )
    assert get_tool_manifest("memory_witness") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt_state", "expected_request_id"),
    (
        ("exact", "42"),
        ("failover_exact", "84"),
        ("trimmed", None),
        ("mismatched", None),
    ),
)
async def test_memory_witness_commits_only_with_final_exact_context_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_state: str,
    expected_request_id: str | None,
) -> None:
    world_content = "world-perception:witness-world\ncurrent world"
    world_sha256 = hashlib.sha256(world_content.encode("utf-8")).hexdigest()
    perception = SimpleNamespace(
        instance_id=MEMORY_WITNESS_INSTANCE_ID,
        from_position=2,
        through_position=5,
        cursor_revision=7,
        delivery_id="witness-world",
        delivery_marker="world-perception:witness-world",
        content=world_content,
        projection_sha256=world_sha256,
        delivered_bytes=len(world_content.encode("utf-8")),
    )
    requests: list[object] = []
    send_streams: list[bool] = []
    attempt_outcomes: list[str] = []
    source_digest = "a" * 64
    subject_text = f"""# Subject Context Projection

- source_digest: `{source_digest}`
- projection_version: `3`

<subject-source path="SOUL.md">
SOUL authority projection
</subject-source>

<subject-source path="USER.md">
USER authority projection
</subject-source>

<subject-source path="MEMORY.md">
MEMORY authority projection
</subject-source>"""
    subject_snapshot = {
        "text": subject_text,
        "source_digest": source_digest,
        "projection_version": 3,
        "projection_sha256": hashlib.sha256(
            subject_text.encode("utf-8")
        ).hexdigest(),
    }

    class _Response:
        message = "我愿意记住这一刻。"

        def __init__(self, request: object) -> None:
            self._request = request
            self.request_record_id = 84 if receipt_state == "failover_exact" else 42

        def __await__(self):
            async def _done() -> str:
                return self.message

            return _done().__await__()

        def effective_context_receipt(self, delivery_id: str) -> object:
            assert delivery_id == perception.delivery_id
            expected_text = self._request.expected_text
            encoded = expected_text.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            exact = receipt_state in {"exact", "failover_exact", "mismatched"}
            return SimpleNamespace(
                delivery_id=(
                    "another-delivery"
                    if receipt_state == "mismatched"
                    else perception.delivery_id
                ),
                exact_present=exact,
                expected_utf8_bytes=len(encoded),
                expected_sha256=digest,
                effective_utf8_bytes=(len(encoded) if exact else None),
                effective_sha256=(digest if exact else None),
                part_kind="text",
            )

    class _Request:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.expected_text = ""
            self.payloads: list[object] = []
            requests.append(self)

        def add_payload(self, payload: object) -> None:
            self.payloads.append(payload)

        def register_context_delivery(
            self,
            delivery_id: str,
            expected_text: str,
            *,
            marker: str,
        ) -> None:
            assert delivery_id == perception.delivery_id
            assert marker == perception.delivery_marker
            assert expected_text == perception.content
            self.expected_text = expected_text

        async def send(self, *, stream: bool = True) -> _Response:
            send_streams.append(stream)
            if receipt_state == "failover_exact":
                attempt_outcomes.extend(("primary_stream_failed", "fallback_succeeded"))
            return _Response(self)

    async def _prepare(_instance_id: str) -> object:
        return perception

    async def _subject_projection(**kwargs: object) -> dict[str, object]:
        assert kwargs == {
            "projection_kind": "memory_witness",
            "max_bytes": 24 * 1024,
        }
        return subject_snapshot

    service = SimpleNamespace(
        _cfg=lambda: SimpleNamespace(
            memory_witness=SimpleNamespace(
                model_task_name="witness",
                timeout_seconds=30,
            )
        ),
        prepare_perception=_prepare,
        get_subject_context_projection_snapshot=_subject_projection,
        _workspace_dir=lambda: tmp_path,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.get_model_set_by_task",
        lambda _task: [{"model_identifier": "test"}],
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.LLMRequest",
        _Request,
    )
    coordinator = MemoryWitnessCoordinator(service)
    instance = ConsciousnessInstance(
        instance_id=MEMORY_WITNESS_INSTANCE_ID,
        kind="memory_witness",
    )

    if receipt_state in {"exact", "failover_exact"}:
        authored = await coordinator._author_witness(instance, [_occurrence()])
        assert authored.text == "我愿意记住这一刻。"
        assert authored.world_payload["proof_state"] == "exact_final_attempt"
        assert authored.world_payload["receipt"]["delivery_id"] == (
            perception.delivery_id
        )
        assert (
            authored.world_payload["receipt"]["transport_request_id"]
            == expected_request_id
        )
        assert coordinator._last_subject_projection == {
            "source_digest": source_digest,
            "projection_version": 3,
            "projection_sha256": subject_snapshot["projection_sha256"],
            "max_bytes": 24 * 1024,
        }
    else:
        with pytest.raises(
            RuntimeError,
            match="MemoryWitnessPerceptionDeliveryUnverified",
        ):
            await coordinator._author_witness(instance, [_occurrence()])
    assert len(requests) == 1
    assert send_streams == [False]
    if receipt_state == "failover_exact":
        assert attempt_outcomes == ["primary_stream_failed", "fallback_succeeded"]
    else:
        assert attempt_outcomes == []
    assert requests[0].expected_text == perception.content
    user_texts = [
        content.text
        for payload in requests[0].payloads
        for content in payload.content
        if hasattr(content, "text")
    ]
    assert user_texts[-1] == perception.content
    assert sum(perception.delivery_marker in text for text in user_texts) == 1
    assert perception.content in user_texts
    assert all(
        text == perception.content or perception.content not in text
        for text in user_texts
    )


def test_projection_path_is_deterministic_and_stream_scoped() -> None:
    first = _experience()
    second = _experience("event-2", 2)
    same = MemoryWitnessCoordinator._projection_path([first, second])
    repeated = MemoryWitnessCoordinator._projection_path([first, second])
    other = MemoryWitnessCoordinator._projection_path(
        [replace(first, event_id="event-3", stream_id="stream-2")]
    )

    assert same == repeated
    assert same != other
    assert "000000000001-000000000002" in same


def _witness_projection_record() -> WitnessMemory:
    return WitnessMemory(
        witness_id="witness-projection-1",
        content="我记得这段经历。",
        consciousness_instance_id=MEMORY_WITNESS_INSTANCE_ID,
        perspective_subject_id="elysia",
        epistemic_kind=EpistemicKind.SUBJECTIVE_WITNESS.value,
        source_kind="experience_window",
        status="active",
        stream_scope="stream-1",
        visibility="private",
        valid_from="2026-07-29T08:00:00+08:00",
        valid_to="2026-07-29T08:05:00+08:00",
        recorded_at="2026-07-29T08:06:00+08:00",
        source_sequence_start=1,
        source_sequence_end=2,
        source_event_ids=("event-1", "event-2"),
        projection_path="diaries/witness/2026-07/witness-projection-1.md",
    )


class _WitnessProjectionMemoryStub:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, str, float]] = []
        self.projections: list[tuple[str, dict[str, object]]] = []

    async def upsert_document(
        self,
        path: str,
        body: str,
        *,
        title: str,
        source_mtime: float,
    ) -> None:
        assert title.startswith("第一人称经历见证")
        self.upserts.append((path, body, source_mtime))

    async def mark_witness_projection(
        self,
        witness_id: str,
        **kwargs: object,
    ) -> None:
        self.projections.append((witness_id, kwargs))


async def test_witness_projection_uses_subject_write_ahead_when_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _WitnessProjectionMemoryStub()
    calls: list[dict[str, object]] = []

    async def _subject_writer(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        target = tmp_path / str(kwargs["workspace_relative_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(kwargs["content_bytes"]))
        return {"status": "committed"}

    service = SimpleNamespace(
        memory_service=memory,
        selected_subject_storage_enabled=True,
        write_selected_subject_document=_subject_writer,
        _workspace_dir=lambda: tmp_path,
    )
    coordinator = MemoryWitnessCoordinator(service)
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness._atomic_write_text",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy write used")),
    )

    witness = _witness_projection_record()
    await coordinator._project_witness(witness)

    assert len(calls) == 1
    assert calls[0]["occurrence_id"] == witness.witness_id
    assert calls[0]["semantic_actor_id"] == witness.consciousness_instance_id
    assert calls[0]["recorded_source"] == "memory-witness"
    assert memory.upserts[0][0] == witness.projection_path
    assert memory.projections == [
        (
            witness.witness_id,
            {
                "projection_path": witness.projection_path,
                "status": "complete",
            },
        )
    ]


async def test_witness_projection_keeps_local_atomic_write_when_disabled(
    tmp_path: Path,
) -> None:
    memory = _WitnessProjectionMemoryStub()
    service = SimpleNamespace(
        memory_service=memory,
        selected_subject_storage_enabled=False,
        _workspace_dir=lambda: tmp_path,
    )
    coordinator = MemoryWitnessCoordinator(service)
    witness = _witness_projection_record()

    await coordinator._project_witness(witness)

    assert (tmp_path / witness.projection_path).read_text(encoding="utf-8")
    assert memory.projections[0][1]["status"] == "complete"


@pytest.mark.parametrize(
    ("failure", "expected_delay", "expected_summary"),
    [
        (
            LLMAPIError(
                "upstream unavailable",
                status_code=500,
                error_code="do_request_failed",
            ),
            60,
            "LLMAPIError(status=500, code=do_request_failed)",
        ),
        (
            LLMModelsCoolingDownError(
                request_name="life_memory_witness",
                retry_after=240,
                models=("mimo-v2.5", "gpt-5.6-luna"),
            ),
            240,
            "LLMModelsCoolingDownError",
        ),
    ],
)
async def test_witness_loop_retries_transient_upstream_failure_quietly(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_delay: int,
    expected_summary: str,
) -> None:
    """A temporary model outage keeps evidence pending and retries soon."""

    config = SimpleNamespace(
        enabled=True,
        run_on_startup=True,
        interval_seconds=300,
        retry_delay_seconds=60,
    )
    service = SimpleNamespace(
        _cfg=lambda: SimpleNamespace(memory_witness=config),
        _state=SimpleNamespace(running=True),
        _stop_event=None,
    )
    coordinator = MemoryWitnessCoordinator(service)
    run_count = 0
    delays: list[float] = []
    recorded_errors: list[Exception] = []
    warnings: list[str] = []
    errors: list[str] = []
    infos: list[str] = []

    async def _run_once() -> None:
        nonlocal run_count
        run_count += 1
        if run_count == 1:
            raise failure
        service._state.running = False

    async def _record_error(exc: Exception) -> None:
        recorded_errors.append(exc)

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(coordinator, "run_once", _run_once)
    monkeypatch.setattr(coordinator, "_record_error", _record_error)
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.asyncio.sleep",
        _sleep,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.warning",
        warnings.append,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.error",
        errors.append,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.info",
        infos.append,
    )

    await coordinator.loop()

    assert run_count == 2
    assert delays == [expected_delay]
    assert len(recorded_errors) == 1
    assert errors == []
    assert len(warnings) == 1
    assert "待处理经历已保留" in warnings[0]
    assert expected_summary in warnings[0]
    assert infos == ["记忆见证上游已恢复: previous_failures=1"]


async def test_search_tool_accepts_open_retrieval_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = LifeEngineSearchMemoryTool(plugin=SimpleNamespace())
    observed: dict[str, object] = {}

    class _OpenModeService:
        async def search_memory(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

        async def search_evidence_aware(
            self,
            _query: str,
            **kwargs: object,
        ) -> list[object]:
            observed.update(kwargs)
            return []

    async def _service() -> object:
        return _OpenModeService()

    monkeypatch.setattr(tool, "_get_service", _service)

    ok, payload = await tool.execute("经历", search_mode="objective_truth")

    assert ok is True
    assert payload["search_mode"] == "objective_truth"
    assert observed["mode"] == "objective_truth"


async def test_search_tool_uses_only_canonical_living_association_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = LifeEngineSearchMemoryTool(plugin=SimpleNamespace())
    observed: dict[str, object] = {}

    class _CanonicalAssociationService:
        async def search_memory(
            self,
            *_args: object,
            **kwargs: object,
        ) -> list[object]:
            observed["legacy_enable_association"] = kwargs["enable_association"]
            return []

        async def expand_living_document_associations(
            self,
            results: list[object],
            **kwargs: object,
        ) -> list[object]:
            observed["canonical_called"] = True
            observed["canonical_limit"] = kwargs["limit"]
            return results

        async def search_evidence_aware(
            self,
            _query: str,
            **_kwargs: object,
        ) -> list[object]:
            return []

    async def _service() -> object:
        return _CanonicalAssociationService()

    monkeypatch.setattr(tool, "_get_service", _service)

    ok, _payload = await tool.execute("关联", enable_association=True, top_k=4)

    assert ok is True
    assert observed == {
        "legacy_enable_association": False,
        "canonical_called": True,
        "canonical_limit": 4,
    }


async def test_search_tool_returns_evidence_payload_without_fake_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = LifeEngineSearchMemoryTool(plugin=SimpleNamespace())
    evidence = EvidenceAwareMemoryResult(
        record_id="witness-1",
        kind=EpistemicKind.SUBJECTIVE_WITNESS.value,
        content="我记得这段经历。",
        rank_score=0.875,
        confidence=None,
        source="witness_fts",
        valid_from="2026-07-29T08:00:00+08:00",
        valid_to="2026-07-29T08:05:00+08:00",
        recorded_at="2026-07-29T08:06:00+08:00",
        stream_scope="stream-1",
        visibility="private",
        provenance=("event-1",),
        metadata={"subjective": True},
    )

    class _SearchServiceStub:
        async def search_memory(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

        async def search_evidence_aware(
            self,
            _query: str,
            *,
            mode: str,
            top_k: int,
            stream_scope: str | None,
            enable_association: bool,
            valid_at: str,
            recorded_as_of: str,
        ) -> list[EvidenceAwareMemoryResult]:
            assert mode == "autobiographical"
            assert top_k == 3
            assert stream_scope == "stream-1"
            assert enable_association is False
            assert valid_at == ""
            assert recorded_as_of == ""
            return [evidence]

    async def _service() -> object:
        return _SearchServiceStub()

    monkeypatch.setattr(tool, "_get_service", _service)

    ok, payload = await tool.execute(
        "经历",
        top_k=3,
        enable_association=False,
        search_mode="autobiographical",
        stream_scope="stream-1",
    )

    assert ok is True
    assert payload["search_mode"] == "autobiographical"
    assert payload["stream_scope"] == "stream-1"
    assert payload["valid_at"] == ""
    assert payload["recorded_as_of"] == ""
    assert payload["total_found"] == 1
    projected = payload["evidence_results"][0]
    assert projected["record_id"] == "witness-1"
    assert projected["kind"] == EpistemicKind.SUBJECTIVE_WITNESS.value
    assert projected["confidence"] is None
    assert projected["content_delivery"] == "full"
    assert "content" not in projected
    canonical = next(
        item
        for item in payload["canonical_items"]
        if item["ref"] == projected["content_ref"]
    )
    assert canonical["content"] == "我记得这段经历。"
    assert canonical["content_sha256"] == hashlib.sha256(
        "我记得这段经历。".encode("utf-8")
    ).hexdigest()
    assert projected["provenance_count"] == 1
    assert projected["metadata_bytes"] > 0
    assert payload["projection_version"] == MEMORY_SEARCH_PROJECTION_VERSION
    assert payload["delivered_bytes"] == len(str(payload).encode("utf-8"))
    assert payload["delivered_bytes"] <= MEMORY_SEARCH_CORE_MAX_BYTES


async def test_search_tool_bounds_large_unicode_results_and_exposes_only_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = LifeEngineSearchMemoryTool(plugin=SimpleNamespace())
    tool._runtime_task_name = "core"
    evidence = [
        EvidenceAwareMemoryResult(
            record_id=f"witness-{index:02d}",
            kind=EpistemicKind.SUBJECTIVE_WITNESS.value,
            content=(f"第{index}段珍贵经历。" + "爱莉♪" * 40000),
            rank_score=1.0 / (index + 1),
            confidence=None,
            source="witness_fts",
            provenance=(f"event-{index}",),
            metadata={"oversized": "元" * 20000},
        )
        for index in range(20)
    ]
    exposed_pages: list[tuple[object, ...]] = []
    corecalls: list[object] = []
    episodes: list[dict[str, object]] = []

    class _BoundedService:
        async def begin_memory_recall(self, **kwargs: object) -> SimpleNamespace:
            episodes.append(dict(kwargs))
            return SimpleNamespace(episode_id=kwargs["episode_id"])

        async def search_memory(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

        async def search_evidence_aware(
            self,
            _query: str,
            **_kwargs: object,
        ) -> list[EvidenceAwareMemoryResult]:
            return evidence

        async def append_memory_recall_events(self, events: tuple[object, ...]) -> None:
            exposed_pages.append(events)

        async def append_memory_corecall(self, event: object) -> None:
            corecalls.append(event)

    async def _service() -> object:
        return _BoundedService()

    monkeypatch.setattr(tool, "_get_service", _service)
    monkeypatch.setattr(
        memory_tools,
        "_resolve_search_recall_identity",
        lambda _tool, *, binding: SimpleNamespace(
            actor_consciousness_instance_id="consciousness-1",
            stream_scope="chat-stream",
            source_occurrence_id="life-turn-1",
            recall_chain_id="memory-search-chain-1",
            recorded_at="2026-08-13T00:00:00+00:00",
        ),
    )

    def _delivery_id(payload: dict[str, object]) -> str:
        binding = payload.get("recall_delivery_binding")
        assert isinstance(binding, dict)
        return str(binding["delivery_id"])

    def _receipt(payload: dict[str, object]) -> EffectiveContextReceipt:
        delivery_id = _delivery_id(payload)
        expected = ToolResult(
            value=payload,
            call_id="tool-call",
            name="nucleus_search_memory",
        ).to_text()
        encoded = expected.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        return EffectiveContextReceipt(
            delivery_id=delivery_id,
            exact_present=True,
            expected_utf8_bytes=len(encoded),
            expected_sha256=digest,
            effective_utf8_bytes=len(encoded),
            effective_sha256=digest,
            part_kind="tool_result",
        )

    ok, first = await tool.execute("珍贵经历")

    assert ok is True
    assert first["truncated"] is True
    assert first["continuation"]
    assert first["original_items"] == 20
    assert 0 < first["delivered_items"] < first["original_items"]
    assert first["omitted_items"] > 0
    assert first["delivered_bytes"] == len(str(first).encode("utf-8"))
    assert first["delivered_bytes"] <= MEMORY_SEARCH_CORE_MAX_BYTES
    assert any(item["delivery"] == "excerpt" for item in first["canonical_items"])
    first_refs = {item["entity_ref"] for item in first["evidence_results"]}
    assert _delivery_id(first)
    assert first["recall_episode"]["persisted"] is False
    assert first["recall_episode"]["trace_state"] == (
        "pending_exact_tool_result_delivery"
    )
    assert exposed_pages == []
    assert corecalls == []
    coordinator = get_memory_search_recall_delivery_coordinator()
    coordinator.register_pending_tool_result(
        first,
        ToolResult(
            value=first,
            call_id="tool-call",
            name="nucleus_search_memory",
        ).to_text(),
    )
    assert await coordinator.commit_exact(
        _delivery_id(first),
        _receipt(first),
    )
    assert {event.entity_ref for event in exposed_pages[0]} == first_refs
    assert all(
        event.action == "delivered_to_model_context"
        for event in exposed_pages[0]
    )
    assert {event.actor for event in corecalls} == {"consciousness-1"}

    ok, second = await tool.execute(
        "珍贵经历",
        continuation=first["continuation"],
    )

    assert ok is True
    assert second["frontier_sha256"] == first["frontier_sha256"]
    assert second["delivered_bytes"] == len(str(second).encode("utf-8"))
    assert second["delivered_bytes"] <= MEMORY_SEARCH_CORE_MAX_BYTES
    second_refs = {item["entity_ref"] for item in second["evidence_results"]}
    assert first_refs.isdisjoint(second_refs)
    assert len(exposed_pages) == 1
    coordinator.register_pending_tool_result(
        second,
        ToolResult(
            value=second,
            call_id="tool-call",
            name="nucleus_search_memory",
        ).to_text(),
    )
    assert await coordinator.commit_exact(
        _delivery_id(second),
        _receipt(second),
    )
    assert {event.entity_ref for event in exposed_pages[1]} == second_refs
    assert len({item["episode_id"] for item in episodes}) == 1


async def test_search_tool_uses_task_budget_and_deduplicates_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = "同一段正文♪" * 5000
    evidence = EvidenceAwareMemoryResult(
        record_id="document-1",
        kind="document_evidence",
        content=shared,
        rank_score=1.0,
        confidence=None,
        source="document_search",
    )
    bundle = MemoryBundle(
        query="同一内容",
        current_understanding=shared,
        primary_path="notes/shared.md",
        evidence=[
            MemoryEvidence(
                file_path="notes/shared.md",
                title="共享",
                snippet=shared,
                source="direct",
            )
        ],
    )

    class _DeduplicatedService:
        async def search_memory(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

        async def build_memory_bundles(
            self,
            **_kwargs: object,
        ) -> list[MemoryBundle]:
            return [bundle]

        async def search_evidence_aware(
            self,
            _query: str,
            **_kwargs: object,
        ) -> list[EvidenceAwareMemoryResult]:
            return [evidence]

    async def _service() -> object:
        return _DeduplicatedService()

    core = LifeEngineSearchMemoryTool(plugin=SimpleNamespace())
    core._runtime_task_name = "core"
    expression = LifeEngineSearchMemoryTool(plugin=SimpleNamespace())
    expression._runtime_task_name = "expression"
    monkeypatch.setattr(core, "_get_service", _service)
    monkeypatch.setattr(expression, "_get_service", _service)

    core_ok, core_payload = await core.execute("同一内容")
    expression_ok, expression_payload = await expression.execute("同一内容")

    assert core_ok is expression_ok is True
    assert core_payload["budget_bytes"] == MEMORY_SEARCH_CORE_MAX_BYTES
    assert expression_payload["budget_bytes"] == MEMORY_SEARCH_EXPRESSION_MAX_BYTES
    assert core_payload["delivered_bytes"] <= MEMORY_SEARCH_CORE_MAX_BYTES
    assert expression_payload["delivered_bytes"] <= MEMORY_SEARCH_EXPRESSION_MAX_BYTES
    shared_digest = hashlib.sha256(shared.encode("utf-8")).hexdigest()
    assert sum(
        item["content_sha256"] == shared_digest
        for item in expression_payload["canonical_items"]
    ) == 1
    evidence_ref = expression_payload["evidence_results"][0]["content_ref"]
    assert expression_payload["direct_results"][0]["content_ref"] == evidence_ref
    assert expression_payload["memory_bundles"][0]["current_refs"] == [evidence_ref]


async def test_search_tool_rejects_tampered_continuation_without_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = LifeEngineSearchMemoryTool(plugin=SimpleNamespace())
    exposures: list[tuple[object, ...]] = []

    class _Service:
        async def search_memory(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

        async def search_evidence_aware(
            self,
            _query: str,
            **_kwargs: object,
        ) -> list[EvidenceAwareMemoryResult]:
            return []

        async def append_memory_recall_events(self, events: tuple[object, ...]) -> None:
            exposures.append(events)

    async def _service() -> object:
        return _Service()

    monkeypatch.setattr(tool, "_get_service", _service)

    ok, payload = await tool.execute("记忆", continuation="tampered.token")

    assert ok is False
    assert "continuation" in payload["error"]
    assert exposures == []
