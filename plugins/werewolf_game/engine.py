"""Deterministic Werewolf rule engine.

This module deliberately contains no LLM calls. Hidden referee state stays here;
callers must use the player-view helpers instead of reading raw GameState.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass

from .models import GameState, NightState, Phase, Player, Role, ROLE_LABELS


MIN_PLAYERS = 6


@dataclass(slots=True)
class ActionResult:
    ok: bool
    message: str
    public_messages: list[str] | None = None
    game_ended: bool = False


class WerewolfEngine:
    """Small but complete Werewolf rule engine for QQ group play."""

    def create_game(
        self,
        *,
        platform: str,
        group_id: str,
        group_name: str,
        group_stream_id: str,
        owner_id: str,
    ) -> GameState:
        return GameState(
            platform=platform,
            group_id=group_id,
            group_name=group_name,
            group_stream_id=group_stream_id,
            owner_id=owner_id,
        )

    def add_player(
        self,
        game: GameState,
        *,
        user_id: str,
        display_name: str,
        is_bot: bool = False,
    ) -> ActionResult:
        if game.phase != Phase.WAITING:
            return ActionResult(False, "本局已经开始，不能再加入。")
        if user_id in game.players:
            return ActionResult(False, f"{display_name} 已经在房间里。")
        game.players[user_id] = Player(
            user_id=str(user_id),
            display_name=display_name or str(user_id),
            is_bot=is_bot,
        )
        return ActionResult(True, f"{display_name} 加入了房间。当前 {len(game.players)} 人。")

    def remove_player(self, game: GameState, user_id: str) -> ActionResult:
        if game.phase != Phase.WAITING:
            return ActionResult(False, "本局已经开始，不能退出。")
        player = game.players.pop(user_id, None)
        if not player:
            return ActionResult(False, "你还没有加入本局。")
        return ActionResult(True, f"{player.display_name} 已退出房间。")

    def start_game(self, game: GameState, *, rng: random.Random | None = None) -> ActionResult:
        if game.phase != Phase.WAITING:
            return ActionResult(False, "本局已经开始。")
        if len(game.players) < MIN_PLAYERS:
            return ActionResult(False, f"至少需要 {MIN_PLAYERS} 名玩家才能开始。")

        roles = self._roles_for_count(len(game.players))
        shuffler = rng or random.SystemRandom()
        shuffler.shuffle(roles)
        for player, role in zip(game.players.values(), roles, strict=True):
            player.role = role
            player.alive = True

        game.phase = Phase.NIGHT
        game.day_number = 1
        game.night = NightState()
        game.votes.clear()
        game.public_log.append("游戏开始，进入第 1 个夜晚。")
        return ActionResult(
            True,
            "身份已分配，游戏开始。",
            public_messages=["狼人杀开始。请所有玩家查看私聊身份，夜晚行动请私聊我。"],
        )

    def public_status(self, game: GameState) -> str:
        phase_label = {
            Phase.WAITING: "等待加入",
            Phase.NIGHT: f"第 {game.day_number} 夜",
            Phase.DAY: f"第 {game.day_number} 天",
            Phase.ENDED: "已结束",
        }[game.phase]
        lines = [f"狼人杀状态：{phase_label}", "玩家："]
        for index, player in enumerate(game.players.values(), start=1):
            state = "存活" if player.alive else "出局"
            lines.append(f"{index}. {player.display_name}（{state}）")
        if game.phase == Phase.WAITING:
            lines.append(f"人数：{len(game.players)}/{MIN_PLAYERS}+")
        if game.phase == Phase.ENDED and game.ended_reason:
            lines.append(f"结局：{game.ended_reason}")
        return "\n".join(lines)

    def player_view(self, game: GameState, user_id: str) -> str:
        player = game.players.get(str(user_id))
        if not player:
            return "你还没有加入这局狼人杀。"

        lines = [
            f"当前阶段：{self._phase_label(game)}",
            f"你的身份：{player.role_label}",
            f"你的状态：{'存活' if player.alive else '出局'}",
            "公开玩家列表：",
        ]
        for index, item in enumerate(game.players.values(), start=1):
            lines.append(f"{index}. {item.display_name}（{'存活' if item.alive else '出局'}）")

        if player.role == Role.WEREWOLF:
            teammates = [
                item.display_name
                for item in game.players.values()
                if item.role == Role.WEREWOLF and item.user_id != player.user_id
            ]
            lines.append("狼队友：" + ("、".join(teammates) if teammates else "无"))
        if player.role == Role.WITCH:
            potions = []
            if game.witch_heal_available:
                potions.append("解药")
            if game.witch_poison_available:
                potions.append("毒药")
            lines.append("剩余药剂：" + ("、".join(potions) if potions else "无"))
        return "\n".join(lines)

    def role_notice(self, game: GameState, user_id: str) -> str:
        player = game.players[user_id]
        lines = [f"你的身份是：{player.role_label}。"]
        if player.role == Role.WEREWOLF:
            wolves = [
                item.display_name
                for item in game.players.values()
                if item.role == Role.WEREWOLF and item.user_id != user_id
            ]
            lines.append("你的狼队友：" + ("、".join(wolves) if wolves else "无"))
            lines.append("夜晚私聊发送：/狼人杀 杀 编号")
        elif player.role == Role.SEER:
            lines.append("夜晚私聊发送：/狼人杀 验 编号")
        elif player.role == Role.WITCH:
            lines.append("夜晚私聊发送：/狼人杀 救 或 /狼人杀 毒 编号，也可以 /狼人杀 跳过")
        else:
            lines.append("夜晚无需行动；白天在群里 /狼人杀 投票 编号")
        return "\n".join(lines)

    def night_action(
        self,
        game: GameState,
        *,
        actor_id: str,
        action: str,
        target_id: str | None = None,
    ) -> ActionResult:
        if game.phase != Phase.NIGHT:
            return ActionResult(False, "现在不是夜晚行动阶段。")
        actor = game.players.get(str(actor_id))
        if not actor or not actor.alive:
            return ActionResult(False, "你不是本局存活玩家。")

        action = self._normalize_action(action)
        target = game.players.get(target_id or "") if target_id else None
        if target_id and (not target or not target.alive):
            return ActionResult(False, "目标不存在或已经出局。")

        if action == "kill":
            if actor.role != Role.WEREWOLF:
                return ActionResult(False, "只有狼人能在夜晚刀人。")
            game.night.wolf_target = target.user_id if target else None
            game.night.wolf_done = True
            return self._maybe_resolve_night(game, "狼队行动已记录。")

        if action == "check":
            if actor.role != Role.SEER:
                return ActionResult(False, "只有预言家能验人。")
            if actor.user_id in game.night.seer_done:
                return ActionResult(False, "你本夜已经查验过。")
            if not target:
                return ActionResult(False, "验人需要指定目标。")
            game.night.seer_done.add(actor.user_id)
            alignment = "狼人" if target.role == Role.WEREWOLF else "好人"
            return self._maybe_resolve_night(game, f"查验结果：{target.display_name} 是{alignment}。")

        if action == "heal":
            if actor.role != Role.WITCH:
                return ActionResult(False, "只有女巫能用药。")
            if actor.user_id in game.night.witch_done:
                return ActionResult(False, "你本夜已经行动过。")
            if not game.witch_heal_available:
                return ActionResult(False, "解药已经用过。")
            if not game.night.wolf_target:
                return ActionResult(False, "今晚暂时没有可救目标；可以稍后再救、使用毒药，或跳过。")
            game.night.healed_target = game.night.wolf_target
            game.witch_heal_available = False
            game.night.witch_done.add(actor.user_id)
            return self._maybe_resolve_night(game, "解药行动已记录。")

        if action == "poison":
            if actor.role != Role.WITCH:
                return ActionResult(False, "只有女巫能用药。")
            if actor.user_id in game.night.witch_done:
                return ActionResult(False, "你本夜已经行动过。")
            if not game.witch_poison_available:
                return ActionResult(False, "毒药已经用过。")
            if not target:
                return ActionResult(False, "毒药需要指定目标。")
            game.night.poisoned_target = target.user_id
            game.witch_poison_available = False
            game.night.witch_done.add(actor.user_id)
            return self._maybe_resolve_night(game, "毒药行动已记录。")

        if action == "pass":
            if actor.role == Role.WEREWOLF:
                game.night.wolf_target = None
                game.night.wolf_done = True
            elif actor.role == Role.SEER:
                game.night.seer_done.add(actor.user_id)
            elif actor.role == Role.WITCH:
                game.night.witch_done.add(actor.user_id)
            return self._maybe_resolve_night(game, "已跳过本次夜晚行动。")

        return ActionResult(False, "未知夜晚行动。")

    def vote(self, game: GameState, *, voter_id: str, target_id: str | None) -> ActionResult:
        if game.phase != Phase.DAY:
            return ActionResult(False, "现在不是白天投票阶段。")
        voter = game.players.get(str(voter_id))
        if not voter or not voter.alive:
            return ActionResult(False, "只有本局存活玩家可以投票。")
        if not target_id:
            game.votes.pop(voter.user_id, None)
            return ActionResult(True, "已取消投票。")
        target = game.players.get(target_id)
        if not target or not target.alive:
            return ActionResult(False, "投票目标不存在或已经出局。")
        game.votes[voter.user_id] = target.user_id

        alive_count = len(game.alive_players())
        if len(game.votes) < alive_count:
            return ActionResult(
                True,
                f"已投给 {target.display_name}。当前 {len(game.votes)}/{alive_count} 票。",
            )

        counts = Counter(game.votes.values())
        top_count = max(counts.values())
        top_targets = [uid for uid, count in counts.items() if count == top_count]
        public_messages: list[str] = []
        if len(top_targets) == 1:
            eliminated = game.players[top_targets[0]]
            eliminated.alive = False
            public_messages.append(f"投票结束，{eliminated.display_name} 出局。")
        else:
            public_messages.append("投票平票，今天无人出局。")
        game.votes.clear()

        winner = self._check_winner(game)
        if winner:
            game.phase = Phase.ENDED
            game.ended_reason = winner
            public_messages.append(winner)
            return ActionResult(True, "投票结算完成，游戏结束。", public_messages, True)

        game.phase = Phase.NIGHT
        game.day_number += 1
        game.night = NightState()
        public_messages.append(f"进入第 {game.day_number} 个夜晚，请相关玩家私聊行动。")
        return ActionResult(True, "投票结算完成，进入夜晚。", public_messages)

    def resolve_target(self, game: GameState, raw: str) -> str | None:
        text = str(raw or "").strip().lstrip("@")
        if not text or text in {"0", "弃票", "取消", "跳过", "pass"}:
            return None
        players = list(game.players.values())
        if text.isdigit():
            index = int(text) - 1
            if 0 <= index < len(players):
                return players[index].user_id
        if text in game.players:
            return text
        for player in players:
            if text == player.display_name or text in player.display_name:
                return player.user_id
        return None

    def _roles_for_count(self, count: int) -> list[Role]:
        wolves = 2 if count < 8 else 3
        roles = [Role.WEREWOLF] * wolves + [Role.SEER, Role.WITCH]
        if count >= 7:
            roles.append(Role.HUNTER)
        roles.extend([Role.VILLAGER] * (count - len(roles)))
        return roles

    def _phase_label(self, game: GameState) -> str:
        if game.phase == Phase.NIGHT:
            return f"第 {game.day_number} 夜"
        if game.phase == Phase.DAY:
            return f"第 {game.day_number} 天"
        return {
            Phase.WAITING: "等待加入",
            Phase.ENDED: "已结束",
        }[game.phase]

    def _normalize_action(self, action: str) -> str:
        table = {
            "杀": "kill",
            "刀": "kill",
            "kill": "kill",
            "验": "check",
            "查验": "check",
            "check": "check",
            "救": "heal",
            "heal": "heal",
            "毒": "poison",
            "poison": "poison",
            "跳过": "pass",
            "过": "pass",
            "pass": "pass",
        }
        return table.get(str(action or "").strip().lower(), str(action or "").strip().lower())

    def _maybe_resolve_night(self, game: GameState, private_message: str) -> ActionResult:
        if not self._night_ready(game):
            return ActionResult(True, private_message)

        killed: list[str] = []
        if game.night.wolf_target and game.night.wolf_target != game.night.healed_target:
            killed.append(game.night.wolf_target)
        if game.night.poisoned_target:
            killed.append(game.night.poisoned_target)

        dead_names: list[str] = []
        for user_id in dict.fromkeys(killed):
            player = game.players.get(user_id)
            if player and player.alive:
                player.alive = False
                dead_names.append(player.display_name)

        game.phase = Phase.DAY
        game.votes.clear()
        public_messages = [
            "天亮了。昨夜无人出局。" if not dead_names else f"天亮了。昨夜出局：{'、'.join(dead_names)}。"
        ]

        winner = self._check_winner(game)
        if winner:
            game.phase = Phase.ENDED
            game.ended_reason = winner
            public_messages.append(winner)
            return ActionResult(True, private_message, public_messages, True)

        public_messages.append("进入白天讨论和投票阶段。存活玩家可在群里发送：/狼人杀 投票 编号")
        return ActionResult(True, private_message, public_messages)

    def _night_ready(self, game: GameState) -> bool:
        alive = game.alive_players()
        has_wolf = any(player.role == Role.WEREWOLF for player in alive)
        if has_wolf and not game.night.wolf_done:
            return False
        for player in alive:
            if player.role == Role.SEER and player.user_id not in game.night.seer_done:
                return False
            if player.role == Role.WITCH and player.user_id not in game.night.witch_done:
                return False
        return True

    def _check_winner(self, game: GameState) -> str | None:
        wolves = game.living_role_count(Role.WEREWOLF)
        good = len(game.alive_players()) - wolves
        if wolves <= 0:
            return "游戏结束，好人阵营胜利。"
        if wolves >= good:
            return "游戏结束，狼人阵营胜利。"
        return None
