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
    ExperienceRecord,
    MemorySearchMode,
    create_life_memory_schema,
    insert_experiences,
    insert_witness_memory,
    migrate_legacy_witness,
    search_witness_memories,
)
from plugins.life_engine.memory.tools import LifeEngineSearchMemoryTool
from plugins.life_engine.service.consciousness import ConsciousnessRegistry
from plugins.life_engine.service.event_bus import LifeEvent, RawEventGapError
from plugins.life_engine.service.legacy_diary import parse_legacy_diary_file
from plugins.life_engine.service.memory_witness import (
    MEMORY_WITNESS_INSTANCE_ID,
    MemoryWitnessCoordinator,
)
from plugins.life_engine.service.tool_manifests import get_tool_manifest


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


def test_memory_witness_is_registered_as_consciousness_without_tools(
    tmp_path: Path,
) -> None:
    registry = ConsciousnessRegistry()
    service = SimpleNamespace(
        consciousness_registry=registry,
        save_consciousness_registry=lambda: None,
        _cfg=lambda: SimpleNamespace(memory_witness=SimpleNamespace(enabled=True)),
    )

    instance = MemoryWitnessCoordinator(service).ensure_instance()

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


class _WitnessMemoryStub:
    def __init__(self, *, existing: object | None = None) -> None:
        self.existing = existing
        self.states: list[dict[str, object]] = []
        self.recorded = 0

    async def get_witness_state(self, _instance_id: str) -> dict[str, object]:
        return {"last_sequence": 0}

    async def append_experiences(self, records: list[ExperienceRecord]) -> int:
        return len(records)

    async def list_pending_witness_projections(self, *, limit: int) -> list[object]:
        assert limit == 20
        return []

    async def get_witness_by_projection_path(self, _path: str) -> object | None:
        return self.existing

    async def record_witness_memory(self, **kwargs: object) -> object:
        self.recorded += 1
        return SimpleNamespace(
            witness_id="witness-1",
            projection_path=kwargs["projection_path"],
        )

    async def update_witness_state(self, _instance_id: str, **kwargs: object) -> None:
        self.states.append(kwargs)


class _RawStoreStub:
    def __init__(self, event: LifeEvent) -> None:
        self.event = event

    async def read_since(self, sequence: int, *, limit: int) -> list[LifeEvent]:
        assert sequence == 0
        assert limit == 80
        return [self.event]


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
    return SimpleNamespace(
        consciousness_registry=registry,
        save_consciousness_registry=lambda: None,
        memory_service=memory,
        _cfg=lambda: SimpleNamespace(memory_witness=config),
        _get_event_bus=lambda: SimpleNamespace(store=_RawStoreStub(event)),
        _workspace_dir=lambda: tmp_path,
    )


async def test_search_tool_rejects_unknown_mode_before_service_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = LifeEngineSearchMemoryTool(plugin=SimpleNamespace())

    async def _must_not_lookup() -> object:
        raise AssertionError("invalid mode must not touch the memory service")

    monkeypatch.setattr(tool, "_get_service", _must_not_lookup)

    ok, payload = await tool.execute("经历", search_mode="objective_truth")

    assert ok is False
    assert "search_mode 必须是" in payload["error"]


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
            mode: MemorySearchMode,
            top_k: int,
            stream_scope: str | None,
            enable_association: bool,
            valid_at: str,
            recorded_as_of: str,
        ) -> list[EvidenceAwareMemoryResult]:
            assert mode is MemorySearchMode.AUTOBIOGRAPHICAL
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


async def test_witness_recovers_from_retained_event_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retention gap is reported once, then processing resumes safely."""

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
    event = service._get_event_bus().store.event
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
    service._get_event_bus = lambda: SimpleNamespace(store=store)
    coordinator = MemoryWitnessCoordinator(service)

    async def _project(_witness: object) -> None:
        return None

    monkeypatch.setattr(coordinator, "_project_witness", _project)
    warnings: list[str] = []
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.warning",
        warnings.append,
    )

    report = await coordinator.run_once()

    assert store.calls == [1, 3]
    assert report.last_sequence == 4
    assert memory.states[-1]["last_sequence"] == 4
    assert len(warnings) == 1
    assert "记忆见证游标落后" in warnings[0]
