"""Life Engine 生命记忆本体与见证意识回归测试。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.memory.experience import (
    EpistemicKind,
    EvidenceAwareMemoryResult,
    ExperienceAppendReport,
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
from plugins.life_engine.memory.tools import LifeEngineSearchMemoryTool
from plugins.life_engine.service.consciousness import (
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from plugins.life_engine.service.event_bus import LifeEvent, RawEventGapError
from plugins.life_engine.service.legacy_diary import parse_legacy_diary_file
from plugins.life_engine.service.memory_witness import (
    MEMORY_WITNESS_INSTANCE_ID,
    MemoryWitnessCoordinator,
)
from plugins.life_engine.service.tool_manifests import get_tool_manifest
from src.kernel.llm.exceptions import LLMAPIError, LLMModelsCoolingDownError
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


class _WitnessMemoryStub:
    def __init__(self, *, existing: object | None = None) -> None:
        self.existing = existing
        self.states: list[dict[str, object]] = []
        self.appended: list[ExperienceRecord] = []
        self.recorded = 0
        self.recorded_kwargs: list[dict[str, object]] = []

    async def get_witness_state(self, _instance_id: str) -> dict[str, object]:
        return {"last_sequence": 0}

    async def append_experiences(self, records: list[ExperienceRecord]) -> int:
        self.appended.extend(records)
        return len(records)

    async def list_pending_witness_projections(self, *, limit: int) -> list[object]:
        assert limit == 20
        return []

    async def get_witness_by_projection_path(self, _path: str) -> object | None:
        return self.existing

    async def record_witness_memory(self, **kwargs: object) -> object:
        self.recorded += 1
        self.recorded_kwargs.append(kwargs)
        return SimpleNamespace(
            witness_id="witness-1",
            projection_path=kwargs["projection_path"],
        )

    async def update_witness_state(self, _instance_id: str, **kwargs: object) -> None:
        self.states.append(kwargs)


class _RawStoreStub:
    def __init__(self, event: LifeEvent | list[LifeEvent]) -> None:
        self.events = list(event) if isinstance(event, list) else [event]
        self.event = self.events[0]

    async def read_since(self, sequence: int, *, limit: int) -> list[LifeEvent]:
        assert sequence == 0
        assert limit == 80
        return list(self.events)


def _witness_service_stub(tmp_path: Path, memory: object) -> SimpleNamespace:
    registry = ConsciousnessRegistry()
    event = LifeEvent(
        event_id="event-1",
        sequence=1,
        timestamp="2026-07-29T08:00:00+08:00",
        source="chat",
        channel="chat",
        event_type="text",
        content="真实经历",
        stream_id="stream-1",
    )
    config = SimpleNamespace(
        enabled=True,
        max_events_per_run=80,
        model_task_name="diary",
        migrate_legacy_diaries=False,
    )

    async def _register(instance: ConsciousnessInstance) -> ConsciousnessInstance:
        return registry.register(instance)

    async def _resume(instance_id: str, **kwargs: object) -> bool:
        return registry.resume(instance_id, **kwargs)

    async def _touch(instance_id: str, **kwargs: object) -> None:
        registry.touch(instance_id, **kwargs)

    store = _RawStoreStub(event)
    return SimpleNamespace(
        consciousness_registry=registry,
        save_consciousness_registry=lambda: None,
        register_consciousness_instance=_register,
        resume_consciousness_instance=_resume,
        touch_consciousness_instance=_touch,
        memory_service=memory,
        _cfg=lambda: SimpleNamespace(memory_witness=config),
        _get_life_event_store=lambda: store,
        _workspace_dir=lambda: tmp_path,
    )


async def test_witness_self_presence_side_effect_is_persisted_without_authoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _WitnessMemoryStub()
    service = _witness_service_stub(tmp_path, memory)
    self_presence = LifeEvent(
        event_id="presence-1",
        sequence=1,
        timestamp="2026-08-04T12:00:00+08:00",
        source="life_engine.presence",
        channel="system",
        event_type="consciousness.instance_seen",
        content="memory witness lease maintained",
        stream_id="presence:memory_witness",
        source_instance_id=MEMORY_WITNESS_INSTANCE_ID,
    )
    store = service._get_life_event_store()
    store.event = self_presence
    store.events = [self_presence]
    coordinator = MemoryWitnessCoordinator(service)

    async def _must_not_author(*_args: object) -> str:
        raise AssertionError("self Presence side effect must not invoke the model")

    monkeypatch.setattr(coordinator, "_author_witness", _must_not_author)

    report = await coordinator.run_once()

    assert [item.sequence for item in memory.appended] == [1]
    assert memory.appended[0].event_type == "consciousness.instance_seen"
    assert memory.appended[0].consciousness_instance_id == (MEMORY_WITNESS_INSTANCE_ID)
    assert memory.recorded == 0
    assert report.synced_experiences == 1
    assert report.considered_events == 1
    assert report.suppressed_self_echo_events == 1
    assert report.written_witnesses == ()
    assert report.skipped_scopes == ()
    assert report.last_sequence == 1
    assert memory.states[-1]["last_sequence"] == 1


async def test_witness_mixed_window_fences_only_own_presence_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _WitnessMemoryStub()
    service = _witness_service_stub(tmp_path, memory)
    events = [
        LifeEvent(
            event_id="presence-self",
            sequence=1,
            timestamp="2026-08-04T12:00:00+08:00",
            source="life_engine.presence",
            channel="system",
            event_type="consciousness.instance_seen",
            content="memory witness lease maintained",
            stream_id="stream-1",
            source_instance_id=MEMORY_WITNESS_INSTANCE_ID,
        ),
        LifeEvent(
            event_id="presence-other",
            sequence=2,
            timestamp="2026-08-04T12:00:01+08:00",
            source="life_engine.presence",
            channel="system",
            event_type="consciousness.instance_seen",
            content="another consciousness remains present",
            stream_id="stream-1",
            source_instance_id="chat_global",
        ),
        LifeEvent(
            event_id="chat-1",
            sequence=3,
            timestamp="2026-08-04T12:00:02+08:00",
            source="chat",
            channel="chat",
            event_type="text",
            content="a retained experience",
            stream_id="stream-1",
            source_instance_id="chat_global",
        ),
        LifeEvent(
            event_id="witness-thought-1",
            sequence=4,
            timestamp="2026-08-04T12:00:03+08:00",
            source="life_engine.memory_witness",
            channel="internal",
            event_type="reflection",
            content="the witness's own retained inner experience",
            stream_id="stream-1",
            source_instance_id=MEMORY_WITNESS_INSTANCE_ID,
        ),
    ]
    store = service._get_life_event_store()
    store.event = events[0]
    store.events = events
    coordinator = MemoryWitnessCoordinator(service)
    authored: list[ExperienceRecord] = []

    async def _author(
        _instance: ConsciousnessInstance,
        records: list[ExperienceRecord],
    ) -> str:
        authored.extend(records)
        return "a first-person witness"

    async def _project(_witness: object) -> None:
        return None

    monkeypatch.setattr(coordinator, "_author_witness", _author)
    monkeypatch.setattr(coordinator, "_project_witness", _project)

    report = await coordinator.run_once()

    assert [item.sequence for item in memory.appended] == [1, 2, 3, 4]
    assert [item.sequence for item in authored] == [2, 3, 4]
    assert authored[0].consciousness_instance_id == "chat_global"
    assert authored[-1].consciousness_instance_id == MEMORY_WITNESS_INSTANCE_ID
    assert memory.recorded == 1
    assert memory.recorded_kwargs[0]["source_sequence_start"] == 2
    assert memory.recorded_kwargs[0]["source_sequence_end"] == 4
    assert memory.recorded_kwargs[0]["source_event_ids"] == [
        item.event_id for item in authored
    ]
    assert report.synced_experiences == 4
    assert report.considered_events == 4
    assert report.suppressed_self_echo_events == 1
    assert report.written_witnesses == ("witness-1",)
    assert report.last_sequence == 4
    assert memory.states[-1]["last_sequence"] == 4


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
    assert payload["evidence_results"][0] == {
        "record_id": "witness-1",
        "kind": EpistemicKind.SUBJECTIVE_WITNESS.value,
        "content": "我记得这段经历。",
        "rank_score": 0.875,
        "confidence": None,
        "source": "witness_fts",
        "valid_from": "2026-07-29T08:00:00+08:00",
        "valid_to": "2026-07-29T08:05:00+08:00",
        "recorded_at": "2026-07-29T08:06:00+08:00",
        "stream_scope": "stream-1",
        "visibility": "private",
        "status": "active",
        "provenance": ["event-1"],
        "metadata": {"subjective": True},
    }


async def test_projection_failure_does_not_advance_witness_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _WitnessMemoryStub()
    coordinator = MemoryWitnessCoordinator(_witness_service_stub(tmp_path, memory))

    async def _author(*_args: object) -> str:
        return "我的第一人称见证。"

    async def _fail_projection(_witness: object) -> None:
        raise OSError("projection failed")

    monkeypatch.setattr(coordinator, "_author_witness", _author)
    monkeypatch.setattr(coordinator, "_project_witness", _fail_projection)

    with pytest.raises(OSError, match="projection failed"):
        await coordinator.run_once()

    assert memory.recorded == 1
    assert not any("last_sequence" in state for state in memory.states)


async def test_witness_retry_authors_existing_experiences_before_advancing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model failure after append must not turn the retry into a silent skip."""

    class _RetryMemory(_WitnessMemoryStub):
        def __init__(self) -> None:
            super().__init__()
            self.append_calls = 0

        async def append_experiences_detailed(
            self,
            records: list[ExperienceRecord],
        ) -> ExperienceAppendReport:
            self.append_calls += 1
            canonical = tuple(records)
            if self.append_calls == 1:
                return ExperienceAppendReport(inserted=canonical)
            return ExperienceAppendReport(existing=canonical)

    memory = _RetryMemory()
    coordinator = MemoryWitnessCoordinator(_witness_service_stub(tmp_path, memory))
    author_calls = 0

    async def _author(*_args: object) -> str:
        nonlocal author_calls
        author_calls += 1
        if author_calls == 1:
            raise LLMAPIError("temporary", status_code=500)
        return "retry witness"

    async def _project(_witness: object) -> None:
        return None

    monkeypatch.setattr(coordinator, "_author_witness", _author)
    monkeypatch.setattr(coordinator, "_project_witness", _project)

    with pytest.raises(LLMAPIError):
        await coordinator.run_once()

    assert memory.recorded == 0
    assert not any("last_sequence" in state for state in memory.states)

    report = await coordinator.run_once()

    assert author_calls == 2
    assert memory.append_calls == 2
    assert memory.recorded == 1
    assert report.synced_experiences == 0
    assert report.last_sequence == 1
    assert memory.states[-1]["last_sequence"] == 1


async def test_existing_window_is_reused_without_calling_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = SimpleNamespace(witness_id="existing", projection_path="path.md")
    memory = _WitnessMemoryStub(existing=existing)
    coordinator = MemoryWitnessCoordinator(_witness_service_stub(tmp_path, memory))
    projected: list[object] = []

    async def _must_not_author(*_args: object) -> str:
        raise AssertionError("model must not be called for an existing window")

    async def _project(witness: object) -> None:
        projected.append(witness)

    monkeypatch.setattr(coordinator, "_author_witness", _must_not_author)
    monkeypatch.setattr(coordinator, "_project_witness", _project)

    report = await coordinator.run_once()

    assert projected == [existing]
    assert memory.recorded == 0
    assert report.last_sequence == 1
    assert memory.states[-1]["last_sequence"] == 1


async def test_witness_refuses_to_skip_retained_event_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retention gap is fatal to the consumer cursor and is never skipped."""

    memory = _WitnessMemoryStub(
        existing=SimpleNamespace(
            witness_id="existing",
            projection_path="path.md",
        )
    )

    async def _get_state(_instance_id: str) -> dict[str, object]:
        return {"last_sequence": 1}

    memory.get_witness_state = _get_state  # type: ignore[method-assign]
    service = _witness_service_stub(tmp_path, memory)
    event = service._get_life_event_store().event
    event = LifeEvent(
        event_id=event.event_id,
        sequence=4,
        timestamp=event.timestamp,
        source=event.source,
        channel=event.channel,
        event_type=event.event_type,
        content=event.content,
        stream_id=event.stream_id,
    )

    class _GapStore:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def read_since(
            self,
            sequence: int,
            *,
            limit: int,
        ) -> list[LifeEvent]:
            assert limit == 80
            self.calls.append(sequence)
            if len(self.calls) == 1:
                raise RawEventGapError(sequence, 4)
            return [event]

    store = _GapStore()
    service._get_life_event_store = lambda: store
    coordinator = MemoryWitnessCoordinator(service)

    async def _project(_witness: object) -> None:
        return None

    monkeypatch.setattr(coordinator, "_project_witness", _project)
    warnings: list[str] = []
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.warning",
        warnings.append,
    )

    with pytest.raises(RuntimeError, match="MemoryWitnessRawLedgerGap"):
        await coordinator.run_once()

    assert store.calls == [1]
    assert memory.states == []
    assert warnings == []
