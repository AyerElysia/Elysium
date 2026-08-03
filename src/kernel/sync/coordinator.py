"""Retrying coordinator for local Outbox and remote shared ledger."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .local_store import LocalSyncStore
from .models import PublishResult, SyncEnvelope


class RemoteLedger(Protocol):
    async def initialize(self) -> None: ...

    async def publish(self, envelope: SyncEnvelope) -> PublishResult: ...

    async def fetch_after(
        self,
        remote_position: int,
        *,
        limit: int,
        allowed_visibilities: set[str],
    ) -> list[tuple[int, SyncEnvelope]]: ...

    async def close(self) -> None: ...


ApplyCallback = Callable[[SyncEnvelope], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SyncRunResult:
    pushed: int = 0
    duplicates: int = 0
    pulled: int = 0
    conflicts: int = 0
    failed: int = 0


class SyncCoordinator:
    """At-least-once delivery with durable idempotent application boundaries."""

    def __init__(
        self,
        local: LocalSyncStore,
        remote: RemoteLedger,
        *,
        consumer_id: str = "life_engine.shared_sync",
        allowed_visibilities: set[str] | None = None,
        batch_size: int = 100,
        lease_seconds: float = 60.0,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 300.0,
        apply_callback: ApplyCallback | None = None,
    ) -> None:
        self.local = local
        self.remote = remote
        self.consumer_id = str(consumer_id)
        self.allowed_visibilities = {
            str(item).strip().lower()
            for item in (allowed_visibilities or {"shared"})
            if str(item).strip()
        }
        self.batch_size = max(1, int(batch_size))
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.base_backoff_seconds = max(0.0, float(base_backoff_seconds))
        self.max_backoff_seconds = max(
            self.base_backoff_seconds,
            float(max_backoff_seconds),
        )
        self.apply_callback = apply_callback
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._running = False
        self._last_result = SyncRunResult()
        self._transport_failed = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self.local.ensure_schema)
            await self.remote.initialize()
            self._initialized = True
            await asyncio.to_thread(self.local.update_runtime, success=True)

    def _retry_delay(self, attempt_count: int) -> float:
        exponent = max(0, min(20, int(attempt_count) - 1))
        return min(self.max_backoff_seconds, self.base_backoff_seconds * (2**exponent))

    async def _push(self) -> tuple[int, int, int, int]:
        pushed = duplicates = conflicts = failed = 0
        for _ in range(self.batch_size):
            claimed = await asyncio.to_thread(
                self.local.claim_next,
                lease_seconds=self.lease_seconds,
                allowed_visibilities=self.allowed_visibilities,
            )
            if claimed is None:
                break
            try:
                result = await self.remote.publish(claimed.envelope)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._transport_failed = True
                await asyncio.to_thread(
                    self.local.retry,
                    claimed.envelope.event_id,
                    claimed.lease_token,
                    error=f"{type(exc).__name__}: {exc}",
                    delay_seconds=self._retry_delay(claimed.attempt_count),
                )
                failed += 1
                break
            if result.accepted:
                await asyncio.to_thread(
                    self.local.confirm,
                    claimed.envelope.event_id,
                    claimed.lease_token,
                    result.remote_position,
                )
                if result.status == "duplicate":
                    duplicates += 1
                else:
                    pushed += 1
                continue
            await asyncio.to_thread(
                self.local.conflict,
                claimed.envelope.event_id,
                claimed.lease_token,
                expected_hash=claimed.envelope.payload_hash,
                actual_hash=result.existing_hash,
                detail=result.conflict_reason or "remote immutable identity conflict",
            )
            conflicts += 1
            break
        return pushed, duplicates, conflicts, failed

    async def _apply_staged(self) -> tuple[int, int]:
        if self.apply_callback is None:
            return 0, 0
        pulled = failed = 0
        node_id = await asyncio.to_thread(self.local.node_id)
        rows = await asyncio.to_thread(
            self.local.staged_inbox,
            self.consumer_id,
            limit=self.batch_size,
        )
        for remote_position, envelope in rows:
            try:
                if envelope.origin_node_id != node_id:
                    outcome = self.apply_callback(envelope)
                    if inspect.isawaitable(outcome):
                        await outcome
                await asyncio.to_thread(
                    self.local.mark_inbox_applied,
                    self.consumer_id,
                    remote_position,
                )
                pulled += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await asyncio.to_thread(
                    self.local.record_inbox_error,
                    remote_position,
                    f"{type(exc).__name__}: {exc}",
                )
                failed += 1
                break
        return pulled, failed

    async def _pull(self) -> tuple[int, int, int]:
        if self.apply_callback is None:
            return 0, 0, 0
        pulled, failed = await self._apply_staged()
        if failed or pulled >= self.batch_size:
            return pulled, 0, failed
        cursor = await asyncio.to_thread(self.local.cursor, self.consumer_id)
        try:
            remote_rows = await self.remote.fetch_after(
                cursor,
                limit=self.batch_size - pulled,
                allowed_visibilities=self.allowed_visibilities,
            )
        except Exception:
            self._transport_failed = True
            raise
        conflicts = 0
        for remote_position, envelope in remote_rows:
            staged = await asyncio.to_thread(
                self.local.stage_inbox,
                remote_position,
                envelope,
            )
            if staged == "conflict":
                conflicts += 1
                break
        if conflicts:
            return pulled, conflicts, failed
        applied, apply_failed = await self._apply_staged()
        return pulled + applied, 0, failed + apply_failed

    async def run_once(self, *, push: bool = True, pull: bool = False) -> SyncRunResult:
        try:
            self._transport_failed = False
            await self.initialize()
            pushed = duplicates = pulled = conflicts = failed = 0
            if push:
                pushed, duplicates, push_conflicts, push_failed = await self._push()
                conflicts += push_conflicts
                failed += push_failed
            if pull and failed == 0 and conflicts == 0:
                pulled, pull_conflicts, pull_failed = await self._pull()
                conflicts += pull_conflicts
                failed += pull_failed
            result = SyncRunResult(
                pushed=pushed,
                duplicates=duplicates,
                pulled=pulled,
                conflicts=conflicts,
                failed=failed,
            )
            self._last_result = result
            if failed:
                await asyncio.to_thread(
                    self.local.update_runtime,
                    success=False,
                    error="synchronization delivery failed; retry retained",
                    remote_available=not self._transport_failed,
                )
            elif conflicts:
                await asyncio.to_thread(
                    self.local.update_runtime,
                    success=False,
                    error="synchronization conflict requires resolution",
                    remote_available=True,
                )
            else:
                await asyncio.to_thread(self.local.update_runtime, success=True)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.local.update_runtime,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                remote_available=not self._transport_failed and self._initialized,
            )
            raise

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        poll_interval_seconds: float = 1.0,
        push: bool = True,
        pull: bool = False,
    ) -> None:
        self._running = True
        retry_delay = self.base_backoff_seconds or 1.0
        try:
            while not stop_event.is_set():
                try:
                    await self.run_once(push=push, pull=pull)
                    retry_delay = self.base_backoff_seconds or 1.0
                    delay = max(0.05, float(poll_interval_seconds))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - retry loop must survive transport faults
                    delay = min(self.max_backoff_seconds, retry_delay)
                    retry_delay = min(
                        self.max_backoff_seconds, max(1.0, retry_delay * 2)
                    )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            self._running = False

    async def close(self) -> None:
        await self.remote.close()

    def health_snapshot(self) -> dict[str, Any]:
        snapshot = self.local.health_snapshot()
        snapshot.update(
            {
                "status": (
                    "degraded"
                    if snapshot["degraded_reason"]
                    else "healthy"
                    if snapshot["remote_available"]
                    else "starting"
                ),
                "running": self._running,
                "initialized": self._initialized,
                "last_run": {
                    "pushed": self._last_result.pushed,
                    "duplicates": self._last_result.duplicates,
                    "pulled": self._last_result.pulled,
                    "conflicts": self._last_result.conflicts,
                    "failed": self._last_result.failed,
                },
            }
        )
        return snapshot
