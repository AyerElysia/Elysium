"""Audited exact-byte subject document export to a new directory."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.kernel.storage import canonical_json

from ..subject_adapters import normalize_subject_path
from ..subject_contracts import SubjectDocumentStorePort


class SubjectDocumentExportError(RuntimeError):
    """Raised when subject reverse export equivalence cannot be proven."""


@dataclass(frozen=True, slots=True)
class SubjectDocumentExportReport:
    """Secret-free evidence for one newly-created exact-byte export."""

    destination_directory: str
    document_count: int
    total_bytes: int
    root_sha256: str
    manifest_sha256: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination_directory": self.destination_directory,
            "document_count": self.document_count,
            "total_bytes": self.total_bytes,
            "root_sha256": self.root_sha256,
            "manifest_sha256": self.manifest_sha256,
            "verified": self.verified,
        }


def _root_update(
    digest: Any,
    *,
    logical_path: str,
    content_hash: str,
    byte_length: int,
) -> None:
    digest.update(logical_path.encode())
    digest.update(b"\0")
    digest.update(content_hash.encode())
    digest.update(b"\0")
    digest.update(str(int(byte_length)).encode())
    digest.update(b"\n")


def _safe_export_path(workspace: Path, logical_path: str) -> Path:
    normalized = normalize_subject_path(logical_path)
    candidate = (workspace / normalized).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise SubjectDocumentExportError(
            "subject export path escapes workspace"
        ) from exc
    return candidate


async def export_subject_documents(
    source: SubjectDocumentStorePort,
    destination_directory: str | Path,
) -> SubjectDocumentExportReport:
    """Create and independently verify a new exact-byte subject export."""

    destination = Path(destination_directory).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=False)
    workspace = destination / "workspace"
    workspace.mkdir()
    marker = destination / "EXPORT_INCOMPLETE"
    marker.write_text(
        "Subject document reverse export is incomplete.\n", encoding="utf-8"
    )
    manifest_path = destination / "manifest.json"
    source_root = hashlib.sha256()
    entries: list[dict[str, Any]] = []
    cursor = ""
    total_bytes = 0
    try:
        while True:
            commits = await source.list_current_versions(
                after_logical_path=cursor,
                limit=500,
            )
            if not commits:
                break
            if any(commit.head.logical_path <= cursor for commit in commits):
                raise SubjectDocumentExportError(
                    "source returned non-monotonic subject heads"
                )
            for committed in commits:
                head = committed.head
                if not head.current_version_id:
                    raise SubjectDocumentExportError(
                        f"subject head has no version: {head.logical_path}"
                    )
                version = committed.version
                if (
                    version.document_id != head.document_id
                    or version.logical_path != head.logical_path
                    or hashlib.sha256(version.content_bytes).hexdigest()
                    != version.content_hash
                    or len(version.content_bytes) != version.byte_length
                ):
                    raise SubjectDocumentExportError(
                        f"subject source version mismatch: {head.logical_path}"
                    )
                output = _safe_export_path(workspace, head.logical_path)
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as handle:
                    handle.write(version.content_bytes)
                relative = output.relative_to(destination).as_posix()
                entries.append(
                    {
                        "logical_path": head.logical_path,
                        "output_relative": relative,
                        "document_id": head.document_id,
                        "version_id": version.version_id,
                        "revision": head.revision,
                        "bytes": version.byte_length,
                        "sha256": version.content_hash,
                        "byte_fidelity": version.byte_fidelity,
                        "encoding": version.encoding,
                        "newline_style": version.newline_style,
                    }
                )
                _root_update(
                    source_root,
                    logical_path=head.logical_path,
                    content_hash=version.content_hash,
                    byte_length=version.byte_length,
                )
                total_bytes += version.byte_length
            cursor = commits[-1].head.logical_path

        target_root = hashlib.sha256()
        for entry in entries:
            output = (destination / str(entry["output_relative"])).resolve()
            try:
                output.relative_to(destination)
            except ValueError as exc:
                raise SubjectDocumentExportError(
                    "subject manifest output escapes destination"
                ) from exc
            content = output.read_bytes()
            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash != entry["sha256"] or len(content) != entry["bytes"]:
                raise SubjectDocumentExportError(
                    f"subject exported bytes changed: {entry['logical_path']}"
                )
            _root_update(
                target_root,
                logical_path=str(entry["logical_path"]),
                content_hash=actual_hash,
                byte_length=len(content),
            )
        root_sha256 = source_root.hexdigest()
        if target_root.hexdigest() != root_sha256:
            raise SubjectDocumentExportError("subject export aggregate root mismatch")
        manifest = {
            "format": "elysium-subject-document-export-v1",
            "document_count": len(entries),
            "total_bytes": total_bytes,
            "root_sha256": root_sha256,
            "documents": entries,
            "verified": True,
        }
        manifest_encoded = canonical_json(manifest)
        manifest_sha256 = hashlib.sha256(manifest_encoded.encode()).hexdigest()
        manifest_path.write_text(
            canonical_json({**manifest, "manifest_sha256": manifest_sha256}) + "\n",
            encoding="utf-8",
        )
        marker.unlink()
        return SubjectDocumentExportReport(
            destination_directory=str(destination),
            document_count=len(entries),
            total_bytes=total_bytes,
            root_sha256=root_sha256,
            manifest_sha256=manifest_sha256,
            verified=True,
        )
    except OSError as exc:
        raise SubjectDocumentExportError("subject export filesystem failure") from exc


__all__ = [
    "SubjectDocumentExportError",
    "SubjectDocumentExportReport",
    "export_subject_documents",
]
