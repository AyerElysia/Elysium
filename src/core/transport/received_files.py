"""Bounded, content-addressed storage for files received from adapters.

Adapters may need to materialize a platform resource before the unified message
pipeline can expose it as an attachment capability. This module stores those
bytes outside prompt payloads and returns a content-free local reference.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_RECEIVED_FILE_BYTES = 200 * 1024 * 1024
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._()\-\u4e00-\u9fff]+")


@dataclass(frozen=True, slots=True)
class ReceivedFileReference:
    """A local, content-addressed reference without the file body."""

    path: Path
    filename: str
    size_bytes: int
    sha256: str
    storage_key: str


def _bounded_filename(filename: str) -> str:
    candidate = Path(str(filename or "")).name.strip().replace("\x00", "")
    candidate = _SAFE_COMPONENT_RE.sub("_", candidate).strip(" .") or "file"
    suffix = Path(candidate).suffix[:32]
    stem = Path(candidate).stem or "file"
    max_stem_bytes = max(1, 180 - len(suffix.encode("utf-8")))
    while len(stem.encode("utf-8")) > max_stem_bytes:
        stem = stem[:-1]
    return f"{stem or 'file'}{suffix}"


def _bounded_platform(platform: str) -> str:
    candidate = re.sub(r"[^a-z0-9_-]+", "_", str(platform or "").lower())
    return candidate.strip("_")[:48] or "unknown"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _persist_received_file_sync(
    data: bytes,
    *,
    filename: str,
    platform: str,
    root: Path,
    max_bytes: int,
) -> ReceivedFileReference:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if len(data) > max_bytes:
        raise ValueError("received file exceeds configured byte limit")

    digest = hashlib.sha256(data).hexdigest()
    safe_name = _bounded_filename(filename)
    platform_name = _bounded_platform(platform)
    resolved_root = root.resolve()
    platform_dir = resolved_root / platform_name
    platform_dir.mkdir(parents=True, exist_ok=True)
    resolved_platform_dir = platform_dir.resolve(strict=True)
    resolved_platform_dir.relative_to(resolved_root)
    target_dir = resolved_platform_dir / digest[:2]
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_target_dir = target_dir.resolve(strict=True)
    resolved_target_dir.relative_to(resolved_root)
    target = resolved_target_dir / f"{digest}_{safe_name}"

    target_is_valid = (
        target.is_file()
        and target.stat().st_size == len(data)
        and _file_sha256(target) == digest
    )
    if not target_is_valid:
        descriptor = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".incoming-",
            suffix=".tmp",
            dir=target_dir,
            delete=False,
        )
        temp_path = Path(descriptor.name)
        try:
            with descriptor:
                descriptor.write(data)
                descriptor.flush()
                os.fsync(descriptor.fileno())
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)

    resolved = target.resolve(strict=True)
    resolved.relative_to(resolved_root)
    if resolved.stat().st_size != len(data) or _file_sha256(resolved) != digest:
        raise OSError("received file content-addressed target failed verification")
    storage_key = resolved.relative_to(resolved_root).as_posix()
    return ReceivedFileReference(
        path=resolved,
        filename=safe_name,
        size_bytes=len(data),
        sha256=digest,
        storage_key=storage_key,
    )


async def persist_received_file(
    data: bytes | bytearray | memoryview,
    *,
    filename: str,
    platform: str,
    root: Path | None = None,
    max_bytes: int = MAX_RECEIVED_FILE_BYTES,
) -> ReceivedFileReference:
    """Persist verified bytes atomically and return a content-free reference."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("received file data must be bytes-like")
    raw = bytes(data)
    storage_root = root or Path("data/received_files")
    return await asyncio.to_thread(
        _persist_received_file_sync,
        raw,
        filename=filename,
        platform=platform,
        root=storage_root,
        max_bytes=max_bytes,
    )


__all__ = [
    "MAX_RECEIVED_FILE_BYTES",
    "ReceivedFileReference",
    "persist_received_file",
]
