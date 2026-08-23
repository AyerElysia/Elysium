#!/usr/bin/env python3
"""Copy the frozen unified proactive authority into a fenced MySQL candidate."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.storage.attention_schema import (
    ensure_attention_thread_schema,
)
from plugins.life_engine.storage.migration.copy_authority import (
    MySQLCopyAuthorityRegistry,
    open_mysql_copy_runtime,
)
from plugins.life_engine.storage.migration.manifest import load_snapshot_manifest
from plugins.life_engine.storage.proactive_migration import (
    PROACTIVE_SNAPSHOT_SOURCE,
    copy_proactive_authority_from_snapshot,
    verify_proactive_authority_copy,
)
from plugins.life_engine.storage.runtime_schema import ensure_runtime_state_schema
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
    parser.add_argument("--owner-id", default="proactive-authority-migrator")
    parser.add_argument("--lease-seconds", type=int, default=3_600)
    parser.add_argument(
        "--source-relative",
        default=PROACTIVE_SNAPSHOT_SOURCE.as_posix(),
        help="source SQLite path recorded in the frozen snapshot manifest",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_snapshot_manifest(args.snapshot / "manifest.json")
    writer_frozen = bool(manifest.get("writer_frozen"))
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
                "domain": "proactive_authority",
                "selection_contract": "unified-proactive-authority-v1",
                "record_families": ["attention", "initiative"],
                "legacy_thought_stream": "archive_only",
                "database_immutability": (
                    "trigger-enforced" if writer_frozen else "not-activatable"
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
        await ensure_runtime_state_schema(runtime)
        await ensure_attention_thread_schema(
            runtime,
            require_database_immutability=writer_frozen,
        )
        copied = await copy_proactive_authority_from_snapshot(
            args.snapshot,
            runtime,
            migration_id=str(args.run_id),
            source_relative=PurePosixPath(str(args.source_relative)),
        )
        await registry.set_progress(
            token,
            copied_records=int(copied.row_count) + 1,
        )
        independent = await verify_proactive_authority_copy(
            args.snapshot,
            runtime,
            source_relative=PurePosixPath(str(args.source_relative)),
        )
        verification = {
            "verified": bool(copied.verified)
            and bool(independent.get("verified")),
            "database_immutability": (
                "trigger-enforced" if writer_frozen else "not-activatable"
            ),
            "copy": copied.to_dict(),
            "independent_verification": independent,
            "canonical_authority": dict(
                independent.get("canonical_authority") or {}
            ),
            "legacy_thought_stream": {
                "mode": "archive_only",
                "migrated_as_live_authority": False,
            },
        }
        final_run = await registry.complete(token, verification=verification)
        return {"run": final_run, "verification": verification}
    except BaseException as exc:
        if token is not None:
            try:
                await registry.fail(token, reason=type(exc).__name__)
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


def main() -> int:
    try:
        result = asyncio.run(_run(_arguments()))
    except Exception as exc:  # noqa: BLE001 - never print credentials/payloads
        print(canonical_json({"status": "failed", "reason": type(exc).__name__}))
        return 2
    print(canonical_json(result))
    return 0 if bool(result["verification"]["verified"]) else 3


if __name__ == "__main__":
    raise SystemExit(main())
