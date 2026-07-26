"""统一组件注册表。

替代 plugin_manager + service_manager + adapter_manager 的分散管理。
所有"能力提供者"（插件、服务、适配器）统一注册、统一发现、统一生命周期。

设计原则：
- 不区分类型：框架不预设"这是插件还是服务"，都是 Component
- 协议发现：通过 Protocol 类型发现能力，而非通过名字
- 生命周期统一：load → start → stop，由注册表统一管理

用法：
    from src.core.registry import component_registry, Component

    class MyService(Component):
        name = "my_service"

        async def on_start(self):
            ...

    component_registry.register(MyService())
    await component_registry.start_all()
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from src.kernel.logger import get_logger

logger = get_logger("registry", display="Registry", enable_event_broadcast=False)


class ComponentState(str, Enum):
    """组件生命周期状态。"""

    REGISTERED = "registered"
    LOADED = "loaded"
    STARTED = "started"
    STOPPED = "stopped"
    ERROR = "error"


class Component:
    """组件基类。

    所有可注册到框架的实体都继承此类。
    不强制区分 Plugin / Service / Adapter —— 都是 Component。
    """

    name: str = "unnamed"
    version: str = "0.1.0"
    priority: int = 0  # 启动优先级（越小越先）

    def __init__(self) -> None:
        self._state = ComponentState.REGISTERED
        self._capabilities: dict[type, Any] = {}

    @property
    def state(self) -> ComponentState:
        return self._state

    def provides(self, protocol: type, impl: Any) -> None:
        """声明此组件提供的能力。"""
        self._capabilities[protocol] = impl

    def capability(self, protocol: type) -> Any | None:
        """获取此组件提供的某项能力。"""
        return self._capabilities.get(protocol)

    # ─── 生命周期钩子（子类覆写）──────────────

    async def on_load(self) -> None:
        """加载阶段：读取配置、注册工具等轻量操作。"""

    async def on_start(self) -> None:
        """启动阶段：建立连接、开始监听等。"""

    async def on_stop(self) -> None:
        """停止阶段：释放资源。"""


class ComponentRegistry:
    """统一组件注册表。"""

    def __init__(self) -> None:
        self._components: dict[str, Component] = {}
        self._by_protocol: dict[type, list[Component]] = {}

    # ─── 注册 ────────────────────────────────

    def register(self, component: Component) -> None:
        """注册组件。"""
        if component.name in self._components:
            logger.warning(f"组件 '{component.name}' 已存在，覆盖注册")
        self._components[component.name] = component
        logger.debug(f"注册组件: {component.name} v{component.version}")

    def unregister(self, name: str) -> Component | None:
        """注销组件。"""
        comp = self._components.pop(name, None)
        if comp:
            # 清理协议索引
            for proto, comps in self._by_protocol.items():
                if comp in comps:
                    comps.remove(comp)
        return comp

    # ─── 发现 ────────────────────────────────

    def get(self, name: str) -> Component | None:
        """按名称获取。"""
        return self._components.get(name)

    def discover(self, protocol: type) -> list[Component]:
        """按协议类型发现组件。"""
        return self._by_protocol.get(protocol, [])

    def all(self) -> list[Component]:
        """所有已注册组件。"""
        return list(self._components.values())

    def __len__(self) -> int:
        return len(self._components)

    def __contains__(self, name: str) -> bool:
        return name in self._components

    # ─── 生命周期 ─────────────────────────────

    async def load_all(self) -> None:
        """按优先级加载所有组件。"""
        sorted_comps = sorted(self._components.values(), key=lambda c: c.priority)
        for comp in sorted_comps:
            try:
                await comp.on_load()
                comp._state = ComponentState.LOADED
            except Exception as e:
                comp._state = ComponentState.ERROR
                logger.error(f"组件 '{comp.name}' 加载失败: {e}")

    async def start_all(self) -> None:
        """按优先级启动所有已加载组件。"""
        sorted_comps = sorted(
            (c for c in self._components.values() if c._state == ComponentState.LOADED),
            key=lambda c: c.priority,
        )
        for comp in sorted_comps:
            try:
                await comp.on_start()
                comp._state = ComponentState.STARTED
            except Exception as e:
                comp._state = ComponentState.ERROR
                logger.error(f"组件 '{comp.name}' 启动失败: {e}")

    async def stop_all(self) -> None:
        """逆优先级停止所有运行中组件。"""
        sorted_comps = sorted(
            (c for c in self._components.values() if c._state == ComponentState.STARTED),
            key=lambda c: -c.priority,
        )
        for comp in sorted_comps:
            try:
                await comp.on_stop()
                comp._state = ComponentState.STOPPED
            except Exception as e:
                logger.error(f"组件 '{comp.name}' 停止失败: {e}")


# ─────────────────────────────────────────────
# 全局实例
# ─────────────────────────────────────────────

component_registry = ComponentRegistry()
