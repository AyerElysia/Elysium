"""MySQL adapter contracts for workspace projection ownership fencing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.memory.workspace_projection_identity import (
    WorkspaceProjectionBindingConflict,
    WorkspaceProjectionIdentity,
    WorkspaceProjectionRevisionConflict,
    bind_workspace_projection,
    rebuild_workspace_projection_generation,
)
from plugins.life_engine.storage.memory.mysql import (
    MySQLWorkspaceProjectionBindingStore,
)


class _Mappings:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row


class _Result:
    def __init__(
        self,
        row: dict[str, Any] | None = None,
        *,
        rowcount: int = 1,
    ) -> None:
        self._row = row
        self.rowcount = rowcount

    def mappings(self) -> _Mappings:
        return _Mappings(self._row)


class _Session:
    def __init__(self, current: dict[str, Any] | None = None) -> None:
        self.current = current
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        statement: object,
        parameters: dict[str, Any],
    ) -> _Result:
        sql = " ".join(str(statement).split())
        self.calls.append((sql, dict(parameters)))
        if sql.startswith("SELECT * FROM memory_workspace_projection_heads"):
            return _Result(self.current)
        return _Result()


def _identity(
    *, root: str = "/workspace", inventory: str = "b"
) -> WorkspaceProjectionIdentity:
    return WorkspaceProjectionIdentity(
        canonical_root=Path(root),
        canonical_root_sha256="a" * 64,
        eligible_inventory_sha256=inventory * 64,
        source_root_sha256="c" * 64,
        eligible_document_count=2,
        eligible_total_bytes=123,
    )


def _initial_transition():  # type: ignore[no-untyped-def]
    return bind_workspace_projection(
        _identity(),
        storage_generation_id="generation-1",
        projection_generation_id="projection-1",
        owner_id="elysium-runtime-1",
        actor_id="elysium-runtime-1",
        audit_occurrence_id="workspace-bind-1",
        reason_code="initial-bind",
        occurred_at="2026-08-11T12:00:00+08:00",
    )


def _port_with_session(
    session: _Session,
) -> MySQLWorkspaceProjectionBindingStore:
    port = object.__new__(MySQLWorkspaceProjectionBindingStore)

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(session)

    port._write = _write  # type: ignore[method-assign]
    return port


@pytest.mark.asyncio
async def test_mysql_binding_commit_appends_event_before_installing_head() -> None:
    transition = _initial_transition()
    session = _Session()
    port = _port_with_session(session)

    committed = await port.commit_transition(transition)

    statements = [sql for sql, _parameters in session.calls]
    assert statements[0].endswith("FOR UPDATE")
    assert statements[1].startswith("INSERT INTO memory_workspace_projection_events")
    assert statements[2].startswith("INSERT INTO memory_workspace_projection_heads")
    event_parameters = session.calls[1][1]
    assert event_parameters["event_sha256"] == transition.event.event_sha256
    assert event_parameters["payload_sha256"] == transition.event.event_sha256
    assert committed == transition.binding


@pytest.mark.asyncio
async def test_mysql_binding_commit_is_idempotent_for_exact_event() -> None:
    transition = _initial_transition()
    session = _Session(transition.binding.safe_dict())
    port = _port_with_session(session)

    committed = await port.commit_transition(transition)

    assert committed == transition.binding
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_mysql_binding_commit_rejects_revision_or_event_drift() -> None:
    transition = _initial_transition()
    drifted = transition.binding.safe_dict()
    drifted["revision"] = 2
    drifted["last_event_sha256"] = "d" * 64
    session = _Session(drifted)
    port = _port_with_session(session)

    with pytest.raises(WorkspaceProjectionRevisionConflict):
        await port.commit_transition(transition)

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_mysql_adapter_refuses_unisolated_projection_rebuild() -> None:
    initial = _initial_transition()
    rebuilt = rebuild_workspace_projection_generation(
        initial.binding,
        _identity(root="/other-workspace"),
        expected_revision=1,
        new_projection_generation_id="projection-2",
        new_owner_id="elysium-runtime-2",
        actor_id="elysium-runtime-2",
        audit_occurrence_id="workspace-rebuild-1",
        reason_code="different-root",
        occurred_at="2026-08-11T12:01:00+08:00",
    )
    port = object.__new__(MySQLWorkspaceProjectionBindingStore)

    with pytest.raises(WorkspaceProjectionBindingConflict):
        await port.commit_transition(rebuilt)
