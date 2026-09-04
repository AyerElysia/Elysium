"""life_engine 子系统集成模块。

包含记忆集成的初始化与管理。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from .core import LifeEngineService


logger = get_logger("life_engine", display="life_engine")


def to_jsonable(value: Any) -> Any:
    """将复杂对象转换为 JSON 可序列化结构。

    Args:
        value: 要转换的值

    Returns:
        JSON 可序列化的值
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()

    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return to_jsonable(tolist())
        except (TypeError, ValueError, AttributeError) as e:
            logger.debug(f"tolist() conversion failed for {type(value)}: {e}")

    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError, AttributeError) as e:
            logger.debug(f"item() conversion failed for {type(value)}: {e}")

    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value

    return str(value)


class MemoryIntegration:
    """记忆系统集成管理器。

    负责把记忆服务接入 Life Engine 拥有的唯一 coherent runtime。
    旧日衰减入口只保留为显式 no-op 兼容边界。
    """

    def __init__(self, service: "LifeEngineService") -> None:
        """初始化记忆集成管理器。

        Args:
            service: LifeEngineService 实例
        """
        self._service = service

    async def init_memory_service(self) -> None:
        """初始化可追溯生命记忆服务。"""
        try:
            from ..memory.service import LifeMemoryService

            cfg = self._service._cfg()
            workspace = Path(cfg.settings.workspace_path)
            index_config = getattr(cfg, "memory_index", None)
            storage_enabled = bool(self._service._selectable_storage_enabled)
            storage_runtime = self._service.storage_runtime if storage_enabled else None
            if storage_enabled and storage_runtime is None:
                raise RuntimeError(
                    "selectable Memory storage requires the Life Engine coherent runtime"
                )
            self._service._memory_service = LifeMemoryService(
                workspace,
                vector_backend_enabled=bool(
                    getattr(index_config, "backend_enabled", True)
                ),
                # The outbox consumer loop is gated on `memory_index.enabled`
                # (see service/core.py). Health must know whether the consumer
                # is expected to run, otherwise a disabled worker with a
                # growing backlog reports as a silent `ok`.
                index_worker_enabled=bool(getattr(index_config, "enabled", True)),
                storage_runtime=storage_runtime,
                selectable_storage_enabled=storage_enabled,
            )
            await self._service._memory_service.initialize()
            logger.info("life_engine 生命记忆服务已初始化")
        except Exception as e:
            logger.error(f"记忆服务初始化失败: {e}", exc_info=True)
            self._service._memory_service = None

    async def maybe_run_daily_decay(self) -> None:
        """Compatibility no-op for the retired score-driven decay loop.

        Recall history and explicit subject interpretation now drive living
        accessibility.  Infrastructure must not delete or weaken a relation
        because a score, age, access count, or emotion field crossed a limit.
        """

        return None
