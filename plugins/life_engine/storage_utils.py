"""Durable filesystem helpers shared by life_engine state stores."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Write text through a unique sibling file and atomically replace *path*.

    A unique temporary name prevents concurrent writers from stealing one
    another's staging file. Flushing the file before ``os.replace`` also keeps
    a completed write from exposing partially buffered JSON.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some platforms/filesystems do not support fsync on directories.
            pass
    finally:
        temp_path.unlink(missing_ok=True)
