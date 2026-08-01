"""Runtime service for Werewolf games v2.0.

Handles command routing, AI bot automation, timers, and narration.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseService
from src.core.models.message import Message
from src.core.models.stream import ChatStream

from .ai_player import AIPlayerStrategy
from .boards import board_list_text, get_board
from .engine import ActionResult, WerewolfEngine
from .models import GameState, Phase, Player, Role
from .narrator import Narrator

logger = get_logger("werewolf_game")


class WerewolfGameService(BaseService):
    """Stateful facade around the deterministic Werewolf engine v2."""

    service_name = "werewolf_game"
    service_description = "QQ 群狼人杀裁判服务（商业级）"
    version = "2.0.0"

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
        if getattr(plugin, "_werewolf_engine", None) is None:
            plugin._werewolf_engine = WerewolfEngine()
        if getattr(plugin, "_werewolf_games", None) is None:
            plugin._werewolf_games = {}
        if getattr(plugin, "_werewolf_ai", None) is None:
            difficulty = "normal"
            try:
                difficulty = plugin.config.ai.difficulty
            except Exception:
                pass
            plugin._werewolf_ai = AIPlayerStrategy(difficulty=difficulty)
        if getattr(plugin, "_werewolf_narrator", None) is None:
            style = "standard"
            try:
                style = plugin.config.narration.style
            except Exception:
                pass
            plugin._werewolf_narrator = Narrator(style=style)

        self.engine: WerewolfEngine = plugin._werewolf_engine
        self.games: dict[str, GameState] = plugin._werewolf_games
        self.ai: AIPlayerStrategy = plugin._werewolf_ai
        self.narrator: Narrator = plugin._werewolf_narrator
        self._timers: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Group command handler
    # ------------------------------------------------------------------

    async def handle_group_command(self, message: Message, args: list[str]) -> str:
        command = args[0] if args else "状态"
        rest = args[1:]
        platform = str(message.platform or "qq")
        group_id = self._group_id_from_message(message)
        if not group_id:
            return "狼人杀只能在群聊里开局。"
        key = self._key(platform, group_id)

        # --- 创建房间 ---
        if command in {"开局", "创建", "create", "new"}:
            if key in self.games and self.games[key].phase != Phase.ENDED:
                return "这个群已经有一局狼人杀在进行。"
            # 解析板子参数
            board_name = "12人标准屠边局"
            for arg in rest:
                if "板子=" in arg or "board=" in arg:
                    board_name = arg.split("=", 1)[1].strip()
            try:
                board_name = self.plugin.config.game.default_board
            except Exception:
                pass
            if rest and "=" not in rest[0]:
                board_name = rest[0]

            game = self.engine.create_game(
                platform=platform,
                group_id=group_id,
                group_name=str(message.extra.get("group_name") or ""),
                group_stream_id=message.stream_id,
                owner_id=str(message.sender_id),
                board_name=board_name,
            )
            self.games[key] = game
            self.engine.add_player(game, user_id=str(message.sender_id), display_name=self._sender_name(message))
            board = get_board(game.board_name)
            return (
                f"狼人杀房间已创建（{board.name}，{board.player_count}人）。\n"
                f"发送 /狼人杀 加入 参与，房主 /狼人杀 开始 发牌。\n"
                f"查看板子：/狼人杀 板子"
            )

        game = self.games.get(key)

        # --- 帮助 ---
        if command in {"帮助", "help", "指令", "commands"}:
            return self._help_text(game)

        # --- 板子列表 ---
        if command in {"板子", "board", "boards"}:
            return board_list_text()

        if not game:
            return "这个群还没有狼人杀房间。发送 /狼人杀 开局 创建。"

        # --- 加入 ---
        if command in {"加入", "join"}:
            if rest and rest[0].strip().lower() in {"爱莉", "aili", "bot", "机器人"}:
                return await self._join_bot(game)
            result = self.engine.add_player(game, user_id=str(message.sender_id), display_name=self._sender_name(message))
            return result.message

        # --- 退出 ---
        if command in {"退出", "离开", "quit", "leave"}:
            return self.engine.remove_player(game, str(message.sender_id)).message

        # --- 开始 ---
        if command in {"开始", "start"}:
            if str(message.sender_id) != game.owner_id:
                return "只有房主可以开始本局。"
            result = self.engine.start_game(game)
            if not result.ok:
                return result.message
            await self._on_game_started(game)
            return self.narrator.game_start(game)

        # --- 测试开始 ---
        if command in {"测试开始", "测试", "teststart", "test"}:
            if str(message.sender_id) != game.owner_id:
                return "只有房主可以开始本局。"
            result = self.engine.start_test_game(game)
            if not result.ok:
                return result.message
            await self._on_game_started(game)
            return "测试局开始。请查看私聊身份。"

        # --- 状态 ---
        if command in {"状态", "status", "玩家", "players"}:
            return self.engine.public_status(game)

        # --- 结束 ---
        if command in {"结束", "end", "stop"}:
            if str(message.sender_id) != game.owner_id:
                return "只有房主可以结束本局。"
            game.phase = Phase.ENDED
            game.ended_reason = "房主结束了本局。"
            self._cancel_timer(key)
            return "本局狼人杀已结束。\n" + self.narrator.recap(game)

        # --- 投票 ---
        if command in {"投票", "票", "vote"}:
            if not rest:
                return "投票需要指定编号，例如：/狼人杀 投票 2"
            target_id = self.engine.resolve_target(game, rest[0])
            result = self.engine.vote(game, voter_id=str(message.sender_id), target_id=target_id)
            await self._publish(game, result)
            await self._after_action(game)
            return result.message

        # --- 发言 ---
        if command in {"发言", "说", "speech", "speak"}:
            text = " ".join(rest) if rest else ""
            if not text:
                return "发言需要内容：/狼人杀 发言 <内容>"
            result = self.engine.record_speech(game, user_id=str(message.sender_id), text=text)
            return result.message

        # --- 下一位（推进发言） ---
        if command in {"下一位", "next", "过"}:
            result = self.engine.advance_speaker(game)
            await self._publish(game, result)
            return result.message

        # --- 自爆 ---
        if command in {"自爆", "boom", "explode"}:
            last_words = " ".join(rest) if rest else ""
            result = self.engine.wolf_self_destruct(game, user_id=str(message.sender_id), last_words=last_words)
            await self._publish(game, result)
            if result.ok and game.phase == Phase.NIGHT:
                await self._send_night_prompts(game)
            return result.message

        # --- 竞选 ---
        if command in {"竞选", "参选", "campaign"}:
            result = self.engine.sheriff_register(game, user_id=str(message.sender_id))
            await self._publish(game, result)
            return result.message

        # --- 退选 ---
        if command in {"退选", "withdraw"}:
            result = self.engine.sheriff_withdraw(game, user_id=str(message.sender_id))
            return result.message

        # --- 警长投票 ---
        if command in {"选警长", "警长投票", "sheriff_vote"}:
            if not rest:
                return "请指定候选人编号：/狼人杀 选警长 编号"
            target_id = self.engine.resolve_target(game, rest[0])
            result = self.engine.sheriff_vote(game, voter_id=str(message.sender_id), target_id=target_id)
            await self._publish(game, result)
            return result.message

        # --- 移交 ---
        if command in {"移交", "transfer"}:
            if not rest:
                return "请指定移交目标：/狼人杀 移交 编号"
            target_id = self.engine.resolve_target(game, rest[0])
            if not target_id:
                return "目标不存在。"
            result = self.engine.sheriff_transfer(game, sheriff_id=str(message.sender_id), target_id=target_id)
            await self._publish(game, result)
            return result.message

        # --- 撕毁警徽 ---
        if command in {"撕毁", "撕警徽", "destroy"}:
            result = self.engine.sheriff_destroy(game, sheriff_id=str(message.sender_id))
            await self._publish(game, result)
            return result.message

        # --- 遗言 ---
        if command in {"遗言", "lastwords", "last_words"}:
            text = " ".join(rest) if rest else ""
            if not text:
                return "遗言需要内容：/狼人杀 遗言 <内容>"
            result = self.engine.submit_last_words(game, user_id=str(message.sender_id), text=text)
            await self._publish(game, result)
            await self._after_action(game)
            return result.message

        # --- 跳过（遗言/夜晚） ---
        if command in {"跳过", "skip", "pass"}:
            if game.phase == Phase.LAST_WORDS:
                result = self.engine.skip_last_words(game, user_id=str(message.sender_id))
                await self._publish(game, result)
                await self._after_action(game)
                return result.message
            return "夜晚行动请私聊我发送。"

        # --- 开枪 ---
        if command in {"开枪", "shoot", "shot"}:
            target_id = self.engine.resolve_target(game, rest[0]) if rest else None
            result = self.engine.hunter_shot(game, hunter_id=str(message.sender_id), target_id=target_id)
            await self._publish(game, result)
            await self._after_action(game)
            return result.message

        # --- 夜晚行动提示（群里拦截） ---
        if command in {"杀", "刀", "验", "查验", "救", "毒", "守", "守护"}:
            return "夜晚身份行动不要发在群里，请私聊我发送同样命令。"

        # --- 复盘 ---
        if command in {"复盘", "recap", "回顾"}:
            if game.phase != Phase.ENDED:
                return "游戏还没结束，不能复盘。"
            return self.narrator.recap(game)

        return self._help_text(game)

    # ------------------------------------------------------------------
    # Private command handler
    # ------------------------------------------------------------------

    async def handle_private_command(self, message: Message, args: list[str]) -> str:
        command = args[0] if args else "视角"
        rest = args[1:]
        game = self._find_game_for_player(str(message.sender_id))
        if not game:
            return "没找到你参与中的狼人杀。"

        if command in {"视角", "身份", "状态", "view", "status"}:
            return self.engine.player_view(game, str(message.sender_id))

        # 夜晚行动
        result = await self._apply_night_command(game, actor_id=str(message.sender_id), command=command, target_raw=rest[0] if rest else "")
        return result.message

    # ------------------------------------------------------------------
    # Bot action (from LifeChatter)
    # ------------------------------------------------------------------

    async def handle_bot_action(self, chat_stream: ChatStream, *, action: str, target: str = "") -> str:
        game = await self._game_for_chat_stream(chat_stream)
        if not game:
            return "当前群没有正在进行的狼人杀。"
        bot_id = await self._bot_id(game.platform)
        if not bot_id:
            return "无法获取当前 QQ Bot 身份。"

        if action in {"视角", "身份", "状态", "view", "status", ""}:
            return self.engine.player_view(game, bot_id)
        if action in {"加入", "join"}:
            return await self._join_bot(game)
        if action in {"投票", "票", "vote"}:
            target_id = self.engine.resolve_target(game, target)
            result = self.engine.vote(game, voter_id=bot_id, target_id=target_id)
            await self._publish(game, result)
            await self._after_action(game)
            return result.message

        result = await self._apply_night_command(game, actor_id=bot_id, command=action, target_raw=target)
        return result.message

    # ------------------------------------------------------------------
    # Internal: game flow automation
    # ------------------------------------------------------------------

    async def _on_game_started(self, game: GameState) -> None:
        """After game starts: send roles, night prompts, schedule AI."""
        await self._send_role_notices(game)
        await self._send_night_prompts(game)
        await self._run_ai_night_actions(game)

    async def _after_action(self, game: GameState) -> None:
        """After any player action, check if AI needs to act."""
        if game.phase == Phase.NIGHT:
            await self._run_ai_night_actions(game)
        elif game.phase == Phase.HUNTER_SHOT and game.pending_hunter_shot:
            hunter = game.players.get(game.pending_hunter_shot)
            if hunter and hunter.is_bot:
                await self._run_ai_hunter_shot(game, hunter)
        elif game.phase == Phase.LAST_WORDS and game.pending_last_words:
            # AI skip last words
            for uid in list(game.pending_last_words):
                p = game.players.get(uid)
                if p and p.is_bot:
                    self.engine.skip_last_words(game, user_id=uid)
            await self._publish_phase_change(game)

    async def _run_ai_night_actions(self, game: GameState) -> None:
        """Execute all AI bot night actions."""
        if game.phase != Phase.NIGHT:
            return
        for player in game.alive_players():
            if not player.is_bot:
                continue
            if player.role in (Role.VILLAGER, Role.IDIOT):
                continue
            decision = self.ai.decide_night_action(game, player)
            result = self.engine.night_action(
                game, actor_id=player.user_id, action=decision.action, target_id=decision.target_id
            )
            await self._publish(game, result)
            if game.phase != Phase.NIGHT:
                break  # Night resolved

        # If night resolved, handle aftermath
        if game.phase != Phase.NIGHT:
            await self._publish_phase_change(game)
            await self._after_action(game)

    async def _run_ai_hunter_shot(self, game: GameState, hunter: Player) -> None:
        target_id = self.ai.decide_hunter_shot(game, hunter)
        result = self.engine.hunter_shot(game, hunter_id=hunter.user_id, target_id=target_id)
        await self._publish(game, result)

    async def _publish_phase_change(self, game: GameState) -> None:
        """Publish narration for phase changes."""
        if game.phase == Phase.DAY_BREAK:
            pass  # Already handled in night resolution
        elif game.phase == Phase.SPEAKING:
            speaker = self.engine.get_current_speaker(game)
            if speaker:
                await self._send_public(game, self.narrator.speaking_prompt(speaker, 1, len(game.speaking_order)))
        elif game.phase == Phase.VOTE:
            await self._send_public(game, self.narrator.vote_prompt(game))
        elif game.phase == Phase.NIGHT:
            await self._send_public(game, self.narrator.night_falls(game))
            await self._send_night_prompts(game)

    # ------------------------------------------------------------------
    # Internal: messaging
    # ------------------------------------------------------------------

    async def _publish(self, game: GameState, result: ActionResult) -> None:
        if result.public_messages:
            for text in result.public_messages:
                await self._send_public(game, text)

    async def _send_public(self, game: GameState, text: str) -> None:
        if text:
            await send_text(text, stream_id=game.group_stream_id, platform=game.platform)

    async def _send_role_notices(self, game: GameState) -> None:
        for player in game.players.values():
            if player.is_bot:
                continue
            await self._send_private(game.platform, player.user_id, self.engine.role_notice(game, player.user_id))

    async def _send_night_prompts(self, game: GameState) -> None:
        if game.phase != Phase.NIGHT:
            return
        for player in game.alive_players():
            if player.is_bot:
                continue
            prompt = self.engine.night_prompt(game, player.user_id)
            if prompt:
                await self._send_private(game.platform, player.user_id, prompt)

    async def _send_private(self, platform: str, user_id: str, text: str) -> bool:
        adapter_signature = self._adapter_signature_for_platform(platform)
        if not adapter_signature:
            return False
        from src.core.managers.adapter_manager import get_adapter_manager
        adapter = get_adapter_manager().get_adapter(adapter_signature)
        if not adapter:
            return False
        envelope = {
            "direction": "outgoing",
            "message_info": {
                "platform": platform,
                "message_id": f"werewolf_{uuid4().hex}",
                "time": time.time(),
                "user_info": {"platform": platform, "user_id": str(user_id), "user_nickname": ""},
            },
            "message_segment": [{"type": "text", "data": text}],
        }
        try:
            await adapter._send_platform_message(envelope)
            return True
        except Exception as exc:
            logger.warning(f"狼人杀私聊发送失败: {user_id} {exc}")
            return False

    # ------------------------------------------------------------------
    # Internal: helpers
    # ------------------------------------------------------------------

    async def _apply_night_command(self, game: GameState, *, actor_id: str, command: str, target_raw: str) -> ActionResult:
        action = self._command_to_action(command)
        target_id = self.engine.resolve_target(game, target_raw) if target_raw else None
        result = self.engine.night_action(game, actor_id=actor_id, action=action, target_id=target_id)
        await self._publish(game, result)
        if result.ok and game.phase != Phase.NIGHT:
            await self._publish_phase_change(game)
            await self._after_action(game)
        return result

    async def _join_bot(self, game: GameState) -> str:
        bot_info = await self._bot_info(game.platform)
        bot_id = str((bot_info or {}).get("bot_id") or "")
        bot_name = str((bot_info or {}).get("bot_name") or "爱莉")
        if not bot_id:
            return "无法获取 QQ Bot 身份，暂时不能让爱莉加入。"
        result = self.engine.add_player(game, user_id=bot_id, display_name=bot_name, is_bot=True)
        return result.message

    def _help_text(self, game: GameState | None = None) -> str:
        lines = [
            "═══ 狼人杀指令 ═══",
            "/狼人杀 开局 [板子名] — 创建房间",
            "/狼人杀 加入 — 加入 | /狼人杀 加入 爱莉 — AI加入",
            "/狼人杀 开始 — 正式开局（房主）",
            "/狼人杀 板子 — 查看所有板子",
            "/狼人杀 状态 — 查看当前局面",
            "/狼人杀 投票 编号 — 白天投票",
            "/狼人杀 发言 <内容> — 结构化发言",
            "/狼人杀 自爆 — 狼人自爆",
            "/狼人杀 竞选 — 报名警长",
            "/狼人杀 选警长 编号 — 警长投票",
            "/狼人杀 移交 编号 — 警长移交",
            "/狼人杀 遗言 <内容> — 发表遗言",
            "/狼人杀 开枪 编号 — 猎人开枪",
            "/狼人杀 复盘 — 结束后回顾",
            "/狼人杀 结束 — 房主结束",
            "",
            "夜晚行动请私聊：杀/验/救/毒/守/跳过 + 编号",
        ]
        return "\n".join(lines)

    def _command_to_action(self, command: str) -> str:
        table = {
            "杀": "kill", "刀": "kill",
            "验": "check", "查验": "check",
            "救": "heal",
            "毒": "poison",
            "守": "guard", "守护": "guard",
            "跳过": "pass", "过": "pass",
        }
        return table.get(str(command).strip().lower(), str(command).strip().lower())

    def _cancel_timer(self, key: str) -> None:
        task = self._timers.pop(key, None)
        if task and not task.done():
            task.cancel()

    def _find_game_for_player(self, user_id: str) -> GameState | None:
        candidates = [
            g for g in self.games.values()
            if g.phase != Phase.ENDED and user_id in g.players
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda g: g.created_at, reverse=True)
        return candidates[0]

    def _group_id_from_message(self, message: Message) -> str:
        return str(message.extra.get("group_id") or message.extra.get("target_group_id") or "")

    def _sender_name(self, message: Message) -> str:
        return str(message.sender_cardname or message.sender_name or message.sender_id)

    def _key(self, platform: str, group_id: str) -> str:
        return f"{platform}:{group_id}"

    def _adapter_signature_for_platform(self, platform: str) -> str | None:
        from src.core.managers.adapter_manager import get_adapter_manager
        for signature, adapter in get_adapter_manager().get_all_adapters().items():
            if str(getattr(adapter, "platform", "")) == platform:
                return signature
        return None

    async def _bot_info(self, platform: str) -> dict[str, str] | None:
        from src.core.managers.adapter_manager import get_adapter_manager
        return await get_adapter_manager().get_bot_info_by_platform(platform)

    async def _bot_id(self, platform: str) -> str:
        bot_info = await self._bot_info(platform)
        return str((bot_info or {}).get("bot_id") or "")

    async def _game_for_chat_stream(self, chat_stream: ChatStream) -> GameState | None:
        group_id = ""
        if chat_stream.chat_type == "group":
            from src.core.managers.stream_manager import get_stream_manager
            info = await get_stream_manager().get_stream_info(chat_stream.stream_id)
            group_id = str((info or {}).get("group_id") or "")
        if not group_id:
            return None
        return self.games.get(self._key(chat_stream.platform, group_id))

