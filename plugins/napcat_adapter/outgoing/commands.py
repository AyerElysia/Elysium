"""命令系统处理器

处理来自核心的 command / adapter_command 消息：
- command: 旧式命令（保留向后兼容）
- adapter_command: 新式通用命令（全量 API 透传）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mofox_wire import MessageEnvelope, SegPayload

from src.app.plugin_system.api.log_api import get_logger

from ..utils.constants import CommandType

if TYPE_CHECKING:
    from ..client import NapCatClient
    from ..config import NapcatAdapterConfig

logger = get_logger("napcat_adapter")


class CommandHandler:
    """命令系统处理器。"""

    def __init__(self, client: "NapCatClient", get_config: Any, core_sink: Any = None) -> None:
        self._client = client
        self._get_config = get_config
        self._core_sink = core_sink

    def set_core_sink(self, core_sink: Any) -> None:
        """设置 core_sink（用于返回 adapter_response）。"""
        self._core_sink = core_sink

    def _config(self) -> "NapcatAdapterConfig | None":
        return self._get_config()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def handle(self, envelope: MessageEnvelope) -> None:
        """处理命令类消息。"""
        segment: SegPayload = envelope.get("message_segment", {})  # type: ignore
        if isinstance(segment, list):
            segment = segment[0] if segment else {}

        seg_type = segment.get("type")

        if seg_type == "command":
            await self._handle_legacy_command(envelope)
        elif seg_type == "adapter_command":
            await self._handle_adapter_command(envelope)
        elif seg_type == "adapter_response":
            logger.debug("收到 adapter_response，跳过")
        else:
            logger.debug(f"未知命令类型: {seg_type}")

    # ------------------------------------------------------------------
    # 旧式命令（向后兼容）
    # ------------------------------------------------------------------

    async def _handle_legacy_command(self, envelope: MessageEnvelope) -> None:
        """处理旧式 command 消息。"""
        message_info = envelope.get("message_info", {})
        group_info = message_info.get("group_info")
        segment: SegPayload = envelope.get("message_segment", {})  # type: ignore
        if isinstance(segment, list):
            segment = segment[0] if segment else {}

        seg_data = segment.get("data", {})
        command_name = seg_data.get("name")
        args = seg_data.get("args", {})
        if not isinstance(args, dict):
            args = {}

        try:
            action, params = self._resolve_legacy_command(command_name, args, group_info)
        except Exception as e:
            logger.error(f"命令解析失败: {command_name}, error={e}")
            return

        if not action:
            logger.error(f"未知命令: {command_name}")
            return

        logger.debug(f"执行旧式命令: {action}, params={params}")
        resp = await self._client.call(action, params)

        if resp.get("status") == "ok":
            logger.info(f"命令 {command_name} 执行成功")
        else:
            logger.warning(f"命令 {command_name} 执行失败: {resp}")

    def _resolve_legacy_command(
        self, command_name: str | None, args: dict, group_info: dict | None
    ) -> tuple[str | None, dict]:
        """解析旧式命令为 (action, params)。"""
        if not command_name:
            return None, {}

        group_id = int(group_info.get("group_id", 0)) if group_info and group_info.get("group_id") else 0

        match command_name:
            case CommandType.GROUP_BAN.name:
                return CommandType.GROUP_BAN.value, {
                    "group_id": group_id,
                    "user_id": int(args["qq_id"]),
                    "duration": min(int(args["duration"]), 2592000),
                }

            case CommandType.GROUP_WHOLE_BAN.name:
                return CommandType.GROUP_WHOLE_BAN.value, {
                    "group_id": group_id,
                    "enable": bool(args["enable"]),
                }

            case CommandType.GROUP_KICK.name:
                return CommandType.GROUP_KICK.value, {
                    "group_id": group_id,
                    "user_id": int(args["qq_id"]),
                    "reject_add_request": False,
                }

            case CommandType.SEND_POKE.name:
                return CommandType.SEND_POKE.value, {
                    "group_id": group_id or None,
                    "user_id": int(args["qq_id"]),
                }

            case CommandType.DELETE_MSG.name:
                return CommandType.DELETE_MSG.value, {"message_id": args["message_id"]}

            case CommandType.AI_VOICE_SEND.name:
                return CommandType.AI_VOICE_SEND.value, {
                    "group_id": group_id,
                    "text": args["text"],
                    "character": args["character"],
                }

            case CommandType.SET_EMOJI_LIKE.name:
                return CommandType.SET_EMOJI_LIKE.value, {
                    "message_id": int(args["message_id"]),
                    "emoji_id": int(args["emoji_id"]),
                    "set": bool(args["set"]),
                }

            case CommandType.SEND_AT_MESSAGE.name:
                message_payload = [
                    {"type": "at", "data": {"qq": str(args["qq_id"])}},
                    {"type": "text", "data": {"text": " " + str(args.get("text", ""))}},
                ]
                return "send_group_msg", {"group_id": group_id, "message": message_payload}

            case CommandType.SEND_LIKE.name:
                return CommandType.SEND_LIKE.value, {
                    "user_id": int(args["qq_id"]),
                    "times": int(args.get("times", 1)),
                }

            case _:
                return None, {}

    # ------------------------------------------------------------------
    # 新式 adapter_command（通用 API 透传）
    # ------------------------------------------------------------------

    async def _handle_adapter_command(self, envelope: MessageEnvelope) -> None:
        """处理 adapter_command 消息（通用 API 调用）。"""
        segment: SegPayload = envelope.get("message_segment", {})  # type: ignore
        if isinstance(segment, list):
            segment = segment[0] if segment else {}

        seg_data = segment.get("data", {})
        action = seg_data.get("action")
        params = seg_data.get("params", {})
        request_id = seg_data.get("request_id")
        timeout = float(seg_data.get("timeout", 30.0))

        if not action:
            logger.error("adapter_command 缺少 action 参数")
            return

        logger.debug(f"执行 adapter_command: {action}")

        try:
            # 特殊处理 RAW_API 透传
            if action == CommandType.RAW_API.value:
                raw_action = params.get("action")
                raw_params = params.get("params", {})
                if not raw_action:
                    logger.error("RAW_API 缺少内部 action")
                    return
                response = await self._client.call(raw_action, raw_params, timeout=timeout)
            else:
                response = await self._client.call(action, params, timeout=timeout)
        except Exception as e:
            logger.error(f"adapter_command 执行失败: {action}, error={e}")
            response = {"status": "error", "message": str(e)}

        # 返回 adapter_response
        if request_id and self._core_sink:
            response_envelope: MessageEnvelope = {
                "direction": "incoming",  # type: ignore
                "message_info": {
                    "message_id": str(request_id),
                    "platform": "qq",
                    "time": 0,
                },
                "message_segment": {  # type: ignore
                    "type": "adapter_response",
                    "data": {
                        "request_id": request_id,
                        "response": response,
                    },
                },
            }
            await self._core_sink.send(response_envelope)
            logger.debug(f"已发送 adapter_response: request_id={request_id}")

        if response.get("status") == "ok":
            logger.info(f"adapter_command {action} 执行成功")
        else:
            logger.warning(f"adapter_command {action} 执行失败: {response}")
