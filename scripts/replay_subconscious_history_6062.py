#!/usr/bin/env python3
"""Conservatively redeliver one evidenced work-set gap after heartbeat #6062.

This is NOT an exact restoration of the 119 lost runtime records. The immutable
range 262863..262976 contains 112 supported work-set occurrences. Old delivery
flags/order are unknown; missing occurrences receive NEW pending delivery order.
No original event, subject content, private rolling context, or consumer cursor
is edited, and recorded tool calls are NEVER executed.

Default is a read-only dry-run. Applying requires an explicitly expected latest
global revision/digest, offline ownership of main.py's existing OS lock, private
fsynced full-row/source backups, and an atomic global CAS plus recovery journal.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tomllib
from typing import Any, Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.life_engine.service.state_manager import event_to_dict
from plugins.life_engine.service.subconscious_ingest import reconstruct_workset_event
from plugins.life_engine.storage.event_adapters import SQLLifeEventStore, _parse_datetime
from src.kernel.storage import canonical_json

DATABASE_PATH = REPO_ROOT / "data/life_storage/local.sqlite3"
GLOBAL_NAMESPACE = "life_engine.runtime_context"
GLOBAL_KEY = "global"
OPERATION_ID = "subconscious-history-6062-redelivery-v1"
JOURNAL_NAMESPACE = "life_engine.recovery"
JOURNAL_KEY = OPERATION_ID
JOURNAL_SCHEMA = "elysium.subconscious_history_redelivery.v1"
RAW_LOWER = 262863
RAW_UPPER = 262976
RAW_COUNT = 114
WORKSET_COUNT = 112
RAW_MANIFEST_SHA256 = "188accb0023558879e95d303ef9ab90c804be4fcc132f77d0eeeb173c614d060"
WORKSET_MANIFEST_SHA256 = "ee6375f4b66005c90d5e2d9fb1c94ad903442a8b8f37e15e0e0da1332832b24b"
EXPECTED_GENERATION = "local-selectable-20260824-v3"
ROW_COLUMNS = (
    "namespace", "state_key", "revision", "schema_version",
    "payload_json", "payload_sha256", "updated_at",
)
RAW_COLUMNS = (
    "ingest_position", "occurrence_id", "source_event_id", "source_sequence",
    "occurred_at", "recorded_at", "payload_json", "payload_hash",
)
PRESERVED_TARGETS = (
    (GLOBAL_NAMESPACE, GLOBAL_KEY),
    ("life_heartbeat.rolling_context", "subconscious"),
    ("life_chatter.rolling_context", "chat_global"),
)
_DELIVERY_FIELDS = {
    "sequence", "heartbeat_context_consumed",
    "redelivery_operation_id", "redelivery_source_position",
    "content", "raw_content",
}


class ReplayRefused(RuntimeError):
    """Content-free precondition failure; never broaden the source range."""


class ReplayCommitUnknown(RuntimeError):
    """Commit was attempted: inspect the durable journal before any retry."""


def _require(value: bool, code: str) -> None:
    if not value:
        raise ReplayRefused(code)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _manifest(entries: list[dict[str, Any]]) -> str:
    return _sha(json.dumps(entries, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _manifest_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("ingest_position", "occurrence_id", "payload_hash")}


def assert_storage_fixed() -> None:
    """Inspect config only; do not initialize or claim any storage authority."""
    with (REPO_ROOT / "config/elysium.toml").open("rb") as handle:
        core = tomllib.load(handle).get("storage", {})
    with (REPO_ROOT / "config/plugins/life_engine/config.toml").open("rb") as handle:
        local = tomllib.load(handle).get("storage_local", {})
    _require(
        core.get("backend") == "local"
        and core.get("local_selectable_enabled") is True
        and core.get("multi_writer_enabled") is False
        and core.get("schema_version") == 3
        and core.get("backend_generation") == EXPECTED_GENERATION,
        "SelectedStorageConfigurationChanged",
    )
    _require(
        local.get("database_path") == "data/life_storage/local.sqlite3"
        and local.get("authority_state_path") == "data/life_storage/authority.json",
        "LocalStoragePathChanged",
    )
    expected = REPO_ROOT / "data/life_storage/local.sqlite3"
    _require(DATABASE_PATH == expected, "DatabaseTargetChanged")
    _require(expected.resolve(strict=True) == expected, "DatabasePathContainsSymlink")
    _require(stat.S_ISREG(expected.lstat().st_mode), "DatabaseIsNotRegular")


def assert_service_stopped(repository: Path, *, proc_root: Path = Path("/proc")) -> None:
    """Fail closed for this checkout's main.py/launcher, never stop a process."""
    _require(proc_root.is_dir(), "ProcessInspectionUnavailable")
    target = (repository / "main.py").resolve()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            candidates = [os.fsdecode(arg) for arg in args
                          if arg and Path(os.fsdecode(arg)).name == "main.py"]
            if not candidates:
                continue
            cwd = (entry / "cwd").resolve(strict=True)
            for value in candidates:
                path = Path(value)
                if (path if path.is_absolute() else cwd / path).resolve() == target:
                    raise ReplayRefused("RepositoryMainStillRunning")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise ReplayRefused("ProcessInspectionPermissionDenied") from exc


@contextmanager
def _offline_lock() -> Iterator[Callable[[], None]]:
    """Hold main.py's existing Linux OS lock without creating/writing the file."""
    import fcntl

    path = REPO_ROOT / "data/runtime/elysium.lock"
    _require(path.resolve(strict=True) == path, "InstanceLockPathContainsSymlink")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        original = os.fstat(descriptor)
        _require(stat.S_ISREG(original.st_mode), "InstanceLockIsNotRegular")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ReplayRefused("InstanceLockBusy") from exc
        def verify_lock() -> None:
            current = path.lstat()
            _require(stat.S_ISREG(current.st_mode)
                     and (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino),
                     "InstanceLockReplaced")

        try:
            verify_lock()
            yield verify_lock
        finally:
            verify_lock()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _open_database(*, writable: bool) -> sqlite3.Connection:
    path = DATABASE_PATH.resolve(strict=True)
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode={'rw' if writable else 'ro'}",
        uri=True, timeout=5, isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous=FULL" if writable else "PRAGMA query_only=ON")
    return connection


def _read_row(connection: sqlite3.Connection, namespace: str, key: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM runtime_states WHERE namespace=? AND state_key=?", (namespace, key),
    ).fetchone()
    return dict(row) if row is not None else None


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(row.get("payload_json"), str), "RuntimePayloadNotText")
    _require(_sha(row["payload_json"]) == row.get("payload_sha256"), "RuntimeDigestMismatch")
    value = json.loads(row["payload_json"])
    _require(isinstance(value, dict), "RuntimePayloadNotObject")
    return value


def _read_sources(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM raw_life_events WHERE ingest_position BETWEEN ? AND ? "
        "ORDER BY ingest_position", (RAW_LOWER, RAW_UPPER),
    )]
    _require(len(rows) == RAW_COUNT, "SourceRangeCountChanged")
    _require([row["ingest_position"] for row in rows] == list(range(RAW_LOWER, RAW_UPPER + 1)),
             "SourceRangeGap")
    workset: list[dict[str, Any]] = []
    templates: dict[str, dict[str, Any]] = {}
    event_ids: set[str] = set()
    refs: set[str] = set()
    calls: set[str] = set()
    for row in rows:
        _require(_sha(row["payload_json"]) == row["payload_hash"], "SourcePayloadDigestMismatch")
        value = json.loads(row["payload_json"])
        occurred_at = _parse_datetime(value.get("timestamp")) if isinstance(value, dict) else None
        _require(
            isinstance(value, dict)
            and value.get("occurrence_id") == row["occurrence_id"]
            and value.get("event_id") == row["source_event_id"]
            and value.get("source_sequence") == row["source_sequence"]
            and occurred_at is not None
            and occurred_at == _parse_datetime(row["occurred_at"])
            and isinstance(value.get("content"), str),
            "SourceColumnIdentityMismatch",
        )
        # The selected SQL adapter normalizes occurred_at to UTC but retains
        # the original timestamp representation in payload_json. Its pure
        # decoder hydrates ledger columns without replacing that source text.
        event = reconstruct_workset_event(
            SQLLifeEventStore._decode_event(row), next_sequence=lambda: 0,
        )
        if event is None:
            continue
        template = event_to_dict(event)
        occurrence = template["occurrence_id"]
        _require(isinstance(occurrence, str) and occurrence == row["occurrence_id"],
                 "ReconstructedOccurrenceMismatch")
        _require(occurrence not in templates and event.event_id not in event_ids,
                 "SourceWorksetIdentityConflict")
        templates[occurrence] = template
        event_ids.add(event.event_id)
        if event.call_id:
            calls.add(event.call_id)
        refs.update(str(ref) for ref in (event.parent_event_id, event.causation_id) if ref)
        workset.append(row)
    all_entries = [_manifest_entry(row) for row in rows]
    work_entries = [_manifest_entry(row) for row in workset]
    _require(_manifest(all_entries) == RAW_MANIFEST_SHA256, "FixedRawManifestMismatch")
    _require(len(workset) == WORKSET_COUNT, "FixedWorksetCountMismatch")
    _require(_manifest(work_entries) == WORKSET_MANIFEST_SHA256, "FixedWorksetManifestMismatch")
    # Different occurrences sharing a source id make preserved parent links ambiguous.
    duplicate_sources = connection.execute(
        "SELECT source_event_id FROM raw_life_events "
        "WHERE source_event_id IN (" + ",".join("?" for _ in event_ids) + ") "
        "GROUP BY source_event_id HAVING COUNT(DISTINCT occurrence_id)>1",
        tuple(sorted(event_ids)),
    ).fetchall()
    _require(not duplicate_sources, "AmbiguousSourceEventIdentity")
    return {
        "rows": rows, "workset": workset, "templates": templates,
        "manifest": all_entries, "workset_manifest": work_entries,
        "outside_candidate_references": sorted(refs - event_ids - calls - set(templates)),
    }


def _same_source(existing: dict[str, Any], template: dict[str, Any]) -> None:
    for key, value in template.items():
        if key not in _DELIVERY_FIELDS:
            _require(existing.get(key) == value, "ExistingOccurrenceSourceIdentityConflict")
    exact = existing.get("raw_content")
    if exact is None:
        exact = existing.get("content")
    _require(exact == template["raw_content"], "ExistingOccurrenceOriginalContentConflict")
    _require(isinstance(existing.get("content"), str), "ExistingPresentationNotText")


def _validate_journal(row: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    value = _decode_row(row)
    _require(row["revision"] == 1 and row["schema_version"] == 1, "RecoveryJournalVersionChanged")
    _require(
        value.get("schema") == JOURNAL_SCHEMA
        and value.get("operation_id") == OPERATION_ID
        and value.get("source_lower") == RAW_LOWER
        and value.get("source_upper") == RAW_UPPER
        and value.get("raw_manifest_sha256") == RAW_MANIFEST_SHA256
        and value.get("workset_manifest_sha256") == WORKSET_MANIFEST_SHA256
        and value.get("source_manifest") == sources["manifest"]
        and value.get("workset_manifest") == sources["workset_manifest"]
        and value.get("technical_only") is True
        and value.get("conservative_redelivery_not_exact_restoration") is True
        and value.get("outside_candidate_references") == sources["outside_candidate_references"]
        and value.get("reference_check_scope") == "candidate_manifest_only"
        and value.get("reference_status") == "preserved_as_recorded_not_repaired",
        "RecoveryJournalScopeConflict",
    )
    before_revision = value.get("global_before_revision")
    _require(_integer(before_revision, minimum=1)
             and value.get("global_after_revision") == before_revision + 1
             and all(isinstance(value.get(key), str)
                     and re.fullmatch(r"[0-9a-f]{64}", value[key]) is not None
                     for key in ("global_before_sha256", "global_after_sha256", "backup_sha256")),
             "RecoveryJournalCommitMetadataInvalid")
    added = value.get("added")
    present = value.get("already_present_occurrences")
    _require(isinstance(added, list) and isinstance(present, list), "RecoveryJournalEntriesInvalid")
    source_by_occ = {row["occurrence_id"]: row for row in sources["workset"]}
    covered: set[str] = set()
    sequences: set[int] = set()
    for item in added:
        _require(isinstance(item, dict), "RecoveryJournalAddedEntryInvalid")
        occurrence = item.get("occurrence_id")
        _require(occurrence in source_by_occ and occurrence not in covered, "RecoveryJournalIdentityConflict")
        sequence = item.get("new_sequence")
        _require(_integer(sequence, minimum=1) and sequence not in sequences, "RecoveryJournalSequenceConflict")
        source = source_by_occ[occurrence]
        template = copy.deepcopy(sources["templates"][occurrence])
        template.update(
            sequence=sequence, heartbeat_context_consumed=False,
            redelivery_operation_id=OPERATION_ID,
            redelivery_source_position=source["ingest_position"],
        )
        _require(
            item.get("source_position") == source["ingest_position"]
            and item.get("source_payload_hash") == source["payload_hash"]
            and item.get("runtime_event_sha256") == _sha(canonical_json(template)),
            "RecoveryJournalAddedDigestConflict",
        )
        covered.add(occurrence)
        sequences.add(sequence)
    for occurrence in present:
        _require(occurrence in source_by_occ and occurrence not in covered, "RecoveryJournalIdentityConflict")
        covered.add(occurrence)
    _require(covered == set(source_by_occ), "RecoveryJournalCoverageConflict")
    _require(value.get("added_count") == len(added), "RecoveryJournalCountConflict")
    return value


def prepare_plan(connection: sqlite3.Connection) -> dict[str, Any]:
    """Compute append-only pending changes without touching current event dicts."""
    columns = tuple(row["name"] for row in connection.execute("PRAGMA table_info(runtime_states)"))
    _require(columns == ROW_COLUMNS, "RuntimeTableSchemaChanged")
    raw_columns = tuple(row["name"] for row in connection.execute("PRAGMA table_info(raw_life_events)"))
    _require(raw_columns == RAW_COLUMNS, "RawTableSchemaChanged")
    preserved = []
    for namespace, key in PRESERVED_TARGETS:
        row = _read_row(connection, namespace, key)
        _require(row is not None, "RequiredRuntimeRowMissing")
        _decode_row(row)
        preserved.append(row)
    row = preserved[0]
    _require(row["schema_version"] == 2, "GlobalSchemaVersionChanged")
    payload = _decode_row(row)
    _require(payload.get("version") == 2 and isinstance(payload.get("state"), dict),
             "GlobalPayloadSchemaChanged")
    sources = _read_sources(connection)
    consumers = [dict(item) for item in connection.execute(
        "SELECT * FROM raw_event_consumer_offsets ORDER BY consumer_id",
    )]
    _require(any(item["consumer_id"] == "life_engine_subconscious_ingest:v1" for item in consumers),
             "SubconsciousConsumerMissing")
    journal_row = _read_row(connection, JOURNAL_NAMESPACE, JOURNAL_KEY)
    if journal_row is not None:
        journal = _validate_journal(journal_row, sources)
        return {
            "row": row, "sources": sources, "preserved_rows": preserved,
            "consumer_rows": consumers, "already_applied": True,
            "journal": journal, "journal_row": journal_row,
        }
    history, pending = payload.get("event_history"), payload.get("pending_events")
    _require(isinstance(history, list) and isinstance(pending, list), "RuntimeQueuesInvalid")
    by_occurrence: dict[str, dict[str, Any]] = {}
    by_event_id: dict[str, str] = {}
    high = payload["state"].get("event_sequence")
    cursor = payload["state"].get("heartbeat_context_cursor")
    _require(_integer(high) and _integer(cursor), "RuntimeSequenceOrCursorInvalid")
    high = max(high, cursor)
    for item in history + pending:
        _require(isinstance(item, dict), "RuntimeEventNotObject")
        occurrence = item.get("occurrence_id") or item.get("event_id")
        event_id = item.get("event_id")
        _require(isinstance(occurrence, str) and occurrence and isinstance(event_id, str) and event_id,
                 "RuntimeEventIdentityMissing")
        _require(occurrence not in by_occurrence and event_id not in by_event_id,
                 "RuntimeEventIdentityConflict")
        sequence = item.get("sequence")
        _require(_integer(sequence), "RuntimeEventSequenceInvalid")
        high = max(high, sequence)
        by_occurrence[occurrence] = item
        by_event_id[event_id] = occurrence
    updated = copy.deepcopy(payload)
    added: list[dict[str, Any]] = []
    present: list[str] = []
    for source in sources["workset"]:
        occurrence = source["occurrence_id"]
        template = sources["templates"][occurrence]
        other = by_event_id.get(template["event_id"])
        _require(other is None or other == occurrence, "RuntimeSourceEventIdConflict")
        if occurrence in by_occurrence:
            _same_source(by_occurrence[occurrence], template)
            present.append(occurrence)
            continue
        high += 1
        new = copy.deepcopy(template)
        new.update(
            sequence=high, heartbeat_context_consumed=False,
            redelivery_operation_id=OPERATION_ID,
            redelivery_source_position=source["ingest_position"],
        )
        # Validate the actual runtime provenance contract, without normalizing old dicts.
        rebuilt = reconstruct_workset_event(
            SQLLifeEventStore._decode_event(source),
            next_sequence=lambda: high,
        )
        _require(rebuilt is not None, "SourceReconstructionChanged")
        verified_new = event_to_dict(replace(
            rebuilt, heartbeat_context_consumed=False,
            redelivery_operation_id=OPERATION_ID,
            redelivery_source_position=source["ingest_position"],
        ))
        _require(new == verified_new, "RedeliverySerializationChanged")
        updated["pending_events"].append(new)
        added.append({
            "occurrence_id": occurrence, "source_position": source["ingest_position"],
            "source_payload_hash": source["payload_hash"], "new_sequence": high,
            "runtime_event_sha256": _sha(canonical_json(new)),
        })
    if added:
        updated["state"]["event_sequence"] = high
    updated["state"]["pending_event_count"] = len(updated["pending_events"])
    _require(updated["event_history"] == history
             and updated["pending_events"][:len(pending)] == pending,
             "ExistingRuntimeEventsChanged")
    reversed_payload = copy.deepcopy(updated)
    reversed_payload["pending_events"] = copy.deepcopy(pending)
    reversed_payload["state"]["event_sequence"] = payload["state"]["event_sequence"]
    if "pending_event_count" in payload["state"]:
        reversed_payload["state"]["pending_event_count"] = payload["state"]["pending_event_count"]
    else:
        reversed_payload["state"].pop("pending_event_count", None)
    _require(reversed_payload == payload, "NonAppendRuntimeMutation")
    updated_json = canonical_json(updated)
    return {
        "row": row, "sources": sources, "preserved_rows": preserved,
        "consumer_rows": consumers, "already_applied": False, "added": added,
        "already_present_occurrences": present, "updated_json": updated_json,
        "updated_sha": _sha(updated_json),
        "new_event_sequence": updated["state"]["event_sequence"],
    }


def _private_backup_root() -> tuple[Path, list[Path]]:
    current = REPO_ROOT
    directories = [current]
    _require(not current.is_symlink(), "RepositoryDirectoryIsSymlink")
    for component in ("data", "repair_backups", "heartbeat_history_6062"):
        current = current / component
        if not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
        info = current.lstat()
        _require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),
                 "BackupAncestorInvalid")
        _require(info.st_uid == os.geteuid(), "BackupAncestorOwnerMismatch")
        directories.append(current)
    _require(stat.S_IMODE(current.stat().st_mode) == 0o700, "BackupDirectoryNotPrivate")
    return current, directories


def _write_backup(plan: dict[str, Any]) -> tuple[Path, str]:
    root, directories = _private_backup_root()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    path = root / f"before-{plan['row']['revision']}-{timestamp}-{os.getpid()}.json"
    value = {
        "schema": "elysium.subconscious_history_redelivery.backup.v1",
        "operation_id": OPERATION_ID, "database": str(DATABASE_PATH.resolve()),
        "runtime_rows": plan["preserved_rows"], "raw_consumer_rows": plan["consumer_rows"],
        "raw_source_rows": plan["sources"]["rows"],
        "raw_manifest_sha256": RAW_MANIFEST_SHA256,
        "workset_manifest_sha256": WORKSET_MANIFEST_SHA256,
        "source_lower": RAW_LOWER, "source_upper": RAW_UPPER,
        "proposed_global_sha256": plan["updated_sha"],
    }
    text = canonical_json(value)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    for parent in reversed(directories):
        descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _require(path.read_text(encoding="utf-8") == text, "BackupReadbackMismatch")
    _require(stat.S_IMODE(path.stat().st_mode) == 0o600, "BackupFileNotPrivate")
    return path, _sha(text)


def _journal(plan: dict[str, Any], backup: Path, backup_sha: str) -> dict[str, Any]:
    return {
        "schema": JOURNAL_SCHEMA, "operation_id": OPERATION_ID,
        "technical_only": True, "conservative_redelivery_not_exact_restoration": True,
        "source_lower": RAW_LOWER, "source_upper": RAW_UPPER,
        "raw_manifest_sha256": RAW_MANIFEST_SHA256,
        "workset_manifest_sha256": WORKSET_MANIFEST_SHA256,
        "source_manifest": plan["sources"]["manifest"],
        "workset_manifest": plan["sources"]["workset_manifest"],
        "outside_candidate_references": plan["sources"]["outside_candidate_references"],
        "reference_check_scope": "candidate_manifest_only",
        "reference_status": "preserved_as_recorded_not_repaired",
        "global_before_revision": plan["row"]["revision"],
        "global_before_sha256": plan["row"]["payload_sha256"],
        "global_after_revision": plan["row"]["revision"] + 1,
        "global_after_sha256": plan["updated_sha"],
        "added": plan["added"], "added_count": len(plan["added"]),
        "already_present_occurrences": plan["already_present_occurrences"],
        "backup_path": str(backup), "backup_sha256": backup_sha,
    }


def _apply_cas(connection: sqlite3.Connection, plan: dict[str, Any],
               journal: dict[str, Any]) -> None:
    old = plan["row"]
    timestamp = datetime.now(UTC).isoformat()
    changed = connection.execute(
        "UPDATE runtime_states SET revision=?,payload_json=?,payload_sha256=?,updated_at=? "
        "WHERE namespace=? AND state_key=? AND revision=? AND schema_version=? "
        "AND payload_json=? AND payload_sha256=? AND updated_at=?",
        (old["revision"] + 1, plan["updated_json"], plan["updated_sha"], timestamp,
         *(old[key] for key in ROW_COLUMNS)),
    )
    _require(changed.rowcount == 1, "ExactGlobalCASFailed")
    journal_json = canonical_json(journal)
    connection.execute(
        "INSERT INTO runtime_states VALUES(?,?,?,?,?,?,?)",
        (JOURNAL_NAMESPACE, JOURNAL_KEY, 1, 1, journal_json, _sha(journal_json), timestamp),
    )


def _verify_applied(connection: sqlite3.Connection, plan: dict[str, Any],
                    journal: dict[str, Any]) -> None:
    row = _read_row(connection, GLOBAL_NAMESPACE, GLOBAL_KEY)
    _require(row is not None and row["revision"] == plan["row"]["revision"] + 1
             and row["schema_version"] == 2 and row["payload_json"] == plan["updated_json"]
             and row["payload_sha256"] == plan["updated_sha"], "GlobalReadbackMismatch")
    actual = _read_row(connection, JOURNAL_NAMESPACE, JOURNAL_KEY)
    _require(actual is not None and _validate_journal(actual, plan["sources"]) == journal,
             "RecoveryJournalReadbackMismatch")
    for old in plan["preserved_rows"][1:]:
        _require(_read_row(connection, old["namespace"], old["state_key"]) == old,
                 "PrivateRollingContextChanged")
    consumers = [dict(row) for row in connection.execute(
        "SELECT * FROM raw_event_consumer_offsets ORDER BY consumer_id",
    )]
    _require(consumers == plan["consumer_rows"], "RawConsumerChanged")
    sources = _read_sources(connection)
    _require(sources["rows"] == plan["sources"]["rows"], "RawSourcesChanged")


def _commit(connection: sqlite3.Connection) -> None:
    connection.commit()


def replay(*, apply: bool = False, expected_revision: int | None = None,
           expected_sha256: str | None = None) -> dict[str, Any]:
    """Inspect, or perform the explicitly bounded offline two-row transaction."""
    assert_storage_fixed()
    if apply:
        _require(_integer(expected_revision, minimum=1), "ExpectedRevisionRequired")
        _require(isinstance(expected_sha256, str)
                 and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None,
                 "ExpectedDigestRequired")
        assert_service_stopped(REPO_ROOT)
    backup: Path | None = None
    commit_attempted = False
    try:
        with (_offline_lock() if apply else nullcontext(lambda: None)) as verify_lock:
            if apply:
                assert_service_stopped(REPO_ROOT)
            connection = _open_database(writable=apply)
            try:
                connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
                plan = prepare_plan(connection)
                if plan["already_applied"]:
                    connection.rollback()
                    return {
                        "status": "already_applied", "operation_id": OPERATION_ID,
                        "added_count": plan["journal"]["added_count"],
                        "current_revision": plan["row"]["revision"],
                        "current_sha256": plan["row"]["payload_sha256"],
                        "source_manifest_sha256": WORKSET_MANIFEST_SHA256,
                        "raw_modified": False, "tools_executed": False,
                    }
                row = plan["row"]
                if apply:
                    _require(row["revision"] == expected_revision
                             and row["payload_sha256"] == expected_sha256,
                             "ExpectedGlobalSnapshotMismatch")
                    assert_service_stopped(REPO_ROOT)
                    assert_storage_fixed()
                    backup, backup_sha = _write_backup(plan)
                    verify_lock()
                    assert_service_stopped(REPO_ROOT)
                    journal = _journal(plan, backup, backup_sha)
                    changes = connection.total_changes
                    _apply_cas(connection, plan, journal)
                    _require(connection.total_changes - changes == 2, "UnexpectedRowsChanged")
                    _verify_applied(connection, plan, journal)
                    assert_service_stopped(REPO_ROOT)
                    assert_storage_fixed()
                    verify_lock()
                    commit_attempted = True
                    _commit(connection)
                    _verify_applied(connection, plan, journal)
                    assert_service_stopped(REPO_ROOT)
                    status = "applied"
                else:
                    connection.rollback()
                    status = "dry_run"
                return {
                    "status": status, "operation_id": OPERATION_ID,
                    "source_lower": RAW_LOWER, "source_upper": RAW_UPPER,
                    "source_count": WORKSET_COUNT,
                    "source_manifest_sha256": WORKSET_MANIFEST_SHA256,
                    "current_revision": row["revision"],
                    "current_sha256": row["payload_sha256"],
                    "new_revision": row["revision"] + 1,
                    "new_sha256": plan["updated_sha"],
                    "added_count": len(plan["added"]),
                    "already_present_count": len(plan["already_present_occurrences"]),
                    "new_event_sequence": plan["new_event_sequence"],
                    "outside_candidate_reference_count": len(plan["sources"]["outside_candidate_references"]),
                    "backup_path": str(backup) if backup else None,
                    "raw_modified": False, "tools_executed": False,
                    "exact_old_runtime_restoration": False,
                }
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
    except BaseException as exc:
        if commit_attempted:
            raise ReplayCommitUnknown("CommitAttemptedInspectFixedRecoveryJournal") from exc
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    try:
        result = replay(apply=args.apply, expected_revision=args.expected_revision,
                        expected_sha256=args.expected_sha256)
    except Exception as exc:
        print(json.dumps({
            "status": "commit_outcome_unknown" if isinstance(exc, ReplayCommitUnknown) else "refused",
            "error_type": type(exc).__name__,
            "reason": str(exc) if isinstance(exc, (ReplayRefused, ReplayCommitUnknown))
            else "ValidationOrPersistenceFailed",
        }, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
