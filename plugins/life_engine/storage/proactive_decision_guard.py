"""Cross-family idempotency guard for the single proactive command surface."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from .contracts import StorageBackendRuntime
from .models import BackendKind

_NAMESPACE = "life_proactive.decision_guards"
_INITIATIVE_NAMESPACES = (
    "life_initiative.seed_decisions",
    "life_initiative.outreach_decisions",
)


class ProactiveDecisionGuardConflict(RuntimeError):
    """One stable model tool call was reused for another subject decision."""


def _guard_occurrence(occurrence_id: str) -> str:
    digest = hashlib.sha256(str(occurrence_id).encode("utf-8")).hexdigest()
    return f"proactive:decision-guard:{digest}"


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value or "").strip())
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _bind_time(backend: BackendKind, value: Any) -> datetime | str:
    parsed = _parse_time(value)
    if backend == BackendKind.MYSQL:
        return parsed.replace(tzinfo=None)
    return parsed.isoformat()


async def claim_proactive_decision(
    session: AsyncSession,
    *,
    backend: BackendKind,
    occurrence_id: str,
    record_family: str,
    command_sha256: str,
    occurred_at: str,
    recorded_at: datetime,
) -> None:
    """Claim one occurrence atomically or verify an exact prior claim."""

    payload = {
        "schema_version": 1,
        "occurrence_id": str(occurrence_id),
        "record_family": str(record_family),
        "command_sha256": str(command_sha256),
    }
    payload_json = canonical_json(payload)
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    guard_occurrence = _guard_occurrence(occurrence_id)
    insert_prefix = (
        "INSERT IGNORE" if backend == BackendKind.MYSQL else "INSERT OR IGNORE"
    )
    await session.execute(
        text(
            f"""{insert_prefix} INTO runtime_events (
                namespace, occurrence_id, event_kind, payload_json,
                payload_sha256, occurred_at, recorded_at
            ) VALUES (
                :namespace, :occurrence_id, :event_kind, :payload_json,
                :payload_sha256, :occurred_at, :recorded_at
            )"""
        ),
        {
            "namespace": _NAMESPACE,
            "occurrence_id": guard_occurrence,
            "event_kind": "proactive_decision_claimed",
            "payload_json": payload_json,
            "payload_sha256": payload_sha256,
            "occurred_at": _bind_time(backend, occurred_at),
            "recorded_at": _bind_time(backend, recorded_at),
        },
    )
    for_update = " FOR UPDATE" if backend == BackendKind.MYSQL else ""
    row = (
        (
            await session.execute(
                text(
                    """SELECT namespace, event_kind, payload_json,
                        payload_sha256
                    FROM runtime_events WHERE occurrence_id = :occurrence_id"""
                    + for_update
                ),
                {"occurrence_id": guard_occurrence},
            )
        )
        .mappings()
        .one()
    )
    raw = (
        row["payload_json"].decode("utf-8")
        if isinstance(row["payload_json"], bytes)
        else str(row["payload_json"])
    )
    actual_digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    try:
        existing_payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProactiveDecisionGuardConflict(
            "ProactiveDecisionGuardCorrupt"
        ) from exc
    if (
        str(row["namespace"]) != _NAMESPACE
        or str(row["event_kind"]) != "proactive_decision_claimed"
        or str(row["payload_sha256"]) != actual_digest
        or existing_payload != payload
    ):
        raise ProactiveDecisionGuardConflict(
            "ProactiveDecisionOccurrenceReused"
        )


async def reconcile_proactive_decision_guards(
    runtime: StorageBackendRuntime,
) -> None:
    """Claim every pre-unification decision before accepting new writes.

    Attention uses its own immutable table while Initiative/Outreach use the
    shared runtime ledger.  Startup scans both inside one transaction, rejects
    any historical cross-family occurrence collision, and backfills only exact
    content-bound guard rows.  Re-running is idempotent.
    """

    async with runtime.unit_of_work() as uow:
        session = uow.session
        attention_rows = (
            (
                await session.execute(
                    text(
                        """SELECT occurrence_id, event_sha256,
                            occurred_at, recorded_at
                        FROM attention_thread_events
                        ORDER BY position"""
                    )
                )
            )
            .mappings()
            .all()
        )
        initiative_rows = (
            (
                await session.execute(
                    text(
                        """SELECT namespace, occurrence_id, payload_json,
                            payload_sha256, occurred_at, recorded_at
                        FROM runtime_events
                        WHERE namespace IN (:seed_namespace, :outreach_namespace)
                        ORDER BY position"""
                    ),
                    {
                        "seed_namespace": _INITIATIVE_NAMESPACES[0],
                        "outreach_namespace": _INITIATIVE_NAMESPACES[1],
                    },
                )
            )
            .mappings()
            .all()
        )

        claims: dict[str, tuple[str, str, str, datetime]] = {}

        def remember(
            *,
            occurrence_id: str,
            record_family: str,
            command_sha256: str,
            occurred_at: Any,
            recorded_at: Any,
        ) -> None:
            identity = str(occurrence_id or "").strip()
            digest = str(command_sha256 or "").strip()
            if not identity or len(digest) != 64:
                raise ProactiveDecisionGuardConflict(
                    "ProactiveLegacyDecisionCorrupt"
                )
            claim = (
                record_family,
                digest,
                _parse_time(occurred_at).isoformat(),
                _parse_time(recorded_at),
            )
            existing = claims.get(identity)
            if existing is not None and existing[:2] != claim[:2]:
                raise ProactiveDecisionGuardConflict(
                    "ProactiveLegacyDecisionOccurrenceConflict"
                )
            claims[identity] = claim

        for row in attention_rows:
            remember(
                occurrence_id=str(row["occurrence_id"]),
                record_family="attention",
                command_sha256=str(row["event_sha256"]),
                occurred_at=row["occurred_at"],
                recorded_at=row["recorded_at"],
            )

        for row in initiative_rows:
            raw = (
                row["payload_json"].decode("utf-8")
                if isinstance(row["payload_json"], bytes)
                else str(row["payload_json"])
            )
            if hashlib.sha256(raw.encode("utf-8")).hexdigest() != str(
                row["payload_sha256"]
            ):
                raise ProactiveDecisionGuardConflict(
                    "ProactiveLegacyDecisionPayloadCorrupt"
                )
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ProactiveDecisionGuardConflict(
                    "ProactiveLegacyDecisionPayloadCorrupt"
                ) from exc
            if not isinstance(payload, dict):
                raise ProactiveDecisionGuardConflict(
                    "ProactiveLegacyDecisionPayloadCorrupt"
                )
            family = (
                "initiative"
                if str(row["namespace"]) == _INITIATIVE_NAMESPACES[0]
                else "outreach"
            )
            remember(
                occurrence_id=str(row["occurrence_id"]),
                record_family=family,
                command_sha256=str(payload.get("command_sha256") or ""),
                occurred_at=row["occurred_at"],
                recorded_at=row["recorded_at"],
            )

        for occurrence_id in sorted(claims):
            family, digest, occurred_at, recorded_at = claims[occurrence_id]
            await claim_proactive_decision(
                session,
                backend=runtime.backend,
                occurrence_id=occurrence_id,
                record_family=family,
                command_sha256=digest,
                occurred_at=occurred_at,
                recorded_at=recorded_at,
            )


__all__ = [
    "ProactiveDecisionGuardConflict",
    "claim_proactive_decision",
    "reconcile_proactive_decision_guards",
]
