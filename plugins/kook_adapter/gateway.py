"""KOOK WebSocket Gateway 客户端

实现 KOOK WebSocket 协议：
- 信令 0: 事件推送
- 信令 1: HELLO 握手
- 信令 2: PING（客户端 → 服务端，每 30s）
- 信令 3: PONG
- 信令 4: RESUME
- 信令 5: RECONNECT（服务端要求重连）
- 信令 6: RESUME ACK
"""
from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable, Coroutine
from typing import Any

import websockets
import websockets.exceptions

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.concurrency import get_task_manager

logger = get_logger("kook_adapter")

# 心跳间隔基准（秒），实际加随机 ±5
_HEARTBEAT_INTERVAL = 30
# 心跳超时（秒）
_HEARTBEAT_TIMEOUT = 6
# 最大重连退避（秒）
_MAX_BACKOFF = 60


class KookGateway:
    """KOOK WebSocket Gateway 管理器。

    职责：连接管理、心跳维护、断线重连、事件分发。
    纯传输层——不做任何内容判断。
    """

    def __init__(
        self,
        token: str,
        get_gateway_url: Callable[[], Coroutine[Any, Any, str]],
        on_event: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self._token = token
        self._get_gateway_url = get_gateway_url
        self._on_event = on_event

        self._ws: Any | None = None
        self._session_id: str = ""
        self._sn: int = 0  # 已处理的最新 sn
        self._running = False
        self._heartbeat_task_info: Any | None = None
        self._listen_task_info: Any | None = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._running

    @property
    def alive(self) -> bool:
        """Return whether the gateway manager is still running its retry loop."""

        task_info = self._listen_task_info
        task = task_info.task if task_info is not None else None
        return self._running and task is not None and not task.done()

    async def start(self) -> None:
        """启动 Gateway 连接。"""
        async with self._lifecycle_lock:
            current = self._listen_task_info
            if current is not None and current.task and not current.task.done():
                return
            self._running = True
            self._listen_task_info = get_task_manager().create_task(
                self._connect_loop(),
                name="kook-gateway",
                daemon=True,
            )
            logger.info("KOOK Gateway 启动中...")

    async def stop(self) -> None:
        """停止 Gateway 连接。"""
        async with self._lifecycle_lock:
            self._running = False
            if self._ws:
                try:
                    await asyncio.wait_for(self._ws.close(), timeout=5.0)
                except Exception as exc:  # noqa: BLE001 - transport close is best effort
                    logger.warning(f"KOOK WebSocket 关闭失败: {exc}")
                finally:
                    self._ws = None

            task_infos = [
                info
                for info in (self._heartbeat_task_info, self._listen_task_info)
                if info is not None
            ]
            task_manager = get_task_manager()
            for task_info in task_infos:
                task_manager.cancel_task(task_info.task_id)
            tasks = [
                task_info.task
                for task_info in task_infos
                if task_info.task is not None
                and task_info.task is not asyncio.current_task()
            ]
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=5.0)
                for task in pending:
                    logger.warning(f"KOOK 后台任务未及时停止: {task.get_name()}")
                for task in done:
                    if not task.cancelled():
                        task.exception()
            self._heartbeat_task_info = None
            self._listen_task_info = None
            logger.info("KOOK Gateway 已停止")

    # ─── 连接循环 ───────────────────────────────────────────

    async def _connect_loop(self) -> None:
        """主连接循环：获取 Gateway → 连接 → 监听 → 断线退避重连。"""
        backoff = 2
        while self._running:
            try:
                url = await self._get_gateway_url()
                await self._connect_and_listen(url)
                # 正常退出（如收到 reconnect 信令），重置退避
                backoff = 2
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(f"KOOK Gateway 连接异常: {exc}，{backoff}s 后重连...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _connect_and_listen(self, url: str) -> None:
        """建立 WebSocket 连接并监听消息。"""
        # 如果是 resume，拼接参数
        connect_url = url
        if self._session_id and self._sn > 0:
            sep = "&" if "?" in url else "?"
            connect_url = f"{url}{sep}resume=1&sn={self._sn}&session_id={self._session_id}"

        async with websockets.connect(connect_url, max_size=2**22) as ws:
            self._ws = ws
            logger.info("KOOK WebSocket 已连接")

            # 等待 HELLO（信令 1）
            hello_ok = await self._wait_hello(ws)
            if not hello_ok:
                raise RuntimeError("HELLO 握手失败")

            # 启动心跳
            self._heartbeat_task_info = get_task_manager().create_task(
                self._heartbeat_loop(ws),
                name="kook-heartbeat",
                daemon=True,
            )

            try:
                await self._listen(ws)
            finally:
                heartbeat_info = self._heartbeat_task_info
                if heartbeat_info is not None:
                    get_task_manager().cancel_task(heartbeat_info.task_id)
                    heartbeat_task = heartbeat_info.task
                    if heartbeat_task is not None:
                        await asyncio.gather(heartbeat_task, return_exceptions=True)
                    self._heartbeat_task_info = None
                self._ws = None

    async def _wait_hello(self, ws: Any) -> bool:
        """等待信令 1（HELLO），6s 超时。"""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=_HEARTBEAT_TIMEOUT)
            msg = json.loads(raw)
            if msg.get("s") == 1:
                d = msg.get("d", {})
                if d.get("code") == 0:
                    self._session_id = d.get("session_id", "")
                    logger.info(f"KOOK HELLO 成功，session={self._session_id[:12]}...")
                    return True
                logger.error(f"KOOK HELLO 失败: code={d.get('code')}")
        except asyncio.TimeoutError:
            logger.error("KOOK HELLO 超时（6s）")
        return False

    async def _listen(self, ws: Any) -> None:
        """消息监听主循环。"""
        async for raw in ws:
            if not self._running:
                break
            try:
                msg = json.loads(raw)
                await self._dispatch_signal(msg)
            except json.JSONDecodeError:
                logger.warning(f"KOOK 收到非 JSON 消息: {raw[:100]}")
            except Exception as exc:
                logger.error(f"KOOK 事件处理异常: {exc}", exc_info=True)

    async def _dispatch_signal(self, msg: dict[str, Any]) -> None:
        """分发 WebSocket 信令。"""
        signal = msg.get("s")

        if signal == 0:
            # 事件推送
            sn = msg.get("sn", 0)
            if sn <= self._sn:
                return  # 已处理过的 sn，丢弃
            self._sn = sn
            event_data = msg.get("d", {})
            await self._on_event(event_data)

        elif signal == 3:
            # PONG — 心跳回复，无需处理
            pass

        elif signal == 5:
            # RECONNECT — 服务端要求重连
            logger.warning("KOOK 服务端要求 RECONNECT，清空状态重连...")
            self._sn = 0
            self._session_id = ""
            if self._ws:
                await self._ws.close()

        elif signal == 6:
            # RESUME ACK
            d = msg.get("d", {})
            self._session_id = d.get("session_id", self._session_id)
            logger.info("KOOK RESUME ACK，离线消息已同步")

    # ─── 心跳 ───────────────────────────────────────────────

    async def _heartbeat_loop(self, ws: Any) -> None:
        """每 30±5s 发送 PING（信令 2），携带当前 sn。"""
        try:
            while self._running:
                interval = _HEARTBEAT_INTERVAL + random.randint(-5, 5)
                await asyncio.sleep(interval)
                ping = json.dumps({"s": 2, "sn": self._sn})
                await ws.send(ping)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug(f"KOOK 心跳循环退出: {exc}")
