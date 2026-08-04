#!/usr/bin/env python3
"""Copy one immutable legacy Learning snapshot into a fenced MySQL candidate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.core.router_context_projection import (
    read_subject_authority_sources,
)
from plugins.life_engine.storage.learning_factory import open_learning_stores
from plugins.life_engine.storage.learning_migration import (
    export_learning_legacy_snapshot,
    import_legacy_learning_snapshot,
    verify_learning_legacy_export,
    verify_legacy_learning_import,
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

_LEARNING_SOURCE_PREFIX = PurePosixPath("life_engine_workspace/.life_learning")


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--owner-id", default="life-learning-migrator")
    parser.add_argument("--reverse-export", type=Path)
    parser.add_argument("--lease-seconds", type=int, default=14_400)
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="apply the fenced Learning schema without importing legacy files",
    )
    return parser.parse_args()


def _source_workspace(snapshot: Path) -> Path:
    workspace = (snapshot.resolve() / "workspace" / "life_engine_workspace").resolve()
    if not workspace.is_dir():
        raise RuntimeError("snapshot does not contain life_engine_workspace")
    return workspace


def _verify_manifest_learning_files(
    snapshot: Path,
    manifest: dict[str, Any],
) -> dict[str, str]:
    expected: dict[str, tuple[Path, str, int]] = {}
    rows = manifest.get("exact_files", manifest.get("workspace_files", []))
    if not isinstance(rows, list):
        raise RuntimeError("snapshot exact-file manifest is malformed")
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("snapshot exact-file entry is malformed")
        source_relative = PurePosixPath(str(row.get("source_relative", "")))
        if _LEARNING_SOURCE_PREFIX not in source_relative.parents:
            continue
        relative = source_relative.relative_to(_LEARNING_SOURCE_PREFIX).as_posix()
        backup_relative = str(row.get("backup_relative", ""))
        digest = str(row.get("sha256", ""))
        size = int(row.get("bytes", -1))
        if not relative or not backup_relative or len(digest) != 64 or size < 0:
            raise RuntimeError("snapshot Learning manifest entry is incomplete")
        expected[relative] = ((snapshot / backup_relative).resolve(), digest, size)

    actual_root = _source_workspace(snapshot) / ".life_learning"
    actual: dict[str, str] = {}
    if actual_root.exists():
        for path in sorted(actual_root.rglob("*")):
            if path.is_symlink():
                raise RuntimeError("snapshot Learning source contains a symlink")
            if not path.is_file():
                continue
            relative = path.relative_to(actual_root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            actual[relative] = digest
            entry = expected.get(relative)
            if entry is None:
                raise RuntimeError(f"Learning file is absent from manifest: {relative}")
            manifest_path, manifest_digest, manifest_size = entry
            if (
                path.resolve() != manifest_path
                or digest != manifest_digest
                or path.stat().st_size != manifest_size
            ):
                raise RuntimeError(f"Learning snapshot file differs from manifest: {relative}")
    if set(actual) != set(expected):
        raise RuntimeError("Learning snapshot manifest/file set differs")
    return actual


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = args.snapshot.resolve()
    manifest = load_snapshot_manifest(snapshot / "manifest.json")
    source_workspace = _source_workspace(snapshot)
    manifest_file_hashes = _verify_manifest_learning_files(snapshot, manifest)
    _, subject_revision = read_subject_authority_sources(source_workspace)
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
            writer_frozen=bool(manifest.get("writer_frozen")),
            metadata={
                "domain": "life_learning",
                "selection_contract": "append-only-events-rebuildable-projections-v1",
                "subject_revision": subject_revision,
                "exact_legacy_bytes": "bounded-immutable-chunks-v1",
                "database_immutability": "trigger-enforced",
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
        stores = await open_learning_stores(runtime, initialize_schema=True)
        if args.schema_only:
            verification = {
                "verified": False,
                "schema_only": True,
                "generation_eligible": False,
                "database_immutability": "trigger-enforced",
            }
            final_run = await registry.complete(token, verification=verification)
            return {"run": final_run, "verification": verification}

        imported = await import_legacy_learning_snapshot(
            source_workspace,
            stores.store,
            subject_revision=subject_revision,
        )
        if imported.file_hashes != manifest_file_hashes:
            raise RuntimeError("imported Learning source differs from snapshot manifest")
        import_verification = await verify_legacy_learning_import(
            source_workspace,
            stores.store,
        )
        reverse_report = None
        reverse_verification: dict[str, Any] = {}
        if args.reverse_export is not None:
            reverse_report = await export_learning_legacy_snapshot(
                stores.store,
                args.reverse_export,
            )
            reverse_verification = await verify_learning_legacy_export(
                stores.store,
                args.reverse_export,
            )
        health = await stores.store.health_snapshot()
        verified = bool(import_verification["verified"]) and (
            args.reverse_export is None or bool(reverse_verification["verified"])
        )
        verification = {
            "verified": verified,
            "import": asdict(imported),
            "import_verification": import_verification,
            "reverse_export": asdict(reverse_report) if reverse_report else {},
            "reverse_verification": reverse_verification,
            "health": health,
            "writer_frozen": bool(manifest.get("writer_frozen")),
            "generation_eligible": False,
            "database_immutability": "trigger-enforced",
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
