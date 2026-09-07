"""Reference-only bridge from an already-authorized domain due fact.

The hash below identifies the explicitly typed reference manifest, not the
subject's original statement. No text is copied or reinterpreted as truth.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from src.kernel.storage import canonical_json

from ..storage.opportunity_contracts import (
    OpportunitySchedule,
    ProviderOpportunityProposal,
    ProviderStatus,
)


def next_initiative_scan_batch(
    due: tuple[Any, ...],
    *,
    after_id: str,
    limit: int = 32,
) -> tuple[Any, ...]:
    """Rotate a bounded engineering scan without acknowledging any source.

    Closed/paused offers stay in historical source views; they must not keep
    every newer due record behind the first page forever. This is not a
    durable consumption cursor and restart safely scans again.
    """
    if not 1 <= limit <= 100:
        raise ValueError("OpportunitySourceScanLimitOutOfRange")
    ordered = sorted(due, key=lambda item: item.seed_id)
    remaining = [item for item in ordered if item.seed_id > after_id]
    return tuple((remaining or ordered)[:limit])


async def offer_initiative_reencounter(
    runtime: Any,
    seed: Any,
    *,
    record_source_publication: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """Offer a reference or reconcile its already-durable publication.

    A source acknowledgement can fail independently after the Life Event and
    its outbox receipt commit. The next source scan reuses that exact receipt,
    including its stable time; no new occurrence or semantic outcome is made.
    """
    if (
        seed.status != "open"
        or not seed.reencounter_at
        or seed.reencounter_revision <= 0
        or not seed.reencounter_event_id
        or seed.reencounter_delivered_at
    ):
        return False
    provider_id = "life.initiative_reencounter"
    reference = {
        "schema": "initiative.reencounter_reference.v1",
        "seed_id": seed.seed_id,
        "reencounter_event_id": seed.reencounter_event_id,
        "reencounter_revision": seed.reencounter_revision,
        "reencounter_at": seed.reencounter_at,
    }
    ref_hash = hashlib.sha256(canonical_json(reference).encode("utf-8")).hexdigest()
    identity = hashlib.sha256(
        f"{seed.seed_id}\0{seed.reencounter_event_id}".encode("utf-8")
    ).hexdigest()
    opportunity_id = "opportunity:initiative:" + identity
    existing = await runtime.stores.authority.get_opportunity(opportunity_id)
    if existing is not None:
        if existing.referent_sha256 != ref_hash:
            raise RuntimeError("OpportunitySourceReferenceConflict")
        if record_source_publication is not None:
            publication = await runtime.stores.delivery.latest_publication(
                opportunity_id
            )
            if publication is not None:
                # Reconcile a fact even if the provider was since uninstalled.
                # This never reopens the offer or claims the subject saw it.
                await record_source_publication(
                    seed_id=seed.seed_id,
                    seed_revision=seed.reencounter_revision,
                    life_event_id=publication.life_event_occurrence_id,
                    occurred_at=publication.updated_at,
                )
        # A decision to close, pause or reschedule belongs to the subject.
        return False
    binding = await runtime.stores.authority.get_provider(provider_id)
    if binding is None or binding.status != ProviderStatus.ENABLED:
        return False
    await runtime.stores.authority.propose_opportunity(
        ProviderOpportunityProposal(
            occurrence_id="opportunity:initiative:proposal:" + identity,
            opportunity_id=opportunity_id,
            provider_id=provider_id,
            source_instance_id="infrastructure:initiative-reencounter-bridge",
            source_occurrence_ids=(seed.reencounter_event_id,),
            causation_occurrence_id=seed.reencounter_event_id,
            referent_kind=reference["schema"],
            referent_id=seed.seed_id,
            referent_revision=seed.reencounter_revision,
            referent_sha256=ref_hash,
            workflow_id=binding.workflow_id,
            workflow_revision=binding.workflow_revision,
            workflow_sha256=binding.workflow_sha256,
            schedule=OpportunitySchedule.AT,
            first_due_at=seed.reencounter_at,
            interval_seconds=0,
            occurred_at=seed.reencounter_at,
        )
    )
    return True
