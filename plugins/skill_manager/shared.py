"""SkillManager 组件共享工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def manager_config_value(plugin: Any, name: str, default: Any) -> Any:
    """读取 SkillManager 配置值，兼容测试桩和异常配置。"""
    manager = getattr(getattr(plugin, "config", None), "manager", None)
    return getattr(manager, name, default)


def limit_text(text: str, max_chars: int, label: str) -> str:
    """限制返回给 LLM 或 prompt 的文本长度。"""
    resolved_limit = max(512, int(max_chars))
    if len(text) <= resolved_limit:
        return text
    return text[:resolved_limit].rstrip() + f"\n\n...（{label} 过长，已截断）"


def read_cached_skill_content(plugin: Any, name: str, skill_md_path: Path) -> str:
    """读取并缓存 skill 主文档内容。"""
    content = plugin.skill_contents.get(name)
    if content is None:
        content = skill_md_path.read_text(encoding="utf-8")
        plugin.skill_contents[name] = content
    return content
