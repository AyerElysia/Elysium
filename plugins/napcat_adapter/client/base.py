"""NapCat API Client 基础层

封装 WebSocket 请求/响应池、超时管理、连接状态。
所有 API mixin 通过 self.call(action, params) 发起调用。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import orjson

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("napcat_adapter")


class NapCatClientBase:
    """NapCat WebSocket API 客户端核心。

    职责：
    - 管理 API 请求的 echo 匹配与响应池
    - 提供通用 call(action, params, timeout) 方法
    - 连接状态查询
    """

    def __init__(self) -> None:
        self._response_pool: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ws: Any = None  # WebSocketLike，由 adapter 注入
        self._default_timeout: float = 30.0

    # ------------------------------------------------------------------
    # WebSocket 注入
    # ------------------------------------------------------------------

    def bind_ws(self, ws: Any) -> None:
        """绑定 WebSocket 连接实例（由 adapter 在连接建立时调用）。"""
        self._ws = ws

    def unbind_ws(self) -> None:
        """解绑 WebSocket（连接断开时调用）。"""
        self._ws = None
        # 取消所有等待中的 Future
        for future in self._response_pool.values():
            if not future.done():
                future.cancel()
        self._response_pool.clear()

    @property
    def connected(self) -> bool:
        """当前 WebSocket 是否可用。"""
        return self._ws is not None and not getattr(self._ws, "closed", True)

    # ------------------------------------------------------------------
    # 核心调用方法
    # ------------------------------------------------------------------

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """向 NapCat 发送 API 请求并等待响应。

        Args:
            action: OneBot API 动作名（如 send_group_msg）
            params: API 参数字典
            timeout: 超时秒数，None 使用默认值

        Returns:
            NapCat 返回的完整响应 dict（含 status/data/echo 等）

        Raises:
            RuntimeError: WebSocket 未连接
            asyncio.TimeoutError: 请求超时
        """
        if not self.connected:
            # 记录详细的连接状态用于诊断
            ws_state = "None" if self._ws is None else f"closed={getattr(self._ws, 'closed', 'unknown')}"
            logger.error(f"WebSocket 未连接，无法调用 {action}。状态: _ws={ws_state}")
            raise RuntimeError(f"WebSocket 未连接，无法调用 {action}")

        echo = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._response_pool[echo] = future

        request_payload = orjson.dumps({
            "action": action,
            "params": params or {},
            "echo": echo,
        }).decode()

        effective_timeout = timeout if timeout is not None else self._default_timeout

        try:
            await self._ws.send(request_payload)
            response = await asyncio.wait_for(future, timeout=effective_timeout)
            return response
        except asyncio.TimeoutError:
            logger.error(f"API 请求超时: {action} (timeout={effective_timeout}s)")
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"API 请求失败: {action}, 错误: {e}")
            raise
        finally:
            self._response_pool.pop(echo, None)

    async def call_data(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """调用 API 并直接返回 data 字段（便捷方法）。

        如果 status != ok，记录警告但仍返回 data。
        """
        resp = await self.call(action, params, timeout=timeout)
        if resp.get("status") != "ok":
            logger.warning(f"API {action} 返回非 ok: {resp.get('message', resp.get('msg', ''))}")
        return resp.get("data")

    # ------------------------------------------------------------------
    # 响应分发（由 EventRouter 调用）
    # ------------------------------------------------------------------

    def dispatch_response(self, raw: dict[str, Any]) -> bool:
        """尝试将原始消息作为 API 响应分发到等待池。

        Args:
            raw: WebSocket 收到的原始 JSON dict

        Returns:
            True 表示这是一条 API 响应并已分发，False 表示不是
        """
        if "echo" not in raw:
            return False

        echo = raw.get("echo")
        if not echo or echo not in self._response_pool:
            return False

        future = self._response_pool[echo]
        if not future.done():
            future.set_result(raw)
        return True
