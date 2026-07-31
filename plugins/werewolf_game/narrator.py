"""Dramatic narrator for Werewolf game.

Transforms dry game state messages into atmospheric, theatrical narration.
Three styles: concise / standard / dramatic.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from .models import GameState, Phase, Player, Role, ROLE_LABELS

if TYPE_CHECKING:
    pass


class Narrator:
    """Game narrator that produces atmospheric text."""

    def __init__(self, style: str = "standard") -> None:
        """style: concise | standard | dramatic"""
        self._style = style
        self._rng = random.SystemRandom()

    # ------------------------------------------------------------------
    # Phase transitions
    # ------------------------------------------------------------------

    def game_start(self, game: GameState) -> str:
        if self._style == "concise":
            return f"游戏开始（{game.board_name}），进入第 1 夜。"
        return self._rng.choice([
            f"🌑 夜幕降临，小镇陷入沉寂。\n"
            f"{len(game.players)} 位玩家各怀心事，在月光下隐藏着自己的秘密……\n"
            f"（{game.board_name} · 第 1 夜）",

            f"🕯️ 古老的钟声敲响，黑暗吞噬了最后一丝余晖。\n"
            f"在这个看似平静的小镇里，狼人已经睁开了眼睛……\n"
            f"（{game.board_name} · 第 1 夜 · {len(game.players)} 人）",
        ])

    def night_falls(self, game: GameState) -> str:
        if self._style == "concise":
            return f"进入第 {game.day_number} 夜。"
        templates = [
            f"🌙 月亮再次升起，小镇陷入死寂……\n（第 {game.day_number} 夜）",
            f"🌑  darkness 笼罩大地，某些人正在暗中行动……\n（第 {game.day_number} 夜）",
            f"🕯️ 夜幕再次降临。谁将在今夜永远闭上双眼？\n（第 {game.day_number} 夜）",
        ]
        return self._rng.choice(templates)

    def day_breaks(self, game: GameState, dead_names: list[str]) -> str:
        alive_count = len(game.alive_players())
        if self._style == "concise":
            if not dead_names:
                return f"天亮了。平安夜，无人出局。（存活 {alive_count} 人）"
            return f"天亮了。昨夜出局：{'、'.join(dead_names)}。（存活 {alive_count} 人）"

        if not dead_names:
            return self._rng.choice([
                f"🌅 天亮了。\n"
                f"清晨的阳光洒满小镇，所有人安然无恙——\n"
                f"这是一个平安夜。\n"
                f"（第 {game.day_number} 天 · 存活 {alive_count} 人）",

                f"☀️ 黎明到来。\n"
                f"村民们忐忑地走出家门，互相打量——\n"
                f"奇迹般地，昨夜无人遇难。\n"
                f"（第 {game.day_number} 天 · 存活 {alive_count} 人）",
            ])

        names_text = "、".join(dead_names)
        return self._rng.choice([
            f"🌅 天亮了。\n"
            f"清晨的阳光照进小镇，但广场上多了一具冰冷的尸体——\n"
            f"{names_text} 永远地闭上了眼睛。\n"
            f"村民们聚集在一起，眼中满是恐惧与怀疑……\n"
            f"（第 {game.day_number} 天 · 存活 {alive_count} 人）",

            f"☀️ 天亮了。\n"
            f"一声尖叫划破了清晨的宁静——\n"
            f"{names_text} 已经永远离开了这个世界。\n"
            f"狼人的影子，依然潜伏在人群之中。\n"
            f"（第 {game.day_number} 天 · 存活 {alive_count} 人）",
        ])

    # ------------------------------------------------------------------
    # Key events
    # ------------------------------------------------------------------

    def hunter_shot(self, hunter_name: str, target_name: str | None) -> str:
        if not target_name:
            return f"🔫 {hunter_name}（猎人）选择了沉默，没有扣下扳机。"
        if self._style == "concise":
            return f"猎人 {hunter_name} 开枪带走了 {target_name}。"
        return (
            f"💥 砰——！\n"
            f"{hunter_name} 猛然起身，手中的猎枪喷出火焰！\n"
            f"「你，就是狼人！」\n"
            f"{target_name} 应声倒地。\n"
            f"（猎人开枪 · {target_name} 出局）"
        )

    def wolf_self_destruct(self, wolf_name: str, last_words: str = "") -> str:
        base = (
            f"💥 {wolf_name} 突然大笑起来——\n"
            f"「没错，我就是狼人！」\n"
            f"在众人惊愕的目光中，{wolf_name} 撕碎了自己的面具。"
        )
        if last_words:
            base += f"\n遗言：「{last_words}」"
        return base

    def vote_result(self, eliminated_name: str | None, vote_detail: str = "") -> str:
        if not eliminated_name:
            return "投票结束，今天无人被放逐。"
        if self._style == "concise":
            return f"投票结束，{eliminated_name} 被放逐。"
        return (
            f"⚖️ 投票结束。\n"
            f"众人的手指向了同一个人——\n"
            f"{eliminated_name} 被放逐出了小镇。\n"
            f"{vote_detail}"
        )

    def idiot_reveal(self, player_name: str) -> str:
        return (
            f"🃏 等等！{player_name} 翻开了自己的身份牌——\n"
            f"是白痴！「你们投错人了，朋友们。」\n"
            f"{player_name} 免疫了本次放逐，但此后将失去投票权。"
        )

    def sheriff_elected(self, sheriff_name: str, seat: int) -> str:
        if self._style == "concise":
            return f"{sheriff_name}（{seat}号）当选警长。"
        return (
            f"🎖️ 经过激烈的竞选，\n"
            f"{sheriff_name}（{seat}号）获得了最多选票，当选为本局警长！\n"
            f"警长拥有 1.5 票投票权，并可决定发言顺序。"
        )

    def last_words_prompt(self, player_name: str) -> str:
        return f"📜 {player_name}，你还有最后的话要说吗？\n（发送 /狼人杀 遗言 <内容>，或 /狼人杀 跳过）"

    # ------------------------------------------------------------------
    # Game end & recap
    # ------------------------------------------------------------------

    def game_end(self, game: GameState, winner_text: str) -> str:
        if self._style == "concise":
            return winner_text

        # Determine which faction won
        wolf_win = "狼人" in winner_text
        if wolf_win:
            header = self._rng.choice([
                "🐺 狼嚎响彻夜空——\n狼人阵营获得了最终胜利。",
                "🌑 黑暗彻底吞噬了小镇——\n狼人阵营胜利。",
            ])
        else:
            header = self._rng.choice([
                "☀️ 阳光终于驱散了阴霾——\n好人阵营获得了胜利！",
                "🎉 最后一只狼人倒下了——\n小镇重归安宁，好人阵营胜利！",
            ])
        return f"{header}\n\n{winner_text}"

    def recap(self, game: GameState) -> str:
        """Full game recap with role reveal."""
        lines = [
            "═══════════════════════════",
            "        游 戏 复 盘",
            "═══════════════════════════",
            "",
            "【全员身份揭示】",
        ]
        for p in sorted(game.players.values(), key=lambda x: x.seat):
            state = "✓ 存活" if p.alive else "✗ 出局"
            sheriff = " 👑" if p.is_sheriff else ""
            lines.append(f"  {p.seat:2d}. {p.display_name:<8s} — {p.role_label:<4s} {state}{sheriff}")

        lines.append("")
        lines.append(f"【结局】{game.ended_reason}")
        lines.append(f"【板子】{game.board_name}")
        lines.append(f"【历时】{game.day_number} 天")
        lines.append("")

        # Key events timeline
        lines.append("【事件回顾】")
        key_types = {
            "wolf_kill": "🗡️ 狼刀",
            "witch_heal": "💊 女巫救人",
            "witch_poison": "☠️ 女巫下毒",
            "seer_check": "🔮 预言家查验",
            "guard_protect": "🛡️ 守卫守护",
            "hunter_shot": "🔫 猎人开枪",
            "vote_out": "⚖️ 投票放逐",
            "wolf_self_destruct": "💥 狼人自爆",
            "sheriff_elected": "🎖️ 警长当选",
            "idiot_reveal": "🃏 白痴翻牌",
        }
        for e in game.event_log:
            if e.event_type in key_types:
                icon_label = key_types[e.event_type]
                actor = game.players.get(e.actor_id)
                target = game.players.get(e.target_id)
                a = actor.display_name if actor else ""
                t = target.display_name if target else ""
                detail = f" {e.detail}" if e.detail else ""
                if t:
                    lines.append(f"  第{e.day}轮 {icon_label} {a} → {t}{detail}")
                else:
                    lines.append(f"  第{e.day}轮 {icon_label} {a}{detail}")

        lines.append("")
        lines.append("═══════════════════════════")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def speaking_prompt(self, player: Player, index: int, total: int) -> str:
        """Prompt for current speaker."""
        return f"🎤 请 {player.display_name}（{player.seat}号）发言。（{index}/{total}）"

    def vote_prompt(self, game: GameState) -> str:
        alive = game.alive_players()
        voter_list = "、".join(f"{p.seat}号" for p in alive if p.can_vote)
        return (
            f"📮 投票时间！\n"
            f"存活玩家：{voter_list}\n"
            f"请发送：/狼人杀 投票 编号（弃票发 /狼人杀 投票 0）"
        )

    def pk_prompt(self, game: GameState) -> str:
        names = "、".join(
            game.players[uid].display_name
            for uid in game.pk_candidates
            if uid in game.players
        )
        return (
            f"⚔️ PK 台：{names}\n"
            f"请 PK 台上的玩家再次发言，其余玩家重新投票。\n"
            f"（PK 台上的玩家不能投票）"
        )
