"""TTL 缓存工具

提供带过期时间的内存缓存 + 磁盘持久化，用于群信息、成员信息等。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("napcat_adapter")

# 各类信息的 TTL 缓存过期时间（秒）
GROUP_INFO_TTL = 300
MEMBER_INFO_TTL = 180
STRANGER_INFO_TTL = 300
SELF_INFO_TTL = 300

_CACHE_LOADED = False
_CACHE: dict[str, dict[str, dict[str, Any]]] = {
    "group_info": {},
    "member_info": {},
    "stranger_info": {},
    "self_info": {},
}


async def _ensure_cache_loaded() -> None:
    """确保缓存已从磁盘加载。"""
    global _CACHE_LOADED
    if _CACHE_LOADED:
        return

    from src.kernel.storage import json_store

    try:
        data = await json_store.load("napcat_cache")
    except Exception:
        data = None

    if _CACHE_LOADED:
        return

    if isinstance(data, dict):
        for key, section in _CACHE.items():
            cached_section = data.get(key)
            if isinstance(cached_section, dict):
                section.update(cached_section)

    _CACHE_LOADED = True


async def _save_cache_to_disk() -> None:
    """保存缓存到磁盘。"""
    from src.kernel.storage import json_store

    try:
        await json_store.save("napcat_cache", _CACHE)
    except Exception:
        pass


async def get_cached(section: str, key: str, ttl: int) -> Any | None:
    """获取缓存值，过期则返回 None。"""
    await _ensure_cache_loaded()
    now = time.time()
    entry = _CACHE.get(section, {}).get(key)
    if not entry:
        return None
    ts = entry.get("ts", 0)
    if ts and now - ts <= ttl:
        return entry.get("data")
    _CACHE.get(section, {}).pop(key, None)
    await _save_cache_to_disk()
    return None


async def set_cached(section: str, key: str, data: Any) -> None:
    """设置缓存值。"""
    await _ensure_cache_loaded()
    _CACHE.setdefault(section, {})[key] = {"data": data, "ts": time.time()}
    await _save_cache_to_disk()
