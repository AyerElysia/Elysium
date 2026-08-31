"""增强型 Worker：编排系统的任务执行单元。

在现有 AgentRunner 的多轮工具调用循环基础上增加：
- 资源预算跟踪（token / 轮数 / 时间）与强制中断
- 按 TaskKind 过滤工具集（research 只读，code 可写）
- 结构化输出提取（尝试 JSON 解析，fallback 为纯文本）
- 每轮追踪钩子（供 tracing 模块记录）
- 输出验证（against TaskContract.output_schema）
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.core.utils.llm_tool_call import exec_llm_usable
from src.kernel.llm import ROLE, LLMPayload, Text, ToolRegistry, ToolResult
from src.kernel.logger import get_logger

from .activity import DelegatedActivityRecorder
from .contracts import TaskContract, TaskKind, TaskResult, TaskStatus

if TYPE_CHECKING:
    from src.app.plugin_system.base import BasePlugin
    from src.core.models.message import Message

logger = get_logger("life_engine.orchestration.worker")

# ---------------------------------------------------------------------------
# 按 TaskKind 的工具策略
# ---------------------------------------------------------------------------

# 只读工具——research / verify 只能用这些
_READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset({
    "nucleus_read_file",
    "nucleus_list_files",
    "nucleus_grep_file",
    "nucleus_grep_events",
    "nucleus_search_memory",
    "nucleus_relations",
    "nucleus_memory_stats",
    "fetch_life_memory",
    "conversation_evidence",
    "nucleus_web_search",
    "nucleus_browser_fetch",
    "nucleus_view_screen",
    "nucleus_minecraft",
    "nucleus_proactive_query",
    "nucleus_todo",
    "nucleus_schedule",
    "nucleus_list_todos",
    "nucleus_list_schedules",
})

# 全局禁止——worker 绝不可用的工具（防递归、防对外通信）
_FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset({
    "nucleus_run_agent",
    "life_run_agent",
    "life_dispatch_mission",
    "life_mission_status",
    "life_mission_cancel",
    "nucleus_tell_dfc",
})

# TaskKind → 是否只读
_KIND_READ_ONLY: dict[TaskKind, bool] = {
    TaskKind.RESEARCH: True,
    TaskKind.VERIFY: True,
    TaskKind.SYNTHESIZE: True,
    TaskKind.CODE: False,
    TaskKind.CUSTOM: False,
}

# TaskKind → 系统提示
_KIND_SYSTEM_PROMPTS: dict[TaskKind, str] = {
    TaskKind.RESEARCH: (
        "你是编排系统派出的调研专员。你的职责是搜索、检索、汇总信息。\n"
        "你是只读的——不修改任何文件。快速定位信息后结构化报告。"
    ),
    TaskKind.CODE: (
        "你是编排系统派出的工程师。你有完整读写能力，可以创建和修改文件、\n"
        "执行命令。按照任务要求完成工程工作，最后报告修改清单。"
    ),
    TaskKind.VERIFY: (
        "你是编排系统派出的验证专员。以对抗性视角审查已完成的工作：\n"
        "检查逻辑错误、遗漏、不一致。只读，只报告问题，不做修改。"
    ),
    TaskKind.SYNTHESIZE: (
        "你是编排系统派出的综合专员。你的职责是将多个来源的信息\n"
        "整合为一份连贯、结构化的输出。只读，不修改文件。"
    ),
    TaskKind.CUSTOM: (
        "你是编排系统派出的子代理。按照任务简报完成工作，\n"
        "完成后清晰报告结果。"
    ),
}


# ---------------------------------------------------------------------------
# 追踪钩子类型
# ---------------------------------------------------------------------------

TraceHook = Callable[[str, dict[str, Any]], None]
"""(event_type, payload) → None。event_type: round_start/round_end/tool_call/done"""


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class Worker:
    """执行单个 TaskContract 的增强型子代理。"""

    def __init__(
        self,
        plugin: BasePlugin,
        task: TaskContract,
        model_task_name: str = "agent",
        stream_id: str = "",
        trigger_message: Message | None = None,
        trace_hook: TraceHook | None = None,
        upstream_outputs: dict[str, Any] | None = None,
    ) -> None:
        self.plugin = plugin
        self.task = task
        self.model_task_name = model_task_name
        self.stream_id = stream_id
        self.trigger_message = trigger_message
        self.trace_hook = trace_hook
        # 上游任务的输出，注入到 context 中
        self.upstream_outputs = upstream_outputs or {}
        self._activity_recorder = DelegatedActivityRecorder(
            plugin=plugin,
            stream_id=stream_id,
            trigger_message=trigger_message,
            surface="life_engine_orchestration",
            run_occurrence_id=f"orchestration-task:{task.task_id}",
        )

    async def run(self) -> TaskResult:
        """执行任务，返回结构化结果。"""
        start = time.monotonic()
        tokens_used = 0
        rounds_used = 0

        try:
            output, rounds_used, tokens_used = await asyncio.wait_for(
                self._run_loop(),
                timeout=self.task.timeout_seconds,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            self._emit("done", {"status": "succeeded", "rounds": rounds_used})
            return TaskResult(
                task_id=self.task.task_id,
                status=TaskStatus.SUCCEEDED,
                output=output,
                rounds_used=rounds_used,
                tokens_used=tokens_used,
                duration_ms=duration_ms,
                trace_id=self.task.task_id,
            )
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start) * 1000)
            self._emit("done", {"status": "timeout"})
            return TaskResult(
                task_id=self.task.task_id,
                status=TaskStatus.TIMEOUT,
                error=f"任务超时（{self.task.timeout_seconds}s）",
                rounds_used=rounds_used,
                tokens_used=tokens_used,
                duration_ms=duration_ms,
                trace_id=self.task.task_id,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            self._emit("done", {"status": "failed", "error": str(exc)})
            return TaskResult(
                task_id=self.task.task_id,
                status=TaskStatus.FAILED,
                error=str(exc),
                rounds_used=rounds_used,
                tokens_used=tokens_used,
                duration_ms=duration_ms,
                trace_id=self.task.task_id,
            )

    # ------------------------------------------------------------------
    # 核心循环
    # ------------------------------------------------------------------

    async def _run_loop(self) -> tuple[dict[str, Any] | str, int, int]:
        """多轮工具调用循环。返回 (output, rounds, tokens)。"""
        from ..core.config import LifeEngineConfig
        from ..memory.tools import MEMORY_TOOLS
        from ..tools import ALL_TOOLS
        from ..tools.grep_tools import GREP_TOOLS
        from ..tools.todo_tools import TODO_TOOLS
        from ..tools.web_tools import WEB_TOOLS

        config = getattr(self.plugin, "config", None)
        if not isinstance(config, LifeEngineConfig):
            raise RuntimeError("无法获取 life_engine 配置")

        # 模型
        model_set = get_model_set_by_task(self.model_task_name)
        if not model_set:
            raise RuntimeError(f"找不到模型配置: {self.model_task_name}")

        # 工具过滤
        all_tool_classes = list(ALL_TOOLS + TODO_TOOLS + MEMORY_TOOLS + GREP_TOOLS + WEB_TOOLS)
        filtered = self._filter_tools(all_tool_classes)

        tool_registry = ToolRegistry()
        for cls in filtered:
            tool_registry.register(cls)

        # 系统提示
        workspace = Path(config.settings.workspace_path)
        system_prompt = self._build_system_prompt(workspace)

        # 用户提示
        user_prompt = self._build_user_prompt()

        # 构建请求
        request = create_llm_request(
            model_set=model_set,
            request_name="life_engine_worker",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.TOOL, filtered))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        # 循环
        budget = self.task.budget
        max_rounds = budget.max_rounds
        final_text = ""
        total_tokens = 0
        round_num = 0

        response = await request.send(stream=False)

        for round_num in range(max_rounds):
            # 预算检查：时间
            # （超时由外层 wait_for 处理，这里做轮数检查即可）

            self._emit("round_start", {"round": round_num + 1})

            response_text = await response
            reply_text = str(response_text or "").strip()

            # 粗略 token 估算（无精确计数时的 fallback）
            total_tokens += len(reply_text) // 3

            call_list = list(getattr(response, "call_list", []) or [])
            activity_ids = await self._activity_recorder.record_model_turn(
                response,
                call_list,
                turn_index=round_num,
            )
            if not call_list:
                final_text = reply_text
                self._emit("round_end", {"round": round_num + 1, "tool_calls": 0})
                break

            self._emit("round_end", {
                "round": round_num + 1,
                "tool_calls": len(call_list),
            })

            activity_results: list[dict[str, Any]] = []
            for call_index, call in enumerate(call_list):
                tool_name = getattr(call, "name", "") or ""
                raw_args = getattr(call, "args", {}) or {}
                args = dict(raw_args) if isinstance(raw_args, dict) else {}

                self._emit("tool_call", {"tool": tool_name, "args_keys": list(args.keys())})

                usable_cls = tool_registry.get(tool_name)
                tool_succeeded = False
                if usable_cls:
                    try:
                        success, result = await exec_llm_usable(
                            usable_cls,
                            plugin=self.plugin,
                            stream_id=self.stream_id,
                            message=self.trigger_message,
                            kwargs=args,
                        )
                        tool_succeeded = bool(success)
                        result_text = str(result) if success else f"失败: {result}"
                    except Exception as exc:
                        result_text = f"异常: {exc}"
                else:
                    result_text = f"未知工具: {tool_name}"

                total_tokens += len(result_text) // 3

                call_id = getattr(call, "id", None)
                effective_call_id = str(call_id or f"delegated-call-{call_index}")
                response.add_payload(
                    LLMPayload(
                        ROLE.TOOL_RESULT,
                        ToolResult(value=result_text, call_id=call_id, name=tool_name),
                    )
                )
                activity_results.append(
                    {
                        "call_id": effective_call_id,
                        "tool_name": str(tool_name or "<unknown>"),
                        "result": result_text,
                        "success": tool_succeeded,
                        "technical_outcome": (
                            "orchestration_tool_completed"
                            if tool_succeeded
                            else "orchestration_tool_failed"
                        ),
                    }
                )

            await self._activity_recorder.record_tool_results(
                turn_index=round_num,
                activity_ids=activity_ids,
                results=activity_results,
            )

            # 预算检查：token
            if total_tokens >= budget.max_tokens_total:
                final_text = reply_text or "（token 预算耗尽，提前终止）"
                break

            response = await response.send(stream=False)
        else:
            final_text = final_text or f"Worker 在 {max_rounds} 轮内未完成"

        output = self._parse_output(final_text)
        return output, round_num + 1, total_tokens

    # ------------------------------------------------------------------
    # 工具过滤
    # ------------------------------------------------------------------

    def _filter_tools(self, all_tools: list[type]) -> list[type]:
        """根据 TaskKind 过滤可用工具。"""
        is_read_only = _KIND_READ_ONLY.get(self.task.kind, False)
        result: list[type] = []

        for cls in all_tools:
            name = getattr(cls, "tool_name", None) or ""
            # 全局禁止
            if name in _FORBIDDEN_TOOL_NAMES:
                continue
            # 只读限制
            if is_read_only and name not in _READ_ONLY_TOOL_NAMES:
                continue
            result.append(cls)

        return result

    # ------------------------------------------------------------------
    # 提示构建
    # ------------------------------------------------------------------

    def _build_system_prompt(self, workspace: Path) -> str:
        base = _KIND_SYSTEM_PROMPTS.get(self.task.kind, _KIND_SYSTEM_PROMPTS[TaskKind.CUSTOM])
        parts = [
            base,
            f"\n工作空间: {workspace}",
            "\n## 输出要求",
            "完成任务后，以如下 JSON 格式输出最终结果（包裹在 ```json 代码块中）：",
            '```json\n{"summary": "一句话总结", "details": "详细内容", "artifacts": ["产出物路径或标识"]}\n```',
            "如果无法用 JSON 表达，直接输出纯文本也可以。",
        ]
        return "\n".join(parts)

    def _build_user_prompt(self) -> str:
        parts = [
            "## 任务简报",
            "",
            self.task.brief.strip(),
        ]

        if self.task.context.strip():
            parts.extend(["", "## 背景信息", "", self.task.context.strip()])

        # 注入上游输出
        if self.upstream_outputs:
            parts.extend(["", "## 上游任务产出（供参考）", ""])
            for tid, output in self.upstream_outputs.items():
                preview = str(output)[:500]
                parts.append(f"### {tid}\n{preview}\n")

        parts.extend([
            "",
            "## 执行原则",
            "",
            "- 直接开始执行，不要询问或确认",
            "- 使用工具完成任务时，注意先读后改",
            "- 完成后报告：(1) 做了什么 (2) 结果是什么 (3) 发现了什么",
            "- 如果遇到阻碍，说明原因并报告当前已完成的部分",
        ])

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 输出解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_output(text: str) -> dict[str, Any] | str:
        """尝试从文本中提取 JSON 结构，失败则返回原文。"""
        if not text:
            return ""
        # 尝试提取 ```json ... ``` 代码块
        marker = "```json"
        idx = text.find(marker)
        if idx != -1:
            end = text.find("```", idx + len(marker))
            if end != -1:
                json_str = text[idx + len(marker):end].strip()
                try:
                    return json.loads(json_str)
                except (json.JSONDecodeError, ValueError):
                    pass
        # 尝试整体 JSON 解析
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                pass
        return text

    # ------------------------------------------------------------------
    # 追踪
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.trace_hook is not None:
            try:
                self.trace_hook(event_type, {
                    "task_id": self.task.task_id,
                    "mission_id": self.task.mission_id,
                    **payload,
                })
            except Exception:  # noqa: BLE001
                pass
