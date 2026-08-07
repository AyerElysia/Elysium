"""Stable public schemas for P3-10 tabletop Werewolf rooms."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .common import StrictModel, TimestampedModel, VersionedModel


class TabletopGameDefinition(StrictModel):
    game_type: Literal["werewolf"] = "werewolf"
    rules_version: Literal["werewolf.v2"] = "werewolf.v2"
    capability_status: Literal["validated"] = "validated"
    boards: tuple[dict[str, Any], ...]


class TabletopGamesResponse(StrictModel):
    games: tuple[TabletopGameDefinition, ...]


class TabletopRoomCreateRequest(VersionedModel):
    game_type: Literal["werewolf"] = "werewolf"
    board_name: str = Field(default="12人标准屠边局", min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    platform: str = Field(default="app", min_length=1, max_length=80)
    group_id: str = Field(default="", max_length=200)
    group_name: str = Field(default="", max_length=200)
    group_stream_id: str = Field(default="", max_length=200)


class TabletopJoinRequest(VersionedModel):
    display_name: str = Field(min_length=1, max_length=200)
    expected_revision: int | None = Field(default=None, ge=1)


class TabletopRevisionRequest(VersionedModel):
    expected_revision: int | None = Field(default=None, ge=1)


class TabletopEndRequest(TabletopRevisionRequest):
    reason: str = Field(default="房主结束了本局。", min_length=1, max_length=500)


TabletopActionType = Literal[
    "vote",
    "speech",
    "next_speaker",
    "self_destruct",
    "campaign",
    "withdraw",
    "sheriff_vote",
    "sheriff_transfer",
    "sheriff_destroy",
    "last_words",
    "pass",
    "hunter_shot",
    "kill",
    "check",
    "heal",
    "poison",
    "guard",
]


class TabletopActionRequest(VersionedModel):
    action_type: TabletopActionType
    target_actor_id: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=8000)
    expected_revision: int | None = Field(default=None, ge=1)


class TabletopRoomResponse(StrictModel):
    room: dict[str, Any]


class TabletopActionResponse(StrictModel):
    result: dict[str, Any]
    room: dict[str, Any]


class TabletopEvent(TimestampedModel):
    event_id: str
    room_id: str
    sequence: int
    event_type: str
    actor_id: str
    visibility: Literal["public", "players"]
    payload: dict[str, Any]
    occurred_at: datetime


class TabletopEventPage(StrictModel):
    events: tuple[TabletopEvent, ...]
    next_sequence: int
    has_more: bool


__all__ = [
    "TabletopActionRequest",
    "TabletopActionResponse",
    "TabletopEndRequest",
    "TabletopEvent",
    "TabletopEventPage",
    "TabletopGameDefinition",
    "TabletopGamesResponse",
    "TabletopJoinRequest",
    "TabletopRevisionRequest",
    "TabletopRoomCreateRequest",
    "TabletopRoomResponse",
]
