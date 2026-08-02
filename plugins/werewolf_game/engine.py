"""Deterministic Werewolf rule engine v2.0.

Commercial-grade rules: sheriff election, last words, hunter shot,
guard, idiot, PK vote, wolf self-destruct, configurable boards.

This module deliberately contains NO LLM calls. Hidden referee state stays
here; callers must use player-view helpers instead of reading raw GameState.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass

from .boards import get_board
from .models import (
    DIVINE_ROLES,
    DeathCause,
    GameState,
    NightState,
    Phase,
    Player,
    Role,
    WinRule,
)

MIN_PLAYERS = 6
MIN_TEST_PLAYERS = 3


@dataclass(slots=True)
class ActionResult:
    ok: bool
    message: str
    public_messages: list[str] | None = None
    game_ended: bool = False
    private_messages: dict[str, str] | None = None  # user_id -> msg


class WerewolfEngine:
    """Commercial-grade Werewolf rule engine for QQ group play."""

    # ------------------------------------------------------------------
    # Game lifecycle
    # ------------------------------------------------------------------

    def create_game(
        self,
        *,
        platform: str,
        group_id: str,
        group_name: str,
        group_stream_id: str,
        owner_id: str,
        board_name: str = "12人标准屠边局",
    ) -> GameState:
        board = get_board(board_name)
        return GameState(
            platform=platform,
            group_id=group_id,
            group_name=group_name,
            group_stream_id=group_stream_id,
            owner_id=owner_id,
            board_name=board.name,
            win_rule=board.win_rule,
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
        seat = len(game.players) + 1
        game.players[user_id] = Player(
            user_id=str(user_id),
            display_name=display_name or str(user_id),
            is_bot=is_bot,
            seat=seat,
        )
        return ActionResult(True, f"{display_name} 加入了房间（{seat} 号位）。当前 {len(game.players)} 人。")

    def remove_player(self, game: GameState, user_id: str) -> ActionResult:
        if game.phase != Phase.WAITING:
            return ActionResult(False, "本局已经开始，不能退出。")
        player = game.players.pop(user_id, None)
        if not player:
            return ActionResult(False, "你还没有加入本局。")
        # 重新编号
        for i, p in enumerate(game.players.values(), 1):
            p.seat = i
        return ActionResult(True, f"{player.display_name} 已退出房间。")

    def start_game(self, game: GameState, *, rng: random.Random | None = None) -> ActionResult:
        board = get_board(game.board_name)
        min_players = board.player_count
        if game.phase != Phase.WAITING:
            return ActionResult(False, "本局已经开始。")
        if len(game.players) < min_players:
            return ActionResult(False, f"板子「{board.name}」需要 {min_players} 名玩家，当前 {len(game.players)} 人。")

        roles = board.roles[:]
        shuffler = rng or random.SystemRandom()
        shuffler.shuffle(roles)
        for i, (player, role) in enumerate(zip(game.players.values(), roles, strict=True), 1):
            player.role = role
            player.alive = True
            player.seat = i

        game.phase = Phase.NIGHT
        game.day_number = 1
        game.night = NightState()
        game.votes.clear()
        game.public_log.append("游戏开始，进入第 1 个夜晚。")
        game.log_event("game_start", detail=f"板子={board.name}, 人数={len(game.players)}")
        return ActionResult(
            True,
            "身份已分配，游戏开始。",
            public_messages=[
                f"狼人杀开始（{board.name}）。请所有玩家查看私聊身份。\n"
                "下一步：有夜间技能的玩家请在私聊里行动或跳过。"
            ],
        )

    def start_test_game(self, game: GameState, *, rng: random.Random | None = None) -> ActionResult:
        """3人测试局。"""
        if game.phase != Phase.WAITING:
            return ActionResult(False, "本局已经开始。")
        if len(game.players) < MIN_TEST_PLAYERS:
            return ActionResult(False, f"至少需要 {MIN_TEST_PLAYERS} 名玩家才能测试开始。")
        # 测试局强制使用简单配置
        game.board_name = "测试局"
        game.win_rule = WinRule.EXTERMINATE
        roles = [Role.WEREWOLF, Role.SEER] + [Role.VILLAGER] * (len(game.players) - 2)
        shuffler = rng or random.SystemRandom()
        shuffler.shuffle(roles)
        for i, (player, role) in enumerate(zip(game.players.values(), roles, strict=True), 1):
            player.role = role
            player.alive = True
            player.seat = i
        game.phase = Phase.NIGHT
        game.day_number = 1
        game.night = NightState()
        game.votes.clear()
        game.log_event("game_start", detail="测试局")
        return ActionResult(
            True,
            "测试局开始。",
            public_messages=[
                "测试狼人杀开始。请查看私聊身份。\n"
                "下一步：有夜间技能的玩家请在私聊里行动或跳过。"
            ],
        )

    # ------------------------------------------------------------------
    # Night actions
    # ------------------------------------------------------------------

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
            game.log_event("wolf_kill", actor_id=actor.user_id, target_id=target.user_id if target else "")
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
            game.log_event("seer_check", actor_id=actor.user_id, target_id=target.user_id, detail=alignment)
            return self._maybe_resolve_night(game, f"查验结果：{target.display_name}（{target.seat}号）是{alignment}。")

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
            game.log_event("witch_heal", actor_id=actor.user_id, target_id=game.night.wolf_target)
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
            game.log_event("witch_poison", actor_id=actor.user_id, target_id=target.user_id)
            return self._maybe_resolve_night(game, "毒药行动已记录。")

        if action == "guard":
            if actor.role != Role.GUARD:
                return ActionResult(False, "只有守卫能守护。")
            if actor.user_id in game.night.guard_done:
                return ActionResult(False, "你本夜已经行动过。")
            if target and target.user_id == actor.user_id:
                return ActionResult(False, "守卫不能守护自己。")
            if target and target.user_id == game.last_guard_target:
                return ActionResult(False, f"不能连续两晚守护同一人（{target.display_name}）。")
            game.night.guard_target = target.user_id if target else None
            game.night.guard_done.add(actor.user_id)
            game.log_event("guard_protect", actor_id=actor.user_id, target_id=target.user_id if target else "")
            return self._maybe_resolve_night(game, "守护行动已记录。")

        if action == "pass":
            if actor.role == Role.WEREWOLF:
                game.night.wolf_target = None
                game.night.wolf_done = True
            elif actor.role == Role.SEER:
                game.night.seer_done.add(actor.user_id)
            elif actor.role == Role.WITCH:
                game.night.witch_done.add(actor.user_id)
            elif actor.role == Role.GUARD:
                game.night.guard_target = None
                game.night.guard_done.add(actor.user_id)
            return self._maybe_resolve_night(game, "已跳过本次夜晚行动。")

        return ActionResult(False, "未知夜晚行动。")

    # ------------------------------------------------------------------
    # Night resolution
    # ------------------------------------------------------------------

    def _maybe_resolve_night(self, game: GameState, private_message: str) -> ActionResult:
        if not self._night_ready(game):
            return ActionResult(True, private_message)
        return self._resolve_night(game, private_message)

    def _resolve_night(self, game: GameState, private_message: str) -> ActionResult:
        killed: list[tuple[str, DeathCause]] = []

        # 狼刀（守卫优先挡住）
        if game.night.wolf_target:
            if game.night.guard_target == game.night.wolf_target:
                pass  # 守卫挡住
            elif game.night.healed_target == game.night.wolf_target:
                pass  # 女巫救了
            else:
                killed.append((game.night.wolf_target, DeathCause.WOLF_KILL))

        # 女巫毒（守卫不能挡毒）
        if game.night.poisoned_target:
            # 守卫守住了毒目标 → 毒依然生效（守卫只挡狼刀）
            killed.append((game.night.poisoned_target, DeathCause.WITCH_POISON))

        # 执行死亡
        dead_names: list[str] = []
        game.night_deaths = []
        for user_id, cause in killed:
            player = game.players.get(user_id)
            if player and player.alive:
                player.alive = False
                player.death_cause = cause
                dead_names.append(player.display_name)
                game.night_deaths.append(user_id)
                game.log_event("death", target_id=user_id, detail=cause.value)

        # 更新守卫记忆
        game.last_guard_target = game.night.guard_target

        # 进入白天
        game.phase = Phase.DAY_BREAK
        game.votes.clear()

        public_messages: list[str] = []
        if not dead_names:
            public_messages.append("天亮了。昨夜是平安夜，无人出局。")
        else:
            public_messages.append(f"天亮了。昨夜出局：{'、'.join(dead_names)}。")

        # 检查胜负
        winner = self._check_winner(game)
        if winner:
            game.phase = Phase.ENDED
            game.ended_reason = winner
            public_messages.append(winner)
            return ActionResult(True, private_message, public_messages, True)

        # 遗言规则：第一晚死者有遗言；第二晚起单死有遗言多死无
        if game.night_deaths and self._should_allow_last_words(game):
            game.pending_last_words = list(game.night_deaths)
            game.phase = Phase.LAST_WORDS
            public_messages.append("死者可以发表遗言。")
        else:
            self._advance_to_day_phase(game, public_messages)

        return ActionResult(True, private_message, public_messages)

    def _should_allow_last_words(self, game: GameState) -> bool:
        if game.day_number == 1:
            return True
        return len(game.night_deaths) == 1

    def _advance_to_day_phase(self, game: GameState, public_messages: list[str]) -> None:
        """从白天结算后推进到发言/竞选阶段。"""
        # 第一天且未选警长 → 进入竞选
        if game.day_number == 1 and not game.sheriff_election_done:
            game.phase = Phase.SHERIFF_ELECTION
            game.sheriff_candidates = []
            game.sheriff_votes = {}
            public_messages.append("进入警长竞选阶段。发送 /狼人杀 竞选 报名。")
        else:
            game.phase = Phase.SPEAKING
            self._setup_speaking_order(game)
            public_messages.append(f"进入第 {game.day_number} 天讨论阶段。")

    # ------------------------------------------------------------------
    # Last words
    # ------------------------------------------------------------------

    def submit_last_words(self, game: GameState, *, user_id: str, text: str) -> ActionResult:
        if game.phase != Phase.LAST_WORDS:
            return ActionResult(False, "现在不是遗言阶段。")
        if user_id not in game.pending_last_words:
            return ActionResult(False, "你不需要发表遗言。")
        game.last_words_given[user_id] = text
        game.pending_last_words.remove(user_id)
        player = game.players.get(user_id)
        name = player.display_name if player else user_id
        game.log_event("last_words", actor_id=user_id, detail=text[:100])

        public_messages = [f"【遗言】{name}：{text}"]

        if not game.pending_last_words:
            self._after_last_words(game, public_messages)
        return ActionResult(True, "遗言已发表。", public_messages)

    def skip_last_words(self, game: GameState, *, user_id: str) -> ActionResult:
        if game.phase != Phase.LAST_WORDS:
            return ActionResult(False, "现在不是遗言阶段。")
        if user_id not in game.pending_last_words:
            return ActionResult(False, "你不需要发表遗言。")
        game.pending_last_words.remove(user_id)
        public_messages: list[str] = []
        if not game.pending_last_words:
            self._after_last_words(game, public_messages)
        return ActionResult(True, "已跳过遗言。", public_messages)

    def _after_last_words(self, game: GameState, public_messages: list[str]) -> None:
        """遗言结束后检查猎人开枪，然后推进。"""
        hunter = self._get_pending_hunter(game)
        if hunter:
            game.phase = Phase.HUNTER_SHOT
            game.pending_hunter_shot = hunter.user_id
            public_messages.append(f"{hunter.display_name} 是猎人，可以开枪带走一人！")
        else:
            self._advance_to_day_phase(game, public_messages)

    # ------------------------------------------------------------------
    # Hunter shot
    # ------------------------------------------------------------------

    def hunter_shot(self, game: GameState, *, hunter_id: str, target_id: str | None) -> ActionResult:
        if game.phase != Phase.HUNTER_SHOT:
            return ActionResult(False, "现在不是猎人开枪阶段。")
        if hunter_id != game.pending_hunter_shot:
            return ActionResult(False, "你不是当前等待开枪的猎人。")
        hunter = game.players.get(hunter_id)
        if not hunter:
            return ActionResult(False, "猎人不存在。")

        public_messages: list[str] = []

        if not target_id:
            # 放弃开枪
            public_messages.append(f"{hunter.display_name}（猎人）选择不开枪。")
            game.log_event("hunter_pass", actor_id=hunter_id)
        else:
            target = game.players.get(target_id)
            if not target or not target.alive:
                return ActionResult(False, "目标不存在或已经出局。")
            target.alive = False
            target.death_cause = DeathCause.HUNTER_SHOT
            public_messages.append(f"砰！{hunter.display_name}（猎人）开枪带走了 {target.display_name}！")
            game.log_event("hunter_shot", actor_id=hunter_id, target_id=target_id)

            # 猎人带走的人如果是猎人 → 连锁（标准规则不连锁，这里不实现）
            # 检查胜负
            winner = self._check_winner(game)
            if winner:
                game.phase = Phase.ENDED
                game.ended_reason = winner
                public_messages.append(winner)
                game.pending_hunter_shot = None
                return ActionResult(True, "猎人开枪结算完成。", public_messages, True)

        game.pending_hunter_shot = None
        self._advance_to_day_phase(game, public_messages)
        return ActionResult(True, "猎人开枪结算完成。", public_messages)

    def _get_pending_hunter(self, game: GameState) -> Player | None:
        """检查本轮死亡中是否有可以开枪的猎人。"""
        for uid in game.night_deaths:
            player = game.players.get(uid)
            if (
                player
                and player.role == Role.HUNTER
                and player.death_cause in (DeathCause.WOLF_KILL, DeathCause.VOTED_OUT)
            ):
                return player
        return None

    # ------------------------------------------------------------------
    # Sheriff election
    # ------------------------------------------------------------------

    def sheriff_register(self, game: GameState, *, user_id: str) -> ActionResult:
        if game.phase != Phase.SHERIFF_ELECTION:
            return ActionResult(False, "现在不是警长竞选阶段。")
        player = game.players.get(user_id)
        if not player or not player.alive:
            return ActionResult(False, "只有存活玩家可以竞选。")
        if user_id in game.sheriff_candidates:
            return ActionResult(False, "你已经报名了。")
        game.sheriff_candidates.append(user_id)
        return ActionResult(True, f"{player.display_name} 报名竞选警长。当前 {len(game.sheriff_candidates)} 人参选。")

    def sheriff_withdraw(self, game: GameState, *, user_id: str) -> ActionResult:
        if game.phase != Phase.SHERIFF_ELECTION:
            return ActionResult(False, "现在不是警长竞选阶段。")
        if user_id not in game.sheriff_candidates:
            return ActionResult(False, "你没有报名竞选。")
        game.sheriff_candidates.remove(user_id)
        return ActionResult(True, "已退出竞选。")

    def sheriff_vote(self, game: GameState, *, voter_id: str, target_id: str | None) -> ActionResult:
        if game.phase != Phase.SHERIFF_ELECTION:
            return ActionResult(False, "现在不是警长竞选阶段。")
        voter = game.players.get(voter_id)
        if not voter or not voter.alive:
            return ActionResult(False, "只有存活玩家可以投票。")
        if target_id and target_id not in game.sheriff_candidates:
            return ActionResult(False, "只能投给已报名的候选人。")

        if target_id:
            game.sheriff_votes[voter_id] = target_id
        else:
            game.sheriff_votes.pop(voter_id, None)  # 弃票

        alive_count = len(game.alive_players())
        if len(game.sheriff_votes) < alive_count:
            target_name = ""
            if target_id:
                t = game.players.get(target_id)
                target_name = t.display_name if t else target_id
            return ActionResult(True, f"已投给 {target_name}。当前 {len(game.sheriff_votes)}/{alive_count} 票。")

        # 所有存活玩家已投票 → 结算
        return self._resolve_sheriff_election(game)

    def _resolve_sheriff_election(self, game: GameState) -> ActionResult:
        game.sheriff_election_done = True
        public_messages: list[str] = []

        if not game.sheriff_votes:
            public_messages.append("无人获得选票，本局没有警长。")
            game.phase = Phase.SPEAKING
            self._setup_speaking_order(game)
            return ActionResult(True, "警长竞选结束。", public_messages)

        counts = Counter(game.sheriff_votes.values())
        top_count = max(counts.values())
        top_candidates = [uid for uid, c in counts.items() if c == top_count]

        if len(top_candidates) == 1:
            sheriff = game.players[top_candidates[0]]
            sheriff.is_sheriff = True
            game.sheriff_id = sheriff.user_id
            public_messages.append(f"🎖️ {sheriff.display_name}（{sheriff.seat}号）当选警长！")
            game.log_event("sheriff_elected", actor_id=sheriff.user_id)
        else:
            names = "、".join(game.players[uid].display_name for uid in top_candidates)
            public_messages.append(f"警长竞选平票（{names}），本局没有警长。")

        game.phase = Phase.SPEAKING
        self._setup_speaking_order(game)
        public_messages.append("进入讨论阶段。")
        return ActionResult(True, "警长竞选结束。", public_messages)

    def sheriff_transfer(self, game: GameState, *, sheriff_id: str, target_id: str) -> ActionResult:
        """警长移交警徽。"""
        if game.sheriff_id != sheriff_id:
            return ActionResult(False, "你不是警长。")
        target = game.players.get(target_id)
        if not target or not target.alive:
            return ActionResult(False, "目标不存在或已出局。")
        old_sheriff = game.players.get(sheriff_id)
        if old_sheriff:
            old_sheriff.is_sheriff = False
        target.is_sheriff = True
        game.sheriff_id = target.user_id
        game.log_event("sheriff_transfer", actor_id=sheriff_id, target_id=target_id)
        return ActionResult(True, f"警徽已移交给 {target.display_name}。",
                          [f"🎖️ {target.display_name} 继承了警徽。"])

    def sheriff_destroy(self, game: GameState, *, sheriff_id: str) -> ActionResult:
        """警长撕毁警徽。"""
        if game.sheriff_id != sheriff_id:
            return ActionResult(False, "你不是警长。")
        old_sheriff = game.players.get(sheriff_id)
        if old_sheriff:
            old_sheriff.is_sheriff = False
        game.sheriff_id = None
        game.log_event("sheriff_destroyed", actor_id=sheriff_id)
        return ActionResult(True, "警徽已撕毁，本局不再有警长。", ["警徽被撕毁了。"])

    # ------------------------------------------------------------------
    # Day: speaking
    # ------------------------------------------------------------------

    def _setup_speaking_order(self, game: GameState) -> None:
        """设置发言顺序。"""
        alive = game.alive_players()
        if game.sheriff_id and game.sheriff_id in game.players:
            # 警长决定顺序：从警长下一位开始
            sheriff_seat = game.players[game.sheriff_id].seat
            ordered = sorted(alive, key=lambda p: p.seat)
            # 从警长后一位开始
            start_idx = 0
            for i, p in enumerate(ordered):
                if p.seat > sheriff_seat:
                    start_idx = i
                    break
            game.speaking_order = [p.user_id for p in ordered[start_idx:]] + [p.user_id for p in ordered[:start_idx]]
        else:
            # 随机起始
            ordered = sorted(alive, key=lambda p: p.seat)
            game.speaking_order = [p.user_id for p in ordered]
        game.speaking_index = 0

    def get_current_speaker(self, game: GameState) -> Player | None:
        if game.phase != Phase.SPEAKING:
            return None
        if game.speaking_index >= len(game.speaking_order):
            return None
        uid = game.speaking_order[game.speaking_index]
        return game.players.get(uid)

    def advance_speaker(self, game: GameState) -> ActionResult:
        """推进到下一位发言者，全部发完进入投票。"""
        if game.phase != Phase.SPEAKING:
            return ActionResult(False, "现在不是发言阶段。")
        game.speaking_index += 1
        if game.speaking_index >= len(game.speaking_order):
            game.phase = Phase.VOTE
            game.votes.clear()
            return ActionResult(True, "发言结束，进入投票阶段。",
                              ["发言结束！请所有存活玩家投票：/狼人杀 投票 编号"])
        speaker = self.get_current_speaker(game)
        name = speaker.display_name if speaker else "?"
        return ActionResult(True, f"轮到 {name} 发言。", [f"请 {name}（{speaker.seat}号）发言。"] if speaker else [])

    def record_speech(self, game: GameState, *, user_id: str, text: str) -> ActionResult:
        """记录结构化发言。"""
        if game.phase != Phase.SPEAKING:
            return ActionResult(False, "现在不是发言阶段。")
        player = game.players.get(user_id)
        if not player or not player.alive:
            return ActionResult(False, "只有存活玩家可以发言。")
        game.log_event("speech", actor_id=user_id, detail=text[:200])
        return ActionResult(True, "发言已记录。")

    # ------------------------------------------------------------------
    # Day: vote
    # ------------------------------------------------------------------

    def vote(self, game: GameState, *, voter_id: str, target_id: str | None) -> ActionResult:
        if game.phase not in (Phase.VOTE, Phase.PK_VOTE):
            return ActionResult(False, "现在不是投票阶段。")
        voter = game.players.get(str(voter_id))
        if not voter or not voter.can_vote:
            return ActionResult(False, "你没有投票权。")

        if not target_id:
            game.votes.pop(voter.user_id, None)
            return ActionResult(True, "已取消投票（弃票）。")

        # PK 阶段只能投 PK 台上的人
        if game.phase == Phase.PK_VOTE and target_id not in game.pk_candidates:
            return ActionResult(False, "PK 阶段只能投给 PK 台上的玩家。")
        # PK 台上的人不能投票
        if game.phase == Phase.PK_VOTE and voter_id in game.pk_candidates:
            return ActionResult(False, "PK 台上的玩家不能投票。")

        target = game.players.get(target_id)
        if not target or not target.alive:
            return ActionResult(False, "投票目标不存在或已经出局。")
        game.votes[voter.user_id] = target.user_id

        # 计算需要多少票
        if game.phase == Phase.PK_VOTE:
            eligible_voters = [p for p in game.alive_players() if p.can_vote and p.user_id not in game.pk_candidates]
        else:
            eligible_voters = [p for p in game.alive_players() if p.can_vote]
        needed = len(eligible_voters)

        if len(game.votes) < needed:
            return ActionResult(True, f"已投给 {target.display_name}。当前 {len(game.votes)}/{needed} 票。")

        # 全部投完 → 结算
        return self._resolve_vote(game)

    def _resolve_vote(self, game: GameState) -> ActionResult:
        public_messages: list[str] = []

        if not game.votes:
            public_messages.append("全体弃票，今天无人出局。")
            return self._end_day(game, public_messages, eliminated=None)

        # 计算票数（警长 1.5 票）
        weighted_counts: Counter[str] = Counter()
        for voter_id, target_id in game.votes.items():
            weight = 1.5 if voter_id == game.sheriff_id else 1.0
            weighted_counts[target_id] += weight

        top_count = max(weighted_counts.values())
        top_targets = [uid for uid, c in weighted_counts.items() if c == top_count]

        if len(top_targets) == 1:
            eliminated = game.players[top_targets[0]]
            return self._eliminate_by_vote(game, eliminated, public_messages)

        # 平票处理
        if game.phase == Phase.PK_VOTE:
            # PK 再平票 → 无人出局
            public_messages.append("PK 再次平票，今天无人出局（平安日）。")
            return self._end_day(game, public_messages, eliminated=None)

        # 第一次平票 → 进入 PK
        game.pk_candidates = top_targets
        game.pk_round = 1
        game.phase = Phase.PK_VOTE
        game.votes.clear()
        names = "、".join(game.players[uid].display_name for uid in top_targets)
        public_messages.append(f"平票！{names} 上 PK 台。请 PK 玩家再次发言，其余玩家重新投票。")
        game.log_event("pk_start", detail=names)
        return ActionResult(True, "投票平票，进入 PK。", public_messages)

    def _eliminate_by_vote(self, game: GameState, eliminated: Player, public_messages: list[str]) -> ActionResult:
        """投票出局处理（含白痴翻牌、猎人开枪）。"""
        # 白痴翻牌
        if eliminated.role == Role.IDIOT and not eliminated.idiot_revealed:
            eliminated.idiot_revealed = True
            eliminated.death_cause = DeathCause.IDIOT_REVEALED
            public_messages.append(f"{eliminated.display_name} 翻牌——是白痴！免疫本次放逐，但此后不可投票。")
            game.log_event("idiot_reveal", actor_id=eliminated.user_id)
            game.votes.clear()
            return self._end_day(game, public_messages, eliminated=None)

        # 正常出局
        eliminated.alive = False
        eliminated.death_cause = DeathCause.VOTED_OUT
        public_messages.append(f"投票结束，{eliminated.display_name}（{eliminated.seat}号）被放逐。")
        game.log_event("vote_out", target_id=eliminated.user_id)

        # 警长出局 → 需要移交
        if eliminated.user_id == game.sheriff_id:
            eliminated.is_sheriff = False
            game.sheriff_id = None
            public_messages.append("警长出局，警徽需要移交或撕毁。")

        # 检查胜负
        winner = self._check_winner(game)
        if winner:
            game.phase = Phase.ENDED
            game.ended_reason = winner
            public_messages.append(winner)
            return ActionResult(True, "投票结算完成。", public_messages, True)

        # 遗言
        game.pending_last_words = [eliminated.user_id]
        game.phase = Phase.LAST_WORDS
        public_messages.append(f"{eliminated.display_name} 可以发表遗言。")
        return ActionResult(True, "投票结算完成。", public_messages)

    def _end_day(self, game: GameState, public_messages: list[str], *, eliminated: Player | None) -> ActionResult:
        """结束白天，进入夜晚。"""
        game.votes.clear()
        game.pk_candidates = []
        game.pk_round = 0

        winner = self._check_winner(game)
        if winner:
            game.phase = Phase.ENDED
            game.ended_reason = winner
            public_messages.append(winner)
            return ActionResult(True, "游戏结束。", public_messages, True)

        game.phase = Phase.NIGHT
        game.day_number += 1
        game.night = NightState()
        game.night_deaths = []
        public_messages.append(f"进入第 {game.day_number} 个夜晚。")
        game.log_event("night_start", detail=f"第{game.day_number}夜")
        return ActionResult(True, "白天结束，进入夜晚。", public_messages)

    # ------------------------------------------------------------------
    # Wolf self-destruct
    # ------------------------------------------------------------------

    def wolf_self_destruct(self, game: GameState, *, user_id: str, last_words: str = "") -> ActionResult:
        """狼人自爆。"""
        if game.phase not in (Phase.SPEAKING, Phase.VOTE, Phase.SHERIFF_ELECTION):
            return ActionResult(False, "只有在白天阶段才能自爆。")
        player = game.players.get(user_id)
        if not player or not player.alive:
            return ActionResult(False, "你不是存活玩家。")
        if player.role != Role.WEREWOLF:
            return ActionResult(False, "只有狼人才能自爆。")

        player.alive = False
        player.death_cause = DeathCause.WOLF_SELF_DESTRUCT
        game.log_event("wolf_self_destruct", actor_id=user_id)

        public_messages = [f"💥 {player.display_name}（{player.seat}号）自爆了！身份是狼人！"]
        if last_words:
            public_messages.append(f"【自爆遗言】{player.display_name}：{last_words}")
            game.last_words_given[user_id] = last_words

        # 检查胜负
        winner = self._check_winner(game)
        if winner:
            game.phase = Phase.ENDED
            game.ended_reason = winner
            public_messages.append(winner)
            return ActionResult(True, "自爆结算完成。", public_messages, True)

        # 自爆后立即进入夜晚
        game.votes.clear()
        game.pk_candidates = []
        game.phase = Phase.NIGHT
        game.day_number += 1
        game.night = NightState()
        game.night_deaths = []
        public_messages.append(f"自爆中断了白天流程，直接进入第 {game.day_number} 夜。")
        return ActionResult(True, "自爆结算完成。", public_messages)

    # ------------------------------------------------------------------
    # Win condition check
    # ------------------------------------------------------------------

    def _check_winner(self, game: GameState) -> str | None:
        wolves = game.living_role_count(Role.WEREWOLF)
        good = len(game.alive_players()) - wolves

        if wolves <= 0:
            return "🎉 游戏结束，好人阵营胜利！所有狼人已被消灭。"

        if game.win_rule == WinRule.EXTERMINATE:
            if wolves >= good:
                return "🐺 游戏结束，狼人阵营胜利！好人数量已不足。"
        else:
            # 屠边：杀光村民 OR 杀光神职
            living_villagers = sum(
                1 for p in game.players.values()
                if p.alive and p.role in (Role.VILLAGER, Role.IDIOT)
            )
            living_divine = sum(
                1 for p in game.players.values()
                if p.alive and p.role in DIVINE_ROLES
            )
            if living_villagers <= 0:
                return "🐺 游戏结束，狼人阵营胜利！所有村民已被屠杀。"
            if living_divine <= 0:
                return "🐺 游戏结束，狼人阵营胜利！所有神职已被屠杀。"
            # 通用判定：狼 >= 好
            if wolves >= good:
                return "🐺 游戏结束，狼人阵营胜利！"

        return None

    # ------------------------------------------------------------------
    # Night readiness
    # ------------------------------------------------------------------

    def _night_ready(self, game: GameState) -> bool:
        alive = game.alive_players()
        has_wolf = any(p.role == Role.WEREWOLF for p in alive)
        if has_wolf and not game.night.wolf_done:
            return False
        for p in alive:
            if p.role == Role.SEER and p.user_id not in game.night.seer_done:
                return False
            if p.role == Role.WITCH and p.user_id not in game.night.witch_done:
                return False
            if p.role == Role.GUARD and p.user_id not in game.night.guard_done:
                return False
        return True

    # ------------------------------------------------------------------
    # Views & helpers
    # ------------------------------------------------------------------

    def public_status(self, game: GameState) -> str:
        phase_label = {
            Phase.WAITING: "等待加入",
            Phase.NIGHT: f"第 {game.day_number} 夜",
            Phase.DAY_BREAK: f"第 {game.day_number} 天",
            Phase.SHERIFF_ELECTION: "警长竞选",
            Phase.SPEAKING: f"第 {game.day_number} 天·讨论",
            Phase.VOTE: f"第 {game.day_number} 天·投票",
            Phase.PK_VOTE: "PK 投票",
            Phase.LAST_WORDS: "遗言阶段",
            Phase.HUNTER_SHOT: "猎人开枪",
            Phase.ENDED: "已结束",
        }[game.phase]
        lines = [f"狼人杀状态：{phase_label}（{game.board_name}）", "玩家："]
        for p in sorted(game.players.values(), key=lambda x: x.seat):
            state = "存活" if p.alive else "出局"
            sheriff = " 👑" if p.is_sheriff else ""
            lines.append(f"{p.seat}. {p.display_name}（{state}）{sheriff}")
        if game.phase == Phase.WAITING:
            board = get_board(game.board_name)
            lines.append(f"人数：{len(game.players)}/{board.player_count}")
            if len(game.players) >= board.player_count:
                lines.append("下一步：房主发送 /狼人杀 开始。")
            elif len(game.players) >= MIN_TEST_PLAYERS:
                lines.append(
                    "下一步：继续邀请玩家，或由房主发送 /狼人杀 测试开始。"
                )
            else:
                lines.append("下一步：发送 /狼人杀 加入，等待更多玩家。")
        if game.phase == Phase.ENDED and game.ended_reason:
            lines.append(f"结局：{game.ended_reason}")
        return "\n".join(lines)

    def player_view(self, game: GameState, user_id: str) -> str:
        player = game.players.get(str(user_id))
        if not player:
            return "你还没有加入这局狼人杀。"
        lines = [
            f"当前阶段：{self._phase_label(game)}",
            f"你的身份：{player.role_label}（{player.seat}号）",
            f"你的状态：{'存活' if player.alive else '出局'}",
        ]
        if player.is_sheriff:
            lines.append("你是警长 👑")
        lines.append("玩家列表：")
        for p in sorted(game.players.values(), key=lambda x: x.seat):
            state = "存活" if p.alive else "出局"
            sheriff = " 👑" if p.is_sheriff else ""
            lines.append(f"{p.seat}. {p.display_name}（{state}）{sheriff}")
        if player.role == Role.WEREWOLF:
            teammates = [
                p.display_name for p in game.players.values()
                if p.role == Role.WEREWOLF and p.user_id != player.user_id
            ]
            lines.append("狼队友：" + ("、".join(teammates) if teammates else "无"))
        if player.role == Role.WITCH:
            potions = []
            if game.witch_heal_available:
                potions.append("解药")
            if game.witch_poison_available:
                potions.append("毒药")
            lines.append("剩余药剂：" + ("、".join(potions) if potions else "无"))
        if player.role == Role.GUARD:
            if game.last_guard_target:
                last = game.players.get(game.last_guard_target)
                lines.append(f"昨晚守护了：{last.display_name if last else '?'}（今晚不能守同一人）")
        return "\n".join(lines)

    def role_notice(self, game: GameState, user_id: str) -> str:
        player = game.players[user_id]
        lines = [f"你的身份是：{player.role_label}（{player.seat}号位）。"]
        if player.role == Role.WEREWOLF:
            wolves = [
                p.display_name for p in game.players.values()
                if p.role == Role.WEREWOLF and p.user_id != user_id
            ]
            lines.append("你的狼队友：" + ("、".join(wolves) if wolves else "无"))
            lines.append("夜晚：/狼人杀 杀 编号 | 白天可 /狼人杀 自爆")
        elif player.role == Role.SEER:
            lines.append("夜晚：/狼人杀 验 编号")
        elif player.role == Role.WITCH:
            lines.append("夜晚：/狼人杀 救 或 /狼人杀 毒 编号 | /狼人杀 跳过")
        elif player.role == Role.GUARD:
            lines.append("夜晚：/狼人杀 守 编号（不能守自己，不能连续守同一人）")
        elif player.role == Role.HUNTER:
            lines.append("被狼刀或投票出局时可开枪：/狼人杀 开枪 编号")
        elif player.role == Role.IDIOT:
            lines.append("被投票出局时自动翻牌免疫，但之后不可投票。")
        else:
            lines.append("夜晚无需行动；白天讨论和投票。")
        return "\n".join(lines)

    def night_prompt(self, game: GameState, user_id: str) -> str:
        """生成夜晚私聊提示。"""
        player = game.players.get(user_id)
        if not player or not player.alive:
            return ""
        player_list = self._public_player_list(game)
        if player.role == Role.WEREWOLF:
            return f"第 {game.day_number} 夜，请选择刀人目标：\n{player_list}\n/狼人杀 杀 编号，或 /狼人杀 跳过"
        if player.role == Role.SEER:
            return f"第 {game.day_number} 夜，请选择查验目标：\n{player_list}\n/狼人杀 验 编号，或 /狼人杀 跳过"
        if player.role == Role.WITCH:
            info = ""
            if game.night.wolf_target:
                victim = game.players.get(game.night.wolf_target)
                info = f"\n今晚被刀的是：{victim.display_name if victim else '?'}"
            return f"第 {game.day_number} 夜，女巫行动：{info}\n{player_list}\n/狼人杀 救、/狼人杀 毒 编号，或 /狼人杀 跳过"
        if player.role == Role.GUARD:
            extra = ""
            if game.last_guard_target:
                last = game.players.get(game.last_guard_target)
                extra = f"\n昨晚守了 {last.display_name if last else '?'}，今晚不能守同一人。"
            return f"第 {game.day_number} 夜，请选择守护目标：{extra}\n{player_list}\n/狼人杀 守 编号，或 /狼人杀 跳过"
        return ""

    def resolve_target(self, game: GameState, raw: str) -> str | None:
        text = str(raw or "").strip().lstrip("@")
        if not text or text in {"0", "弃票", "取消", "跳过", "pass"}:
            return None
        players = list(game.players.values())
        if text.isdigit():
            index = int(text) - 1
            if 0 <= index < len(players):
                return players[index].user_id
            # 也尝试按座位号
            for p in players:
                if p.seat == int(text):
                    return p.user_id
        if text in game.players:
            return text
        for p in players:
            if text == p.display_name or text in p.display_name:
                return p.user_id
        return None

    def game_recap(self, game: GameState) -> str:
        """游戏复盘：全员身份揭示 + 事件回顾。"""
        lines = ["═══ 游戏复盘 ═══", "", "【全员身份】"]
        for p in sorted(game.players.values(), key=lambda x: x.seat):
            state = "存活" if p.alive else "出局"
            lines.append(f"{p.seat}. {p.display_name} — {p.role_label}（{state}）")
        lines.append("")
        lines.append(f"【结局】{game.ended_reason}")
        lines.append("")
        lines.append("【关键事件】")
        key_events = [e for e in game.event_log if e.event_type in (
            "wolf_kill", "witch_poison", "witch_heal", "seer_check",
            "hunter_shot", "vote_out", "wolf_self_destruct", "sheriff_elected",
            "guard_protect", "idiot_reveal", "death",
        )]
        for e in key_events[-20:]:  # 最多显示 20 条
            actor = game.players.get(e.actor_id)
            target = game.players.get(e.target_id)
            a_name = actor.display_name if actor else ""
            t_name = target.display_name if target else ""
            lines.append(f"第{e.day}轮 [{e.event_type}] {a_name} → {t_name} {e.detail}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _phase_label(self, game: GameState) -> str:
        labels = {
            Phase.WAITING: "等待加入",
            Phase.NIGHT: f"第 {game.day_number} 夜",
            Phase.DAY_BREAK: f"第 {game.day_number} 天",
            Phase.SHERIFF_ELECTION: "警长竞选",
            Phase.SPEAKING: f"第 {game.day_number} 天·讨论",
            Phase.VOTE: f"第 {game.day_number} 天·投票",
            Phase.PK_VOTE: "PK 投票",
            Phase.LAST_WORDS: "遗言阶段",
            Phase.HUNTER_SHOT: "猎人开枪",
            Phase.ENDED: "已结束",
        }
        return labels[game.phase]

    def _normalize_action(self, action: str) -> str:
        table = {
            "杀": "kill", "刀": "kill", "kill": "kill",
            "验": "check", "查验": "check", "check": "check",
            "救": "heal", "heal": "heal",
            "毒": "poison", "poison": "poison",
            "守": "guard", "守护": "guard", "guard": "guard",
            "跳过": "pass", "过": "pass", "pass": "pass",
        }
        return table.get(str(action or "").strip().lower(), str(action or "").strip().lower())

    def _public_player_list(self, game: GameState) -> str:
        lines = []
        for p in sorted(game.players.values(), key=lambda x: x.seat):
            state = "存活" if p.alive else "出局"
            lines.append(f"{p.seat}. {p.display_name}（{state}）")
        return "\n".join(lines)
