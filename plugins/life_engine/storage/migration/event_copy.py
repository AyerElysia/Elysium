"""Read-only SQLite to selectable-backend copy for the Life Event ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...service.event_bus import life_event_from_dict
from ..event_contracts import (
    LifeEventSnapshotCursor,
    LifeEventSnapshotImportPort,
    LifeEventSnapshotRecord,
)
from .copy_authority import CopyAuthorityToken, MySQLCopyAuthorityRegistry


class LifeEventCopyError(RuntimeError):
    """Raised when source evidence or target equivalence cannot be proven."""


@dataclass(frozen=True, slots=True)
class LifeEventCopyReport:
    """Bounded evidence from one idempotent event-ledger copy."""

    source_path: str
    source_count: int
    source_earliest_position: int
    source_latest_position: int
    copied_count: int
    consumer_count: int
    source_root_sha256: str
    target_root_sha256: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready report without credentials or payload contents."""

        return {
            "source_path": self.source_path,
            "source_count": self.source_count,
            "source_earliest_position": self.source_earliest_position,
            "source_latest_position": self.source_latest_position,
            "copied_count": self.copied_count,
            "consumer_count": self.consumer_count,
            "source_root_sha256": self.source_root_sha256,
            "target_root_sha256": self.target_root_sha256,
            "verified": self.verified,
        }


def _open_source(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.is_file():
        raise LifeEventCopyError(f"Life Event source does not exist: {resolved}")
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or str(integrity[0]).lower() != "ok":
        connection.close()
        raise LifeEventCopyError("Life Event source failed SQLite integrity_check")
    return connection


def _source_record(row: sqlite3.Row) -> LifeEventSnapshotRecord:
    payload_json = str(row["payload_json"])
    calculated = hashlib.sha256(payload_json.encode()).hexdigest()
    expected = str(row["payload_hash"])
    if calculated != expected:
        raise LifeEventCopyError(
            f"source payload hash mismatch at position {row['ingest_position']}"
        )
    raw = json.loads(payload_json)
    if not isinstance(raw, dict):
        raise LifeEventCopyError("source Life Event payload is not an object")
    life_event_from_dict(raw)
    return LifeEventSnapshotRecord(
        ingest_position=int(row["ingest_position"]),
        source_sequence=int(row["source_sequence"]),
        occurrence_id=str(row["occurrence_id"]),
        source_event_id=str(row["source_event_id"]),
        occurred_at=str(row["occurred_at"]),
        recorded_at=str(row["recorded_at"]),
        payload_json=payload_json,
        payload_hash=expected,
    )


def _root_update(
    digest: Any,
    *,
    position: int,
    occurrence_id: str,
    payload_hash: str,
) -> None:
    digest.update(str(int(position)).encode())
    digest.update(b"\0")
    digest.update(str(occurrence_id).encode())
    digest.update(b"\0")
    digest.update(str(payload_hash).encode())
    digest.update(b"\n")


async def copy_life_events_from_sqlite(
    source_path: str | Path,
    target: LifeEventSnapshotImportPort,
    *,
    copy_registry: MySQLCopyAuthorityRegistry,
    token: CopyAuthorityToken,
    batch_size: int = 500,
) -> LifeEventCopyReport:
    """Copy one immutable snapshot ledger and prove identity/hash/order parity."""

    if int(batch_size) <= 0:
        raise ValueError("batch_size must be positive")
    source = Path(source_path).resolve()
    connection = _open_source(source)
    source_root = hashlib.sha256()
    target_root = hashlib.sha256()
    copied_count = 0
    try:
        bounds = connection.execute(
            """SELECT COUNT(*) AS total, MIN(ingest_position) AS earliest,
            MAX(ingest_position) AS latest FROM raw_life_events"""
        ).fetchone()
        if bounds is None:
            raise LifeEventCopyError("Life Event source bounds are unavailable")
        source_count = int(bounds["total"] or 0)
        source_earliest = int(bounds["earliest"] or 0)
        source_latest = int(bounds["latest"] or 0)
        after_position = 0
        while True:
            rows = connection.execute(
                """SELECT ingest_position, occurrence_id, source_event_id,
                source_sequence, occurred_at, recorded_at,
                payload_json, payload_hash
                FROM raw_life_events WHERE ingest_position > ?
                ORDER BY ingest_position LIMIT ?""",
                (after_position, int(batch_size)),
            ).fetchall()
            if not rows:
                break
            records = [_source_record(row) for row in rows]
            persisted = await target.import_snapshot_records(records)
            occurrence_ids = [str(row["occurrence_id"]) for row in rows]
            digests = await target.occurrence_digests(occurrence_ids)
            target_by_occurrence = {item.occurrence_id: item for item in digests}
            for row, stored in zip(rows, persisted, strict=True):
                position = int(row["ingest_position"])
                occurrence_id = str(row["occurrence_id"])
                payload_hash = str(row["payload_hash"])
                target_digest = target_by_occurrence.get(occurrence_id)
                actual_hash = target_digest.payload_hash if target_digest else ""
                actual_position = target_digest.position if target_digest else 0
                if (
                    target_digest is None
                    or stored.position != position
                    or actual_position != position
                    or actual_hash != payload_hash
                ):
                    await copy_registry.record_conflict(
                        token,
                        domain_name="life_event",
                        source_identity=occurrence_id,
                        expected_hash=payload_hash,
                        actual_hash=actual_hash,
                        detail=(
                            f"source_position={position}; "
                            f"target_position={actual_position}; "
                            f"import_position={stored.position}"
                        ),
                    )
                    raise LifeEventCopyError(
                        f"Life Event target mismatch: {occurrence_id}"
                    )
                _root_update(
                    source_root,
                    position=position,
                    occurrence_id=occurrence_id,
                    payload_hash=payload_hash,
                )
                _root_update(
                    target_root,
                    position=actual_position,
                    occurrence_id=occurrence_id,
                    payload_hash=actual_hash,
                )
            copied_count += len(rows)
            after_position = int(rows[-1]["ingest_position"])
            await copy_registry.set_progress(
                token,
                copied_records=copied_count,
            )

        cursor_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(raw_event_consumer_offsets)"
            ).fetchall()
        }
        revision_expression = "revision" if "revision" in cursor_columns else "1"
        consumer_rows = connection.execute(
            f"""SELECT consumer_id, ingest_position, {revision_expression} AS revision,
            updated_at, metadata_json FROM raw_event_consumer_offsets
            ORDER BY consumer_id"""
        ).fetchall()
        snapshot_cursors: list[LifeEventSnapshotCursor] = []
        for row in consumer_rows:
            identity = str(row["consumer_id"])
            position = int(row["ingest_position"])
            if position > source_latest:
                await copy_registry.record_conflict(
                    token,
                    domain_name="life_event_consumer",
                    source_identity=identity,
                    expected_hash="",
                    actual_hash="",
                    detail=(
                        f"consumer_position={position}; source_latest={source_latest}"
                    ),
                )
                raise LifeEventCopyError(
                    f"consumer cursor exceeds source frontier: {identity}"
                )
            metadata = json.loads(str(row["metadata_json"] or "{}"))
            if not isinstance(metadata, dict):
                raise LifeEventCopyError(
                    f"consumer cursor metadata is not an object: {identity}"
                )
            snapshot_cursors.append(
                LifeEventSnapshotCursor(
                    consumer_id=identity,
                    ingest_position=position,
                    revision=max(1, int(row["revision"])),
                    updated_at=str(row["updated_at"]),
                    metadata_json=str(row["metadata_json"] or "{}"),
                )
            )
        await target.import_snapshot_cursors(
            snapshot_cursors,
            source_frontier=source_latest,
        )
        if copied_count != source_count:
            raise LifeEventCopyError(
                f"Life Event row count mismatch: {copied_count} != {source_count}"
            )
        source_root_sha256 = source_root.hexdigest()
        target_root_sha256 = target_root.hexdigest()
        if source_root_sha256 != target_root_sha256:
            raise LifeEventCopyError("Life Event aggregate root mismatch")
        return LifeEventCopyReport(
            source_path=str(source),
            source_count=source_count,
            source_earliest_position=source_earliest,
            source_latest_position=source_latest,
            copied_count=copied_count,
            consumer_count=len(consumer_rows),
            source_root_sha256=source_root_sha256,
            target_root_sha256=target_root_sha256,
            verified=True,
        )
    finally:
        connection.close()


__all__ = [
    "LifeEventCopyError",
    "LifeEventCopyReport",
    "copy_life_events_from_sqlite",
]
