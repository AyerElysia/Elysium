"""智能体运行器。

将 LifeEngineRunAgentTool 中的多轮工具调用循环提取为独立可复用类，
支持不同智能体类型的系统提示、工具过滤和模型选择。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.core.utils.llm_tool_call import exec_llm_usable
from src.kernel.llm import LLMPayload, ROLE, Text, ToolRegistry, ToolResult

from .definitions import AgentResult, AgentTypeDefinition
from .registry import get_agent_type_registry

if TYPE_CHECKING:
    from src.app.plugin_system.base import BasePlugin
    from src.core.models.message import Message


class AgentRunner:
    """执行单个智能体的多轮工具调用循环。"""

    def __init__(
        self,
        plugin: BasePlugin,
        agent_type_def: AgentTypeDefinition,
        task_prompt: str,
        context: str = "",
        extra_mcp_server_names: list[str] | None = None,
        stream_id: str = "",
        trigger_message: Message | None = None,
    ) -> None:
        self.plugin = plugin
        self.agent_type_def = agent_type_def
        self.task_prompt = task_prompt
        self.context = context
        self.extra_mcp_server_names = list(extra_mcp_server_names or [])
        self.stream_id = str(stream_id or "").strip()
        self.trigger_message = trigger_message

    async def run(self) -> AgentResult:
        """同步执行智能体，返回结构化结果。"""
        start = time.monotonic()
        try:
            final_text, rounds, tool_count = await self._run_loop()
            duration_ms = int((time.monotonic() - start) * 1000)
            return AgentResult(
                agent_type=self.agent_type_def.agent_type,
                success=True,
                result_text=final_text,
                tool_use_count=tool_count,
                duration_ms=duration_ms,
                rounds_used=rounds,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return AgentResult(
                agent_type=self.agent_type_def.agent_type,
                success=False,
                result_text=f"执行失败: {exc}",
                duration_ms=duration_ms,
            )

    async def run_background(self) -> asyncio.Task:
        """作为后台异步任务启动，返回 asyncio.Task 用于后续收集。"""
        return asyncio.create_task(self.run(), name=f"agent_{self.agent_type_def.agent_type}")

    async def _run_loop(self) -> tuple[str, int, int]:
        """核心多轮循环。返回 (最终文本, 轮数, 工具调用次数)。"""
        from ..core.config import LifeEngineConfig
        from ..tools import ALL_TOOLS
        from ..tools.todo_tools import TODO_TOOLS
        from ..memory.tools import MEMORY_TOOLS
        from ..tools.grep_tools import GREP_TOOLS
        from ..tools.web_tools import WEB_TOOLS

        config = getattr(self.plugin, "config", None)
        if not isinstance(config, LifeEngineConfig):
            raise RuntimeError("无法获取 life_engine 配置")

        # 模型
        task_name = self.agent_type_def.model_task_name or "sub_actor"
        model_set = get_model_set_by_task(task_name)
        if not model_set:
            raise RuntimeError(f"找不到模型配置: {task_name}")

        # 工具过滤
        all_tool_classes = list(ALL_TOOLS + TODO_TOOLS + MEMORY_TOOLS + GREP_TOOLS + WEB_TOOLS)
        registry_obj = get_agent_type_registry()
        filtered = registry_obj.filter_tools_for_agent(
            self.agent_type_def.agent_type, all_tool_classes
        )

        # MCP 工具没有统一的只读元数据。只读智能体不能因显式服务器委托
        # 获得任意外部副作用能力；general-purpose 才能接收这些工具。
        is_read_only = (
            self.agent_type_def.is_read_only
            or self.agent_type_def.agent_type in {"explore", "plan", "verification"}
        )
        if self.extra_mcp_server_names and not is_read_only:
            from src.core.managers.tool_manager import get_mcp_manager

            mcp_manager = get_mcp_manager()
            connected_names = {
                metadata.server_name for metadata in mcp_manager.get_connected_server_metadata()
            }
            valid_names = [
                name for name in self.extra_mcp_server_names if name in connected_names
            ]
            if valid_names:
                mcp_tool_classes = mcp_manager.get_tool_classes_for_servers(valid_names)
                # 已在 registry 过滤里出现过的不再重复
                existing = set(filtered)
                for cls in mcp_tool_classes:
                    if cls in existing:
                        continue
                    existing.add(cls)
                    filtered.append(cls)

        tool_registry = ToolRegistry()
        for cls in filtered:
            tool_registry.register(cls)

        # 系统提示
        workspace = Path(config.settings.workspace_path)
        system_prompt = self.agent_type_def.system_prompt()
        system_prompt += f"\n\n工作空间: {workspace}"

        # 构建请求
        user_prompt = self._build_user_prompt()

        request = create_llm_request(
            model_set=model_set,
            request_name="life_engine_agent",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.TOOL, filtered))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        # 多轮循环
        max_rounds = self.agent_type_def.max_rounds
        final_result = ""
        round_num = 0
        total_tool_calls = 0

        response = await request.send(stream=False)

        for round_num in range(max_rounds):
            response_text = await response
            reply_text = str(response_text or "").strip()

            call_list = list(getattr(response, "call_list", []) or [])
            if not call_list:
                final_result = reply_text
                break

            total_tool_calls += len(call_list)

            for call in call_list:
                tool_name = getattr(call, "name", "") or ""
                raw_args = getattr(call, "args", {}) or {}
                args = dict(raw_args) if isinstance(raw_args, dict) else {}

                usable_cls = tool_registry.get(tool_name)
                if usable_cls:
                    try:
                        success, result = await exec_llm_usable(
                            usable_cls,
                            plugin=self.plugin,
                            stream_id=self.stream_id,
                            message=self.trigger_message,
                            kwargs=args,
                        )
                        result_text = str(result) if success else f"失败: {result}"
                    except Exception as exc:
                        result_text = f"异常: {exc}"
                else:
                    result_text = f"未知工具: {tool_name}"

                call_id = getattr(call, "id", None)
                response.add_payload(
                    LLMPayload(
                        ROLE.TOOL_RESULT,
                        ToolResult(value=result_text, call_id=call_id, name=tool_name),
                    )
                )

            response = await response.send(stream=False)
        else:
            final_result = reply_text if reply_text else f"子代理在 {max_rounds} 轮内未完成"

        return final_result, round_num + 1, total_tool_calls

    def _build_user_prompt(self) -> str:
        """构建用户提示词。"""
        parts = [
            "你是生命中枢分派的子代理，正在完成一个具体任务。",
            "完成后清晰地报告：做了什么、结果是什么、过程中发现了什么。",
            "",
            "## 任务简报",
            "",
            self.task_prompt.strip(),
        ]

        if self.context.strip():
            parts.extend([
                "",
                "## 背景信息",
                "",
                self.context.strip(),
            ])

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
