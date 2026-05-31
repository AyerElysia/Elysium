"""消息分发器。

订阅 ``ON_MESSAGE_RECEIVED`` 事件，将接收到的消息注入聊天流，
并尝试启动对应流的 Tick 驱动器。

流程：
1. ``ON_MESSAGE_RECEIVED`` 事件携带 ``Message`` 到达
2. 通过 ``StreamManager`` 获取或创建对应的 ``ChatStream``
3. 将消息添加到流的未读列表
4. 在 ``StreamLoopManager`` 中为该流启动驱动器（如果尚未运行）

参考 old/chat/message_manager/message_manager.py 中 ``add_message`` 的实现。
"""

from __future__ import annotations

from typing import cast

from src.core.components.types import EventType
from src.kernel.concurrency import get_task_manager
from src.kernel.event import EventDecision, get_event_bus
from src.kernel.logger import get_logger, COLOR
from src.core.models.message import Message

logger = get_logger("distributor", display="消息分发", color=COLOR.MAGENTA)


async def _on_message_received(_: str, params: dict) -> tuple[EventDecision, dict]:
    """处理 ON_MESSAGE_RECEIVED 事件的回调。

    从事件参数中提取 Message，获取或创建 ChatStream，
    将消息添加到流的未读列表，并尝试启动该流的 Tick 驱动器。

    Args:
        _: 事件名称（未使用）
        params: 事件参数，包含 ``message``、``envelope``、``adapter_signature``

    Returns:
        tuple[EventDecision, dict]: (事件决策, 事件参数)
    """
    import asyncio
    from src.core.managers.stream_manager import get_stream_manager
    from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager

    message: Message = cast(Message, params.get("message"))
    if message is None:
        logger.warning("ON_MESSAGE_RECEIVED 事件缺少 message 参数")
        return EventDecision.PASS, params

    async def _do_distribution() -> None:
        # ── 命令优先检查：若消息是已注册命令，直接分发执行，不进入 Chatter ──
        text: str = message.content if isinstance(message.content, str) else ""
        if text:
            from src.core.managers.command_manager import get_command_manager

            cmd_mgr = get_command_manager()
            _path, matched_cls, _args = cmd_mgr.match_command(text)
            if matched_cls is not None:
                try:
                    success, result = await cmd_mgr.execute_command(message=message)
                    logger.debug(
                        f"命令已分发: text={text[:60]!r}, "
                        f"success={success}, result={result[:60]!r}"
                    )
                    return
                except Exception as e:
                    logger.error(f"命令分发异常: text={text[:60]!r}, error={e}", exc_info=True)
                    # 分发出错时放行，避免消息丢失

        try:
            # 1. 获取或创建 ChatStream
            # message.stream_id 已经是标准哈希格式（由 extract_stream_id 生成）
            sm = get_stream_manager()
            group_id = message.extra.get("group_id") if hasattr(message, "extra") else ""
            group_name = message.extra.get("group_name", "") if hasattr(message, "extra") else ""
            user_id = message.sender_id if message.chat_type != "group" else ""

            # 群聊用群名，私聊用"xxx的私聊"（优先 cardname，fallback sender_name/sender_id）
            if message.chat_type == "group":
                stream_name = group_name or ""
            else:
                display_name = message.sender_cardname or message.sender_name
                stream_name = f"{display_name}的私聊" if display_name else ""

            chat_stream = await sm.get_or_create_stream(
                platform=message.platform,
                stream_id=message.stream_id,
                chat_type=message.chat_type,
                user_id=user_id,
                group_id=group_id or "",
                group_name=stream_name,
            )

            stream_id = chat_stream.stream_id
            context = chat_stream.context
            skip_chatter_distribution = bool(
                params.get("skip_chatter_distribution")
                or (
                    hasattr(message, "extra")
                    and getattr(message, "extra", {}).get("skip_chatter_distribution")
                )
            )

            if skip_chatter_distribution:
                await sm.add_message(message, add_to_unread=False)
                chat_stream.update_active_time()
                logger.debug(
                    "消息已持久化但跳过 Chatter 分发: "
                    f"stream_id={stream_id[:8]}, platform={message.platform}"
                )
                return

            # 2. 先写入内存未读队列并启动驱动器，再后台持久化。
            # 事件总线单个处理器默认只有 5 秒超时；若把数据库写入放在前面，
            # DB/冷启动稍慢就会导致消息只被旁路收集器看到，life_chatter 不会被唤醒。
            context.add_unread_message(message)
            chat_stream.update_active_time()

            async def _persist_message() -> None:
                try:
                    await sm.add_message(message, add_to_unread=False)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"后台持久化入站消息失败: {exc}", exc_info=True)

            get_task_manager().create_task(
                _persist_message(),
                name=f"persist_inbound_message_{message.stream_id[:8]}",
                daemon=True,
            )

            # 3. 更新消息缓冲时间戳。
            # 注意：不要在“每次收到新消息”时重置 message_buffer_skip_count。
            # 否则在高压群聊下，skip_count 会被反复清零，导致缓冲逻辑永远达不到
            # max_skip 的强制放行阈值，从而 Tick 可能被无限跳过。
            import time

            context.last_message_time = time.time()

            # 4. 尝试启动该流的 Tick 驱动器（如果已在运行则跳过）
            slm = get_stream_loop_manager()
            if slm.is_running:
                # 检查是否已有驱动器在运行
                if not (context.stream_loop_task and not context.stream_loop_task.done()):
                    await slm.start_stream_loop(stream_id)
            else:
                logger.warning("StreamLoopManager 未启动，跳过驱动器启动")

        except Exception as e:
            logger.error(f"消息分发失败: {e}", exc_info=True)

    # 提交分发任务给全局任务管理器进行管理
    task_info = get_task_manager().create_task(
        _do_distribution(),
        name=f"distribute_inbound_message_{message.stream_id[:8]}",
        daemon=True,
    )

    try:
        # 在快速路径下（如测试或系统资源充足），我们尽可能同步等待完成（最多等待 1.0 秒）。
        # 这确保了单元测试等依赖即时状态断言的场景完全不受影响。
        await asyncio.wait_for(asyncio.shield(task_info.task), timeout=1.0)
    except asyncio.TimeoutError:
        # 如果 1.0 秒内未完成（例如由于 SQLite 锁导致的 DB 等待），我们允许其在后台继续运行，
        # 并立即向事件总线返回 SUCCESS，以防事件总线硬超时（5.0秒）抛弃事件。
        logger.warning(
            f"消息分发由于底层 I/O 或数据库锁定执行较慢（已超过 1.0 秒），切换至后台继续处理: "
            f"stream_id={message.stream_id[:8]}, platform={message.platform}"
        )

    return EventDecision.SUCCESS, params


async def _on_all_plugins_loaded(_: str, params: dict) -> tuple[EventDecision, dict]:
    """所有插件加载完毕后，启动 StreamLoopManager。

    Args:
        _: 事件名称（未使用）
        params: 事件参数

    Returns:
        tuple[EventDecision, dict]: (事件决策, 事件参数)
    """
    from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager

    slm = get_stream_loop_manager()
    await slm.start()
    logger.info("StreamLoopManager 已随插件加载完成而启动")

    return EventDecision.SUCCESS, params


def initialize_distribution() -> None:
    """初始化消息分发模块。

    将 ``_on_message_received`` 订阅到 ``ON_MESSAGE_RECEIVED`` 事件，
    将 ``_on_all_plugins_loaded`` 订阅到 ``ON_ALL_PLUGIN_LOADED`` 事件
    以便在所有插件就绪后启动 ``StreamLoopManager``。

    应在应用启动阶段调用（与 ``initialize_adapter_manager`` 等并列）。

    Examples:
        >>> initialize_distribution()
    """
    from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager

    bus = get_event_bus()

    # 订阅消息接收事件
    bus.subscribe(
        EventType.ON_MESSAGE_RECEIVED,
        _on_message_received,
        priority=0,  # 默认优先级，在插件事件处理器之后执行
    )

    # 订阅插件加载完成事件，自动启动 StreamLoopManager
    bus.subscribe(
        EventType.ON_ALL_PLUGIN_LOADED,
        _on_all_plugins_loaded,
        priority=-10,  # 较低优先级，确保其他初始化先完成
    )

    # 确保 StreamLoopManager 实例已创建
    get_stream_loop_manager()

    logger.info("消息分发模块初始化完成")
