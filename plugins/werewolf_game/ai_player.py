"""AI player strategy for Werewolf game.

Night decisions use deterministic rules (fast, no LLM).
Day speeches use LLM generation (fun, human-like).
Strict information isolation: AI only sees what its role should know.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.kernel.logger import get_logger

from .models import DIVINE_ROLES, Faction, GameState, Phase, Player, Role

if TYPE_CHECKING:
    pass

logger = get_logger("werewolf_ai")


@dataclass(slots=True)
class NightDecision:
    """AI night action decision."""
    action: str  # kill/check/heal/poison/guard/pass
    target_id: str | None = None


class AIPlayerStrategy:
    """AI player strategy decision maker.

    Provides rule-based night decisions and LLM-driven day speeches.
    All decisions respect information isolation per role.
    """

    def __init__(self, *, difficulty: str = "normal") -> None:
        self._difficulty = difficulty  # easy/normal/hard
        self._rng = random.SystemRandom()

    # ------------------------------------------------------------------
    # Night decisions (rule-based, deterministic-ish)
    # ------------------------------------------------------------------

    def decide_night_action(self, game: GameState, player: Player) -> NightDecision:
        """Decide night action based on role and game state."""
        if not player.alive or player.role is None:
            return NightDecision("pass")

        if player.role == Role.WEREWOLF:
            return self._wolf_night(game, player)
        if player.role == Role.SEER:
            return self._seer_night(game, player)
        if player.role == Role.WITCH:
            return self._witch_night(game, player)
        if player.role == Role.GUARD:
            return self._guard_night(game, player)
        return NightDecision("pass")

    def _wolf_night(self, game: GameState, player: Player) -> NightDecision:
        """Wolf targeting: prioritize divine roles and high-threat players."""
        targets = [
            p for p in game.alive_players()
            if p.user_id != player.user_id and p.faction == Faction.GOOD
        ]
        if not targets:
            return NightDecision("kill")

        # Priority: claimed seer > claimed witch > guard > hunter > villager
        priority_map: dict[Role | None, int] = {
            Role.SEER: 10,
            Role.WITCH: 8,
            Role.GUARD: 7,
            Role.HUNTER: 5,
            Role.IDIOT: 3,
            Role.VILLAGER: 1,
            None: 1,
        }

        # In hard mode, wolves "sense" divine roles with some probability
        if self._difficulty == "hard":
            scored = [(priority_map.get(t.role, 1), t) for t in targets]
        else:
            # Normal: wolves don't know roles, pick somewhat randomly
            # but slightly prefer players who spoke a lot (simulated)
            scored = [(self._rng.randint(1, 5), t) for t in targets]

        scored.sort(key=lambda x: x[0], reverse=True)
        target = scored[0][1]
        return NightDecision("kill", target.user_id)

    def _seer_night(self, game: GameState, player: Player) -> NightDecision:
        """Seer check: prefer unchecked players."""
        targets = [
            p for p in game.alive_players()
            if p.user_id != player.user_id
        ]
        if not targets:
            return NightDecision("pass")
        # Prefer players not yet checked (we track via event log)
        checked_ids = {
            e.target_id for e in game.event_log
            if e.event_type == "seer_check" and e.actor_id == player.user_id
        }
        unchecked = [t for t in targets if t.user_id not in checked_ids]
        pool = unchecked if unchecked else targets
        target = self._rng.choice(pool)
        return NightDecision("check", target.user_id)

    def _witch_night(self, game: GameState, player: Player) -> NightDecision:
        """Witch logic: save first night, poison suspected wolves later."""
        # If wolf target exists and we have heal, save on first night
        if game.night.wolf_target and game.witch_heal_available:
            if game.day_number == 1:
                return NightDecision("heal")
            # Later nights: 50% save (normal), 70% save (hard)
            save_prob = 0.7 if self._difficulty == "hard" else 0.5
            if self._rng.random() < save_prob:
                return NightDecision("heal")

        # Consider poison: only if we have strong suspicion (hard mode)
        if game.witch_poison_available and game.day_number >= 2:
            if self._difficulty == "hard" and self._rng.random() < 0.3:
                # Poison a random non-wolf-suspected player
                targets = [
                    p for p in game.alive_players()
                    if p.user_id != player.user_id and p.role != Role.WEREWOLF
                ]
                # AI witch doesn't actually know roles, pick random
                all_targets = [
                    p for p in game.alive_players()
                    if p.user_id != player.user_id
                ]
                if all_targets:
                    target = self._rng.choice(all_targets)
                    return NightDecision("poison", target.user_id)

        return NightDecision("pass")

    def _guard_night(self, game: GameState, player: Player) -> NightDecision:
        """Guard: protect high-value targets, never same twice."""
        targets = [
            p for p in game.alive_players()
            if p.user_id != player.user_id
            and p.user_id != game.last_guard_target  # Can't guard same person
        ]
        if not targets:
            return NightDecision("pass")

        # Prefer guarding self-claimed divine or sheriff
        if game.sheriff_id and game.sheriff_id != player.user_id:
            sheriff = game.players.get(game.sheriff_id)
            if sheriff and sheriff.alive and sheriff.user_id != game.last_guard_target:
                return NightDecision("guard", sheriff.user_id)

        # Otherwise guard a random alive player
        target = self._rng.choice(targets)
        return NightDecision("guard", target.user_id)

    # ------------------------------------------------------------------
    # Day decisions
    # ------------------------------------------------------------------

    def decide_vote(self, game: GameState, player: Player) -> str | None:
        """Decide vote target. Returns user_id or None (abstain)."""
        alive = [
            p for p in game.alive_players()
            if p.user_id != player.user_id and p.can_vote
        ]
        if not alive:
            return None

        if player.role == Role.WEREWOLF:
            # Wolves vote for good players (try to frame)
            good_players = [p for p in alive if p.faction == Faction.GOOD]
            if good_players:
                return self._rng.choice(good_players).user_id
        else:
            # Good players: vote somewhat randomly (no real info in rule-based)
            # In hard mode, slight bias toward actual wolves (simulated "reading")
            if self._difficulty == "hard":
                wolves = [p for p in alive if p.role == Role.WEREWOLF]
                if wolves and self._rng.random() < 0.4:
                    return self._rng.choice(wolves).user_id

        return self._rng.choice(alive).user_id

    def decide_sheriff_campaign(self, game: GameState, player: Player) -> bool:
        """Decide whether to run for sheriff."""
        # Divine roles and confident villagers run
        if player.role in (Role.SEER, Role.HUNTER, Role.GUARD):
            return True
        if player.role == Role.WEREWOLF:
            # Wolves sometimes run to gain trust
            return self._rng.random() < 0.4
        # Villagers rarely run
        return self._rng.random() < 0.15

    def decide_hunter_shot(self, game: GameState, player: Player) -> str | None:
        """Hunter decides who to shoot."""
        alive = [
            p for p in game.alive_players()
            if p.user_id != player.user_id
        ]
        if not alive:
            return None
        # Shoot suspected wolf (in hard mode, bias toward actual wolves)
        if self._difficulty == "hard":
            wolves = [p for p in alive if p.role == Role.WEREWOLF]
            if wolves and self._rng.random() < 0.6:
                return self._rng.choice(wolves).user_id
        return self._rng.choice(alive).user_id

    def decide_wolf_self_destruct(self, game: GameState, player: Player) -> bool:
        """Wolf decides whether to self-destruct."""
        # Only in desperate situations (last wolf or about to be voted)
        living_wolves = game.living_role_count(Role.WEREWOLF)
        if living_wolves > 1:
            return False
        # Last wolf: 30% chance to self-destruct to skip day
        return self._rng.random() < 0.3

    # ------------------------------------------------------------------
    # LLM-driven speech generation
    # ------------------------------------------------------------------

    async def generate_speech(
        self,
        game: GameState,
        player: Player,
        *,
        context: str = "",
    ) -> str:
        """Generate AI player speech using LLM.

        Falls back to template-based speech if LLM is unavailable.
        """
        try:
            return await self._llm_speech(game, player, context=context)
        except Exception as exc:
            logger.warning(f"AI speech LLM failed, using template: {exc}")
            return self._template_speech(game, player)

    async def _llm_speech(self, game: GameState, player: Player, *, context: str) -> str:
        """Generate speech via project LLM kernel."""
        from src.app.plugin_system.api.llm_api import create_llm_request

        # Build information-isolated prompt
        known_info = self._build_known_info(game, player)
        system_prompt = (
            f"你正在玩狼人杀，你的身份是{player.role_label}（{player.seat}号位）。\n"
            f"{known_info}\n"
            "请以玩家身份发表一段简短的白天发言（2-4句话）。\n"
            "要求：\n"
            "- 不要暴露你不该知道的信息\n"
            "- 语气自然，像真实玩家\n"
            "- 可以分析、怀疑、表态、跟票\n"
            "- 如果是狼人，要伪装好人\n"
            "- 不要说'我是AI'或'作为XX角色'\n"
        )
        user_prompt = f"现在是第 {game.day_number} 天讨论阶段。请发言。"
        if context:
            user_prompt = f"前面的发言：\n{context}\n\n轮到你发言了。"

        request = create_llm_request(
            task_name="werewolf_speech",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=200,
            temperature=0.9,
        )
        response = await request.send()
        text = str(response.content or "").strip()
        # Limit length
        if len(text) > 150:
            text = text[:150] + "……"
        return text or self._template_speech(game, player)

    def _build_known_info(self, game: GameState, player: Player) -> str:
        """Build what this player legitimately knows."""
        lines: list[str] = []
        alive = game.alive_players()
        lines.append(f"存活玩家（{len(alive)}人）：" + "、".join(
            f"{p.seat}号{p.display_name}" for p in alive
        ))

        if player.role == Role.WEREWOLF:
            wolves = [p for p in game.players.values() if p.role == Role.WEREWOLF]
            lines.append("你的狼队友：" + "、".join(
                f"{p.seat}号{p.display_name}" for p in wolves if p.user_id != player.user_id
            ))

        # Public knowledge from event log (deaths, etc.)
        deaths = [e for e in game.event_log if e.event_type == "death"]
        if deaths:
            dead_names = []
            for d in deaths:
                dp = game.players.get(d.target_id)
                if dp:
                    dead_names.append(f"{dp.seat}号{dp.display_name}")
            lines.append("已出局：" + "、".join(dead_names))

        if game.sheriff_id:
            sheriff = game.players.get(game.sheriff_id)
            if sheriff:
                lines.append(f"当前警长：{sheriff.seat}号{sheriff.display_name}")

        return "\n".join(lines)

    def _template_speech(self, game: GameState, player: Player) -> str:
        """Fallback template-based speech."""
        templates_wolf = [
            "我觉得今天得仔细听听大家的发言，看看谁的逻辑有问题。",
            "昨晚的信息量挺大的，我倾向于先听再说。",
            "我比较怀疑前面几位发言避重就轻的人。",
            "大家冷静分析，不要被带节奏。",
        ]
        templates_good = [
            "我目前没有太多信息，先听听大家怎么说。",
            "我觉得可以从昨晚的死亡情况来分析一下。",
            "希望有信息的人能站出来分享一下。",
            "我倾向于跟着逻辑走，不轻易站队。",
            "有没有人注意到谁昨天的发言前后矛盾？",
        ]
        if player.role == Role.WEREWOLF:
            return self._rng.choice(templates_wolf)
        return self._rng.choice(templates_good)
