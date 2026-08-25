#!/usr/bin/env python3
"""Explicit same-backend generation repair for unified proactive binding.

Runtime startup never rewrites generation identity.  After a local selectable
generation bump that keeps the same SQLite endpoint, authority registry and
proactive history root, operators must run this control-plane command.  It
appends a content-free repair certificate and rebinds the cache; it does not
edit subject files or proactive history rows.

Usage (from the repository root, while life_engine is not holding the writer):

    uv run python scripts/repair_proactive_generation_binding.py
    uv run python scripts/repair_proactive_generation_binding.py --apply
    uv run python scripts/repair_proactive_generation_binding.py \\
        --complete-initial-binding --apply \\
        --certificate-backend-identity-sha256 <copy-certificate-identity>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from plugins.life_engine.proactive.backend_binding import (
    ProactiveBackendBindingConflict,
    _generation_repair_required,
    _identity,
    _load_chain,
    _load_head,
    _read_marker,
    complete_proactive_initial_binding,
    ensure_proactive_backend_binding,
    read_sqlite_proactive_backend_binding,
    repair_proactive_generation_binding,
    verify_proactive_backend_binding,
)
from plugins.life_engine.proactive.history import (
    PROACTIVE_MIGRATION_NAMESPACE,
    read_proactive_history_in_session,
)
from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.models import BackendKind
from src.kernel.storage import SQLiteStorageConfig, create_sqlite_storage_engine

_DEFAULT_WORKSPACE = _REPOSITORY_ROOT / "data" / "life_engine_workspace"
_DEFAULT_BINDING = "runtime/proactive/backend-binding.json"
_DEFAULT_AUTHORITY = _REPOSITORY_ROOT / "data" / "life_storage" / "authority.json"
_DEFAULT_SQLITE = _REPOSITORY_ROOT / "data" / "life_storage" / "local.sqlite3"
_DEFAULT_REGISTRY_ID = "life-domain"
_DEFAULT_TARGET_GENERATION = "local-selectable-20260824-v3"
_DEFAULT_OWNER = "elysium-linux-primary:proactive-generation-repair"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=_DEFAULT_WORKSPACE)
    parser.add_argument("--binding-path", default=_DEFAULT_BINDING)
    parser.add_argument("--authority", type=Path, default=_DEFAULT_AUTHORITY)
    parser.add_argument("--sqlite", type=Path, default=_DEFAULT_SQLITE)
    parser.add_argument("--registry-id", default=_DEFAULT_REGISTRY_ID)
    parser.add_argument(
        "--target-generation",
        default=_DEFAULT_TARGET_GENERATION,
    )
    parser.add_argument("--owner-id", default=_DEFAULT_OWNER)
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument(
        "--source-sqlite",
        type=Path,
        default=None,
        help="leftover source proactive sqlite that still holds the certified binding",
    )
    parser.add_argument(
        "--repair-id",
        default="repair-local-selectable-20260823-v2-to-20260824-v3",
    )
    parser.add_argument(
        "--initial-binding-repair-id",
        default="initial-bind-local-selectable-20260824-v3",
    )
    parser.add_argument(
        "--complete-initial-binding",
        action="store_true",
        help="complete the first production bind after a verified copy with empty chain",
    )
    parser.add_argument(
        "--certificate-backend-identity-sha256",
        default="",
        help="required when the copy certificate targets a different sqlite path identity",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the selected repair; default is diagnose only",
    )
    return parser.parse_args()


async def _open_runtime(
    *,
    sqlite_path: Path,
    authority_path: Path,
    registry_id: str,
    target_generation: str,
    owner_id: str,
    lease_seconds: int,
) -> StorageBackendRuntime:
    sqlite_config = SQLiteStorageConfig(
        database_path=sqlite_path.resolve(),
        busy_timeout_seconds=10,
    )
    registry = FileAuthorityRegistry(
        authority_path.resolve(),
        registry_id=registry_id,
    )
    generation = await registry.get_generation(target_generation)
    if generation is None:
        raise RuntimeError(f"generation is not registered: {target_generation}")
    health = await registry.health()
    token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=int(health.get("authority_epoch") or 0),
        owner_id=owner_id,
        lease_seconds=lease_seconds,
        confirm_previous_writers_stopped=False,
    )
    engine = create_sqlite_storage_engine(sqlite_config)
    return StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.LOCAL,
        backend_identity=sqlite_config.safe_identity,
        generation=generation,
        authority_registry=registry,
        authority_token=token,
        engine=engine,
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
    )


async def _diagnose(
    runtime: StorageBackendRuntime,
    marker: dict[str, Any] | None,
) -> dict[str, Any]:
    identity = _identity(runtime)
    bound = marker.get("identity") if isinstance(marker, dict) else None
    expected_root = ""
    if runtime.generation is not None:
        expected_root = str(
            runtime.generation.root_hashes.get(
                f"{runtime.backend.value}:proactive_authority",
                "",
            )
        )
    async with runtime.unit_of_work() as uow:
        history = await read_proactive_history_in_session(uow.session)
        chain = await _load_chain(uow.session)
        head = await _load_head(uow.session)
        cert_rows = (
            (
                await uow.session.execute(
                    text(
                        """SELECT occurrence_id, payload_json, payload_sha256
                        FROM runtime_events
                        WHERE namespace = :namespace ORDER BY position"""
                    ),
                    {"namespace": PROACTIVE_MIGRATION_NAMESPACE},
                )
            )
            .mappings()
            .all()
        )
    chain_tip = chain[-1] if chain else None
    chain_identity = chain_tip.get("identity") if isinstance(chain_tip, dict) else None
    head_identity = head.get("identity") if isinstance(head, dict) else None
    certificates: list[dict[str, Any]] = []
    for row in cert_rows:
        raw = (
            row["payload_json"].decode("utf-8")
            if isinstance(row["payload_json"], bytes)
            else str(row["payload_json"])
        )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            continue
        certificates.append(
            {
                "occurrence_id": str(row["occurrence_id"]),
                "migration_id": payload.get("migration_id"),
                "source_binding_sha256": payload.get("source_binding_sha256"),
                "target_backend_identity_sha256": payload.get(
                    "target_backend_identity_sha256"
                ),
                "target_root_sha256": payload.get("target_root_sha256"),
            }
        )
    cert = certificates[0] if len(certificates) == 1 else None
    live_identity = str(identity.get("backend_identity_sha256") or "")
    cache_identity = (
        str(bound.get("backend_identity_sha256") or "")
        if isinstance(bound, dict)
        else ""
    )
    report: dict[str, Any] = {
        "configured_generation_id": identity.get("generation_id"),
        "configured_generation_manifest_sha256": identity.get(
            "generation_manifest_sha256"
        ),
        "bound_generation_id": (
            bound.get("generation_id") if isinstance(bound, dict) else None
        ),
        "bound_generation_manifest_sha256": (
            bound.get("generation_manifest_sha256")
            if isinstance(bound, dict)
            else None
        ),
        "cache_backend_identity_sha256": cache_identity or None,
        "live_backend_identity_sha256": live_identity or None,
        "chain_length": len(chain),
        "head_present": head is not None,
        "marker_equals_head": marker == head,
        "head_equals_chain_tip": head == chain_tip,
        "chain_tip_generation_id": (
            chain_identity.get("generation_id")
            if isinstance(chain_identity, dict)
            else None
        ),
        "head_generation_id": (
            head_identity.get("generation_id")
            if isinstance(head_identity, dict)
            else None
        ),
        "same_backend_generation_repair_required": (
            len(chain) > 0
            and isinstance(chain_identity, dict)
            and _generation_repair_required(chain_identity, identity)
        ),
        "empty_chain_initial_bind_required": (
            len(chain) == 0
            and expected_root == history.root_sha256
            and cert is not None
        ),
        "certificate_count": len(certificates),
        "certificate_migration_id": None if cert is None else cert["migration_id"],
        "certificate_source_binding_sha256": (
            None if cert is None else cert["source_binding_sha256"]
        ),
        "certificate_backend_identity_sha256": (
            None if cert is None else cert["target_backend_identity_sha256"]
        ),
        "certificate_backend_identity_matches_live": (
            False
            if cert is None
            else str(cert["target_backend_identity_sha256"] or "") == live_identity
        ),
        "generation_proactive_root_sha256": expected_root,
        "live_history_root_sha256": history.root_sha256,
        "history_row_count": history.row_count,
        "roots_match": expected_root == history.root_sha256,
    }
    return report


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    workspace = args.workspace.resolve()
    binding_path = str(args.binding_path)
    marker_path = workspace / binding_path
    marker = _read_marker(marker_path)
    runtime = await _open_runtime(
        sqlite_path=args.sqlite,
        authority_path=args.authority,
        registry_id=args.registry_id,
        target_generation=args.target_generation,
        owner_id=args.owner_id,
        lease_seconds=args.lease_seconds,
    )
    try:
        diagnosis = await _diagnose(runtime, marker)
        if not args.apply:
            return {"applied": False, **diagnosis}
        if not diagnosis["roots_match"]:
            raise RuntimeError(
                "refusing to apply: live proactive history root does not match "
                "the target generation"
            )
        if args.complete_initial_binding:
            if int(diagnosis["chain_length"] or 0) != 0:
                raise RuntimeError(
                    "refusing to apply: binding chain already exists"
                )
            if not diagnosis["empty_chain_initial_bind_required"]:
                raise RuntimeError(
                    "refusing to apply: empty-chain initial bind preconditions "
                    "are not met"
                )
            source_sqlite = args.source_sqlite
            if source_sqlite is None:
                source_sqlite = workspace / "runtime/proactive/proactive.sqlite3"
            source_binding = read_sqlite_proactive_backend_binding(source_sqlite)
            repaired = await complete_proactive_initial_binding(
                workspace_path=workspace,
                binding_path=binding_path,
                runtime=runtime,
                source_binding=source_binding,
                repair_id=args.initial_binding_repair_id,
                certificate_backend_identity_sha256=(
                    args.certificate_backend_identity_sha256
                ),
            )
            mode = "initial_binding"
            repair_id = args.initial_binding_repair_id
        else:
            if int(diagnosis["chain_length"] or 0) == 0:
                raise RuntimeError(
                    "refusing to apply: binding chain is empty; "
                    "use --complete-initial-binding"
                )
            if not diagnosis["same_backend_generation_repair_required"]:
                raise RuntimeError(
                    "refusing to apply: this is not a same-backend generation "
                    "repair"
                )
            bound = marker.get("identity") if isinstance(marker, dict) else None
            if not isinstance(bound, dict):
                raise TypeError("workspace binding identity is missing")
            repaired = await repair_proactive_generation_binding(
                workspace_path=workspace,
                binding_path=binding_path,
                runtime=runtime,
                previous_generation_id=str(bound["generation_id"]),
                previous_generation_manifest_sha256=str(
                    bound["generation_manifest_sha256"]
                ),
                repair_id=args.repair_id,
            )
            mode = "generation_repair"
            repair_id = args.repair_id
        verified = await verify_proactive_backend_binding(
            workspace_path=workspace,
            binding_path=binding_path,
            runtime=runtime,
        )
        ensure_result = await ensure_proactive_backend_binding(
            workspace_path=workspace,
            binding_path=binding_path,
            runtime=runtime,
        )
        return {
            "applied": True,
            "mode": mode,
            **diagnosis,
            "repair_id": repair_id,
            "repaired_binding_sha256": repaired.get("binding_sha256"),
            "repaired_generation_id": (
                repaired.get("identity", {}).get("generation_id")
                if isinstance(repaired.get("identity"), dict)
                else None
            ),
            "verify_status": verified.get("status"),
            "ensure_generation_id": (
                ensure_result.get("identity", {}).get("generation_id")
                if isinstance(ensure_result.get("identity"), dict)
                else None
            ),
        }
    finally:
        try:
            await runtime.revoke_authority()
        finally:
            await runtime.close()


def main() -> int:
    args = _arguments()
    try:
        result = asyncio.run(_run(args))
    except (
        ProactiveBackendBindingConflict,
        RuntimeError,
        ValueError,
        OSError,
    ) as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
