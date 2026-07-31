"""Board configurations for Werewolf game.

Predefined setups for different player counts with role compositions
and win conditions (exterminate vs slaughter-side).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Role, WinRule


@dataclass(frozen=True, slots=True)
class BoardConfig:
    """A single board setup definition."""

    name: str
    player_count: int
    roles: list[Role]
    win_rule: WinRule = WinRule.SLAUGHTER_SIDE
    description: str = ""

    @property
    def role_summary(self) -> str:
        """Human-readable role composition."""
        from collections import Counter
        from .models import ROLE_LABELS
        counts = Counter(self.roles)
        parts = [f"{ROLE_LABELS[r]}×{c}" for r, c in counts.items()]
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Predefined boards
# ---------------------------------------------------------------------------

BOARDS: dict[str, BoardConfig] = {}


def _register(board: BoardConfig) -> None:
    BOARDS[board.name] = board


# 6人新手局
_register(BoardConfig(
    name="6人新手局",
    player_count=6,
    roles=[
        Role.WEREWOLF, Role.WEREWOLF,
        Role.SEER, Role.WITCH,
        Role.VILLAGER, Role.VILLAGER,
    ],
    win_rule=WinRule.EXTERMINATE,
    description="适合新手入门，2狼2神2民，屠城制。",
))

# 9人标准局
_register(BoardConfig(
    name="9人标准局",
    player_count=9,
    roles=[
        Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF,
        Role.SEER, Role.WITCH, Role.HUNTER,
        Role.VILLAGER, Role.VILLAGER, Role.VILLAGER,
    ],
    win_rule=WinRule.SLAUGHTER_SIDE,
    description="3狼3神3民，屠边制。经典入门局。",
))

# 12人标准屠边局
_register(BoardConfig(
    name="12人标准屠边局",
    player_count=12,
    roles=[
        Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF,
        Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD,
        Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER,
    ],
    win_rule=WinRule.SLAUGHTER_SIDE,
    description="4狼4神4民，屠边制。最经典的竞技板子。",
))

# 12人进阶局（含白痴）
_register(BoardConfig(
    name="12人进阶局",
    player_count=12,
    roles=[
        Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF,
        Role.SEER, Role.WITCH, Role.HUNTER, Role.IDIOT,
        Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER,
    ],
    win_rule=WinRule.SLAUGHTER_SIDE,
    description="4狼3神+白痴+4民，屠边制。白痴翻牌增加容错。",
))

# 12人守卫局
_register(BoardConfig(
    name="12人守卫局",
    player_count=12,
    roles=[
        Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF,
        Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD,
        Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER,
    ],
    win_rule=WinRule.SLAUGHTER_SIDE,
    description="4狼4神（含守卫）4民，屠边制。守卫与女巫的配合是核心。",
))

# 测试局（动态生成，不注册）
_TEST_BOARD = BoardConfig(
    name="测试局",
    player_count=3,
    roles=[Role.WEREWOLF, Role.SEER, Role.VILLAGER],
    win_rule=WinRule.EXTERMINATE,
    description="3人测试用。",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_board(name: str) -> BoardConfig:
    """Get board config by name. Falls back to 12-person standard."""
    if name == "测试局":
        return _TEST_BOARD
    return BOARDS.get(name, BOARDS["12人标准屠边局"])


def list_boards() -> list[BoardConfig]:
    """List all available boards."""
    return list(BOARDS.values())


def board_list_text() -> str:
    """Human-readable board list for commands."""
    lines = ["可用板子："]
    for b in BOARDS.values():
        lines.append(f"  {b.name}（{b.player_count}人）— {b.role_summary}")
        if b.description:
            lines.append(f"    {b.description}")
    return "\n".join(lines)
