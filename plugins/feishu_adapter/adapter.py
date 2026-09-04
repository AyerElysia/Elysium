"""Feishu HTTP callback adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import httpx
from PIL import Image as PILImage

from src.core.components.base.adapter import BaseAdapter
from src.core.transport.received_files import (
    MAX_RECEIVED_FILE_BYTES,
    persist_received_file,
)
from src.core.transport.wire import CoreSink, MessageEnvelope
from src.core.utils.audio_transcode import (
    probe_audio_duration_ms,
    transcode_audio_to_opus,
)
from src.kernel.logger import get_logger

from .config import FeishuAdapterConfig

logger = get_logger("FeishuAdapter", color="#00D6B9")

_LARK_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:access_key|ticket|app_secret|tenant_access_token)=)[^&\s]+"
)
_LARK_ROUTINE_RECONNECT_LOGS = (
    "connected to wss://",
    "disconnected to wss://",
    "trying to reconnect for the",
    "receive message loop exit, err:",
)
_LARK_REPEATED_ERROR_LOGS = (
    "connect failed, err:",
    "ping failed, err:",
)
_LARK_REPEAT_LOG_INTERVAL_SECONDS = 300.0
# 飞书图片消息上传接口的实际限制低于媒体动作允许保存的上限。
# 发送前只压缩传输副本，不能改写主体已保存的原始媒体。
_FEISHU_IMAGE_UPLOAD_MAX_BYTES = 9 * 1024 * 1024
_FEISHU_FILE_UPLOAD_MAX_BYTES = 30 * 1024 * 1024


def _redact_lark_sdk_log_message(message: str) -> str:
    """Remove Feishu's short-lived connection credentials from SDK logs."""
    return _LARK_SENSITIVE_QUERY_RE.sub(r"\1<redacted>", message)


class _LarkSdkLogFilter(logging.Filter):
    """Keep SDK diagnostics safe and aggregate expected reconnect chatter."""

    _elysium_lark_sdk_filter = True

    def __init__(self) -> None:
        super().__init__()
        self._last_repeated_error_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        message = _redact_lark_sdk_log_message(record.getMessage())
        # The SDK pre-formats all of its messages. Replacing msg/args here ensures
        # every handler receives the redacted form, including handlers added later.
        record.msg = message
        record.args = ()
        normalized = message.lower()

        # These lines describe the SDK's normal auto-reconnect state machine. The
        # adapter watchdog remains responsible for reporting sustained failures.
        if any(marker in normalized for marker in _LARK_ROUTINE_RECONNECT_LOGS):
            return False

        category = next(
            (marker for marker in _LARK_REPEATED_ERROR_LOGS if marker in normalized),
            None,
        )
        if category is None:
            return True

        now = time.monotonic()
        with self._lock:
            last_emitted_at = self._last_repeated_error_at.get(category)
            if (
                last_emitted_at is not None
                and now - last_emitted_at < _LARK_REPEAT_LOG_INTERVAL_SECONDS
            ):
                return False
            self._last_repeated_error_at[category] = now
        return True


_LARK_SDK_LOG_FILTER = _LarkSdkLogFilter()

# lark-oapi 必须在**主线程、模块加载期**完成导入，不能留到后台线程里首次导入。
#
# 原因：lark_oapi 是一棵 11000+ 个 .py 的巨型模块树（corehr/v2/model 一层就 1754 个）。
# 在 worker 线程里首次导入会长时间持有 Python import lock，与 ChromaDB 的 Rust 扩展
# （_upsert 需要回调 Python）形成死锁 —— 实测两次启动都必然卡死：
#   feishu_long_connection 线程  卡在 import lark_oapi 的 exec_module
#   ThreadPoolExecutor-0_x 线程  卡在 chromadb _upsert 的 futex（CPU 仅 0.02s）
#   MainThread                   再也拿不到机会跑 heartbeat，启动序列永久停滞
# 独立进程实测该 import 仅需 1.77s，所以卡住不是慢而是锁竞争。
#
# 提到模块级后，后台线程里的 `import lark_oapi` 只是 sys.modules 字典命中，不再持锁。
# 依赖缺失时保持优雅降级：置为 None，由 _run_long_connection_client 报错并跳过长连接。
try:
    import lark_oapi as _lark_oapi_module
    import lark_oapi.ws as _lark_oapi_ws_module
    import lark_oapi.ws.client as _lark_oapi_ws_client_module
    from lark_oapi.core.log import logger as _lark_sdk_logger

    _LARK_IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # noqa: BLE001
    _lark_oapi_module = None  # type: ignore[assignment]
    _lark_oapi_ws_module = None  # type: ignore[assignment]
    _lark_oapi_ws_client_module = None  # type: ignore[assignment]
    _lark_sdk_logger = None  # type: ignore[assignment]
    _LARK_IMPORT_ERROR = _exc


def _install_lark_sdk_log_filter() -> None:
    """Install the process-wide SDK filter once without replacing user handlers."""
    if _lark_sdk_logger is None:
        return
    if any(
        getattr(item, "_elysium_lark_sdk_filter", False)
        for item in _lark_sdk_logger.filters
    ):
        return
    _lark_sdk_logger.addFilter(_LARK_SDK_LOG_FILTER)


_install_lark_sdk_log_filter()

PLATFORM = "feishu"
_ADAPTER_INSTANCE: "FeishuAdapter | None" = None

# 飞书原始身份标识的前缀。这些串出现在「人名」位置时说明没解析出真名，
# 对上游（记忆、关系、人物画像）来说等于没有身份信息——她只能认错人。
_RAW_ID_PREFIXES = ("ou_", "on_", "od_", "oc_", "cli_")


def get_feishu_adapter() -> "FeishuAdapter | None":
    return _ADAPTER_INSTANCE


def set_feishu_adapter(adapter: "FeishuAdapter | None") -> None:
    global _ADAPTER_INSTANCE
    _ADAPTER_INSTANCE = adapter


class FeishuAdapter(BaseAdapter):
    """Feishu self-built app adapter.

    入方向：HTTP event callback -> MessageEnvelope -> CoreSink。
    出方向：life_chatter 回复 -> Feishu IM message API。
    """

    adapter_name = "feishu_adapter"
    adapter_version = "0.1.0"
    adapter_description = "Feishu self-built app HTTP callback adapter"
    platform = PLATFORM
    run_in_subprocess = False

    def __init__(
        self, core_sink: CoreSink, plugin: Any | None = None, **kwargs: Any
    ) -> None:
        super().__init__(core_sink, plugin=plugin, transport=None, **kwargs)
        self._tenant_access_token: str = ""
        self._tenant_access_token_expires_at: float = 0.0
        self._tenant_token_lock = asyncio.Lock()
        self._http_client_lock = asyncio.Lock()
        self._http_client: httpx.AsyncClient | None = None
        self._seen_event_ids: list[str] = []
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._long_connection_loop: asyncio.AbstractEventLoop | None = None
        self._long_connection_stop_signal: asyncio.Event | None = None
        self._long_connection_thread: threading.Thread | None = None
        self._long_connection_client: Any | None = None
        self._long_connection_state = "stopped"
        self._long_connection_state_lock = threading.Lock()
        self._last_lark_transport_activity_monotonic = 0.0
        # open_id/union_id -> 真实显示名。值为 "" 表示查过但查不到，
        # 用负缓存避免每条消息都去撞一次没权限的接口。
        self._display_name_cache: dict[str, str] = {}
        self._display_name_cached_at: dict[str, float] = {}
        self._identity_resolved_message_count = 0
        self._identity_unresolved_message_count = 0
        self._identity_last_success_at = 0.0
        self._identity_last_failure_at = 0.0
        self._identity_last_failure_reason = ""
        self._identity_warned_failures: set[str] = set()
        # 已收到私聊消息的用户 -> p2p chat_id。对当前会话的回复优先按 chat_id
        # 发送，避免被飞书按“向用户主动推送”路径限制。
        self._private_chat_ids: dict[str, str] = {}
        # Feishu 连接监控
        self._feishu_stop_event = threading.Event()
        self._feishu_watchdog_thread: threading.Thread | None = None
        set_feishu_adapter(self)
        logger.info("FeishuAdapter 初始化完成")

    async def on_adapter_loaded(self) -> None:
        config = self._config()
        if not config.plugin.enabled:
            logger.info("FeishuAdapter 已禁用")
            return
        if not config.app.app_id or not config.app.app_secret:
            logger.warning(
                "FeishuAdapter 缺少 app_id/app_secret；入站可接收，出站发送会失败"
            )
        self._feishu_stop_event.clear()
        if (
            config.connection.subscription_mode == "long_connection"
            and config.connection.auto_start_long_connection
        ):
            self._start_feishu_watchdog()
            self._start_long_connection()
        logger.info("FeishuAdapter 已加载，等待飞书事件回调")

    async def on_adapter_unloaded(self) -> None:
        self._feishu_stop_event.set()
        if self._feishu_watchdog_thread and self._feishu_watchdog_thread.is_alive():
            await asyncio.to_thread(self._feishu_watchdog_thread.join, 5)
            self._feishu_watchdog_thread = None
        await self._stop_long_connection()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        set_feishu_adapter(None)
        self._main_loop = None
        self._tenant_access_token = ""
        self._tenant_access_token_expires_at = 0.0
        self._seen_event_ids.clear()
        self._private_chat_ids.clear()
        self._display_name_cache.clear()
        self._display_name_cached_at.clear()
        self._identity_warned_failures.clear()
        logger.info("FeishuAdapter 已关闭")

    async def health_check(self) -> bool:
        return self._config().plugin.enabled

    def is_connected(self) -> bool:  # type: ignore[override]
        config = self._config()
        if not config.plugin.enabled:
            return False
        if config.connection.subscription_mode == "long_connection":
            thread = self._long_connection_thread
            with self._long_connection_state_lock:
                state = self._long_connection_state
            return bool(thread and thread.is_alive() and state == "connected")
        return True

    async def get_bot_info(self) -> dict[str, str]:  # type: ignore[override]
        config = self._config()
        return {
            "bot_id": config.bot.bot_open_id or "feishu_bot",
            "bot_name": config.bot.bot_name or "爱莉",
        }

    async def execute_action(
        self, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a named action via the Feishu action executor.

        Used by the unified platform_action tool.
        """
        if not hasattr(self, "_action_executor"):
            from .actions import FeishuActionExecutor

            self._action_executor = FeishuActionExecutor(self)
        return await self._action_executor.execute(action, params)

    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle a Feishu event callback payload."""
        if not self._config().plugin.enabled:
            return {"success": False, "ignored": True, "reason": "adapter_disabled"}

        if "encrypt" in payload:
            raise ValueError(
                "Feishu encrypted callbacks are not supported yet; disable Encrypt Key first"
            )

        envelope = await self.from_platform_message(payload)
        if envelope is None:
            return {"success": True, "ignored": True}
        await self.core_sink.send(envelope)
        return {"success": True, "message_id": envelope["message_info"]["message_id"]}

    async def send_message(self, raw_message: dict[str, Any]) -> dict[str, Any]:
        """Local test helper: inject a normalized Feishu-like text message."""
        envelope = await self.from_platform_message(raw_message)
        if envelope is None:
            raise ValueError("无法转换飞书消息")
        await self.core_sink.send(envelope)
        return raw_message

    def _start_feishu_watchdog(self) -> None:
        """启动 Feishu owner 线程监控。"""
        if self._feishu_watchdog_thread and self._feishu_watchdog_thread.is_alive():
            return
        thread = threading.Thread(
            target=self._feishu_watchdog_loop,
            name="feishu_watchdog",
            daemon=True,
        )
        self._feishu_watchdog_thread = thread
        thread.start()
        logger.info("[Feishu Watchdog] 飞书连接监控线程已启动")

    def _feishu_watchdog_loop(self) -> None:
        """仅在单一 owner 线程已经退出后恢复它。

        SDK 自身负责 ping/pong 和自动重连。进程内的 ``:443`` ``CLOSE-WAIT``
        无法证明 socket 属于飞书，因此不能授权 watchdog 关闭一个仍存活的 SDK。
        """
        while not self._feishu_stop_event.wait(timeout=60.0):
            try:
                thread = self._long_connection_thread
                if thread is None:
                    continue
                if not thread.is_alive():
                    logger.warning(
                        "[Feishu Watchdog] 长连接 owner 线程已退出，重新创建单一客户端"
                    )
                    self._start_long_connection()
            except Exception as exc:
                logger.error(f"[Feishu Watchdog] 监控循环异常: {exc}", exc_info=True)

    def _start_long_connection(self) -> None:
        config = self._config()
        if not config.app.app_id or not config.app.app_secret:
            logger.warning("飞书长连接未启动：缺少 app_id/app_secret")
            return
        if self._long_connection_thread and self._long_connection_thread.is_alive():
            logger.info("飞书长连接已经在运行")
            return

        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._main_loop = None

        thread = threading.Thread(
            target=self._run_long_connection_client,
            name="feishu_long_connection",
            daemon=True,
        )
        self._long_connection_thread = thread
        thread.start()
        logger.info("飞书长连接后台线程已启动")

    async def _stop_long_connection(self) -> None:
        self._feishu_stop_event.set()
        self._set_long_connection_state("stopping")
        thread = self._long_connection_thread
        sdk_loop = self._long_connection_loop
        stop_signal = self._long_connection_stop_signal

        if sdk_loop is not None and sdk_loop.is_running() and stop_signal is not None:
            sdk_loop.call_soon_threadsafe(stop_signal.set)

        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 15)
        if thread is not None and thread.is_alive():
            logger.warning("飞书长连接 owner 线程在 15 秒内未退出")
        elif self._long_connection_thread is thread:
            self._long_connection_thread = None
            self._long_connection_client = None
            self._set_long_connection_state("stopped")

    def _set_long_connection_state(
        self,
        state: str,
        *,
        transport_activity: bool = False,
    ) -> None:
        """在线程间发布 content-free 的 SDK 连接状态。"""
        with self._long_connection_state_lock:
            self._long_connection_state = state
            if transport_activity:
                self._last_lark_transport_activity_monotonic = time.monotonic()

    def _record_lark_transport_activity(self) -> None:
        """记录由当前 Lark 客户端证明的收发活动。"""
        self._set_long_connection_state("connected", transport_activity=True)

    def _instrument_lark_client(
        self,
        client: Any,
        receive_failed: asyncio.Event,
    ) -> None:
        """把连接状态绑定到这个 SDK client 的真实 transport 操作。"""
        original_connect = client._connect
        original_receive_message_loop = client._receive_message_loop
        original_handle_message = client._handle_message
        original_write_message = client._write_message

        async def observed_connect() -> Any:
            self._set_long_connection_state("connecting")
            result = await original_connect()
            if getattr(client, "_conn", None) is not None:
                self._record_lark_transport_activity()
            return result

        async def observed_receive_message_loop() -> Any:
            try:
                return await original_receive_message_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self._feishu_stop_event.is_set():
                    self._set_long_connection_state("failed")
                    receive_failed.set()
                raise

        async def observed_handle_message(message: bytes) -> Any:
            self._record_lark_transport_activity()
            return await original_handle_message(message)

        async def observed_write_message(data: bytes) -> Any:
            result = await original_write_message(data)
            self._record_lark_transport_activity()
            return result

        client._connect = observed_connect
        client._receive_message_loop = observed_receive_message_loop
        client._handle_message = observed_handle_message
        client._write_message = observed_write_message
        if hasattr(client, "on_reconnecting"):
            client.on_reconnecting = lambda: self._set_long_connection_state(
                "reconnecting"
            )
        if hasattr(client, "on_reconnected"):
            client.on_reconnected = self._record_lark_transport_activity

    async def _run_lark_client_session(self, client: Any) -> None:
        """在 owner loop 内运行一个可正常结束的 Lark SDK session。"""
        stop_signal = asyncio.Event()
        receive_failed = asyncio.Event()
        self._long_connection_stop_signal = stop_signal
        self._instrument_lark_client(client, receive_failed)

        try:
            await client._connect()
            asyncio.create_task(client._ping_loop(), name="feishu_sdk_ping")
            stop_wait = asyncio.create_task(
                stop_signal.wait(),
                name="feishu_sdk_stop_wait",
            )
            failure_wait = asyncio.create_task(
                receive_failed.wait(),
                name="feishu_sdk_failure_wait",
            )
            done, _ = await asyncio.wait(
                {stop_wait, failure_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if failure_wait in done and not self._feishu_stop_event.is_set():
                raise RuntimeError("Feishu SDK receive loop exited")
        finally:
            client._auto_reconnect = False
            try:
                await client._disconnect()
            except Exception as exc:
                safe_error = _redact_lark_sdk_log_message(str(exc))
                logger.warning(f"飞书 SDK 连接清理失败: {safe_error}")

            current_task = asyncio.current_task()
            pending_tasks = [
                task
                for task in asyncio.all_tasks()
                if task is not current_task and not task.done()
            ]
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            if self._long_connection_stop_signal is stop_signal:
                self._long_connection_stop_signal = None

    def _run_long_connection_client(self) -> None:
        # 使用模块级已导入的 lark_oapi（见文件头注释）。绝对不要在这里做首次导入：
        # 本函数运行在后台线程，首次导入会持有 import lock 并与 ChromaDB Rust 扩展死锁。
        if (
            _lark_oapi_module is None
            or _lark_oapi_ws_module is None
            or _lark_oapi_ws_client_module is None
        ):
            logger.error(
                "飞书长连接启动失败：锁定环境缺少 lark-oapi。"
                "请停止本次启动并执行 `deploy.sh bootstrap` 恢复依赖，"
                "禁止运行时临时 pip install。"
                f"error={_LARK_IMPORT_ERROR}",
                exc_info=_LARK_IMPORT_ERROR,
            )
            return
        lark = _lark_oapi_module
        lark_ws = _lark_oapi_ws_module

        sdk_loop = asyncio.new_event_loop()
        ws_client_module = _lark_oapi_ws_client_module
        previous_sdk_loop = ws_client_module.loop
        self._long_connection_loop = sdk_loop
        ws_client_module.loop = sdk_loop
        asyncio.set_event_loop(sdk_loop)

        try:
            config = self._config()
            log_level = getattr(
                lark.LogLevel,
                config.connection.long_connection_log_level,
                lark.LogLevel.INFO,
            )
            retry_delay = 5.0

            while not self._feishu_stop_event.is_set():
                event_handler = self._build_lark_event_handler(lark)
                client = lark_ws.Client(
                    app_id=config.app.app_id,
                    app_secret=config.app.app_secret,
                    log_level=log_level,
                    event_handler=event_handler,
                    domain=config.app.api_base_url,
                    auto_reconnect=True,
                    source="elysium-feishu-adapter",
                )
                self._long_connection_client = client
                self._set_long_connection_state("connecting")
                logger.info("飞书长连接正在连接开放平台")
                try:
                    sdk_loop.run_until_complete(self._run_lark_client_session(client))
                except Exception as exc:
                    if not self._feishu_stop_event.is_set():
                        self._set_long_connection_state("failed")
                        safe_error = _redact_lark_sdk_log_message(str(exc))
                        logger.error(f"飞书长连接已退出: {safe_error}")

                if self._feishu_stop_event.is_set():
                    break

                logger.info(f"飞书长连接断开，{retry_delay:.0f}s 后重连...")
                self._set_long_connection_state("reconnecting")
                self._feishu_stop_event.wait(timeout=retry_delay)
                retry_delay = min(retry_delay * 2, 60.0)  # 指数退避，最大 60s
        finally:
            pending_tasks = asyncio.all_tasks(sdk_loop)
            for task in pending_tasks:
                task.cancel()
            if pending_tasks and not sdk_loop.is_closed():
                sdk_loop.run_until_complete(
                    asyncio.gather(*pending_tasks, return_exceptions=True)
                )
            sdk_loop.close()
            asyncio.set_event_loop(None)
            if ws_client_module.loop is sdk_loop:
                ws_client_module.loop = previous_sdk_loop
            if self._long_connection_loop is sdk_loop:
                self._long_connection_loop = None
            self._long_connection_stop_signal = None
            self._long_connection_client = None
            self._set_long_connection_state(
                "stopped" if self._feishu_stop_event.is_set() else "failed"
            )

    def _build_lark_event_handler(self, lark_module: Any) -> Any:
        config = self._config()

        def on_message(event: Any) -> None:
            payload = self._lark_event_to_payload(event)
            loop = self._main_loop
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    self.handle_event(payload), loop
                )
                try:
                    future.result(timeout=30)
                except Exception as exc:
                    logger.error(f"飞书长连接事件投递失败: {exc}", exc_info=True)
            else:
                asyncio.run(self.handle_event(payload))

        def on_reaction(event: Any) -> None:
            # 表情回应（reaction）事件：注册空处理器避免 SDK 报
            # "processor not found" 错误；后续如需把回应当作输入，
            # 可在此接入 handle_event。
            return None

        return (
            lark_module.EventDispatcherHandler.builder(
                config.app.encrypt_key, config.app.verification_token
            )
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_im_message_reaction_created_v1(on_reaction)
            .register_p2_im_message_reaction_deleted_v1(on_reaction)
            .build()
        )

    @staticmethod
    def _lark_event_to_payload(event: Any) -> dict[str, Any]:
        try:
            from lark_oapi.core.json import JSON

            serialized = JSON.marshal(event)
            if isinstance(serialized, str):
                loaded = json.loads(serialized)
                if isinstance(loaded, dict):
                    return loaded
        except Exception:
            pass

        header = getattr(event, "header", None)
        data = getattr(event, "event", None)
        sender = getattr(data, "sender", None)
        message = getattr(data, "message", None)
        sender_id = getattr(sender, "sender_id", None)
        return {
            "schema": "2.0",
            "header": {
                "event_id": str(getattr(header, "event_id", "") or ""),
                "event_type": str(
                    getattr(header, "event_type", "im.message.receive_v1") or ""
                ),
                "token": str(getattr(header, "token", "") or ""),
            },
            "event": {
                "sender": {
                    "sender_type": str(getattr(sender, "sender_type", "") or ""),
                    "sender_id": {
                        "open_id": str(getattr(sender_id, "open_id", "") or ""),
                        "user_id": str(getattr(sender_id, "user_id", "") or ""),
                        "union_id": str(getattr(sender_id, "union_id", "") or ""),
                    },
                },
                "message": {
                    "message_id": str(getattr(message, "message_id", "") or ""),
                    "root_id": str(getattr(message, "root_id", "") or ""),
                    "parent_id": str(getattr(message, "parent_id", "") or ""),
                    "create_time": getattr(message, "create_time", None),
                    "chat_id": str(getattr(message, "chat_id", "") or ""),
                    "chat_type": str(getattr(message, "chat_type", "") or ""),
                    "message_type": str(getattr(message, "message_type", "") or ""),
                    "content": getattr(message, "content", "") or "",
                    "mentions": getattr(message, "mentions", None) or [],
                },
            },
        }

    async def _persisted_identity(
        self,
        account_id: str,
    ) -> tuple[str, str]:
        """Read an exact platform identity previously persisted by Core."""

        if not account_id:
            return "", ""
        try:
            from src.core.utils.user_query_helper import get_user_query_helper

            person = await get_user_query_helper().get_person_info(PLATFORM, account_id)
        except Exception as exc:  # noqa: BLE001 - identity lookup must not drop messages
            logger.debug(f"飞书数据库身份读取失败: {type(exc).__name__}")
            return "", ""
        if person is None:
            return "", ""
        display_name = str(getattr(person, "nickname", "") or "").strip()
        canonical_key = str(getattr(person, "canonical_person_key", "") or "").strip()
        if self._looks_like_raw_id(display_name) or display_name.startswith(
            "身份未解析的飞书用户"
        ):
            display_name = ""
        return display_name, canonical_key

    async def from_platform_message(  # type: ignore[override]
        self,
        raw: dict[str, Any],
    ) -> MessageEnvelope | None:
        try:
            normalized = self._normalize_incoming(raw)
            if normalized is None:
                return None
            if not self._should_process(normalized):
                return None

            message_id = normalized["message_id"]
            chat_id = normalized["chat_id"]
            chat_type = normalized["chat_type"]
            open_id = normalized["open_id"]
            user_id = normalized.get("user_id", "")
            union_id = normalized.get("union_id", "")
            sender_name = normalized["sender_name"]
            stable_account_id = open_id or union_id or user_id
            canonical_person_key = self._canonical_person_key(
                open_id=open_id,
                union_id=union_id,
                user_id=user_id,
            )
            configured_alias = self._configured_display_alias(
                open_id=open_id,
                union_id=union_id,
                user_id=user_id,
            )
            persisted_name, persisted_canonical_key = await self._persisted_identity(
                stable_account_id
            )
            if persisted_canonical_key:
                canonical_person_key = persisted_canonical_key
            identity_status = "resolved"
            identity_source = (
                "configured_alias"
                if configured_alias
                else "person_info"
                if persisted_name
                else "event"
            )

            # @ 段里飞书会直接带 name，白捡的映射先收进缓存
            self._harvest_mention_names(normalized.get("mentions") or [])

            # 没配 alias 的用户，_sender_name 只能退回 union_id/open_id 这种原始 ID。
            # 她看到的"人名"就成了 on_41a2efd3...，同一个群里几个人全是这种串，
            # 自然认不出谁是谁。这里补一次真名解析。
            if configured_alias:
                # An exact account mapping is an authored identity fact and is
                # therefore authoritative over any incidental event label.
                sender_name = configured_alias
            elif persisted_name:
                sender_name = persisted_name
            elif self._looks_like_raw_id(sender_name):
                resolved = await self._resolve_display_name(
                    open_id=open_id,
                    union_id=union_id,
                    user_id=user_id,
                    chat_id=chat_id,
                )
                if resolved:
                    sender_name = resolved
                    identity_source = "directory_or_cache"
                else:
                    sender_name = self._unresolved_sender_label(stable_account_id)
                    identity_status = "unresolved"
                    identity_source = "unresolved"

            if identity_status == "resolved":
                self._record_identity_resolution_success()
            else:
                self._identity_unresolved_message_count += 1

            content = normalized["content"]
            timestamp = normalized["timestamp"]
            media_refs = normalized.get("media_refs") or []

            extra: dict[str, Any] = {
                "source": "feishu",
                "feishu_event_id": normalized.get("event_id", ""),
                "feishu_message_id": message_id,
                "feishu_chat_id": chat_id,
                "chat_id": chat_id,
                "open_id": open_id,
                "feishu_open_id": open_id,
                "feishu_user_id": user_id,
                "feishu_union_id": union_id,
                "sender_platform_account_key": (
                    f"{PLATFORM}:{stable_account_id}" if stable_account_id else ""
                ),
                "canonical_person_key": canonical_person_key,
                "identity_resolution_status": identity_status,
                "identity_display_name_source": identity_source,
                "sender_type": normalized.get("sender_type", ""),
                "feishu_message_type": normalized.get("message_type", ""),
                "format_info": {"accept_format": ["text", "image"]},
            }
            if media_refs:
                extra["feishu_media_refs"] = media_refs
            if chat_type == "group":
                extra["target_group_id"] = chat_id
            else:
                extra["target_user_id"] = open_id

            if chat_type == "private" and open_id and chat_id:
                self._private_chat_ids[open_id] = chat_id

            message_info: dict[str, Any] = {
                "platform": PLATFORM,
                "message_id": message_id,
                "time": timestamp,
                "user_info": {
                    "platform": PLATFORM,
                    "user_id": open_id,
                    "user_nickname": sender_name,
                },
                "extra": extra,
            }
            if chat_type == "group":
                message_info["group_info"] = {
                    "platform": PLATFORM,
                    "group_id": chat_id,
                    "group_name": normalized.get("chat_name") or chat_id,
                }

            segments: list[dict[str, Any]] = []
            if normalized.get("root_message_id"):
                segments.append(
                    {"type": "reply", "data": normalized["root_message_id"]}
                )
            segments.extend(self._mention_segments(normalized.get("mentions") or []))
            media_segments = await self._download_incoming_media_segments(
                message_id, media_refs
            )
            if media_segments:
                if content and content not in {"[图片]", "[语音]", "[文件]"}:
                    segments.append({"type": "text", "data": content})
                segments.extend(media_segments)
            elif content:
                segments.append({"type": "text", "data": content})

            envelope: MessageEnvelope = {  # type: ignore[typeddict-item]
                "direction": "incoming",
                "message_info": message_info,
                "message_segment": segments,  # type: ignore[typeddict-item]
                "metadata": {"raw": raw, "feishu": True},
            }
            logger.info(
                f"收到飞书消息: chat_type={chat_type} sender={sender_name} content={content[:80]}"
            )
            return envelope
        except Exception as exc:
            logger.error(f"飞书消息转换失败: {exc}", exc_info=True)
            return None

    async def _send_platform_message(  # type: ignore[override]
        self,
        envelope: MessageEnvelope,
    ) -> dict[str, Any] | None:
        outgoing = self._extract_outgoing_message(envelope)
        text = outgoing["text"]
        reply_to = outgoing["reply_to"]
        image_data = outgoing["image_data"]
        voice_data = outgoing["voice_data"]
        file_data = outgoing["file_data"]
        file_name = outgoing["file_name"]

        if not text and not image_data and not voice_data and not file_data:
            logger.info("飞书出站消息为空，跳过发送")
            return

        message_info = envelope.get("message_info", {}) or {}
        group_info = message_info.get("group_info") or {}
        user_info = message_info.get("user_info") or {}
        chat_id = str(group_info.get("group_id") or "")
        open_id = str(user_info.get("user_id") or "")

        if image_data:
            return await self._send_image_message(
                chat_id=chat_id,
                open_id=open_id,
                reply_to=reply_to,
                image_data=image_data,
            )

        if voice_data:
            return await self._send_audio_message(
                chat_id=chat_id,
                open_id=open_id,
                reply_to=reply_to,
                voice_data=voice_data,
            )

        if file_data:
            return await self._send_file_message(
                chat_id=chat_id,
                open_id=open_id,
                reply_to=reply_to,
                file_data=file_data,
                file_name=file_name,
            )

        if self._config().behavior.reply_to_message and reply_to:
            response = await self._reply_text(reply_to, text)
            logger.info(f"飞书引用回复发送成功: reply_to={reply_to} text={text[:80]}")
            return response

        if chat_id:
            response = await self._send_text(
                receive_id_type="chat_id",
                receive_id=chat_id,
                text=text,
            )
            logger.info(f"飞书群消息发送成功: chat_id={chat_id} text={text[:80]}")
            return response

        if open_id:
            receive_id_type, receive_id = self._private_receive_target(open_id)
            response = await self._send_text(
                receive_id_type=receive_id_type,
                receive_id=receive_id,
                text=text,
            )
            logger.info(
                f"飞书私聊消息发送成功: {receive_id_type}={receive_id} text={text[:80]}"
            )
            return response

        raise ValueError("飞书出站消息缺少 chat_id/open_id，无法确定发送目标")

    def verify_callback_token(self, payload: dict[str, Any]) -> bool:
        expected = self._config().app.verification_token
        if not expected:
            return True
        token = str(
            payload.get("token") or payload.get("header", {}).get("token") or ""
        )
        return token == expected

    def _config(self) -> FeishuAdapterConfig:
        if self.plugin and isinstance(
            getattr(self.plugin, "config", None), FeishuAdapterConfig
        ):
            return cast(FeishuAdapterConfig, self.plugin.config)
        return FeishuAdapterConfig()

    def _normalize_incoming(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        if self._is_event_duplicate(raw):
            return None

        if "event" in raw and "header" in raw:
            event = raw.get("event") or {}
            header = raw.get("header") or {}
            message = event.get("message") or {}
            sender = event.get("sender") or {}
            sender_id = sender.get("sender_id") or {}
            message_type = str(message.get("message_type") or "")
            content_text = self._parse_content_text(
                message_type=message_type,
                content=message.get("content"),
            )
            media_refs = self._extract_incoming_media_refs(
                message_type=message_type,
                content=message.get("content"),
            )
            if not content_text and not media_refs:
                return None

            chat_type = "private" if message.get("chat_type") == "p2p" else "group"
            return {
                "event_id": str(header.get("event_id") or ""),
                "message_id": str(
                    message.get("message_id") or f"feishu_{uuid.uuid4().hex}"
                ),
                "message_type": message_type,
                "chat_id": str(message.get("chat_id") or ""),
                "chat_type": chat_type,
                "chat_name": str(message.get("chat_name") or ""),
                "open_id": str(
                    sender_id.get("open_id") or sender_id.get("user_id") or ""
                ),
                "user_id": str(sender_id.get("user_id") or ""),
                "union_id": str(sender_id.get("union_id") or ""),
                "sender_name": self._sender_name(sender, sender_id),
                "sender_type": str(sender.get("sender_type") or ""),
                "content": content_text,
                "timestamp": self._parse_time(message.get("create_time")),
                "mentions": message.get("mentions") or [],
                "root_message_id": message.get("root_id")
                or message.get("parent_id")
                or "",
                "media_refs": media_refs,
            }

        # Normalized/local test payload.
        message_type = str(raw.get("message_type") or "text")
        media_refs = self._extract_incoming_media_refs(
            message_type=message_type,
            content=raw.get("content"),
        )
        return {
            "event_id": str(raw.get("event_id") or ""),
            "message_id": str(
                raw.get("message_id") or f"feishu_local_{uuid.uuid4().hex}"
            ),
            "message_type": message_type,
            "chat_id": str(raw.get("chat_id") or raw.get("group_id") or ""),
            "chat_type": str(raw.get("chat_type") or "private"),
            "chat_name": str(raw.get("chat_name") or ""),
            "open_id": str(raw.get("open_id") or raw.get("user_id") or ""),
            "user_id": str(raw.get("user_id") or ""),
            "union_id": str(raw.get("union_id") or ""),
            "sender_name": self._sender_name(raw, raw),
            "sender_type": str(raw.get("sender_type") or "user"),
            "content": str(raw.get("content") or ""),
            "timestamp": float(raw.get("timestamp") or time.time()),
            "mentions": raw.get("mentions") or [],
            "root_message_id": raw.get("root_message_id") or raw.get("reply_to") or "",
            "media_refs": raw.get("media_refs") or media_refs,
        }

    def _is_event_duplicate(self, raw: dict[str, Any]) -> bool:
        event_message_id = ""
        event = raw.get("event")
        if isinstance(event, dict):
            message = event.get("message")
            if isinstance(message, dict):
                event_message_id = str(message.get("message_id") or "")
        dedupe_key = event_message_id or str(
            raw.get("message_id")
            or raw.get("header", {}).get("event_id")
            or raw.get("event_id")
            or ""
        )
        if not dedupe_key:
            return False
        if dedupe_key in self._seen_event_ids:
            logger.debug(f"忽略重复飞书消息事件: {dedupe_key}")
            return True
        self._seen_event_ids.append(dedupe_key)
        if len(self._seen_event_ids) > 2000:
            del self._seen_event_ids[:500]
        return False

    def _should_process(self, message: dict[str, Any]) -> bool:
        config = self._config()
        if config.behavior.ignore_bot_messages:
            sender_type = str(message.get("sender_type") or "")
            open_id = str(message.get("open_id") or "")
            if sender_type == "app" or (
                config.bot.bot_open_id and open_id == config.bot.bot_open_id
            ):
                logger.debug("忽略飞书 Bot 自身消息")
                return False

        if message.get("chat_type") == "group":
            return self._in_list_mode(
                value=str(message.get("chat_id") or ""),
                mode=config.behavior.group_list_type,
                items=config.behavior.group_list,
            )
        return self._in_list_mode(
            value=str(message.get("open_id") or ""),
            mode=config.behavior.private_list_type,
            items=config.behavior.private_list,
        )

    @staticmethod
    def _in_list_mode(value: str, mode: str, items: list[str]) -> bool:
        normalized = {str(item) for item in items}
        if mode == "whitelist":
            return value in normalized
        return value not in normalized

    @staticmethod
    def _parse_content_payload(content: Any) -> Any:
        parsed: Any = content
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = {"text": content}
        return parsed

    @staticmethod
    def _parse_content_text(message_type: str, content: Any) -> str:
        parsed = FeishuAdapter._parse_content_payload(content)
        if not isinstance(parsed, dict):
            return str(parsed or "")
        if message_type == "text":
            return str(parsed.get("text") or "")
        if message_type == "post":
            return FeishuAdapter._flatten_post_content(parsed)
        if message_type:
            if message_type == "image" and parsed.get("image_key"):
                return "[图片]"
            if message_type in {"audio", "file"} and parsed.get("file_key"):
                return "[语音]" if message_type == "audio" else "[文件]"
            return f"[飞书{message_type}消息: {json.dumps(parsed, ensure_ascii=False)[:300]}]"
        return str(parsed.get("text") or parsed.get("content") or "")

    @staticmethod
    def _extract_incoming_media_refs(
        message_type: str, content: Any
    ) -> list[dict[str, Any]]:
        parsed = FeishuAdapter._parse_content_payload(content)
        refs: list[dict[str, Any]] = []
        if not isinstance(parsed, dict):
            return refs

        if message_type == "image":
            image_key = str(parsed.get("image_key") or "").strip()
            if image_key:
                refs.append({"type": "image", "key": image_key})
            return refs

        if message_type in {"audio", "file"}:
            file_key = str(parsed.get("file_key") or "").strip()
            if file_key:
                reference: dict[str, Any] = {
                    "type": "voice" if message_type == "audio" else "file",
                    "key": file_key,
                }
                if message_type == "file":
                    filename = str(
                        parsed.get("file_name") or parsed.get("name") or ""
                    ).strip()
                    if filename:
                        reference["filename"] = filename
                    file_size = parsed.get("file_size") or parsed.get("size")
                    if isinstance(file_size, int) and not isinstance(file_size, bool):
                        reference["size"] = file_size
                refs.append(reference)
            return refs

        if message_type == "post":
            refs.extend(FeishuAdapter._extract_post_image_refs(parsed))
        return refs

    @staticmethod
    def _extract_post_image_refs(content: dict[str, Any]) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        post = content.get("post")
        if not isinstance(post, dict):
            return refs

        for locale_content in post.values():
            if not isinstance(locale_content, dict):
                continue
            for line in locale_content.get("content") or []:
                if not isinstance(line, list):
                    continue
                for item in line:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("tag") or "") not in {"img", "image"}:
                        continue
                    image_key = str(item.get("image_key") or "").strip()
                    if image_key:
                        refs.append({"type": "image", "key": image_key})
        return refs

    async def _download_incoming_media_segments(
        self,
        message_id: str,
        media_refs: list[Any],
    ) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        for media_ref in media_refs:
            if not isinstance(media_ref, dict):
                continue
            media_type = str(media_ref.get("type") or "").strip()
            media_key = str(media_ref.get("key") or "").strip()
            resource_type = "image" if media_type == "image" else "file"
            segment_type = (
                "image"
                if media_type == "image"
                else "voice"
                if media_type == "voice"
                else "file"
            )
            if media_type not in {"image", "voice", "file"} or not media_key:
                continue
            try:
                if media_type == "file":
                    file_bytes = await self._download_message_resource_bytes(
                        message_id=message_id,
                        resource_key=media_key,
                        resource_type=resource_type,
                    )
                    reference = await persist_received_file(
                        file_bytes,
                        filename=str(media_ref.get("filename") or f"{media_key}.bin"),
                        platform="feishu",
                    )
                else:
                    media_base64 = await self._download_message_resource_as_base64(
                        message_id=message_id,
                        resource_key=media_key,
                        resource_type=resource_type,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "飞书媒体下载失败，保留文本占位: "
                    f"message_id={message_id}, media_type={media_type}, "
                    f"media_key={media_key}, error_type={type(exc).__name__}"
                )
                continue
            if media_type == "file":
                segments.append(
                    {
                        "type": segment_type,
                        "data": {
                            "name": reference.filename,
                            "size": reference.size_bytes,
                            "id": media_key,
                            "path": str(reference.path),
                            "sha256": reference.sha256,
                            "storage_key": reference.storage_key,
                            "materialized": True,
                        },
                    }
                )
            elif media_type == "voice":
                segments.append(
                    {
                        "type": segment_type,
                        "data": {
                            "base64": media_base64,
                            "mime_type": "audio/ogg",
                            "filename": f"{media_key}.opus",
                        },
                    }
                )
            else:
                segments.append({"type": segment_type, "data": media_base64})
        return segments

    async def _download_message_resource_as_base64(
        self,
        *,
        message_id: str,
        resource_key: str,
        resource_type: str,
    ) -> str:
        content = await self._download_message_resource_bytes(
            message_id=message_id,
            resource_key=resource_key,
            resource_type=resource_type,
        )
        return base64.b64encode(content).decode("ascii")

    async def _download_message_resource_bytes(
        self,
        *,
        message_id: str,
        resource_key: str,
        resource_type: str,
    ) -> bytes:
        token = await self._get_tenant_access_token()
        url = self._api_url(
            f"/open-apis/im/v1/messages/{message_id}/resources/{resource_key}"
        )
        resp = await self._request_with_retry(
            "GET",
            url,
            timeout=30.0,
            params={"type": resource_type},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )

        content_type = str(resp.headers.get("content-type") or "").lower()
        if resp.status_code >= 400:
            if "json" in content_type:
                raise RuntimeError(
                    f"Feishu resource API failed: {self._decode_response(resp)}"
                )
            raise RuntimeError(
                f"Feishu resource API http error: status={resp.status_code}"
            )
        if "json" in content_type:
            data = self._decode_response(resp)
            if int(data.get("code", 0)) != 0:
                raise RuntimeError(f"Feishu resource API failed: {data}")
            raise RuntimeError(
                f"Feishu resource API returned json without binary resource: {data}"
            )
        if not resp.content:
            raise RuntimeError("Feishu resource API returned empty body")
        if len(resp.content) > MAX_RECEIVED_FILE_BYTES:
            raise RuntimeError("Feishu resource exceeds the received-file byte limit")
        return bytes(resp.content)

    @staticmethod
    def _flatten_post_content(content: dict[str, Any]) -> str:
        parts: list[str] = []
        post = content.get("post")
        if isinstance(post, dict):
            for locale_content in post.values():
                if not isinstance(locale_content, dict):
                    continue
                title = locale_content.get("title")
                if title:
                    parts.append(str(title))
                for line in locale_content.get("content") or []:
                    if not isinstance(line, list):
                        continue
                    for item in line:
                        if isinstance(item, dict) and item.get("text"):
                            parts.append(str(item["text"]))
        return "\n".join(part for part in parts if part).strip()

    def _sender_name(self, sender: dict[str, Any], sender_id: dict[str, Any]) -> str:
        alias = self._sender_name_alias(sender, sender_id)
        if alias:
            return alias

        for key in ("sender_name", "name", "union_id", "user_id", "open_id"):
            value = sender.get(key) or sender_id.get(key)
            if value:
                return str(value)
        return "Feishu User"

    def _sender_name_alias(
        self, sender: dict[str, Any], sender_id: dict[str, Any]
    ) -> str:
        aliases = self._parse_user_name_aliases(
            self._config().identity.user_name_aliases
        )
        if not aliases:
            return ""

        for key in ("open_id", "user_id", "union_id"):
            value = str(sender_id.get(key) or sender.get(key) or "").strip()
            if value and value in aliases:
                return aliases[value]
        return ""

    def _configured_display_alias(
        self,
        *,
        open_id: str,
        union_id: str,
        user_id: str,
    ) -> str:
        """Return a display alias only when an exact platform ID is configured."""
        aliases = self._parse_user_name_aliases(
            self._config().identity.user_name_aliases
        )
        for key in (open_id, union_id, user_id):
            if key and key in aliases:
                return aliases[key]
        return ""

    def _canonical_person_key(
        self,
        *,
        open_id: str,
        union_id: str,
        user_id: str,
    ) -> str:
        """Resolve an explicitly authored cross-platform person key.

        This mapping is an identity fact. It deliberately does not fall back to
        display-name similarity or message content.
        """
        aliases = self._parse_user_name_aliases(
            self._config().identity.canonical_identity_aliases
        )
        for key in (open_id, union_id, user_id):
            if key and key in aliases:
                return aliases[key]
        return ""

    @staticmethod
    def _parse_user_name_aliases(items: list[str]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for item in items:
            raw = str(item or "").strip()
            if not raw or "=" not in raw:
                continue
            key, name = raw.split("=", 1)
            key = key.strip()
            name = name.strip()
            if key and name:
                aliases[key] = name
        return aliases

    @staticmethod
    def _looks_like_raw_id(name: str) -> bool:
        """判断这个"名字"其实是一串飞书原始 ID。

        飞书的消息事件里 sender 只带 ID，不带 display name。原本 ``_sender_name``
        的兜底链最后会退到 union_id / open_id，于是她在上下文里看到的"人名"是
        ``on_41a2efd323503ed77bd4ce206f309db7`` 这种东西——两个人的 ID 长得一样乱，
        她自然就认错人。这里识别出这种伪名字，交给 API 去换真名。
        """
        value = name.strip()
        if not value:
            return True
        return value.startswith(_RAW_ID_PREFIXES)

    @staticmethod
    def _unresolved_sender_label(stable_account_id: str) -> str:
        """Build a stable, non-identifying label for an unresolved account."""
        normalized = str(stable_account_id or "").strip()
        suffix = normalized[-6:] if normalized else "unknown"
        return (
            f"身份未解析的飞书用户（账号…{suffix}；不可根据消息内容推断为任何已知人物）"
        )

    def _cache_display_name(self, key: str, name: str) -> None:
        """写入显示名缓存；``name`` 为空表示负缓存。"""
        if not key:
            return
        self._display_name_cache[key] = name
        self._display_name_cached_at[key] = time.time()

    def _cached_display_name(self, key: str) -> str | None:
        """读显示名缓存，超过 TTL 视为未命中。返回 None 表示未命中。"""
        if not key or key not in self._display_name_cache:
            return None
        cached_value = self._display_name_cache[key]
        identity_config = self._config().identity
        ttl = float(
            identity_config.display_name_cache_ttl
            if cached_value
            else identity_config.display_name_negative_cache_ttl
        )
        cached_at = self._display_name_cached_at.get(key, 0.0)
        if ttl > 0 and time.time() - cached_at > ttl:
            self._display_name_cache.pop(key, None)
            self._display_name_cached_at.pop(key, None)
            return None
        return cached_value

    def _record_identity_resolution_success(self) -> None:
        self._identity_resolved_message_count += 1
        self._identity_last_success_at = time.time()

    def _record_identity_lookup_failure(self, source: str, exc: Exception) -> None:
        reason = self._identity_failure_reason(source, exc)
        self._identity_last_failure_at = time.time()
        self._identity_last_failure_reason = reason
        warning_key = (
            "permission_denied" if reason.endswith(":permission_denied") else reason
        )
        if warning_key in self._identity_warned_failures:
            return
        self._identity_warned_failures.add(warning_key)
        logger.warning(
            "飞书身份解析已降级: "
            f"source={source} reason={reason}; "
            "请配置确定性身份别名，或为飞书应用授予并发布 im:chat.members:read 权限"
        )

    @staticmethod
    def _identity_failure_reason(source: str, exc: Exception) -> str:
        text = str(exc).lower()
        if any(
            marker in text
            for marker in (
                "99991672",
                "41050",
                "access denied",
                "no user authority",
                "permission",
            )
        ):
            return f"{source}:permission_denied"
        return f"{source}:{type(exc).__name__}"

    def identity_health_snapshot(self) -> dict[str, Any]:
        """Return read-only, privacy-safe identity resolution health."""
        config = self._config().identity
        if not config.resolve_display_names:
            status = "disabled"
        elif (
            self._identity_last_failure_at > self._identity_last_success_at
            and self._identity_unresolved_message_count > 0
        ):
            status = "degraded"
        elif self._identity_resolved_message_count > 0:
            status = "ok"
        else:
            status = "unknown"
        return {
            "status": status,
            "configured_display_aliases": len(
                self._parse_user_name_aliases(config.user_name_aliases)
            ),
            "configured_canonical_identities": len(
                set(
                    self._parse_user_name_aliases(
                        config.canonical_identity_aliases
                    ).values()
                )
            ),
            "resolved_messages": self._identity_resolved_message_count,
            "unresolved_messages": self._identity_unresolved_message_count,
            "negative_cache_entries": sum(
                1 for value in self._display_name_cache.values() if not value
            ),
            "last_success_at": self._identity_last_success_at or None,
            "last_failure_at": self._identity_last_failure_at or None,
            "last_failure_reason": self._identity_last_failure_reason or None,
        }

    def _harvest_mention_names(self, mentions: list[Any]) -> None:
        """从 @ 段里白捡显示名。

        飞书在 mentions 里是会给 name 的，而 sender 里不给。群里只要有人被 @ 过，
        就能零成本地把他的 ID→真名 记下来，之后他自己发言时就有名字可用了。
        """
        for mention in mentions or []:
            if not isinstance(mention, dict):
                continue
            name = str(mention.get("name") or "").strip()
            if not name or self._looks_like_raw_id(name):
                continue
            mention_id = mention.get("id")
            if isinstance(mention_id, dict):
                for key in ("open_id", "union_id", "user_id"):
                    value = str(mention_id.get(key) or "").strip()
                    if value:
                        self._cache_display_name(value, name)
            elif mention_id:
                self._cache_display_name(str(mention_id).strip(), name)

    async def _resolve_display_name(
        self,
        *,
        open_id: str,
        union_id: str,
        user_id: str,
        chat_id: str,
    ) -> str:
        """把发送者 ID 换成人类可读的名字。

        顺序：本地缓存（含 @ 段白捡的）→ 通讯录接口 → 群成员列表。
        全部失败时返回 ""，由调用方保留原有兜底，绝不因为取名失败而丢消息。

        Args:
            open_id: 发送者 open_id
            union_id: 发送者 union_id
            user_id: 发送者 user_id
            chat_id: 消息所在会话 ID，用于群成员列表兜底

        Returns:
            str: 解析到的显示名；未解析到返回空串
        """
        keys = [k for k in (open_id, union_id, user_id) if k]

        # 先扫一遍正命中：任一 ID 上挂着真名就直接用，
        # 不能因为 open_id 上有负缓存就放弃 union_id 上已经捡到的名字。
        negative_hit = False
        for key in keys:
            cached = self._cached_display_name(key)
            if cached:
                return cached
            if cached == "":
                negative_hit = True
        if negative_hit:
            # 上次查过且查不到，TTL 内不再打接口
            return ""

        config = self._config()
        if not config.identity.resolve_display_names:
            return ""
        if not config.app.app_id or not config.app.app_secret:
            # 没凭据就拿不到 tenant_access_token，打接口只是白白抛异常。
            # 这里不写负缓存：等凭据配上以后立刻就能正常解析，不用等 TTL 过期。
            return ""

        name = ""
        if open_id:
            name = await self._fetch_contact_name(open_id)
        if not name and chat_id:
            name = await self._fetch_name_from_chat_members(chat_id, keys)

        # 无论成功与否都写缓存：成功避免重复查询，失败写负缓存避免每条消息都打接口
        for key in keys:
            self._cache_display_name(key, name)
        return name

    async def _fetch_contact_name(self, open_id: str) -> str:
        """查通讯录接口拿显示名。需要 contact:user.base:readonly 权限。"""
        try:
            data = await self._get_json(
                f"/open-apis/contact/v3/users/{open_id}?user_id_type=open_id"
            )
        except Exception as exc:
            self._record_identity_lookup_failure("contact", exc)
            logger.debug(f"飞书通讯录查名失败: open_id={open_id} error={exc}")
            return ""
        user = (data.get("data") or {}).get("user") or {}
        for key in ("nickname", "name", "en_name"):
            value = str(user.get(key) or "").strip()
            if value and not self._looks_like_raw_id(value):
                return value
        return ""

    async def _fetch_name_from_chat_members(self, chat_id: str, keys: list[str]) -> str:
        """从群成员列表里找名字。

        通讯录接口常因权限未开而 403，群成员列表只要 bot 在群里通常就能读，
        所以留作兜底。
        """
        try:
            data = await self._get_json(
                f"/open-apis/im/v1/chats/{chat_id}/members"
                "?member_id_type=open_id&page_size=100"
            )
        except Exception as exc:
            self._record_identity_lookup_failure("chat_members", exc)
            logger.debug(f"飞书群成员查名失败: chat_id={chat_id} error={exc}")
            return ""

        wanted = {k for k in keys if k}
        for item in (data.get("data") or {}).get("items") or []:
            if not isinstance(item, dict):
                continue
            member_id = str(item.get("member_id") or "").strip()
            if member_id and member_id in wanted:
                name = str(item.get("name") or "").strip()
                if name and not self._looks_like_raw_id(name):
                    return name
        return ""

    async def _get_json(self, path: str) -> dict[str, Any]:
        """GET 飞书开放平台接口并校验业务 code。"""
        token = await self._get_tenant_access_token()
        resp = await self._request_with_retry(
            "GET",
            self._api_url(path),
            timeout=10.0,
            headers={"Authorization": f"Bearer {token}"},
        )
        data = self._decode_response(resp)
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"Feishu API failed: path={path}, response={data}")
        return data

    @staticmethod
    def _parse_time(raw_time: Any) -> float:
        if raw_time is None:
            return time.time()
        try:
            value = float(raw_time)
        except (TypeError, ValueError):
            return time.time()
        if value > 10_000_000_000:
            return value / 1000.0
        return value

    @staticmethod
    def _mention_segments(mentions: list[Any]) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_id = mention.get("id") or {}
            user_id = (
                mention_id.get("open_id")
                if isinstance(mention_id, dict)
                else mention_id
            )
            if not user_id:
                continue
            segments.append(
                {
                    "type": "at",
                    "data": str(user_id),
                    "name": str(mention.get("name") or ""),
                }
            )
        return segments

    @staticmethod
    def _extract_outgoing_text(envelope: MessageEnvelope) -> tuple[str, str]:
        outgoing = FeishuAdapter._extract_outgoing_message(envelope)
        return outgoing["text"], outgoing["reply_to"]

    @staticmethod
    def _extract_outgoing_message(envelope: MessageEnvelope) -> dict[str, Any]:
        segments = envelope.get("message_segment", []) or []
        if isinstance(segments, dict):
            segments = [segments]
        text_parts: list[str] = []
        reply_to = ""
        image_data = ""
        voice_data = ""
        file_data = ""
        file_name = ""
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data")
            if seg_type == "reply" and not reply_to:
                reply_to = str(data or "")
            elif seg_type == "text":
                text_parts.append(str(data or ""))
            elif seg_type == "image" and not image_data:
                image_data = FeishuAdapter._stringify_media_data(data)
            elif seg_type == "voice" and not voice_data:
                voice_data = FeishuAdapter._stringify_media_data(data)
            elif seg_type == "file" and not file_data:
                file_data = FeishuAdapter._stringify_media_data(data)
                if isinstance(data, dict):
                    file_name = str(
                        data.get("name") or data.get("filename") or ""
                    ).strip()
                if (
                    not file_name
                    and file_data
                    and len(file_data) < 4096
                    and not file_data.startswith(("data:", "base64|", "base64://"))
                ):
                    file_name = Path(file_data).name
        return {
            "text": "".join(text_parts).strip(),
            "reply_to": reply_to,
            "image_data": image_data,
            "voice_data": voice_data,
            "file_data": file_data,
            "file_name": file_name,
        }

    def _private_receive_target(self, open_id: str) -> tuple[str, str]:
        """优先返回已建立的私聊会话，缺失时回退到用户 open_id。"""
        private_chat_id = self._private_chat_ids.get(open_id, "")
        if private_chat_id:
            return "chat_id", private_chat_id
        return "open_id", open_id

    async def _send_image_message(
        self,
        *,
        chat_id: str,
        open_id: str,
        reply_to: str,
        image_data: str,
    ) -> dict[str, Any]:
        image_key = await self._upload_image_data(image_data)
        if self._config().behavior.reply_to_message and reply_to:
            response = await self._reply_image(reply_to, image_key)
            logger.info(
                f"飞书引用图片发送成功: reply_to={reply_to} image_key={image_key}"
            )
            return response
        if chat_id:
            response = await self._send_image("chat_id", chat_id, image_key)
            logger.info(f"飞书群图片发送成功: chat_id={chat_id} image_key={image_key}")
            return response
        if open_id:
            receive_id_type, receive_id = self._private_receive_target(open_id)
            response = await self._send_image(receive_id_type, receive_id, image_key)
            logger.info(
                "飞书私聊图片发送成功: "
                f"{receive_id_type}={receive_id} image_key={image_key}"
            )
            return response
        raise ValueError("飞书出站图片缺少 chat_id/open_id，无法确定发送目标")

    @staticmethod
    def _prepare_image_upload_bytes(image_bytes: bytes) -> tuple[bytes, str]:
        """Return an upload-safe image copy without modifying the saved original."""
        if len(image_bytes) <= _FEISHU_IMAGE_UPLOAD_MAX_BYTES:
            return image_bytes, "application/octet-stream"
        try:
            with PILImage.open(BytesIO(image_bytes)) as source:
                image = source.convert("RGB")
                # Resize oversized raster images before quality reduction. The
                # original bytes remain in the workspace and are never replaced.
                max_dimension = 4096
                if max(image.size) > max_dimension:
                    scale = max_dimension / max(image.size)
                    image = image.resize(
                        (
                            max(1, round(image.width * scale)),
                            max(1, round(image.height * scale)),
                        ),
                        PILImage.Resampling.LANCZOS,
                    )
                for quality in (85, 75, 65, 55, 45):
                    output = BytesIO()
                    image.save(output, format="JPEG", quality=quality, optimize=True)
                    candidate = output.getvalue()
                    if len(candidate) <= _FEISHU_IMAGE_UPLOAD_MAX_BYTES:
                        return candidate, "image/jpeg"
        except Exception as exc:  # noqa: BLE001
            raise ValueError("飞书图片超过上传限制，且无法生成压缩传输副本") from exc
        raise ValueError(
            f"飞书图片压缩后仍超过上传限制 {_FEISHU_IMAGE_UPLOAD_MAX_BYTES} bytes"
        )

    async def _upload_image_data(self, image_data: str) -> str:
        image_bytes = self._decode_media_data(image_data, media_label="图片")
        image_bytes, image_mime = self._prepare_image_upload_bytes(image_bytes)
        token = await self._get_tenant_access_token()
        resp = await self._request_with_retry(
            "POST",
            self._api_url("/open-apis/im/v1/images"),
            timeout=30.0,
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={
                "image": (
                    "image.jpg" if image_mime == "image/jpeg" else "image.png",
                    image_bytes,
                    image_mime,
                )
            },
        )
        payload = self._decode_response(resp)
        image_key = str((payload.get("data") or {}).get("image_key") or "")
        if not image_key:
            raise ValueError(f"飞书图片上传响应缺少 image_key: {payload}")
        return image_key

    async def _reply_image(self, message_id: str, image_key: str) -> dict[str, Any]:
        normalized_message_id = self._normalize_message_id(message_id)
        return await self._post_json(
            f"/open-apis/im/v1/messages/{normalized_message_id}/reply",
            {
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
            },
        )

    async def _send_image(
        self,
        receive_id_type: str,
        receive_id: str,
        image_key: str,
    ) -> dict[str, Any]:
        return await self._post_json(
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
            },
        )

    async def _send_audio_message(
        self,
        *,
        chat_id: str,
        open_id: str,
        reply_to: str,
        voice_data: str,
    ) -> dict[str, Any]:
        try:
            file_key, duration_ms = await self._upload_audio(voice_data)
        except Exception as exc:
            logger.error(f"飞书语音上传失败，将降级为文本提示: {exc}", exc_info=True)
            fallback_text = "[语音发送失败：飞书音频上传没有成功]"
            if self._config().behavior.reply_to_message and reply_to:
                return await self._reply_text(reply_to, fallback_text)
            if chat_id:
                return await self._send_text("chat_id", chat_id, fallback_text)
            if open_id:
                receive_id_type, receive_id = self._private_receive_target(open_id)
                return await self._send_text(receive_id_type, receive_id, fallback_text)
            raise ValueError(
                "飞书出站语音缺少 chat_id/open_id，无法确定发送目标"
            ) from exc

        if self._config().behavior.reply_to_message and reply_to:
            response = await self._reply_audio(reply_to, file_key, duration_ms)
            logger.info(
                f"飞书引用语音发送成功: reply_to={reply_to} file_key={file_key}"
            )
            return response

        if chat_id:
            response = await self._send_audio(
                receive_id_type="chat_id",
                receive_id=chat_id,
                file_key=file_key,
                duration_ms=duration_ms,
            )
            logger.info(f"飞书群语音发送成功: chat_id={chat_id} file_key={file_key}")
            return response

        if open_id:
            receive_id_type, receive_id = self._private_receive_target(open_id)
            response = await self._send_audio(
                receive_id_type=receive_id_type,
                receive_id=receive_id,
                file_key=file_key,
                duration_ms=duration_ms,
            )
            logger.info(
                "飞书私聊语音发送成功: "
                f"{receive_id_type}={receive_id} file_key={file_key}"
            )
            return response

        raise ValueError("飞书出站语音缺少 chat_id/open_id，无法确定发送目标")

    async def _upload_audio(self, voice_data: str) -> tuple[str, int]:
        audio_bytes = self._decode_media_data(voice_data)
        opus_bytes, duration_ms = await asyncio.to_thread(
            self._convert_audio_to_opus, audio_bytes
        )
        token = await self._get_tenant_access_token()
        url = self._api_url("/open-apis/im/v1/files")
        filename = "voice.opus"
        files = {
            "file": (
                filename,
                opus_bytes,
                "audio/opus",
            ),
        }
        data = {
            "file_type": "opus",
            "file_name": filename,
            "duration": str(duration_ms),
        }
        resp = await self._request_with_retry(
            "POST",
            url,
            timeout=30.0,
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            files=files,
        )
        payload = self._decode_response(resp)
        file_key = str((payload.get("data") or {}).get("file_key") or "")
        if not file_key:
            raise ValueError(f"飞书文件上传响应缺少 file_key: {payload}")
        return file_key, duration_ms

    async def _reply_audio(
        self, message_id: str, file_key: str, duration_ms: int
    ) -> dict[str, Any]:
        normalized_message_id = self._normalize_message_id(message_id)
        return await self._post_json(
            f"/open-apis/im/v1/messages/{normalized_message_id}/reply",
            {
                "msg_type": "audio",
                "content": json.dumps(
                    {"file_key": file_key, "duration": duration_ms},
                    ensure_ascii=False,
                ),
            },
        )

    async def _send_audio(
        self,
        receive_id_type: str,
        receive_id: str,
        file_key: str,
        duration_ms: int,
    ) -> dict[str, Any]:
        return await self._post_json(
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "audio",
                "content": json.dumps(
                    {"file_key": file_key, "duration": duration_ms},
                    ensure_ascii=False,
                ),
            },
        )

    async def _send_file_message(
        self,
        *,
        chat_id: str,
        open_id: str,
        reply_to: str,
        file_data: str,
        file_name: str,
    ) -> dict[str, Any]:
        file_key, uploaded_name = await self._upload_file_data(
            file_data,
            file_name=file_name,
        )
        if self._config().behavior.reply_to_message and reply_to:
            response = await self._reply_file(reply_to, file_key)
            logger.info(
                f"飞书引用文件发送成功: reply_to={reply_to} file_name={uploaded_name}"
            )
            return response
        if chat_id:
            response = await self._send_file("chat_id", chat_id, file_key)
            logger.info(
                f"飞书群文件发送成功: chat_id={chat_id} file_name={uploaded_name}"
            )
            return response
        if open_id:
            receive_id_type, receive_id = self._private_receive_target(open_id)
            response = await self._send_file(
                receive_id_type,
                receive_id,
                file_key,
            )
            logger.info(
                "飞书私聊文件发送成功: "
                f"{receive_id_type}={receive_id} file_name={uploaded_name}"
            )
            return response
        raise ValueError("飞书出站文件缺少 chat_id/open_id，无法确定发送目标")

    async def _upload_file_data(
        self,
        file_data: str,
        *,
        file_name: str = "",
    ) -> tuple[str, str]:
        raw = str(file_data or "").strip()
        if not raw:
            raise ValueError("飞书文件数据为空")

        source_path: Path | None = None
        if len(raw) < 4096:
            try:
                candidate = Path(raw).expanduser()
                if candidate.exists() and candidate.is_file():
                    source_path = candidate.resolve()
            except OSError:
                source_path = None

        if source_path is not None:
            stat = await asyncio.to_thread(source_path.stat)
            if stat.st_size <= 0:
                raise ValueError("飞书不允许上传空文件")
            if stat.st_size > _FEISHU_FILE_UPLOAD_MAX_BYTES:
                raise ValueError("飞书文件超过上传字节上限")
            file_bytes = await asyncio.to_thread(source_path.read_bytes)
            default_name = source_path.name
        else:
            file_bytes = await asyncio.to_thread(
                self._decode_media_data,
                raw,
                media_label="文件",
            )
            default_name = "attachment.bin"

        if not file_bytes:
            raise ValueError("飞书不允许上传空文件")
        if len(file_bytes) > _FEISHU_FILE_UPLOAD_MAX_BYTES:
            raise ValueError("飞书文件超过上传字节上限")
        safe_name = Path(str(file_name or default_name)).name.strip() or default_name

        token = await self._get_tenant_access_token()
        resp = await self._request_with_retry(
            "POST",
            self._api_url("/open-apis/im/v1/files"),
            timeout=120.0,
            headers={"Authorization": f"Bearer {token}"},
            data={"file_type": "stream", "file_name": safe_name},
            files={
                "file": (
                    safe_name,
                    file_bytes,
                    "application/octet-stream",
                )
            },
        )
        payload = self._decode_response(resp)
        file_key = str((payload.get("data") or {}).get("file_key") or "")
        if not file_key:
            raise ValueError("飞书文件上传响应缺少 file_key")
        return file_key, safe_name

    async def _reply_file(self, message_id: str, file_key: str) -> dict[str, Any]:
        normalized_message_id = self._normalize_message_id(message_id)
        return await self._post_json(
            f"/open-apis/im/v1/messages/{normalized_message_id}/reply",
            {
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
            },
        )

    async def _send_file(
        self,
        receive_id_type: str,
        receive_id: str,
        file_key: str,
    ) -> dict[str, Any]:
        return await self._post_json(
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
            },
        )

    @staticmethod
    def _convert_audio_to_opus(audio_bytes: bytes) -> tuple[bytes, int]:
        opus_bytes = transcode_audio_to_opus(audio_bytes)
        return opus_bytes, probe_audio_duration_ms(opus_bytes)

    @staticmethod
    def _probe_audio_duration_ms(
        path: Path,
        *,
        ffprobe: str | None = None,
    ) -> int:
        try:
            ffprobe_command = ffprobe or shutil.which("ffprobe")
            if not ffprobe_command:
                return 1
            result = subprocess.run(
                [
                    ffprobe_command,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            seconds = float(result.stdout.strip() or "0")
            return max(1, int(seconds * 1000))
        except Exception:
            return 1

    @staticmethod
    def _stringify_media_data(data: Any) -> str:
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("data", "base64", "url", "path"):
                value = data.get(key)
                if value:
                    return str(value)
        return str(data or "")

    @staticmethod
    def _decode_media_data(data: str, *, media_label: str = "音频") -> bytes:
        raw = str(data or "").strip()
        if not raw:
            raise ValueError(f"{media_label}数据为空")
        if raw.startswith("data:"):
            _, _, raw = raw.partition(",")
        if raw.startswith("base64|"):
            raw = raw.removeprefix("base64|")
        if raw.startswith(("http://", "https://")):
            raise ValueError(f"飞书{media_label}暂不支持 URL 直传")
        if len(raw) < 4096:
            try:
                path = Path(raw)
                if path.exists() and path.is_file():
                    return path.read_bytes()
            except OSError:
                pass
        return base64.b64decode(raw, validate=False)

    @staticmethod
    def _normalize_message_id(message_id: str) -> str:
        """还原飞书 OpenAPI 接受的原始消息 ID。"""
        normalized = str(message_id or "").strip()
        if normalized.startswith("msg_om_"):
            return normalized.removeprefix("msg_")
        return normalized

    async def _reply_text(self, message_id: str, text: str) -> dict[str, Any]:
        normalized_message_id = self._normalize_message_id(message_id)
        return await self._post_json(
            f"/open-apis/im/v1/messages/{normalized_message_id}/reply",
            {
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    async def _send_text(
        self, receive_id_type: str, receive_id: str, text: str
    ) -> dict[str, Any]:
        return await self._post_json(
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 2,
        initial_delay: float = 0.5,
        backoff_factor: float = 2.0,
        **kwargs: Any,
    ) -> httpx.Response:
        """发起 HTTP 请求，仅对传输层抖动重试。

        飞书开放平台偶发 "Server disconnected without sending a response"，
        不重试会让消息永久丢失。业务错误（4xx / code != 0）由调用方处理，不在此重试。
        """
        delay = initial_delay
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                client = await self._get_http_client()
                return await client.request(
                    method,
                    url,
                    timeout=timeout,
                    **kwargs,
                )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                logger.warning(
                    f"飞书请求传输失败，重试中: url={url} "
                    f"attempt={attempt + 1}/{max_retries + 1} "
                    f"error={type(exc).__name__}: {exc}"
                )
                await asyncio.sleep(delay)
                delay *= backoff_factor
        assert last_error is not None
        raise last_error

    async def _get_http_client(self) -> httpx.AsyncClient:
        """获取适配器生命周期内复用的 HTTP 连接池。"""
        client = self._http_client
        if client is not None and not client.is_closed:
            return client

        async with self._http_client_lock:
            client = self._http_client
            if client is None or client.is_closed:
                client = httpx.AsyncClient(
                    timeout=20.0,
                    limits=httpx.Limits(
                        max_connections=20,
                        max_keepalive_connections=10,
                    ),
                )
                self._http_client = client
            return client

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self._get_tenant_access_token()
        url = self._api_url(path)
        resp = await self._request_with_retry(
            "POST",
            url,
            timeout=20.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
        )
        data = self._decode_response(resp)
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"Feishu API failed: path={path}, response={data}")
        return data

    async def _get_tenant_access_token(self) -> str:
        now = time.time()
        if self._tenant_access_token and self._tenant_access_token_expires_at > now:
            return self._tenant_access_token

        async with self._tenant_token_lock:
            now = time.time()
            if self._tenant_access_token and self._tenant_access_token_expires_at > now:
                return self._tenant_access_token

            config = self._config()
            if not config.app.app_id or not config.app.app_secret:
                raise RuntimeError("Feishu app_id/app_secret 未配置")

            resp = await self._request_with_retry(
                "POST",
                self._api_url("/open-apis/auth/v3/tenant_access_token/internal"),
                timeout=20.0,
                json={
                    "app_id": config.app.app_id,
                    "app_secret": config.app.app_secret,
                },
            )
            data = self._decode_response(resp)
            if int(data.get("code", 0)) != 0:
                raise RuntimeError(f"Feishu tenant_access_token failed: {data}")
            token = str(data.get("tenant_access_token") or "")
            if not token:
                raise RuntimeError(f"Feishu tenant_access_token missing: {data}")
            expire = float(data.get("expire") or 7200)
            self._tenant_access_token = token
            self._tenant_access_token_expires_at = now + max(
                60.0,
                expire - 300.0,
            )
            return token

    def _api_url(self, path: str) -> str:
        base = self._config().app.api_base_url.rstrip("/")
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{base}{path}"

    @staticmethod
    def _decode_response(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(
                f"Feishu API returned non-json: status={resp.status_code}"
            ) from exc
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Feishu API http error: status={resp.status_code}, response={data}"
            )
        if not isinstance(data, dict):
            raise RuntimeError(f"Feishu API returned invalid json: {data}")
        return data
