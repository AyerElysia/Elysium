"""Subject-document review opportunities and fail-closed mutation contracts."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.learning import tools as learning_tools
from plugins.life_engine.learning.scheduler import LearningScheduler
from plugins.life_engine.learning.tools import LifeReviewSubjectDocumentTool
from plugins.life_engine.storage.subject_contracts import (
    SubjectAuthoritySnapshot,
    SubjectDocumentCommit,
    SubjectDocumentHead,
    SubjectDocumentVersion,
)


def _workspace(tmp_path: Path) -> None:
    old = (datetime.now(UTC) - timedelta(days=3)).timestamp()
    for path in ("SOUL.md", "USER.md", "MEMORY.md"):
        target = tmp_path / path
        target.write_text(f"# {path}\ncurrent\n", encoding="utf-8")
        os.utime(target, (old, old))


def _scheduler(tmp_path: Path) -> LearningScheduler:
    return LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("a"),
        validate_active_consciousness_instance=lambda actor: _is_active(actor),
        subject_review_soul_interval_hours=24.0,
        subject_review_user_interval_hours=24.0,
        subject_review_memory_interval_hours=24.0,
        subject_review_offer_cooldown_hours=24.0,
    )


async def _revision(character: str) -> str:
    return character * 64


async def _is_active(actor: str) -> bool:
    return actor == "consciousness-1"


def _remote_snapshot(contents: dict[str, bytes]) -> SubjectAuthoritySnapshot:
    commits: dict[str, SubjectDocumentCommit] = {}
    for index, (path, content) in enumerate(contents.items(), start=1):
        logical_path = f"life_engine_workspace/{path}"
        version_id = f"remote-version-{index}"
        commits[path] = SubjectDocumentCommit(
            version=SubjectDocumentVersion(
                version_id=version_id,
                document_id=f"remote-document-{index}",
                logical_path=logical_path,
                parent_version_id="",
                occurrence_id=f"remote-occurrence-{index}",
                semantic_actor_id="elysia",
                semantic_source_id="remote-test",
                occurred_at="2026-08-06T00:00:00+00:00",
                recorded_by="test",
                recorded_source="mysql",
                recorded_at="2026-08-06T00:00:00+00:00",
                provenance_status="complete",
                content_bytes=content,
                content_hash=hashlib.sha256(content).hexdigest(),
                byte_length=len(content),
                byte_fidelity="exact_bytes",
                encoding="utf-8",
                newline_style="LF",
                change_context={},
            ),
            head=SubjectDocumentHead(
                document_id=f"remote-document-{index}",
                logical_path=logical_path,
                declared_owner="elysia",
                current_version_id=version_id,
                revision=1,
            ),
        )
    return SubjectAuthoritySnapshot(commits=commits, revision="a" * 64)  # type: ignore[arg-type]


class _Ledger:
    def __init__(self) -> None:
        self.candidates: list[object] = []
        self.decisions: list[object] = []

    async def append_candidate(self, candidate: object) -> SimpleNamespace:
        self.candidates.append(candidate)
        return SimpleNamespace(status="open")

    async def record_decision(self, decision: object) -> SimpleNamespace:
        self.decisions.append(decision)
        return SimpleNamespace(
            decision_occurrence_id=getattr(decision, "decision_occurrence_id"),
            status=getattr(decision, "decision_kind"),
        )


async def test_review_opportunity_is_bounded_and_offer_has_cooldown(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)

    first = await scheduler.get_subject_review_snapshot(mark_offered=True)
    second = await scheduler.get_subject_review_snapshot(mark_offered=False)

    assert first["authority_status"] == "migration_required"
    assert first["direct_mutation_blocked"] is True
    assert first["subject_revision"] == "a" * 64
    assert first["due_count"] == 3
    assert all(len(item["content_sha256"]) == 64 for item in first["documents"])
    assert second["due_count"] == 0
    prompt = await scheduler.get_subject_review_prompt()
    assert prompt == ""
    assert scheduler.get_state()["subject_review"]["authority_status"] == (
        "migration_required"
    )


async def test_local_review_can_record_no_change_but_not_a_candidate(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)
    revision = await scheduler.validate_subject_review_context(
        actor_consciousness_instance_id="consciousness-1",
        expected_subject_revision="a" * 64,
    )

    record = await scheduler.record_subject_review_outcome(
        target_path="MEMORY.md",
        outcome="unchanged",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision=revision,
        occurrence_id="review:memory:1",
        reason="I read the current version and want to keep it.",
    )

    assert record["last_outcome"] == "unchanged"
    journal = tmp_path / ".life_learning" / "subject_reviews.jsonl"
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["actor_consciousness_instance_id"] == "consciousness-1"
    assert event["subject_revision"] == "a" * 64
    assert event["authority"] == "review_evidence_only"

    repeated = await scheduler.record_subject_review_outcome(
        target_path="MEMORY.md",
        outcome="unchanged",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision=revision,
        occurrence_id="review:memory:1",
        reason="idempotent replay",
    )
    assert repeated["last_occurrence_id"] == "review:memory:1"
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1

    with pytest.raises(RuntimeError, match="SubjectAuthorityMigrationRequired"):
        await scheduler.record_subject_review_outcome(
            target_path="MEMORY.md",
            outcome="candidate_proposed",
            actor_consciousness_instance_id="consciousness-1",
            subject_revision=revision,
            occurrence_id="review:memory:candidate",
            reason="candidate must not fall back to local files",
        )


async def test_review_tool_fails_closed_for_proposal_before_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    tool = LifeReviewSubjectDocumentTool(
        plugin=SimpleNamespace(config=config),
    )
    monkeypatch.setattr(learning_tools, "_get_scheduler", lambda _plugin: scheduler)
    monkeypatch.setattr(
        learning_tools,
        "_decision_actor",
        lambda _tool: (None, "consciousness-1"),
    )
    current = (tmp_path / "MEMORY.md").read_bytes()
    current_hash = hashlib.sha256(current).hexdigest()

    ok, error = await tool.execute(
        action="propose",
        target_path="MEMORY.md",
        expected_subject_revision="a" * 64,
        reviewed_content_sha256=current_hash,
        reason="I want to consider a different interpretation.",
        proposed_content="# MEMORY.md\nnew interpretation\n",
    )

    assert ok is False
    assert "SubjectAuthorityMigrationRequired" in str(error)
    assert (tmp_path / "MEMORY.md").read_bytes() == current
    assert not (tmp_path / ".life_learning" / "subject_reviews.jsonl").exists()


async def test_review_tool_records_unchanged_against_exact_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    tool = LifeReviewSubjectDocumentTool(plugin=SimpleNamespace(config=config))
    monkeypatch.setattr(learning_tools, "_get_scheduler", lambda _plugin: scheduler)
    monkeypatch.setattr(
        learning_tools,
        "_decision_actor",
        lambda _tool: (None, "consciousness-1"),
    )
    current = (tmp_path / "USER.md").read_bytes()

    ok, payload = await tool.execute(
        action="unchanged",
        target_path="USER.md",
        expected_subject_revision="a" * 64,
        reviewed_content_sha256=hashlib.sha256(current).hexdigest(),
        reason="This still describes the relationship as I understand it.",
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["action"] == "subject_review_unchanged"
    assert payload["authority_status"] == "migration_required"
    assert (tmp_path / "USER.md").read_bytes() == current


async def test_selected_review_proposes_candidate_without_writing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)
    ledger = _Ledger()
    scheduler.decision_ledger = ledger  # type: ignore[assignment]
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    tool = LifeReviewSubjectDocumentTool(plugin=SimpleNamespace(config=config))
    monkeypatch.setattr(learning_tools, "_get_scheduler", lambda _plugin: scheduler)
    monkeypatch.setattr(
        learning_tools,
        "_decision_actor",
        lambda _tool: (None, "consciousness-1"),
    )
    target = tmp_path / "MEMORY.md"
    current = target.read_bytes()

    ok, payload = await tool.execute(
        action="propose",
        target_path="MEMORY.md",
        expected_subject_revision="a" * 64,
        reviewed_content_sha256=hashlib.sha256(current).hexdigest(),
        reason="I want to keep this alternative open for a separate decision.",
        proposed_content="# MEMORY.md\nproposed interpretation\n",
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["status"] == "open"
    assert len(ledger.candidates) == 1
    candidate = ledger.candidates[0]
    assert getattr(candidate, "actor_consciousness_instance_id") == (
        "consciousness-1"
    )
    assert getattr(candidate, "subject_revision") == "a" * 64
    assert getattr(candidate, "target_path") == "MEMORY.md"
    assert target.read_bytes() == current
    assert not (tmp_path / ".life_learning" / "subject_reviews.jsonl").exists()


async def test_selected_review_reads_remote_memory_and_never_local_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = {
        "SOUL.md": b"remote soul",
        "USER.md": b"remote user",
        "MEMORY.md": b"# Remote memory\nchosen continuity\n",
    }
    (tmp_path / "MEMORY.md").write_text("LOCAL SHADOW", encoding="utf-8")

    async def read_remote() -> SubjectAuthoritySnapshot:
        return _remote_snapshot(remote)

    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("a"),
        read_subject_authority=read_remote,
        validate_active_consciousness_instance=lambda actor: _is_active(actor),
    )
    scheduler.decision_ledger = _Ledger()  # type: ignore[assignment]
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    tool = LifeReviewSubjectDocumentTool(plugin=SimpleNamespace(config=config))
    monkeypatch.setattr(learning_tools, "_get_scheduler", lambda _plugin: scheduler)
    monkeypatch.setattr(
        learning_tools,
        "_decision_actor",
        lambda _tool: (None, "consciousness-1"),
    )

    ok, status = await tool.execute(action="status", target_path="MEMORY.md")

    assert ok is True
    assert isinstance(status, dict)
    assert status["content"] == remote["MEMORY.md"].decode("utf-8")
    assert status["documents"][0]["content_sha256"] == hashlib.sha256(
        remote["MEMORY.md"]
    ).hexdigest()
    assert "LOCAL SHADOW" not in status["content"]

    ok, proposed = await tool.execute(
        action="propose",
        target_path="MEMORY.md",
        expected_subject_revision="a" * 64,
        reviewed_content_sha256=hashlib.sha256(remote["MEMORY.md"]).hexdigest(),
        reason="I choose to preserve a new memory in my remote authority.",
        proposed_content="# Remote memory\nchosen continuity\nnew memory\n",
    )

    assert ok is True
    assert isinstance(proposed, dict)
    assert proposed["status"] == "open"
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "LOCAL SHADOW"


def test_subject_memory_tools_are_available_to_chat_consciousness() -> None:
    assert "life_chatter" in LifeReviewSubjectDocumentTool.chatter_allow
    assert "life_chatter" in learning_tools.LifeListSubjectCandidatesTool.chatter_allow
    assert "life_chatter" in learning_tools.LifeReadSubjectCandidateTool.chatter_allow
    assert "life_chatter" in learning_tools.LifeDecideSubjectCandidateTool.chatter_allow


async def test_review_context_rejects_stale_revision_and_inactive_actor(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)

    with pytest.raises(RuntimeError, match="LearningSubjectRevisionConflict"):
        await scheduler.validate_subject_review_context(
            actor_consciousness_instance_id="consciousness-1",
            expected_subject_revision="b" * 64,
        )
    with pytest.raises(PermissionError, match="LearningDecisionActorIsNotActive"):
        await scheduler.validate_subject_review_context(
            actor_consciousness_instance_id="inactive",
            expected_subject_revision="a" * 64,
        )
