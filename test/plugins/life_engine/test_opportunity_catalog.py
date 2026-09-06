"""Contracts for the inactive opportunity capability catalog."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.opportunity.catalog import (
    CAPABILITY_DEFAULT_SKILL_NAME,
    CAPABILITY_MANIFEST_NAME,
    CAPABILITY_MANUAL_NAME,
    CapabilityCatalog,
    CapabilityCatalogError,
    CapabilityPackageConflict,
    load_capability_package,
)


def _manifest(capability_id: str = "learning", **changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "capability_id": capability_id,
        "package_version": "1.0.0",
        "removability": "subject_removable",
        "provider_kind": "cognitive_workflow",
        "manual": CAPABILITY_MANUAL_NAME,
        "default_skill": CAPABILITY_DEFAULT_SKILL_NAME,
        "operations": ["nucleus_learn", "learning.query"],
        "dependencies": [],
    }
    payload.update(changes)
    return payload


def _write_package(
    root: Path,
    *,
    capability_id: str = "learning",
    manifest_changes: dict[str, Any] | None = None,
    manifest_bytes: bytes | None = None,
    manual: bytes = b"# Technical capability\n",
    default_skill: bytes = b"# Subject-editable workflow template\n",
) -> Path:
    root.mkdir(parents=True)
    manifest = _manifest(capability_id, **(manifest_changes or {}))
    encoded_manifest = manifest_bytes or json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (root / CAPABILITY_MANIFEST_NAME).write_bytes(encoded_manifest)
    (root / CAPABILITY_MANUAL_NAME).write_bytes(manual)
    (root / CAPABILITY_DEFAULT_SKILL_NAME).write_bytes(default_skill)
    return root


def test_loads_three_exact_artifacts_and_keeps_manual_separate(tmp_path: Path) -> None:
    manual = "# 工程能力\n这是稳定接口。\n".encode()
    workflow = "# 爱莉可修改的流程\n这不是权限。\n".encode()
    package = _write_package(
        tmp_path / "learning",
        manual=manual,
        default_skill=workflow,
    )

    descriptor = load_capability_package(package)

    assert descriptor.capability_id == "learning"
    assert descriptor.provider_kind == "cognitive_workflow"
    assert descriptor.operations == ("nucleus_learn", "learning.query")
    assert descriptor.technical_manual.content_bytes == manual
    assert descriptor.default_skill_template.content_bytes == workflow
    assert descriptor.technical_manual is not descriptor.default_skill_template
    assert descriptor.manual_sha256 == hashlib.sha256(manual).hexdigest()
    assert descriptor.default_skill_sha256 == hashlib.sha256(workflow).hexdigest()
    assert len(descriptor.package_sha256) == 64


def test_exact_file_byte_change_changes_package_digest(tmp_path: Path) -> None:
    first = load_capability_package(
        _write_package(tmp_path / "first", manual=b"# Manual\n")
    )
    second = load_capability_package(
        _write_package(tmp_path / "second", manual=b"# Manual\r\n")
    )

    assert first.manual_sha256 != second.manual_sha256
    assert first.package_sha256 != second.package_sha256


def test_duplicate_capability_same_digest_is_idempotent(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "learning")
    descriptor = load_capability_package(package)
    catalog = CapabilityCatalog()

    first = catalog.add(descriptor)
    second = catalog.add(descriptor)

    assert first is second
    assert catalog.list_descriptors() == (descriptor,)


def test_duplicate_capability_different_digest_is_conflict(tmp_path: Path) -> None:
    first = load_capability_package(
        _write_package(tmp_path / "first", manual=b"# First\n")
    )
    second = load_capability_package(
        _write_package(tmp_path / "second", manual=b"# Second\n")
    )
    catalog = CapabilityCatalog([first])

    with pytest.raises(CapabilityPackageConflict, match="learning"):
        catalog.add(second)


def test_discovers_immediate_packages_in_stable_id_order(tmp_path: Path) -> None:
    _write_package(tmp_path / "z-dir", capability_id="memory.review")
    _write_package(tmp_path / "a-dir", capability_id="learning")
    (tmp_path / "unrelated").mkdir()
    catalog = CapabilityCatalog()

    discovered = catalog.discover(tmp_path)

    assert [item.capability_id for item in discovered] == [
        "learning",
        "memory.review",
    ]
    assert [item.capability_id for item in catalog.list_descriptors()] == [
        "learning",
        "memory.review",
    ]


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "importance",
        "priority",
        "score",
        "audience",
        "platform",
        "prewritten_action",
        "prewrittenAction",
        "prewritten_expression",
    ],
)
def test_rejects_subjective_ranking_targeting_and_prewritten_action_fields(
    tmp_path: Path,
    forbidden_key: str,
) -> None:
    package = _write_package(
        tmp_path / forbidden_key,
        manifest_changes={forbidden_key: "must run"},
    )

    with pytest.raises(CapabilityCatalogError, match="forbidden field"):
        load_capability_package(package)


def test_rejects_forbidden_fields_even_when_nested(tmp_path: Path) -> None:
    package = _write_package(
        tmp_path / "nested",
        manifest_changes={"operations": [{"id": "learning.run", "score": 9}]},
    )

    with pytest.raises(CapabilityCatalogError, match="score"):
        load_capability_package(package)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": True}, "schema_version"),
        ({"schema_version": 2}, "schema_version"),
        ({"removability": "foundation"}, "subject_removable"),
        ({"provider_kind": "Learning Provider"}, "snake_case"),
        ({"manual": "../CAPABILITY.md"}, "manual"),
        ({"default_skill": "SKILL.md"}, "default_skill"),
        ({"operations": ["nucleus_learn", "nucleus_learn"]}, "duplicate"),
        ({"dependencies": ["learning"]}, "depend on itself"),
    ],
)
def test_rejects_ambiguous_or_non_removable_manifest_contracts(
    tmp_path: Path,
    changes: dict[str, Any],
    message: str,
) -> None:
    package = _write_package(
        tmp_path / message.replace(" ", "-"), manifest_changes=changes
    )

    with pytest.raises(CapabilityCatalogError, match=message):
        load_capability_package(package)


def test_partial_package_fails_instead_of_disappearing(tmp_path: Path) -> None:
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / CAPABILITY_MANUAL_NAME).write_text("# Manual\n", encoding="utf-8")

    with pytest.raises(CapabilityCatalogError, match="missing manifest.json"):
        CapabilityCatalog().discover(tmp_path)


def test_unknown_manifest_field_is_rejected(tmp_path: Path) -> None:
    package = _write_package(
        tmp_path / "unknown",
        manifest_changes={"automatic_decision": False},
    )

    with pytest.raises(CapabilityCatalogError, match="unknown fields"):
        load_capability_package(package)


def test_duplicate_json_field_is_rejected(tmp_path: Path) -> None:
    encoded = json.dumps(
        _manifest(),
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = encoded.replace(
        '"capability_id":"learning"',
        '"capability_id":"learning","capability_id":"life.other"',
        1,
    )
    package = _write_package(
        tmp_path / "duplicate-json",
        manifest_bytes=encoded.encode(),
    )

    with pytest.raises(CapabilityCatalogError, match="duplicate field"):
        load_capability_package(package)


def test_batch_discovery_conflict_leaves_catalog_unmodified(tmp_path: Path) -> None:
    _write_package(tmp_path / "one", manual=b"# One\n")
    _write_package(tmp_path / "two", manual=b"# Two\n")
    catalog = CapabilityCatalog()

    with pytest.raises(CapabilityPackageConflict):
        catalog.discover(tmp_path)

    assert len(catalog) == 0


def test_provider_kind_is_open_not_a_closed_domain_enum(tmp_path: Path) -> None:
    descriptor = load_capability_package(
        _write_package(
            tmp_path / "future",
            capability_id="life.future.embodiment",
            manifest_changes={"provider_kind": "future_embodied_play"},
        )
    )

    assert descriptor.provider_kind == "future_embodied_play"


def test_default_skill_body_is_loaded_as_data_and_never_executed(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-exist"
    workflow = (
        "# Workflow text only\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "requested_extra_permission: root\n"
    ).encode()
    descriptor = load_capability_package(
        _write_package(tmp_path / "data-only", default_skill=workflow)
    )

    assert descriptor.default_skill_template.content_bytes == workflow
    assert not marker.exists()


def test_package_file_symlink_is_rejected(tmp_path: Path) -> None:
    external = tmp_path / "outside.md"
    external.write_text("# Outside\n", encoding="utf-8")
    package = _write_package(tmp_path / "symlink")
    manual = package / CAPABILITY_MANUAL_NAME
    manual.unlink()
    manual.symlink_to(external)

    with pytest.raises(CapabilityCatalogError, match="symlink"):
        load_capability_package(package)
