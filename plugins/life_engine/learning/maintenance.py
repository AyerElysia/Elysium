"""Content-free maintenance run evidence for the learning spiral."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


class LearningPhase(StrEnum):
    """Independently recoverable learning maintenance phases."""

    REFLECTION = "reflection"
    EPISTEMIC_BACKFILL = "epistemic_backfill"
    AUDIT = "audit"
    COMPRESSION = "compression"
    DISTILLATION = "distillation"
    METRICS = "metrics"
    STALENESS = "staleness"


class LearningPhaseOutcome(StrEnum):
    """A phase attempt result without semantic payloads."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LearningMaintenanceEvent:
    """One append-only, content-free phase attempt."""

    event_id: str
    run_id: str
    phase: str
    outcome: str
    started_at: str
    finished_at: str
    duration_ms: int
    pending_count: int | None
    error_type: str
    error_fingerprint: str
    schema_version: int = 1

    @classmethod
    def started(
        cls,
        *,
        run_id: str,
        phase: LearningPhase,
        started_at: datetime,
        pending_count: int | None,
    ) -> LearningMaintenanceEvent:
        return cls(
            event_id=f"learning_run_{uuid4().hex}",
            run_id=run_id,
            phase=phase.value,
            outcome=LearningPhaseOutcome.STARTED.value,
            started_at=started_at.isoformat(),
            finished_at="",
            duration_ms=0,
            pending_count=(
                None if pending_count is None else max(0, int(pending_count))
            ),
            error_type="",
            error_fingerprint="",
        )

    @classmethod
    def succeeded(
        cls,
        *,
        run_id: str,
        phase: LearningPhase,
        started_at: datetime,
        pending_count: int | None,
    ) -> LearningMaintenanceEvent:
        return cls._build(
            run_id=run_id,
            phase=phase,
            outcome=LearningPhaseOutcome.SUCCEEDED,
            started_at=started_at,
            pending_count=pending_count,
            error=None,
        )

    @classmethod
    def failed(
        cls,
        *,
        run_id: str,
        phase: LearningPhase,
        started_at: datetime,
        pending_count: int | None,
        error: Exception,
    ) -> LearningMaintenanceEvent:
        return cls._build(
            run_id=run_id,
            phase=phase,
            outcome=LearningPhaseOutcome.FAILED,
            started_at=started_at,
            pending_count=pending_count,
            error=error,
        )

    @classmethod
    def _build(
        cls,
        *,
        run_id: str,
        phase: LearningPhase,
        outcome: LearningPhaseOutcome,
        started_at: datetime,
        pending_count: int | None,
        error: Exception | None,
    ) -> LearningMaintenanceEvent:
        finished_at = datetime.now(UTC)
        error_type = type(error).__name__ if error is not None else ""
        error_fingerprint = ""
        if error is not None:
            error_fingerprint = hashlib.sha256(
                (
                    f"{type(error).__module__}:{type(error).__qualname__}"
                ).encode("utf-8")
            ).hexdigest()
        return cls(
            event_id=f"learning_run_{uuid4().hex}",
            run_id=run_id,
            phase=phase.value,
            outcome=outcome.value,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_ms=max(
                0,
                int((finished_at - started_at).total_seconds() * 1000),
            ),
            pending_count=(
                None if pending_count is None else max(0, int(pending_count))
            ),
            error_type=error_type,
            error_fingerprint=error_fingerprint,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LearningMaintenanceEvent:
        return cls(
            event_id=str(value["event_id"]),
            run_id=str(value["run_id"]),
            phase=str(value["phase"]),
            outcome=str(value["outcome"]),
            started_at=str(value["started_at"]),
            finished_at=str(value["finished_at"]),
            duration_ms=max(0, int(value.get("duration_ms") or 0)),
            pending_count=(
                None
                if value.get("pending_count") is None
                else max(0, int(value["pending_count"]))
            ),
            error_type=str(value.get("error_type") or ""),
            error_fingerprint=str(value.get("error_fingerprint") or ""),
            schema_version=int(value.get("schema_version") or 1),
        )


@runtime_checkable
class LearningMaintenanceJournalPort(Protocol):
    """Append-only phase evidence with a bounded cached health view."""

    async def initialize(self) -> None:
        """Restore a bounded latest-event cache."""

    async def append(self, event: LearningMaintenanceEvent) -> None:
        """Durably append exactly one event or raise."""

    def health_snapshot(self) -> dict[str, Any]:
        """Return content-free latest results without blocking I/O."""


class LocalLearningMaintenanceJournal:
    """Bounded-tail JSONL journal used while selectable storage is disabled."""

    _TAIL_BYTES = 1024 * 1024

    def __init__(self, workspace_path: str | Path) -> None:
        workspace = Path(workspace_path).resolve()
        self.path = workspace / ".life_learning" / "maintenance_runs.jsonl"
        self._lock = asyncio.Lock()
        self._initialized = False
        self._latest: dict[str, LearningMaintenanceEvent] = {}
        self._observed_events = 0

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            events = await asyncio.to_thread(self._read_bounded_tail)
            for event in events:
                self._remember(event)
            self._initialized = True

    async def append(self, event: LearningMaintenanceEvent) -> None:
        await self.initialize()
        async with self._lock:
            await asyncio.to_thread(self._append_sync, event)
            self._remember(event)

    def health_snapshot(self) -> dict[str, Any]:
        latest = {
            phase: event.to_dict() for phase, event in sorted(self._latest.items())
        }
        incomplete_or_failed = sum(
            event.outcome != LearningPhaseOutcome.SUCCEEDED.value
            for event in self._latest.values()
        )
        return {
            "status": "degraded" if incomplete_or_failed else "healthy",
            "journal": "local_jsonl",
            "initialized": self._initialized,
            "observed_events": self._observed_events,
            "latest_by_phase": latest,
        }

    def _remember(self, event: LearningMaintenanceEvent) -> None:
        self._latest[event.phase] = event
        self._observed_events += 1

    def _append_sync(self, event: LearningMaintenanceEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            event.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_bounded_tail(self) -> list[LearningMaintenanceEvent]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        offset = max(0, size - self._TAIL_BYTES)
        with self.path.open("rb") as handle:
            handle.seek(offset)
            if offset:
                handle.readline()
            raw_lines = handle.readlines()
        events: list[LearningMaintenanceEvent] = []
        for raw_line in raw_lines:
            try:
                value = json.loads(raw_line.decode("utf-8"))
                if isinstance(value, dict):
                    events.append(LearningMaintenanceEvent.from_dict(value))
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError):
                continue
        return events


__all__ = [
    "LearningMaintenanceEvent",
    "LearningMaintenanceJournalPort",
    "LearningPhase",
    "LearningPhaseOutcome",
    "LocalLearningMaintenanceJournal",
]
