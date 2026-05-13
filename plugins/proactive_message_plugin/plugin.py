"""Proactive Message Plugin - 主动续话插件主模块。

让 Bot 具有在用户长时间未回复时主动续话的能力。

核心逻辑：
1. 用户 last_message 后开始计时
2. 等待 N 分钟（可配置）后触发一次主动机会
3. 交给 life_chatter 在统一提示词下先记录内心独白，再决定发消息或继续等待
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import TYPE_CHECKING

from src.core.components.base import BasePlugin
from src.core.components.base.event_handler import BaseEventHandler
from src.core.components.types import EventType
from src.core.components.loader import register_plugin
from src.core.models.stream import ChatStream
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.event_api import register_handler

from .service import PendingFollowup, ProactiveMessageService, get_proactive_message_service
from .config import ProactiveMessageConfig
from .actions.schedule_followup_message import ScheduleFollowupMessageAction
from .tools.wait_longer import WaitLongerTool

if TYPE_CHECKING:
    from src.core.components.base import BaseConfig

logger = get_logger("proactive_message_plugin", display="主动续话")


class ProactiveMessageEventHandler(BaseEventHandler):
    """主动发消息插件的事件处理器。

    订阅以下事件：
    - ON_MESSAGE_RECEIVED: 收到用户消息时重置等待状态
    - ON_MESSAGE_SENT: Bot 发出消息时记录最近一次显式回复
    - ON_CHATTER_STEP_RESULT: Chatter 执行一步后检查是否进入 Wait 状态
    """

    plugin_name = "proactive_message_plugin"
    handler_name = "on_message"
    handler_description = "处理消息接收和 Chatter 步件事件"

    init_subscribe: list[EventType | str] = [
        EventType.ON_MESSAGE_RECEIVED,
        EventType.ON_MESSAGE_SENT,
        EventType.ON_CHATTER_STEP_RESULT,
    ]

    async def execute(
        self, event_name: str, params: dict
    ) -> tuple:
        """执行事件处理。

        Args:
            event_name: 事件名称
            params: 事件参数

        Returns:
            tuple: (EventDecision, params)
        """
        from src.kernel.event import EventDecision

        plugin = self.plugin
        if not isinstance(plugin, ProactiveMessagePlugin):
            return EventDecision.SUCCESS, params

        try:
            normalized_event_name = (
                event_name.value if isinstance(event_name, EventType) else str(event_name)
            )

            if normalized_event_name == EventType.ON_MESSAGE_RECEIVED.value:
                # 收到用户消息
                chat_stream = params.get("chat_stream")
                message = params.get("message")

                if message is not None and str(getattr(message, "sender_role", "") or "").lower() == "bot":
                    return EventDecision.SUCCESS, params

                if chat_stream is None and message is not None:
                    stream_id = getattr(message, "stream_id", "")
                    if stream_id:
                        try:
                            from src.app.plugin_system.api.stream_api import get_stream

                            chat_stream = await get_stream(stream_id)
                        except Exception as error:
                            logger.debug(f"通过 stream_id 获取 chat_stream 失败：{error}")

                if chat_stream:
                    await plugin._on_user_message(chat_stream)

            elif normalized_event_name == EventType.ON_MESSAGE_SENT.value:
                message = params.get("message")
                if message is not None:
                    await plugin._on_bot_message_sent(message)

            elif normalized_event_name == EventType.ON_CHATTER_STEP_RESULT.value:
                # Chatter 执行一步
                stream_id = params.get("stream_id")
                result = params.get("result")
                if stream_id and result:
                    await plugin._on_chatter_step(stream_id, params.get("context"), result)

        except Exception as e:
            logger.error(f"事件处理失败：{e}")

        return EventDecision.SUCCESS, params


@register_plugin
class ProactiveMessagePlugin(BasePlugin):
    """主动发消息插件。

    功能：
    1. 追踪用户最后消息时间
    2. 在等待达到阈值后触发主动机会
    3. 交给当前对话器统一判断是否开口
    """

    plugin_name = "proactive_message_plugin"
    plugin_version = "1.0.0"
    plugin_author = "Neo-MoFox Team"
    plugin_description = "让 Bot 具有在用户长时间未回复时主动发消息的能力"
    configs = [ProactiveMessageConfig]

    def __init__(self, config: "BaseConfig | None" = None) -> None:
        super().__init__(config)
        self._service: ProactiveMessageService | None = None

    @property
    def service(self) -> ProactiveMessageService:
        """获取主动消息服务"""
        if self._service is None:
            self._service = get_proactive_message_service()
        return self._service

    def _should_ignore(self, chat_stream: ChatStream) -> bool:
        """检查是否应该忽略该聊天流"""
        from .config import ProactiveMessageConfig
        chat_type = str(getattr(chat_stream, "chat_type", "")).lower()
        config = self.config
        if not isinstance(config, ProactiveMessageConfig):
            return True
        if not bool(getattr(config.settings, "enabled", True)):
            return True
        ignored_types = config.settings.ignored_chat_types
        return chat_type in ignored_types

    async def _is_platform_connected(self, platform: str) -> bool:
        """检查目标平台是否存在可用连接。"""
        normalized_platform = str(platform or "").strip().lower()
        if not normalized_platform:
            return False

        try:
            from src.core.managers.adapter_manager import get_adapter_manager

            adapter_manager = get_adapter_manager()
            matched = False
            for signature, adapter in adapter_manager.get_all_adapters().items():
                adapter_platform = str(getattr(adapter, "platform", "") or "").strip().lower()
                if adapter_platform != normalized_platform:
                    continue
                matched = True

                checker = getattr(adapter, "is_connected", None)
                if not callable(checker):
                    # 没有连接探针时，只要适配器处于活跃列表就视为可用
                    return True

                try:
                    connected = checker()
                    if inspect.isawaitable(connected):
                        connected = await connected
                    if bool(connected):
                        return True
                except Exception as error:
                    logger.debug(
                        f"检查适配器连接状态失败: {signature}, error={error}"
                    )

            if not matched:
                logger.warning(f"未找到平台适配器: platform={normalized_platform}")
        except Exception as error:
            logger.warning(f"检查平台连接状态失败: platform={normalized_platform}, error={error}")

        return False

    async def on_plugin_loaded(self) -> None:
        """插件加载时的初始化"""
        logger.info("主动发消息插件已加载")
        # 初始化配置和服务
        _ = self.config
        _ = self.service
        # 注册事件处理器
        self._register_handlers()
        logger.info("主动发消息插件初始化完成")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时的清理"""
        logger.info("主动发消息插件已卸载")
        if self._service:
            self._service.clear_all()
        self._service = None
        self._config = None

    def get_components(self) -> list[type]:
        """获取插件内所有组件类"""
        return [WaitLongerTool, ScheduleFollowupMessageAction, ProactiveMessageEventHandler]

    def _register_handlers(self) -> None:
        """注册事件处理器"""
        # 创建事件处理器实例
        handler = ProactiveMessageEventHandler(self)
        # 注册到事件系统
        import asyncio
        asyncio.create_task(register_handler("proactive_message_plugin:event_handler:on_message", handler))

    async def _on_user_message(self, chat_stream: ChatStream) -> None:
        """当收到用户消息时调用"""
        if not self._should_ignore(chat_stream):
            # 仅重置状态；首轮等待应从本轮消息处理完成、Chatter 重新进入 Wait 后开始。
            # 否则在 first_check_minutes 很小时，内心独白会抢在对这条用户消息的正常回复前触发。
            self.service.on_user_message(chat_stream, cancel_task=False)

    async def _on_bot_message_sent(self, message) -> None:
        """记录最近一次显式 Bot 回复。"""
        try:
            stream_id = str(getattr(message, "stream_id", "") or "").strip()
            if not stream_id:
                return

            if str(getattr(message, "sender_role", "") or "bot").lower() != "bot":
                return

            chat_type = str(getattr(message, "chat_type", "") or "").lower()
            ignored_types = getattr(self.config.settings, "ignored_chat_types", [])
            if chat_type in ignored_types:
                return

            text = getattr(message, "processed_plain_text", "") or getattr(message, "content", "")
            text = str(text or "").strip()
            if not text:
                return

            self.service.record_bot_message(stream_id, text)
            state = self.service.get_state(stream_id)
            if state is not None and state.proactive_opportunity_active:
                self.service.mark_proactive_opportunity_sent(stream_id)
                logger.info(f"[{stream_id[:8]}] 主动机会已由对话器发送显式消息")

            if state is not None and state.followup_trigger_active:
                if self.service.mark_followup_trigger_sent(stream_id):
                    logger.info(
                        f"[{stream_id[:8]}] 延迟续话已发送，本轮续话链计数 -> "
                        f"{self.service.get_state(stream_id).followup_chain_count}"
                    )
            logger.debug(f"[{stream_id[:8]}] 已记录最近一条 Bot 消息：{text[:60]}")
        except Exception as exc:
            logger.debug(f"记录 Bot 发送消息失败：{exc}")

    async def _on_chatter_step(self, stream_id: str, context, result) -> None:
        """当 Chatter 执行一步时调用"""
        from src.core.components.base.chatter import Wait, Stop

        if isinstance(result, (Wait, Stop)):
            state = self.service.get_state(stream_id)
            if state is not None and state.proactive_opportunity_active:
                sent_message = state.proactive_opportunity_sent_message
                self.service.clear_proactive_opportunity(stream_id)
                if sent_message:
                    logger.debug(f"[{stream_id[:8]}] 主动机会轮次已发送，进入发送后等待")
                    self.service.prepare_post_send_state(
                        stream_id,
                        reset_followup_chain=True,
                    )
                    await self._schedule_post_send_followup(stream_id)
                else:
                    wait_minutes = float(
                        getattr(
                            self.config.settings,
                            "declined_opportunity_wait_minutes",
                            30.0,
                        )
                        or 30.0
                    )
                    logger.debug(
                        f"[{stream_id[:8]}] 主动机会被对话器放弃，"
                        f"{wait_minutes:.1f} 分钟后再次检查"
                    )
                    self.service.checkpoint_wait(stream_id)
                    await self._schedule_continue_waiting(stream_id, wait_minutes)
                return

            state = self.service.get_state(stream_id)
            if state is not None and state.followup_trigger_active:
                self.service.clear_followup_trigger(stream_id)
                logger.debug(f"[{stream_id[:8]}] 延迟续话轮次结束")
                state_after = self.service.get_state(stream_id)
                if (
                    state_after is not None
                    and state_after.pending_followup is not None
                    and state_after.active_check_kind == "followup"
                ):
                    logger.debug(f"[{stream_id[:8]}] 续话轮次中已登记下一次延迟续话，跳过普通等待计时")
                    return

                self.service.enter_followup_cooldown(
                    stream_id,
                    float(getattr(self.config.settings, "followup_cooldown_minutes", 10.0) or 10.0),
                )
                await self._schedule_post_send_followup(stream_id)
                return

        if isinstance(result, Wait):
            # Bot 进入等待状态
            try:
                from src.app.plugin_system.api.stream_api import get_stream
                from src.core.managers import get_stream_manager

                chat_stream = await get_stream(stream_id)
                if chat_stream is None:
                    logger.debug(f"[{stream_id[:8]}] get_stream 未命中，尝试从 StreamManager 兜底获取")
                    chat_stream = get_stream_manager()._streams.get(stream_id)

                if chat_stream is None:
                    logger.warning(f"[{stream_id[:8]}] 检测到 Wait，但未找到 chat_stream，无法启动主动消息计时")
                    return

                if self._should_ignore(chat_stream):
                    logger.debug(f"[{stream_id[:8]}] 检测到 Wait，但该聊天类型被忽略")
                    return

                logger.debug(f"[{stream_id[:8]}] 检测到 Wait，准备启动主动消息等待计时")
                await self._start_waiting(chat_stream)
            except Exception as e:
                logger.debug(f"处理 Chatter Wait 状态失败：{e}")

    async def _start_waiting(self, chat_stream: ChatStream) -> None:
        """开始等待计时

        Args:
            chat_stream: 聊天流
        """
        stream_id = getattr(chat_stream, "stream_id", "")
        if not stream_id:
            return

        # 检查是否已在等待中
        state = self.service.get_state(stream_id)
        if state is not None and state.is_waiting:
            logger.debug(f"已在等待中：{stream_id[:8]}...")
            return

        # 开始等待
        logger.info(f"开始等待计时：{stream_id[:8]}...")

        async def _timeout_callback() -> None:
            await self._on_check_timeout(stream_id)

        await self.service.start_waiting(
            stream_id=stream_id,
            wait_minutes=self.config.settings.first_check_minutes,
            callback=_timeout_callback,
        )

    async def _restart_waiting(self, chat_stream: ChatStream) -> None:
        """用户新消息后，重置并从首轮等待时间重新调度。"""
        stream_id = getattr(chat_stream, "stream_id", "")
        if not stream_id:
            return

        async def _timeout_callback() -> None:
            await self._on_check_timeout(stream_id)

        await self.service.start_waiting(
            stream_id=stream_id,
            wait_minutes=self.config.settings.first_check_minutes,
            callback=_timeout_callback,
        )

    async def _on_check_timeout(self, stream_id: str) -> None:
        """当检查超时时调用 - 触发主动机会

        Args:
            stream_id: 聊天流 ID
        """
        state = self.service.get_state(stream_id)
        if state is None or not state.is_waiting or state.active_check_kind != "silence_wait":
            logger.debug(f"[{stream_id[:8]}] 跳过过期的沉默检查任务")
            return

        logger.info(f"检查超时，触发主动机会：{stream_id[:8]}...")

        # 获取状态
        # 使用累计等待时间，包含多次 wait_longer
        elapsed = self.service.get_total_wait_minutes(stream_id)

        # 获取聊天流信息
        try:
            from src.app.plugin_system.api.stream_api import get_stream

            chat_stream = await get_stream(stream_id)

            if chat_stream is None:
                logger.warning(f"未找到聊天流：{stream_id[:8]}...")
                return

            # 检查是否应该忽略
            if self._should_ignore(chat_stream):
                return

            # 平台断联时直接短路，避免无意义调用 LLM 空烧 token
            if not await self._is_platform_connected(chat_stream.platform):
                fallback_wait = max(
                    float(getattr(self.config.settings, "post_send_followup_minutes", 10.0) or 10.0),
                    float(getattr(self.config.settings, "min_wait_interval_minutes", 5.0) or 5.0),
                )
                logger.warning(
                    f"[{stream_id[:8]}] 平台 {chat_stream.platform} 当前断联，"
                    f"跳过主动机会并延后 {fallback_wait:.1f} 分钟重试"
                )
                await self._schedule_continue_waiting(stream_id, fallback_wait)
                return

            # 获取用户名称
            user_name = getattr(chat_stream, "stream_name", "用户")

            await self._wake_stream_for_proactive_opportunity(
                chat_stream,
                elapsed_minutes=elapsed,
                user_name=user_name,
            )
            return

        except Exception as e:
            logger.error(f"主动机会处理失败：{e}", exc_info=True)
            # 失败则继续等待
            await self._schedule_continue_waiting(stream_id, 30)

    async def schedule_followup_for_stream(
        self,
        chat_stream: ChatStream,
        *,
        delay_seconds: float,
        thought: str,
        topic: str,
        followup_type: str,
        source: str,
    ) -> tuple[bool, str]:
        """为某个聊天流登记一次延迟续话。"""
        if self._should_ignore(chat_stream):
            return False, "当前聊天类型已被忽略"

        if not getattr(self.config.settings, "followup_enabled", True):
            return False, "延迟续话已在配置中禁用"

        stream_id = getattr(chat_stream, "stream_id", "")
        if not stream_id:
            return False, "缺少 stream_id"

        state = self.service.get_or_create_state(stream_id)
        if self.service.is_followup_cooldown_active(stream_id):
            return False, "当前仍处于延迟续话冷却期"

        max_chain = max(int(getattr(self.config.settings, "followup_max_chain_count", 2) or 0), 0)
        if max_chain > 0 and state.followup_chain_count >= max_chain:
            return False, "延迟续话链已达上限"

        min_delay = float(getattr(self.config.settings, "followup_min_delay_seconds", 20.0) or 20.0)
        max_delay = float(getattr(self.config.settings, "followup_max_delay_seconds", 90.0) or 90.0)
        delay_seconds = max(float(delay_seconds or 0), min_delay)
        delay_seconds = min(delay_seconds, max_delay)

        followup = PendingFollowup(
            topic=str(topic or "未命名话题").strip() or "未命名话题",
            thought=str(thought or "").strip(),
            followup_type=str(followup_type or "share_new_thought").strip() or "share_new_thought",
            delay_seconds=delay_seconds,
            scheduled_at=datetime.now(),
            check_at=datetime.now(),
            source=source,
        )

        async def _timeout_callback() -> None:
            await self._on_followup_timeout(stream_id)

        await self.service.start_followup_wait(
            stream_id=stream_id,
            delay_seconds=delay_seconds,
            followup=followup,
            callback=_timeout_callback,
        )
        logger.info(
            f"[{stream_id[:8]}] 已登记延迟续话：{followup.followup_type}, "
            f"{delay_seconds:.1f} 秒后检查, topic={followup.topic}"
        )
        return True, f"已登记一条延迟续话，会在 {delay_seconds:.0f} 秒后重新判断。"

    async def _on_followup_timeout(self, stream_id: str) -> None:
        """延迟续话到点后执行判断。"""
        state = self.service.get_state(stream_id)
        if (
            state is None
            or not state.is_waiting
            or state.active_check_kind != "followup"
            or state.pending_followup is None
        ):
            logger.debug(f"[{stream_id[:8]}] 跳过过期的延迟续话任务")
            return

        followup = state.pending_followup
        self.service.clear_pending_followup(stream_id)
        self.service.mark_followup_trigger_active(stream_id)
        logger.info(f"[{stream_id[:8]}] 延迟续话到时，唤醒当前运行模式判断是否继续说")

        try:
            from src.app.plugin_system.api.stream_api import get_stream
            from src.core.managers import get_stream_manager

            chat_stream = await get_stream(stream_id)
            if chat_stream is None:
                chat_stream = get_stream_manager()._streams.get(stream_id)
            if chat_stream is None:
                logger.warning(f"[{stream_id[:8]}] 延迟续话未找到 chat_stream")
                self.service.clear_followup_trigger(stream_id)
                return
            if self._should_ignore(chat_stream):
                self.service.clear_followup_trigger(stream_id)
                return
            if not await self._is_platform_connected(chat_stream.platform):
                logger.warning(
                    f"[{stream_id[:8]}] 平台 {chat_stream.platform} 当前断联，跳过本次延迟续话唤醒"
                )
                self.service.clear_followup_trigger(stream_id)
                self.service.enter_followup_cooldown(
                    stream_id,
                    float(getattr(self.config.settings, "followup_cooldown_minutes", 10.0) or 10.0),
                )
                await self._schedule_post_send_followup(stream_id)
                return

            max_chain = int(getattr(self.config.settings, "followup_max_chain_count", 2) or 2)
            if max_chain > 0 and state.followup_chain_count >= max_chain:
                logger.info(f"[{stream_id[:8]}] 延迟续话链已达上限，结束本轮续话")
                self.service.clear_followup_trigger(stream_id)
                self.service.enter_followup_cooldown(
                    stream_id,
                    float(getattr(self.config.settings, "followup_cooldown_minutes", 10.0) or 10.0),
                )
                await self._schedule_post_send_followup(stream_id)
                return

            await self._wake_stream_for_followup(chat_stream, followup)
        except Exception as exc:
            logger.error(f"[{stream_id[:8]}] 延迟续话处理失败：{exc}", exc_info=True)
            self.service.clear_followup_trigger(stream_id)
            self.service.enter_followup_cooldown(
                stream_id,
                float(getattr(self.config.settings, "followup_cooldown_minutes", 10.0) or 10.0),
            )
            await self._schedule_post_send_followup(stream_id)

    async def _wake_stream_for_proactive_opportunity(
        self,
        chat_stream: ChatStream,
        *,
        elapsed_minutes: float,
        user_name: str,
    ) -> None:
        """把沉默检查转换为一条由当前对话器判断的主动机会。"""
        from src.core.models.message import Message
        from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager
        import time
        import uuid

        stream_id = chat_stream.stream_id
        target_user_id, target_user_name = self._resolve_followup_target(chat_stream)
        target_user_name = target_user_name or str(user_name or "对方")
        state = self.service.get_state(stream_id)
        last_bot_message = state.last_bot_message_excerpt if state else ""
        elapsed_text = f"{float(elapsed_minutes or 0):.0f}"

        prompt = (
            "[主动续话机会] 这不是用户的新消息，而是沉默等待到点后产生的一次主动机会。"
            "请作为当前对话器判断要不要自然开口。"
            "你必须先调用 action-record_inner_monologue，记录你此刻新的心理推进；"
            "然后再二选一：如果你觉得现在主动说一句是自然、有价值的，就像平时一样使用当前动作回复；"
            "如果只是为了说而说，就使用 pass_and_wait 继续等待。"
            f"\n- 当前执行者：{chat_stream.bot_nickname or '你'}"
            f"\n- 对方：{target_user_name}"
            f"\n- 距离上次外界消息已过去约 {elapsed_text} 分钟"
            f"\n- 你最近一条显式消息：{last_bot_message or '（暂无记录）'}"
            "\n- 重要：这是机会，不是命令；不要机械续话，不要假装对方刚刚发了新消息。"
        )

        await self._record_proactive_opportunity_event(
            chat_stream,
            prompt,
        )

        self.service.mark_proactive_opportunity_active(stream_id)

        trigger_message = Message(
            message_id=f"proactive_opportunity_{uuid.uuid4().hex[:12]}",
            platform=chat_stream.platform or "unknown",
            stream_id=stream_id,
            sender_id=target_user_id or "system",
            sender_name="系统（主动机会）",
            sender_role="other",
            content=prompt,
            processed_plain_text=prompt,
            time=time.time(),
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            is_proactive_opportunity_trigger=True,
        )
        chat_stream.context.add_unread_message(trigger_message)

        loop_mgr = get_stream_loop_manager()
        removed = loop_mgr._wait_states.pop(stream_id, None)
        if removed:
            logger.debug(f"[{stream_id[:8]}] 已清除等待锁，准备让对话器处理主动机会")
        logger.info(
            f"[{stream_id[:8]}] 已注入主动续话机会，由当前对话器判断是否开口"
        )

    @staticmethod
    async def _record_proactive_opportunity_event(chat_stream: ChatStream, content: str) -> None:
        """把主动机会写入 life_engine 事件流；life_engine 不可用时静默降级。"""
        try:
            from plugins.life_engine.service import LifeEngineService

            service = LifeEngineService.get_instance()
            if service is None:
                return
            enqueue = getattr(service, "enqueue_proactive_opportunity", None)
            if not callable(enqueue):
                return
            await enqueue(
                content,
                stream_id=chat_stream.stream_id,
                platform=chat_stream.platform,
                chat_type=chat_stream.chat_type,
                sender_name="主动机会调度器",
            )
        except Exception as error:
            logger.debug(f"写入主动机会事件失败：{error}")

    async def _wake_stream_for_followup(self, chat_stream: ChatStream, followup: PendingFollowup) -> None:
        """向目标流注入一条续话机会触发消息，并唤醒当前运行模式。"""
        from src.core.models.message import Message
        from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager
        import time
        import uuid

        stream_id = chat_stream.stream_id
        context = chat_stream.context
        target_user_id, target_user_name = self._resolve_followup_target(chat_stream)
        last_bot_message = self.service.get_state(stream_id).last_bot_message_excerpt if self.service.get_state(stream_id) else ""
        elapsed_seconds = followup.delay_seconds
        state = self.service.get_state(stream_id)
        if state and state.last_bot_message_time is not None:
            elapsed_seconds = max(
                0.0,
                (datetime.now() - state.last_bot_message_time).total_seconds(),
            )

        prompt = (
            "[延迟续话机会] 这不是用户的新消息，而是一次由系统交给你的主动续话机会。"
            "你必须先调用 action-record_inner_monologue，记录你此刻新的心理推进；"
            "然后再二选一：如果你觉得刚才的话头还值得延续，就像平时一样使用当前对话器的动作自己回复；"
            "如果觉得现在不该继续说，可以 pass_and_wait。"
            f"\n- 当前执行者：{chat_stream.bot_nickname or '你'}"
            f"\n- 距离你上一条显式消息已过去约 {elapsed_seconds:.0f} 秒"
            f"\n- 你刚刚对对方说的是：{last_bot_message or '（上一条消息为空）'}"
            f"\n- 你当时留下的未尽之意：{followup.thought or '（未填写）'}"
            f"\n- 续话主题：{followup.topic}"
            f"\n- 续话类型：{followup.followup_type}"
            "\n- 重要：不要机械续话，不要为了说而说；如果不自然，就先停住。"
        )

        trigger_message = Message(
            message_id=f"proactive_followup_{uuid.uuid4().hex[:12]}",
            platform=chat_stream.platform or "unknown",
            stream_id=stream_id,
            sender_id=target_user_id or "system",
            sender_name="系统（续话触发）",
            sender_role="other",
            content=prompt,
            processed_plain_text=prompt,
            time=time.time(),
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            is_proactive_followup_trigger=True,
            proactive_followup_topic=followup.topic,
            proactive_followup_type=followup.followup_type,
        )
        context.add_unread_message(trigger_message)
        loop_mgr = get_stream_loop_manager()
        removed = loop_mgr._wait_states.pop(stream_id, None)
        if removed:
                logger.debug(f"[{stream_id[:8]}] 已清除等待锁，准备让对话器处理续话机会")
        logger.info(
            f"[{stream_id[:8]}] 已注入续话机会触发消息：topic={followup.topic}, type={followup.followup_type}"
        )

    @staticmethod
    def _resolve_followup_target(chat_stream: ChatStream) -> tuple[str, str]:
        """从当前流上下文推断续话对象。"""
        bot_id = str(getattr(chat_stream, "bot_id", "") or "")
        history = list(getattr(chat_stream.context, "history_messages", []) or [])
        for msg in reversed(history):
            sender_id = str(getattr(msg, "sender_id", "") or "")
            sender_role = str(getattr(msg, "sender_role", "") or "").lower()
            if sender_role == "bot":
                continue
            if bot_id and sender_id == bot_id:
                continue
            if sender_id:
                return sender_id, str(getattr(msg, "sender_name", "") or "")
        return "", ""

    @staticmethod
    def _normalize_content_segments(content: str | list[str]) -> list[str]:
        """将内容标准化为非空字符串段列表。

        支持：
        - list[str]: 直接过滤空字符串，并将段内换行拆成多条消息
        - str: 按 <br /> 分割取第一部分，尝试解析 JSON 数组

        Returns:
            list[str]: 非空字符串段列表，解析失败或空内容返回空列表
        """
        import json
        import re

        def _split_text_segments(text: str) -> list[str]:
            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            normalized = normalized.replace("\\n", "\n")
            return [
                segment.strip()
                for segment in re.split(r"\n+", normalized)
                if segment.strip()
            ]

        if isinstance(content, list):
            segments: list[str] = []
            for item in content:
                if isinstance(item, str):
                    segments.extend(_split_text_segments(item))
            return segments

        if not isinstance(content, str):
            return []

        stripped = content.strip()
        if not stripped:
            return []

        # 按 <br /> 分割，取第一部分（兼容多行分割语法）
        first_block = re.split(r"<br\s*/?>", stripped, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if not first_block:
            return []

        # 尝试解析为 JSON 数组
        try:
            parsed = json.loads(first_block)
            if isinstance(parsed, list):
                segments: list[str] = []
                for item in parsed:
                    if isinstance(item, str):
                        segments.extend(_split_text_segments(item))
                return segments
        except json.JSONDecodeError:
            pass

        # 非数组格式，直接作为单一段落
        return _split_text_segments(first_block)

    async def _send_message(self, chat_stream: ChatStream, content: str | list[str]) -> bool:
        """发送消息

        Args:
            chat_stream: 聊天流
            content: 消息内容

        Returns:
            bool: 是否发送成功
        """
        try:
            from src.core.models.message import Message, MessageType
            from src.core.transport.message_send import get_message_sender
            from uuid import uuid4

            segments = self._normalize_content_segments(content)
            if not segments:
                return False

            sender = get_message_sender()

            sent_count = 0
            for segment in segments:
                # 构建消息对象时只允许纯字符串，避免列表透传到数据库层
                message = Message(
                    message_id=f"proactive_{uuid4().hex}",
                    content=segment,
                    processed_plain_text=segment,
                    message_type=MessageType.TEXT,
                    sender_id=chat_stream.bot_id or "bot",
                    sender_name=chat_stream.bot_nickname or "Bot",
                    sender_role="bot",
                    platform=chat_stream.platform,
                    chat_type=chat_stream.chat_type,
                    stream_id=chat_stream.stream_id,
                )

                success = await sender.send_message(message)
                if not success:
                    logger.error(f"主动消息发送失败：{chat_stream.stream_id[:8]}...")
                    return False
                sent_count += 1

            if sent_count > 1:
                logger.info(
                    f"主动消息分段发送成功：{chat_stream.stream_id[:8]}..., 共 {sent_count} 条"
                )
            else:
                logger.info(f"主动消息发送成功：{chat_stream.stream_id[:8]}...")
            return True

        except Exception as e:
            logger.error(f"发送主动消息失败：{e}", exc_info=True)
            return False

    async def _schedule_continue_waiting(self, stream_id: str, wait_minutes: float) -> None:
        """调度继续等待

        Args:
            stream_id: 聊天流 ID
            wait_minutes: 等待分钟数
        """
        state = self.service.get_state(stream_id)
        if state is None:
            return

        # 应用最大等待时间限制（累计）
        max_wait = self.config.settings.max_wait_minutes
        total_wait = self.service.get_total_wait_minutes(stream_id)

        if total_wait >= max_wait:
            logger.info(f"已达最大等待时间，强制触发：{stream_id[:8]}...")
            # 强制触发
            await self._on_check_timeout(stream_id)
            return

        # 应用最小等待间隔
        min_wait = self.config.settings.min_wait_interval_minutes
        wait_minutes = max(wait_minutes, min_wait)

        async def _timeout_callback() -> None:
            await self._on_check_timeout(stream_id)

        # 调度下次检查
        await self.service.start_waiting(
            stream_id=stream_id,
            wait_minutes=wait_minutes,
            callback=_timeout_callback,
        )

    async def _schedule_post_send_followup(self, stream_id: str) -> None:
        """主动发送后，无人回复的二次检查调度。"""
        if not stream_id:
            return

        async def _timeout_callback() -> None:
            await self._on_check_timeout(stream_id)

        wait_minutes = getattr(self.config.settings, "post_send_followup_minutes", 10.0)
        # 发送后重置累计计时，再按 post_send_followup_minutes 重新等待
        state = self.service.get_or_create_state(stream_id)
        state.accumulated_wait_minutes = 0.0
        state.last_user_message_time = datetime.now()

        await self.service.start_waiting(
            stream_id=stream_id,
            wait_minutes=wait_minutes,
            callback=_timeout_callback,
        )

# 全局插件实例引用
_plugin_instance: ProactiveMessagePlugin | None = None


def get_proactive_message_plugin() -> ProactiveMessagePlugin | None:
    """获取主动发消息插件实例"""
    global _plugin_instance
    return _plugin_instance
