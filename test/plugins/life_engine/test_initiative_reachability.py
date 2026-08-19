"""Reachability is physical routing, not a salience or identity heuristic."""

from __future__ import annotations

import pytest

from plugins.life_engine.initiative.contracts import (
    InitiativeSurfaceUnavailable,
    ReachableSurface,
)
from plugins.life_engine.initiative.reachability import (
    ReachabilityRow,
    project_reachable_surfaces,
    resolve_reachable_surface,
)


def test_explicit_canonical_person_can_have_cross_platform_surfaces() -> None:
    surfaces = project_reachable_surfaces(
        [
            ReachabilityRow(
                stream_id="kook-stream",
                platform="kook",
                chat_type="private",
                person_id="account-kook",
                canonical_person_key="xiaoxi",
                user_label="小希",
            ),
            ReachabilityRow(
                stream_id="qq-stream",
                platform="qq",
                chat_type="private",
                person_id="account-qq",
                canonical_person_key="xiaoxi",
                user_label="小希",
            ),
        ]
    )

    assert {item.audience_ref for item in surfaces} == {"person:xiaoxi"}
    assert {item.platform for item in surfaces} == {"kook", "qq"}
    assert all("stream_id" not in item.public_projection() for item in surfaces)


def test_unlinked_accounts_are_not_merged_by_same_display_name() -> None:
    surfaces = project_reachable_surfaces(
        [
            ReachabilityRow(
                stream_id="one",
                platform="kook",
                chat_type="private",
                person_id="account-one",
                user_label="小希",
            ),
            ReachabilityRow(
                stream_id="two",
                platform="qq",
                chat_type="private",
                person_id="account-two",
                user_label="小希",
            ),
        ]
    )
    assert len({item.audience_ref for item in surfaces}) == 2
    assert all(item.audience_ref.startswith("account:") for item in surfaces)


def test_same_raw_account_id_on_two_platforms_never_merges() -> None:
    surfaces = project_reachable_surfaces(
        [
            ReachabilityRow(
                stream_id="kook-stream",
                platform="kook",
                chat_type="private",
                person_id="same-provider-id",
            ),
            ReachabilityRow(
                stream_id="qq-stream",
                platform="qq",
                chat_type="private",
                person_id="same-provider-id",
            ),
        ]
    )

    assert len(surfaces) == 2
    assert len({item.audience_ref for item in surfaces}) == 2


def test_projection_order_is_stable_and_not_current_or_recent_order() -> None:
    rows = [
        ReachabilityRow(
            stream_id="z-stream",
            platform="qq",
            chat_type="private",
            person_id="z-account",
            user_label="Z",
        ),
        ReachabilityRow(
            stream_id="a-stream",
            platform="kook",
            chat_type="private",
            person_id="a-account",
            user_label="A",
        ),
    ]
    first = project_reachable_surfaces(rows)
    second = project_reachable_surfaces(reversed(rows))
    assert first == second
    assert not hasattr(first[0], "last_active_time")
    assert not hasattr(first[0], "is_current")


def test_groups_are_places_and_never_people() -> None:
    surfaces = project_reachable_surfaces(
        [
            ReachabilityRow(
                stream_id="group-stream",
                platform="qq",
                chat_type="group",
                group_id="123",
                group_name="始源之地",
            )
        ]
    )
    assert len(surfaces) == 1
    assert surfaces[0].audience_ref.startswith("place:")
    assert surfaces[0].chat_type == "group"


@pytest.mark.asyncio
async def test_surface_resolution_requires_exact_audience_pair(monkeypatch) -> None:
    surface = ReachableSurface(
        surface_ref="surface:exact",
        audience_ref="person:xiaoxi",
        platform="qq",
        chat_type="private",
        display_name="小希",
        stream_id="internal-stream",
    )

    async def _load() -> tuple[ReachableSurface, ...]:
        return (surface,)

    monkeypatch.setattr(
        "plugins.life_engine.initiative.reachability.load_reachable_surfaces",
        _load,
    )
    resolved = await resolve_reachable_surface(
        audience_ref="person:xiaoxi",
        surface_ref="surface:exact",
    )
    assert resolved.stream_id == "internal-stream"
    with pytest.raises(InitiativeSurfaceUnavailable):
        await resolve_reachable_surface(
            audience_ref="person:someone-else",
            surface_ref="surface:exact",
        )
    with pytest.raises(InitiativeSurfaceUnavailable):
        await resolve_reachable_surface(
            audience_ref="person:xiaoxi",
            surface_ref="surface:exa",
        )
