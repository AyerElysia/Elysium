"""轻量级服务容器（依赖注入）。

设计目标：
- 替代散落的全局单例 (get_event_bus, get_task_manager, ...)
- 协议驱动：通过 Protocol/ABC 类型注册和解析
- 零依赖：不引入第三方 DI 框架
- 作用域：支持全局单例 + 请求级 scoped 实例

用法：
    from src.kernel.container import container

    # 注册（启动阶段）
    container.register(EventBusProtocol, event_bus_instance)
    container.register(LogStoreProtocol, log_store, singleton=True)

    # 解析（任何地方）
    bus = container.resolve(EventBusProtocol)

    # 工厂模式（每次 resolve 创建新实例）
    container.register_factory(SessionProtocol, lambda c: Session(c.resolve(DBProtocol)))

    # 作用域（请求级）
    async with container.scoped() as scope:
        session = scope.resolve(SessionProtocol)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Callable, TypeVar, overload

T = TypeVar("T")


class ServiceNotFoundError(KeyError):
    """请求的服务未注册。"""

    def __init__(self, protocol: type) -> None:
        super().__init__(f"服务未注册: {protocol.__name__}")
        self.protocol = protocol


class _Registration:
    """内部注册条目。"""

    __slots__ = ("factory", "instance", "singleton")

    def __init__(
        self,
        factory: Callable[["Container"], Any] | None,
        instance: Any | None,
        singleton: bool,
    ) -> None:
        self.factory = factory
        self.instance = instance
        self.singleton = singleton


class Container:
    """服务容器。

    线程安全（asyncio 环境下无锁，因为 GIL + 单事件循环）。
    """

    def __init__(self) -> None:
        self._registry: dict[type, _Registration] = {}
        self._overrides: dict[type, Any] = {}  # 测试用临时覆盖

    # ─── 注册 ────────────────────────────────

    def register(
        self,
        protocol: type[T],
        instance: T,
        *,
        singleton: bool = True,
    ) -> None:
        """注册一个服务实例。

        Args:
            protocol: 服务接口类型（Protocol/ABC/class）
            instance: 实现实例
            singleton: 是否单例（默认 True）
        """
        self._registry[protocol] = _Registration(
            factory=None, instance=instance, singleton=singleton
        )

    def register_factory(
        self,
        protocol: type[T],
        factory: Callable[["Container"], T],
        *,
        singleton: bool = False,
    ) -> None:
        """注册一个服务工厂。

        Args:
            protocol: 服务接口类型
            factory: 工厂函数，接收容器实例，返回服务实例
            singleton: 是否缓存首次创建结果
        """
        self._registry[protocol] = _Registration(
            factory=factory, instance=None, singleton=singleton
        )

    # ─── 解析 ────────────────────────────────

    @overload
    def resolve(self, protocol: type[T]) -> T: ...

    @overload
    def resolve(self, protocol: type[T], default: T) -> T: ...

    def resolve(self, protocol: type[T], default: Any = ...) -> T:
        """解析服务。

        Args:
            protocol: 服务接口类型
            default: 未注册时的默认值（不提供则抛 ServiceNotFoundError）

        Returns:
            服务实例
        """
        # 测试覆盖优先
        if protocol in self._overrides:
            return self._overrides[protocol]

        reg = self._registry.get(protocol)
        if reg is None:
            if default is not ...:
                return default
            raise ServiceNotFoundError(protocol)

        # 已有实例
        if reg.instance is not None:
            return reg.instance

        # 工厂创建
        if reg.factory is not None:
            instance = reg.factory(self)
            if reg.singleton:
                reg.instance = instance  # 缓存
            return instance

        if default is not ...:
            return default
        raise ServiceNotFoundError(protocol)

    def has(self, protocol: type) -> bool:
        """检查服务是否已注册。"""
        return protocol in self._overrides or protocol in self._registry

    # ─── 测试支持 ─────────────────────────────

    def override(self, protocol: type[T], instance: T) -> None:
        """临时覆盖服务（测试用）。"""
        self._overrides[protocol] = instance

    def clear_overrides(self) -> None:
        """清除所有测试覆盖。"""
        self._overrides.clear()

    # ─── 作用域 ────────────────────────────────

    @asynccontextmanager
    async def scoped(self):
        """创建请求级作用域容器。

        作用域内的注册不影响父容器；解析时先查本作用域，再查父容器。
        """
        child = ScopedContainer(parent=self)
        try:
            yield child
        finally:
            await child.cleanup()

    # ─── 生命周期 ─────────────────────────────

    def reset(self) -> None:
        """清空所有注册（仅测试用）。"""
        self._registry.clear()
        self._overrides.clear()

    def __contains__(self, protocol: type) -> bool:
        return self.has(protocol)

    def __repr__(self) -> str:
        services = [p.__name__ for p in self._registry]
        return f"Container(services={services})"


class ScopedContainer:
    """请求级作用域容器。"""

    def __init__(self, parent: Container) -> None:
        self._parent = parent
        self._local: dict[type, Any] = {}
        self._disposables: list[Any] = []

    def register(self, protocol: type[T], instance: T) -> None:
        """在作用域内注册服务。"""
        self._local[protocol] = instance

    def resolve(self, protocol: type[T], default: Any = ...) -> T:
        """解析：先查本作用域，再查父容器。"""
        if protocol in self._local:
            return self._local[protocol]
        return self._parent.resolve(protocol, default)

    def track_disposable(self, obj: Any) -> None:
        """追踪需要在作用域结束时清理的对象。"""
        self._disposables.append(obj)

    async def cleanup(self) -> None:
        """清理作用域内的可释放资源。"""
        for obj in self._disposables:
            if hasattr(obj, "aclose"):
                await obj.aclose()
            elif hasattr(obj, "close"):
                obj.close()
        self._disposables.clear()
        self._local.clear()


# ─────────────────────────────────────────────
# 全局容器实例
# ─────────────────────────────────────────────

container = Container()
