"""Deprecated compatibility facade for ``fetch_chat_history``.

New model manifests expose :class:`LifeEngineConversationEvidenceTool`.
This facade keeps historical direct callers and trajectory replay readable,
but deliberately removes implicit stream selection, platform I/O, raw payload
copies, nested context windows, and tool-event mixing.
"""

from __future__ import annotations

import json
from typing import Annotated, ClassVar, Literal

from src.app.plugin_system.base import BaseTool

from .conversation_evidence import LifeEngineConversationEvidenceTool


class LifeEngineFetchChatHistoryTool(BaseTool):
    """Map the legacy call shape to bounded conversation evidence."""

    tool_name = "fetch_chat_history"
    tool_description = (
        "已弃用的聊天历史兼容入口。新调用请使用 conversation_evidence。"
        "兼容入口只读本地消息证据且结果有硬字节上限；不再自动选择最近会话、回补 NapCat 或混入工具事件。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal"]

    async def execute(
        self,
        query: Annotated[str, "关键词或正则；留空返回最近消息"] = "",
        use_regex: Annotated[bool, "是否按正则匹配 query"] = False,
        case_insensitive: Annotated[bool, "保留兼容；规范检索固定忽略大小写"] = True,
        stream_ids: Annotated[
            list[str] | None, "显式 stream_id；留空只允许使用当前绑定流"
        ] = None,
        cross_stream: Annotated[
            bool | None, "保留兼容；跨流必须同时显式给出 stream_ids"
        ] = None,
        platform: Annotated[str, "保留兼容；规范检索按 stream identity 限定"] = "",
        time_from: Annotated[str, "Unix 起始时间"] = "",
        time_to: Annotated[str, "Unix 结束时间"] = "",
        limit: Annotated[int, "返回条数上限"] = 20,
        context_before: Annotated[int, "合并的前置邻居数"] = 1,
        context_after: Annotated[int, "合并的后置邻居数"] = 1,
        source_mode: Annotated[
            Literal["auto", "local_db", "napcat"],
            "仅 local_db/auto 兼容；napcat 已拆分",
        ] = "auto",
        force_backfill: Annotated[bool, "已弃用；平台同步必须显式调用独立能力"] = False,
        include_tool_calls: Annotated[bool, "已弃用；工具历史请查询 Trace"] = False,
    ) -> tuple[bool, str]:
        del case_insensitive, platform, include_tool_calls
        explicit = [
            str(item or "").strip()
            for item in (stream_ids or [])
            if str(item or "").strip()
        ]
        if source_mode == "napcat" or force_backfill:
            return False, json.dumps(
                {
                    "error": {
                        "code": "platform_sync_separated",
                        "message": "use sync_platform_history explicitly, then query conversation_evidence",
                    }
                },
                separators=(",", ":"),
            )
        if bool(cross_stream) and len(explicit) < 2:
            return False, json.dumps(
                {
                    "error": {
                        "code": "explicit_streams_required",
                        "message": "cross-stream compatibility calls must list every stream_id explicitly",
                    }
                },
                separators=(",", ":"),
            )

        def _time(raw: str) -> float | None:
            text = str(raw or "").strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return None

        canonical = LifeEngineConversationEvidenceTool(plugin=self.plugin)
        canonical._bind_runtime_context(
            stream_id=self.get_current_stream_id(),
            message=self.trigger_message,
        )
        if hasattr(self, "_runtime_task_name"):
            canonical._runtime_task_name = self._runtime_task_name
        return await canonical.execute(
            operation="search" if str(query or "") else "page",
            query=str(query or ""),
            use_regex=bool(use_regex),
            stream_ids=explicit or None,
            limit=limit,
            context_radius=max(int(context_before), int(context_after)),
            time_from=_time(time_from),
            time_to=_time(time_to),
        )


# The deprecated class is intentionally not registered in primary manifests.
CHAT_HISTORY_TOOLS: list[type[BaseTool]] = []
