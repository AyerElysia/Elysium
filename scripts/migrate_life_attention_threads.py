#!/usr/bin/env python3
"""Archive legacy ThoughtStream bytes and copy snapshot-only evidence to MySQL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from sqlalchemy.exc import OperationalError

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.storage.attention_factory import (
    open_attention_thread_stores,
)
from plugins.life_engine.storage.attention_migration import (
    AttentionLegacyArchiveReport,
    create_legacy_attention_archive,
    export_legacy_attention_snapshot,
    import_legacy_attention_snapshot,
    load_legacy_attention_archive,
    verify_legacy_attention_import,
)
from plugins.life_engine.storage.migration.copy_authority import (
    MySQLCopyAuthorityRegistry,
    open_mysql_copy_runtime,
)
from plugins.life_engine.storage.migration.manifest import load_snapshot_manifest
from src.kernel.storage import (
    MySQLStorageConfig,
    canonical_json,
    create_mysql_storage_engine,
)

_ATTENTION_SOURCE = PurePosixPath("life_engine_workspace/thoughts/streams.json")
_TRANSIENT_MYSQL_DISCONNECT_CODES = frozenset({2006, 2013})
_T = TypeVar("_T")


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--owner-id", default="life-attention-migrator")
    parser.add_argument("--reverse-export", type=Path)
    parser.add_argument("--lease-seconds", type=int, default=14_400)
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="apply fenced schema without importing the legacy snapshot",
    )
    return parser.parse_args()


def _is_transient_mysql_disconnect(exc: OperationalError) -> bool:
    original = getattr(exc, "orig", None)
    arguments = getattr(original, "args", ())
    return bool(arguments) and arguments[0] in _TRANSIENT_MYSQL_DISCONNECT_CODES


async def _retry_transient_mysql(
    operation: Callable[[], Awaitable[_T]],
    *,
    attempts: int = 3,
) -> _T:
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except OperationalError as exc:
            if attempt == attempts or not _is_transient_mysql_disconnect(exc):
                raise
            await asyncio.sleep(0.25 * attempt)
    raise AssertionError("unreachable")


def _manifest_attention_source(
    snapshot: Path,
    manifest: dict[str, Any],
) -> Path:
    rows = manifest.get("exact_files", manifest.get("workspace_files", []))
    if not isinstance(rows, list):
        raise TypeError("snapshot exact-file manifest is malformed")
    matches: list[tuple[Path, str, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("snapshot exact-file entry is malformed")
        if PurePosixPath(str(row.get("source_relative", ""))) != _ATTENTION_SOURCE:
            continue
        backup_relative = str(row.get("backup_relative", ""))
        digest = str(row.get("sha256", ""))
        size = int(row.get("bytes", -1))
        if not backup_relative or len(digest) != 64 or size < 0:
            raise RuntimeError("snapshot Attention manifest entry is incomplete")
        matches.append(((snapshot / backup_relative).resolve(), digest, size))
    if len(matches) != 1:
        raise RuntimeError("snapshot must declare exactly one legacy streams.json")
    source, expected_digest, expected_size = matches[0]
    try:
        source.relative_to(snapshot)
    except ValueError as exc:
        raise RuntimeError("snapshot Attention file escapes snapshot root") from exc
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("snapshot Attention source is missing or symbolic")
    raw = source.read_bytes()
    if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_digest:
        raise RuntimeError("snapshot Attention source differs from manifest")
    return source


def _archive_source(source: Path, archive: Path) -> AttentionLegacyArchiveReport:
    if not archive.exists():
        return create_legacy_attention_archive(source, archive)
    snapshot, manifest = load_legacy_attention_archive(archive)
    source_raw = source.read_bytes()
    if snapshot.raw_bytes != source_raw:
        raise RuntimeError("existing Attention archive differs from snapshot")
    return AttentionLegacyArchiveReport(
        archive_directory=str(archive.resolve()),
        snapshot_sha256=snapshot.sha256,
        byte_length=snapshot.byte_length,
        row_count=len(snapshot.rows),
        rows_root_sha256=str(manifest["rows_root_sha256"]),
        manifest_sha256=str(manifest["manifest_sha256"]),
        verified=True,
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = args.snapshot.resolve()
    manifest = load_snapshot_manifest(snapshot / "manifest.json")
    source = _manifest_attention_source(snapshot, manifest)
    archive_report = _archive_source(source, args.archive.resolve())
    writer_frozen = bool(manifest.get("writer_frozen"))
    database_immutability = (
        "trigger-enforced" if writer_frozen else "application-enforced-shadow"
    )
    config = MySQLStorageConfig(
        host=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_HOST"),
        port=int(_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_PORT")),
        database=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_DATABASE"),
        user=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_USER"),
        password=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_PASSWORD"),
        ssl_mode="disabled",
        pool_size=2,
        max_overflow=2,
        connect_timeout_seconds=10,
        pool_timeout_seconds=30,
        application_query_timeout_seconds=120,
        innodb_lock_wait_timeout_seconds=15,
    )
    engine = create_mysql_storage_engine(config)
    registry = MySQLCopyAuthorityRegistry(engine)
    runtime = None
    token = None
    try:
        run = await registry.create_run(
            run_id=str(args.run_id),
            source_manifest_sha256=str(manifest["manifest_sha256"]),
            source_snapshot_sha256=str(manifest["source_snapshot_sha256"]),
            writer_frozen=writer_frozen,
            metadata={
                "domain": "attention_thread",
                "selection_contract": "subject-authority-append-only-v1",
                "legacy_import_mode": "snapshot_only",
                "legacy_history_claim": "no_fabricated_events",
                "legacy_generation_eligible": False,
                "archive_manifest_sha256": archive_report.manifest_sha256,
                "database_immutability": database_immutability,
            },
        )
        token = await registry.acquire(
            str(args.run_id),
            expected_epoch=int(run["authority_epoch"]),
            owner_id=str(args.owner_id),
            lease_seconds=int(args.lease_seconds),
        )
        runtime = open_mysql_copy_runtime(
            registry,
            token,
            backend_identity=config.safe_identity,
        )
        await open_attention_thread_stores(
            runtime,
            initialize_schema=True,
            require_database_immutability=writer_frozen,
        )
        if args.schema_only:
            verification = {
                "verified": False,
                "schema_only": True,
                "generation_eligible": False,
                "database_immutability": database_immutability,
                "archive": archive_report.to_dict(),
            }
            final_run = await registry.complete(token, verification=verification)
            return {"run": final_run, "verification": verification}

        copied = await _retry_transient_mysql(
            lambda: import_legacy_attention_snapshot(source, runtime)
        )
        await registry.set_progress(token, copied_records=int(copied.row_count) + 1)
        import_verification = await _retry_transient_mysql(
            lambda: verify_legacy_attention_import(source, runtime)
        )
        reverse_report = None
        if args.reverse_export is not None:
            reverse_report = await export_legacy_attention_snapshot(
                runtime,
                snapshot_sha256=copied.snapshot_sha256,
                archive_directory=args.reverse_export,
            )
        verified = bool(import_verification["verified"]) and (
            reverse_report is None or reverse_report.verified
        )
        canonical_is_empty = (
            copied.canonical_event_count_after == 0
            and copied.canonical_head_count == 0
            and copied.canonical_focus_count == 0
        )
        verification = {
            "verified": verified,
            "database_immutability": database_immutability,
            "archive": archive_report.to_dict(),
            "copy": copied.to_dict(),
            "import_verification": import_verification,
            "reverse_export": asdict(reverse_report) if reverse_report else {},
            "legacy_snapshot": {
                "import_mode": "snapshot_only",
                "history_claim": "no_fabricated_events",
                "generation_eligible": False,
            },
            "canonical_authority": {
                "generation_eligible": writer_frozen
                and verified
                and canonical_is_empty,
                "root_sha256": copied.canonical_root_sha256,
                "event_frontier": copied.canonical_event_count_after,
                "head_count": copied.canonical_head_count,
                "focus_count": copied.canonical_focus_count,
            },
        }
        final_run = await registry.complete(token, verification=verification)
        return {"run": final_run, "verification": verification}
    except BaseException as exc:
        if token is not None:
            try:
                await registry.fail(token, reason=type(exc).__name__)
            except Exception as cleanup_error:  # noqa: BLE001 - retain root cause
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


def main() -> int:
    try:
        result = asyncio.run(_run(_arguments()))
    except Exception as exc:  # noqa: BLE001 - CLI emits only the bounded type
        print(canonical_json({"status": "failed", "reason": type(exc).__name__}))
        return 2
    print(canonical_json(result))
    return 0 if bool(result["verification"]["verified"]) else 3


if __name__ == "__main__":
    raise SystemExit(main())
