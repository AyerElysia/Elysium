"""Life Trace query tools."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from src.app.plugin_system.base import BaseTool

from ..core.config import LifeEngineConfig
from .store import LifeTraceRecord, LifeTraceStore


_TRACE_CHATTER_ALLOW = ["life_engine_internal", "life_chatter", "default_chatter"]


def _store(plugin: Any) -> LifeTraceStore:
    return LifeTraceStore(_get_workspace(plugin))


def _get_workspace(plugin: Any) -> Path:
    config = getattr(plugin, "config", None)
    if isinstance(config, LifeEngineConfig):
        workspace = config.settings.workspace_path
    else:
        workspace = str(Path(__file__).parent.parent.parent.parent / "data" / "life_engine_workspace")
    path = Path(workspace).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_summary(record: LifeTraceRecord) -> dict[str, Any]:
    return {
        "trace_id": record.trace_id,
        "timestamp": record.timestamp,
        "path": record.path,
        "operation": record.operation,
        "tool_name": record.tool_name,
        "actor": record.actor,
        "reason": record.reason,
        "before_hash": record.before_hash[:12] if record.before_hash else "",
        "after_hash": record.after_hash[:12] if record.after_hash else "",
        "before_size": record.before_size,
        "after_size": record.after_size,
    }


class LifeTraceRecentChangesTool(BaseTool):
    """查看最近文件追溯记录。"""

    tool_name: str = "nucleus_trace_recent_changes"
    tool_description: str = (
        "查看私人工作空间最近的文件修改追溯。"
        "用于回答“最近改过什么”“SOUL/TOOLS/MEMORY 是什么时候变的”等问题。"
    )
    chatter_allow: list[str] = _TRACE_CHATTER_ALLOW

    async def execute(
        self,
        limit: Annotated[int, "最多返回多少条，默认 10"] = 10,
        path: Annotated[str, "可选：只看某个 workspace 相对路径"] = "",
    ) -> tuple[bool, str | dict]:
        try:
            records = _store(self.plugin).recent(
                limit=max(1, min(int(limit or 10), 50)),
                path=path,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"读取追溯记录失败: {exc}"
        return True, {
            "action": "trace_recent_changes",
            "count": len(records),
            "records": [_record_summary(record) for record in records],
        }


class LifeTraceFileHistoryTool(BaseTool):
    """查看单个文件的追溯历史。"""

    tool_name: str = "nucleus_trace_file_history"
    tool_description: str = (
        "查看某个文件的修改历史。"
        "适合追问“这个文件为什么变成现在这样”“上次改 SOUL.md 是什么时候”。"
    )
    chatter_allow: list[str] = _TRACE_CHATTER_ALLOW

    async def execute(
        self,
        path: Annotated[str, "workspace 相对路径，例如 SOUL.md / MEMORY.md / notes/a.md"],
        limit: Annotated[int, "最多返回多少条，默认 20"] = 20,
    ) -> tuple[bool, str | dict]:
        try:
            records = _store(self.plugin).history(
                path,
                limit=max(1, min(int(limit or 20), 100)),
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"读取文件追溯历史失败: {exc}"
        return True, {
            "action": "trace_file_history",
            "path": path,
            "count": len(records),
            "records": [_record_summary(record) for record in records],
        }


class LifeTraceShowDiffTool(BaseTool):
    """查看某次追溯记录的 diff。"""

    tool_name: str = "nucleus_trace_show_diff"
    tool_description: str = (
        "查看某次文件修改的具体 diff。"
        "先用 nucleus_trace_recent_changes 或 nucleus_trace_file_history 找 trace_id。"
    )
    chatter_allow: list[str] = _TRACE_CHATTER_ALLOW

    async def execute(
        self,
        trace_id: Annotated[str, "追溯 ID，支持前缀匹配"],
        max_chars: Annotated[int, "diff 最大返回字符数，默认 8000"] = 8000,
    ) -> tuple[bool, str | dict]:
        store = _store(self.plugin)
        record, diff_text = store.read_diff(trace_id)
        if record is None:
            return False, f"找不到追溯记录: {trace_id}"
        max_len = max(500, min(int(max_chars or 8000), 30000))
        truncated = len(diff_text) > max_len
        if truncated:
            diff_text = diff_text[: max_len - 1].rstrip() + "…"
        return True, {
            "action": "trace_show_diff",
            "record": _record_summary(record),
            "diff": diff_text,
            "truncated": truncated,
        }


class LifeTracePreviewVersionTool(BaseTool):
    """预览某次修改前/后的完整文件内容。"""

    tool_name: str = "nucleus_trace_preview_version"
    tool_description: str = (
        "预览某次追溯记录对应的修改前或修改后内容。"
        "这是只读预览，不会回滚文件。"
    )
    chatter_allow: list[str] = _TRACE_CHATTER_ALLOW

    async def execute(
        self,
        trace_id: Annotated[str, "追溯 ID，支持前缀匹配"],
        side: Annotated[str, "before 或 after，默认 before"] = "before",
        max_chars: Annotated[int, "内容最大返回字符数，默认 12000"] = 12000,
    ) -> tuple[bool, str | dict]:
        store = _store(self.plugin)
        record = store.get(trace_id)
        if record is None:
            return False, f"找不到追溯记录: {trace_id}"
        normalized_side = str(side or "before").strip().lower()
        if normalized_side not in {"before", "after"}:
            return False, "side 仅支持 before/after"
        digest = record.before_hash if normalized_side == "before" else record.after_hash
        content = store.read_blob(digest)
        if content is None:
            return False, f"该记录没有 {normalized_side} 版本内容"
        max_len = max(500, min(int(max_chars or 12000), 50000))
        truncated = len(content) > max_len
        if truncated:
            content = content[: max_len - 1].rstrip() + "…"
        return True, {
            "action": "trace_preview_version",
            "side": normalized_side,
            "record": _record_summary(record),
            "content": content,
            "truncated": truncated,
        }


LIFE_TRACE_TOOLS = [
    LifeTraceRecentChangesTool,
    LifeTraceFileHistoryTool,
    LifeTraceShowDiffTool,
    LifeTracePreviewVersionTool,
]
