"""Certify an exact frozen local SQLite relocation without rewriting history.

The target must already contain the frozen database and binding cache at their
final paths. This appends a fresh copy certificate, registers a new immutable
generation, and appends the verified backend binding. It neither starts the
application nor edits configuration, subject documents, or old certificates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any
from uuid import uuid4

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker

from plugins.life_engine.proactive.backend_binding import (
    ensure_proactive_backend_binding,
    read_sqlite_proactive_backend_binding,
    verify_proactive_backend_binding,
)
from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.migration import LifeStorageLayout, create_local_snapshot
from plugins.life_engine.storage.migration.manifest import build_backend_generation, load_snapshot_manifest
from plugins.life_engine.storage.migration.snapshot import sha256_file
from plugins.life_engine.storage.models import BackendKind
from plugins.life_engine.storage.proactive_migration import (
    copy_proactive_authority_from_snapshot,
    verify_proactive_authority_copy,
)
from scripts.bootstrap_local_selectable import _open_local_copy_runtime
from src.app.runtime.single_instance import SingleInstanceLock
from src.kernel.storage import SQLiteStorageConfig, create_sqlite_storage_engine


async def certify_relocation(
    *, snapshot: Path, database: Path, workspace: Path, authority: Path,
    evidence: Path, generation_id: str, schema_version: int, registry_id: str,
    source_relative: PurePosixPath,
) -> dict[str, Any]:
    """Idempotent certificate/binding operation under isolated copy authority."""
    manifest = load_snapshot_manifest(snapshot / "manifest.json")
    before_binding = read_sqlite_proactive_backend_binding(database)
    candidate, copy_registry, copy_token = await _open_local_copy_runtime(
        database, evidence / f"copy-authority-{uuid4().hex}.json"
    )
    try:
        copied = await copy_proactive_authority_from_snapshot(
            snapshot, candidate, migration_id=generation_id, source_relative=source_relative
        )
        independent = await verify_proactive_authority_copy(
            snapshot, candidate, source_relative=source_relative
        )
        if not copied.verified or not independent["verified"]:
            raise RuntimeError("relocation copy proof failed")
    finally:
        await copy_registry.revoke(copy_token)
        await candidate.close()

    registry = FileAuthorityRegistry(authority, registry_id=registry_id)
    generation = await registry.get_generation(generation_id)
    if generation is None:
        generation = build_backend_generation(
            manifest, generation_id=generation_id, backend=BackendKind.LOCAL,
            backend_schema_version=schema_version, verification=independent,
            additional_root_hashes={"local:proactive_authority": copied.target_root_sha256},
        )
        await registry.register_generation(generation)
    elif (
        generation.backend != BackendKind.LOCAL
        or generation.schema_version != schema_version
        or generation.source_snapshot_sha256 != manifest["source_snapshot_sha256"]
        or generation.root_hashes.get("local:proactive_authority") != copied.target_root_sha256
        or generation.metadata.get("snapshot_manifest_sha256") != manifest["manifest_sha256"]
    ):
        raise RuntimeError("registered relocation generation conflicts with proof")
    health = await registry.health()
    # The CLI holds the target main-process lock and requires a stopped-source
    # assertion. No runtime startup invokes this explicit takeover operation.
    token = await registry.activate_generation(
        generation_id, expected_epoch=int(health["authority_epoch"]),
        owner_id="spark-cutover-control-plane", lease_seconds=120,
        confirm_previous_writers_stopped=True,
    )
    config = SQLiteStorageConfig(database_path=database, busy_timeout_seconds=10)
    engine = create_sqlite_storage_engine(config)
    active = StorageBackendRuntime(
        enabled=True, backend=BackendKind.LOCAL, backend_identity=config.safe_identity,
        generation=generation, authority_registry=registry, authority_token=token,
        engine=engine, session_factory=async_sessionmaker(engine, expire_on_commit=False),
    )
    try:
        bound = await ensure_proactive_backend_binding(
            workspace_path=workspace, binding_path="runtime/proactive/backend-binding.json", runtime=active
        )
        binding_health = await verify_proactive_backend_binding(
            workspace_path=workspace, binding_path="runtime/proactive/backend-binding.json", runtime=active
        )
        after = await verify_proactive_authority_copy(
            snapshot, active, source_relative=source_relative
        )
        if not after["verified"]:
            raise RuntimeError("proactive history changed during binding")
        return {
            "verified": True, "generation_id": generation_id,
            "generation_manifest_sha256": generation.manifest_sha256,
            "before_binding": before_binding, "binding": bound,
            "binding_health": binding_health, "copy": copied.to_dict(),
            "independent_verification": after,
        }
    finally:
        await active.revoke_authority()
        await active.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", type=Path, required=True)
    parser.add_argument("--target-repository", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--schema-version", type=int, default=3)
    parser.add_argument("--registry-id", default="life-domain")
    parser.add_argument("--writer-frozen", action="store_true", required=True)
    args = parser.parse_args()
    source_data = args.source_data.resolve(strict=True)
    target = args.target_repository.resolve(strict=True)
    evidence = args.evidence.resolve()
    database = target / "data/life_storage/local.sqlite3"
    relative = PurePosixPath("life_storage/local.sqlite3")
    source_database = source_data / relative
    if evidence.exists() or source_database.resolve() == database.resolve():
        parser.error("evidence must be new and source must be separate")
    if Path(str(database) + "-wal").exists():
        parser.error("target has WAL sidecar; verify frozen copy before relocating")
    with SingleInstanceLock(target / "data/runtime/elysium.lock"):
        source_hash, target_hash = sha256_file(source_database), sha256_file(database)
        if source_hash != target_hash:
            raise RuntimeError("target database is not the exact frozen source copy")
        evidence.mkdir(mode=0o700)
        snapshot = evidence / "snapshot"
        create_local_snapshot(
            source_data, snapshot,
            layout=LifeStorageLayout(
                sqlite_sources=(Path(relative),), exact_roots=(),
                excluded_rebuildable_roots=(), excluded_preserved_backup_roots=(),
            ),
            writer_frozen=True,
        )
        result = asyncio.run(certify_relocation(
            snapshot=snapshot, database=database,
            workspace=target / "data/life_engine_workspace",
            authority=target / "data/life_storage/authority.json", evidence=evidence,
            generation_id=args.generation_id, schema_version=args.schema_version,
            registry_id=args.registry_id, source_relative=relative,
        ))
        result["physical_copy_sha256"] = source_hash
        with (evidence / "relocation-report.json").open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
        print(json.dumps({"verified": result["verified"], "generation_id": args.generation_id,
                          "binding_health": result["binding_health"], "report": str(evidence / "relocation-report.json")}))


if __name__ == "__main__":
    main()
