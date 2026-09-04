"""Opportunity offer contract: facts and delivery, never ranking or meaning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

OfferKind = Literal["continuity", "invitation"]
ContinuityDomain = Literal["attention", "initiative"]
InvitationDomain = Literal["learning", "memory", "narrative", "file_care", "epistemic"]
OfferDomain = ContinuityDomain | InvitationDomain

KIND_ORDER: dict[str, int] = {"continuity": 0, "invitation": 1}

ALLOWED_DOMAINS: dict[str, frozenset[str]] = {
    "continuity": frozenset({"attention", "initiative"}),
    "invitation": frozenset(
        {"learning", "memory", "narrative", "file_care", "epistemic"}
    ),
}

OPPORTUNITY_PAGE_MAX_BYTES = 4096
OPPORTUNITY_PAGE_MIN_BYTES = 2048


def canonical_json(payload: Any) -> str:
    """Stable UTF-8 JSON for identity hashes; not a meaning ranking."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_payload(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OpportunityOffer:
    """One visible fact. Ignore is not rejection; silence is a complete choice."""

    offer_id: str
    kind: OfferKind
    domain: OfferDomain
    producer: str
    observed_at: str
    facts: dict[str, Any]
    disclosure_ref: tuple[str, ...]
    facts_digest: str = ""

    def __post_init__(self) -> None:
        offer_id = str(self.offer_id or "").strip()
        kind = str(self.kind or "").strip()
        domain = str(self.domain or "").strip()
        producer = str(self.producer or "").strip()
        observed_at = str(self.observed_at or "").strip()
        if not offer_id:
            raise ValueError("offer_id must not be empty")
        if kind not in ALLOWED_DOMAINS:
            raise ValueError("offer kind is not continuity or invitation")
        if domain not in ALLOWED_DOMAINS[kind]:
            raise ValueError("offer domain does not match kind")
        if not producer:
            raise ValueError("offer producer must not be empty")
        if not observed_at:
            raise ValueError("offer observed_at must not be empty")
        for key in ("importance", "priority", "score"):
            if key in self.facts:
                raise ValueError("opportunity offers cannot carry ranking fields")
        refs = tuple(str(item).strip() for item in self.disclosure_ref if str(item).strip())
        facts = dict(self.facts)
        digest = str(self.facts_digest or "").strip() or digest_payload(facts)
        object.__setattr__(self, "offer_id", offer_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "producer", producer)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "facts", facts)
        object.__setattr__(self, "disclosure_ref", refs)
        object.__setattr__(self, "facts_digest", digest)

    def sort_key(self) -> tuple[int, str, str]:
        return (KIND_ORDER[self.kind], self.domain, self.offer_id)


@dataclass(frozen=True, slots=True)
class OpportunityPage:
    """One bounded heartbeat page. Cooldown uses exact receipt of this text."""

    delivery_id: str
    delivery_marker: str
    text: str
    offers: tuple[OpportunityOffer, ...]
    shown_ids: tuple[str, ...]
    omitted_ids: tuple[str, ...]
    observed_at: str

    def shown_offers(self) -> tuple[OpportunityOffer, ...]:
        shown = set(self.shown_ids)
        return tuple(offer for offer in self.offers if offer.offer_id in shown)


def offer_identity_payload(offer: OpportunityOffer) -> dict[str, str]:
    return {
        "offer_id": offer.offer_id,
        "kind": offer.kind,
        "domain": offer.domain,
        "facts_digest": offer.facts_digest,
    }


def is_mapping_without_ranking(facts: Mapping[str, Any]) -> bool:
    return not any(key in facts for key in ("importance", "priority", "score"))
