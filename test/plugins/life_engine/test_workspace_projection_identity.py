from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from plugins.life_engine.memory.workspace_projection_identity import (
    WorkspaceProjectionBindingConflict,
    WorkspaceProjectionBulkRetirementForbidden,
    WorkspaceProjectionDeleteEvidenceError,
    WorkspaceProjectionEventKind,
    WorkspaceProjectionIdentityError,
    WorkspaceProjectionRebuildRequired,
    WorkspaceProjectionRevisionConflict,
    WorkspaceProjectionTransition,
    authorize_explicit_document_deletion,
    authorize_workspace_projection_write,
    bind_workspace_projection,
    build_workspace_projection_identity,
    commit_workspace_projection_inventory,
    handoff_workspace_projection_owner,
    rebuild_workspace_projection_generation,
)

NOW = "2026-08-11T00:00:00+08:00"
LATER = "2026-08-11T00:01:00+08:00"


def _workspace(root: Path, *, memory: str = "same") -> Path:
    root.mkdir(parents=True)
    (root / "MEMORY.md").write_text(memory, encoding="utf-8")
    notes = root / "notes"
    notes.mkdir()
    (notes / "index.md").write_text("note", encoding="utf-8")
    return root


def _bind(root: Path, *, owner_id: str = "elysium-wsl-primary"):
    identity = build_workspace_projection_identity(root)
    transition = bind_workspace_projection(
        identity,
        storage_generation_id="life-mysql-v1",
        projection_generation_id="memory-docs-wsl-v1",
        owner_id=owner_id,
        actor_id="operator-1",
        audit_occurrence_id="projection-bind-1",
        reason_code="initial-bind",
        occurred_at=NOW,
    )
    return identity, transition


def test_identity_is_content_free_and_distinguishes_workspace_roots(
    tmp_path: Path,
) -> None:
    first = build_workspace_projection_identity(
        _workspace(tmp_path / "first", memory="aaaa")
    )
    second = build_workspace_projection_identity(
        _workspace(tmp_path / "second", memory="bbbb")
    )

    assert first.eligible_inventory_sha256 == second.eligible_inventory_sha256
    assert first.canonical_root_sha256 != second.canonical_root_sha256
    assert first.source_root_sha256 != second.source_root_sha256
    assert first.eligible_document_count == 2
    assert first.eligible_total_bytes == 8

    safe = first.safe_dict()
    rendered = repr(first)
    assert str(first.canonical_root) not in rendered
    assert str(first.canonical_root) not in str(safe)
    assert "MEMORY.md" not in str(safe)
    assert "aaaa" not in str(safe)


def test_inventory_change_keeps_root_identity_but_changes_source_snapshot(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path / "workspace")
    before = build_workspace_projection_identity(root)
    (root / "notes" / "later.md").write_text("later", encoding="utf-8")
    after = build_workspace_projection_identity(root)

    assert after.canonical_root_sha256 == before.canonical_root_sha256
    assert after.eligible_inventory_sha256 != before.eligible_inventory_sha256
    assert after.source_root_sha256 != before.source_root_sha256
    assert after.eligible_document_count == before.eligible_document_count + 1


def test_bound_owner_can_authorize_present_document_projection(tmp_path: Path) -> None:
    identity, transition = _bind(_workspace(tmp_path / "workspace"))
    permit = authorize_workspace_projection_write(
        transition.binding,
        identity,
        storage_generation_id="life-mysql-v1",
        projection_generation_id="memory-docs-wsl-v1",
        owner_id="elysium-wsl-primary",
    )

    assert permit.binding_revision == 1
    assert permit.binding_event_sha256 == transition.event.event_sha256
    assert permit.safe_dict()["scan_derived_retirement_allowed"] is False
    assert len(permit.write_fence_sha256) == 64


@pytest.mark.parametrize(
    ("storage_generation_id", "projection_generation_id", "owner_id"),
    [
        ("life-mysql-v2", "memory-docs-wsl-v1", "elysium-wsl-primary"),
        ("life-mysql-v1", "memory-docs-wsl-v2", "elysium-wsl-primary"),
        ("life-mysql-v1", "memory-docs-wsl-v1", "elysium-windows-primary"),
    ],
)
def test_generation_and_owner_mismatch_fail_closed(
    tmp_path: Path,
    storage_generation_id: str,
    projection_generation_id: str,
    owner_id: str,
) -> None:
    identity, transition = _bind(_workspace(tmp_path / "workspace"))

    with pytest.raises(WorkspaceProjectionBindingConflict):
        authorize_workspace_projection_write(
            transition.binding,
            identity,
            storage_generation_id=storage_generation_id,
            projection_generation_id=projection_generation_id,
            owner_id=owner_id,
        )


def test_same_owner_from_a_different_workspace_requires_rebuild(tmp_path: Path) -> None:
    _, transition = _bind(_workspace(tmp_path / "wsl", memory="aaaa"))
    other = build_workspace_projection_identity(
        _workspace(tmp_path / "windows", memory="bbbb")
    )

    with pytest.raises(WorkspaceProjectionRebuildRequired):
        authorize_workspace_projection_write(
            transition.binding,
            other,
            storage_generation_id="life-mysql-v1",
            projection_generation_id="memory-docs-wsl-v1",
            owner_id="elysium-wsl-primary",
        )


def test_scan_absence_can_never_authorize_bulk_tombstones(tmp_path: Path) -> None:
    identity, transition = _bind(_workspace(tmp_path / "workspace"))
    permit = authorize_workspace_projection_write(
        transition.binding,
        identity,
        storage_generation_id="life-mysql-v1",
        projection_generation_id="memory-docs-wsl-v1",
        owner_id="elysium-wsl-primary",
    )

    permit.reject_scan_derived_retirements([])
    with pytest.raises(
        WorkspaceProjectionBulkRetirementForbidden,
        match="scan absence",
    ):
        permit.reject_scan_derived_retirements(["file:missing-a", "file:missing-b"])


def test_explicit_delete_requires_occurrence_and_exact_index_revision(
    tmp_path: Path,
) -> None:
    identity, transition = _bind(_workspace(tmp_path / "workspace"))
    permit = authorize_workspace_projection_write(
        transition.binding,
        identity,
        storage_generation_id="life-mysql-v1",
        projection_generation_id="memory-docs-wsl-v1",
        owner_id="elysium-wsl-primary",
    )

    deletion = authorize_explicit_document_deletion(
        permit,
        file_path="notes/index.md",
        expected_content_hash="a" * 64,
        expected_index_revision=4,
        source_occurrence_id="workspace-delete-7",
        actor_id="elysium-wsl-primary",
        reason_code="authorized-file-delete",
    )
    assert deletion.file_path == "notes/index.md"
    assert deletion.expected_index_revision == 4
    assert deletion.projection_write_fence_sha256 == permit.write_fence_sha256
    assert "notes/index.md" not in str(deletion.safe_dict())

    with pytest.raises(WorkspaceProjectionDeleteEvidenceError):
        authorize_explicit_document_deletion(
            permit,
            file_path="notes/index.md",
            expected_content_hash="a" * 64,
            expected_index_revision=4,
            source_occurrence_id="",
            actor_id="elysium-wsl-primary",
            reason_code="authorized-file-delete",
        )
    with pytest.raises(WorkspaceProjectionDeleteEvidenceError):
        authorize_explicit_document_deletion(
            permit,
            file_path="notes/index.md",
            expected_content_hash="not-a-digest",
            expected_index_revision=4,
            source_occurrence_id="workspace-delete-8",
            actor_id="elysium-wsl-primary",
            reason_code="authorized-file-delete",
        )


def test_owner_handoff_is_explicit_hash_chained_and_same_root_only(
    tmp_path: Path,
) -> None:
    identity, initial = _bind(_workspace(tmp_path / "workspace"))
    handoff = handoff_workspace_projection_owner(
        initial.binding,
        identity,
        expected_revision=1,
        new_owner_id="elysium-wsl-secondary",
        actor_id="operator-1",
        audit_occurrence_id="handoff-1",
        reason_code="operator-approved-handoff",
        occurred_at=LATER,
    )

    assert handoff.event.event_kind == WorkspaceProjectionEventKind.OWNER_HANDOFF
    assert handoff.event.previous_event_sha256 == initial.event.event_sha256
    assert handoff.binding.owner_id == "elysium-wsl-secondary"
    assert handoff.binding.revision == 2
    assert handoff.binding.last_event_sha256 == handoff.event.event_sha256

    changed = replace(handoff.event, reason_code="different-reason")
    assert changed.event_sha256 != handoff.event.event_sha256


def test_handoff_cannot_silently_switch_to_another_workspace(tmp_path: Path) -> None:
    _, initial = _bind(_workspace(tmp_path / "wsl"))
    other = build_workspace_projection_identity(_workspace(tmp_path / "windows"))

    with pytest.raises(WorkspaceProjectionRebuildRequired):
        handoff_workspace_projection_owner(
            initial.binding,
            other,
            expected_revision=1,
            new_owner_id="elysium-windows-primary",
            actor_id="operator-1",
            audit_occurrence_id="handoff-1",
            reason_code="operator-approved-handoff",
            occurred_at=LATER,
        )


def test_different_workspace_can_only_enter_a_new_projection_generation(
    tmp_path: Path,
) -> None:
    _, initial = _bind(_workspace(tmp_path / "wsl"))
    other = build_workspace_projection_identity(_workspace(tmp_path / "windows"))
    rebuilt = rebuild_workspace_projection_generation(
        initial.binding,
        other,
        expected_revision=1,
        new_projection_generation_id="memory-docs-windows-v1",
        new_owner_id="elysium-windows-primary",
        actor_id="operator-1",
        audit_occurrence_id="rebuild-1",
        reason_code="operator-approved-rebuild",
        occurred_at=LATER,
    )

    assert rebuilt.event.event_kind == WorkspaceProjectionEventKind.GENERATION_REBUILT
    assert rebuilt.event.previous_projection_generation_id == "memory-docs-wsl-v1"
    assert rebuilt.binding.projection_generation_id == "memory-docs-windows-v1"
    assert rebuilt.binding.workspace_root_sha256 == other.canonical_root_sha256
    assert rebuilt.event.previous_event_sha256 == initial.event.event_sha256

    with pytest.raises(WorkspaceProjectionBindingConflict, match="new projection"):
        rebuild_workspace_projection_generation(
            initial.binding,
            other,
            expected_revision=1,
            new_projection_generation_id="memory-docs-wsl-v1",
            new_owner_id="elysium-windows-primary",
            actor_id="operator-1",
            audit_occurrence_id="rebuild-2",
            reason_code="operator-approved-rebuild",
            occurred_at=LATER,
        )


def test_inventory_commit_is_audited_without_granting_delete_by_absence(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path / "workspace")
    _, initial = _bind(root)
    (root / "notes" / "later.md").write_text("later", encoding="utf-8")
    changed = build_workspace_projection_identity(root)
    committed = commit_workspace_projection_inventory(
        initial.binding,
        changed,
        expected_revision=1,
        owner_id="elysium-wsl-primary",
        actor_id="elysium-wsl-primary",
        audit_occurrence_id="reconcile-2",
        reason_code="present-documents-committed",
        occurred_at=LATER,
    )

    assert (
        committed.event.event_kind == WorkspaceProjectionEventKind.INVENTORY_COMMITTED
    )
    assert committed.binding.eligible_inventory_sha256 == (
        changed.eligible_inventory_sha256
    )
    assert committed.binding.revision == 2
    permit = authorize_workspace_projection_write(
        committed.binding,
        changed,
        storage_generation_id="life-mysql-v1",
        projection_generation_id="memory-docs-wsl-v1",
        owner_id="elysium-wsl-primary",
    )
    with pytest.raises(WorkspaceProjectionBulkRetirementForbidden):
        permit.reject_scan_derived_retirements(["file:absent"])


def test_transition_revision_is_compare_and_swap_bound(tmp_path: Path) -> None:
    identity, initial = _bind(_workspace(tmp_path / "workspace"))

    with pytest.raises(WorkspaceProjectionRevisionConflict):
        commit_workspace_projection_inventory(
            initial.binding,
            identity,
            expected_revision=0,
            owner_id="elysium-wsl-primary",
            actor_id="elysium-wsl-primary",
            audit_occurrence_id="reconcile-stale",
            reason_code="present-documents-committed",
            occurred_at=LATER,
        )


def test_transition_rejects_a_head_that_does_not_match_its_event(
    tmp_path: Path,
) -> None:
    _, initial = _bind(_workspace(tmp_path / "workspace"))

    with pytest.raises(WorkspaceProjectionIdentityError, match="head fields"):
        WorkspaceProjectionTransition(
            binding=replace(initial.binding, owner_id="different-owner"),
            event=initial.event,
        )
