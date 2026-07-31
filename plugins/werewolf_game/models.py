"""Data models for the Werewolf game plugin (v2.0).

Commercial-grade werewolf game with full role support, sheriff system,
last words, PK mechanism, and configurable board setups.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from time import time


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


class Role(StrEnum):
    # 狼人阵营
    WEREWOLF = "werewolf"
    # 神职
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"
    GUARD = "guard"
    # 平民
    VILLAGER = "villager"
    IDIOT = "idiot"


class Faction(StrEnum):
    WOLF = "wolf"
    GOOD = "good"


ROLE_LABELS: dict[Role, str] = {
    Role.WEREWOLF: "狼人",
    Role.SEER: "预言家",
    Role.WITCH: "女巫",
    Role.HUNTER: "猎人",
    Role.GUARD: "守卫",
    Role.VILLAGER: "村民",
    Role.IDIOT: "白痴",
}

ROLE_FACTION: dict[Role, Faction] = {
    Role.WEREWOLF: Faction.WOLF,
    Role.SEER: Faction.GOOD,
    Role.WITCH: Faction.GOOD,
    Role.HUNTER: Faction.GOOD,
    Role.GUARD: Faction.GOOD,
    Role.VILLAGER: Faction.GOOD,
    Role.IDIOT: Faction.GOOD,
}

# 神职列表（用于屠边判定）
DIVINE_ROLES: frozenset[Role] = frozenset({
    Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD,
})


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


class Phase(StrEnum):
    WAITING = "waiting"
    NIGHT = "night"
    DAY_BREAK = "day_break"          # 天亮结算
    SHERIFF_ELECTION = "sheriff_election"  # 警长竞选（仅第一天）
    SPEAKING = "speaking"            # 白天发言
    VOTE = "vote"                    # 投票
    PK_VOTE = "pk_vote"             # 平票 PK
    LAST_WORDS = "last_words"       # 遗言
    HUNTER_SHOT = "hunter_shot"     # 猎人开枪
    ENDED = "ended"


# ---------------------------------------------------------------------------
# Death causes (for hunter shot eligibility)
# ---------------------------------------------------------------------------


class DeathCause(StrEnum):
    WOLF_KILL = "wolf_kill"
    WITCH_POISON = "witch_poison"
    VOTED_OUT = "voted_out"
    HUNTER_SHOT = "hunter_shot"
    WOLF_SELF_DESTRUCT = "wolf_self_destruct"
    IDIOT_REVEALED = "idiot_revealed"  # 白痴翻牌（不算真正死亡）


# ---------------------------------------------------------------------------
# Win conditions
# ---------------------------------------------------------------------------


class WinRule(StrEnum):
    EXTERMINATE = "exterminate"  # 屠城：杀光所有好人
    SLAUGHTER_SIDE = "slaughter_side"  # 屠边：杀光村民 OR 杀光神职


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Player:
    user_id: str
    display_name: str
    role: Role | None = None
    alive: bool = True
    is_bot: bool = False
    seat: int = 0  # 座位号（1-based）

    # 状态标记
    death_cause: DeathCause | None = None
    idiot_revealed: bool = False  # 白痴已翻牌（可发言不可投票）
    is_sheriff: bool = False

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, "未分配") if self.role else "未分配"

    @property
    def faction(self) -> Faction:
        if self.role is None:
            return Faction.GOOD
        return ROLE_FACTION[self.role]

    @property
    def can_vote(self) -> bool:
        """白痴翻牌后不可投票。"""
        return self.alive and not self.idiot_revealed


# ---------------------------------------------------------------------------
# Night state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NightState:
    # 狼人
    wolf_target: str | None = None
    wolf_done: bool = False
    # 预言家
    seer_done: set[str] = field(default_factory=set)
    # 女巫
    witch_done: set[str] = field(default_factory=set)
    healed_target: str | None = None
    poisoned_target: str | None = None
    # 守卫
    guard_done: set[str] = field(default_factory=set)
    guard_target: str | None = None


# ---------------------------------------------------------------------------
# Game event log (for recap)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GameEvent:
    day: int
    phase: str
    event_type: str  # "kill", "save", "poison", "check", "vote", "speech", etc.
    actor_id: str = ""
    target_id: str = ""
    detail: str = ""
    timestamp: float = field(default_factory=time)


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GameState:
    platform: str
    group_id: str
    group_name: str
    group_stream_id: str
    owner_id: str

    # 基础状态
    phase: Phase = Phase.WAITING
    day_number: int = 0
    players: dict[str, Player] = field(default_factory=dict)
    night: NightState = field(default_factory=NightState)
    created_at: float = field(default_factory=time)
    ended_reason: str = ""

    # 板子配置
    board_name: str = "12人标准屠边局"
    win_rule: WinRule = WinRule.SLAUGHTER_SIDE

    # 投票
    votes: dict[str, str] = field(default_factory=dict)

    # 女巫药剂
    witch_heal_available: bool = True
    witch_poison_available: bool = True

    # 守卫记忆
    last_guard_target: str | None = None

    # 警长系统
    sheriff_id: str | None = None
    sheriff_candidates: list[str] = field(default_factory=list)
    sheriff_votes: dict[str, str] = field(default_factory=dict)
    sheriff_election_done: bool = False

    # 平票 PK
    pk_candidates: list[str] = field(default_factory=list)
    pk_round: int = 0

    # 遗言
    pending_last_words: list[str] = field(default_factory=list)  # user_ids
    last_words_given: dict[str, str] = field(default_factory=dict)  # user_id -> text

    # 猎人
    pending_hunter_shot: str | None = None  # 等待开枪的猎人 user_id

    # 发言顺序
    speaking_order: list[str] = field(default_factory=list)
    speaking_index: int = 0

    # 事件日志（复盘用）
    event_log: list[GameEvent] = field(default_factory=list)
    public_log: list[str] = field(default_factory=list)

    # 夜晚死亡记录（用于遗言规则判定）
    night_deaths: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.group_id}"

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    def living_role_count(self, role: Role) -> int:
        return sum(
            1 for p in self.players.values()
            if p.alive and p.role == role
        )

    def living_faction_count(self, faction: Faction) -> int:
        return sum(
            1 for p in self.players.values()
            if p.alive and p.faction == faction
        )

    def get_player_by_seat(self, seat: int) -> Player | None:
        for p in self.players.values():
            if p.seat == seat:
                return p
        return None

    def log_event(self, event_type: str, *, actor_id: str = "", target_id: str = "", detail: str = "") -> None:
        self.event_log.append(GameEvent(
            day=self.day_number,
            phase=self.phase.value,
            event_type=event_type,
            actor_id=actor_id,
            target_id=target_id,
            detail=detail,
        ))
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

