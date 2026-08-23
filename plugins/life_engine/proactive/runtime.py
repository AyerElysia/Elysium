"""Owned local runtime for the canonical proactive authority.

The optional selectable life-domain backend remains a deployment choice.  A
local deployment must still have the same append-only AttentionThread and
Initiative contracts, so this module opens a small fenced SQLite runtime just
for those records.  It never activates or migrates the other life domains.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncContextManager
from uuid import uuid4

from src.app.runtime.single_instance import AlreadyRunningError, SingleInstanceLock

from ..attention_threads import AttentionThreadService
from ..storage.attention_factory import open_attention_thread_stores
from ..storage.authority import FileAuthorityRegistry
from ..storage.contracts import StorageBackendRuntime
from ..storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from ..storage.initiative_factory import open_initiative_record_store
from ..storage.models import (
    AuthorityToken,
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from ..storage.proactive_decision_guard import (
    reconcile_proactive_decision_guards,
)
from .actor_gate import ProactiveActorDecisionGate
from .authority import ProactiveAuthority
from .backend_binding import (
    ensure_proactive_backend_binding,
    verify_proactive_backend_binding,
)

_GENERATION_ID = "life-proactive-local-v1"
_SCHEMA_VERSION = 1
_CONTRACT_CREATED_AT = "2026-08-23T00:00:00+00:00"
_FENCING_ENV = "ELYSIUM_PROACTIVE_LOCAL_FENCING_TOKEN"


def _contract_digest() -> str:
    material = (
        "life-proactive-local-v1\0"
        "attention-thread-schema-v1\0"
        "runtime-state-schema-v3\0"
        "single-authority-no-legacy-stream-migration"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _generation() -> BackendGeneration:
    digest = _contract_digest()
    return BackendGeneration(
        generation_id=_GENERATION_ID,
        backend=BackendKind.LOCAL,
        schema_version=_SCHEMA_VERSION,
        source_snapshot_sha256=digest,
        root_hashes={"proactive_contract": digest},
        frontiers={"attention_events": 0, "initiative_events": 0},
        created_at=_CONTRACT_CREATED_AT,
        verified_at=_CONTRACT_CREATED_AT,
        status=GenerationStatus.VERIFIED,
        metadata={
            "scope": "proactive_only",
            "legacy_thought_stream_migration": "forbidden",
        },
    )


def _workspace_path(workspace: Path, value: str, *, field_name: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} must not be empty")
    candidate = Path(raw)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (workspace / candidate).resolve()
    )
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must stay inside the Life Engine workspace"
        ) from exc
    return resolved


@dataclass(slots=True)
class LocalProactiveRuntime:
    """One locally fenced proactive authority and its owned resources."""

    runtime: StorageBackendRuntime
    authority: ProactiveAuthority
    process_lock: SingleInstanceLock
    lease_seconds: int
    renew_interval_seconds: int
    actor_decision_guard: Callable[[str], AsyncContextManager[None]]
    binding_workspace: Path
    binding_path: str
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _renew_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _renewal_health: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "healthy",
            "last_success_at": "",
            "error_type": "",
            "consecutive_failures": 0,
        },
        repr=False,
    )
    _health_cache: dict[str, Any] = field(
        default_factory=lambda: {
            "component": "proactive_runtime",
            "status": "initializing",
            "backend": "local",
        },
        repr=False,
    )

    def start_renewal(self) -> None:
        if self._renew_task is not None:
            return
        self._renew_task = asyncio.create_task(
            self._renew_loop(),
            name="life_engine_local_proactive_authority_renewal",
        )

    def renewal_health_snapshot(self) -> dict[str, Any]:
        """Return the latest content-free lease state without doing I/O."""

        return dict(self._renewal_health)

    def cached_health_snapshot(self) -> dict[str, Any]:
        """Return the last bounded probe without doing storage I/O."""

        snapshot = dict(self._health_cache)
        snapshot["authority_renewal"] = dict(self._renewal_health)
        if str(self._renewal_health.get("status") or "failed") == "failed":
            snapshot["status"] = "failed"
        return snapshot

    async def _renew_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=float(self.renew_interval_seconds),
                )
                return
            except TimeoutError:
                pass
            try:
                await self.runtime.renew_authority(
                    lease_seconds=self.lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - authority must fail closed
                self.runtime.invalidate_writer()
                self._renewal_health = {
                    "status": "failed",
                    "last_success_at": self._renewal_health.get(
                        "last_success_at", ""
                    ),
                    "error_type": type(exc).__name__,
                    "consecutive_failures": int(
                        self._renewal_health.get("consecutive_failures", 0) or 0
                    )
                    + 1,
                }
                return
            self._renewal_health = {
                "status": "healthy",
                "last_success_at": datetime.now(UTC).isoformat(),
                "error_type": "",
                "consecutive_failures": 0,
            }
            try:
                await asyncio.wait_for(self.health_snapshot(), timeout=5.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - lease remains authoritative
                cached = self.cached_health_snapshot()
                cached.update(
                    {
                        "status": "degraded",
                        "probe_error_type": type(exc).__name__,
                    }
                )
                self._health_cache = cached

    async def health_snapshot(self) -> dict[str, Any]:
        async def _probe(
            component: str,
            operation: Callable[[], Awaitable[dict[str, Any]]],
        ) -> dict[str, Any]:
            try:
                return await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - health must remain observable
                return {
                    "component": component,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }

        runtime_health, authority_health, binding_health = await asyncio.gather(
            _probe("storage_runtime", self.runtime.health),
            _probe("proactive_authority", self.authority.health_snapshot),
            _probe(
                "proactive_backend_binding",
                lambda: verify_proactive_backend_binding(
                    workspace_path=self.binding_workspace,
                    binding_path=self.binding_path,
                    runtime=self.runtime,
                ),
            ),
        )
        statuses = {
            str(runtime_health.get("status") or "failed"),
            str(authority_health.get("status") or "failed"),
            str(binding_health.get("status") or "failed"),
            str(self._renewal_health.get("status") or "failed"),
        }
        status = "failed" if "failed" in statuses else (
            "degraded" if "degraded" in statuses else "healthy"
        )
        snapshot = {
            "component": "proactive_runtime",
            "status": status,
            "backend": "local",
            "runtime": runtime_health,
            "authority": authority_health,
            "backend_binding": binding_health,
            "authority_renewal": dict(self._renewal_health),
            "probed_at": datetime.now(UTC).isoformat(),
        }
        self._health_cache = snapshot
        return dict(snapshot)

    async def close(self) -> None:
        """Stop renewal, revoke the exact writer, close SQL, and release OS lock."""

        self._stop_event.set()
        errors: list[BaseException] = []
        task = self._renew_task
        self._renew_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:  # noqa: BLE001 - aggregate cleanup
                errors.append(exc)
        try:
            if self.runtime.authority_token is not None:
                await self.runtime.revoke_authority()
        except BaseException as exc:  # noqa: BLE001 - close and unlock regardless
            self.runtime.invalidate_writer()
            errors.append(exc)
        try:
            await self.runtime.close()
        except BaseException as exc:  # noqa: BLE001 - unlock regardless
            errors.append(exc)
        finally:
            await asyncio.to_thread(self.process_lock.release)
        if errors:
            raise BaseExceptionGroup("local proactive runtime close failed", errors)


async def open_local_proactive_runtime(
    *,
    workspace_path: str | Path,
    config: Any,
    validate_active_actor: Callable[[str], Awaitable[bool]],
    actor_decision_guard: (
        Callable[[str], AsyncContextManager[None]] | None
    ) = None,
) -> LocalProactiveRuntime:
    """Open the local canonical authority without activating global storage."""

    workspace = Path(workspace_path).resolve()
    if actor_decision_guard is None:
        actor_decision_gate = ProactiveActorDecisionGate()
        actor_decision_guard = actor_decision_gate.hold
    database_path = _workspace_path(
        workspace,
        str(getattr(config, "local_database_path", "runtime/proactive/proactive.sqlite3")),
        field_name="proactive.local_database_path",
    )
    authority_path = _workspace_path(
        workspace,
        str(getattr(config, "local_authority_state_path", "runtime/proactive/authority.json")),
        field_name="proactive.local_authority_state_path",
    )
    lease_seconds = int(getattr(config, "authority_lease_seconds", 60) or 60)
    renew_interval = int(
        getattr(config, "authority_renew_interval_seconds", 20) or 20
    )
    if lease_seconds < 15:
        raise ValueError("proactive authority lease must be at least 15 seconds")
    if renew_interval <= 0 or renew_interval * 2 >= lease_seconds:
        raise ValueError(
            "proactive authority renew interval must be positive and less than half the lease"
        )

    process_lock = SingleInstanceLock(authority_path.with_suffix(".writer.lock"))
    try:
        await asyncio.to_thread(process_lock.acquire)
    except AlreadyRunningError as exc:
        raise RuntimeError("ProactiveAuthorityAlreadyOwnedByAnotherProcess") from exc

    runtime: StorageBackendRuntime | None = None
    registry: FileAuthorityRegistry | None = None
    token: AuthorityToken | None = None
    try:
        generation = _generation()
        registry = FileAuthorityRegistry(
            authority_path,
            registry_id="life-proactive-local",
        )
        await registry.register_generation(generation)
        health = await registry.health()
        if str(health.get("status") or "") == "failed":
            raise RuntimeError("ProactiveAuthorityRegistryUnavailable")
        owner_id = f"life-proactive:pid-{os.getpid()}:{uuid4().hex[:16]}"
        token = await registry.activate_generation(
            generation.generation_id,
            expected_epoch=int(health.get("authority_epoch") or 0),
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            # The process lock proves no current implementation owns this
            # proactive writer.  A leftover registry lease therefore belongs
            # to a crashed process and can be fenced by a new epoch.
            confirm_previous_writers_stopped=bool(
                str(health.get("active_generation") or "")
            ),
        )
        settings = StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=generation.generation_id,
            schema_version=generation.schema_version,
            registry_id="life-proactive-local",
            authority_provider="file",
            authority_epoch=token.authority_epoch,
            authority_owner_id=owner_id,
            fencing_token_env=_FENCING_ENV,
            require_verified_generation=True,
            authority_lease_seconds=lease_seconds,
            authority_renew_interval_seconds=renew_interval,
            local=LocalBackendSettings(
                database_path=database_path,
                authority_state_path=authority_path,
                busy_timeout_seconds=10,
            ),
        )
        runtime = await open_storage_backend(
            settings,
            environment={_FENCING_ENV: token.fencing_token},
        )
        attention_stores = await open_attention_thread_stores(
            runtime,
            initialize_schema=True,
            validate_active_actor=validate_active_actor,
            actor_decision_guard=actor_decision_guard,
        )
        binding_path = str(
            getattr(
                config,
                "backend_binding_path",
                "runtime/proactive/backend-binding.json",
            )
        )
        await ensure_proactive_backend_binding(
            workspace_path=workspace,
            binding_path=binding_path,
            runtime=runtime,
        )
        initiative = await open_initiative_record_store(
            runtime,
            validate_active_actor=validate_active_actor,
            actor_decision_guard=actor_decision_guard,
        )
        await reconcile_proactive_decision_guards(runtime)
        authority = ProactiveAuthority(
            attention=AttentionThreadService(
                attention_stores.authority,
                attention_stores.focus,
            ),
            initiative=initiative,
        )
        owned = LocalProactiveRuntime(
            runtime=runtime,
            authority=authority,
            process_lock=process_lock,
            lease_seconds=lease_seconds,
            renew_interval_seconds=renew_interval,
            actor_decision_guard=actor_decision_guard,
            binding_workspace=workspace,
            binding_path=binding_path,
        )
        owned.start_renewal()
        return owned
    except BaseException as primary_error:
        cleanup_errors: list[BaseException] = []
        if runtime is not None:
            try:
                if runtime.authority_token is not None:
                    await runtime.revoke_authority()
            except BaseException as exc:  # noqa: BLE001 - close must still run
                runtime.invalidate_writer()
                cleanup_errors.append(exc)
            try:
                await runtime.close()
            except BaseException as exc:  # noqa: BLE001 - preserve primary failure
                cleanup_errors.append(exc)
        elif registry is not None and token is not None:
            try:
                await registry.revoke(token)
            except BaseException as exc:  # noqa: BLE001 - release OS lock regardless
                cleanup_errors.append(exc)
        try:
            await asyncio.to_thread(process_lock.release)
        except BaseException as exc:  # noqa: BLE001 - preserve every cleanup failure
            cleanup_errors.append(exc)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "local proactive runtime initialization failed",
                [primary_error, *cleanup_errors],
            ) from primary_error
        raise


__all__ = ["LocalProactiveRuntime", "open_local_proactive_runtime"]
