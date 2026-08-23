"""Immutable operational view over the retired ThoughtStream snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .legacy_snapshot import LegacyStreamsSnapshot, read_legacy_streams_snapshot


@dataclass(frozen=True, slots=True)
class ArchivedThoughtStream:
    id: str
    title: str
    status: str
    curiosity_score: float
    advance_count: int
    last_thought: str
    source_ordinal: int
    row_sha256: str


class LegacyThoughtStreamArchive:
    """Strict, no-write adapter used only by offline/operations diagnostics."""

    def __init__(self, snapshot: LegacyStreamsSnapshot) -> None:
        self.snapshot = snapshot

    @classmethod
    def open(cls, path: str | Path) -> LegacyThoughtStreamArchive:
        return cls(read_legacy_streams_snapshot(path))

    @property
    def current_revision(self) -> int:
        if self.snapshot.global_revision is not None:
            return self.snapshot.global_revision
        return max(
            (
                int(row.original_fields.get("revision") or 0)
                for row in self.snapshot.rows
            ),
            default=0,
        )

    def list_for_projection(
        self,
        *,
        include_dormant: bool,
    ) -> list[ArchivedThoughtStream]:
        accepted_statuses = {"active", "dormant"} if include_dormant else {"active"}
        rows: list[ArchivedThoughtStream] = []
        for row in self.snapshot.rows:
            values = row.original_fields
            status = str(values.get("status") or "active")
            if status not in accepted_statuses:
                continue
            rows.append(
                ArchivedThoughtStream(
                    id=str(values["id"]),
                    title=str(values["title"]),
                    status=status,
                    curiosity_score=float(values.get("curiosity_score") or 0.0),
                    advance_count=int(values.get("advance_count") or 0),
                    last_thought=str(values.get("last_thought") or ""),
                    source_ordinal=row.source_ordinal,
                    row_sha256=row.row_sha256,
                )
            )
        return rows


__all__ = ["ArchivedThoughtStream", "LegacyThoughtStreamArchive"]
