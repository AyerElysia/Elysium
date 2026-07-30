"""Provider 工厂。

根据配置创建对应的全双工 Provider 实例。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.kernel.logger import get_logger

from .base import BaseRealtimeProvider

if TYPE_CHECKING:
    from ..config import VoiceLiveConfig

logger = get_logger("voice_live.factory", display="Provider Factory")


def create_provider(config: "VoiceLiveConfig") -> BaseRealtimeProvider | None:
    """根据配置创建全双工 Provider。

    Args:
        config: 插件配置

    Returns:
        Provider 实例，如果配置为 disabled 则返回 None
    """
    fd_config = config.full_duplex
    provider_type = fd_config.provider_type

    if provider_type == "disabled":
        logger.info("全双工 Provider 已禁用，将使用降级管线")
        return None

    if not fd_config.upstream_url:
        logger.warning("全双工 Provider 未配置 upstream_url，将使用降级管线")
        return None

    match provider_type:
        case "openai_realtime":
            from .openai_realtime import OpenAIRealtimeProvider

            provider = OpenAIRealtimeProvider(
                upstream_url=fd_config.upstream_url,
                api_key=fd_config.api_key,
                model=fd_config.model_name,
                voice=fd_config.voice,
                connect_timeout=fd_config.connect_timeout,
            )
            logger.info(f"创建 OpenAI Realtime Provider: {fd_config.model_name}")
            return provider

        case "moshi":
            from .moshi import MoshiProvider

            provider = MoshiProvider(
                upstream_url=fd_config.upstream_url,
                connect_timeout=fd_config.connect_timeout,
            )
            logger.info(f"创建 Moshi Provider: {fd_config.upstream_url}")
            return provider

        case _:
            logger.error(f"未知的 Provider 类型: {provider_type}")
            return None
