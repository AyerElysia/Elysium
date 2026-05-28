"""life_engine 中枢文件内容搜索工具。

参考 Claude Code GrepTool 的设计理念，为数字生命的私人文件系统提供正则搜索能力。
这是在自己的记忆空间中查找具体内容的工具——帮助回忆"我在哪里写过这件事"。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ._utils import _get_workspace, _resolve_path

logger = log_api.get_logger("life_engine.grep")

# 搜索结果上限，防止过大的匹配结果淹没上下文
_DEFAULT_MAX_RESULTS = 50
# 忽略的目录和文件模式
_IGNORE_DIRS = {".memory", "__pycache__", ".git", ".svn", "node_modules"}
_IGNORE_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".pyc", ".pyo", ".tmp"}
# 单文件最大扫描字节（跳过过大的二进制文件）
_MAX_FILE_SIZE = 1024 * 1024  # 1MB


def _should_skip_path(path: Path) -> bool:
    """判断是否应跳过此路径。"""
    # 跳过隐藏目录和忽略目录
    for part in path.parts:
        if part in _IGNORE_DIRS:
            return True
    # 跳过隐藏文件（以 . 开头的文件名）
    if path.name.startswith("."):
        return True
    # 跳过特定扩展名
    if path.suffix.lower() in _IGNORE_EXTENSIONS:
        return True
    return False


def _matches_glob(path: Path, glob_pattern: str, workspace: Path) -> bool:
    """检查路径是否匹配 glob 模式。"""
    if not glob_pattern:
        return True
    try:
        rel_path = path.relative_to(workspace)
        # 支持多个 glob 模式（逗号分隔）
        patterns = [p.strip() for p in glob_pattern.split(",") if p.strip()]
        for pattern in patterns:
            if rel_path.match(pattern):
                return True
        return False
    except ValueError:
        return False


def _grep_file(
    file_path: Path,
    pattern: re.Pattern,
    *,
    context_lines: int = 0,
    max_line_length: int = 500,
) -> list[dict[str, Any]]:
    """在单个文件中搜索匹配行。"""
    matches: list[dict[str, Any]] = []

    try:
        # 检查文件大小
        if file_path.stat().st_size > _MAX_FILE_SIZE:
            return []

        content = file_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()

        for line_num, line in enumerate(lines, start=1):
            if pattern.search(line):
                match_entry: dict[str, Any] = {
                    "line": line_num,
                    "content": line[:max_line_length],
                }

                # 添加上下文行
                if context_lines > 0:
                    ctx_before = []
                    ctx_after = []
                    for offset in range(1, context_lines + 1):
                        before_idx = line_num - 1 - offset
                        after_idx = line_num - 1 + offset
                        if 0 <= before_idx < len(lines):
                            ctx_before.insert(0, f"{before_idx + 1}: {lines[before_idx][:max_line_length]}")
                        if 0 <= after_idx < len(lines):
                            ctx_after.append(f"{after_idx + 1}: {lines[after_idx][:max_line_length]}")
                    if ctx_before:
                        match_entry["context_before"] = ctx_before
                    if ctx_after:
                        match_entry["context_after"] = ctx_after

                matches.append(match_entry)

    except (UnicodeDecodeError, PermissionError, OSError):
        pass

    return matches


class LifeEngineGrepFileTool(BaseTool):
    """在私人文件系统中搜索内容的工具。"""

    tool_name: str = "nucleus_grep_file"
    tool_description: str = (
        "在你的私人文件系统中搜索内容——帮你回忆「我在哪里写过这件事」。"
        "\n\n"
        "**何时使用：**\n"
        "- ✓ 想找回「我之前在哪篇日记里提到过音乐」\n"
        "- ✓ 搜索某个关键词在所有笔记中出现的位置\n"
        "- ✓ 在动手编辑前，先确认文件中某段内容的确切位置\n"
        "- ✓ 跨多个文件查找某个话题的讨论\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 已经知道文件路径，只想读取内容 → 用 nucleus_read_file\n"
        "- ✗ 想看目录结构 → 用 nucleus_list_files\n"
        "- ✗ 想按语义搜索记忆 → 用 nucleus_search_memory\n"
        "\n"
        "**输出模式：**\n"
        "- `files_with_matches`（默认）：只返回匹配的文件列表，快速定位\n"
        "- `content`：返回匹配行和上下文，深入查看内容\n"
        "\n"
        "**模式语法：** 支持正则表达式（如 `日记.*音乐`、`\\d{4}-\\d{2}-\\d{2}`）"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        pattern: Annotated[str, "搜索模式（支持正则表达式，如 '音乐' 或 '日记.*感悟'）"],
        path: Annotated[str, "搜索路径（workspace 相对路径，空字符串搜索整个空间）"] = "",
        glob: Annotated[str, "文件通配符过滤（如 '*.md', 'diaries/*'），逗号分隔多个模式"] = "",
        output_mode: Annotated[
            Literal["content", "files_with_matches"],
            "输出模式：'files_with_matches'返回文件列表，'content'返回匹配行内容",
        ] = "files_with_matches",
        case_insensitive: Annotated[bool, "是否忽略大小写"] = True,
        context_lines: Annotated[int, "匹配行前后显示几行上下文（仅 content 模式有效）"] = 0,
        max_results: Annotated[int, "最大结果数量"] = _DEFAULT_MAX_RESULTS,
    ) -> tuple[bool, str | dict]:
        """在 workspace 文件中搜索匹配内容。

        Returns:
            成功返回 (True, {...})
            失败返回 (False, error_message)
        """
        if not pattern.strip():
            return False, "搜索模式不能为空"

        workspace = _get_workspace(self.plugin)

        # 确定搜索根路径
        if path.strip():
            ok, resolved_path = _resolve_path(self.plugin, path)
            if not ok:
                return False, str(resolved_path)
            search_root = resolved_path
            if not search_root.exists():
                return False, f"路径不存在: {path}"
        else:
            search_root = workspace

        # 编译正则表达式
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return False, f"正则表达式语法错误: {e}"

        # 收集要搜索的文件
        if search_root.is_file():
            files_to_search = [search_root]
        else:
            files_to_search = []
            for root, dirs, filenames in os.walk(search_root):
                # 过滤忽略的目录
                dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not d.startswith(".")]
                for fname in filenames:
                    fpath = Path(root) / fname
                    if _should_skip_path(fpath):
                        continue
                    if glob and not _matches_glob(fpath, glob, workspace):
                        continue
                    files_to_search.append(fpath)

        # 执行搜索
        matched_files: list[dict[str, Any]] = []
        total_match_count = 0

        for fpath in sorted(files_to_search):
            if total_match_count >= max_results:
                break

            file_matches = _grep_file(
                fpath,
                compiled,
                context_lines=context_lines if output_mode == "content" else 0,
            )

            if not file_matches:
                continue

            rel_path = str(fpath.relative_to(workspace))
            total_match_count += len(file_matches)

            if output_mode == "files_with_matches":
                matched_files.append({
                    "path": rel_path,
                    "match_count": len(file_matches),
                })
            else:
                # content mode: 包含匹配行内容
                matched_files.append({
                    "path": rel_path,
                    "match_count": len(file_matches),
                    "matches": file_matches[:max_results - (total_match_count - len(file_matches))],
                })

        if not matched_files:
            return True, {
                "action": "grep_file",
                "pattern": pattern,
                "output_mode": output_mode,
                "total_files": 0,
                "total_matches": 0,
                "message": "没有找到匹配的内容",
            }

        result: dict[str, Any] = {
            "action": "grep_file",
            "pattern": pattern,
            "output_mode": output_mode,
            "search_path": path or "(整个工作空间)",
            "total_files": len(matched_files),
            "total_matches": total_match_count,
            "results": matched_files,
        }

        if total_match_count >= max_results:
            result["truncated"] = True
            result["note"] = f"结果已截断，显示前 {max_results} 条匹配。可缩小搜索范围或使用 glob 过滤。"

        return True, result


# 导出
GREP_TOOLS = [
    LifeEngineGrepFileTool,
]
