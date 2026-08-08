"""Per-node projection progress with continuous frontier validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from .contracts import StorageBackendRuntime
from .models import BackendKind


class ProjectionProgressConflict(RuntimeError):
    """Projection configuration changed or its frontier has a gap."""


@dataclass(frozen=True, slots=True)
class ProjectionProgress:
    projection_name: str
    projection_node_id: str
    source_frontier: int
    source_digest: str
    config_digest: str
    status: str
    last_success_at: str | None
    backlog: int


class SQLProjectionProgressStore:
    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled:
            raise RuntimeError("projection progress requires enabled storage")
        self.runtime = runtime
        self.backend = runtime.backend

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    @staticmethod
    def _record(row: Any) -> ProjectionProgress:
        return ProjectionProgress(
            projection_name=str(row["projection_name"]), projection_node_id=str(row["projection_node_id"]),
            source_frontier=int(row["source_frontier"]), source_digest=str(row["source_digest"]),
            config_digest=str(row["config_digest"]), status=str(row["status"]),
            last_success_at=str(row["last_success_at"]) if row["last_success_at"] is not None else None,
            backlog=int(row["backlog"]),
        )

    async def get(self, projection_name: str, projection_node_id: str) -> ProjectionProgress | None:
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("""SELECT * FROM projection_progress
                WHERE projection_name=:name AND projection_node_id=:node"""), {"name": projection_name, "node": projection_node_id})).mappings().first()
        return self._record(row) if row is not None else None

    async def advance(self, *, projection_name: str, projection_node_id: str, expected_frontier: int, next_frontier: int, source_digest: str, config_digest: str, backlog: int = 0) -> ProjectionProgress:
        expected_frontier, next_frontier = int(expected_frontier), int(next_frontier)
        if expected_frontier < 0 or next_frontier < expected_frontier or next_frontier > expected_frontier + 1:
            raise ProjectionProgressConflict("projection frontier must advance continuously")
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("""SELECT * FROM projection_progress
                WHERE projection_name=:name AND projection_node_id=:node""" + self._for_update), {"name": projection_name, "node": projection_node_id})).mappings().first()
            current = self._record(row) if row is not None else None
            current_frontier = current.source_frontier if current is not None else 0
            if current_frontier != expected_frontier:
                raise ProjectionProgressConflict(f"projection frontier conflict: expected={expected_frontier}:actual={current_frontier}")
            if current is not None and current.config_digest != config_digest:
                raise ProjectionProgressConflict("projection config digest changed")
            if row is None:
                await uow.session.execute(text("""INSERT INTO projection_progress
                    (projection_name,projection_node_id,source_frontier,source_digest,config_digest,status,last_success_at,backlog)
                    VALUES (:name,:node,:frontier,:source,:config,'ready',CURRENT_TIMESTAMP,:backlog)"""),
                    {"name": projection_name, "node": projection_node_id, "frontier": next_frontier, "source": source_digest, "config": config_digest, "backlog": max(0, int(backlog))})
            else:
                await uow.session.execute(text("""UPDATE projection_progress SET source_frontier=:frontier,source_digest=:source,
                    status='ready',last_success_at=CURRENT_TIMESTAMP,backlog=:backlog WHERE projection_name=:name AND projection_node_id=:node"""),
                    {"frontier": next_frontier, "source": source_digest, "backlog": max(0, int(backlog)), "name": projection_name, "node": projection_node_id})
            row = (await uow.session.execute(text("SELECT * FROM projection_progress WHERE projection_name=:name AND projection_node_id=:node"), {"name": projection_name, "node": projection_node_id})).mappings().one()
        return self._record(row)


__all__ = ["ProjectionProgress", "ProjectionProgressConflict", "SQLProjectionProgressStore"]
