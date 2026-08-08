from __future__ import annotations

import pytest

from plugins.life_engine.storage.instance_identity import (
    InstanceIdentity,
    InstanceIdentityError,
    assert_protocol_compatible,
    compute_config_digest,
    generate_boot_id,
    json_identity_digest,
)


def _identity(**overrides: object) -> InstanceIdentity:
    values: dict[str, object] = {
        "deployment_id": "deploy-a",
        "instance_id": "instance-1",
        "boot_id": "boot-1",
        "owner_id": "elysium-windows-primary",
        "protocol_version": 1,
        "schema_generation": "gen-v3",
        "config_digest": "c" * 64,
        "workspace_revision": "w-1",
    }
    values.update(overrides)
    return InstanceIdentity(**values)


def test_identity_validates_and_formats_owner() -> None:
    identity = _identity()
    identity.validate()
    assert identity.claim_owner == "deploy-a:instance-1:boot-1"
    assert identity.short_owner == "deploy-a:instance-1"


@pytest.mark.parametrize(
    "field,value",
    [
        ("deployment_id", ""),
        ("instance_id", "bad space"),
        ("boot_id", "含中文"),
        ("owner_id", "-leading-dash"),
        ("schema_generation", ""),
        ("config_digest", "x" * 300),
        ("workspace_revision", ""),
    ],
)
def test_identity_rejects_invalid_fields(field: str, value: str) -> None:
    with pytest.raises(InstanceIdentityError):
        _identity(**{field: value}).validate()


def test_identity_rejects_non_positive_protocol() -> None:
    with pytest.raises(InstanceIdentityError, match="protocol_version"):
        _identity(protocol_version=0).validate()


def test_boot_id_changes_every_generation() -> None:
    assert generate_boot_id() != generate_boot_id()
    assert generate_boot_id().startswith("boot-")


def test_config_digest_is_deterministic_and_content_sensitive() -> None:
    first = compute_config_digest({"embedding_dim": 1024, "model": "mimo"})
    second = compute_config_digest({"embedding_dim": 1024, "model": "mimo"})
    assert first == second
    changed = compute_config_digest({"embedding_dim": 768, "model": "mimo"})
    assert changed != first


def test_identity_digest_changes_when_boot_changes() -> None:
    before = json_identity_digest(_identity())
    after = json_identity_digest(_identity(boot_id="boot-2"))
    assert after != before


def test_protocol_compatibility_fails_closed() -> None:
    node = _identity(protocol_version=1)
    assert_protocol_compatible(
        node=node,
        generation_schema_version=3,
        protocol_version=1,
    )
    with pytest.raises(InstanceIdentityError, match="incompatible"):
        assert_protocol_compatible(
            node=node,
            generation_schema_version=3,
            protocol_version=2,
        )
