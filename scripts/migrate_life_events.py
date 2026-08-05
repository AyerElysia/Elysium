#!/usr/bin/env python3
"""Copy one immutable Life Event snapshot into a fenced MySQL candidate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.storage.event_contracts import (
    LifeEventSnapshotImportPort,
    LifeEventSnapshotSourcePort,
)
from plugins.life_engine.storage.event_factory import open_life_event_store
from plugins.life_engine.storage.migration.copy_authority import (
    MySQLCopyAuthorityRegistry,
    open_mysql_copy_runtime,
)
from plugins.life_engine.storage.migration.event_copy import (
    copy_life_events_from_sqlite,
)
from plugins.life_engine.storage.migration.event_export import (
    export_life_events_to_sqlite,
)
from plugins.life_engine.storage.migration.manifest import load_snapshot_manifest
from src.kernel.storage import (
    MySQLStorageConfig,
    canonical_json,
    create_mysql_storage_engine,
)

_SOURCE_RELATIVE = PurePosixPath("life_engine_workspace/life_events.sqlite3")


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--owner-id", default="life-event-migrator")
    parser.add_argument("--reverse-export", type=Path)
    parser.add_argument("--lease-seconds", type=int, default=14_400)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="apply the fenced Life Event schema without copying rows",
    )
    return parser.parse_args()


def _snapshot_event_source(
    snapshot: Path,
    manifest: dict[str, Any],
) -> Path:
    """Resolve and hash-check the declared event ledger inside one snapshot."""

    rows = manifest.get("sqlite")
    if not isinstance(rows, list):
        raise TypeError("snapshot SQLite manifest is malformed")
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and PurePosixPath(str(row.get("source_relative", ""))) == _SOURCE_RELATIVE
    ]
    if len(matches) != 1:
        raise RuntimeError("snapshot must declare exactly one Life Event ledger")
    row = matches[0]
    backup_relative = PurePosixPath(str(row.get("backup_relative", "")))
    if backup_relative.is_absolute() or ".." in backup_relative.parts:
        raise RuntimeError("Life Event snapshot path escapes the snapshot root")
    source = (snapshot / backup_relative.as_posix()).resolve()
    try:
        source.relative_to(snapshot)
    except ValueError as exc:
        raise RuntimeError("Life Event snapshot path escapes the snapshot root") from exc
    expected_hash = str(row.get("backup_sha256") or row.get("sha256") or "")
    if not source.is_file() or len(expected_hash) != 64:
        raise RuntimeError("Life Event snapshot evidence is incomplete")
    actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise RuntimeError("Life Event snapshot database hash mismatch")
    return source


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = args.snapshot.resolve()
    manifest = load_snapshot_manifest(snapshot / "manifest.json")
    source_path = _snapshot_event_source(snapshot, manifest)
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
        pool_size=4,
        max_overflow=4,
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
                "domain": "life_event",
                "selection_contract": "exact-ledger-and-consumer-cursors-v2",
                "payload_fidelity": "exact-json-text",
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
        store = await open_life_event_store(
            runtime,
            initialize_schema=True,
            require_database_immutability=writer_frozen,
        )
        if not isinstance(store, LifeEventSnapshotImportPort) or not isinstance(
            store, LifeEventSnapshotSourcePort
        ):
            raise TypeError("selected Life Event adapter lacks migration contracts")
        if args.schema_only:
            verification = {
                "verified": False,
                "schema_only": True,
                "writer_frozen": writer_frozen,
                "generation_eligible": False,
                "database_immutability": database_immutability,
            }
            final_run = await registry.complete(token, verification=verification)
            return {"run": final_run, "verification": verification}

        copy_report = await copy_life_events_from_sqlite(
            source_path,
            store,
            copy_registry=registry,
            token=token,
            batch_size=int(args.batch_size),
        )
        reverse_report = None
        if args.reverse_export is not None:
            reverse_report = await export_life_events_to_sqlite(
                store,
                args.reverse_export,
                batch_size=int(args.batch_size),
            )
        verification = {
            "verified": bool(copy_report.verified)
            and (reverse_report is None or bool(reverse_report.verified)),
            "copy": copy_report.to_dict(),
            "reverse_export": (
                reverse_report.to_dict() if reverse_report is not None else {}
            ),
            "writer_frozen": writer_frozen,
            "generation_eligible": False,
            "database_immutability": database_immutability,
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


def main() -> None:
    print(canonical_json(asyncio.run(_run(_arguments()))))


if __name__ == "__main__":
    main()
