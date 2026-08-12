from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from plugins.life_engine.storage.models import BackendKind, GenerationStatus
from scripts import adopt_life_mysql_baseline as baseline
from scripts.adopt_life_mysql_baseline import (
    ATTENTION_TABLES,
    DOMAIN_TABLES,
    LEARNING_TABLES,
    RUNTIME_STATE_TABLES,
    TableEvidence,
    _domain_root,
    _encoded_value,
    _guard_trigger_installation,
    build_remote_generation,
)


def test_remote_baseline_table_contract_is_explicit_and_unique() -> None:
    tables = [table for values in DOMAIN_TABLES.values() for table in values]

    assert set(DOMAIN_TABLES) == {
        "life_event",
        "life_memory",
        "subject_document",
        "presence_world",
        "life_learning",
    }
    assert len(tables) == len(set(tables))
    assert "subject_document_versions" in tables
    assert "storage_backend_generations" not in tables
    assert RUNTIME_STATE_TABLES == (
        "runtime_states",
        "runtime_events",
        "runtime_singleton_writer_claims",
        "runtime_singleton_writer_events",
        "runtime_singleton_writer_bindings",
    )
    assert LEARNING_TABLES == DOMAIN_TABLES["life_learning"]
    assert ATTENTION_TABLES == (
        "attention_thread_events",
        "attention_thread_heads",
        "attention_instance_focus",
        "attention_legacy_snapshots",
        "attention_legacy_candidates",
    )
    assert not set(RUNTIME_STATE_TABLES).intersection(tables)
    assert not set(ATTENTION_TABLES).intersection(tables)
    assert not set(ATTENTION_TABLES).intersection(RUNTIME_STATE_TABLES)


@pytest.mark.asyncio
async def test_learning_upgrade_orders_claim_and_learning_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class _Runner:
        def __init__(self, _engine: object, *, table_name: str, lock_name: str):
            calls.append(("runner", table_name, lock_name))

        async def apply(self, migrations: tuple[object, ...]) -> None:
            calls.append(
                (
                    "apply",
                    tuple(
                        (migration.version, migration.name)  # type: ignore[attr-defined]
                        for migration in migrations
                    ),
                )
            )

    async def _verify(_engine: object, contracts: tuple[object, ...]) -> None:
        calls.append(
            (
                "verify",
                tuple(contract.name for contract in contracts),  # type: ignore[attr-defined]
            )
        )

    monkeypatch.setattr(baseline, "MySQLMigrationRunner", _Runner)
    monkeypatch.setattr(baseline, "verify_mysql_trigger_contract", _verify)

    await baseline._install_learning_schema(object())  # type: ignore[arg-type]

    assert calls[0] == (
        "runner",
        "life_singleton_writer_schema_migrations",
        "elysium:life-singleton-writer-schema",
    )
    assert calls[1] == ("apply", ((1, "life_singleton_writer_claim_v1"),))
    assert calls[3] == (
        "runner",
        "life_learning_schema_migrations",
        "elysium:life-learning-schema",
    )
    assert calls[4] == (
        "apply",
        (
            (1, "life_learning_storage_v1"),
            (2, "life_learning_singleton_claim_guard_v2"),
            (3, "life_learning_singleton_claim_guard_retirement_v3"),
            (4, "life_learning_projector_claim_guard_v4"),
        ),
    )
    assert calls[5][0] == "verify"
    assert set(calls[5][1]) == {
        "learning_events_immutable_update_v1",
        "learning_events_immutable_delete_v1",
        "learning_projections_projector_claim_insert_v4",
        "learning_projections_projector_claim_update_v4",
        "learning_projections_projector_claim_delete_v4",
    }


@pytest.mark.asyncio
async def test_memory_upgrade_orders_schema_immutability_and_trigger_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class _Runner:
        def __init__(self, _engine: object, *, table_name: str, lock_name: str):
            calls.append(("runner", table_name, lock_name))

        async def apply(self, migrations: tuple[object, ...]) -> None:
            calls.append(
                (
                    "apply",
                    tuple(
                        (migration.version, migration.name)  # type: ignore[attr-defined]
                        for migration in migrations
                    ),
                )
            )

    async def _verify(_engine: object, contracts: tuple[object, ...]) -> None:
        calls.append(
            (
                "verify",
                tuple(contract.name for contract in contracts),  # type: ignore[attr-defined]
            )
        )

    monkeypatch.setattr(baseline, "MySQLMigrationRunner", _Runner)
    monkeypatch.setattr(baseline, "verify_mysql_trigger_contract", _verify)

    await baseline._install_memory_schema(object())  # type: ignore[arg-type]

    assert calls[:4] == [
        (
            "runner",
            "life_memory_schema_migrations",
            "elysium:life-memory-schema",
        ),
        (
            "apply",
            tuple((item.version, item.name) for item in baseline.MEMORY_MIGRATIONS),
        ),
        (
            "runner",
            "life_memory_immutability_schema_migrations",
            "elysium:life-memory-immutability",
        ),
        (
            "apply",
            tuple(
                (item.version, item.name)
                for item in baseline.MEMORY_IMMUTABILITY_MIGRATIONS
            ),
        ),
    ]
    assert calls[4][0] == "verify"
    assert len(calls[4][1]) == len(baseline.MEMORY_IMMUTABILITY_TRIGGER_CONTRACT)
    assert {
        "memory_workspace_projection_events_immutable_update",
        "memory_workspace_projection_events_immutable_delete",
    }.issubset(set(calls[4][1]))


def test_memory_trigger_contract_uses_delivery_specific_error_marker() -> None:
    contracts = {
        item.table: item.action_fragment
        for item in baseline._memory_trigger_contracts()
    }

    assert contracts["memory_witnesses"] == "MemoryWitnessAuthorityImmutable"
    assert (
        contracts["memory_witness_delivery_jobs"]
        == "MemoryWitnessDeliveryAuthorityImmutable"
    )
    assert contracts["memory_experiences"] == "MemoryAuthorityRecordImmutable"


def _memory_upgrade_snapshot(*, new_tables_present: bool) -> dict[str, object]:
    existing = TableEvidence(
        "memory_experiences",
        3,
        "a" * 64,
        ("event_id",),
        "InnoDB",
        "utf8mb4_bin",
    )
    projection_tables = (
        [
            TableEvidence(
                table_name,
                0,
                hashlib_empty,
                (
                    ("event_sha256",)
                    if table_name.endswith("events")
                    else ("storage_generation_id",)
                ),
                "InnoDB",
                "utf8mb4_bin",
            ).to_dict()
            for table_name in baseline.MEMORY_UPGRADE_NEW_TABLES
        ]
        if new_tables_present
        else []
    )
    authority = {
        "registry_id": "life-domain",
        "active_backend": "mysql",
        "active_generation": "generation-1",
        "authority_epoch": 7,
        "owner_id": "owner-1",
        "last_event_hash": "b" * 64,
        "authority_event_count": 4,
        "generation_backend": "mysql",
        "generation_status": "verified",
        "generation_manifest_sha256": "c" * 64,
    }
    return {
        "existing_memory": {
            "root_sha256": baseline._domain_root([existing]),
            "row_count": 3,
            "tables": [existing.to_dict()],
        },
        "workspace_projection": {
            "present_tables": [item["table_name"] for item in projection_tables],
            "root_sha256": "d" * 64,
            "row_count": 0,
            "tables": projection_tables,
        },
        "schema_migrations": baseline._expected_migration_evidence(
            baseline.MEMORY_MIGRATIONS
        ),
        "immutability_migrations": baseline._expected_migration_evidence(
            baseline.MEMORY_IMMUTABILITY_MIGRATIONS
        ),
        "installed_memory_trigger_names": sorted(
            contract.name for contract in baseline._memory_trigger_contracts()
        ),
        "active_singleton_claims": [],
        "local_elysium_processes": [],
        "authority": authority,
    }


hashlib_empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_memory_upgrade_invariants_accept_additive_empty_tables() -> None:
    before = _memory_upgrade_snapshot(new_tables_present=False)
    after = _memory_upgrade_snapshot(new_tables_present=True)

    baseline._assert_memory_upgrade_invariants(before, after)


def test_memory_upgrade_invariants_allow_schema_metadata_evolution() -> None:
    before = _memory_upgrade_snapshot(new_tables_present=False)
    after = _memory_upgrade_snapshot(new_tables_present=True)
    after["existing_memory"]["tables"][0].update(  # type: ignore[index]
        primary_key=["event_id", "revision"],
        table_collation="utf8mb4_0900_bin",
    )

    baseline._assert_memory_upgrade_invariants(before, after)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda value: value["existing_memory"].update(root_sha256="f" * 64),
            "existing Memory content changed",
        ),
        (
            lambda value: value["authority"].update(authority_epoch=8),
            "authority generation/epoch/owner changed",
        ),
        (
            lambda value: value["workspace_projection"]["tables"][0].update(
                row_count=1
            ),
            "new Memory table was not initialized empty",
        ),
    ),
)
def test_memory_upgrade_invariants_reject_data_or_authority_changes(
    mutation: object,
    message: str,
) -> None:
    before = _memory_upgrade_snapshot(new_tables_present=False)
    after = _memory_upgrade_snapshot(new_tables_present=True)

    mutation(after)  # type: ignore[operator]

    with pytest.raises(RuntimeError, match=message):
        baseline._assert_memory_upgrade_invariants(before, after)


def test_memory_upgrade_invariants_reject_live_writer_claim() -> None:
    before = _memory_upgrade_snapshot(new_tables_present=False)
    after = _memory_upgrade_snapshot(new_tables_present=True)
    before["active_singleton_claims"] = [{"namespace": "life_engine.runtime_context"}]

    with pytest.raises(RuntimeError, match="all Elysium writers to be stopped"):
        baseline._assert_memory_upgrade_invariants(before, after)


def test_memory_upgrade_invariants_reject_local_elysium_process() -> None:
    before = _memory_upgrade_snapshot(new_tables_present=False)
    after = _memory_upgrade_snapshot(new_tables_present=True)
    before["local_elysium_processes"] = [{"pid": 42}]

    with pytest.raises(RuntimeError, match="local Elysium process to stop"):
        baseline._assert_memory_upgrade_invariants(before, after)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda value: value["schema_migrations"].pop(),
            "schema migration contract is incomplete",
        ),
        (
            lambda value: value["immutability_migrations"].pop(),
            "immutability migration contract is incomplete",
        ),
        (
            lambda value: value["installed_memory_trigger_names"].pop(),
            "trigger contract is incomplete",
        ),
    ),
)
def test_memory_upgrade_invariants_reject_incomplete_installation(
    mutation: object,
    message: str,
) -> None:
    before = _memory_upgrade_snapshot(new_tables_present=False)
    after = _memory_upgrade_snapshot(new_tables_present=True)

    mutation(after)  # type: ignore[operator]

    with pytest.raises(RuntimeError, match=message):
        baseline._assert_memory_upgrade_invariants(before, after)


def test_cli_exposes_explicit_memory_upgrade_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adopt_life_mysql_baseline.py",
            "upgrade-memory",
            "--confirm-memory-upgrade",
            "--output",
            "evidence",
        ],
    )

    args = baseline._arguments()

    assert args.mode == "upgrade-memory"
    assert args.confirm_memory_upgrade is True
    assert args.output.name == "evidence"


@pytest.mark.asyncio
async def test_memory_upgrade_rejects_missing_confirmation(tmp_path: object) -> None:
    args = argparse.Namespace(
        confirm_memory_upgrade=False,
        output=tmp_path,
        registry_id="life-domain",
    )

    with pytest.raises(RuntimeError, match="--confirm-memory-upgrade"):
        await baseline._upgrade_memory(
            args,
            object(),  # type: ignore[arg-type]
            backend_identity="mysql://redacted",
        )


def test_evidence_value_encoding_preserves_type_and_exact_bytes() -> None:
    observed = {
        _encoded_value(None),
        _encoded_value(b"1"),
        _encoded_value("1"),
        _encoded_value(1),
        _encoded_value(Decimal("1.0")),
        _encoded_value(datetime(2026, 8, 5, tzinfo=UTC)),
    }

    assert len(observed) == 6
    with pytest.raises(TypeError, match="unsupported MySQL evidence value"):
        _encoded_value(object())


def test_domain_root_is_order_and_count_sensitive() -> None:
    first = TableEvidence("alpha", 1, "a" * 64, ("id",), "InnoDB", "utf8mb4_bin")
    second = TableEvidence("beta", 2, "b" * 64, ("id",), "InnoDB", "utf8mb4_bin")

    assert _domain_root([first, second]) != _domain_root([second, first])
    changed = TableEvidence("alpha", 2, "a" * 64, ("id",), "InnoDB", "utf8mb4_bin")
    assert _domain_root([first]) != _domain_root([changed])


def test_trigger_installation_rejects_untrusted_binary_log_policy() -> None:
    evidence = {
        "server_policy": {
            "trigger_privilege": True,
            "log_bin": True,
            "log_bin_trust_function_creators": False,
        }
    }

    with pytest.raises(RuntimeError, match="log_bin_trust_function_creators=ON"):
        _guard_trigger_installation(evidence)


def test_trigger_installation_accepts_required_privilege_and_policy() -> None:
    evidence = {
        "server_policy": {
            "trigger_privilege": True,
            "log_bin": True,
            "log_bin_trust_function_creators": True,
        }
    }

    _guard_trigger_installation(evidence)


def test_remote_generation_discloses_adoption_without_local_parity_claim() -> None:
    evidence = {
        "schema_version": 1,
        "global_root_sha256": "f" * 64,
        "domains": {
            name: {"root_sha256": format(index, "064x"), "row_count": index}
            for index, name in enumerate(DOMAIN_TABLES, start=1)
        },
    }

    generation = build_remote_generation(
        evidence,
        generation_id="life-remote-adopted-20260805-v1",
    )

    assert generation.backend == BackendKind.MYSQL
    assert generation.status == GenerationStatus.VERIFIED
    assert generation.source_snapshot_sha256 == "f" * 64
    assert generation.metadata["local_parity_claimed"] is False
    assert generation.metadata["adoption_mode"] == "existing_remote_shadow_baseline"
