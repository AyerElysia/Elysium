"""One authority facade over the two durable proactive record families.

Attention threads and initiative seeds describe different things: the former
is a subject-chosen focus, while the latter preserves a possible future act.
They nevertheless share one lifecycle, one actor gate, and one model-facing
system.  This facade makes that ownership explicit without flattening their
domain records into an ambiguous mutable object.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..attention_threads import (
        AttentionThreadCommand,
        AttentionThreadCommit,
        AttentionThreadEventPage,
        AttentionThreadPage,
        AttentionThreadPageQuery,
        AttentionThreadService,
        AttentionThreadValueChunk,
        AttentionThreadView,
        InstanceFocus,
    )
    from ..initiative.contracts import (
        InitiativeOutreachClaimReceipt,
        InitiativeOutreachCommand,
        InitiativeOutreachDeliveryReceipt,
        InitiativeOutreachOutcome,
        InitiativeOutreachReceipt,
        InitiativeOutreachResolutionReceipt,
        InitiativePendingExpression,
        InitiativePendingOutreach,
        InitiativePlatformDeliveryProofReceipt,
        InitiativeRecordStorePort,
        InitiativeReencounterReceipt,
        InitiativeSeedCommand,
        InitiativeSeedCommit,
        InitiativeSeedView,
    )


class ProactiveAuthority:
    """The only live authority exported by Life Engine for proactive state."""

    def __init__(
        self,
        *,
        attention: AttentionThreadService,
        initiative: InitiativeRecordStorePort,
    ) -> None:
        self._attention = attention
        self._initiative = initiative

    async def decide_attention(
        self,
        command: AttentionThreadCommand,
    ) -> AttentionThreadCommit:
        return await self._attention.decide(command)

    async def get_attention(self, thread_id: str) -> AttentionThreadView | None:
        return await self._attention.get(thread_id)

    async def page_attention(
        self,
        query: AttentionThreadPageQuery,
    ) -> AttentionThreadPage:
        return await self._attention.page(query)

    async def attention_event_page(
        self,
        thread_id: str,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> AttentionThreadEventPage:
        return await self._attention.event_page(
            thread_id,
            after_position=after_position,
            limit=limit,
        )

    async def read_attention_statement(
        self,
        event_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> AttentionThreadValueChunk:
        return await self._attention.read_statement_chunk(
            event_id,
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

    async def set_attention_focus(self, focus: InstanceFocus) -> InstanceFocus:
        return await self._attention.set_focus(focus)

    async def get_attention_focus(
        self,
        instance_id: str,
    ) -> InstanceFocus | None:
        return await self._attention.get_focus(instance_id)

    async def clear_attention_focus(
        self,
        instance_id: str,
        *,
        expected_revision: int,
    ) -> None:
        await self._attention.clear_focus(
            instance_id,
            expected_revision=expected_revision,
        )

    async def decide_initiative(
        self,
        command: InitiativeSeedCommand,
    ) -> InitiativeSeedCommit:
        return await self._initiative.decide_seed(command)

    async def get_initiative(
        self,
        seed_id: str,
    ) -> InitiativeSeedView | None:
        return await self._initiative.get_seed(seed_id)

    async def list_initiatives(
        self,
        *,
        include_released: bool = False,
    ) -> tuple[InitiativeSeedView, ...]:
        return await self._initiative.list_seeds(
            include_released=include_released
        )

    async def due_reencounters(
        self,
        *,
        now: str,
    ) -> tuple[InitiativeSeedView, ...]:
        return await self._initiative.due_reencounters(now=now)

    async def record_reencounter_delivery(
        self,
        *,
        seed_id: str,
        seed_revision: int,
        life_event_id: str,
        occurred_at: str,
    ) -> InitiativeReencounterReceipt:
        return await self._initiative.record_reencounter_delivery(
            seed_id=seed_id,
            seed_revision=seed_revision,
            life_event_id=life_event_id,
            occurred_at=occurred_at,
        )

    async def begin_outreach(
        self,
        command: InitiativeOutreachCommand,
    ) -> InitiativeOutreachReceipt:
        return await self._initiative.begin_outreach(command)

    async def pending_outreach(
        self,
        *,
        limit: int = 32,
    ) -> tuple[InitiativePendingOutreach, ...]:
        return await self._initiative.pending_outreach(limit=limit)

    async def record_outreach_delivery(
        self,
        *,
        outreach_occurrence_id: str,
        stream_id: str,
        trigger_message_id: str,
        occurred_at: str,
        platform: str = "unknown",
    ) -> InitiativeOutreachDeliveryReceipt:
        return await self._initiative.record_outreach_delivery(
            outreach_occurrence_id=outreach_occurrence_id,
            stream_id=stream_id,
            trigger_message_id=trigger_message_id,
            occurred_at=occurred_at,
            platform=platform,
        )

    async def pending_expression_outreach(
        self,
        *,
        limit: int = 32,
    ) -> tuple[InitiativePendingExpression, ...]:
        return await self._initiative.pending_expression_outreach(limit=limit)

    async def claim_outreach_expression(
        self,
        *,
        outreach_occurrence_id: str,
        action_id: str,
        claim_owner: str,
        lease_seconds: int,
        occurred_at: str,
    ) -> InitiativeOutreachClaimReceipt:
        return await self._initiative.claim_outreach_expression(
            outreach_occurrence_id=outreach_occurrence_id,
            action_id=action_id,
            claim_owner=claim_owner,
            lease_seconds=lease_seconds,
            occurred_at=occurred_at,
        )

    async def resolve_outreach_expression(
        self,
        *,
        outreach_occurrence_id: str,
        outcome: InitiativeOutreachOutcome,
        action_id: str = "",
        delivery_receipt_sha256: str = "",
        delivery_message_id: str = "",
        occurred_at: str,
    ) -> InitiativeOutreachResolutionReceipt:
        return await self._initiative.resolve_outreach_expression(
            outreach_occurrence_id=outreach_occurrence_id,
            outcome=outcome,
            action_id=action_id,
            delivery_receipt_sha256=delivery_receipt_sha256,
            delivery_message_id=delivery_message_id,
            occurred_at=occurred_at,
        )

    async def record_outreach_delivery_proof(
        self,
        *,
        outreach_occurrence_id: str,
        action_id: str,
        delivery_receipt: dict[str, Any],
        occurred_at: str,
    ) -> InitiativePlatformDeliveryProofReceipt:
        return await self._initiative.record_outreach_delivery_proof(
            outreach_occurrence_id=outreach_occurrence_id,
            action_id=action_id,
            delivery_receipt=delivery_receipt,
            occurred_at=occurred_at,
        )

    async def health_snapshot(self) -> dict[str, Any]:
        """Return content-free health for the unified authority."""

        attention, initiative = await asyncio.gather(
            self._attention.health_snapshot(),
            self._initiative.health_snapshot(),
        )
        statuses = {
            str(attention.get("status") or "failed"),
            str(initiative.get("status") or "failed"),
        }
        if "failed" in statuses:
            status = "failed"
        elif "degraded" in statuses:
            status = "degraded"
        else:
            status = "healthy"
        return {
            "component": "proactive_authority",
            "status": status,
            "authority_count": 1,
            "record_families": ("attention", "initiative"),
            "attention": attention,
            "initiative": initiative,
        }


__all__ = ["ProactiveAuthority"]
