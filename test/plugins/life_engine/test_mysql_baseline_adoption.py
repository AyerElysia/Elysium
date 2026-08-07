from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from plugins.life_engine.storage.models import BackendKind, GenerationStatus
from scripts.adopt_life_mysql_baseline import (
    ATTENTION_TABLES,
    DOMAIN_TABLES,
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
    assert RUNTIME_STATE_TABLES == ("runtime_states", "runtime_events")
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
