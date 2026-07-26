"""消息管线。

替代 transport/ 的 5 个子包，用一条管线 + middleware 链处理所有消息。

设计：
- 每个 middleware 是 async (msg, next) -> msg | None
- 返回 None 表示拦截（消息不再继续）
- 管线是双向的：incoming（收到消息）和 outgoing（发送消息）

用法：
    from src.core.pipeline import MessagePipeline

    pipeline = MessagePipeline()
    pipeline.use(dedup_middleware)
    pipeline.use(permission_middleware)
    pipeline.use(routing_middleware)

    result = await pipeline.process_incoming(message)
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from src.kernel.logger import get_logger

logger = get_logger("pipeline", display="Pipeline", enable_event_broadcast=False)

# Middleware 签名：接收消息和 next 函数，返回处理后的消息或 None（拦截）
NextFn = Callable[[Any], Coroutine[Any, Any, Any]]
Middleware = Callable[[Any, NextFn], Coroutine[Any, Any, Any]]


class MessagePipeline:
    """消息处理管线。

    支持入站（incoming）和出站（outgoing）两条管线。
    """

    def __init__(self) -> None:
        self._incoming: list[Middleware] = []
        self._outgoing: list[Middleware] = []

    # ─── 注册 ────────────────────────────────

    def use(self, middleware: Middleware, *, direction: str = "incoming") -> "MessagePipeline":
        """添加 middleware。

        Args:
            middleware: 中间件函数
            direction: "incoming" | "outgoing" | "both"

        Returns:
            self（支持链式调用）
        """
        if direction in ("incoming", "both"):
            self._incoming.append(middleware)
        if direction in ("outgoing", "both"):
            self._outgoing.append(middleware)
        return self

    def use_incoming(self, middleware: Middleware) -> "MessagePipeline":
        """添加入站 middleware。"""
        self._incoming.append(middleware)
        return self

    def use_outgoing(self, middleware: Middleware) -> "MessagePipeline":
        """添加出站 middleware。"""
        self._outgoing.append(middleware)
        return self

    # ─── 执行 ────────────────────────────────

    async def process_incoming(self, message: Any) -> Any:
        """处理入站消息。

        消息依次通过所有 incoming middleware。
        任何 middleware 返回 None 则消息被拦截。
        """
        return await self._execute(self._incoming, message)

    async def process_outgoing(self, message: Any) -> Any:
        """处理出站消息。"""
        return await self._execute(self._outgoing, message)

    async def _execute(self, middlewares: list[Middleware], message: Any) -> Any:
        """构建 middleware 链并执行。"""
        if not middlewares:
            return message

        async def terminal(msg: Any) -> Any:
            """链尾：直接返回消息。"""
            return msg

        # 从后往前构建链
        handler = terminal
        for mw in reversed(middlewares):
            handler = _wrap_middleware(mw, handler)

        return await handler(message)

    # ─── 内省 ────────────────────────────────

    @property
    def incoming_count(self) -> int:
        return len(self._incoming)

    @property
    def outgoing_count(self) -> int:
        return len(self._outgoing)


def _wrap_middleware(mw: Middleware, next_fn: NextFn) -> NextFn:
    """包装 middleware，捕获异常防止链断裂。"""

    async def wrapped(msg: Any) -> Any:
        try:
            return await mw(msg, next_fn)
        except Exception as e:
            logger.error(f"Middleware 异常: {mw.__name__}: {e}")
            # 异常时跳过此 middleware，继续链
            return await next_fn(msg)

    wrapped.__name__ = f"wrapped_{getattr(mw, '__name__', 'anonymous')}"
    return wrapped


# ─────────────────────────────────────────────
# 常用 Middleware 工厂
# ─────────────────────────────────────────────


def filter_middleware(predicate: Callable[[Any], bool], *, name: str = "filter") -> Middleware:
    """过滤 middleware：不满足条件的消息被拦截。"""

    async def mw(msg: Any, next_fn: NextFn) -> Any:
        if predicate(msg):
            return await next_fn(msg)
        return None  # 拦截

    mw.__name__ = name
    return mw


def transform_middleware(transform: Callable[[Any], Any], *, name: str = "transform") -> Middleware:
    """转换 middleware：修改消息内容。"""

    async def mw(msg: Any, next_fn: NextFn) -> Any:
        transformed = transform(msg)
        return await next_fn(transformed)

    mw.__name__ = name
    return mw


def tap_middleware(callback: Callable[[Any], None], *, name: str = "tap") -> Middleware:
    """旁路 middleware：观察消息但不修改（日志、指标等）。"""

    async def mw(msg: Any, next_fn: NextFn) -> Any:
        callback(msg)
        return await next_fn(msg)

    mw.__name__ = name
    return mw


# ─────────────────────────────────────────────
# 全局管线实例
# ─────────────────────────────────────────────

pipeline = MessagePipeline()
