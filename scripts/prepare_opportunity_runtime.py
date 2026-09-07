#!/usr/bin/env python3
"""Explicitly verify or prepare the canonical Opportunity storage schema.

The default invocation is a connection-free dry run.  Schema installation and
the irreversible managed marker both require explicit command-line choices;
this command never installs a provider, workflow, capability, or Skill.

``--verify`` and ``--apply`` open the normal writer-capable selected runtime.
On MySQL this can join, refresh, or (when none is active) activate generation
authority membership. They require a maintenance-safe window and an explicit
acknowledgement; only the default dry run is connection-free.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageWriterRole
from plugins.life_engine.storage.factory import (
    StorageFactorySettings,
    open_storage_backend,
    settings_from_life_engine_config,
)
from plugins.life_engine.storage.models import BackendKind, GenerationStatus
from plugins.life_engine.storage.opportunity_contracts import (
    OpportunityRuntimeMarker,
)
from plugins.life_engine.storage.opportunity_schema import (
    OPPORTUNITY_SCHEMA_VERSION,
    ensure_opportunity_schema,
    mark_opportunity_runtime_managed,
    read_opportunity_runtime_marker,
    verify_opportunity_schema,
)
from plugins.life_engine.storage.runtime_schema import ensure_runtime_state_schema
from src.app.runtime.single_instance import SingleInstanceLock
from src.core.config.core_config import CoreConfig
from src.kernel.storage import canonical_json

_MAINTENANCE_LOCK_PATH = _REPOSITORY_ROOT / "data/runtime/elysium.lock"
_MAINTENANCE_FENCING_ENV = "ELYSIUM_OPPORTUNITY_MAINTENANCE_FENCING_TOKEN"


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Only the default mode is connection-free. --verify/--apply open "
            "the selected writer runtime and may update generation authority "
            "membership; coordinate a maintenance-safe window."
        ),
    )
    parser.add_argument(
        "--core-config",
        type=Path,
        default=Path("config/elysium.toml"),
    )
    parser.add_argument(
        "--life-config",
        type=Path,
        default=Path("config/plugins/life_engine/config.toml"),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--verify",
        action="store_true",
        help="open writer runtime and verify schema/marker without domain writes",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="install/upgrade only the Opportunity domain schema",
    )
    parser.add_argument(
        "--confirm-generation",
        default="",
        help="exact selected generation id required by --verify/--apply",
    )
    parser.add_argument(
        "--confirm-writer-runtime",
        action="store_true",
        help=(
            "acknowledge that --verify/--apply may join, refresh, or activate "
            "selected generation authority membership"
        ),
    )
    parser.add_argument(
        "--activate-local-authority",
        action="store_true",
        help=(
            "while holding the service's single-instance lock, activate the "
            "verified local generation for maintenance; refuse active authority"
        ),
    )
    parser.add_argument(
        "--mark-managed",
        action="store_true",
        help="after schema verification, write the irreversible managed marker",
    )
    parser.add_argument(
        "--migration-occurrence-id",
        default="",
        help="stable infrastructure occurrence id required by --mark-managed",
    )
    args = parser.parse_args(argv)
    if args.activate_local_authority and not (args.verify or args.apply):
        parser.error("--activate-local-authority requires --verify or --apply")
    if args.mark_managed and not args.apply:
        parser.error("--mark-managed requires --apply")
    if args.migration_occurrence_id and not args.mark_managed:
        parser.error("--migration-occurrence-id requires --mark-managed")
    if args.mark_managed and not str(args.migration_occurrence_id).strip():
        parser.error("--mark-managed requires --migration-occurrence-id")
    return args


def _load_settings(
    core_config_path: Path,
    life_config_path: Path,
) -> StorageFactorySettings:
    core = CoreConfig.load(core_config_path, auto_update=False)
    life = LifeEngineConfig.load(life_config_path, auto_update=False)
    return settings_from_life_engine_config(life, global_config=core)


def _base_report(
    settings: StorageFactorySettings,
    *,
    mode: str,
) -> dict[str, Any]:
    return {
        "status": "planned" if mode == "dry_run" else "running",
        "mode": mode,
        "backend": settings.authoritative_backend.value,
        "generation_id": settings.backend_generation,
        "opportunity_schema_version": OPPORTUNITY_SCHEMA_VERSION,
        "database_immutability_required": True,
        "installs_capabilities": False,
        "opens_writer_runtime": mode != "dry_run",
        "authority_membership_may_change": mode != "dry_run",
    }


def _marker_report(marker: OpportunityRuntimeMarker) -> dict[str, Any]:
    return {
        "present": True,
        "marker_key": marker.marker_key,
        "generation_id": marker.generation_id,
        "migration_occurrence_id": marker.migration_occurrence_id,
        "schema_version": marker.schema_version,
        "marker_sha256": marker.marker_sha256,
    }


def _missing_marker_report() -> dict[str, Any]:
    return {
        "present": False,
        "reason": "managed_marker_not_written",
    }


async def _release_runtime(runtime: Any) -> None:
    """Revoke owned authority, then close every caller-owned runtime resource."""

    errors: list[BaseException] = []
    try:
        await runtime.revoke_authority()
    except BaseException as exc:  # noqa: BLE001 - close must still run
        errors.append(exc)
    try:
        await runtime.close()
    except BaseException as exc:  # noqa: BLE001 - aggregate owned cleanup
        errors.append(exc)
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup("Opportunity runtime cleanup failed", errors)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = _load_settings(args.core_config, args.life_config)
    if not settings.enabled:
        raise RuntimeError("OpportunityPreparationRequiresSelectedStorage")
    if not settings.backend_generation:
        raise RuntimeError("OpportunityPreparationRequiresGeneration")

    mode = "apply" if args.apply else "verify" if args.verify else "dry_run"
    report = _base_report(settings, mode=mode)
    if mode == "dry_run":
        report["planned_operations"] = [
            "verify_selected_generation",
            "verify_opportunity_schema",
        ]
        return report

    if mode != "dry_run" and not args.confirm_writer_runtime:
        raise RuntimeError("OpportunityWriterRuntimeAcknowledgementRequired")
    if mode != "dry_run" and args.confirm_generation != settings.backend_generation:
        raise RuntimeError("OpportunityGenerationConfirmationMismatch")

    if args.activate_local_authority:
        if (
            settings.authoritative_backend != BackendKind.LOCAL
            or settings.authority_provider != "file"
        ):
            raise RuntimeError("OpportunityMaintenanceRequiresLocalFileAuthority")
        # This is the canonical service lock, not an independently supplied path.
        # It remains held through authority revocation and engine disposal.
        with SingleInstanceLock(_MAINTENANCE_LOCK_PATH):
            runtime = await _open_local_maintenance_runtime(settings)
            return await _prepare_runtime(args, settings, report, runtime)
    runtime = await open_storage_backend(settings)
    return await _prepare_runtime(args, settings, report, runtime)


async def _open_local_maintenance_runtime(settings: StorageFactorySettings) -> Any:
    """Activate only a verified, inactive local generation under the entry lock."""

    registry = FileAuthorityRegistry(
        settings.local.authority_state_path, registry_id=settings.registry_id
    )
    generation = await registry.get_generation(settings.backend_generation)
    if (
        generation is None
        or generation.backend != settings.authoritative_backend
        or generation.status != GenerationStatus.VERIFIED
        or generation.schema_version != settings.schema_version
    ):
        raise RuntimeError("OpportunityMaintenanceRequiresVerifiedGeneration")
    health = await registry.health()
    if health.get("status") != "disabled" or health.get("active_generation"):
        raise RuntimeError("OpportunityMaintenanceRequiresInactiveAuthority")
    owner_id = f"{settings.authority_owner_id}:opportunity-maintenance:{os.getpid()}"
    activation = asyncio.create_task(
        registry.activate_generation(
            generation.generation_id,
            expected_epoch=int(health["authority_epoch"]),
            owner_id=owner_id,
            lease_seconds=settings.authority_lease_seconds,
            confirm_previous_writers_stopped=False,
        ),
        name="opportunity-maintenance-authority",
    )
    try:
        token = await asyncio.shield(activation)
    except asyncio.CancelledError as primary:
        # File authority uses a thread. Do not release the process lock while
        # that thread can still acquire authority after our cancellation.
        while not activation.done():
            try:
                await asyncio.shield(activation)
            except asyncio.CancelledError:
                continue
            except BaseException:  # noqa: BLE001 - collect settled activation below
                break
        try:
            acquired = activation.result()
            await registry.revoke(acquired)
        except BaseException as cleanup_error:  # noqa: BLE001 - preserve cancellation
            primary.add_note(
                "Opportunity activation cleanup: "
                f"{type(cleanup_error).__name__}"
            )
        raise
    activated = replace(
        settings,
        authority_epoch=token.authority_epoch,
        authority_owner_id=owner_id,
        fencing_token_env=_MAINTENANCE_FENCING_ENV,
    )
    try:
        return await open_storage_backend(
            activated,
            environment={_MAINTENANCE_FENCING_ENV: token.fencing_token},
        )
    except BaseException as primary:
        try:
            await registry.revoke(token)
        except BaseException as cleanup_error:  # noqa: BLE001 - preserve open failure
            primary.add_note(
                "Opportunity activation cleanup: "
                f"{type(cleanup_error).__name__}"
            )
        raise


async def _prepare_runtime(
    args: argparse.Namespace,
    settings: StorageFactorySettings,
    report: dict[str, Any],
    runtime: Any,
) -> dict[str, Any]:
    """Prepare schema and release the owned runtime before releasing its lock."""

    try:
        if runtime.writer_role != StorageWriterRole.ACTIVE:
            raise RuntimeError("OpportunityPreparationRequiresActiveAuthority")
        if runtime.generation is None:
            raise RuntimeError("OpportunityPreparationRequiresGeneration")
        if runtime.generation.generation_id != settings.backend_generation:
            raise RuntimeError("OpportunityGenerationMismatch")
        await runtime.validate_writer()

        if args.apply:
            await ensure_runtime_state_schema(runtime)
            await ensure_opportunity_schema(
                runtime,
                require_database_immutability=True,
            )
            await verify_opportunity_schema(
                runtime,
                require_database_immutability=True,
                require_scheduler_claim_guard=True,
            )
            if args.mark_managed:
                marker = await mark_opportunity_runtime_managed(
                    runtime,
                    migration_occurrence_id=args.migration_occurrence_id,
                )
                report["managed_marker"] = _marker_report(marker)
            else:
                marker = await read_opportunity_runtime_marker(runtime)
                report["managed_marker"] = (
                    _marker_report(marker) if marker else _missing_marker_report()
                )
            report["status"] = "applied"
        else:
            await verify_opportunity_schema(
                runtime,
                require_database_immutability=True,
                require_scheduler_claim_guard=True,
            )
            marker = await read_opportunity_runtime_marker(runtime)
            report["managed_marker"] = (
                _marker_report(marker) if marker else _missing_marker_report()
            )
            report["status"] = "verified"
        await runtime.validate_writer()
    except BaseException as primary:
        try:
            await _release_runtime(runtime)
        except BaseException as cleanup_error:  # noqa: BLE001 - preserve primary
            primary.add_note(
                "Opportunity runtime cleanup also failed: "
                f"{type(cleanup_error).__name__}"
            )
        raise
    await _release_runtime(runtime)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = asyncio.run(_run(_arguments(argv)))
    except Exception as exc:  # noqa: BLE001 - output must remain content-free
        print(
            canonical_json(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
            )
        )
        return 2
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
