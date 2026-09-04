"""life_engine 中枢文件内容搜索工具。

参考 Claude Code GrepTool 的设计理念，为数字生命的私人文件系统提供正则搜索能力。
这是在自己的记忆空间中查找具体内容的工具——帮助回忆"我在哪里写过这件事"。
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ._utils import _get_workspace, _resolve_path
from .bounded_projection import project_bounded_items, sha256_json

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


def _split_globs(glob_pattern: str) -> list[str]:
    return [item.strip() for item in str(glob_pattern or "").split(",") if item.strip()]


def _posix_relative(path: Path, workspace: Path) -> str:
    return path.relative_to(workspace).as_posix()


def _glob_hit(rel_posix: str, name: str, pattern: str) -> bool:
    relative = PurePosixPath(rel_posix.replace("\\", "/"))
    basename = PurePosixPath(name)
    if relative.match(pattern) or basename.match(pattern):
        return True
    prefix = pattern.rstrip("/*")
    if prefix and (rel_posix == prefix or rel_posix.startswith(prefix + "/")):
        return True
    return False


def _matches_glob(path: Path, glob_pattern: str, workspace: Path) -> bool:
    """Include filter: workspace-relative path, basename, or directory prefix."""
    patterns = _split_globs(glob_pattern)
    if not patterns:
        return True
    try:
        rel_posix = _posix_relative(path, workspace)
    except ValueError:
        return False
    return any(_glob_hit(rel_posix, path.name, pattern) for pattern in patterns)


def _excluded_by_glob(path: Path, exclude_glob: str, workspace: Path) -> bool:
    patterns = _split_globs(exclude_glob)
    if not patterns:
        return False
    try:
        rel_posix = _posix_relative(path, workspace)
    except ValueError:
        return True
    return any(_glob_hit(rel_posix, path.name, pattern) for pattern in patterns)


def _parse_time_bound(value: str, *, as_end: bool = False) -> datetime | None:
    """Parse YYYY-MM-DD or ISO datetime. Date-only is local midnight; as_end is next midnight."""
    text = str(value or "").strip()
    if not text:
        return None
    relative = re.fullmatch(r"(\d+)\s*([dDhH])", text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        delta = timedelta(days=amount) if unit == "d" else timedelta(hours=amount)
        return datetime.now().astimezone() - delta
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        local = datetime.now().astimezone().tzinfo or timezone.utc
        start = datetime.fromisoformat(text).replace(tzinfo=local)
        if as_end:
            return start + timedelta(days=1)
        return start
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
    return parsed


def _mtime_in_range(stat_mtime: float, after: datetime | None, before: datetime | None) -> bool:
    when = datetime.fromtimestamp(stat_mtime, tz=timezone.utc).astimezone()
    if after is not None and when < after:
        return False
    if before is not None and when >= before:
        return False
    return True


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
        "- ✓ 按时间/目录收窄：modified_after、modified_before、max_depth、exclude_glob\n"
        "\n"
        "**常用收窄：**\n"
        "- `path=diaries` + `max_depth=1`：只看手写日记根目录，不钻进 witness 回望\n"
        "- `exclude_glob=diaries/witness`：整棵见证投影树都跳过\n"
        "- `modified_after=2026-08-01`、`modified_before=2026-09-01`：按修改时间（也可用 `7d`/`24h`）\n"
        "- `glob=2026-08*.md`：按文件名；`fixed_string=true`：把 pattern 当普通文字，不要当正则\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 已经知道文件路径，只想读取内容 → 用 nucleus_read_file"
        "（默认一页 80 行；日记用 from_end=true；全文才 limit=0）\n"
        "- ✗ 想看目录结构或最近有哪些文件 → 用 nucleus_list_files"
        "（默认最近修改在前）。不要用 pattern=\".\" 当列目录。\n"
        "- ✗ 想按语义搜索记忆 → 用 nucleus_search_memory\n"
        "\n"
        "**输出模式：**\n"
        "- `files_with_matches`（默认）：只返回匹配的文件列表；max_results 计文件数；默认按修改时间新→旧\n"
        "- `content`：返回匹配行和上下文；max_results 计匹配行数；默认按路径名\n"
        "\n"
        "搜索因上限提前停止时，看 search_truncated / candidate_files / files_returned，"
        "那不是「目录里只有这些文件」。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        pattern: Annotated[str, "搜索模式（默认正则；fixed_string=true 时当普通文字）"],
        path: Annotated[str, "搜索路径（workspace 相对路径，空字符串搜索整个空间）"] = "",
        glob: Annotated[str, "只包含这些文件（如 '*.md', '2026-08*.md'），逗号分隔"] = "",
        exclude_glob: Annotated[
            str,
            "排除这些路径（目录前缀也可，如 diaries/witness），逗号分隔",
        ] = "",
        max_depth: Annotated[
            int,
            "相对搜索根的最大目录深度；0=不限制。1=只搜这一层，不进子目录",
        ] = 0,
        modified_after: Annotated[
            str,
            "只搜此时间之后改过的文件。YYYY-MM-DD、ISO，或相对时间如 7d / 24h",
        ] = "",
        modified_before: Annotated[
            str,
            "只搜此时间之前改过的文件。日期-only 表示当天 0 点之前（不含当天）",
        ] = "",
        fixed_string: Annotated[bool, "true 时按字面匹配，不把 pattern 当正则"] = False,
        sort: Annotated[
            Literal["", "mtime", "name"],
            "mtime=新→旧；name=路径名。默认 files_with_matches 用 mtime，content 用 name",
        ] = "",
        output_mode: Annotated[
            Literal["content", "files_with_matches"],
            "输出模式：'files_with_matches'返回文件列表，'content'返回匹配行内容",
        ] = "files_with_matches",
        case_insensitive: Annotated[bool, "是否忽略大小写"] = True,
        context_lines: Annotated[int, "匹配行前后显示几行上下文（仅 content 模式有效）"] = 0,
        max_results: Annotated[int, "最大结果数量"] = _DEFAULT_MAX_RESULTS,
        continuation: Annotated[
            str,
            "Optional continuation returned by the previous file grep page",
        ] = "",
        max_bytes: Annotated[
            int | None,
            "Optional result byte budget; the task hard cap still applies",
        ] = None,
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
        expression = re.escape(pattern) if fixed_string else pattern
        try:
            compiled = re.compile(expression, flags)
        except re.error as e:
            return False, f"正则表达式语法错误: {e}"

        # 收集要搜索的文件
        if search_root.is_file():
            files_to_search = [search_root]
        else:
            files_to_search = []
            for root, dirs, filenames in os.walk(search_root):
                dirs[:] = [
                    name
                    for name in dirs
                    if name not in _IGNORE_DIRS and not name.startswith(".")
                ]
                root_path = Path(root)
                if exclude_glob:
                    dirs[:] = [
                        name
                        for name in dirs
                        if not _excluded_by_glob(
                            root_path / name, exclude_glob, workspace
                        )
                    ]
                if max_depth > 0:
                    relative_root = root_path.relative_to(search_root)
                    depth = 0 if str(relative_root) == "." else len(relative_root.parts)
                    if depth >= max_depth - 1:
                        dirs[:] = []
                for fname in filenames:
                    fpath = root_path / fname
                    if _should_skip_path(fpath):
                        continue
                    if exclude_glob and _excluded_by_glob(
                        fpath, exclude_glob, workspace
                    ):
                        continue
                    if glob and not _matches_glob(fpath, glob, workspace):
                        continue
                    files_to_search.append(fpath)

        try:
            after_bound = _parse_time_bound(modified_after)
            before_bound = _parse_time_bound(modified_before)
        except ValueError as exc:
            return False, f"时间格式无法解析: {exc}"

        if after_bound is not None or before_bound is not None:
            files_to_search = [
                fpath
                for fpath in files_to_search
                if _mtime_in_range(fpath.stat().st_mtime, after_bound, before_bound)
            ]

        limit_unit = "file" if output_mode == "files_with_matches" else "line"
        sort_mode = str(sort or "").strip()
        if not sort_mode:
            sort_mode = "mtime" if output_mode == "files_with_matches" else "name"
        if sort_mode not in {"mtime", "name"}:
            return False, f"不支持的 sort: {sort}"
        if sort_mode == "mtime":
            files_to_search.sort(key=lambda item: str(item))
            files_to_search.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
        else:
            files_to_search = sorted(files_to_search)
        candidate_files = len(files_to_search)

        # 执行搜索
        matched_files: list[dict[str, Any]] = []
        source_files: list[dict[str, Any]] = []
        total_match_count = 0
        search_truncated = False

        for fpath in files_to_search:
            if output_mode == "files_with_matches":
                if len(matched_files) >= max_results:
                    search_truncated = True
                    break
            elif total_match_count >= max_results:
                search_truncated = True
                break

            stat_before = fpath.stat()
            file_matches = _grep_file(
                fpath,
                compiled,
                context_lines=context_lines if output_mode == "content" else 0,
            )

            if not file_matches:
                continue

            rel_path = str(fpath.relative_to(workspace))
            raw_bytes = fpath.read_bytes()
            stat_after = fpath.stat()
            if (
                stat_before.st_size != stat_after.st_size
                or stat_before.st_mtime_ns != stat_after.st_mtime_ns
            ):
                return False, "file changed while grep results were prepared"
            source_files.append(
                {
                    "path": rel_path,
                    "bytes": len(raw_bytes),
                    "content_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                }
            )
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

        census = {
            "candidate_files": candidate_files,
            "files_returned": len(matched_files),
            "search_truncated": search_truncated,
            "limit_unit": limit_unit,
            "sort": sort_mode,
            "max_depth": int(max_depth or 0),
        }
        if search_truncated:
            census["note"] = (
                f"搜索在上限处停止：已返回 {len(matched_files)} 个文件 / "
                f"候选 {candidate_files} 个。这不是目录里只有这些文件。"
            )

        if not matched_files:
            result: dict[str, Any] = {
                "action": "grep_file",
                "pattern": pattern,
                "output_mode": output_mode,
                "total_files": 0,
                "total_matches": 0,
                "message": "没有找到匹配的内容",
                **census,
            }
        else:
            result = {
                "action": "grep_file",
                "pattern": pattern,
                "output_mode": output_mode,
                "search_path": path or "(整个工作空间)",
                "total_files": len(matched_files),
                "total_matches": total_match_count,
                "results": matched_files,
                **census,
            }

        source_items = list(result.get("results") or [])
        file_hash_by_path = {
            str(item["path"]): str(item["content_sha256"])
            for item in source_files
        }
        item_refs = []
        for item in source_items:
            rel_path = str(item.get("path") or "")
            item_hash = sha256_json(item)
            content_hash = file_hash_by_path.get(rel_path, item_hash)
            item_refs.append(
                f"workspace-file:{rel_path}:sha256:{content_hash}:grep:{item_hash}"
            )
        try:
            projected = project_bounded_items(
                projection_name="workspace-file-grep",
                task_name=getattr(self, "_runtime_task_name", ""),
                requested_max_bytes=max_bytes,
                binding={
                    "pattern": str(pattern),
                    "path": str(path),
                    "glob": str(glob),
                    "exclude_glob": str(exclude_glob),
                    "max_depth": int(max_depth),
                    "modified_after": str(modified_after),
                    "modified_before": str(modified_before),
                    "fixed_string": bool(fixed_string),
                    "sort": str(sort_mode),
                    "output_mode": str(output_mode),
                    "case_insensitive": bool(case_insensitive),
                    "context_lines": int(context_lines),
                    "max_results": int(max_results),
                },
                frontier={
                    "files": source_files,
                    "results_sha256": sha256_json(source_items),
                },
                base_payload={
                    key: value
                    for key, value in result.items()
                    if key != "results"
                },
                items_key="results",
                items=source_items,
                item_refs=item_refs,
                continuation=continuation,
            )
        except ValueError as exc:
            return False, str(exc)
        if len(str(projected).encode("utf-8")) > projected["budget_bytes"]:
            return False, "file grep projection exceeded its byte budget"
        return True, projected


# 导出
GREP_TOOLS = [
    LifeEngineGrepFileTool,
]
