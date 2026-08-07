"""Life Trace query tools."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from src.app.plugin_system.base import BaseTool

from ..core.config import LifeEngineConfig
from .store import LifeTraceRecord, LifeTraceStore


_TRACE_CHATTER_ALLOW = ["life_engine_internal", "life_chatter", "default_chatter"]


def _store(plugin: Any) -> Any:
    service = getattr(plugin, "service", None)
    get_store = getattr(service, "life_trace_store", None)
    if not callable(get_store):
        raise RuntimeError("LifeTraceServiceUnavailable")
    return get_store()


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
    data: dict[str, Any] = {
        "trace_id": record.trace_id,
        "timestamp": record.timestamp,
        "kind": record.kind,
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
    if record.summary:
        data["summary"] = record.summary
    if record.stream_id:
        data["stream_id"] = record.stream_id
    return data


class LifeTraceRecentChangesTool(BaseTool):
    """查看长河最近的留痕。"""

    tool_name: str = "nucleus_trace_recent_changes"
    tool_description: str = (
        "查看你的长河里最近的留痕——文件修改、意图归宿、闭合的思考流、承接的好奇。"
        "用于回答“最近改过什么”“SOUL 是什么时候变的”“我最近形成过哪些意向”等问题。"
        "kind 可过滤：file_change / intent / thought_stream / curiosity。"
    )
    chatter_allow: list[str] = _TRACE_CHATTER_ALLOW

    async def execute(
        self,
        limit: Annotated[int, "最多返回多少条，默认 10"] = 10,
        path: Annotated[str, "可选：只看某个 workspace 相对路径"] = "",
        kind: Annotated[str, "可选：只看某类留痕（file_change/intent/thought_stream/curiosity）"] = "",
    ) -> tuple[bool, str | dict]:
        try:
            records = await _store(self.plugin).recent(
                limit=max(1, min(int(limit or 10), 50)),
                path=path,
                kind=kind,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"读取追溯记录失败: {exc}"
        return True, {
            "action": "trace_recent_changes",
            "count": len(records),
            "records": [_record_summary(record) for record in records],
        }


class LifeTraceOriginTool(BaseTool):
    """长河源头：她是从哪里开始的。"""

    tool_name: str = "nucleus_trace_origin"
    tool_description: str = (
        "查看你的长河源头与全貌：最早的一条留痕、至今共有多少条、跨越了多少天、"
        "各类留痕（文件/意图/思考流/好奇）各有多少。"
        "想知道“我是怎么来的”“我走了多远”时使用。"
    )
    chatter_allow: list[str] = _TRACE_CHATTER_ALLOW

    async def execute(self) -> tuple[bool, str | dict]:
        try:
            overview = await _store(self.plugin).origin()
        except Exception as exc:  # noqa: BLE001
            return False, f"读取长河源头失败: {exc}"
        if not overview.get("total"):
            return True, {
                "action": "trace_origin",
                "total": 0,
                "note": "长河还没有留痕——你的来路从此刻开始。",
            }
        return True, {"action": "trace_origin", **overview}


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
            records = await _store(self.plugin).history(
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
        record, diff_text = await store.read_diff(trace_id)
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
        record = await store.get(trace_id)
        if record is None:
            return False, f"找不到追溯记录: {trace_id}"
        normalized_side = str(side or "before").strip().lower()
        if normalized_side not in {"before", "after"}:
            return False, "side 仅支持 before/after"
        digest = record.before_hash if normalized_side == "before" else record.after_hash
        content = await store.read_blob(digest)
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


class NucleusTraceTool(BaseTool):
    """统一的文件追溯工具（合并原 5 个 trace 工具）。"""

    tool_name: str = "nucleus_trace"
    tool_description: str = (
        "文件修改追溯。\n\n"
        "action=recent：查看最近的文件修改记录\n"
        "action=history：查看某个文件的修改历史\n"
        "action=diff：查看某次修改的差异\n"
        "action=preview：查看某次修改前/后的文件内容\n"
        "action=origin：查看文件的创建来源"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(self, action: Annotated[str, "操作：recent/history/diff/preview/origin"] = "recent", **kwargs: object) -> tuple[bool, str | dict]:
        action_value = str(action or "recent").strip().lower()
        tool_map = {
            "recent": LifeTraceRecentChangesTool,
            "history": LifeTraceFileHistoryTool,
            "diff": LifeTraceShowDiffTool,
            "preview": LifeTracePreviewVersionTool,
            "origin": LifeTraceOriginTool,
        }
        cls = tool_map.get(action_value, LifeTraceRecentChangesTool)
        tool = cls(plugin=self.plugin)
        return await tool.execute(**kwargs)  # type: ignore[arg-type]


LIFE_TRACE_TOOLS = [
    NucleusTraceTool,
]
