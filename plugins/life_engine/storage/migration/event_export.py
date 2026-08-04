"""Audited Life Event reverse export to a new immutable SQLite directory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.kernel.storage import canonical_json

from ..event_contracts import LifeEventSnapshotSourcePort
from ..event_schema import LOCAL_EVENT_SCHEMA_STATEMENTS


class LifeEventExportError(RuntimeError):
    """Raised when a reverse export cannot prove exact ledger equivalence."""


@dataclass(frozen=True, slots=True)
class LifeEventExportReport:
    """Secret-free evidence for one newly-created SQLite export."""

    destination_directory: str
    database_path: str
    event_count: int
    consumer_count: int
    earliest_position: int
    latest_position: int
    root_sha256: str
    manifest_sha256: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination_directory": self.destination_directory,
            "database_path": self.database_path,
            "event_count": self.event_count,
            "consumer_count": self.consumer_count,
            "earliest_position": self.earliest_position,
            "latest_position": self.latest_position,
            "root_sha256": self.root_sha256,
            "manifest_sha256": self.manifest_sha256,
            "verified": self.verified,
        }


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


async def export_life_events_to_sqlite(
    source: LifeEventSnapshotSourcePort,
    destination_directory: str | Path,
    *,
    batch_size: int = 500,
) -> LifeEventExportReport:
    """Create and independently verify a new SQLite ledger without overwrite."""

    if int(batch_size) <= 0:
        raise ValueError("batch_size must be positive")
    destination = Path(destination_directory).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=False)
    marker = destination / "EXPORT_INCOMPLETE"
    database_path = destination / "life_events.sqlite3"
    manifest_path = destination / "manifest.json"
    marker.write_text("Life Event reverse export is incomplete.\n", encoding="utf-8")

    connection = sqlite3.connect(database_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    source_root = hashlib.sha256()
    event_count = 0
    earliest_position = 0
    latest_position = 0
    try:
        for statement in LOCAL_EVENT_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        after_position = 0
        while True:
            records = await source.snapshot_records_after(
                after_position,
                limit=int(batch_size),
            )
            if not records:
                break
            positions = [record.ingest_position for record in records]
            if positions != sorted(positions) or positions[0] <= after_position:
                raise LifeEventExportError("source returned non-monotonic positions")
            parameters: list[tuple[Any, ...]] = []
            for record in records:
                calculated = hashlib.sha256(record.payload_json.encode()).hexdigest()
                if calculated != record.payload_hash:
                    raise LifeEventExportError(
                        f"source payload hash mismatch: {record.occurrence_id}"
                    )
                payload = json.loads(record.payload_json)
                if not isinstance(payload, dict):
                    raise LifeEventExportError("source event payload is not an object")
                parameters.append(
                    (
                        record.ingest_position,
                        record.occurrence_id,
                        record.source_event_id,
                        record.source_sequence,
                        record.occurred_at,
                        record.recorded_at,
                        record.payload_json,
                        record.payload_hash,
                    )
                )
                _root_update(
                    source_root,
                    position=record.ingest_position,
                    occurrence_id=record.occurrence_id,
                    payload_hash=record.payload_hash,
                )
            connection.executemany(
                """INSERT INTO raw_life_events (
                    ingest_position, occurrence_id, source_event_id,
                    source_sequence, occurred_at, recorded_at,
                    payload_json, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                parameters,
            )
            if earliest_position == 0:
                earliest_position = positions[0]
            latest_position = positions[-1]
            event_count += len(records)
            after_position = latest_position

        cursors = await source.snapshot_cursors()
        for cursor in cursors:
            if cursor.ingest_position > latest_position:
                raise LifeEventExportError(
                    f"consumer cursor exceeds export frontier: {cursor.consumer_id}"
                )
            metadata = json.loads(cursor.metadata_json)
            if not isinstance(metadata, dict):
                raise LifeEventExportError("consumer metadata is not an object")
            connection.execute(
                """INSERT INTO raw_event_consumer_offsets (
                    consumer_id, ingest_position, revision,
                    updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    cursor.consumer_id,
                    cursor.ingest_position,
                    cursor.revision,
                    cursor.updated_at,
                    canonical_json(metadata),
                ),
            )
        connection.commit()

        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise LifeEventExportError("export failed SQLite integrity_check")
        target_root = hashlib.sha256()
        verified_count = 0
        for row in connection.execute(
            """SELECT ingest_position, occurrence_id, payload_json, payload_hash
            FROM raw_life_events ORDER BY ingest_position"""
        ):
            payload_json = str(row["payload_json"])
            payload_hash = str(row["payload_hash"])
            if hashlib.sha256(payload_json.encode()).hexdigest() != payload_hash:
                raise LifeEventExportError(
                    f"export payload hash mismatch: {row['occurrence_id']}"
                )
            _root_update(
                target_root,
                position=int(row["ingest_position"]),
                occurrence_id=str(row["occurrence_id"]),
                payload_hash=payload_hash,
            )
            verified_count += 1
        if verified_count != event_count:
            raise LifeEventExportError("export row count changed during verification")
        root_sha256 = source_root.hexdigest()
        if target_root.hexdigest() != root_sha256:
            raise LifeEventExportError("export aggregate root mismatch")

        manifest = {
            "format": "elysium-life-event-sqlite-export-v1",
            "database": database_path.name,
            "event_count": event_count,
            "consumer_count": len(cursors),
            "earliest_position": earliest_position,
            "latest_position": latest_position,
            "root_sha256": root_sha256,
            "timestamp_columns": "UTC-normalized; payload_json bytes exact",
            "verified": True,
        }
        manifest_encoded = canonical_json(manifest)
        manifest_sha256 = hashlib.sha256(manifest_encoded.encode()).hexdigest()
        manifest_path.write_text(
            canonical_json({**manifest, "manifest_sha256": manifest_sha256}) + "\n",
            encoding="utf-8",
        )
        marker.unlink()
        return LifeEventExportReport(
            destination_directory=str(destination),
            database_path=str(database_path),
            event_count=event_count,
            consumer_count=len(cursors),
            earliest_position=earliest_position,
            latest_position=latest_position,
            root_sha256=root_sha256,
            manifest_sha256=manifest_sha256,
            verified=True,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "LifeEventExportError",
    "LifeEventExportReport",
    "export_life_events_to_sqlite",
]
