from __future__ import annotations

import os
import re
from pathlib import Path

from src.core.components import BasePlugin, register_plugin
from src.core.prompt import SystemReminderInsertType
from src.kernel.logger import get_logger

from .commands import SkillManagerCommand
from .config import SkillManagerConfig
from .handlers import SkillManagerLoadHandler
from .models import SkillEntry
from .tools import SkillGetReferenceTool, SkillGetScriptTool, SkillGetTool

logger = get_logger("skill_manager")
_FRONT_MATTER_FIELD_RE = re.compile(r"^(name|description)\s*:\s*(.+)$", re.IGNORECASE)
_LIFE_WORKSPACE_MAIN_DOCS = (
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "SUBCONSCIOUS.md",
    "TOOL.md",
)


def _is_path_inside(base_dir: Path, target_path: Path) -> bool:
    """判断目标路径是否在指定根目录内部。"""

    try:
        target_path.resolve().relative_to(base_dir.resolve())
        return True
    except ValueError:
        return False


def _strip_quoted_text(value: str) -> str:
    """去除首尾引号并清理空白。"""

    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1].strip()
    return text


def _parse_skill_front_matter(raw_text: str) -> tuple[str | None, str | None]:
    """从 SKILL.md 首段 front matter 提取 name 和 description。"""

    lines = raw_text.splitlines()
    if len(lines) < 3:
        return None, None
    if lines[0].strip() != "---":
        return None, None

    parsed_name: str | None = None
    parsed_description: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        matched = _FRONT_MATTER_FIELD_RE.match(line.strip())
        if not matched:
            continue
        key, value = matched.groups()
        normalized_value = _strip_quoted_text(value)
        if key.lower() == "name":
            parsed_name = normalized_value
        elif key.lower() == "description":
            parsed_description = normalized_value
    return parsed_name, parsed_description


@register_plugin
class SkillManagerPlugin(BasePlugin):
    """Skill 管理器插件。"""

    plugin_name: str = "skill_manager"
    plugin_description: str = "技能管理器"
    plugin_version: str = "1.0.0"

    configs: list[type] = [SkillManagerConfig]

    def __init__(self, config: SkillManagerConfig | None = None) -> None:
        """初始化 SkillManager 运行态。"""

        super().__init__(config)
        self._workspace_root: Path = Path(__file__).resolve().parents[2]
        self.paths: list[str] = []
        self.skills: dict[str, SkillEntry] = {}
        self.skill_contents: dict[str, str] = {}
        self.injected_skills: set[str] = set()

    def get_components(self) -> list[type]:
        """返回插件组件列表。"""

        if (
            isinstance(self.config, SkillManagerConfig)
            and not self.config.manager.enabled
        ):
            logger.info("skill_manager 已在配置中禁用")
            return []
        SkillManagerCommand._plugin_instance = self
        return [
            SkillManagerLoadHandler,
            SkillManagerCommand,
            SkillGetTool,
            SkillGetReferenceTool,
            SkillGetScriptTool,
        ]

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时清理 system reminder。"""

        from src.core.prompt import get_system_reminder_store

        store = get_system_reminder_store()
        store.delete("actor", "skill_manager_catalog")
        store.delete("sub_actor", "skill_manager_catalog")

    async def refresh_skill_catalog(self) -> None:
        """扫描配置路径并刷新 skill 索引。"""

        configured_paths = self._resolve_skill_paths()
        self.paths = [str(item) for item in configured_paths]
        logger.info(
            "skill_manager 开始刷新 skill 索引: "
            f"paths={', '.join(self.paths) if self.paths else '(none)'}"
        )

        discovered: dict[str, SkillEntry] = {}
        per_path_counts: dict[str, int] = {}
        for base_dir in configured_paths:
            path_key = str(base_dir)
            per_path_counts[path_key] = 0
            if not base_dir.exists() or not base_dir.is_dir():
                logger.warning(f"skill 路径不存在或不可读，已跳过: {base_dir}")
                continue

            for skill_root, skill_md_path in self._iter_skill_roots(base_dir):
                if not skill_md_path.is_file():
                    continue

                text = skill_md_path.read_text(encoding="utf-8")
                parsed_name, parsed_description = _parse_skill_front_matter(text)
                skill_name = (parsed_name or skill_root.name).strip()
                skill_description = (
                    parsed_description
                    or self._build_default_skill_description(skill_name, skill_md_path)
                ).strip()

                markdown_files = [
                    path.relative_to(skill_root).as_posix()
                    for path in sorted(skill_root.rglob("*.md"))
                    if path.is_file()
                ]

                discovered[skill_name] = SkillEntry(
                    name=skill_name,
                    description=skill_description,
                    root_dir=skill_root,
                    skill_md_path=skill_md_path,
                    files=markdown_files,
                )
                per_path_counts[path_key] += 1

        self.apply_discovered_skills(discovered)
        path_summary = ", ".join(
            f"{path}={count}" for path, count in per_path_counts.items()
        )
        if self.skills:
            skill_names = ", ".join(sorted(self.skills.keys(), key=str.lower))
            logger.info(
                "skill_manager 已刷新 skill 索引: "
                f"count={len(self.skills)} paths=({path_summary}) skills=[{skill_names}]"
            )
        else:
            logger.warning(
                "skill_manager 已刷新 skill 索引但未发现任何 skill: "
                f"paths=({path_summary})"
            )

    def _resolve_skill_paths(self) -> list[Path]:
        """将配置中的路径转换为绝对路径列表。"""

        default_paths = ["skill", "skills"]
        if isinstance(self.config, SkillManagerConfig):
            configured = [
                item.strip() for item in self.config.manager.paths if item.strip()
            ]
            paths = configured or default_paths
        else:
            paths = default_paths

        resolved_paths: list[Path] = []
        seen_paths: set[Path] = set()
        for raw_path in paths:
            expanded_path = Path(os.path.expandvars(os.path.expanduser(raw_path)))
            if expanded_path.is_absolute():
                resolved = expanded_path.resolve()
                if resolved not in seen_paths:
                    resolved_paths.append(resolved)
                    seen_paths.add(resolved)
                continue
            resolved = (self._workspace_root / expanded_path).resolve()
            if resolved not in seen_paths:
                resolved_paths.append(resolved)
                seen_paths.add(resolved)
        return resolved_paths

    @classmethod
    def _iter_skill_roots(cls, base_dir: Path) -> list[tuple[Path, Path]]:
        """从基目录中解析 skill 根目录集合。"""

        direct_main_doc = cls._resolve_skill_main_doc(base_dir)
        if direct_main_doc is not None:
            return [(base_dir, direct_main_doc)]

        skill_roots: list[tuple[Path, Path]] = []
        for child in sorted(base_dir.iterdir()):
            if not child.is_dir():
                continue
            main_doc = cls._resolve_skill_main_doc(child)
            if main_doc is not None:
                skill_roots.append((child, main_doc))
        return skill_roots

    @staticmethod
    def _resolve_skill_main_doc(skill_root: Path) -> Path | None:
        """解析 skill 主文档，兼容 life_engine_workspace 这类无 SKILL.md 目录。"""

        skill_md_path = skill_root / "SKILL.md"
        if skill_md_path.is_file():
            return skill_md_path

        for filename in _LIFE_WORKSPACE_MAIN_DOCS:
            candidate = skill_root / filename
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _build_default_skill_description(skill_name: str, main_doc_path: Path) -> str:
        """为没有 front matter 的 skill 生成可读描述。"""

        if main_doc_path.name in _LIFE_WORKSPACE_MAIN_DOCS:
            return (
                f"Life workspace {skill_name}，主文档为 {main_doc_path.name}，"
                "可用 get_reference 继续读取同目录 Markdown。"
            )
        return f"Skill {skill_name}，通过 get_skill 读取后可使用扩展引用与脚本"

    def apply_discovered_skills(self, discovered: dict[str, SkillEntry]) -> None:
        """应用刷新后的 skill 索引并清理运行态缓存。"""

        self.skills = discovered
        valid_names = set(self.skills.keys())
        self.skill_contents = {
            name: content
            for name, content in self.skill_contents.items()
            if name in valid_names
        }
        self.injected_skills = {
            name for name in self.injected_skills if name in valid_names
        }
        self._sync_system_reminder()

    def _resolve_skill_relative_path(
        self,
        *,
        skill_entry: SkillEntry,
        relative_path: str,
        required_suffix: str | tuple[str, ...],
    ) -> tuple[Path | None, str | None]:
        """将 skill 内相对路径解析为受限的绝对路径。"""

        normalized_location = relative_path.strip().replace("\\", "/")
        if not normalized_location:
            return None, "location 不能为空"

        resolved_target = (skill_entry.root_dir / normalized_location).resolve()
        if not _is_path_inside(skill_entry.root_dir, resolved_target):
            return None, f"location 越界: {relative_path}"
        if not resolved_target.is_file():
            return None, f"文件不存在: {relative_path}"
        allowed_suffixes = (
            (required_suffix,)
            if isinstance(required_suffix, str)
            else tuple(required_suffix)
        )
        if resolved_target.suffix.lower() not in allowed_suffixes:
            suffix_display = ", ".join(allowed_suffixes)
            return None, f"仅支持 {suffix_display} 文件: {relative_path}"
        return resolved_target, None

    def _sync_system_reminder(self) -> None:
        """更新 actor/sub_actor 的 skill 清单提示。"""

        from src.core.prompt import get_system_reminder_store

        store = get_system_reminder_store()
        if not self.skills:
            store.delete("actor", "skill_manager_catalog")
            store.delete("sub_actor", "skill_manager_catalog")
            return

        script_hint = "get_script"
        if isinstance(self.config, SkillManagerConfig) and not self.config.manager.allow_script_execution:
            script_hint = "get_script（当前配置已关闭脚本执行）"

        reminder_lines = [
            "## SkillManager 可用技能清单",
            "当任务复杂、上下文长、需要专用流程时，可按需读取 skill。",
            f"先调用 get_skill(name) 注入后，再按需使用 get_reference/{script_hint} 逐步展开。",
            "",
        ]
        for entry in sorted(self.skills.values(), key=lambda item: item.name.lower()):
            reminder_lines.append(f"- {entry.name}: {entry.description}")

        reminder_text = "\n".join(reminder_lines)
        if isinstance(self.config, SkillManagerConfig):
            max_chars = max(512, int(self.config.manager.max_catalog_chars))
            if len(reminder_text) > max_chars:
                reminder_text = reminder_text[:max_chars].rstrip() + "\n...（skill 清单过长，已截断）"

        inject_actor = True
        inject_sub_actor = True
        if isinstance(self.config, SkillManagerConfig):
            inject_actor = self.config.manager.inject_actor_reminder
            inject_sub_actor = self.config.manager.inject_sub_actor_reminder

        if inject_actor:
            store.set(
                "actor",
                name="skill_manager_catalog",
                content=reminder_text,
                insert_type=SystemReminderInsertType.DYNAMIC,
            )
        else:
            store.delete("actor", "skill_manager_catalog")

        if inject_sub_actor:
            store.set(
                "sub_actor",
                name="skill_manager_catalog",
                content=reminder_text,
                insert_type=SystemReminderInsertType.DYNAMIC,
            )
        else:
            store.delete("sub_actor", "skill_manager_catalog")
