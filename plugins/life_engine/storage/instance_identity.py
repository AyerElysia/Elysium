"""Structured deployment/instance/boot identity for multi-writer nodes.

Specification section 6 (instance identity and compatibility): every process
must carry a stable deployment id, a globally unique instance id, a boot id
that changes on every start, an authority owner id, a protocol version, the
database generation, a config digest and a workspace revision.

The contract deliberately keeps identity *content-free*: no secrets, no
message bodies and no first-person subject files are ever stored or logged
through this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from src.kernel.storage import canonical_json

_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,126}$")


class InstanceIdentityError(ValueError):
    """Raised when a node identity violates the compatibility contract."""


def generate_boot_id() -> str:
    """Return a fresh boot identity; every process start must differ."""
    return f"boot-{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class InstanceIdentity:
    """Immutable identity of one Elysium process joining a generation.

    Attributes:
        deployment_id: stable deployment node identity (same machine/service).
        instance_id: globally unique process instance identity; must be unique
            among all live instances at any moment.
        boot_id: changes on every start; used to fence stale claims.
        owner_id: authority audit source (e.g. ``elysium-windows-primary``).
        protocol_version: multi-writer protocol version this node speaks.
        schema_generation: database generation this node is allowed to join.
        config_digest: sha256 of canonicalized critical configuration.
        workspace_revision: workspace content revision managed by deployment.
    """

    deployment_id: str
    instance_id: str
    boot_id: str
    owner_id: str
    protocol_version: int
    schema_generation: str
    config_digest: str
    workspace_revision: str

    def validate(self) -> None:
        for field, value in (
            ("deployment_id", self.deployment_id),
            ("instance_id", self.instance_id),
            ("boot_id", self.boot_id),
            ("owner_id", self.owner_id),
            ("schema_generation", self.schema_generation),
            ("config_digest", self.config_digest),
            ("workspace_revision", self.workspace_revision),
        ):
            if not _OWNER_PATTERN.fullmatch(str(value or "")):
                raise InstanceIdentityError(
                    f"{field} must match {_OWNER_PATTERN.pattern!r}"
                )
        if int(self.protocol_version) <= 0:
            raise InstanceIdentityError("protocol_version must be positive")

    @property
    def claim_owner(self) -> str:
        """Stable, audit-safe owner key used by claims and operation receipts.

        Includes the boot id so a crashed process's claim is recognizable as
        stale without any secret material.
        """
        return f"{self.deployment_id}:{self.instance_id}:{self.boot_id}"

    @property
    def short_owner(self) -> str:
        """Content-free owner label for health output and logs."""
        return f"{self.deployment_id}:{self.instance_id}"


def compute_config_digest(entries: Mapping[str, Any]) -> str:
    """Return a deterministic sha256 digest of critical configuration.

    The digest is content-free: values are canonicalized, and secrets must be
    excluded by the caller before calling this function.
    """
    payload = {str(key): value for key, value in dict(entries).items()}
    return hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()


def json_identity_digest(identity: InstanceIdentity) -> str:
    """Return a digest that changes when any identity field changes."""
    identity.validate()
    return hashlib.sha256(
        canonical_json(
            {
                "deployment_id": identity.deployment_id,
                "instance_id": identity.instance_id,
                "boot_id": identity.boot_id,
                "owner_id": identity.owner_id,
                "protocol_version": identity.protocol_version,
                "schema_generation": identity.schema_generation,
                "config_digest": identity.config_digest,
                "workspace_revision": identity.workspace_revision,
            }
        ).encode("utf-8")
    ).hexdigest()


def assert_protocol_compatible(
    *,
    node: InstanceIdentity,
    generation_schema_version: int,
    protocol_version: int,
) -> None:
    """Fail closed unless the node may join the generation protocol."""
    node.validate()
    if int(generation_schema_version) <= 0:
        raise InstanceIdentityError("generation schema version must be positive")
    if int(node.protocol_version) != int(protocol_version):
        raise InstanceIdentityError(
            "node protocol version is incompatible with generation: "
            f"node={node.protocol_version}:generation={protocol_version}"
        )


__all__ = [
    "InstanceIdentity",
    "InstanceIdentityError",
    "assert_protocol_compatible",
    "compute_config_digest",
    "generate_boot_id",
    "json_identity_digest",
]
