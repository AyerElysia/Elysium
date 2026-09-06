"""Discover opportunity capability packages without activating them.

A capability package is engineering metadata, not a cognitive judgement. The
catalog keeps its technical manual and subject-editable workflow template as
separate byte-exact artifacts. Discovery never installs or enables anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CAPABILITY_SCHEMA_VERSION = 1
CAPABILITY_MANIFEST_NAME = "manifest.json"
CAPABILITY_MANUAL_NAME = "CAPABILITY.md"
CAPABILITY_DEFAULT_SKILL_NAME = "DEFAULT_SKILL.md"
CAPABILITY_REMOVABILITY = "subject_removable"

_PACKAGE_FILES = (
    CAPABILITY_MANIFEST_NAME,
    CAPABILITY_MANUAL_NAME,
    CAPABILITY_DEFAULT_SKILL_NAME,
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "capability_id",
        "package_version",
        "removability",
        "provider_kind",
        "manual",
        "default_skill",
        "operations",
        "dependencies",
    }
)
_FORBIDDEN_FIELD_KEYS = frozenset(
    {
        "importance",
        "priority",
        "score",
        "audience",
        "platform",
        "prewrittenaction",
        "prewrittenexpression",
    }
)
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_PROVIDER_KIND_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


class CapabilityCatalogError(ValueError):
    """Base class for invalid or unavailable capability packages."""


class CapabilityPackageConflict(RuntimeError):
    """The same capability id was discovered with different exact bytes."""


class CapabilityNotFoundError(LookupError):
    """A requested capability id is not present in this catalog."""


@dataclass(frozen=True, slots=True)
class CapabilityArtifact:
    """One exact UTF-8 package file and its content identity."""

    file_name: str
    content_bytes: bytes
    sha256: str

    @property
    def utf8_bytes(self) -> int:
        return len(self.content_bytes)

    @property
    def text(self) -> str:
        return self.content_bytes.decode("utf-8")


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """A discovered technical capability; it carries no runtime state."""

    capability_id: str
    package_version: str
    removability: str
    provider_kind: str
    operations: tuple[str, ...]
    dependencies: tuple[str, ...]
    package_root: Path
    manifest_artifact: CapabilityArtifact
    technical_manual: CapabilityArtifact
    default_skill_template: CapabilityArtifact
    package_sha256: str

    @property
    def manifest_sha256(self) -> str:
        return self.manifest_artifact.sha256

    @property
    def manual_sha256(self) -> str:
        return self.technical_manual.sha256

    @property
    def default_skill_sha256(self) -> str:
        return self.default_skill_template.sha256

    def declares_operation(self, operation_id: str) -> bool:
        """Return whether the immutable package manifest declares an operation."""

        return operation_id in self.operations


def _normalise_field_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def _reject_forbidden_fields(value: Any, *, location: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if _normalise_field_key(key) in _FORBIDDEN_FIELD_KEYS:
                raise CapabilityCatalogError(
                    f"{location} contains forbidden field {key!r}"
                )
            _reject_forbidden_fields(child, location=f"{location}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_fields(child, location=f"{location}[{index}]")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise CapabilityCatalogError(
                f"manifest.json contains duplicate field: {key}"
            )
        parsed[key] = value
    return parsed


def _require_exact_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CapabilityCatalogError(f"{field} must be a non-empty exact string")
    return value


def _require_identifier(value: Any, *, field: str) -> str:
    identity = _require_exact_string(value, field=field)
    if not _NAMESPACE_RE.fullmatch(identity):
        raise CapabilityCatalogError(
            f"{field} must be an open lowercase namespace identifier"
        )
    return identity


def _require_identifier_list(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CapabilityCatalogError(f"{field} must be a list of string ids")
    identities = tuple(
        _require_identifier(item, field=f"{field}[{index}]")
        for index, item in enumerate(value)
    )
    if len(set(identities)) != len(identities):
        raise CapabilityCatalogError(f"{field} must not contain duplicate ids")
    return identities


def _read_artifact(package_root: Path, file_name: str) -> CapabilityArtifact:
    path = package_root / file_name
    if path.is_symlink():
        raise CapabilityCatalogError(
            f"capability package file must not be a symlink: {file_name}"
        )
    if not path.is_file():
        raise CapabilityCatalogError(f"capability package is missing {file_name}")
    try:
        content = path.read_bytes()
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapabilityCatalogError(
            f"capability package file is not valid UTF-8: {file_name}"
        ) from exc
    if not content.strip():
        raise CapabilityCatalogError(
            f"capability package file must not be empty: {file_name}"
        )
    return CapabilityArtifact(
        file_name=file_name,
        content_bytes=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _package_digest(artifacts: Iterable[CapabilityArtifact]) -> str:
    digest = hashlib.sha256(b"elysium.opportunity.capability.package.v1\x00")
    for artifact in artifacts:
        digest.update(artifact.file_name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(artifact.utf8_bytes.to_bytes(8, "big"))
        digest.update(bytes.fromhex(artifact.sha256))
    return digest.hexdigest()


def load_capability_package(package_root: Path | str) -> CapabilityDescriptor:
    """Load and validate one package without installing or enabling it."""

    requested_root = Path(package_root)
    if requested_root.is_symlink():
        raise CapabilityCatalogError("capability package root must not be a symlink")
    if not requested_root.is_dir():
        raise CapabilityCatalogError("capability package root is not a directory")
    root = requested_root.resolve()
    manifest_artifact = _read_artifact(root, CAPABILITY_MANIFEST_NAME)
    manual_artifact = _read_artifact(root, CAPABILITY_MANUAL_NAME)
    default_skill_artifact = _read_artifact(root, CAPABILITY_DEFAULT_SKILL_NAME)

    try:
        manifest = json.loads(
            manifest_artifact.text,
            object_pairs_hook=_strict_json_object,
        )
    except json.JSONDecodeError as exc:
        raise CapabilityCatalogError("manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise CapabilityCatalogError("manifest.json must contain a JSON object")
    _reject_forbidden_fields(manifest)
    actual_fields = set(manifest)
    missing = sorted(_MANIFEST_FIELDS - actual_fields)
    unknown = sorted(actual_fields - _MANIFEST_FIELDS)
    if missing:
        raise CapabilityCatalogError(
            "manifest.json is missing fields: " + ", ".join(missing)
        )
    if unknown:
        raise CapabilityCatalogError(
            "manifest.json contains unknown fields: " + ", ".join(unknown)
        )
    if type(manifest["schema_version"]) is not int or (
        manifest["schema_version"] != CAPABILITY_SCHEMA_VERSION
    ):
        raise CapabilityCatalogError(
            f"schema_version must be integer {CAPABILITY_SCHEMA_VERSION}"
        )

    capability_id = _require_identifier(
        manifest["capability_id"], field="capability_id"
    )
    package_version = _require_exact_string(
        manifest["package_version"], field="package_version"
    )
    removability = _require_exact_string(manifest["removability"], field="removability")
    if removability != CAPABILITY_REMOVABILITY:
        raise CapabilityCatalogError(
            "opportunity capabilities must be subject_removable; "
            "foundation packages are not valid cognitive capabilities"
        )
    provider_kind = _require_exact_string(
        manifest["provider_kind"], field="provider_kind"
    )
    if not _PROVIDER_KIND_RE.fullmatch(provider_kind):
        raise CapabilityCatalogError(
            "provider_kind must be an open lowercase snake_case identifier"
        )
    if manifest["manual"] != CAPABILITY_MANUAL_NAME:
        raise CapabilityCatalogError(
            f"manual must be exactly {CAPABILITY_MANUAL_NAME!r}"
        )
    if manifest["default_skill"] != CAPABILITY_DEFAULT_SKILL_NAME:
        raise CapabilityCatalogError(
            f"default_skill must be exactly {CAPABILITY_DEFAULT_SKILL_NAME!r}"
        )
    operations = _require_identifier_list(manifest["operations"], field="operations")
    dependencies = _require_identifier_list(
        manifest["dependencies"], field="dependencies"
    )
    if capability_id in dependencies:
        raise CapabilityCatalogError("a capability must not depend on itself")

    artifacts = (manifest_artifact, manual_artifact, default_skill_artifact)
    return CapabilityDescriptor(
        capability_id=capability_id,
        package_version=package_version,
        removability=removability,
        provider_kind=provider_kind,
        operations=operations,
        dependencies=dependencies,
        package_root=root,
        manifest_artifact=manifest_artifact,
        technical_manual=manual_artifact,
        default_skill_template=default_skill_artifact,
        package_sha256=_package_digest(artifacts),
    )


class CapabilityCatalog:
    """Thread-safe catalog of discovered, inactive capability descriptors."""

    def __init__(self, descriptors: Iterable[CapabilityDescriptor] = ()) -> None:
        self._descriptors: dict[str, CapabilityDescriptor] = {}
        self._lock = threading.RLock()
        for descriptor in descriptors:
            self.add(descriptor)

    def add(self, descriptor: CapabilityDescriptor) -> CapabilityDescriptor:
        """Add a descriptor; exact duplicates are idempotent."""

        with self._lock:
            existing = self._descriptors.get(descriptor.capability_id)
            if existing is None:
                self._descriptors[descriptor.capability_id] = descriptor
                return descriptor
            if existing.package_sha256 != descriptor.package_sha256:
                raise CapabilityPackageConflict(
                    "capability id has conflicting package bytes: "
                    f"{descriptor.capability_id}"
                )
            return existing

    def discover_package(self, package_root: Path | str) -> CapabilityDescriptor:
        """Discover one package and add only its inactive descriptor."""

        return self.add(load_capability_package(package_root))

    def discover(self, root: Path | str) -> tuple[CapabilityDescriptor, ...]:
        """Discover a package root or its immediate package children.

        A child containing any package marker is treated as a package, so a
        partial package fails explicitly instead of disappearing silently.
        """

        base = Path(root)
        if base.is_symlink():
            raise CapabilityCatalogError(
                "capability catalog root must not be a symlink"
            )
        if not base.is_dir():
            raise CapabilityCatalogError("capability catalog root is not a directory")
        if any((base / file_name).exists() for file_name in _PACKAGE_FILES):
            candidates = (base,)
        else:
            candidates = tuple(
                child
                for child in sorted(base.iterdir(), key=lambda item: item.name)
                if child.is_dir()
                and any((child / file_name).exists() for file_name in _PACKAGE_FILES)
            )
        loaded = sorted(
            (load_capability_package(candidate) for candidate in candidates),
            key=lambda descriptor: descriptor.capability_id,
        )
        pending: dict[str, CapabilityDescriptor] = {}
        for descriptor in loaded:
            existing = pending.get(descriptor.capability_id)
            if existing is not None and (
                existing.package_sha256 != descriptor.package_sha256
            ):
                raise CapabilityPackageConflict(
                    "capability id has conflicting package bytes: "
                    f"{descriptor.capability_id}"
                )
            pending[descriptor.capability_id] = existing or descriptor
        with self._lock:
            for capability_id, descriptor in pending.items():
                existing = self._descriptors.get(capability_id)
                if existing is not None and (
                    existing.package_sha256 != descriptor.package_sha256
                ):
                    raise CapabilityPackageConflict(
                        f"capability id has conflicting package bytes: {capability_id}"
                    )
            for capability_id, descriptor in pending.items():
                self._descriptors.setdefault(capability_id, descriptor)
            return tuple(self._descriptors[key] for key in sorted(pending))

    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        with self._lock:
            return self._descriptors.get(str(capability_id or ""))

    def require(self, capability_id: str) -> CapabilityDescriptor:
        descriptor = self.get(capability_id)
        if descriptor is None:
            raise CapabilityNotFoundError(
                f"capability is not present in the catalog: {capability_id}"
            )
        return descriptor

    def list_descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        with self._lock:
            return tuple(self._descriptors[key] for key in sorted(self._descriptors))

    def __len__(self) -> int:
        with self._lock:
            return len(self._descriptors)
