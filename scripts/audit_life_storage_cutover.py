#!/usr/bin/env python3
"""Audit whether one frozen snapshot and its MySQL copies may be sealed."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.storage.migration.copy_authority import (
    CopyAuthorityError,
    MySQLCopyAuthorityRegistry,
)
from plugins.life_engine.storage.migration.manifest import (
    build_backend_generation,
    load_snapshot_manifest,
)
from plugins.life_engine.storage.migration.verify import verify_local_snapshot
from plugins.life_engine.storage.models import BackendKind
from src.kernel.storage import (
    MySQLStorageConfig,
    canonical_json,
    canonical_json_sha256,
    create_mysql_storage_engine,
)

_REQUIRED_DOMAINS = (
    "life_event",
    "life_memory",
    "subject_document",
    "presence_world",
    "life_learning",
    "attention_thread",
)
_IMMUTABLE_DOMAINS = frozenset(
    {
        "life_event",
        "life_memory",
        "subject_document",
        "life_learning",
        "attention_thread",
    }
)


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--life-event-run", required=True)
    parser.add_argument("--memory-run", required=True)
    parser.add_argument("--subject-run", required=True)
    parser.add_argument("--presence-world-run", required=True)
    parser.add_argument("--learning-run", required=True)
    parser.add_argument("--attention-run", required=True)
    parser.add_argument("--generation-id")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _domain_root(domain: str, verification: dict[str, Any]) -> str:
    if domain == "attention_thread":
        authority = dict(verification.get("canonical_authority") or {})
        value = str(authority.get("root_sha256") or "")
        if len(value) == 64:
            return value
    if domain == "life_learning":
        imported = dict(verification.get("import_verification") or {})
        value = str(imported.get("snapshot_sha256") or "")
        if len(value) == 64:
            return value
    copy = dict(verification.get("copy") or {})
    for key in (
        "target_root_sha256",
        "target_root",
        "root_sha256",
        "source_root_sha256",
    ):
        value = str(copy.get(key) or "")
        if len(value) == 64:
            return value
    return canonical_json_sha256(verification)


def evaluate_cutover_runs(
    manifest: dict[str, Any],
    snapshot_verification: dict[str, Any],
    runs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate only recorded evidence; never infer a missing gate as success."""

    manifest_hash = str(manifest.get("manifest_sha256") or "")
    snapshot_hash = str(manifest.get("source_snapshot_sha256") or "")
    writer_frozen = bool(manifest.get("writer_frozen"))
    domain_results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    root_hashes: dict[str, str] = {}
    for domain in _REQUIRED_DOMAINS:
        run = runs.get(domain)
        reasons: list[str] = []
        if run is None:
            reasons.append("copy run is missing")
            domain_results[domain] = {"eligible": False, "reasons": reasons}
            failures.append(f"{domain}: copy run is missing")
            continue
        verification = dict(run.get("verification") or {})
        metadata = dict(run.get("metadata") or {})
        if str(metadata.get("domain") or "") != domain:
            reasons.append("copy run domain differs")
        if str(run.get("source_manifest_sha256") or "") != manifest_hash:
            reasons.append("copy run manifest differs")
        if str(run.get("source_snapshot_sha256") or "") != snapshot_hash:
            reasons.append("copy run snapshot differs")
        if bool(run.get("writer_frozen")) != writer_frozen:
            reasons.append("copy run freeze evidence differs")
        if str(run.get("state") or "") != "verified":
            reasons.append("copy run is not verified")
        if int(run.get("conflict_count") or 0) != 0:
            reasons.append("copy run has conflicts")
        if not bool(verification.get("verified")):
            reasons.append("domain verification is not verified")
        immutability = str(verification.get("database_immutability") or "")
        if domain in _IMMUTABLE_DOMAINS and immutability != "trigger-enforced":
            reasons.append("database immutability is not trigger-enforced")
        if domain == "attention_thread":
            legacy = dict(verification.get("legacy_snapshot") or {})
            authority = dict(verification.get("canonical_authority") or {})
            if legacy.get("import_mode") != "snapshot_only":
                reasons.append("legacy Attention evidence is not snapshot-only")
            if legacy.get("history_claim") != "no_fabricated_events":
                reasons.append("legacy Attention history claim is unsafe")
            if legacy.get("generation_eligible") is not False:
                reasons.append("legacy Attention snapshot is incorrectly activatable")
            if not bool(authority.get("generation_eligible")):
                reasons.append("canonical Attention authority is not generation-ready")
            if int(authority.get("event_frontier", -1)) != 0:
                reasons.append("canonical Attention authority did not start empty")
            if int(authority.get("head_count", -1)) != 0:
                reasons.append("canonical Attention heads did not start empty")
            if int(authority.get("focus_count", -1)) != 0:
                reasons.append("canonical Attention focus did not start empty")
        domain_results[domain] = {
            "run_id": str(run.get("run_id") or ""),
            "state": str(run.get("state") or ""),
            "copied_records": int(run.get("copied_records") or 0),
            "conflict_count": int(run.get("conflict_count") or 0),
            "database_immutability": immutability,
            "eligible": not reasons,
            "reasons": reasons,
        }
        if reasons:
            failures.extend(f"{domain}: {reason}" for reason in reasons)
        else:
            root_hashes[f"mysql:{domain}"] = _domain_root(domain, verification)

    if not bool(snapshot_verification.get("verified")):
        failures.append("snapshot independent verification failed")
    if not writer_frozen:
        failures.append("snapshot writer_frozen is false")
    if str(snapshot_verification.get("manifest_sha256") or "") != manifest_hash:
        failures.append("snapshot verification manifest differs")
    eligible = not failures
    return {
        "eligible": eligible,
        "writer_frozen": writer_frozen,
        "manifest_sha256": manifest_hash,
        "source_snapshot_sha256": snapshot_hash,
        "snapshot_verification": snapshot_verification,
        "domains": domain_results,
        "root_hashes": root_hashes,
        "failures": failures,
    }


def _write_generation(
    destination: Path,
    *,
    generation_id: str,
    manifest: dict[str, Any],
    snapshot_verification: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    if not audit["eligible"]:
        raise RuntimeError("cutover evidence is not eligible for generation sealing")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    generation = build_backend_generation(
        manifest,
        generation_id=generation_id,
        backend=BackendKind.MYSQL,
        backend_schema_version=1,
        verification=snapshot_verification,
    )
    if generation.status.value != "verified":
        raise RuntimeError("cutover evidence is not eligible for generation sealing")
    generation = replace(
        generation,
        root_hashes={
            **generation.root_hashes,
            **dict(audit["root_hashes"]),
        },
        metadata={
            **generation.metadata,
            "cutover_audit_sha256": canonical_json_sha256(audit),
            "required_domains": list(_REQUIRED_DOMAINS),
        },
    )
    body = generation.to_dict()
    sealed = {
        **body,
        "manifest_sha256": generation.manifest_sha256,
    }
    (destination / "generation.json").write_text(
        canonical_json(sealed) + "\n",
        encoding="utf-8",
    )
    (destination / "cutover-audit.json").write_text(
        canonical_json(audit) + "\n",
        encoding="utf-8",
    )
    return sealed


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = args.snapshot.resolve()
    manifest = load_snapshot_manifest(snapshot / "manifest.json")
    snapshot_verification = verify_local_snapshot(snapshot)
    config = MySQLStorageConfig(
        host=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_HOST"),
        port=int(_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_PORT")),
        database=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_DATABASE"),
        user=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_USER"),
        password=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_PASSWORD"),
        ssl_mode="disabled",
        pool_size=2,
        max_overflow=1,
        connect_timeout_seconds=10,
        pool_timeout_seconds=15,
        application_query_timeout_seconds=30,
        innodb_lock_wait_timeout_seconds=5,
    )
    engine = create_mysql_storage_engine(config)
    registry = MySQLCopyAuthorityRegistry(engine)
    run_ids = {
        "life_event": args.life_event_run,
        "life_memory": args.memory_run,
        "subject_document": args.subject_run,
        "presence_world": args.presence_world_run,
        "life_learning": args.learning_run,
        "attention_thread": args.attention_run,
    }
    try:
        runs: dict[str, dict[str, Any]] = {}
        for domain, run_id in run_ids.items():
            try:
                runs[domain] = await registry.get_run(run_id)
            except CopyAuthorityError:
                continue
        audit = evaluate_cutover_runs(manifest, snapshot_verification, runs)
        result: dict[str, Any] = {
            "audited_at": datetime.now(UTC).isoformat(),
            "backend_identity": config.safe_identity,
            "audit": audit,
        }
        if args.output is not None or args.generation_id is not None:
            if args.output is None or not str(args.generation_id or "").strip():
                raise RuntimeError(
                    "--output and --generation-id must be provided together"
                )
            result["generation"] = _write_generation(
                args.output,
                generation_id=str(args.generation_id),
                manifest=manifest,
                snapshot_verification=snapshot_verification,
                audit=audit,
            )
        return result
    finally:
        await engine.dispose()


def main() -> int:
    try:
        result = asyncio.run(_run(_arguments()))
    except Exception as exc:  # noqa: BLE001 - CLI emits only the bounded type
        print(canonical_json({"status": "failed", "reason": type(exc).__name__}))
        return 2
    print(canonical_json(result))
    return 0 if bool(result["audit"]["eligible"]) else 3


if __name__ == "__main__":
    raise SystemExit(main())
