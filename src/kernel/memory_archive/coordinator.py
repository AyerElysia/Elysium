"""Finite and recurring coordinators for the unified memory archive."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .models import ArchiveRecord, ArchiveRunSummary
from .mysql_store import RemoteMemoryArchive
from .state import ArchiveState


class ArchiveVerificationError(RuntimeError):
    """Remote manifest links do not reproduce the local record-id root."""


RecordFactory = Callable[[], Iterator[ArchiveRecord]]
ProgressCallback = Callable[[dict[str, int]], None]


def _take(iterator: Iterator[ArchiveRecord], size: int) -> list[ArchiveRecord]:
    return list(itertools.islice(iterator, max(1, int(size))))


def _bounded_batches(
    records: Sequence[ArchiveRecord],
    *,
    max_count: int,
    max_payload_bytes: int,
) -> Iterator[list[ArchiveRecord]]:
    batch: list[ArchiveRecord] = []
    size = 0
    for record in records:
        record_size = len(record.payload_json.encode("utf-8")) + 2048
        if batch and (
            len(batch) >= max_count or size + record_size > max_payload_bytes
        ):
            yield batch
            batch = []
            size = 0
        batch.append(record)
        size += record_size
    if batch:
        yield batch


class MemoryArchiveCoordinator:
    """Publish exact records while retaining local work across network faults."""

    def __init__(
        self,
        state: ArchiveState,
        remote: RemoteMemoryArchive,
        *,
        publish_batch_size: int = 250,
        scan_batch_size: int = 500,
        max_batch_bytes: int = 4 * 1024 * 1024,
        publish_concurrency: int = 1,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.state = state
        self.remote = remote
        self.publish_batch_size = max(1, int(publish_batch_size))
        self.scan_batch_size = max(1, int(scan_batch_size))
        self.max_batch_bytes = max(64 * 1024, int(max_batch_bytes))
        self.publish_concurrency = min(8, max(1, int(publish_concurrency)))
        self.progress_callback = progress_callback
        self._running = False
        self._last_summary: ArchiveRunSummary | None = None
        self._last_error = ""

    async def synchronize(
        self,
        records_factory: RecordFactory,
        *,
        full_snapshot: bool,
    ) -> ArchiveRunSummary:
        """Run one replay-safe pass; full snapshots relink every observed record."""

        source_node_id = await asyncio.to_thread(self.state.node_id)
        manifest_id = str(uuid.uuid4())
        run_mode = "full_snapshot" if full_snapshot else "incremental"
        source_counts: Counter[str] = Counter()
        accepted = duplicates = conflicts = selected_count = 0
        ordinal = 0
        root = hashlib.sha256()
        iterator: Iterator[ArchiveRecord] | None = None
        scan_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="elysium-memory-archive-scan",
        )
        loop = asyncio.get_running_loop()
        run_started = False
        run_finalized = False
        self._last_error = ""
        try:
            await self.remote.initialize()
            await self.remote.start_run(
                manifest_id,
                source_node_id,
                run_mode=run_mode,
            )
            run_started = True
            iterator = await loop.run_in_executor(scan_executor, records_factory)
            while True:
                observed = await loop.run_in_executor(
                    scan_executor,
                    _take,
                    iterator,
                    self.scan_batch_size,
                )
                if not observed:
                    break
                for record in observed:
                    if record.source_node_id != source_node_id:
                        raise ValueError(
                            "archive record source node does not match local state"
                        )
                    source_counts[f"observed:{record.source_domain}"] += 1
                    source_counts[f"role:{record.archive_role}"] += 1
                if full_snapshot:
                    selected = observed
                else:
                    known_ids = await asyncio.to_thread(
                        self.state.exact_known_ids,
                        observed,
                    )
                    selected = [
                        record
                        for record in observed
                        if record.record_id not in known_ids
                    ]
                for record in selected:
                    source_counts[f"selected:{record.source_domain}"] += 1
                batches = _bounded_batches(
                    selected,
                    max_count=self.publish_batch_size,
                    max_payload_bytes=self.max_batch_bytes,
                )
                while True:
                    window = list(itertools.islice(batches, self.publish_concurrency))
                    if not window:
                        break
                    starts: list[int] = []
                    next_ordinal = ordinal
                    for batch in window:
                        starts.append(next_ordinal)
                        next_ordinal += len(batch)
                    published = await asyncio.gather(
                        *(
                            self.remote.publish_batch(
                                batch,
                                manifest_id=manifest_id,
                                starting_ordinal=start,
                                update_projections=not full_snapshot,
                            )
                            for batch, start in zip(window, starts, strict=True)
                        )
                    )
                    for batch, results in zip(window, published, strict=True):
                        record_by_id = {record.record_id: record for record in batch}
                        selected_count += len(batch)
                        accepted += sum(
                            result.status == "accepted" for result in results
                        )
                        duplicates += sum(
                            result.status == "duplicate" for result in results
                        )
                        conflicts += sum(
                            result.status == "conflict" for result in results
                        )
                        for result in results:
                            if not result.accepted:
                                continue
                            root.update(result.record_id.encode("ascii"))
                            root.update(b":")
                            record = record_by_id[result.record_id]
                            root.update(record.payload_hash.encode("ascii"))
                            root.update(b"\n")
                        await asyncio.to_thread(
                            self.state.remember,
                            batch,
                            results,
                        )
                        ordinal += len(batch)
                    if self.progress_callback is not None:
                        self.progress_callback(
                            {
                                "scanned": selected_count,
                                "accepted": accepted,
                                "duplicates": duplicates,
                                "conflicts": conflicts,
                            }
                        )
                    if conflicts:
                        break
                if conflicts:
                    break
            if full_snapshot and conflicts == 0:
                await self.remote.finalize_full_snapshot(manifest_id)
            status = "complete" if conflicts == 0 else "conflict"
            summary = ArchiveRunSummary(
                manifest_id=manifest_id,
                source_node_id=source_node_id,
                scanned=selected_count,
                accepted=accepted,
                duplicates=duplicates,
                conflicts=conflicts,
                root_hash=root.hexdigest(),
                source_counts=dict(sorted(source_counts.items())),
                status=status,
            )
            await self.remote.finish_run(
                manifest_id,
                status=status,
                scanned_count=selected_count,
                accepted_count=accepted,
                duplicate_count=duplicates,
                conflict_count=conflicts,
                source_counts=summary.source_counts,
                root_hash=summary.root_hash,
                error_summary=(
                    "immutable identity conflict requires review" if conflicts else ""
                ),
            )
            run_finalized = True
            if status == "complete":
                verification = await self.remote.verify_run(manifest_id)
                if not verification["verified"]:
                    await self.remote.mark_run_verification_failed(
                        manifest_id,
                        "remote run manifest failed root/count verification",
                    )
                    raise ArchiveVerificationError(
                        "remote run manifest failed root/count verification"
                    )
            await asyncio.to_thread(
                self.state.update_runtime,
                success=status == "complete",
                manifest_id=manifest_id,
                error=("" if status == "complete" else "archive conflict"),
                remote_available=True,
            )
            self._last_summary = summary
            return summary
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            if run_started and not run_finalized:
                try:
                    await self.remote.finish_run(
                        manifest_id,
                        status="failed",
                        scanned_count=selected_count,
                        accepted_count=accepted,
                        duplicate_count=duplicates,
                        conflict_count=conflicts,
                        source_counts=dict(sorted(source_counts.items())),
                        root_hash=root.hexdigest(),
                        error_summary=self._last_error,
                    )
                except Exception as finish_exc:  # noqa: BLE001
                    self._last_error += (
                        "; failed to finalize remote run: "
                        f"{type(finish_exc).__name__}: {finish_exc}"
                    )
            await asyncio.to_thread(
                self.state.update_runtime,
                success=False,
                manifest_id=manifest_id if run_started else "",
                error=self._last_error,
                remote_available=run_started,
            )
            raise
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                await loop.run_in_executor(scan_executor, close)
            await asyncio.to_thread(scan_executor.shutdown, True)

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        records_factory: RecordFactory,
        *,
        interval_seconds: float = 300.0,
        retry_max_seconds: float = 900.0,
    ) -> None:
        self._running = True
        retry_delay = 5.0
        try:
            while not stop_event.is_set():
                try:
                    await self.synchronize(
                        records_factory,
                        full_snapshot=False,
                    )
                    retry_delay = 5.0
                    delay = max(1.0, float(interval_seconds))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - bounded retry retains local work
                    delay = min(max(5.0, float(retry_max_seconds)), retry_delay)
                    retry_delay = min(
                        max(5.0, float(retry_max_seconds)),
                        retry_delay * 2,
                    )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            self._running = False

    def health_snapshot(self) -> dict[str, Any]:
        snapshot = self.state.health()
        snapshot.update(
            {
                "component": "unified_memory_archive",
                "status": (
                    "degraded"
                    if snapshot["last_error"] or self._last_error
                    else "healthy"
                    if snapshot["last_success_at"]
                    else "starting"
                ),
                "running": self._running,
                "degraded_reason": self._last_error or snapshot["last_error"],
                "last_run": (
                    None
                    if self._last_summary is None
                    else {
                        "manifest_id": self._last_summary.manifest_id,
                        "scanned": self._last_summary.scanned,
                        "accepted": self._last_summary.accepted,
                        "duplicates": self._last_summary.duplicates,
                        "conflicts": self._last_summary.conflicts,
                        "status": self._last_summary.status,
                    }
                ),
            }
        )
        return snapshot
