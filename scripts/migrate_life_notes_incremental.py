#!/usr/bin/env python3
"""Incremental subject-document migration for local notes/ into MySQL.

One-shot backfill: the legacy notes/ files never entered the subject-document
ledger (8-04 baseline only covered SOUL/USER/MEMORY + diaries/). This script
appends each local notes/ file as an exact-byte external observation through
the fenced candidate-copy authority, preserving CAS, provenance and audit.

It is intentionally NOT the full migrate_life_subject_documents.py path:
that script requires len(target_heads) == len(snapshot documents), which no
longer holds on the live 2,016-document ledger.

Usage (from repo root):
  ELYSIUM_MYSQL_PASSWORD=<pwd> uv run python scripts/migrate_life_notes_incremental.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.storage.migration.copy_authority import (
    MySQLCopyAuthorityRegistry,
    open_mysql_copy_runtime,
)
from plugins.life_engine.storage.subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentConflict,
)
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from src.kernel.storage import (
    MySQLStorageConfig,
    canonical_json,
    create_mysql_storage_engine,
)

_NOTES_ROOT = Path("life_engine_workspace/notes")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data"),
        help="data root containing life_engine_workspace/notes",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="copy run id (default: notes-backfill-<YYYYmmdd-HHMMSS>)",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help=(
            "append a new revision when the target head already exists and its "
            "content differs from the local file (revision 2+), instead of "
            "recording a conflict. Use only after confirming the local content "
            "is the intended authoritative update."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list files and computed hashes without writing",
    )
    return parser.parse_args()


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _local_notes_files(data_root: Path) -> list[tuple[Path, str]]:
    notes_dir = data_root / _NOTES_ROOT
    if not notes_dir.is_dir():
        raise RuntimeError(f"notes directory does not exist: {notes_dir}")
    files: list[tuple[Path, str]] = []
    for path in sorted(notes_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(data_root).as_posix()
        files.append((path, f"life_engine_workspace/{relative.removeprefix('life_engine_workspace/')}"))
    if not files:
        raise RuntimeError(f"no notes files found under {notes_dir}")
    return files


async def _run(args: argparse.Namespace) -> dict[str, object]:
    config_toml = tomllib.loads(
        (_REPOSITORY_ROOT / "config/core.toml").read_text(encoding="utf-8")
    )
    db = config_toml["database"]
    config = MySQLStorageConfig(
        host=str(db["mysql_host"]),
        port=int(db["mysql_port"]),
        database=str(db["mysql_database"]),
        user=str(db["mysql_user"]),
        password=_required_environment("ELYSIUM_MYSQL_PASSWORD"),
        ssl_mode=str(db.get("mysql_ssl_mode", "disabled")),
        pool_size=4,
        max_overflow=4,
        connect_timeout_seconds=10,
        pool_timeout_seconds=20,
        application_query_timeout_seconds=30,
        innodb_lock_wait_timeout_seconds=10,
    )
    engine = create_mysql_storage_engine(config)
    registry = MySQLCopyAuthorityRegistry(engine)

    files = _local_notes_files(args.data_root)
    documents: list[dict[str, object]] = []
    for path, logical_path in files:
        content = path.read_bytes()
        documents.append(
            {
                "logical_path": logical_path,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    manifest_hash = hashlib.sha256(
        canonical_json(
            [{"path": d["logical_path"], "sha256": d["sha256"]} for d in documents]
        ).encode()
    ).hexdigest()
    snapshot_hash = hashlib.sha256(
        f"notes-incremental:{manifest_hash}".encode()
    ).hexdigest()

    if args.dry_run:
        return {
            "dry_run": True,
            "manifest_sha256": manifest_hash,
            "documents": documents,
        }

    run_id = args.run_id or (
        "notes-backfill-"
        + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    )
    runtime = None
    token = None
    try:
        run = await registry.create_run(
            run_id=run_id,
            source_manifest_sha256=manifest_hash,
            source_snapshot_sha256=snapshot_hash,
            writer_frozen=False,
            metadata={
                "domain": "subject_document",
                "selection_contract": "notes-incremental-v1",
                "byte_fidelity": "exact_bytes",
                "database_immutability": "application-enforced-shadow",
                "note": "local notes/ backfill (one-shot)",
            },
        )
        token = await registry.acquire(
            run_id,
            expected_epoch=int(run["authority_epoch"]),
            owner_id="notes-backfill-migrator",
            lease_seconds=1800,
        )
        runtime = open_mysql_copy_runtime(
            registry,
            token,
            backend_identity=config.safe_identity,
        )
        store = await open_subject_document_store(
            runtime,
            initialize_schema=False,
        )

        copied: list[dict[str, object]] = []
        unchanged: list[dict[str, object]] = []
        appended: list[dict[str, object]] = []
        conflicts: list[dict[str, object]] = []
        file_by_logical = {
            logical_path: path for path, logical_path in files
        }
        for document in documents:
            logical_path = str(document["logical_path"])
            source_path = file_by_logical[logical_path]
            content = source_path.read_bytes()
            occurrence_id = (
                "migration:notes:"
                + hashlib.sha256(
                    f"{logical_path}\0{document['sha256']}".encode()
                ).hexdigest()
            )

            head = await store.get_head(logical_path)
            if head is not None and head.current_version_id:
                current = await store.get_version(head.current_version_id)
                if current.content_hash == document["sha256"]:
                    unchanged.append(
                        {
                            "logical_path": logical_path,
                            "sha256": document["sha256"],
                            "detail": "head already matches local bytes",
                        }
                    )
                    continue
                if not args.append:
                    await registry.record_conflict(
                        token,
                        domain_name="subject_document",
                        source_identity=logical_path,
                        expected_hash=str(document["sha256"]),
                        actual_hash=current.content_hash,
                        detail=(
                            "head exists with different bytes; "
                            "rerun with --append to append a new revision"
                        ),
                    )
                    conflicts.append(
                        {
                            "logical_path": logical_path,
                            "expected_sha256": document["sha256"],
                            "actual_sha256": current.content_hash,
                            "detail": "head exists with different bytes",
                        }
                    )
                    continue
                expected_revision = head.revision
                expected_head = head.current_version_id
            else:
                expected_revision = 0
                expected_head = ""

            try:
                committed = await store.append_version(
                    AppendSubjectDocumentVersion(
                        logical_path=logical_path,
                        expected_revision=expected_revision,
                        expected_head_version_id=expected_head,
                        content_bytes=content,
                        occurrence_id=occurrence_id,
                        recorded_by="storage-migration",
                        recorded_source=f"notes-backfill:{manifest_hash[:16]}",
                        declared_owner="elysia",
                        semantic_actor_id=None,
                        semantic_source_id=None,
                        occurred_at=None,
                        provenance_status="semantic_source_missing",
                        byte_fidelity="exact_bytes",
                        encoding="utf-8",
                        newline_style="lf",
                        change_context={
                            "migration_observation": True,
                            "incremental_notes_backfill": True,
                            "source_relative": logical_path,
                            "append_revision": bool(head is not None),
                        },
                    )
                )
                record = {
                    "logical_path": logical_path,
                    "revision": committed.head.revision,
                    "version_id": committed.version.version_id,
                    "sha256": committed.version.content_hash,
                }
                if head is not None:
                    appended.append(record)
                else:
                    copied.append(record)
                await registry.set_progress(
                    token,
                    copied_records=len(copied) + len(appended),
                )
            except SubjectDocumentConflict as exc:
                latest = await store.get_head(logical_path)
                actual_hash = ""
                if latest is not None and latest.current_version_id:
                    latest_version = await store.get_version(
                        latest.current_version_id
                    )
                    actual_hash = latest_version.content_hash
                if actual_hash == document["sha256"]:
                    unchanged.append(
                        {
                            "logical_path": logical_path,
                            "sha256": actual_hash,
                            "detail": "head already matches local bytes",
                        }
                    )
                else:
                    await registry.record_conflict(
                        token,
                        domain_name="subject_document",
                        source_identity=logical_path,
                        expected_hash=str(document["sha256"]),
                        actual_hash=actual_hash,
                        detail=str(exc),
                    )
                    conflicts.append(
                        {
                            "logical_path": logical_path,
                            "expected_sha256": document["sha256"],
                            "actual_sha256": actual_hash,
                            "detail": str(exc),
                        }
                    )

        verification = {
            "verified": not conflicts,
            "copied": copied,
            "appended": appended,
            "unchanged": unchanged,
            "conflicts": conflicts,
            "database_immutability": "application-enforced-shadow",
        }
        final_run = await registry.complete(token, verification=verification)
        return {"run": final_run, "verification": verification}
    except BaseException as exc:
        if token is not None:
            try:
                await registry.fail(token, reason=f"{type(exc).__name__}: {exc}")
            except Exception as cleanup_error:  # noqa: BLE001
                exc.add_note(
                    "copy-run failure evidence could not be persisted: "
                    f"{type(cleanup_error).__name__}"
                )
        raise
    finally:
        if runtime is not None:
            await runtime.close()
        else:
            await engine.dispose()


def main() -> None:
    args = _arguments()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
