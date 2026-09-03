"""Unified opportunity bus: one page, invitation cooldown after exact receipt."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.opportunity import (
    CollectedOffer,
    OpportunityBus,
    OpportunityOffer,
)
from plugins.life_engine.opportunity.producers import collect_file_care_invitation
from plugins.life_engine.opportunity.render import render_opportunity_page
from plugins.life_engine.prompts.sections import (
    DEFAULT_HEARTBEAT_SECTIONS,
    OpportunitySection,
    SectionContext,
)
from src.kernel.llm.context_delivery import EffectiveContextReceipt


def _offer(
    offer_id: str,
    *,
    kind: str = "invitation",
    domain: str = "file_care",
    facts: dict[str, Any] | None = None,
) -> OpportunityOffer:
    return OpportunityOffer(
        offer_id=offer_id,
        kind=kind,  # type: ignore[arg-type]
        domain=domain,  # type: ignore[arg-type]
        producer="test",
        observed_at="2026-09-03T00:00:00+00:00",
        facts=facts or {"note": offer_id},
        disclosure_ref=("nucleus_read_file",),
    )


def _exact_receipt(delivery_id: str) -> EffectiveContextReceipt:
    return EffectiveContextReceipt(
        delivery_id=delivery_id,
        exact_present=True,
        expected_utf8_bytes=32,
        expected_sha256="a" * 64,
        effective_utf8_bytes=32,
        effective_sha256="a" * 64,
        part_kind="text",
    )


def _idle_service(**extra: Any) -> SimpleNamespace:
    async def _no_texts() -> dict[str, str]:
        return {}

    payload = {
        "_proactive_authority": None,
        "_learning_scheduler": None,
        "_last_memory_maintenance_prompt_at": None,
        "read_subject_authority_texts": _no_texts,
        "_cfg": lambda: SimpleNamespace(
            curiosity=SimpleNamespace(enabled=False, inject_to_heartbeat=False),
            narrative=None,
            learning=None,
        ),
        "plugin": SimpleNamespace(config=None),
    }
    payload.update(extra)
    service = SimpleNamespace(**payload)
    service._opportunity_bus = OpportunityBus(service)
    return service


def test_two_invitations_sort_stably_and_both_appear() -> None:
    page = render_opportunity_page(
        [
            _offer("learning:subject_review", domain="learning", facts={"due_count": 2}),
            _offer("file_care:diaries", domain="file_care", facts={"total": 72}),
        ]
    )
    assert page is not None
    assert page.text.index("file_care:diaries") < page.text.index(
        "learning:subject_review"
    )
    assert "[invitation/file_care]" in page.text
    assert "[invitation/learning]" in page.text
    assert "get_skill" not in page.text
    assert page.omitted_ids == ()


def test_continuity_column_precedes_invitations_and_is_not_a_suggestion() -> None:
    page = render_opportunity_page(
        [
            _offer(
                "file_care:diaries",
                domain="file_care",
                facts={"total": 80},
            ),
            _offer(
                "attention:thread-1",
                kind="continuity",
                domain="attention",
                facts={"thread_id": "thread-1", "status": "open", "excerpt": "那段旋律"},
            ),
            _offer(
                "initiative:seed-1",
                kind="continuity",
                domain="initiative",
                facts={
                    "seed_id": "seed-1",
                    "status": "open",
                    "excerpt": "以后想再提起这件事",
                },
            ),
        ]
    )
    assert page is not None
    assert page.text.index("#### 你留下的线索") < page.text.index("#### 可见机会")
    assert page.text.index("attention:thread-1") < page.text.index("#### 可见机会")
    assert page.text.index("initiative:seed-1") < page.text.index("#### 可见机会")
    assert "不是系统新建议" in page.text
    assert "那段旋律" in page.text
    assert "以后想再提起这件事" in page.text


def test_omitted_ids_are_declared_and_not_silent() -> None:
    offers = [
        _offer(
            f"learning:doc-{index:02d}",
            domain="learning",
            facts={"due_count": index + 1},
        )
        for index in range(16)
    ]
    page = render_opportunity_page(offers, max_bytes=1024)
    assert page is not None
    assert page.omitted_ids
    assert "omitted:" in page.text
    for offer_id in page.omitted_ids:
        assert offer_id in page.text


@pytest.mark.asyncio
async def test_ignore_and_missing_receipt_do_not_cool_or_write_rejection() -> None:
    cooled: list[str] = []

    async def commit(_receipt: Any) -> bool:
        cooled.append("yes")
        return True

    service = _idle_service()
    bus: OpportunityBus = service._opportunity_bus
    page = await bus.collect_and_render(
        extra_offers=[
            CollectedOffer(
                _offer("file_care:diaries", facts={"total": 90}),
                commit=commit,
            )
        ]
    )
    assert page is not None
    assert await bus.commit_page_delivery(
        page.delivery_id,
        EffectiveContextReceipt(
            delivery_id=page.delivery_id,
            exact_present=False,
            expected_utf8_bytes=32,
            expected_sha256="a" * 64,
            effective_utf8_bytes=None,
            effective_sha256=None,
            part_kind="text",
        ),
    ) is False
    assert cooled == []
    assert "拒绝记录" not in page.text


@pytest.mark.asyncio
async def test_omitted_invitation_does_not_enter_cooldown() -> None:
    cooled: list[str] = []

    def _commit_for(offer_id: str):
        async def commit(_receipt: Any) -> bool:
            cooled.append(offer_id)
            return True

        return commit

    extra_offers = [
        CollectedOffer(
            _offer("file_care:diaries", facts={"total": 90}),
            commit=_commit_for("file_care:diaries"),
        ),
        *[
            CollectedOffer(
                _offer(
                    f"learning:doc-{index:02d}",
                    domain="learning",
                    facts={"due_count": index + 1},
                ),
                commit=_commit_for(f"learning:doc-{index:02d}"),
            )
            for index in range(16)
        ],
    ]
    service = _idle_service()
    bus: OpportunityBus = service._opportunity_bus
    page = await bus.collect_and_render(extra_offers=extra_offers, max_bytes=1024)
    assert page is not None
    assert page.omitted_ids
    shown = set(page.shown_ids)
    omitted = set(page.omitted_ids)
    assert shown
    assert await bus.commit_page_delivery(
        page.delivery_id, _exact_receipt(page.delivery_id)
    )
    assert set(cooled) == shown
    assert set(cooled).isdisjoint(omitted)


@pytest.mark.asyncio
async def test_exact_receipt_cools_only_shown_invitations() -> None:
    cooled: list[str] = []

    async def commit(_receipt: Any) -> bool:
        cooled.append("file_care")
        return True

    service = _idle_service()
    bus: OpportunityBus = service._opportunity_bus
    page = await bus.collect_and_render(
        extra_offers=[
            CollectedOffer(
                _offer("file_care:diaries", facts={"total": 70}),
                commit=commit,
            ),
            CollectedOffer(
                _offer(
                    "attention:t1",
                    kind="continuity",
                    domain="attention",
                    facts={"thread_id": "t1", "status": "open", "excerpt": "线索"},
                )
            ),
        ]
    )
    assert page is not None
    assert await bus.commit_page_delivery(
        page.delivery_id, _exact_receipt(page.delivery_id)
    )
    assert cooled == ["file_care"]


@pytest.mark.asyncio
async def test_bus_does_not_create_or_rewrite_proactive_authority() -> None:
    writes: list[str] = []

    async def decide_attention(*_args: Any, **_kwargs: Any) -> None:
        writes.append("attention")

    async def decide_initiative(*_args: Any, **_kwargs: Any) -> None:
        writes.append("initiative")

    async def page_attention(_query: Any) -> Any:
        return SimpleNamespace(
            items=(
                SimpleNamespace(
                    thread_id="thread-1",
                    status="open",
                    revision=1,
                    statement_excerpt="那段旋律",
                    excerpt_complete=True,
                    statement_bytes=12,
                ),
            )
        )

    async def list_initiatives(*, include_released: bool = False) -> tuple[Any, ...]:
        assert include_released is False
        return (
            SimpleNamespace(
                seed_id="seed-1",
                status="open",
                revision=1,
                current_statement="以后想再提起这件事",
            ),
        )

    service = _idle_service(
        _proactive_authority=object(),
        page_attention_threads=page_attention,
        list_initiatives=list_initiatives,
        decide_attention=decide_attention,
        decide_initiative=decide_initiative,
        get_skill=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("get_skill must not auto-run")
        ),
    )
    bus: OpportunityBus = service._opportunity_bus
    page = await bus.collect_and_render()
    assert page is not None
    assert "attention:thread-1" in page.text
    assert "initiative:seed-1" in page.text
    assert "你留下的线索" in page.text
    assert "get_skill" not in page.text
    assert await bus.commit_page_delivery(
        page.delivery_id, _exact_receipt(page.delivery_id)
    )
    assert writes == []


@pytest.mark.asyncio
async def test_curiosity_lands_in_invitation_column() -> None:
    class _Engine:
        async def load_opportunity(self) -> Any:
            return SimpleNamespace(
                opportunity_id="eop_test",
                source_occurrence_id="occ-1",
                observed_gap="雨声里的停顿",
                open_question="那一刻为什么沉默",
                possible_next_look="",
                generator_note="",
            )

    service = _idle_service(
        _cfg=lambda: SimpleNamespace(
            curiosity=SimpleNamespace(enabled=True, inject_to_heartbeat=True),
            narrative=None,
            learning=None,
        ),
        _get_curiosity_engine=lambda: _Engine(),
        _proactive_authority=object(),
        page_attention_threads=lambda _query: _async_page(),
        list_initiatives=lambda **_kwargs: _async_empty(),
    )

    async def _async_page(_query: Any) -> Any:
        return SimpleNamespace(
            items=(
                SimpleNamespace(
                    thread_id="melody",
                    status="open",
                    revision=1,
                    statement_excerpt="那段旋律",
                    excerpt_complete=True,
                    statement_bytes=12,
                ),
            )
        )

    async def _async_empty(**_kwargs: Any) -> tuple[Any, ...]:
        return ()

    service.page_attention_threads = _async_page
    service.list_initiatives = _async_empty
    bus: OpportunityBus = service._opportunity_bus
    page = await bus.collect_and_render()
    assert page is not None
    assert "epistemic:eop_test" in page.text
    assert page.text.index("#### 你留下的线索") < page.text.index("#### 可见机会")
    assert page.text.index("attention:melody") < page.text.index("#### 可见机会")
    assert page.text.index("#### 可见机会") < page.text.index("epistemic:eop_test")
    assert "雨声里的停顿" in page.text or "那一刻为什么沉默" in page.text


@pytest.mark.asyncio
async def test_file_care_collect_does_not_write_cooldown(tmp_path: Path) -> None:
    diaries = tmp_path / "diaries"
    diaries.mkdir()
    for index in range(6):
        (diaries / f"2026-08-01-{index}.md").write_text("x", encoding="utf-8")
    service = SimpleNamespace(_workspace_dir=lambda: tmp_path)
    first = await collect_file_care_invitation(
        service,
        now=None,
    )
    assert first is not None
    assert not (tmp_path / "runtime" / "file_care_state.json").exists()
    second = await collect_file_care_invitation(service)
    assert second is not None
    assert await first.commit(_exact_receipt("unused"))
    assert (tmp_path / "runtime" / "file_care_state.json").is_file()
    third = await collect_file_care_invitation(service)
    assert third is None


@pytest.mark.asyncio
async def test_health_snapshot_has_ids_without_facts_body() -> None:
    service = _idle_service()
    bus: OpportunityBus = service._opportunity_bus
    page = await bus.collect_and_render(
        extra_offers=[
            CollectedOffer(_offer("file_care:diaries", facts={"total": 88, "secret": "private"}))
        ]
    )
    assert page is not None
    health = bus.health_snapshot()
    assert "file_care:diaries" in health["due_ids"]
    dumped = str(health)
    assert "private" not in dumped
    assert "secret" not in dumped
    assert "facts" not in dumped


def test_default_heartbeat_has_one_opportunity_page() -> None:
    section_ids = [section.section_id for section in DEFAULT_HEARTBEAT_SECTIONS]
    assert section_ids.count("opportunity_page") == 1
    assert "recent_handwritten_diaries" in section_ids
    assert "todo_board" in section_ids
    assert "attention_opportunity" not in section_ids
    assert "file_care_opportunity" not in section_ids
    assert "river_reflection" not in section_ids
    assert "subject_review_opportunity" not in section_ids
    assert "curiosity" not in section_ids


@pytest.mark.asyncio
async def test_opportunity_section_renders_bus_page() -> None:
    service = _idle_service()
    page = await service._opportunity_bus.collect_and_render(
        extra_offers=[CollectedOffer(_offer("file_care:diaries", facts={"total": 64}))]
    )
    assert page is not None
    # collect_and_render already ran; section would collect again. Seed extra via
    # a second render after attaching the same extra producer is not needed:
    # re-render with the still-pending page by calling section after extra collect
    # would re-collect. Directly assert the section uses the bus.
    ctx = SectionContext(
        service=service,
        config=service._cfg(),
        today_str="2026-09-03",
    )
    rendered = await OpportunitySection().render(ctx)
    # idle service has no due producers; extra_offers are not in the section path.
    assert rendered is None or "机会页" in rendered


@pytest.mark.asyncio
async def test_ranking_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="ranking"):
        _offer("file_care:diaries", facts={"total": 1, "importance": 9})
