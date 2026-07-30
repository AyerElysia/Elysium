"""平台适配器工厂。

根据配置创建对应平台的适配器实例。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import BasePlatformAdapter

if TYPE_CHECKING:
    from ..config import LivestreamConfig


def create_platform_adapter(config: "LivestreamConfig") -> BasePlatformAdapter:
    """根据配置创建平台适配器。

    Args:
        config: 完整的 LivestreamConfig 配置对象。

    Returns:
        对应平台的适配器实例。

    Raises:
        ValueError: 不支持的平台类型。
    """
    platform_cfg = config.platform
    platform_type = platform_cfg.platform_type.lower()

    match platform_type:
        case "bilibili":
            from .bilibili import BilibiliAdapter

            return BilibiliAdapter(
                room_id=platform_cfg.room_id,
                sessdata=platform_cfg.sessdata,
                buvid3=platform_cfg.buvid3,
                reconnect_interval=platform_cfg.reconnect_interval,
            )
        case _:
            raise ValueError(
                f"不支持的直播平台: {platform_type}，"
                f"当前支持: bilibili"
            )
