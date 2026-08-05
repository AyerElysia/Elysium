"""Cutover audit must fail closed when any domain evidence is incomplete."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from plugins.life_engine.storage.migration.manifest import snapshot_manifest_sha256
from plugins.life_engine.storage.models import BackendGeneration
from scripts.audit_life_storage_cutover import _write_generation, evaluate_cutover_runs


def _manifest(*, writer_frozen: bool) -> dict[str, object]:
    return {
        "manifest_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64,
        "writer_frozen": writer_frozen,
    }


def _run(domain: str, *, writer_frozen: bool = True) -> dict[str, object]:
    immutability = "fenced-cas" if domain == "presence_world" else "trigger-enforced"
    verification: dict[str, object] = {
        "verified": True,
        "database_immutability": immutability,
        "copy": {"target_root_sha256": "c" * 64},
    }
    if domain == "attention_thread":
        verification.update(
            {
                "legacy_snapshot": {
                    "import_mode": "snapshot_only",
                    "history_claim": "no_fabricated_events",
                    "generation_eligible": False,
                },
                "canonical_authority": {
                    "generation_eligible": True,
                    "event_frontier": 0,
                    "head_count": 0,
                    "focus_count": 0,
                    "root_sha256": "f" * 64,
                },
            }
        )
    return {
        "run_id": f"{domain}:run",
        "source_manifest_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64,
        "writer_frozen": writer_frozen,
        "state": "verified",
        "copied_records": 10,
        "conflict_count": 0,
        "metadata": {"domain": domain},
        "verification": verification,
    }


def _runs() -> dict[str, dict[str, object]]:
    return {
        domain: _run(domain)
        for domain in (
            "life_event",
            "life_memory",
            "subject_document",
            "presence_world",
            "life_learning",
            "attention_thread",
        )
    }


def test_cutover_audit_accepts_only_complete_frozen_trigger_evidence() -> None:
    result = evaluate_cutover_runs(
        _manifest(writer_frozen=True),
        {
            "verified": True,
            "manifest_sha256": "a" * 64,
            "verification_root_sha256": "d" * 64,
        },
        _runs(),
    )

    assert result["eligible"] is True
    assert result["failures"] == []
    assert set(result["root_hashes"]) == {
        "mysql:life_event",
        "mysql:life_memory",
        "mysql:subject_document",
        "mysql:presence_world",
        "mysql:life_learning",
        "mysql:attention_thread",
    }


def test_cutover_rejects_fabricated_or_activatable_legacy_attention() -> None:
    runs = _runs()
    attention = runs["attention_thread"]["verification"]
    assert isinstance(attention, dict)
    legacy = attention["legacy_snapshot"]
    assert isinstance(legacy, dict)
    legacy["generation_eligible"] = True

    result = evaluate_cutover_runs(
        _manifest(writer_frozen=True),
        {
            "verified": True,
            "manifest_sha256": "a" * 64,
            "verification_root_sha256": "d" * 64,
        },
        runs,
    )

    assert result["eligible"] is False
    assert any("legacy Attention snapshot" in reason for reason in result["failures"])


def test_cutover_audit_rejects_online_shadow_and_application_immutability() -> None:
    runs = deepcopy(_runs())
    for run in runs.values():
        run["writer_frozen"] = False
        run["state"] = "copied"
    runs["life_memory"]["verification"]["database_immutability"] = (
        "application-enforced-shadow"
    )

    result = evaluate_cutover_runs(
        _manifest(writer_frozen=False),
        {
            "verified": True,
            "manifest_sha256": "a" * 64,
            "verification_root_sha256": "d" * 64,
        },
        runs,
    )

    assert result["eligible"] is False
    assert "snapshot writer_frozen is false" in result["failures"]
    assert any("life_memory" in reason for reason in result["failures"])


def test_ineligible_audit_never_creates_generation_directory(tmp_path: Path) -> None:
    destination = tmp_path / "generation"

    with pytest.raises(RuntimeError, match="not eligible"):
        _write_generation(
            destination,
            generation_id="must-not-exist",
            manifest={},
            snapshot_verification={},
            audit={"eligible": False},
        )

    assert not destination.exists()


def test_eligible_audit_writes_a_validated_generation(tmp_path: Path) -> None:
    destination = tmp_path / "generation"
    manifest = {
        "schema_version": 1,
        "created_at": "2026-08-05T00:00:00+00:00",
        "writer_frozen": True,
        "source_snapshot_sha256": "b" * 64,
        "root_hashes": {"local": "d" * 64},
        "frontiers": {"life_event": 10},
    }
    manifest["manifest_sha256"] = snapshot_manifest_sha256(manifest)
    audit = {
        "eligible": True,
        "root_hashes": {"mysql:life_event": "c" * 64},
    }

    sealed = _write_generation(
        destination,
        generation_id="mysql-generation-1",
        manifest=manifest,
        snapshot_verification={
            "verified": True,
            "verified_at": "2026-08-05T00:01:00+00:00",
            "verification_root_sha256": "e" * 64,
        },
        audit=audit,
    )

    persisted = json.loads((destination / "generation.json").read_text())
    generation = BackendGeneration.from_dict(persisted)
    assert generation.root_hashes["mysql:life_event"] == "c" * 64
    assert sealed["manifest_sha256"] == generation.manifest_sha256
