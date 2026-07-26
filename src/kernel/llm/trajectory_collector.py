"""Append-only JSONL trajectory collector for post-training data."""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .trajectory_types import (
    TRAJECTORY_FIELDS,
    TRAJECTORY_SCHEMA_VERSION,
    TrajectoryRecord,
    ensure_trajectory_record,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_BASE_PATH = _PROJECT_ROOT / "data/training_data_lake"
_SCHEMA_DIR_NAME = ".schema"
_SCHEMA_FILE_NAME = "trajectory.v1.json"
_README_FILE_NAME = "README.md"

_SCHEMA_DOCUMENT = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Neo-MoFox text-only trajectory",
    "type": "object",
    "schema_version": TRAJECTORY_SCHEMA_VERSION,
    "format": "jsonl",
    "additionalProperties": True,
    "fields": sorted(TRAJECTORY_FIELDS),
}
_README = """# Training Data Lake\n\nThis directory contains append-only, text-only LLM trajectory records.\n\n- `raw/YYYY-MM-DD.jsonl`: UTC-date partitioned JSONL attempt events.\n- `processed/`: reserved for later normalization.\n- `export/`: reserved for training exports.\n- `archive/`: reserved for archived partitions.\n- `.schema/trajectory.v1.json`: schema metadata for the raw records.\n\nMedia bytes, data URLs, local paths, and media source URLs are redacted as\n`[removed]`; media messages may retain only a type placeholder and non-content\nmetadata. Each raw line is an independent JSON document.\n"""


class _DisabledTrajectoryCollector:
    """No-op collector used when trajectory logging is disabled."""

    enabled = False

    def record(self, record: dict[str, Any] | TrajectoryRecord) -> None:
        return None

    def append(self, record: dict[str, Any] | TrajectoryRecord) -> None:
        return None

    def flush(self) -> None:
        return None

    def rotate(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class TrajectoryCollector:
    """Thread-safe append-only JSONL collector.

    Records are sanitized before entering the in-memory queue. A flush holds the
    queue lock while appending one complete JSON line at a time, then calls
    ``flush`` and ``fsync``. The queue is only removed after all three operations
    succeed; if an operation fails, the append is rolled back when possible and
    the records remain queued for a later retry.
    """

    enabled = True

    def __init__(
        self,
        base_path: str | Path | None = None,
        *,
        flush_interval: float = 5.0,
        queue_limit: int = 10000,
    ) -> None:
        self.base_path = _resolve_base_path(base_path)
        self.raw_path = self.base_path / "raw"
        self.processed_path = self.base_path / "processed"
        self.export_path = self.base_path / "export"
        self.archive_path = self.base_path / "archive"
        self.schema_path = self.base_path / _SCHEMA_DIR_NAME

        self._lock = threading.RLock()
        self._queue: list[TrajectoryRecord] = []
        self._flush_interval = max(0.01, float(flush_interval))
        self._queue_limit = max(1, int(queue_limit))
        self._stop_event = threading.Event()
        self._shutdown = False
        self._worker: threading.Thread | None = None
        self._last_raw_path: Path | None = None

        self._initialize_layout()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="trajectory-jsonl-flush",
            daemon=True,
        )
        self._worker.start()
        atexit.register(self.shutdown)

    def _initialize_layout(self) -> None:
        for directory in (
            self.raw_path,
            self.processed_path,
            self.export_path,
            self.archive_path,
            self.schema_path,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        schema_file = self.schema_path / _SCHEMA_FILE_NAME
        if not schema_file.exists():
            try:
                with schema_file.open("x", encoding="utf-8") as file:
                    json.dump(_SCHEMA_DOCUMENT, file, ensure_ascii=False, indent=2)
                    file.write("\n")
            except FileExistsError:
                pass
            except OSError as exc:
                logger.warning("无法初始化 trajectory schema %s: %s", schema_file, exc)

        readme_file = self.base_path / _README_FILE_NAME
        if not readme_file.exists():
            try:
                with readme_file.open("x", encoding="utf-8") as file:
                    file.write(_README)
            except FileExistsError:
                pass
            except OSError as exc:
                logger.warning("无法初始化 trajectory README %s: %s", readme_file, exc)

        schema_readme = self.schema_path / _README_FILE_NAME
        if not schema_readme.exists():
            try:
                with schema_readme.open("x", encoding="utf-8") as file:
                    file.write("# Trajectory Schemas\n\nSchema metadata for the JSONL lake.\n")
            except FileExistsError:
                pass
            except OSError as exc:
                logger.warning("无法初始化 trajectory schema README %s: %s", schema_readme, exc)

    @property
    def queue_size(self) -> int:
        with self._lock:
            return len(self._queue)

    def record(self, record: dict[str, Any] | TrajectoryRecord) -> None:
        """Sanitize and enqueue one trajectory record."""
        if self._shutdown:
            logger.warning("trajectory collector 已关闭，忽略新记录")
            return
        sanitized = ensure_trajectory_record(record)
        with self._lock:
            self._queue.append(sanitized)
            if len(self._queue) > self._queue_limit:
                # A failed disk must never cause silent data loss. The limit is a
                # pressure signal; the queue remains intact until a flush works.
                logger.warning(
                    "trajectory queue 超出限制 (%s > %s)，等待持久化",
                    len(self._queue),
                    self._queue_limit,
                )

    def append(self, record: dict[str, Any] | TrajectoryRecord) -> None:
        """Alias for :meth:`record` for append-only collector callers."""
        self.record(record)

    def _path_for_date(self, current_date: str | None = None) -> Path:
        date_text = current_date or datetime.now(timezone.utc).date().isoformat()
        return self.raw_path / f"{date_text}.jsonl"

    def _flush_locked(self) -> bool:
        if not self._queue:
            return True

        batch = list(self._queue)
        raw_file = self._path_for_date()
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        start_offset: int | None = None
        file_handle = None
        try:
            file_handle = raw_file.open("a+", encoding="utf-8", newline="\n")
            file_handle.seek(0, os.SEEK_END)
            start_offset = file_handle.tell()
            for record in batch:
                line = json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                file_handle.write(line)
                file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        except Exception as exc:
            logger.warning("trajectory JSONL flush 失败 %s: %s", raw_file, exc)
            if file_handle is not None and start_offset is not None:
                try:
                    file_handle.seek(start_offset)
                    file_handle.truncate()
                    file_handle.flush()
                except Exception as rollback_exc:
                    logger.warning("trajectory JSONL 半行回滚失败 %s: %s", raw_file, rollback_exc)
            return False
        finally:
            if file_handle is not None:
                try:
                    file_handle.close()
                except Exception as close_exc:
                    logger.warning("关闭 trajectory JSONL 文件失败 %s: %s", raw_file, close_exc)

        # The lock is held, so no producer can insert before this batch.
        del self._queue[: len(batch)]
        self._last_raw_path = raw_file
        return True

    def flush(self) -> bool:
        """Synchronously persist all currently queued records."""
        with self._lock:
            return self._flush_locked()

    def rotate(self) -> None:
        """Finish the current batch; the next flush resolves the current UTC partition."""
        self.flush()
        with self._lock:
            self._last_raw_path = None

    def _worker_loop(self) -> None:
        while not self._stop_event.wait(timeout=self._flush_interval):
            self.flush()
        self.flush()

    def shutdown(self) -> None:
        """Stop the worker and make a final best-effort durable flush."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._stop_event.set()
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=max(5.0, self._flush_interval + 1.0))
        self.flush()


_global_collector: TrajectoryCollector | _DisabledTrajectoryCollector | None = None
_global_collector_lock = threading.Lock()


def _resolve_base_path(base_path: str | Path | None) -> Path:
    path = Path(base_path or _DEFAULT_BASE_PATH).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (_PROJECT_ROOT / path).resolve()


def get_global_trajectory_collector(
    base_path: str | Path | None = None,
    enabled: bool = True,
    *,
    flush_interval: float = 5.0,
    queue_limit: int = 10000,
) -> TrajectoryCollector | _DisabledTrajectoryCollector:
    """Return the process-wide trajectory collector.

    Relative paths are resolved against the project root, so changing the
    process working directory cannot split a dataset across multiple locations.
    Configuration arguments apply only when the singleton is first created.
    """
    global _global_collector
    if _global_collector is not None:
        return _global_collector
    with _global_collector_lock:
        if _global_collector is not None:
            return _global_collector
        if not enabled:
            _global_collector = _DisabledTrajectoryCollector()
        else:
            _global_collector = TrajectoryCollector(
                base_path,
                flush_interval=flush_interval,
                queue_limit=queue_limit,
            )
        return _global_collector


def record_trajectory(
    record: dict[str, Any] | TrajectoryRecord,
    *,
    collector: TrajectoryCollector | _DisabledTrajectoryCollector | None = None,
    base_path: str | Path | None = None,
    enabled: bool = True,
    flush_interval: float = 5.0,
    queue_limit: int = 10000,
) -> None:
    """Record one event without allowing collector errors to escape."""
    try:
        target = collector or get_global_trajectory_collector(
            base_path=base_path,
            enabled=enabled,
            flush_interval=flush_interval,
            queue_limit=queue_limit,
        )
        target.record(record)
    except Exception as exc:
        logger.warning("trajectory record 失败: %s", exc)


__all__ = [
    "TrajectoryCollector",
    "get_global_trajectory_collector",
    "record_trajectory",
]
