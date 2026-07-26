"""服务协议定义。

所有 kernel 级服务的 Protocol 接口集中于此。
模块通过 `container.resolve(XxxProtocol)` 获取服务，
而非直接 import 具体实现——实现可替换，接口不变。

用法：
    from src.kernel.protocols import EventBusProtocol, LogStoreProtocol
    from src.kernel.container import container

    bus = container.resolve(EventBusProtocol)
    await bus.publish("message_received", {"content": "hello"})
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ─────────────────────────────────────────────
# 事件总线
# ─────────────────────────────────────────────


@runtime_checkable
class EventBusProtocol(Protocol):
    """事件总线协议。"""

    async def publish(self, event: str, params: dict[str, Any]) -> dict[str, Any]:
        """发布事件，返回最终参数。"""
        ...

    def subscribe(self, event: str, handler: Any, *, priority: int = 0) -> None:
        """订阅事件。"""
        ...

    def unsubscribe(self, event: str, handler: Any) -> None:
        """取消订阅。"""
        ...


# ─────────────────────────────────────────────
# 日志存储
# ─────────────────────────────────────────────


@runtime_checkable
class LogStoreProtocol(Protocol):
    """日志存储协议。"""

    def write(self, level: str, module: str, message: str, **extra: Any) -> None:
        """写入一条日志。"""
        ...

    def query(
        self,
        *,
        level: str | None = None,
        module: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询日志。"""
        ...


# ─────────────────────────────────────────────
# 任务调度
# ─────────────────────────────────────────────


@runtime_checkable
class SchedulerProtocol(Protocol):
    """任务调度器协议。"""

    def schedule(
        self,
        fn: Any,
        *,
        interval: float | None = None,
        cron: str | None = None,
        name: str | None = None,
    ) -> str:
        """注册定时任务，返回任务 ID。"""
        ...

    def cancel(self, task_id: str) -> bool:
        """取消定时任务。"""
        ...


# ─────────────────────────────────────────────
# 数据库
# ─────────────────────────────────────────────


@runtime_checkable
class DatabaseProtocol(Protocol):
    """数据库服务协议。"""

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        """执行 SQL。"""
        ...

    async def fetch_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """查询多行。"""
        ...

    async def fetch_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """查询单行。"""
        ...


# ─────────────────────────────────────────────
# 向量存储
# ─────────────────────────────────────────────


@runtime_checkable
class VectorStoreProtocol(Protocol):
    """向量存储协议。"""

    async def add(
        self, texts: list[str], metadatas: list[dict[str, Any]] | None = None, ids: list[str] | None = None
    ) -> list[str]:
        """添加向量。"""
        ...

    async def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """语义搜索。"""
        ...


# ─────────────────────────────────────────────
# LLM 服务
# ─────────────────────────────────────────────


@runtime_checkable
class LLMServiceProtocol(Protocol):
    """LLM 服务协议。"""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "default",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """单轮对话。"""
        ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "default",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        """流式对话，返回 AsyncGenerator。"""
        ...


# ─────────────────────────────────────────────
# 工具注册表
# ─────────────────────────────────────────────


@runtime_checkable
class ToolRegistryProtocol(Protocol):
    """工具注册表协议。"""

    def register(self, name: str, fn: Any, *, schema: dict[str, Any] | None = None) -> None:
        """注册工具。"""
        ...

    def get(self, name: str) -> Any | None:
        """获取工具。"""
        ...

    def list_tools(self) -> list[dict[str, Any]]:
        """列出所有工具的 schema。"""
        ...

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """执行工具。"""
        ...
