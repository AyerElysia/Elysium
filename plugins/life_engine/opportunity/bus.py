"""One opportunity bus: collect, render, exact-receipt cooldown for invitations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.app.plugin_system.api import log_api

from .contracts import OPPORTUNITY_PAGE_MAX_BYTES, OpportunityPage
from .producers import CollectedOffer, collect_all_offers
from .render import render_opportunity_page

logger = log_api.get_logger("life_engine.opportunity")


def receipt_is_exact(delivery_id: str, receipt: Any) -> bool:
    identity = str(delivery_id or "").strip()
    expected_bytes = getattr(receipt, "expected_utf8_bytes", None)
    effective_bytes = getattr(receipt, "effective_utf8_bytes", None)
    return bool(
        identity
        and str(getattr(receipt, "delivery_id", "") or "") == identity
        and str(getattr(receipt, "part_kind", "") or "") == "text"
        and bool(getattr(receipt, "exact_present", False))
        and isinstance(expected_bytes, int)
        and isinstance(effective_bytes, int)
        and expected_bytes == effective_bytes
        and str(getattr(receipt, "expected_sha256", "") or "")
        == str(getattr(receipt, "effective_sha256", "") or "")
    )


class OpportunityBus:
    """Rebuildable delivery projection. Not Life Event authority."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self._pending_page: OpportunityPage | None = None
        self._pending_hooks: dict[str, CollectedOffer] = {}
        self._last_delivery_id: str = ""
        self._last_delivery_at: str = ""
        self._last_error_types: tuple[str, ...] = ()
        self._last_due_ids: tuple[str, ...] = ()
        self._last_omitted_ids: tuple[str, ...] = ()
        self._last_kinds: dict[str, int] = {"continuity": 0, "invitation": 0}
        self._last_domains: tuple[str, ...] = ()

    def get_pending_page(self) -> OpportunityPage | None:
        return self._pending_page

    def health_snapshot(self) -> dict[str, Any]:
        """Content-free health: ids and error types, never facts or statements."""

        due_ids = list(self._last_due_ids)
        omitted_ids = list(self._last_omitted_ids)
        status = "ready" if due_ids or omitted_ids else "empty"
        if self._last_error_types:
            status = "degraded"
        return {
            "component": "opportunity_bus",
            "status": status,
            "due_ids": due_ids,
            "omitted_ids": omitted_ids,
            "kinds": dict(self._last_kinds),
            "domains": list(self._last_domains),
            "last_delivery_id": self._last_delivery_id,
            "last_delivery_at": self._last_delivery_at,
            "error_type": ",".join(self._last_error_types),
        }

    async def collect_and_render(
        self,
        *,
        config: Any | None = None,
        max_bytes: int = OPPORTUNITY_PAGE_MAX_BYTES,
        now: datetime | None = None,
        extra_offers: list[CollectedOffer] | None = None,
    ) -> OpportunityPage | None:
        collected, errors = await collect_all_offers(
            self._service,
            config=config,
            now=now,
        )
        if extra_offers:
            collected.extend(extra_offers)
        self._last_error_types = errors
        offers = tuple(item.offer for item in collected)
        by_id = {item.offer.offer_id: item for item in collected}
        self._pending_page = None
        self._pending_hooks = {}
        if not offers:
            self._remember_page(None)
            return None
        page = render_opportunity_page(
            offers,
            max_bytes=max_bytes,
            observed_at=(now or datetime.now(UTC).astimezone()).isoformat(),
        )
        if page is None:
            self._remember_page(None)
            return None
        shown = set(page.shown_ids)
        hooks = {
            offer_id: collected_offer
            for offer_id, collected_offer in by_id.items()
            if offer_id in shown
            and collected_offer.offer.kind == "invitation"
            and collected_offer.commit is not None
        }
        self._pending_page = page
        self._pending_hooks = hooks
        self._remember_page(page)
        self._bind_learning_delivery(page)
        return page

    def _bind_learning_delivery(self, page: OpportunityPage) -> None:
        if "learning:subject_review" not in page.shown_ids:
            return
        scheduler = getattr(self._service, "_learning_scheduler", None)
        bind = getattr(scheduler, "bind_subject_review_offer_delivery", None)
        if callable(bind):
            bind(page.delivery_id, page.delivery_marker)

    def _remember_page(self, page: OpportunityPage | None) -> None:
        if page is None:
            self._last_due_ids = ()
            self._last_omitted_ids = ()
            self._last_kinds = {"continuity": 0, "invitation": 0}
            self._last_domains = ()
            return
        self._last_due_ids = tuple(offer.offer_id for offer in page.offers)
        self._last_omitted_ids = page.omitted_ids
        kinds = {"continuity": 0, "invitation": 0}
        domains: list[str] = []
        for offer in page.offers:
            kinds[offer.kind] = kinds.get(offer.kind, 0) + 1
            if offer.domain not in domains:
                domains.append(offer.domain)
        self._last_kinds = kinds
        self._last_domains = tuple(domains)

    async def commit_page_delivery(self, delivery_id: str, receipt: Any) -> bool:
        """Cool shown invitations only after exact page receipt. Never write clues."""

        page = self._pending_page
        identity = str(delivery_id or "").strip()
        if page is None or page.delivery_id != identity:
            return False
        if not receipt_is_exact(identity, receipt):
            self._pending_page = None
            self._pending_hooks = {}
            return False
        hooks = dict(self._pending_hooks)
        self._pending_page = None
        self._pending_hooks = {}
        cooled: list[str] = []
        for offer_id, collected in hooks.items():
            offer = collected.offer
            if offer.kind != "invitation" or offer_id not in page.shown_ids:
                continue
            if collected.commit is None:
                continue
            try:
                accepted = await collected.commit(receipt)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "opportunity invitation commit failed: "
                    f"offer_id={offer_id} error_type={type(exc).__name__}"
                )
                continue
            if accepted:
                cooled.append(offer_id)
        self._last_delivery_id = identity
        self._last_delivery_at = datetime.now(UTC).astimezone().isoformat()
        logger.debug(
            "opportunity page delivery committed: "
            f"delivery_id={identity} cooled={len(cooled)} "
            f"omitted={len(page.omitted_ids)}"
        )
        return True
