#!/usr/bin/env python3
"""Copy one immutable Life Memory snapshot into a fenced MySQL candidate."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.storage.migration.copy_authority import (
    MySQLCopyAuthorityRegistry,
    open_mysql_copy_runtime,
)
from plugins.life_engine.storage.migration.manifest import load_snapshot_manifest
from plugins.life_engine.storage.migration.memory_copy import (
    copy_memory_from_snapshot,
    ensure_memory_copy_schema,
)
from plugins.life_engine.storage.migration.memory_export import (
    export_memory_to_sqlite,
)
from src.kernel.storage import (
    MySQLStorageConfig,
    canonical_json,
    create_mysql_storage_engine,
)


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--owner-id", default="life-memory-migrator")
    parser.add_argument("--reverse-export", type=Path)
    parser.add_argument("--lease-seconds", type=int, default=14_400)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="apply fenced Memory schema migrations without copying rows",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_snapshot_manifest(args.snapshot / "manifest.json")
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
        writer_frozen = bool(manifest.get("writer_frozen"))
        run = await registry.create_run(
            run_id=str(args.run_id),
            source_manifest_sha256=str(manifest["manifest_sha256"]),
            source_snapshot_sha256=str(manifest["source_snapshot_sha256"]),
            writer_frozen=writer_frozen,
            metadata={
                "domain": "life_memory",
                "selection_contract": "explicit-memory-tables-v1",
                "deleted_nodes": "preserved",
                "legacy_sqlite_fts": "rebuildable-visible-content",
                "database_immutability": (
                    "database-trigger-required"
                    if writer_frozen
                    else "application-enforced-shadow"
                ),
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
        if args.schema_only:
            database_immutability = await ensure_memory_copy_schema(
                runtime,
                copy_registry=registry,
                token=token,
            )
            verification = {
                "verified": False,
                "schema_only": True,
                "generation_eligible": False,
                "database_immutability": (
                    "database-trigger"
                    if database_immutability
                    else "application-enforced-shadow"
                ),
            }
            final_run = await registry.complete(token, verification=verification)
            return {"run": final_run, "verification": verification}
        copy_report = await copy_memory_from_snapshot(
            args.snapshot,
            runtime,
            copy_registry=registry,
            token=token,
            batch_size=int(args.batch_size),
        )
        reverse_report = None
        if args.reverse_export is not None:
            reverse_report = await export_memory_to_sqlite(
                runtime,
                template_snapshot_directory=args.snapshot,
                destination_directory=args.reverse_export,
                batch_size=int(args.batch_size),
            )
        verification = {
            "verified": bool(copy_report.verified)
            and (reverse_report is None or bool(reverse_report.verified)),
            "copy": copy_report.to_dict(),
            "reverse_export": (
                reverse_report.to_dict() if reverse_report is not None else {}
            ),
            "database_immutability": (
                "database-trigger" if writer_frozen else "application-enforced-shadow"
            ),
            "writer_frozen": writer_frozen,
            "generation_eligible": False,
        }
        final_run = await registry.complete(token, verification=verification)
        return {"run": final_run, "verification": verification}
    except BaseException as exc:
        if token is not None:
            try:
                await registry.fail(token, reason=f"{type(exc).__name__}: {exc}")
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
