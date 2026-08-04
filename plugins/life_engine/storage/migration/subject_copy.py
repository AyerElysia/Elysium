"""Exact-byte snapshot copy for explicitly declared subject documents."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..subject_adapters import normalize_subject_path
from ..subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentCommit,
    SubjectDocumentConflict,
    SubjectDocumentStorePort,
)
from .copy_authority import CopyAuthorityToken, MySQLCopyAuthorityRegistry
from .manifest import LifeSnapshotError, load_snapshot_manifest
from .snapshot import sha256_file

_SUBJECT_ROOT_DOCUMENTS = {
    "life_engine_workspace/MEMORY.md",
    "life_engine_workspace/SOUL.md",
    "life_engine_workspace/USER.md",
}
_SUBJECT_PREFIXES = ("diaries/", "life_engine_workspace/diaries/")


class SubjectDocumentCopyError(RuntimeError):
    """Raised when subject snapshot evidence and target history diverge."""


@dataclass(frozen=True, slots=True)
class SubjectDocumentCopyReport:
    """Secret-free exact-byte equivalence evidence for one candidate copy."""

    snapshot_directory: str
    document_count: int
    total_bytes: int
    copied_count: int
    source_root_sha256: str
    target_root_sha256: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_directory": self.snapshot_directory,
            "document_count": self.document_count,
            "total_bytes": self.total_bytes,
            "copied_count": self.copied_count,
            "source_root_sha256": self.source_root_sha256,
            "target_root_sha256": self.target_root_sha256,
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class _SnapshotDocument:
    logical_path: str
    backup_path: Path
    content_hash: str
    byte_length: int
    source_stat: dict[str, Any]


def _is_declared_subject_path(path: str) -> bool:
    return path in _SUBJECT_ROOT_DOCUMENTS or path.startswith(_SUBJECT_PREFIXES)


def _safe_snapshot_path(snapshot_root: Path, relative: str) -> Path:
    candidate = (snapshot_root / relative).resolve()
    try:
        candidate.relative_to(snapshot_root)
    except ValueError as exc:
        raise SubjectDocumentCopyError(
            "subject snapshot path escapes snapshot root"
        ) from exc
    return candidate


def _load_subject_documents(
    snapshot_root: Path,
) -> tuple[dict[str, Any], list[_SnapshotDocument]]:
    try:
        manifest = load_snapshot_manifest(snapshot_root / "manifest.json")
    except LifeSnapshotError as exc:
        raise SubjectDocumentCopyError(str(exc)) from exc
    if (snapshot_root / "SNAPSHOT_INCOMPLETE").exists():
        raise SubjectDocumentCopyError("subject snapshot is marked incomplete")
    selected: list[_SnapshotDocument] = []
    seen: set[str] = set()
    for raw in list(manifest.get("exact_files") or []):
        if not isinstance(raw, dict):
            raise SubjectDocumentCopyError("snapshot exact file entry is invalid")
        source_relative = normalize_subject_path(str(raw.get("source_relative") or ""))
        if not _is_declared_subject_path(source_relative):
            continue
        if source_relative in seen:
            raise SubjectDocumentCopyError(
                f"duplicate declared subject path: {source_relative}"
            )
        seen.add(source_relative)
        backup = _safe_snapshot_path(
            snapshot_root,
            str(raw.get("backup_relative") or ""),
        )
        expected_hash = str(raw.get("sha256") or "")
        expected_bytes = int(raw.get("bytes") or 0)
        if len(expected_hash) != 64 or not backup.is_file():
            raise SubjectDocumentCopyError(
                f"declared subject snapshot entry is incomplete: {source_relative}"
            )
        if (
            backup.stat().st_size != expected_bytes
            or sha256_file(backup) != expected_hash
        ):
            raise SubjectDocumentCopyError(
                f"declared subject snapshot checksum mismatch: {source_relative}"
            )
        source_stat = raw.get("source_stat") or {}
        if not isinstance(source_stat, dict):
            raise SubjectDocumentCopyError("subject source_stat must be an object")
        selected.append(
            _SnapshotDocument(
                logical_path=source_relative,
                backup_path=backup,
                content_hash=expected_hash,
                byte_length=expected_bytes,
                source_stat=dict(source_stat),
            )
        )
    selected.sort(key=lambda item: item.logical_path)
    if not selected:
        raise SubjectDocumentCopyError(
            "snapshot contains no declared subject documents"
        )
    return manifest, selected


def _encoding_and_newlines(content: bytes) -> tuple[str | None, str | None]:
    encoding: str | None = None
    try:
        if content.startswith(b"\xef\xbb\xbf"):
            content.decode("utf-8-sig")
            encoding = "utf-8-sig"
        else:
            content.decode("utf-8")
            encoding = "utf-8"
    except UnicodeDecodeError:
        pass
    crlf = content.count(b"\r\n")
    lone_lf = content.count(b"\n") - crlf
    lone_cr = content.count(b"\r") - crlf
    styles = [
        name
        for name, count in (("crlf", crlf), ("lf", lone_lf), ("cr", lone_cr))
        if count
    ]
    newline_style = styles[0] if len(styles) == 1 else ("mixed" if styles else None)
    return encoding, newline_style


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


def _occurrence_id(manifest_hash: str, item: _SnapshotDocument) -> str:
    digest = hashlib.sha256()
    digest.update(manifest_hash.encode())
    digest.update(b"\0")
    digest.update(item.logical_path.encode())
    digest.update(b"\0")
    digest.update(item.content_hash.encode())
    return f"migration:subject:{digest.hexdigest()}"


async def _all_current_versions(
    store: SubjectDocumentStorePort,
) -> list[SubjectDocumentCommit]:
    commits: list[SubjectDocumentCommit] = []
    cursor = ""
    while True:
        batch = await store.list_current_versions(
            after_logical_path=cursor,
            limit=500,
        )
        if not batch:
            return commits
        if any(commit.head.logical_path <= cursor for commit in batch):
            raise SubjectDocumentCopyError(
                "target returned non-monotonic subject heads"
            )
        commits.extend(batch)
        cursor = batch[-1].head.logical_path


async def copy_subject_documents_from_snapshot(
    snapshot_directory: str | Path,
    target: SubjectDocumentStorePort,
    *,
    copy_registry: MySQLCopyAuthorityRegistry,
    token: CopyAuthorityToken,
    progress_interval: int = 50,
) -> SubjectDocumentCopyReport:
    """Copy declared subject files and prove exact bytes/path parity."""

    if int(progress_interval) <= 0:
        raise ValueError("progress_interval must be positive")
    snapshot_root = Path(snapshot_directory).resolve()
    manifest, documents = _load_subject_documents(snapshot_root)
    manifest_hash = str(manifest["manifest_sha256"])
    source_root = hashlib.sha256()
    copied_count = 0
    total_bytes = 0
    for item in documents:
        content = item.backup_path.read_bytes()
        encoding, newline_style = _encoding_and_newlines(content)
        occurrence_id = _occurrence_id(manifest_hash, item)
        try:
            committed = await target.append_version(
                AppendSubjectDocumentVersion(
                    logical_path=item.logical_path,
                    expected_revision=0,
                    expected_head_version_id="",
                    content_bytes=content,
                    occurrence_id=occurrence_id,
                    recorded_by="storage-migration",
                    recorded_source=f"snapshot:{manifest_hash[:16]}",
                    declared_owner="elysia",
                    semantic_actor_id=None,
                    semantic_source_id=None,
                    occurred_at=None,
                    provenance_status="semantic_source_missing",
                    byte_fidelity="exact_bytes",
                    encoding=encoding,
                    newline_style=newline_style,
                    change_context={
                        "migration_observation": True,
                        "source_relative": item.logical_path,
                        "source_snapshot_sha256": str(
                            manifest["source_snapshot_sha256"]
                        ),
                        "snapshot_manifest_sha256": manifest_hash,
                        "source_stat": item.source_stat,
                    },
                )
            )
        except SubjectDocumentConflict as exc:
            head = await target.get_head(item.logical_path)
            actual_hash = ""
            if head is not None and head.current_version_id:
                actual_hash = (
                    await target.get_version(head.current_version_id)
                ).content_hash
            await copy_registry.record_conflict(
                token,
                domain_name="subject_document",
                source_identity=item.logical_path,
                expected_hash=item.content_hash,
                actual_hash=actual_hash,
                detail=str(exc),
            )
            raise SubjectDocumentCopyError(
                f"subject target conflict: {item.logical_path}"
            ) from exc
        version = committed.version
        if (
            version.logical_path != item.logical_path
            or version.content_bytes != content
            or version.content_hash != item.content_hash
            or version.byte_length != item.byte_length
            or version.occurrence_id != occurrence_id
            or version.byte_fidelity != "exact_bytes"
            or committed.head.current_version_id != version.version_id
            or committed.head.revision != 1
        ):
            await copy_registry.record_conflict(
                token,
                domain_name="subject_document",
                source_identity=item.logical_path,
                expected_hash=item.content_hash,
                actual_hash=version.content_hash,
                detail="target version/head verification mismatch",
            )
            raise SubjectDocumentCopyError(
                f"subject target mismatch: {item.logical_path}"
            )
        _root_update(
            source_root,
            logical_path=item.logical_path,
            content_hash=item.content_hash,
            byte_length=item.byte_length,
        )
        total_bytes += item.byte_length
        copied_count += 1
        if copied_count % int(progress_interval) == 0:
            await copy_registry.set_progress(token, copied_records=copied_count)
    await copy_registry.set_progress(token, copied_records=copied_count)

    target_commits = await _all_current_versions(target)
    if len(target_commits) != len(documents):
        raise SubjectDocumentCopyError(
            "subject target document count contains missing or extra heads"
        )
    expected_by_path = {item.logical_path: item for item in documents}
    target_root = hashlib.sha256()
    for committed in target_commits:
        head = committed.head
        expected = expected_by_path.get(head.logical_path)
        if expected is None or head.revision != 1 or not head.current_version_id:
            raise SubjectDocumentCopyError(
                f"unexpected subject target head: {head.logical_path}"
            )
        version = committed.version
        if (
            version.content_hash != expected.content_hash
            or version.byte_length != expected.byte_length
            or version.content_bytes != expected.backup_path.read_bytes()
        ):
            raise SubjectDocumentCopyError(
                f"subject final verification mismatch: {head.logical_path}"
            )
        _root_update(
            target_root,
            logical_path=head.logical_path,
            content_hash=version.content_hash,
            byte_length=version.byte_length,
        )
    source_root_sha256 = source_root.hexdigest()
    target_root_sha256 = target_root.hexdigest()
    if source_root_sha256 != target_root_sha256:
        raise SubjectDocumentCopyError("subject aggregate root mismatch")
    return SubjectDocumentCopyReport(
        snapshot_directory=str(snapshot_root),
        document_count=len(documents),
        total_bytes=total_bytes,
        copied_count=copied_count,
        source_root_sha256=source_root_sha256,
        target_root_sha256=target_root_sha256,
        verified=True,
    )


__all__ = [
    "SubjectDocumentCopyError",
    "SubjectDocumentCopyReport",
    "copy_subject_documents_from_snapshot",
]
