"""life_engine 命令处理器。"""

from __future__ import annotations

import re
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision

logger = get_logger("life_engine", display="life_engine")


class LifeEngineCommandHandler(BaseEventHandler):
    """处理 life_engine 相关的命令。"""

    plugin_name = "life_engine"
    handler_name = "command_handler"
    handler_description = "处理 life_engine 命令（心跳/直连留言）"
    # 需要高于 command_dispatch_plugin(2000)，避免被“未知命令”先拦截。
    weight = 2100
    intercept_message = False
    init_subscribe: list[EventType | str] = [
        EventType.ON_MESSAGE_RECEIVED,
    ]

    _HEARTBEAT_COMMANDS = {"/heartbeat", "/心跳", "!heartbeat", "!心跳"}
    _DREAM_COMMANDS = {"/dream", "!dream", "/做梦", "!做梦"}
    _DIRECT_MESSAGE_COMMANDS = {
        "/life",
        "!life",
        "/nucleus",
        "!nucleus",
        "/中枢",
        "!中枢",
        "/生命中枢",
        "!生命中枢",
    }
    _CQ_AT_PATTERN = re.compile(r"\[CQ:at,[^\]]+\]")
    _AT_TOKEN_PATTERN = re.compile(r"@\S+")

    @classmethod
    def _normalize_command_text(cls, content: str) -> str:
        """规范化命令文本，去除 @ 信息并折叠空白。"""
        text = content.strip()
        if not text:
            return ""

        text = cls._CQ_AT_PATTERN.sub(" ", text)
        text = cls._AT_TOKEN_PATTERN.sub(" ", text)
        return " ".join(text.split())

    @classmethod
    def _extract_heartbeat_command(cls, content: str) -> str | None:
        """提取心跳命令（支持 @机器人 + 命令）。"""
        normalized = cls._normalize_command_text(content)
        if not normalized:
            return None

        tokens = [token for token in normalized.split() if token]
        if len(tokens) != 1:
            return None
        token = tokens[0]
        return token if token in cls._HEARTBEAT_COMMANDS else None

    @classmethod
    def _extract_dream_command(cls, content: str) -> str | None:
        """提取手动做梦命令（支持 @机器人 + 命令）。"""
        normalized = cls._normalize_command_text(content)
        if not normalized:
            return None

        tokens = [token for token in normalized.split() if token]
        if len(tokens) != 1:
            return None
        token = tokens[0]
        return token if token in cls._DREAM_COMMANDS else None

    @classmethod
    def _extract_direct_message(cls, content: str) -> str | None:
        """提取直连生命中枢命令内容。

        返回：
        - None: 不是直连命令
        - "": 是直连命令但缺少正文
        - 非空字符串: 直连正文
        """
        normalized = cls._normalize_command_text(content)
        if not normalized:
            return None

        for cmd in cls._DIRECT_MESSAGE_COMMANDS:
            if normalized == cmd:
                return ""
            prefix = f"{cmd} "
            if normalized.startswith(prefix):
                return normalized[len(prefix):].strip()
        return None

    async def _send_reply(self, trigger_message: Any, content: str) -> None:
        """向触发命令的会话发送一条文本回复。"""
        from src.core.models.message import Message as CoreMessage
        from src.core.models.message import MessageType
        from src.core.transport.message_send import get_message_sender
        from uuid import uuid4

        reply_message = CoreMessage(
            message_id=f"life_cmd_reply_{uuid4().hex}",
            platform=trigger_message.platform,
            chat_type=trigger_message.chat_type,
            stream_id=trigger_message.stream_id,
            sender_id="",
            sender_name="Bot",
            sender_role="assistant",
            message_type=MessageType.TEXT,
            content=content,
            processed_plain_text=content,
            time=trigger_message.time,
        )
        if getattr(trigger_message, "chat_type", "") == "group":
            group_id = ""
            group_name = ""
            extra = getattr(trigger_message, "extra", {}) or {}
            if isinstance(extra, dict):
                group_id = str(extra.get("group_id") or extra.get("target_group_id") or "").strip()
                group_name = str(extra.get("group_name") or extra.get("target_group_name") or "").strip()
            if group_id:
                reply_message.extra["target_group_id"] = group_id
            if group_name:
                reply_message.extra["target_group_name"] = group_name
        else:
            user_id = str(getattr(trigger_message, "sender_id", "") or "").strip()
            user_name = (
                str(getattr(trigger_message, "sender_cardname", "") or "").strip()
                or str(getattr(trigger_message, "sender_name", "") or "").strip()
            )
            if user_id:
                reply_message.extra["target_user_id"] = user_id
            if user_name:
                reply_message.extra["target_user_name"] = user_name

        sender = get_message_sender()
        await sender.send_message(reply_message)

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """检查消息是否是 life_engine 命令，如果是则执行。"""
        if event_name != EventType.ON_MESSAGE_RECEIVED.value:
            return EventDecision.PASS, params

        message = params.get("message")
        if message is None:
            return EventDecision.PASS, params

        # 获取消息文本
        content = getattr(message, "processed_plain_text", "") or getattr(message, "content", "")
        if not isinstance(content, str):
            return EventDecision.PASS, params

        content = content.strip()

        plugin = self.plugin
        if getattr(plugin, "plugin_name", "") != "life_engine":
            return EventDecision.PASS, params

        service = getattr(plugin, "service", None)
        if service is None:
            return EventDecision.PASS, params

        # 1) 心跳命令（支持 @机器人 + 命令）
        command = self._extract_heartbeat_command(content)
        if command is not None:
            try:
                logger.info(f"收到手动触发心跳命令: {command}")
                result = await service.trigger_heartbeat_manually()

                if result.get("success"):
                    reply_text = (
                        f"✓ 心跳已手动触发\n"
                        f"序号: #{result.get('heartbeat_count')}\n"
                        f"事件数: {result.get('event_count')}\n"
                        f"回复: {result.get('reply', '（无）')[:200]}"
                    )
                else:
                    reply_text = f"✗ 心跳触发失败: {result.get('error', '未知错误')}"

                await self._send_reply(message, reply_text)
                logger.info(f"已回复心跳命令结果: success={result.get('success')}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"处理心跳命令失败: {exc}")

            return EventDecision.STOP, params

        # 1.5) 做梦命令（支持 @机器人 + 命令）
        dream_command = self._extract_dream_command(content)
        if dream_command is not None:
            try:
                logger.info(f"收到手动触发做梦命令: {dream_command}")
                result = await service.trigger_dream_manually()

                if result.get("success"):
                    dream_text = str(result.get("dream_text") or "").strip()
                    if len(dream_text) > 280:
                        dream_text = dream_text[:279] + "…"
                    residue = result.get("dream_residue") or {}
                    residue_summary = str(residue.get("summary") or "").strip()
                    archive_path = str(result.get("archive_path") or "").strip()
                    seed_titles = result.get("seed_titles") or []
                    reply_text = (
                        f"✓ 做梦已手动触发\n"
                        f"Dream ID: {result.get('dream_id')}\n"
                        f"耗时: {result.get('duration_seconds')}s\n"
                        f"NREM: {result.get('nrem_episodes')} 集 / {result.get('nrem_steps')} 步\n"
                        f"REM: 激活 {result.get('rem_nodes')} 节点，新增 {result.get('rem_new_edges')} 边，修剪 {result.get('rem_pruned_edges')} 边\n"
                        f"梦核: {', '.join(seed_titles) if seed_titles else '（无）'}\n"
                        f"余韵: {residue_summary or '（无）'}\n"
                        f"梦札: {archive_path or '（未写入）'}\n"
                        f"梦境:\n{dream_text or '（无正文）'}"
                    )
                else:
                    reply_text = f"✗ 做梦触发失败: {result.get('error', '未知错误')}"

                await self._send_reply(message, reply_text)
                logger.info(f"已回复做梦命令结果: success={result.get('success')}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"处理做梦命令失败: {exc}")

            return EventDecision.STOP, params

        # 2) 直连命令：绕过 DFC，直接把内容投递到 life_engine
        direct_message = self._extract_direct_message(content)
        if direct_message is None:
            return EventDecision.PASS, params

        try:
            if not direct_message:
                await self._send_reply(
                    message,
                    "用法：/life 你想对生命中枢说的话\n"
                    "也支持：/nucleus ... /中枢 ...",
                )
                return EventDecision.STOP, params

            sender_display = (
                str(getattr(message, "sender_cardname", "") or "").strip()
                or str(getattr(message, "sender_name", "") or "").strip()
                or str(getattr(message, "sender_id", "") or "").strip()
                or "外部用户"
            )
            receipt = await service.enqueue_direct_message(
                message=direct_message,
                stream_id=str(getattr(message, "stream_id", "") or "").strip(),
                platform=str(getattr(message, "platform", "") or "").strip(),
                chat_type=str(getattr(message, "chat_type", "") or "").strip(),
                sender_name=sender_display,
                sender_id=str(getattr(message, "sender_id", "") or "").strip(),
            )
            event_id = str(receipt.get("event_id") or "unknown")

            # 直连留言后立刻主动触发一次心跳，尽量即时处理
            heartbeat_result = await service.trigger_heartbeat_manually()
            if heartbeat_result.get("success"):
                heartbeat_count = heartbeat_result.get("heartbeat_count")
                heartbeat_reply = str(heartbeat_result.get("reply") or "").strip()
                if len(heartbeat_reply) > 120:
                    heartbeat_reply = heartbeat_reply[:119] + "…"
                reply_text = (
                    f"✓ 已直达生命中枢（event_id={event_id}）并立即触发心跳 #{heartbeat_count}\n"
                    f"中枢回应: {heartbeat_reply or '（空）'}"
                )
            else:
                heartbeat_error = str(heartbeat_result.get("error") or "未知错误")
                reply_text = (
                    f"✓ 已直达生命中枢（event_id={event_id}）\n"
                    f"⚠️ 已尝试立即触发心跳但未成功: {heartbeat_error}\n"
                    "消息仍已入队，会在后续心跳中处理。"
                )

            await self._send_reply(
                message,
                reply_text,
            )
            logger.info(
                "已处理 life 直连命令: "
                f"stream_id={getattr(message, 'stream_id', '')} event_id={event_id} "
                f"heartbeat_success={bool(heartbeat_result.get('success'))}"
            )

        except Exception as exc:  # noqa: BLE001
            logger.error(f"处理 life 直连命令失败: {exc}")
            try:
                await self._send_reply(message, f"✗ 投递到生命中枢失败: {exc}")
            except Exception:
                pass

        # 拦截这条消息，不让它进入正常的对话流程（实现“绕过 DFC”）
        return EventDecision.STOP, params
