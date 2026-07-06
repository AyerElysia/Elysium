"""Life engine skill authoring tools."""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from src.app.plugin_system.base import BaseTool
from src.core.managers import get_plugin_manager
from src.kernel.logger import get_logger

from ._utils import _get_workspace

logger = get_logger("life_engine.skill_tools")

SkillAction = Literal["draft", "revise", "publish", "archive", "list", "validate"]
SkillLocation = Literal["draft", "published"]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")
_FRONTMATTER_RE = re.compile(r"^---\n(?P<meta>.*?)\n---\n(?P<body>.*)$", re.DOTALL)
_FIELD_RE = re.compile(r"^(?P<key>name|description)\s*:\s*(?P<value>.*)$", re.IGNORECASE)
_MAX_DESCRIPTION_CHARS = 1024
_MAX_BODY_CHARS = 20000


def _root_dirs(workspace: Path) -> dict[str, Path]:
    return {
        "published": workspace / "skills",
        "draft": workspace / "skill_drafts",
        "archive": workspace / "skill_archive",
    }


def _normalize_name(name: str) -> str:
    text = (name or "").strip().lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:64].strip("-")


def _is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name or ""))


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _escape_frontmatter_value(value: str) -> str:
    value = " ".join((value or "").split())
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


def _skill_md(name: str, description: str, body: str) -> str:
    body = (body or "").strip()
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {_escape_frontmatter_value(description)}\n"
        "---\n\n"
        f"{body}\n"
    )


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str, list[str]]:
    match = _FRONTMATTER_RE.match(content.replace("\r\n", "\n"))
    if not match:
        return {}, content, ["缺少 YAML frontmatter，文件必须以 --- 开头并包含 name/description"]

    metadata: dict[str, str] = {}
    warnings: list[str] = []
    for line in match.group("meta").splitlines():
        matched = _FIELD_RE.match(line.strip())
        if not matched:
            continue
        metadata[matched.group("key").lower()] = _strip_quotes(matched.group("value"))

    body = match.group("body").strip()
    if not metadata.get("name"):
        warnings.append("frontmatter 缺少 name")
    if not metadata.get("description"):
        warnings.append("frontmatter 缺少 description")
    return metadata, body, warnings


def _validate_skill_content(content: str, expected_name: str | None = None) -> tuple[bool, list[str], dict[str, Any]]:
    metadata, body, warnings = _parse_frontmatter(content)
    name = metadata.get("name", "")
    description = metadata.get("description", "")

    if name and not _is_valid_name(name):
        warnings.append("name 只能包含小写字母、数字和连字符，长度不超过 64")
    if expected_name and name and name != expected_name:
        warnings.append(f"name 与目标目录不一致: frontmatter={name} target={expected_name}")
    if len(description) > _MAX_DESCRIPTION_CHARS:
        warnings.append(f"description 过长，最多 {_MAX_DESCRIPTION_CHARS} 字符")
    if not body:
        warnings.append("SKILL.md 正文不能为空")
    if len(body) > _MAX_BODY_CHARS:
        warnings.append(f"正文过长，最多 {_MAX_BODY_CHARS} 字符")

    lower_body = body.lower()
    if any(marker in lower_body for marker in ("```sh", "```bash", "subprocess", "shell_command", "nucleus_bash")):
        warnings.append("当前工具只允许发布 instruction-only skill；涉及 shell/script 的内容需要人工确认")

    return not warnings, warnings, {
        "name": name,
        "description": description,
        "body_chars": len(body),
    }


def _safe_skill_path(root: Path, name: str) -> Path:
    target = (root / name / "SKILL.md").resolve()
    target.relative_to(root.resolve())
    return target


def _location_key(location: str) -> str | None:
    value = str(location or "").strip().lower()
    if value in {"draft", "published"}:
        return value
    return None


def _read_skill(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_skill(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _refresh_skill_manager() -> dict[str, Any]:
    try:
        plugin = get_plugin_manager().get_plugin("skill_manager")
        if plugin is None or not hasattr(plugin, "refresh_skill_catalog"):
            return {"refreshed": False, "reason": "skill_manager 未加载"}
        await plugin.refresh_skill_catalog()
        return {
            "refreshed": True,
            "indexed_count": len(getattr(plugin, "skills", {}) or {}),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"刷新 skill_manager 索引失败: {exc}")
        return {"refreshed": False, "reason": str(exc)}


class LifeEngineManageSkillTool(BaseTool):
    """Manage life_engine-authored skills."""

    tool_name: str = "nucleus_manage_skill"
    tool_description: str = (
        "管理爱莉自己沉淀的 instruction-only skill。"
        "支持 draft/revise/publish/archive/list/validate。"
        "skill 是工作方式沉淀，不是后台自动化脚本；包含脚本、shell、外部 API 的 skill 不能静默发布。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        action: Annotated[SkillAction, "操作：draft / revise / publish / archive / list / validate"],
        name: Annotated[str, "skill 名称，使用小写字母、数字和连字符"] = "",
        description: Annotated[str, "skill 触发描述；draft/revise 时可填写"] = "",
        body: Annotated[str, "SKILL.md 正文；draft/revise 时可填写"] = "",
        location: Annotated[SkillLocation, "目标位置：draft / published"] = "draft",
        replace_existing: Annotated[bool, "发布或草稿已存在时是否覆盖"] = False,
        reason: Annotated[str, "这次管理 skill 的原因，便于未来追溯"] = "",
    ) -> tuple[bool, str | dict[str, Any]]:
        workspace = _get_workspace(self.plugin)
        roots = _root_dirs(workspace)
        for root in roots.values():
            root.mkdir(parents=True, exist_ok=True)

        action_value = str(action or "").strip().lower()
        if action_value == "list":
            return True, self._list_skills(roots)

        normalized = _normalize_name(name)
        if not normalized or not _is_valid_name(normalized):
            return False, "name 无效：请使用小写字母、数字和连字符，长度不超过 64"

        if action_value == "draft":
            return await self._draft(roots, normalized, description, body, replace_existing, reason)
        if action_value == "revise":
            return await self._revise(roots, normalized, location, description, body, reason)
        if action_value == "publish":
            return await self._publish(roots, normalized, replace_existing, reason)
        if action_value == "archive":
            return await self._archive(roots, normalized, location, reason)
        if action_value == "validate":
            return self._validate(roots, normalized, location)

        return False, "action 只能是 draft / revise / publish / archive / list / validate"

    def _list_skills(self, roots: dict[str, Path]) -> dict[str, Any]:
        def collect(root: Path) -> list[dict[str, str]]:
            items: list[dict[str, str]] = []
            for path in sorted(root.glob("*/SKILL.md")):
                try:
                    metadata, _, _ = _parse_frontmatter(_read_skill(path))
                except Exception:
                    metadata = {}
                items.append({
                    "name": path.parent.name,
                    "description": metadata.get("description", ""),
                    "path": str(path),
                })
            return items

        archives = [
            {"name": path.parent.name, "path": str(path)}
            for path in sorted(roots["archive"].glob("*/SKILL.md"))
        ]
        return {
            "drafts": collect(roots["draft"]),
            "published": collect(roots["published"]),
            "archived": archives,
        }

    async def _draft(
        self,
        roots: dict[str, Path],
        name: str,
        description: str,
        body: str,
        replace_existing: bool,
        reason: str,
    ) -> tuple[bool, str | dict[str, Any]]:
        if not description.strip():
            return False, "draft 需要 description"
        if not body.strip():
            return False, "draft 需要 body"
        path = _safe_skill_path(roots["draft"], name)
        if path.exists() and not replace_existing:
            return False, f"草稿已存在: {name}；如需覆盖请设置 replace_existing=true"
        content = _skill_md(name, description, body)
        ok, warnings, summary = _validate_skill_content(content, expected_name=name)
        if not ok:
            return False, {"valid": False, "warnings": warnings, "summary": summary}
        _write_skill(path, content)
        return True, {
            "action": "draft",
            "name": name,
            "path": str(path),
            "reason": reason,
            "valid": True,
        }

    async def _revise(
        self,
        roots: dict[str, Path],
        name: str,
        location: str,
        description: str,
        body: str,
        reason: str,
    ) -> tuple[bool, str | dict[str, Any]]:
        root_key = _location_key(location)
        if root_key is None:
            return False, "location 只能是 draft / published"
        path = _safe_skill_path(roots[root_key], name)
        if not path.exists():
            return False, f"{location} skill 不存在: {name}"

        existing = _read_skill(path)
        metadata, existing_body, warnings = _parse_frontmatter(existing)
        if warnings:
            return False, {"valid": False, "warnings": warnings}
        next_description = description.strip() or metadata.get("description", "")
        next_body = body.strip() or existing_body
        content = _skill_md(name, next_description, next_body)
        ok, validate_warnings, summary = _validate_skill_content(content, expected_name=name)
        if not ok:
            return False, {"valid": False, "warnings": validate_warnings, "summary": summary}
        _write_skill(path, content)
        refresh = await _refresh_skill_manager() if root_key == "published" else {"refreshed": False}
        return True, {
            "action": "revise",
            "location": root_key,
            "name": name,
            "path": str(path),
            "reason": reason,
            "valid": True,
            "refresh": refresh,
        }

    async def _publish(
        self,
        roots: dict[str, Path],
        name: str,
        replace_existing: bool,
        reason: str,
    ) -> tuple[bool, str | dict[str, Any]]:
        draft_path = _safe_skill_path(roots["draft"], name)
        if not draft_path.exists():
            return False, f"草稿不存在: {name}"
        content = _read_skill(draft_path)
        ok, warnings, summary = _validate_skill_content(content, expected_name=name)
        if not ok:
            return False, {"valid": False, "warnings": warnings, "summary": summary}

        published_path = _safe_skill_path(roots["published"], name)
        if published_path.exists() and not replace_existing:
            return False, f"已发布 skill 存在: {name}；如需覆盖请设置 replace_existing=true"
        _write_skill(published_path, content)
        refresh = await _refresh_skill_manager()
        return True, {
            "action": "publish",
            "name": name,
            "path": str(published_path),
            "reason": reason,
            "valid": True,
            "refresh": refresh,
        }

    async def _archive(
        self,
        roots: dict[str, Path],
        name: str,
        location: str,
        reason: str,
    ) -> tuple[bool, str | dict[str, Any]]:
        root_key = _location_key(location)
        if root_key is None:
            return False, "location 只能是 draft / published"
        source_dir = roots[root_key] / name
        if not source_dir.exists():
            return False, f"{location} skill 不存在: {name}"

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_dir = roots["archive"] / f"{stamp}_{root_key}_{name}"
        shutil.move(str(source_dir), str(dest_dir))
        refresh = await _refresh_skill_manager() if root_key == "published" else {"refreshed": False}
        return True, {
            "action": "archive",
            "location": root_key,
            "name": name,
            "archived_path": str(dest_dir / "SKILL.md"),
            "reason": reason,
            "refresh": refresh,
        }

    def _validate(
        self,
        roots: dict[str, Path],
        name: str,
        location: str,
    ) -> tuple[bool, str | dict[str, Any]]:
        root_key = _location_key(location)
        if root_key is None:
            return False, "location 只能是 draft / published"
        path = _safe_skill_path(roots[root_key], name)
        if not path.exists():
            return False, f"{location} skill 不存在: {name}"
        ok, warnings, summary = _validate_skill_content(_read_skill(path), expected_name=name)
        return True, {
            "action": "validate",
            "location": root_key,
            "name": name,
            "path": str(path),
            "valid": ok,
            "warnings": warnings,
            "summary": summary,
        }


SKILL_TOOLS = [LifeEngineManageSkillTool]

__all__ = [
    "LifeEngineManageSkillTool",
    "SKILL_TOOLS",
]
