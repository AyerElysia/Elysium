"""Append-only JSONL trajectory collector for post-training data."""

from __future__ import annotations

import atexit
import gzip
import json
import logging
import os
import shutil
import threading
from datetime import date, datetime, timedelta, timezone
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
    "title": "Elysium text-only trajectory",
    "type": "object",
    "schema_version": TRAJECTORY_SCHEMA_VERSION,
    "format": "jsonl",
    "additionalProperties": True,
    "fields": sorted(TRAJECTORY_FIELDS),
}
_README = """# Training Data Lake\n\nThis directory contains append-only, text-only LLM trajectory records.\n\n- `raw/YYYY-MM-DD.jsonl`: recent UTC-date partitioned JSONL events.\n- `processed/`: reserved for later normalization.\n- `export/`: reserved for training exports.\n- `archive/*.jsonl.gz`: compressed historical UTC-date partitions.\n- `.schema/trajectory.v1.json`: schema metadata for the raw records.\n\nMedia bytes, data URLs, local paths, and media source URLs are redacted as\n`[removed]`; media messages may retain only a type placeholder and non-content\nmetadata. Each raw line is an independent JSON document.\n"""


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

    def stats(self) -> dict[str, int]:
        return {
            "queue_size": 0,
            "dropped_count": 0,
            "flush_failure_count": 0,
            "archived_partition_count": 0,
            "pruned_partition_count": 0,
        }


class TrajectoryCollector:
    """Thread-safe append-only JSONL collector.

    Records are sanitized before entering the in-memory queue. A flush swaps out
    one batch under the queue lock, then performs file I/O without blocking
    producers. In-flight records still count toward the hard queue limit. If a
    write fails, the file append is rolled back when possible and the batch is
    restored ahead of records that arrived during the failed flush.
    """

    enabled = True

    def __init__(
        self,
        base_path: str | Path | None = None,
        *,
        flush_interval: float = 5.0,
        queue_limit: int = 10000,
        raw_retention_days: int = 3,
        archive_retention_days: int = 0,
    ) -> None:
        self.base_path = _resolve_base_path(base_path)
        self.raw_path = self.base_path / "raw"
        self.processed_path = self.base_path / "processed"
        self.export_path = self.base_path / "export"
        self.archive_path = self.base_path / "archive"
        self.schema_path = self.base_path / _SCHEMA_DIR_NAME

        self._lock = threading.RLock()
        self._flush_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._queue: list[TrajectoryRecord] = []
        self._inflight_count = 0
        self._flush_interval = max(0.01, float(flush_interval))
        self._queue_limit = max(1, int(queue_limit))
        self._raw_retention_days = max(1, int(raw_retention_days))
        self._archive_retention_days = max(0, int(archive_retention_days))
        self._stop_event = threading.Event()
        self._shutdown = False
        self._dropped_count = 0
        self._flush_failure_count = 0
        self._archived_partition_count = 0
        self._pruned_partition_count = 0
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
            return len(self._queue) + self._inflight_count

    def stats(self) -> dict[str, int]:
        """Return bounded-queue health counters for monitoring."""
        with self._lock:
            return {
                "queue_size": len(self._queue) + self._inflight_count,
                "dropped_count": self._dropped_count,
                "flush_failure_count": self._flush_failure_count,
                "archived_partition_count": self._archived_partition_count,
                "pruned_partition_count": self._pruned_partition_count,
            }

    def record(self, record: dict[str, Any] | TrajectoryRecord) -> None:
        """Sanitize and enqueue one trajectory record."""
        if self._shutdown:
            logger.warning("trajectory collector 已关闭，忽略新记录")
            return
        sanitized = ensure_trajectory_record(record)
        with self._lock:
            if self._shutdown:
                logger.warning("trajectory collector 已关闭，忽略新记录")
                return
            queued_total = len(self._queue) + self._inflight_count
            if queued_total >= self._queue_limit:
                self._dropped_count += 1
                # Avoid a second log storm while the disk is already unhealthy.
                if self._dropped_count == 1 or self._dropped_count % 100 == 0:
                    logger.error(
                        "trajectory queue 已满 (%s)，累计丢弃 %s 条新记录",
                        self._queue_limit,
                        self._dropped_count,
                    )
                return
            self._queue.append(sanitized)

    def append(self, record: dict[str, Any] | TrajectoryRecord) -> None:
        """Alias for :meth:`record` for append-only collector callers."""
        self.record(record)

    def _path_for_date(self, current_date: str | None = None) -> Path:
        date_text = current_date or datetime.now(timezone.utc).date().isoformat()
        return self.raw_path / f"{date_text}.jsonl"

    def _flush_once(self) -> bool:
        """Persist one detached batch while allowing producers to continue."""
        with self._lock:
            if not self._queue:
                return True
            batch = self._queue
            self._queue = []
            self._inflight_count = len(batch)

        raw_file = self._path_for_date()
        start_offset: int | None = None
        file_handle = None
        try:
            raw_file.parent.mkdir(parents=True, exist_ok=True)
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
            with self._lock:
                self._queue[0:0] = batch
                self._inflight_count = 0
                self._flush_failure_count += 1
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

        with self._lock:
            self._inflight_count = 0
            self._last_raw_path = raw_file
        return True

    def flush(self) -> bool:
        """Synchronously persist all currently queued records."""
        with self._flush_lock:
            return self._flush_once()

    def rotate(self) -> None:
        """Finish the current batch; the next flush resolves the current UTC partition."""
        self.flush()
        with self._lock:
            self._last_raw_path = None

    @staticmethod
    def _partition_date(path: Path) -> date | None:
        """Parse a UTC date from a raw or gzip partition filename."""
        date_text = path.name.split(".jsonl", 1)[0]
        try:
            return date.fromisoformat(date_text)
        except ValueError:
            return None

    def _run_partition_maintenance(self) -> None:
        """Compress old raw partitions and prune expired compressed archives."""
        # Archiving and flushing touch the same raw partitions.  A maintenance
        # pass must never copy/unlink a file while an append is still being
        # flushed, otherwise the gzip can become a valid but incomplete
        # snapshot of the partition.
        with self._flush_lock:
            with self._maintenance_lock:
                self._run_partition_maintenance_locked()

    def _run_partition_maintenance_locked(self) -> None:
        """Run partition maintenance while the maintenance lock is held."""
        today = datetime.now(timezone.utc).date()
        archive_cutoff = today - timedelta(days=self._raw_retention_days)
        prune_cutoff = (
            today - timedelta(days=self._archive_retention_days)
            if self._archive_retention_days > 0
            else None
        )

        for raw_file in sorted(self.raw_path.glob("*.jsonl")):
            partition_date = self._partition_date(raw_file)
            if partition_date is None or partition_date >= archive_cutoff:
                continue

            archive_file = self.archive_path / f"{raw_file.name}.gz"
            if archive_file.exists():
                logger.warning(
                    "trajectory 归档已存在，保留原始分区避免覆盖: %s",
                    archive_file,
                )
                continue
            temp_file = archive_file.with_name(
                f".{archive_file.name}.{os.getpid()}.tmp"
            )
            try:
                with raw_file.open("rb") as source, gzip.open(
                    temp_file, "wb", compresslevel=6
                ) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                os.replace(temp_file, archive_file)
                raw_file.unlink()
                with self._lock:
                    self._archived_partition_count += 1
            except OSError as exc:
                logger.warning("trajectory 分区归档失败 %s: %s", raw_file, exc)
                try:
                    temp_file.unlink(missing_ok=True)
                except OSError:
                    pass

        for archive_file in sorted(self.archive_path.glob("*.jsonl.gz")):
            partition_date = self._partition_date(archive_file)
            if (
                prune_cutoff is None
                or partition_date is None
                or partition_date >= prune_cutoff
            ):
                continue
            try:
                archive_file.unlink()
                with self._lock:
                    self._pruned_partition_count += 1
            except OSError as exc:
                logger.warning("trajectory 过期归档清理失败 %s: %s", archive_file, exc)

    def _worker_loop(self) -> None:
        self._run_partition_maintenance()
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
    raw_retention_days: int = 3,
    archive_retention_days: int = 0,
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
                raw_retention_days=raw_retention_days,
                archive_retention_days=archive_retention_days,
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
    raw_retention_days: int = 3,
    archive_retention_days: int = 0,
) -> None:
    """Record one event without allowing collector errors to escape."""
    try:
        target = collector or get_global_trajectory_collector(
            base_path=base_path,
            enabled=enabled,
            flush_interval=flush_interval,
            queue_limit=queue_limit,
            raw_retention_days=raw_retention_days,
            archive_retention_days=archive_retention_days,
        )
        target.record(record)
    except Exception as exc:
        logger.warning("trajectory record 失败: %s", exc)


__all__ = [
    "TrajectoryCollector",
    "get_global_trajectory_collector",
    "record_trajectory",
]
