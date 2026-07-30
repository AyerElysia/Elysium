"""平台适配层。"""

from .base import BasePlatformAdapter, PlatformEvent
from .factory import create_platform_adapter

__all__ = ["BasePlatformAdapter", "PlatformEvent", "create_platform_adapter"]
