"""Content-free multi-writer health observation.

Specification section 20 (observability and health): a node must be able to
report readiness without exposing authority tokens, claim tokens, database
passwords, platform secrets, message bodies or first-person subject files.

This module performs read-only aggregation over the shared multi-writer
tables and never acquires a claim, never locks a row, and never reads
payload contents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

# Must stay in sync with multi_writer_protocol._MULTI_WRITER_ANCHOR_TABLE.
_MULTI_WRITER_ANCHOR_TABLE = "operations"

_MULTI_WRITER_TABLES = (
    "operations",
    "operation_receipts",
    "outbox_actions",
    "projection_progress",
    "stream_turns",
    "heartbeat_operations",
)


@dataclass(frozen=True, slots=True)
class ProjectionReadiness:
    projection_name: str
    projection_node_id: str
    source_frontier: int
    status: str
    backlog: int
    last_success_at: str | None


@dataclass(frozen=True, slots=True)
class MultiWriterHealthSnapshot:
    status: str  # ready | not_ready | degraded | failed | disabled
    generation_id: str
    schema_version: int
    protocol_version: int
    local_claim_count: int
    expired_claim_count: int
    operation_counts: dict[str, int] = field(default_factory=dict)
    outbox_counts: dict[str, int] = field(default_factory=dict)
    projections: list[ProjectionReadiness] = field(default_factory=list)
    last_successful_commit_at: str | None = None
    missing_tables: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()


async def observe_multi_writer_health(
    runtime: Any,
    *,
    local_owner: str = "",
    generation_id: str = "",
    schema_version: int = 0,
    protocol_version: int = 0,
) -> MultiWriterHealthSnapshot:
    """Aggregate read-only health counters for the shared multi-writer tables.

    Any missing table is reported in ``missing_tables`` and degrades the
    status to ``not_ready``; a connection-level failure flips to ``failed``.
    The snapshot never contains payloads, hashes, secrets or owner strings.
    """
    if not getattr(runtime, "enabled", False) or getattr(runtime, "session_factory", None) is None:
        return MultiWriterHealthSnapshot(
            status="disabled",
            generation_id=generation_id,
            schema_version=int(schema_version),
            protocol_version=int(protocol_version),
            local_claim_count=0,
            expired_claim_count=0,
            notes=["storage runtime is not enabled"],
        )

    operation_counts: dict[str, int] = {}
    outbox_counts: dict[str, int] = {}
    projections: list[ProjectionReadiness] = []
    local_claims = 0
    expired_claims = 0
    last_commit: str | None = None
    missing: list[str] = []
    notes: list[str] = []
    hard_failure: str | None = None

    now_literal = "CURRENT_TIMESTAMP"  # unused placeholder, kept for clarity

    async with runtime.session_factory() as session:
        for table in _MULTI_WRITER_TABLES:
            try:
                await session.scalar(text(f"SELECT 1 FROM {table} LIMIT 1"))
            except (OperationalError, ProgrammingError):
                missing.append(table)

        anchor_ready = _MULTI_WRITER_ANCHOR_TABLE not in missing

        if anchor_ready:
            try:
                rows = (
                    await session.execute(
                        text(
                            "SELECT operation_type, status, COUNT(*) AS n "
                            "FROM operations GROUP BY operation_type, status"
                        )
                    )
                ).mappings().all()
                for row in rows:
                    key = f"{row['operation_type']}:{row['status']}"
                    operation_counts[key] = int(row["n"])
            except (OperationalError, ProgrammingError) as exc:
                hard_failure = f"operations scan failed: {exc}"

            try:
                rows = (
                    await session.execute(
                        text(
                            "SELECT status, COUNT(*) AS n FROM outbox_actions GROUP BY status"
                        )
                    )
                ).mappings().all()
                for row in rows:
                    outbox_counts[str(row["status"])] = int(row["n"])
            except (OperationalError, ProgrammingError) as exc:
                notes.append(f"outbox scan unavailable: {exc}")

            try:
                rows = (
                    await session.execute(
                        text(
                            "SELECT projection_name, projection_node_id, source_frontier, "
                            "status, backlog, last_success_at FROM projection_progress"
                        )
                    )
                ).mappings().all()
                for row in rows:
                    projections.append(
                        ProjectionReadiness(
                            projection_name=str(row["projection_name"]),
                            projection_node_id=str(row["projection_node_id"]),
                            source_frontier=int(row["source_frontier"] or 0),
                            status=str(row["status"] or ""),
                            backlog=int(row["backlog"] or 0),
                            last_success_at=_iso(row["last_success_at"]),
                        )
                    )
            except (OperationalError, ProgrammingError) as exc:
                notes.append(f"projection scan unavailable: {exc}")

            try:
                receipt_row = await session.execute(
                    text("SELECT MAX(committed_at) AS latest FROM operation_receipts")
                )
                latest = receipt_row.scalar_one_or_none()
                last_commit = _iso(latest)
            except (OperationalError, ProgrammingError) as exc:
                notes.append(f"receipt scan unavailable: {exc}")

            if local_owner:
                try:
                    local_claims = int(
                        await session.scalar(
                            text(
                                "SELECT COUNT(*) FROM operations WHERE claim_owner = :owner"
                            ),
                            {"owner": local_owner},
                        )
                        or 0
                    )
                except (OperationalError, ProgrammingError) as exc:
                    notes.append(f"local claim scan unavailable: {exc}")

            now_iso = datetime.now(UTC).isoformat()
            for table, statuses in (
                ("operations", ("claimed", "processing")),
                ("stream_turns", ("claimed", "processing")),
                ("heartbeat_operations", ("claimed", "processing")),
                ("outbox_actions", ("claimed", "sending")),
            ):
                if table in missing:
                    continue
                try:
                    # Compare timestamps in Python: SQLite stores ISO strings
                    # while CURRENT_TIMESTAMP is space-separated, so an SQL
                    # comparison would silently never match.
                    placeholders = ", ".join(f":s{i}" for i in range(len(statuses)))
                    params = {f"s{i}": value for i, value in enumerate(statuses)}
                    rows = (
                        await session.execute(
                            text(
                                f"SELECT lease_until FROM {table} "
                                f"WHERE status IN ({placeholders})"
                            ),
                            params,
                        )
                    ).mappings().all()
                    for row in rows:
                        parsed = _iso(row["lease_until"])
                        if parsed is not None and parsed < now_iso:
                            expired_claims += 1
                except (OperationalError, ProgrammingError) as exc:
                    notes.append(f"expired claim scan unavailable ({table}): {exc}")

    if hard_failure is not None:
        status = "failed"
        notes.append(hard_failure)
    elif not anchor_ready:
        status = "not_ready"
        notes.append("multi-writer anchor table is not deployed")
    elif outbox_counts.get("unknown", 0) > 0:
        status = "degraded"
        notes.append("unknown outbox actions require reconciliation")
    elif operation_counts and any(
        key.endswith(":failed") or key.endswith(":conflict")
        for key in operation_counts
    ):
        status = "degraded"
        notes.append("failed or conflicting operations require review")
    elif expired_claims > 0:
        status = "degraded"
        notes.append("expired claims are eligible for takeover")
    else:
        status = "ready"

    return MultiWriterHealthSnapshot(
        status=status,
        generation_id=generation_id,
        schema_version=int(schema_version),
        protocol_version=int(protocol_version),
        local_claim_count=local_claims,
        expired_claim_count=expired_claims,
        operation_counts=operation_counts,
        outbox_counts=outbox_counts,
        projections=projections,
        last_successful_commit_at=last_commit,
        missing_tables=missing,
        notes=notes,
    )


__all__ = [
    "MultiWriterHealthSnapshot",
    "ProjectionReadiness",
    "observe_multi_writer_health",
]
