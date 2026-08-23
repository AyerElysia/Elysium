"""Pure initiative event codecs and reducers.

This module contains no store, lock, actor gate, or mutation entry point.  The
only writable initiative implementation is the SQL record family owned by
``ProactiveAuthority``; keeping the deterministic codec here avoids creating a
second authority merely to replay immutable events.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from ..storage.runtime_contracts import RuntimeEventRecord
from .contracts import (
    InitiativeConflict,
    InitiativeOutreachCommand,
    InitiativeSeedCommand,
    InitiativeSeedView,
    InitiativeTransitionError,
)


def reencounter_occurrence(seed_id: str, seed_revision: int) -> str:
    digest = hashlib.sha256(
        f"{seed_id}\0{int(seed_revision)}".encode()
    ).hexdigest()
    return f"initiative:reencounter:{digest}"


def outreach_delivery_occurrence(outreach_occurrence_id: str) -> str:
    """Return the legacy in-memory delivery occurrence identity."""

    digest = hashlib.sha256(
        str(outreach_occurrence_id).encode("utf-8")
    ).hexdigest()
    return f"initiative:outreach:delivery:{digest}"


def outreach_inbox_occurrence(outreach_occurrence_id: str) -> str:
    digest = hashlib.sha256(
        str(outreach_occurrence_id).encode("utf-8")
    ).hexdigest()
    return f"initiative:outreach:inbox:{digest}"


def outreach_resolution_occurrence(outreach_occurrence_id: str) -> str:
    digest = hashlib.sha256(
        str(outreach_occurrence_id).encode("utf-8")
    ).hexdigest()
    return f"initiative:outreach:resolution:{digest}"


def outreach_claim_occurrence(
    outreach_occurrence_id: str,
    action_id: str,
) -> str:
    digest = hashlib.sha256(
        (
            str(outreach_occurrence_id)
            + "\0"
            + str(action_id)
        ).encode("utf-8")
    ).hexdigest()
    return f"initiative:outreach:claim:{digest}"


def outreach_delivery_proof_occurrence(
    outreach_occurrence_id: str,
    action_id: str,
    delivery_message_id: str,
) -> str:
    digest = hashlib.sha256(
        (
            str(outreach_occurrence_id)
            + "\0"
            + str(action_id)
            + "\0"
            + str(delivery_message_id)
        ).encode("utf-8")
    ).hexdigest()
    return f"initiative:outreach:delivery-proof:{digest}"


def seed_command_from_payload(payload: dict[str, Any]) -> InitiativeSeedCommand:
    return InitiativeSeedCommand(
        occurrence_id=str(payload["occurrence_id"]),
        seed_id=str(payload["seed_id"]),
        action=str(payload["action"]),  # type: ignore[arg-type]
        actor_consciousness_instance_id=str(payload["actor"]),
        source_instance_id=str(payload["source_instance_id"]),
        source_occurrence_ids=tuple(payload.get("source_occurrence_ids") or ()),
        causation_occurrence_id=str(payload["causation_occurrence_id"]),
        expected_revision=int(payload["expected_revision"]),
        public_statement=str(payload.get("public_statement") or ""),
        related_entity_refs=tuple(payload.get("related_entity_refs") or ()),
        occurred_at=str(payload["occurred_at"]),
        reencounter_after_minutes=int(
            payload.get("reencounter_after_minutes") or 0
        ),
    )


def seed_command_payload(command: InitiativeSeedCommand) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command_sha256": command.canonical_sha256(),
        "occurrence_id": command.occurrence_id,
        "seed_id": command.seed_id,
        "action": command.action,
        "actor": command.actor_consciousness_instance_id,
        "source_instance_id": command.source_instance_id,
        "source_occurrence_ids": list(command.source_occurrence_ids),
        "causation_occurrence_id": command.causation_occurrence_id,
        "expected_revision": command.expected_revision,
        "public_statement": command.public_statement,
        "related_entity_refs": list(command.related_entity_refs),
        "occurred_at": command.occurred_at,
        "reencounter_after_minutes": command.reencounter_after_minutes,
    }


def apply_seed_event(
    current: InitiativeSeedView | None,
    record: RuntimeEventRecord,
) -> InitiativeSeedView:
    command = seed_command_from_payload(record.payload)
    if current is None:
        if command.action != "hold":
            raise InitiativeTransitionError(
                "initiative history must begin with hold"
            )
        return InitiativeSeedView(
            seed_id=command.seed_id,
            status="open",
            revision=1,
            current_statement=command.public_statement,
            related_entity_refs=command.related_entity_refs,
            opened_at=command.occurred_at,
            last_changed_at=command.occurred_at,
            last_event_position=record.position,
            last_event_id=f"initiative:seed:event:{record.position}",
            last_occurrence_id=command.occurrence_id,
            content_event_id=f"initiative:seed:event:{record.position}",
            content_revision=1,
        )
    if current.status == "released":
        raise InitiativeTransitionError("released initiative is terminal")
    if command.expected_revision != current.revision:
        raise InitiativeConflict(
            "initiative history contains a stale revision",
            seed_id=command.seed_id,
            current_revision=current.revision,
        )
    changes: dict[str, Any] = {}
    if command.action == "rewrite":
        changes.update(
            current_statement=command.public_statement,
            related_entity_refs=command.related_entity_refs,
            content_event_id=f"initiative:seed:event:{record.position}",
            content_revision=command.revision,
        )
    elif command.action == "reencounter":
        changes.update(
            reencounter_at=command.reencounter_at(),
            reencounter_revision=command.revision,
            reencounter_event_id=f"initiative:seed:event:{record.position}",
            reencounter_delivered_at="",
            reencounter_delivery_event_id="",
        )
    elif command.action == "release":
        changes.update(
            status="released",
            current_statement=command.public_statement,
            related_entity_refs=command.related_entity_refs,
            content_event_id=f"initiative:seed:event:{record.position}",
            content_revision=command.revision,
            reencounter_at="",
            reencounter_revision=0,
            reencounter_event_id="",
            reencounter_delivered_at="",
            reencounter_delivery_event_id="",
        )
    else:
        raise InitiativeTransitionError("hold cannot mutate an existing seed")
    return replace(
        current,
        revision=command.revision,
        last_changed_at=command.occurred_at,
        last_event_position=record.position,
        last_event_id=f"initiative:seed:event:{record.position}",
        last_occurrence_id=command.occurrence_id,
        **changes,
    )


def outreach_command_payload(command: InitiativeOutreachCommand) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command_sha256": command.canonical_sha256(),
        "occurrence_id": command.occurrence_id,
        "actor": command.actor_consciousness_instance_id,
        "source_instance_id": command.source_instance_id,
        "source_occurrence_ids": list(command.source_occurrence_ids),
        "causation_occurrence_id": command.causation_occurrence_id,
        "audience_ref": command.audience_ref,
        "surface_ref": command.surface_ref,
        "public_intention": command.public_intention,
        "occurred_at": command.occurred_at,
        "seed_id": command.seed_id,
        "seed_revision": command.seed_revision,
    }


def outreach_command_from_payload(
    payload: dict[str, Any],
) -> InitiativeOutreachCommand:
    return InitiativeOutreachCommand(
        occurrence_id=str(payload["occurrence_id"]),
        actor_consciousness_instance_id=str(payload["actor"]),
        source_instance_id=str(payload["source_instance_id"]),
        source_occurrence_ids=tuple(payload.get("source_occurrence_ids") or ()),
        causation_occurrence_id=str(payload["causation_occurrence_id"]),
        audience_ref=str(payload["audience_ref"]),
        surface_ref=str(payload["surface_ref"]),
        public_intention=str(payload["public_intention"]),
        occurred_at=str(payload["occurred_at"]),
        seed_id=str(payload.get("seed_id") or ""),
        seed_revision=int(payload.get("seed_revision") or 0),
    )


__all__ = [
    "apply_seed_event",
    "outreach_command_from_payload",
    "outreach_command_payload",
    "outreach_claim_occurrence",
    "outreach_delivery_proof_occurrence",
    "outreach_delivery_occurrence",
    "outreach_inbox_occurrence",
    "outreach_resolution_occurrence",
    "reencounter_occurrence",
    "seed_command_from_payload",
    "seed_command_payload",
]
