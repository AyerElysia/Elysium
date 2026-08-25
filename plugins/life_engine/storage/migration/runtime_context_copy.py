"""Copy and verify the technical runtime checkpoint from a frozen snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import text

from src.kernel.storage import canonical_json

from ..contracts import StorageBackendRuntime, StorageWriterRole
from ..runtime_adapters import SQLRuntimeStateStore
from .manifest import LifeSnapshotError, load_snapshot_manifest
from .snapshot import inspect_sqlite_database, sha256_file

RUNTIME_CONTEXT_SOURCE = PurePosixPath(
    "life_engine_workspace/life_engine_context.json"
)
RUNTIME_CONTEXT_DATABASE_SOURCE = PurePosixPath("life_storage/local.sqlite3")
RUNTIME_CONTEXT_NAMESPACE = "life_engine.runtime_context"
RUNTIME_CONTEXT_STATE_KEY = "global"
RUNTIME_CONTEXT_SCHEMA_VERSION = 2


class RuntimeContextCopyError(RuntimeError):
    """Raised when a frozen technical checkpoint cannot be proven."""


def _safe_snapshot_path(snapshot: Path, relative: str) -> Path:
    candidate = (snapshot / relative).resolve()
    try:
        candidate.relative_to(snapshot)
    except ValueError as exc:
        raise RuntimeContextCopyError(
            "runtime context snapshot path escapes root"
        ) from exc
    return candidate


def _snapshot_file(
    snapshot: Path,
    manifest: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    matches = [
        dict(item)
        for item in list(manifest.get("exact_files") or [])
        if isinstance(item, dict)
        and str(item.get("source_relative") or "")
        == RUNTIME_CONTEXT_SOURCE.as_posix()
    ]
    if len(matches) != 1:
        raise RuntimeContextCopyError(
            "snapshot must declare exactly one runtime context file"
        )
    evidence = matches[0]
    backup_relative = str(evidence.get("backup_relative") or "")
    source = _safe_snapshot_path(snapshot, backup_relative)
    expected_hash = str(evidence.get("sha256") or "")
    expected_bytes = int(evidence.get("bytes") or -1)
    if len(expected_hash) != 64 or not source.is_file():
        raise RuntimeContextCopyError(
            "runtime context snapshot evidence is incomplete"
        )
    if source.stat().st_size != expected_bytes or sha256_file(source) != expected_hash:
        raise RuntimeContextCopyError("runtime context snapshot checksum mismatch")
    return source, evidence


def _snapshot_database(
    snapshot: Path,
    manifest: dict[str, Any],
) -> tuple[Path, dict[str, Any]] | None:
    matches = [
        dict(item)
        for item in list(manifest.get("sqlite") or [])
        if isinstance(item, dict)
        and str(item.get("source_relative") or "")
        == RUNTIME_CONTEXT_DATABASE_SOURCE.as_posix()
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeContextCopyError(
            "snapshot must declare exactly one selected runtime database"
        )
    evidence = matches[0]
    backup_relative = str(evidence.get("backup_relative") or "")
    source = _safe_snapshot_path(snapshot, backup_relative)
    expected_hash = str(
        evidence.get("backup_sha256") or evidence.get("sha256") or ""
    )
    expected_bytes = int(
        evidence.get("backup_bytes") or evidence.get("bytes") or -1
    )
    if len(expected_hash) != 64 or not source.is_file():
        raise RuntimeContextCopyError(
            "selected runtime database evidence is incomplete"
        )
    if source.stat().st_size != expected_bytes or sha256_file(source) != expected_hash:
        raise RuntimeContextCopyError(
            "selected runtime database checksum mismatch"
        )
    try:
        inspection = inspect_sqlite_database(source)
    except (OSError, sqlite3.Error, LifeSnapshotError) as exc:
        raise RuntimeContextCopyError(
            "selected runtime database integrity verification failed"
        ) from exc
    expected_root = str(evidence.get("database_root_sha256") or "")
    if expected_root and inspection["database_root_sha256"] != expected_root:
        raise RuntimeContextCopyError(
            "selected runtime database logical root mismatch"
        )
    return source, evidence


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise RuntimeContextCopyError(f"runtime context {field} must be an integer")
    if isinstance(value, str):
        rendered = value.strip()
        if not rendered.isdigit() or (
            len(rendered) > 1 and rendered.startswith("0")
        ):
            raise RuntimeContextCopyError(f"runtime context {field} is invalid")
        parsed = int(rendered)
    elif isinstance(value, int):
        parsed = value
    else:
        raise RuntimeContextCopyError(f"runtime context {field} must be an integer")
    if parsed < 0:
        raise RuntimeContextCopyError(f"runtime context {field} is invalid")
    return parsed


def _validate_event_list(
    raw: Any,
    *,
    field: str,
    event_ids: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise RuntimeContextCopyError(f"runtime context {field} must be a list")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeContextCopyError(
                f"runtime context {field} contains invalid event"
            )
        event_id = str(item.get("event_id") or "").strip()
        if not event_id or event_id in event_ids:
            raise RuntimeContextCopyError(
                f"runtime context {field} contains duplicate event id"
            )
        _nonnegative_int(item.get("sequence"), field=f"{field}.sequence")
        event_ids.add(event_id)
        result.append(dict(item))
    return result


def _load_payload(source: Path) -> tuple[dict[str, Any], dict[str, int]]:
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeContextCopyError("runtime context snapshot is unreadable") from exc
    return _validate_payload(payload)


def _validate_payload(
    payload: Any,
) -> tuple[dict[str, Any], dict[str, int]]:
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 2:
        raise RuntimeContextCopyError(
            "runtime context snapshot version is unsupported"
        )
    state = payload.get("state")
    if not isinstance(state, dict):
        raise RuntimeContextCopyError("runtime context state is missing")
    heartbeat_count = _nonnegative_int(
        state.get("heartbeat_count"), field="heartbeat_count"
    )
    event_sequence = _nonnegative_int(
        state.get("event_sequence"), field="event_sequence"
    )
    heartbeat_cursor = _nonnegative_int(
        state.get("heartbeat_context_cursor"),
        field="heartbeat_context_cursor",
    )
    if heartbeat_cursor > event_sequence:
        raise RuntimeContextCopyError(
            "runtime context heartbeat cursor exceeds event sequence"
        )
    event_ids: set[str] = set()
    pending = _validate_event_list(
        payload.get("pending_events"),
        field="pending_events",
        event_ids=event_ids,
    )
    history = _validate_event_list(
        payload.get("event_history"),
        field="event_history",
        event_ids=event_ids,
    )
    for item in [*pending, *history]:
        if _nonnegative_int(item.get("sequence"), field="event.sequence") > event_sequence:
            raise RuntimeContextCopyError(
                "runtime context event exceeds event sequence"
            )
    frontiers = {
        "heartbeat_count": heartbeat_count,
        "event_sequence": event_sequence,
        "heartbeat_context_cursor": heartbeat_cursor,
        "pending_event_count": len(pending),
        "history_event_count": len(history),
    }
    return dict(payload), frontiers


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _initial_root(
    source_sha256: str,
    payload_sha256: str,
    *,
    revision: int,
    schema_version: int,
    frontiers: dict[str, int],
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "source_sha256": source_sha256,
                "payload_sha256": payload_sha256,
                "revision": int(revision),
                "schema_version": int(schema_version),
                "frontiers": frontiers,
            }
        ).encode("utf-8")
    ).hexdigest()


def _read_database_payload(
    source: Path,
) -> tuple[dict[str, Any], dict[str, int], int, int, str, str]:
    connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """SELECT revision, schema_version, payload_json, payload_sha256
            FROM runtime_states
            WHERE namespace = ? AND state_key = ?""",
            (RUNTIME_CONTEXT_NAMESPACE, RUNTIME_CONTEXT_STATE_KEY),
        ).fetchone()
        if row is None:
            raise RuntimeContextCopyError(
                "selected runtime database checkpoint is missing"
            )
        revision = _nonnegative_int(row["revision"], field="revision")
        schema_version = _nonnegative_int(
            row["schema_version"], field="schema_version"
        )
        raw = str(row["payload_json"])
        stored_hash = str(row["payload_sha256"])
        actual_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if actual_hash != stored_hash:
            raise RuntimeContextCopyError(
                "selected runtime checkpoint payload hash mismatch"
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeContextCopyError(
                "selected runtime checkpoint payload is unreadable"
            ) from exc
        payload, frontiers = _validate_payload(payload)
        return payload, frontiers, revision, schema_version, stored_hash, raw
    except sqlite3.Error as exc:
        raise RuntimeContextCopyError(
            "selected runtime database checkpoint is unreadable"
        ) from exc
    finally:
        connection.close()


async def copy_runtime_context_from_snapshot(
    snapshot_directory: str | Path,
    runtime: StorageBackendRuntime,
    *,
    expect_source_sha256: str | None = None,
    expect_heartbeat_count: int | None = None,
) -> dict[str, Any]:
    """Copy one exact technical checkpoint into candidate selected storage."""

    if runtime.writer_role != StorageWriterRole.CANDIDATE_COPY:
        raise RuntimeContextCopyError("runtime context candidate writer required")
    snapshot = Path(snapshot_directory).resolve(strict=True)
    try:
        manifest = load_snapshot_manifest(snapshot / "manifest.json")
    except LifeSnapshotError as exc:
        raise RuntimeContextCopyError(str(exc)) from exc
    if not bool(manifest.get("writer_frozen")):
        raise RuntimeContextCopyError(
            "runtime context requires a writer-frozen snapshot"
        )

    database = _snapshot_database(snapshot, manifest)
    if database is not None:
        source, evidence = database
        (
            payload,
            frontiers,
            revision,
            schema_version,
            payload_sha256,
            raw_payload,
        ) = _read_database_payload(source)
        source_sha256 = str(evidence.get("backup_sha256") or evidence.get("sha256"))
        source_relative = RUNTIME_CONTEXT_DATABASE_SOURCE.as_posix()
        source_bytes = int(
            evidence.get("backup_bytes") or evidence.get("bytes") or source.stat().st_size
        )
    else:
        source, evidence = _snapshot_file(snapshot, manifest)
        source_sha256 = str(evidence["sha256"])
        source_relative = RUNTIME_CONTEXT_SOURCE.as_posix()
        source_bytes = int(evidence["bytes"])
        payload, frontiers = _load_payload(source)
        revision = 1
        schema_version = RUNTIME_CONTEXT_SCHEMA_VERSION
        raw_payload = canonical_json(payload)
        payload_sha256 = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()

    if expect_source_sha256 and source_sha256 != str(expect_source_sha256):
        raise RuntimeContextCopyError(
            "runtime context source checksum precondition failed"
        )
    if (
        expect_heartbeat_count is not None
        and frontiers["heartbeat_count"] != int(expect_heartbeat_count)
    ):
        raise RuntimeContextCopyError("runtime context heartbeat precondition failed")

    await runtime.validate_writer()
    idempotent_replay = False
    async with runtime.unit_of_work() as uow:
        row = (
            (
                await uow.session.execute(
                    text(
                        """SELECT namespace, state_key, revision, schema_version,
                            payload_json, payload_sha256 FROM runtime_states
                        WHERE namespace = :namespace AND state_key = :state_key"""
                    ),
                    {
                        "namespace": RUNTIME_CONTEXT_NAMESPACE,
                        "state_key": RUNTIME_CONTEXT_STATE_KEY,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            raw = (
                row["payload_json"].decode("utf-8")
                if isinstance(row["payload_json"], bytes)
                else str(row["payload_json"])
            )
            if (
                int(row["revision"]) != revision
                or int(row["schema_version"]) != schema_version
                or raw != raw_payload
                or str(row["payload_sha256"]) != payload_sha256
            ):
                raise RuntimeContextCopyError(
                    "runtime context target conflicts with snapshot"
                )
            idempotent_replay = True
        else:
            now = await uow.session.scalar(
                text("SELECT STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')")
            )
            await uow.session.execute(
                text(
                    """INSERT INTO runtime_states (
                        namespace, state_key, revision, schema_version,
                        payload_json, payload_sha256, updated_at
                    ) VALUES (
                        :namespace, :state_key, :revision, :schema_version,
                        :payload_json, :payload_sha256, :updated_at
                    )"""
                ),
                {
                    "namespace": RUNTIME_CONTEXT_NAMESPACE,
                    "state_key": RUNTIME_CONTEXT_STATE_KEY,
                    "revision": revision,
                    "schema_version": schema_version,
                    "payload_json": raw_payload,
                    "payload_sha256": payload_sha256,
                    "updated_at": now,
                },
            )
    store = SQLRuntimeStateStore(runtime)
    record = await store.get_state(RUNTIME_CONTEXT_NAMESPACE, RUNTIME_CONTEXT_STATE_KEY)
    if (
        record is None
        or record.revision != revision
        or record.schema_version != schema_version
    ):
        raise RuntimeContextCopyError("runtime context post-copy record is missing")
    if record.payload != payload or record.payload_sha256 != payload_sha256:
        raise RuntimeContextCopyError("runtime context post-copy verification failed")
    return {
        "source_relative": source_relative,
        "source_sha256": source_sha256,
        "source_bytes": source_bytes,
        "payload_sha256": payload_sha256,
        "revision": revision,
        "schema_version": schema_version,
        "frontiers": frontiers,
        "initial_root_sha256": _initial_root(
            source_sha256,
            payload_sha256,
            revision=revision,
            schema_version=schema_version,
            frontiers=frontiers,
        ),
        "idempotent_replay": idempotent_replay,
        "verified": True,
    }


__all__ = [
    "RUNTIME_CONTEXT_DATABASE_SOURCE",
    "RUNTIME_CONTEXT_NAMESPACE",
    "RUNTIME_CONTEXT_SCHEMA_VERSION",
    "RUNTIME_CONTEXT_SOURCE",
    "RuntimeContextCopyError",
    "copy_runtime_context_from_snapshot",
]
