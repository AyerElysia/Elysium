"""Elysium Kernel。

基础设施层：提供配置、事件、并发、LLM、日志、调度、存储等核心服务。

快速入口：
    from src.kernel import container, get_config, bootstrap
"""

from .container import container
from .config.unified import get_config, init_config
from .protocols import (
    EventBusProtocol,
    LogStoreProtocol,
    SchedulerProtocol,
    DatabaseProtocol,
    VectorStoreProtocol,
    LLMServiceProtocol,
    ToolRegistryProtocol,
)

__all__ = [
    "container",
    "get_config",
    "init_config",
    # Protocols
    "EventBusProtocol",
    "LogStoreProtocol",
    "SchedulerProtocol",
    "DatabaseProtocol",
    "VectorStoreProtocol",
    "LLMServiceProtocol",
    "ToolRegistryProtocol",
]
