"""Data models for the Werewolf game plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from time import time


class Role(StrEnum):
    WEREWOLF = "werewolf"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"
    VILLAGER = "villager"


class Phase(StrEnum):
    WAITING = "waiting"
    NIGHT = "night"
    DAY = "day"
    ENDED = "ended"


ROLE_LABELS: dict[Role, str] = {
    Role.WEREWOLF: "狼人",
    Role.SEER: "预言家",
    Role.WITCH: "女巫",
    Role.HUNTER: "猎人",
    Role.VILLAGER: "村民",
}


@dataclass(slots=True)
class Player:
    user_id: str
    display_name: str
    role: Role | None = None
    alive: bool = True
    is_bot: bool = False

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, "未分配") if self.role else "未分配"


@dataclass(slots=True)
class NightState:
    wolf_target: str | None = None
    wolf_done: bool = False
    seer_done: set[str] = field(default_factory=set)
    witch_done: set[str] = field(default_factory=set)
    healed_target: str | None = None
    poisoned_target: str | None = None


@dataclass(slots=True)
class GameState:
    platform: str
    group_id: str
    group_name: str
    group_stream_id: str
    owner_id: str
    phase: Phase = Phase.WAITING
    day_number: int = 0
    players: dict[str, Player] = field(default_factory=dict)
    night: NightState = field(default_factory=NightState)
    votes: dict[str, str] = field(default_factory=dict)
    witch_heal_available: bool = True
    witch_poison_available: bool = True
    public_log: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time)
    ended_reason: str = ""

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.group_id}"

    def alive_players(self) -> list[Player]:
        return [player for player in self.players.values() if player.alive]

    def living_role_count(self, role: Role) -> int:
        return sum(
            1
            for player in self.players.values()
            if player.alive and player.role == role
        )

