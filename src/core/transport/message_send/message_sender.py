"""消息发送器。

负责将 Message 发送到正确的 Adapter。
参考 old/chat/message_receive/uni_message_sender.py 的设计。
"""

import hashlib
import json
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from mofox_wire import MessageEnvelope

from src.kernel.logger import get_logger

if TYPE_CHECKING:
    from src.core.models.message import Message

logger = get_logger("message_sender")

_DEDUP_WINDOW_SECONDS = 30.0
_DEDUP_MAX_ENTRIES = 256


class MessageSender:
    """消息发送器。

    负责将 Message 发送到 Adapter。

    职责：
    1. 使用 MessageConverter 将 Message 转换为 MessageEnvelope
    2. 根据 platform 推断目标 Adapter
    3. 通过 AdapterManager 获取 Adapter 实例
    4. 发布发送前事件并调用 Adapter._send_platform_message() 发送消息
    5. 历史写入后发布确认投递事件

    Attributes:
        _converter: 消息转换器
        _adapter_manager: 适配器管理器引用

    Examples:
        >>> sender = MessageSender()
        >>> success = await sender.send_message(message)
    """

    def __init__(self) -> None:
        """初始化消息发送器。"""
        from src.core.transport.message_receive.converter import MessageConverter

        self._converter = MessageConverter()
        self._adapter_manager: Any = None
        self._unknown_delivery_fingerprints: deque[tuple[float, str]] = deque()
        self._unknown_delivery_fingerprint_set: set[str] = set()
        self._dedupe_lock = threading.Lock()
        logger.info("MessageSender 初始化完成")

    def set_adapter_manager(self, adapter_manager: Any) -> None:
        """设置适配器管理器引用。

        Args:
            adapter_manager: 适配器管理器实例

        Examples:
            >>> sender.set_adapter_manager(get_adapter_manager())
        """
        self._adapter_manager = adapter_manager
        logger.debug("MessageSender 设置适配器管理器")

    async def send_message(
        self,
        message: "Message",
        adapter_signature: str | None = None,
    ) -> bool:
        """发送消息到 Adapter。

        Args:
            message: 待发送的消息
            adapter_signature: 目标适配器签名（None 表示自动推断）

        Returns:
            bool: 是否发送成功

        Raises:
            ValueError: 如果消息格式不正确或无法确定目标 Adapter

        Examples:
            >>> success = await sender.send_message(message)
            >>> success = await sender.send_message(message, "my_plugin:adapter:qq")
        """
        adapter_send_started = False
        adapter_send_completed = False
        timeout_fingerprint = ""
        try:
            # 1. 确定目标 Adapter
            if not adapter_signature:
                # live / game operator 平台走虚拟发送，无需查找 Adapter，优先判断以避免误报 WARNING
                if self._should_use_virtual_send(message):
                    return await self._send_virtual_message(message)
                adapter_signature = self._infer_adapter_signature(message)

            if not adapter_signature:
                logger.error(
                    f"无法确定目标 Adapter: platform={message.platform}, "
                    f"message_id={message.message_id}"
                )
                return False

            # 2. 获取 Adapter 实例
            if not self._adapter_manager:
                from src.core.managers.adapter_manager import get_adapter_manager

                self._adapter_manager = get_adapter_manager()

            adapter = self._adapter_manager.get_adapter(adapter_signature)

            if not adapter:
                logger.error(
                    f"Adapter 未找到: {adapter_signature}, "
                    f"message_id={message.message_id}"
                )
                return False

            # 3. 使用 bot 信息覆盖 sender 字段
            await self._apply_bot_sender_info(message, adapter)

            # 4. 转换为 MessageEnvelope
            envelope = await self._converter.message_to_envelope(message)

            # 5. 发布发送前事件，允许处理器拦截。
            should_send = await self._emit_send_event(
                message,
                envelope,
                adapter_signature,
            )
            if not should_send:
                logger.info(f"消息被事件处理器拦截，取消发送: {message.message_id}")
                return True

            # 6. 仅在前置处理器允许发送后检查投递状态未知的短窗重试。
            timeout_fingerprint = self._build_dedupe_fingerprint(
                message,
                envelope,
                adapter_signature,
            )
            if (
                timeout_fingerprint
                and self._has_unknown_delivery_fingerprint(timeout_fingerprint)
            ):
                logger.warning(
                    "检测到投递状态未知消息的短窗重试，已跳过: "
                    f"message_id={message.message_id}, platform={message.platform}, "
                    f"stream_id={message.stream_id}"
                )
                return True

            # 7. 调用开始后的超时无法判断平台是否已收到消息。
            adapter_send_started = True
            await adapter._send_platform_message(envelope)
            adapter_send_completed = True

            # 8. 写入历史消息。
            history_persisted = await self._persist_sent_message_to_history(message)

            # 9. 适配器成功且历史实际写入后发布确认投递事件。
            if history_persisted:
                await self._emit_delivery_event(message, envelope, adapter_signature)

            # 提取消息文本用于日志
            msg_text = (
                message.processed_plain_text
                or (message.content if isinstance(message.content, str) else "")
                or "(无文本内容)"
            )
            # 日志中截断超长内容
            if len(msg_text) > 100:
                msg_text = msg_text[:100] + "..."

            logger.debug(
                f"消息发送成功: {message.message_id} → {adapter_signature}"
            )
            logger.info(f'消息发送成功: [dim]{msg_text}[/dim]')

            return True

        except ValueError as e:
            logger.error(f"消息格式错误: {e}")
            return False
        except Exception as e:
            logger.error(
                f"发送消息失败: message_id={message.message_id}, error={e}",
                exc_info=True,
            )
            delivery_unknown = (
                adapter_send_started
                and not adapter_send_completed
                and self._is_timeout_exception(e)
            )
            if delivery_unknown and timeout_fingerprint:
                self._remember_unknown_delivery_fingerprint(timeout_fingerprint)
                logger.warning(
                    "平台发送超时，投递状态未知；保留短窗指纹以抑制立即重复发送: "
                    f"message_id={message.message_id}"
                )
            elif delivery_unknown:
                logger.warning(
                    "平台发送超时，投递状态未知；无法构造安全指纹，不抑制重试: "
                    f"message_id={message.message_id}"
                )
            return False

    @staticmethod
    def _is_timeout_exception(error: BaseException) -> bool:
        """沿异常链识别内置及 httpx/httpcore 超时，不引入传输层依赖。"""

        cursor: BaseException | None = error
        seen: set[int] = set()
        while cursor is not None and id(cursor) not in seen:
            seen.add(id(cursor))
            if isinstance(cursor, TimeoutError):
                return True

            error_type = type(cursor)
            module = str(getattr(error_type, "__module__", "") or "").lower()
            name = str(getattr(error_type, "__name__", "") or "").lower()
            if module.startswith(("httpx", "httpcore")) and (
                name.endswith("timeout") or name == "timeoutexception"
            ):
                return True
            cursor = cursor.__cause__ or cursor.__context__
        return False

    @classmethod
    def _build_dedupe_fingerprint(
        cls,
        message: "Message",
        envelope: MessageEnvelope,
        adapter_signature: str,
    ) -> str:
        """仅在出站目标和消息段均明确时构造未知投递重试指纹。"""

        try:
            extra = getattr(message, "extra", {}) or {}
            if not isinstance(extra, dict):
                extra = {}

            message_info = envelope.get("message_info") or {}
            if not isinstance(message_info, dict):
                message_info = {}
            user_info = message_info.get("user_info") or {}
            group_info = message_info.get("group_info") or {}
            if not isinstance(user_info, dict):
                user_info = {}
            if not isinstance(group_info, dict):
                group_info = {}

            target = {
                "stream_id": str(getattr(message, "stream_id", "") or ""),
                "group_id": str(
                    group_info.get("group_id")
                    or extra.get("target_group_id")
                    or extra.get("group_id")
                    or ""
                ),
                "user_id": str(
                    user_info.get("user_id")
                    or extra.get("target_user_id")
                    or ""
                ),
            }
            if not (target["group_id"] or target["user_id"]):
                return ""

            segments = envelope.get("message_segment") or envelope.get("message_chain")
            if not (
                isinstance(segments, (list, tuple, dict))
                and segments
                and cls._has_segment_payload_identity(segments)
            ):
                return ""
            payload: Any = segments

            identity = {
                "adapter_signature": adapter_signature,
                "platform": str(
                    message_info.get("platform")
                    or getattr(message, "platform", "")
                    or ""
                ),
                "chat_type": str(getattr(message, "chat_type", "") or ""),
                "target": target,
                "reply_to": str(getattr(message, "reply_to", "") or ""),
                "message_type": str(
                    getattr(
                        getattr(message, "message_type", ""),
                        "value",
                        getattr(message, "message_type", ""),
                    )
                    or ""
                ),
                "payload": payload,
            }
            serialized = json.dumps(
                cls._normalize_dedupe_value(identity),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        except Exception as e:
            logger.debug(f"构造未知投递重试指纹失败: {e}")
            return ""

    @classmethod
    def _normalize_dedupe_value(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, dict):
            return {
                str(key): cls._normalize_dedupe_value(item)
                for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            }
        if isinstance(value, (list, tuple)):
            return [cls._normalize_dedupe_value(item) for item in value]
        return str(value)

    @classmethod
    def _has_segment_payload_identity(cls, segments: Any) -> bool:
        if isinstance(segments, dict):
            return bool(segments.get("data"))
        if isinstance(segments, (list, tuple)):
            return any(cls._has_segment_payload_identity(segment) for segment in segments)
        return bool(segments)

    def _has_unknown_delivery_fingerprint(self, fingerprint: str) -> bool:
        now = time.monotonic()
        with self._dedupe_lock:
            self._prune_unknown_delivery_fingerprints(now)
            return fingerprint in self._unknown_delivery_fingerprint_set

    def _remember_unknown_delivery_fingerprint(self, fingerprint: str) -> None:
        now = time.monotonic()
        with self._dedupe_lock:
            self._prune_unknown_delivery_fingerprints(now)
            if fingerprint in self._unknown_delivery_fingerprint_set:
                return
            self._unknown_delivery_fingerprints.append((now, fingerprint))
            self._unknown_delivery_fingerprint_set.add(fingerprint)
            while len(self._unknown_delivery_fingerprints) > _DEDUP_MAX_ENTRIES:
                _, old = self._unknown_delivery_fingerprints.popleft()
                self._unknown_delivery_fingerprint_set.discard(old)

    def _prune_unknown_delivery_fingerprints(self, now: float) -> None:
        expire_before = now - _DEDUP_WINDOW_SECONDS
        while (
            self._unknown_delivery_fingerprints
            and self._unknown_delivery_fingerprints[0][0] < expire_before
        ):
            _, old = self._unknown_delivery_fingerprints.popleft()
            self._unknown_delivery_fingerprint_set.discard(old)

    @staticmethod
    def _should_use_virtual_send(message: "Message") -> bool:
        """判断当前消息是否应走虚拟发送分支。"""
        virtual_platforms = {"live", "game.sts2.operator", "game.minecraft.operator"}
        return str(getattr(message, "platform", "") or "").strip().lower() in virtual_platforms

    async def _send_virtual_message(self, message: "Message") -> bool:
        """处理不依赖外部 Adapter 的虚拟平台发送。

        虚拟发送同样先发布 ON_MESSAGE_SENT 供处理器拦截；历史写入完成后
        再发布 ON_MESSAGE_DELIVERED，表示虚拟投递已确认。
        """
        adapter_signature = "live_bridge:adapter:virtual_live"

        try:
            envelope = await self._converter.message_to_envelope(message)
            should_send = await self._emit_send_event(
                message,
                envelope,
                adapter_signature,
            )
            if not should_send:
                logger.info(f"虚拟消息被事件处理器拦截: {message.message_id}")
                return True

            history_persisted = await self._persist_sent_message_to_history(message)
            if history_persisted:
                await self._emit_delivery_event(message, envelope, adapter_signature)

            msg_text = (
                message.processed_plain_text
                or (message.content if isinstance(message.content, str) else "")
                or "(无文本内容)"
            )
            if len(msg_text) > 100:
                msg_text = msg_text[:100] + "..."

            logger.debug(f"虚拟消息发送成功: {message.message_id} → {adapter_signature}")
            logger.info(f'虚拟消息发送成功: [dim]{msg_text}[/dim]')
            return True

        except Exception as e:
            logger.error(
                f"虚拟消息发送失败: message_id={message.message_id}, error={e}",
                exc_info=True,
            )
            return False

    async def _apply_bot_sender_info(self, message: "Message", adapter: Any) -> None:
        """在发送前将消息发送者信息设置为 Bot 信息。"""
        try:
            bot_info: dict[str, Any] = {}
            bot_info = await adapter.get_bot_info()

            bot_id = str(bot_info.get("bot_id", "") or "")
            bot_name = str(bot_info.get("bot_name", "") or "")

            if bot_id:
                message.sender_id = bot_id
            if bot_name:
                message.sender_name = bot_name
                if not message.sender_cardname:
                    message.sender_cardname = bot_name
        except Exception as e:
            logger.warning(
                f"获取 Bot sender 信息失败，保留原 sender: message_id={message.message_id}, error={e}"
            )

    async def _persist_sent_message_to_history(self, message: "Message") -> bool:
        """发送成功后写入聊天流历史，并返回是否实际写入。"""
        if not message.stream_id:
            logger.warning(
                f"发送消息缺少 stream_id，跳过历史写入: message_id={message.message_id}"
            )
            return False

        from src.core.managers.stream_manager import get_stream_manager

        sm = get_stream_manager()

        group_id = str(
            message.extra.get("target_group_id")
            or message.extra.get("group_id")
            or ""
        )
        user_id = str(message.extra.get("target_user_id") or "")

        await sm.get_or_create_stream(
            stream_id=message.stream_id,
            platform=message.platform,
            user_id=user_id,
            group_id=group_id,
            chat_type=message.chat_type,
        )
        await sm.add_sent_message_to_history(message)
        return True

    def _infer_adapter_signature(self, message: "Message") -> str | None:
        """推断目标 Adapter 签名。

        根据 message.platform 查找匹配的 Adapter。

        Args:
            message: 消息对象

        Returns:
            str | None: Adapter 签名，如果未找到则返回 None
        """
        try:
            from src.core.components.registry import get_global_registry
            from src.core.components.types import ComponentType

            registry = get_global_registry()
            adapters = registry.get_by_type(ComponentType.ADAPTER)

            # 查找匹配平台的 Adapter
            for sig, adapter_cls in adapters.items():
                if hasattr(adapter_cls, "platform") and adapter_cls.platform == message.platform:
                    logger.debug(
                        f"推断 Adapter 签名: {sig} (platform={message.platform})"
                    )
                    return sig

            logger.warning(
                f"未找到匹配的 Adapter: platform={message.platform}"
            )
            return None

        except Exception as e:
            logger.error(f"推断 Adapter 签名失败: {e}")
            return None

    async def _emit_send_event(
        self,
        message: "Message",
        envelope: MessageEnvelope,
        adapter_signature: str,
    ) -> bool:
        """发布发送前事件，并返回处理器是否允许继续发送。"""
        try:
            from src.core.components.types import EventType
            from src.core.managers.event_manager import get_event_manager
            from src.kernel.event import EventDecision

            result = await get_event_manager().publish_event(
                EventType.ON_MESSAGE_SENT,
                {
                    "message": message,
                    "envelope": envelope,
                    "adapter_signature": adapter_signature,
                    "continue_send": True,
                },
            )
            final_params = result.get("params") or {}
            if final_params.get("continue_send", True) is False:
                return False
            return result.get("decision") != EventDecision.STOP
        except Exception as e:
            logger.warning(f"触发发送事件失败: {e}")
            return True

    async def _emit_delivery_event(
        self,
        message: "Message",
        envelope: MessageEnvelope,
        adapter_signature: str,
    ) -> None:
        """发布适配器成功且历史写入完成后的确认投递事件。"""
        try:
            from src.core.components.types import EventType
            from src.core.managers.event_manager import get_event_manager

            await get_event_manager().publish_event(
                EventType.ON_MESSAGE_DELIVERED,
                {
                    "message": message,
                    "envelope": envelope,
                    "adapter_signature": adapter_signature,
                },
            )
        except Exception as e:
            logger.warning(f"触发确认投递事件失败: {e}")


# 全局单例
_global_message_sender: "MessageSender | None" = None


def get_message_sender() -> MessageSender:
    """获取全局 MessageSender 单例。

    Returns:
        MessageSender: 全局 MessageSender 单例

    Examples:
        >>> sender = get_message_sender()
    """
    global _global_message_sender
    if _global_message_sender is None:
        _global_message_sender = MessageSender()
    return _global_message_sender


def set_message_sender(sender: MessageSender) -> None:
    """设置全局 MessageSender 单例。

    Args:
        sender: MessageSender 实例

    Examples:
        >>> set_message_sender(MessageSender())
    """
    global _global_message_sender
    _global_message_sender = sender


def reset_message_sender() -> None:
    """重置全局 MessageSender。

    主要用于测试场景，确保测试之间不会相互影响。

    Examples:
        >>> reset_message_sender()
    """
    global _global_message_sender
    _global_message_sender = None


__all__ = [
    "MessageSender",
    "get_message_sender",
    "set_message_sender",
    "reset_message_sender",
]
