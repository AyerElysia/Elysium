"""心跳 prompt 注入的统一协议层。"""

from .sections import (
    DEFAULT_HEARTBEAT_SECTIONS,
    HeartbeatSectionProvider,
    SectionContext,
    render_heartbeat_sections,
)

__all__ = [
    "DEFAULT_HEARTBEAT_SECTIONS",
    "HeartbeatSectionProvider",
    "SectionContext",
    "render_heartbeat_sections",
]
