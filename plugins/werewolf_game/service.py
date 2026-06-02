"""Runtime service for QQ Werewolf games."""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseService
from src.core.models.message import Message
from src.core.models.stream import ChatStream

from .engine import ActionResult, WerewolfEngine
from .models import GameState, Phase, Role

logger = get_logger("werewolf_game")


class WerewolfGameService(BaseService):
    """Stateful facade around the deterministic Werewolf engine."""

    service_name = "werewolf_game"
    service_description = "QQ 群狼人杀裁判服务"
    version = "0.1.0"

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
        if getattr(plugin, "_werewolf_engine", None) is None:
            plugin._werewolf_engine = WerewolfEngine()
        if getattr(plugin, "_werewolf_games", None) is None:
            plugin._werewolf_games = {}
        self.engine: WerewolfEngine = plugin._werewolf_engine
        self.games: dict[str, GameState] = plugin._werewolf_games

    async def handle_group_command(self, message: Message, args: list[str]) -> str:
        command = args[0] if args else "状态"
        rest = args[1:]
        platform = str(message.platform or "qq")
        group_id = self._group_id_from_message(message)
        if not group_id:
            return "狼人杀只能在群聊里开局。"
        key = self._key(platform, group_id)

        if command in {"开局", "创建", "create", "new"}:
            if key in self.games and self.games[key].phase != Phase.ENDED:
                return "这个群已经有一局狼人杀在进行。"
            game = self.engine.create_game(
                platform=platform,
                group_id=group_id,
                group_name=str(message.extra.get("group_name") or ""),
                group_stream_id=message.stream_id,
                owner_id=str(message.sender_id),
            )
            self.games[key] = game
            result = self.engine.add_player(
                game,
                user_id=str(message.sender_id),
                display_name=self._sender_name(message),
            )
            return (
                f"狼人杀房间已创建。\n{result.message}\n"
                "发送 /狼人杀 加入 参与，房主发送 /狼人杀 开始 发牌。\n"
                "三人测试可用 /狼人杀 测试开始。"
            )

        game = self.games.get(key)

        if command in {"帮助", "help", "指令", "commands"}:
            return self._help_text(game)

        if not game:
            return "这个群还没有狼人杀房间。发送 /狼人杀 开局 创建。"

        if command in {"加入", "join"}:
            if rest and rest[0].strip().lower() in {"爱莉", "aili", "bot", "机器人"}:
                return await self._join_bot(game)
            result = self.engine.add_player(
                game,
                user_id=str(message.sender_id),
                display_name=self._sender_name(message),
            )
            if result.ok:
                return f"{result.message}\n{self.engine.public_guidance(game)}"
            return result.message

        if command in {"退出", "离开", "quit", "leave"}:
            return self.engine.remove_player(game, str(message.sender_id)).message

        if command in {"开始", "start"}:
            if str(message.sender_id) != game.owner_id:
                return "只有房主可以开始本局。"
            result = self.engine.start_game(game)
            if not result.ok:
                return result.message
            await self._send_role_notices(game)
            await self._send_night_prompts(game)
            return "\n".join(result.public_messages or [result.message])

        if command in {"测试开始", "测试", "teststart", "test"}:
            if str(message.sender_id) != game.owner_id:
                return "只有房主可以开始本局。"
            result = self.engine.start_test_game(game)
            if not result.ok:
                return result.message
            await self._send_role_notices(game)
            await self._send_night_prompts(game)
            return "\n".join(result.public_messages or [result.message])

        if command in {"状态", "status", "玩家", "players"}:
            return self.engine.public_status(game)

        if command in {"结束", "end", "stop"}:
            if str(message.sender_id) != game.owner_id:
                return "只有房主可以结束本局。"
            game.phase = Phase.ENDED
            game.ended_reason = "房主结束了本局。"
            return "本局狼人杀已结束。"

        if command in {"投票", "票", "vote"}:
            if not rest:
                return "投票需要指定编号，例如：/狼人杀 投票 2"
            target_id = self.engine.resolve_target(game, rest[0])
            result = self.engine.vote(game, voter_id=str(message.sender_id), target_id=target_id)
            await self._publish_public_messages(game, result)
            if result.ok and game.phase == Phase.NIGHT:
                await self._send_night_prompts(game)
            return result.message

        if command in {"杀", "刀", "验", "查验", "救", "毒", "跳过", "过", "kill", "check", "heal", "poison", "pass"}:
            return "夜晚身份行动不要发在群里，请私聊我发送同样命令。"

        return self._help_text(game)

    async def handle_private_command(self, message: Message, args: list[str]) -> str:
        command = args[0] if args else "视角"
        rest = args[1:]
        game = self._find_game_for_player(str(message.sender_id))
        if not game:
            return "没找到你参与中的狼人杀。"

        if command in {"视角", "身份", "状态", "view", "status"}:
            return self.engine.player_view(game, str(message.sender_id))

        result = await self._apply_player_command(
            game,
            actor_id=str(message.sender_id),
            command=command,
            target_raw=rest[0] if rest else "",
        )
        return result.message

    async def handle_bot_action(
        self,
        chat_stream: ChatStream,
        *,
        action: str,
        target: str = "",
    ) -> str:
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
            await self._publish_public_messages(game, result)
            if result.ok and game.phase == Phase.NIGHT:
                await self._send_night_prompts(game)
            return result.message

        result = await self._apply_player_command(
            game,
            actor_id=bot_id,
            command=action,
            target_raw=target,
        )
        return result.message

    async def _apply_player_command(
        self,
        game: GameState,
        *,
        actor_id: str,
        command: str,
        target_raw: str,
    ) -> ActionResult:
        action = self._command_to_action(command)
        target_id = self.engine.resolve_target(game, target_raw) if target_raw else None
        result = self.engine.night_action(
            game,
            actor_id=actor_id,
            action=action,
            target_id=target_id,
        )
        await self._publish_public_messages(game, result)
        return result

    async def _publish_public_messages(self, game: GameState, result: ActionResult) -> None:
        if not result.public_messages:
            return
        for text in result.public_messages:
            await send_text(text, stream_id=game.group_stream_id, platform=game.platform)

    async def _send_role_notices(self, game: GameState) -> None:
        for player in game.players.values():
            if player.is_bot:
                continue
            await self._send_private_referee_message(
                game.platform,
                player.user_id,
                self.engine.role_notice(game, player.user_id),
            )

    async def _send_night_prompts(self, game: GameState) -> None:
        if game.phase != Phase.NIGHT:
            return
        player_list = self._public_player_list(game)
        for player in game.alive_players():
            if player.is_bot:
                continue
            prompt = ""
            if player.role == Role.WEREWOLF:
                prompt = f"第 {game.day_number} 夜，请选择刀人目标：\n{player_list}\n/狼人杀 杀 编号，或 /狼人杀 跳过"
            elif player.role == Role.SEER:
                prompt = f"第 {game.day_number} 夜，请选择查验目标：\n{player_list}\n/狼人杀 验 编号，或 /狼人杀 跳过"
            elif player.role == Role.WITCH:
                prompt = f"第 {game.day_number} 夜，女巫行动：\n{player_list}\n/狼人杀 救、/狼人杀 毒 编号，或 /狼人杀 跳过"
            if prompt:
                await self._send_private_referee_message(game.platform, player.user_id, prompt)

    async def _join_bot(self, game: GameState) -> str:
        bot_info = await self._bot_info(game.platform)
        bot_id = str((bot_info or {}).get("bot_id") or "")
        bot_name = str((bot_info or {}).get("bot_name") or "爱莉")
        if not bot_id:
            return "无法获取 QQ Bot 身份，暂时不能让爱莉加入。"
        result = self.engine.add_player(
            game,
            user_id=bot_id,
            display_name=bot_name or "爱莉",
            is_bot=True,
        )
        if result.ok:
            return f"{result.message}\n{self.engine.public_guidance(game)}"
        return result.message

    def _help_text(self, game: GameState | None = None) -> str:
        lines = [
            "狼人杀指令：",
            "/狼人杀 状态 - 查看当前阶段、玩家列表和下一步",
            "/狼人杀 加入 - 加入当前房间",
            "/狼人杀 加入 爱莉 - 让爱莉作为玩家加入",
            "/狼人杀 开始 - 正式开局，至少 6 人，房主可用",
            "/狼人杀 测试开始 - 三人测试开局，房主可用",
            "/狼人杀 投票 编号 - 白天在群里投票",
            "/狼人杀 结束 - 房主结束本局",
            "夜晚行动请私聊爱莉：/狼人杀 身份、杀 编号、验 编号、救、毒 编号、跳过",
        ]
        if game:
            lines.extend(["", self.engine.public_guidance(game)])
        return "\n".join(lines)

    async def _send_private_referee_message(self, platform: str, user_id: str, text: str) -> bool:
        adapter_signature = self._adapter_signature_for_platform(platform)
        if not adapter_signature:
            logger.warning(f"未找到平台适配器，无法发送狼人杀私聊: platform={platform}")
            return False

        from src.core.managers.adapter_manager import get_adapter_manager

        adapter = get_adapter_manager().get_adapter(adapter_signature)
        if not adapter:
            logger.warning(f"平台适配器未激活，无法发送狼人杀私聊: {adapter_signature}")
            return False

        envelope = {
            "direction": "outgoing",
            "message_info": {
                "platform": platform,
                "message_id": f"werewolf_referee_{uuid4().hex}",
                "time": time.time(),
                "user_info": {
                    "platform": platform,
                    "user_id": str(user_id),
                    "user_nickname": "",
                },
            },
            "message_segment": [{"type": "text", "data": text}],
        }
        try:
            await adapter._send_platform_message(envelope)
            return True
        except Exception as exc:
            logger.warning(f"狼人杀私聊发送失败: user_id={user_id} error={exc}")
            return False

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

    def _find_game_for_player(self, user_id: str) -> GameState | None:
        candidates = [
            game
            for game in self.games.values()
            if game.phase != Phase.ENDED and user_id in game.players
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.created_at, reverse=True)
        return candidates[0]

    def _group_id_from_message(self, message: Message) -> str:
        return str(
            message.extra.get("group_id")
            or message.extra.get("target_group_id")
            or ""
        )

    def _sender_name(self, message: Message) -> str:
        return str(message.sender_cardname or message.sender_name or message.sender_id)

    def _public_player_list(self, game: GameState) -> str:
        lines = []
        for index, player in enumerate(game.players.values(), start=1):
            lines.append(f"{index}. {player.display_name}（{'存活' if player.alive else '出局'}）")
        return "\n".join(lines)

    def _command_to_action(self, command: str) -> str:
        table = {
            "杀": "kill",
            "刀": "kill",
            "验": "check",
            "查验": "check",
            "救": "heal",
            "毒": "poison",
            "跳过": "pass",
            "过": "pass",
        }
        return table.get(str(command).strip().lower(), str(command).strip().lower())

    def _key(self, platform: str, group_id: str) -> str:
        return f"{platform}:{group_id}"
