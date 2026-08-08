"""Explicit readiness gates and read-only diagnostics for multi-writer storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

MULTI_WRITER_PROTOCOL_VERSION = 1
# True only after every production hot path has left the legacy full-snapshot
# writer:
#
# - inbound message facts + per-message stream turn claim/commit (distributor
#   -> inbound fact hook -> bridge.record_inbound_message -> chatter commit);
# - heartbeat operations (register/claim/commit/mark_failed around each round);
# - outbox send intents + settlement (send-before-intent and receipt after);
# - memory index worker (atomic local claim) + per-node projection progress.
#
# An explicitly enabled node still fails closed unless schema v3, protocol
# match and legacy singleton retirement all hold; this flag only records that
# the production hot paths themselves are wired.
MULTI_WRITER_HOT_PATHS_READY = True

# Legacy exclusive writer for the whole selected runtime-context snapshot.
# Multi-writer mode cannot start while any unreleased claim for this scope exists.
LEGACY_RUNTIME_CONTEXT_NAMESPACE = "life_engine.runtime_context"
LEGACY_RUNTIME_CONTEXT_STATE_KEY = "global"

# Representative table deployed by the multi-writer schema migration.
_MULTI_WRITER_ANCHOR_TABLE = "operations"


class MultiWriterProtocolError(RuntimeError):
    """Raised when a node cannot safely join the multi-writer generation."""


@dataclass(frozen=True, slots=True)
class MultiWriterProtocolConfig:
    protocol_version: int = MULTI_WRITER_PROTOCOL_VERSION
    require_singleton_retired: bool = True
    allow_legacy_global_snapshot_writer: bool = False

    def validate(self) -> None:
        if int(self.protocol_version) != MULTI_WRITER_PROTOCOL_VERSION:
            raise MultiWriterProtocolError(
                f"unsupported multi-writer protocol version: {self.protocol_version}"
            )
        if self.require_singleton_retired and self.allow_legacy_global_snapshot_writer:
            raise MultiWriterProtocolError(
                "legacy global snapshot writer cannot be enabled when singleton is retired"
            )


def validate_multi_writer_readiness(
    *,
    config: MultiWriterProtocolConfig,
    generation_schema_version: int,
    observed_protocol_version: int,
    singleton_retired: bool,
    hot_paths_ready: bool = True,
) -> dict[str, Any]:
    """Return content-free readiness or fail closed before accepting work."""

    config.validate()
    if not hot_paths_ready:
        raise MultiWriterProtocolError(
            "multi-writer hot paths are not fully migrated; refusing protocol join"
        )
    if int(generation_schema_version) < 3:
        raise MultiWriterProtocolError("generation schema is too old for multi-writer operations")
    if int(observed_protocol_version) != config.protocol_version:
        raise MultiWriterProtocolError("node protocol version is incompatible with generation")
    if config.require_singleton_retired and not singleton_retired:
        raise MultiWriterProtocolError("global singleton writer has not been retired")
    return {
        "status": "ready",
        "protocol_version": config.protocol_version,
        "schema_version": int(generation_schema_version),
        "singleton_retired": bool(singleton_retired),
    }


@dataclass(slots=True)
class MultiWriterRuntimeState:
    """Read-only observation of a backend generation's multi-writer posture.

    This never mutates storage and must stay safe against schema drift: a
    missing or partially migrated generation reports conservative defaults
    rather than raising.
    """

    legacy_singleton_table_present: bool
    total_legacy_global_claims: int
    live_legacy_global_claims: int
    multi_writer_tables_present: bool

    @property
    def legacy_singleton_retired(self) -> bool:
        return self.live_legacy_global_claims == 0


async def observe_multi_writer_state(runtime: Any) -> MultiWriterRuntimeState:
    """Inspect a backend generation without acquiring any writer claim.

    Reports whether the legacy ``life_engine.runtime_context/global`` singleton
    writer still holds an unreleased claim, and whether the multi-writer anchor
    table has been deployed. Operators use this before scheduling the singleton
    retirement maintenance window.
    """

    if (
        not getattr(runtime, "enabled", False)
        or getattr(runtime, "session_factory", None) is None
    ):
        return MultiWriterRuntimeState(
            legacy_singleton_table_present=False,
            total_legacy_global_claims=0,
            live_legacy_global_claims=0,
            multi_writer_tables_present=False,
        )

    generation_id = getattr(getattr(runtime, "generation", None), "generation_id", None)
    base_params: dict[str, Any] = {
        "namespace": LEGACY_RUNTIME_CONTEXT_NAMESPACE,
        "state_key": LEGACY_RUNTIME_CONTEXT_STATE_KEY,
        "generation_id": generation_id,
    }

    legacy_present = False
    total_legacy = 0
    live_legacy = 0
    anchor_present = False

    async with runtime.session_factory() as session:
        claim_query = text(
            """
            SELECT COUNT(*) FROM runtime_singleton_writer_claims
            WHERE namespace = :namespace AND state_key = :state_key
                AND (:generation_id IS NULL OR generation_id = :generation_id)
            """
        )
        live_query = text(
            """
            SELECT COUNT(*) FROM runtime_singleton_writer_claims
            WHERE namespace = :namespace AND state_key = :state_key
                AND (:generation_id IS NULL OR generation_id = :generation_id)
                AND released_at IS NULL
            """
        )
        anchor_query = text(f"SELECT 1 FROM {_MULTI_WRITER_ANCHOR_TABLE} LIMIT 1")
        try:
            legacy_present = True
            total_legacy = int(await session.scalar(claim_query, base_params) or 0)
            live_legacy = int(await session.scalar(live_query, base_params) or 0)
        except (OperationalError, ProgrammingError):
            legacy_present = False
            total_legacy = 0
            live_legacy = 0
        try:
            await session.scalar(anchor_query)
            anchor_present = True
        except (OperationalError, ProgrammingError):
            anchor_present = False

    return MultiWriterRuntimeState(
        legacy_singleton_table_present=legacy_present,
        total_legacy_global_claims=total_legacy,
        live_legacy_global_claims=live_legacy,
        multi_writer_tables_present=anchor_present,
    )


__all__ = [
    "MULTI_WRITER_PROTOCOL_VERSION",
    "MULTI_WRITER_HOT_PATHS_READY",
    "LEGACY_RUNTIME_CONTEXT_NAMESPACE",
    "LEGACY_RUNTIME_CONTEXT_STATE_KEY",
    "MultiWriterProtocolConfig",
    "MultiWriterProtocolError",
    "MultiWriterRuntimeState",
    "validate_multi_writer_readiness",
    "observe_multi_writer_state",
]
