#!/usr/bin/env python3
"""Repair only the evidenced #5998 model-output projection that blocks #6001.

Default is read-only dry-run. --apply requires this checkout's main.py to be
stopped, a synced private full-row backup, and an exact one-row compare-and-swap.
No raw activity, subject document, archive, cursor, writer claim, or other row
is edited. The quoted model text remains byte-for-byte recoverable.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.life_engine.core.context_stewardship import (
    HEARTBEAT_ARCHIVE_NAMESPACE,
    checkpoint_data,
    quote_model_checkpoint_text,
    verify_subject_checkpoint_archives,
)
from plugins.life_engine.service.heartbeat_rolling import deserialize_rolling_payloads
from src.kernel.storage import canonical_json, canonical_json_sha256

DATABASE_PATH = REPO_ROOT / "data/life_storage/local.sqlite3"
NAMESPACE = "life_heartbeat.rolling_context"
STATE_KEY = "subconscious"
OLD_REVISION = 1568
OLD_OUTER_SHA = "9ddcae447d82b20a704f6e7048470ed7420601de96c946557408a631f3912371"
OLD_INNER_SHA = "2ea5208d5cdf6a7ce8ef189dbbe64ab75e50a4ecadc55f1eac4044ce133b4041"
MODEL_TEXT_SHA = "4abd535c14397cb4900c43bbd78f249ddc06093c33d40e2ece1d22b91e81feb0"
RAW_POSITION = 262848
RAW_OCCURRENCE = (
    "conscious_model_turn_17ce0792ee6e60de0fbadd96710b98d63"
    "efbcfd0f3512c83bbafd796cac7c03a:generated"
)
RAW_RUN = "heartbeat-5998-19de2685a8cc"
TRUE_CHECKPOINT_IDS = {
    2: "selfctx_73d35c917d74e4e9bc8d6d2ea62740cfe563d9d8aed3893900121dc54f363773",
    5: "selfctx_3d3a20dcf08d801f84173d275da808ecd7a6699e77d36bdd50f0319c92c5903d",
    23: "selfctx_aa212e4e37f233332ba969a03db51301ebc44d4e40dd04363e92a1b96ea548f0",
}
ROW_COLUMNS = (
    "namespace", "state_key", "revision", "schema_version",
    "payload_json", "payload_sha256", "updated_at",
)


class RepairRefused(RuntimeError):
    """A content-free precondition failure; do not try a broader repair."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RepairRefused(code)


def assert_service_stopped(
    repository: Path, *, proc_root: Path = Path("/proc"),
) -> None:
    """Fail closed if the exact checkout main.py or its launcher is running."""
    _require(proc_root.is_dir(), "ProcessInspectionUnavailable")
    target = (repository / "main.py").resolve()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            candidates = [
                os.fsdecode(arg) for arg in args
                if arg and Path(os.fsdecode(arg)).name == "main.py"
            ]
            if not candidates:
                continue
            cwd = (entry / "cwd").resolve(strict=True)
            for arg in candidates:
                path = Path(arg)
                if (path if path.is_absolute() else cwd / path).resolve() == target:
                    raise RepairRefused(f"RepositoryMainStillRunning:pid={entry.name}")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise RepairRefused("ProcessInspectionPermissionDenied") from exc


def _open_database(*, writable: bool) -> sqlite3.Connection:
    path = DATABASE_PATH.resolve(strict=True)
    mode = "rw" if writable else "ro"
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode={mode}", uri=True, timeout=5,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    if writable:
        connection.execute("PRAGMA synchronous=FULL")
    else:
        connection.execute("PRAGMA query_only=ON")
    return connection


def _read_row(connection: sqlite3.Connection) -> dict[str, Any]:
    info = connection.execute("PRAGMA table_info(runtime_states)").fetchall()
    _require(tuple(row["name"] for row in info) == ROW_COLUMNS, "RuntimeSchemaChanged")
    row = connection.execute(
        "SELECT * FROM runtime_states WHERE namespace=? AND state_key=?",
        (NAMESPACE, STATE_KEY),
    ).fetchone()
    _require(row is not None, "TargetRuntimeRowMissing")
    return dict(row)


def _decode_runtime_row(row: dict[str, Any]) -> dict[str, Any]:
    text = row["payload_json"]
    _require(isinstance(text, str), "RuntimePayloadNotText")
    _require(_sha(text) == row["payload_sha256"], "RuntimeOuterDigestMismatch")
    payload = json.loads(text)
    _require(isinstance(payload, dict), "RuntimePayloadNotObject")
    return payload


class _ReadOnlyArchiveStore:
    """Minimal SELECT-only verifier shim; never opens a production adapter."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.checked: set[str] = set()

    async def get_state(self, namespace: str, key: str) -> Any:
        _require(namespace == HEARTBEAT_ARCHIVE_NAMESPACE, "ArchiveNamespaceChanged")
        row = self.connection.execute(
            "SELECT * FROM runtime_states WHERE namespace=? AND state_key=?",
            (namespace, key),
        ).fetchone()
        if row is None:
            return None
        self.checked.add(key)
        return SimpleNamespace(payload=_decode_runtime_row(dict(row)))


def _verify_all_archives(
    connection: sqlite3.Connection, payload: dict[str, Any],
) -> int:
    payloads = deserialize_rolling_payloads(payload)
    actual = {
        data["revision"]: data["checkpoint_id"]
        for item in payloads
        if (data := checkpoint_data(item)) is not None
    }
    _require(actual == TRUE_CHECKPOINT_IDS, "GenuineCheckpointSetChanged")
    store = _ReadOnlyArchiveStore(connection)
    service = SimpleNamespace(runtime_state_store=lambda: store)
    asyncio.run(verify_subject_checkpoint_archives(
        payloads,
        actor_consciousness_instance_id="chat_global",
        service=service,
        workspace_path=str(REPO_ROOT / "data/life_engine_workspace"),
        namespace=HEARTBEAT_ARCHIVE_NAMESPACE,
    ))
    return len(store.checked)


def _raw_model_text(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT * FROM raw_life_events WHERE ingest_position=? AND occurrence_id=?",
        (RAW_POSITION, RAW_OCCURRENCE),
    ).fetchone()
    _require(row is not None, "ExactRawOccurrenceMissing")
    _require(_sha(row["payload_json"]) == row["payload_hash"], "RawDigestMismatch")
    event = json.loads(row["payload_json"])
    _require(
        event.get("event_type") == "conscious_activity_model_turn"
        and event.get("metadata", {}).get("heartbeat_run_id") == RAW_RUN,
        "RawGenerationIdentityMismatch",
    )
    activity = json.loads(event["content"])
    _require(
        activity.get("phase") == "generated"
        and activity.get("tool_call_ids") == [],
        "RawGenerationHasToolCallsOrWrongPhase",
    )
    text = activity.get("assistant_message")
    _require(isinstance(text, str) and _sha(text) == MODEL_TEXT_SHA, "RawModelTextMismatch")
    return text


def _target_part(payload: dict[str, Any]) -> dict[str, Any]:
    _require(len(payload.get("payloads", [])) == 35, "PayloadCountChanged")
    item = payload["payloads"][34]
    _require(item.get("role") == "assistant", "TargetRoleChanged")
    _require(len(item.get("content", [])) == 2, "TargetPartCountChanged")
    part = item["content"][1]
    _require(part.get("type") == "text", "TargetPartNotText")
    return part


def prepare_plan(connection: sqlite3.Connection) -> dict[str, Any]:
    """Validate the exact old row or the exact idempotent repaired successor."""
    row = _read_row(connection)
    _require(row["schema_version"] == 1, "RuntimeSchemaVersionChanged")
    _require(row["revision"] in (OLD_REVISION, OLD_REVISION + 1), "RuntimeRevisionChanged")
    payload = _decode_runtime_row(row)
    deserialize_rolling_payloads(payload)  # Also checks the inner digest/encoding.
    raw_text = _raw_model_text(connection)
    quoted = quote_model_checkpoint_text(raw_text)
    _require(quoted != raw_text, "ForwardIsolationHelperDidNotQuote")
    part = _target_part(payload)
    already_repaired = row["revision"] == OLD_REVISION + 1
    expected_text = quoted if already_repaired else raw_text
    _require(part.get("text", "").encode("utf-8") == expected_text.encode("utf-8"),
             "DerivedTextDoesNotMatchExactRawGeneration")

    original_payload = copy.deepcopy(payload)
    _target_part(original_payload)["text"] = raw_text
    original_payload["payload_digest"] = canonical_json_sha256(original_payload["payloads"])
    _require(original_payload["payload_digest"] == OLD_INNER_SHA, "OldInnerDigestChanged")
    _require(_sha(canonical_json(original_payload)) == OLD_OUTER_SHA, "OldOuterDigestChanged")
    if not already_repaired:
        _require(row["payload_sha256"] == OLD_OUTER_SHA, "ExactOldRowDigestChanged")

    repaired_payload = copy.deepcopy(original_payload)
    _target_part(repaired_payload)["text"] = quoted
    repaired_payload["payload_digest"] = canonical_json_sha256(repaired_payload["payloads"])
    repaired_json = canonical_json(repaired_payload)
    if already_repaired:
        _require(row["payload_json"] == repaired_json, "RepairedSuccessorChanged")
    archives = _verify_all_archives(connection, repaired_payload)
    # Prove that replacing just this one Text (plus derived digest) is reversible.
    reversed_payload = copy.deepcopy(repaired_payload)
    _target_part(reversed_payload)["text"] = raw_text
    reversed_payload["payload_digest"] = OLD_INNER_SHA
    _require(reversed_payload == original_payload, "RepairIsNotExactlyReversible")
    return {
        "row": row, "repaired_payload": repaired_payload,
        "repaired_json": repaired_json, "repaired_sha": _sha(repaired_json),
        "already_repaired": already_repaired, "verified_archive_count": archives,
    }


def _private_backup_root() -> tuple[Path, list[Path]]:
    """Validate every ancestor before making a private, owned backup leaf."""
    current = REPO_ROOT
    directories = [current]
    _require(not current.is_symlink(), "RepositoryDirectoryIsSymlink")
    for component in ("data", "repair_backups", "heartbeat_checkpoint_6001"):
        current = current / component
        if not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
        info = current.lstat()
        _require(stat.S_ISDIR(info.st_mode), "BackupAncestorIsNotDirectory")
        _require(not stat.S_ISLNK(info.st_mode), "BackupAncestorIsSymlink")
        _require(info.st_uid == os.geteuid(), "BackupAncestorOwnerMismatch")
        directories.append(current)
    _require(stat.S_IMODE(current.stat().st_mode) == 0o700, "BackupDirectoryNotPrivate")
    return current, directories


def _write_backup(plan: dict[str, Any]) -> Path:
    root, directories = _private_backup_root()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    path = root / f"runtime-row-before-{OLD_REVISION}-{timestamp}.json"
    payload = {
        "schema": "elysium.derived_context_repair.backup.v1",
        "database": str(DATABASE_PATH.resolve()),
        "table": "runtime_states",
        "row": plan["row"],
        "raw_occurrence": RAW_OCCURRENCE,
        "raw_position": RAW_POSITION,
        "model_text_sha256": MODEL_TEXT_SHA,
        "new_payload_sha256": plan["repaired_sha"],
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(canonical_json(payload))
        handle.flush()
        os.fsync(handle.fileno())
    # Sync newly created directory entries as well as the backup file itself.
    for parent in reversed(directories):
        directory = os.open(
            parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return path


def _apply_cas(connection: sqlite3.Connection, plan: dict[str, Any]) -> None:
    old = plan["row"]
    updated_at = datetime.now(UTC).isoformat()
    result = connection.execute(
        "UPDATE runtime_states SET revision=?,payload_json=?,payload_sha256=?,updated_at=? "
        "WHERE namespace=? AND state_key=? AND revision=? AND schema_version=? "
        "AND payload_json=? AND payload_sha256=? AND updated_at=?",
        (
            OLD_REVISION + 1, plan["repaired_json"], plan["repaired_sha"], updated_at,
            *(old[key] for key in ROW_COLUMNS),
        ),
    )
    _require(result.rowcount == 1, "ExactRuntimeRowCASFailed")


def repair(*, apply: bool = False) -> dict[str, Any]:
    """Run fixed-target diagnosis or the explicitly requested one-row repair."""
    if apply:
        assert_service_stopped(REPO_ROOT)
    connection = _open_database(writable=apply)
    backup: Path | None = None
    try:
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        plan = prepare_plan(connection)
        if apply and not plan["already_repaired"]:
            assert_service_stopped(REPO_ROOT)
            backup = _write_backup(plan)
            assert_service_stopped(REPO_ROOT)
            changes_before = connection.total_changes
            _apply_cas(connection, plan)
            _require(connection.total_changes - changes_before == 1, "UnexpectedRowsChanged")
            verified = prepare_plan(connection)
            _require(verified["already_repaired"], "RepairedReadbackNotConfirmed")
            connection.commit()
            status = "repaired"
        else:
            connection.rollback()
            status = "already_repaired" if plan["already_repaired"] else "dry_run"
        return {
            "status": status, "namespace": NAMESPACE, "state_key": STATE_KEY,
            "old_revision": OLD_REVISION,
            "new_revision": OLD_REVISION + 1,
            "old_payload_sha256": OLD_OUTER_SHA,
            "new_payload_sha256": plan["repaired_sha"],
            "model_text_sha256": MODEL_TEXT_SHA,
            "verified_archive_count": plan["verified_archive_count"],
            "unchanged_checkpoint_revisions": sorted(TRUE_CHECKPOINT_IDS),
            "backup_path": str(backup) if backup else None,
            "raw_activity_modified": False,
        }
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply this exact offline repair")
    args = parser.parse_args()
    try:
        result = repair(apply=args.apply)
    except Exception as exc:
        # Deliberately no traceback/payload dump: diagnostics are content-free.
        print(json.dumps({"status": "refused", "error_type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, RepairRefused)
                          else "ValidationOrPersistenceFailed"}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
