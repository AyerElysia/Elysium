"""life_chatter 子代理委托工具。

提供 life_run_agent 工具，让 life_chatter 表达层可以把复杂多步任务
委托给独立子代理执行。子代理拥有独立 LLM 上下文，支持：

- 4 种内置类型（explore / plan / general-purpose / verification）
- 同步模式（阻塞等待结果）和后台模式（立即返回，结果回流到 life_engine 事件流）
- 通过 mcp_servers 参数把指定 MCP 服务器能力委托给子代理

设计上复用 life_engine 已有的 AgentRunner / AgentCoordinator 系统，
不引入 default_chatter 的持久 SubAgentSession 机制（那套依赖
WaitResumeEvent 和 system reminder bucket，与 life_chatter 的全局
共享 runtime 架构不兼容）。
"""

from __future__ import annotations

from typing import Annotated, Any

from src.app.plugin_system.base import BaseTool
from src.kernel.logger import get_logger

logger = get_logger("life_chatter.sub_agent")


class LifeRunAgentTool(BaseTool):
    """life_chatter 子代理委托工具。"""

    tool_name: str = "life_run_agent"
    tool_description: str = (
        "启动一个子代理处理复杂的多步骤任务。"
        "子代理拥有独立的 LLM 上下文和工具集，可以多轮调用工具完成任务后返回结果。"
        "\n\n"
        "**适用场景：**\n"
        "- 需要多次文件操作 + 记忆检索 + 网络搜索的复合任务\n"
        "- 需要对抗性验证已完成的复杂工作（用 verification 类型）\n"
        "- 需要在后台执行耗时任务，不阻塞当前对话（run_in_background=true）\n"
        "\n"
        "**子代理类型：**\n"
        "- explore：只读检索专员，快速搜索信息（记忆/文件/网页/事件）\n"
        "- plan：只读规划专员，分析现状制定方案\n"
        "- general-purpose：全能子代理，完整读写能力\n"
        "- verification：只读验证专员，对抗性审查已完成工作\n"
        "- 不传 max_rounds 时使用 life_engine.chatter.sub_agent_default_max_rounds；"
        "传入正数可覆盖，范围为 1-30。\n"
        "\n"
        "**MCP 委托：**\n"
        "通过 mcp_servers 参数把指定 MCP 服务器的能力委托给 general-purpose 子代理。"
        "未在 life_chatter 主工具列表中暴露的延迟加载 MCP 服务器，"
        "可以通过此方式让 general-purpose 使用。"
        "explore、plan 和 verification 保持只读，不能委托 MCP 工具，"
        "因为 MCP 工具未统一标注副作用。"
        "\n\n"
        "**写任务简报的原则：**\n"
        "1. 说清要做什么、为什么这么做\n"
        "2. 提供已知信息（文件路径、内容位置、关键词）\n"
        "3. 说明期望的结果形式\n"
        "4. 不要写「帮我整理一下」这种模糊指令\n"
        "\n"
        "**同步 vs 后台：**\n"
        "- 同步（默认）：阻塞等待子代理完成，直接拿到结果。适合短任务。\n"
        "- 后台（run_in_background=true）：立即返回 agent_id，"
        "子代理在后台跑完后结果会注入到 life_engine 事件流，"
        "下次 life_chatter 唤醒时看到。适合长任务。"
    )
    chatter_allow: list[str] = ["life_chatter"]

    def _get_chatter_config(self) -> Any:
        """读取 life_engine 的 chatter 配置段。"""
        plugin_config = getattr(self.plugin, "config", None)
        return getattr(plugin_config, "chatter", None) if plugin_config is not None else None

    def _is_sub_agent_enabled(self) -> bool:
        """读取 enable_sub_agent 开关。"""
        cfg = self._get_chatter_config()
        return bool(cfg is not None and getattr(cfg, "enable_sub_agent", False))

    def _is_mcp_delegation_enabled(self) -> bool:
        """读取 sub_agent_allow_mcp 开关。"""
        cfg = self._get_chatter_config()
        return bool(cfg is not None and getattr(cfg, "sub_agent_allow_mcp", True))

    def _resolve_task_name(self, agent_type_def: Any) -> str:
        """解析子代理使用的模型任务名。"""
        cfg = self._get_chatter_config()
        configured = ""
        if cfg is not None:
            configured = str(getattr(cfg, "sub_agent_task_name", "") or "").strip()
        type_specific = getattr(agent_type_def, "model_task_name", None)
        return str(type_specific or configured or "agent").strip() or "agent"

    def _resolve_default_max_rounds(self) -> int:
        """读取 sub_agent_default_max_rounds。"""
        cfg = self._get_chatter_config()
        if cfg is None:
            return 8
        try:
            return max(1, min(30, int(getattr(cfg, "sub_agent_default_max_rounds", 8))))
        except (TypeError, ValueError):
            return 8

    def _resolve_max_rounds(self, value: Any) -> tuple[int | None, str]:
        """解析调用方覆盖值；未覆盖时使用 chatter 配置默认值。"""
        try:
            requested = 0 if value in (None, "") else int(value)
        except (TypeError, ValueError):
            return None, "max_rounds 必须是整数"

        if requested <= 0:
            return self._resolve_default_max_rounds(), ""
        return max(1, min(30, requested)), ""

    def _validate_mcp_server_names(self, requested: list[str]) -> tuple[list[str], list[str]]:
        """校验 mcp_servers 参数，返回 (有效列表, 无效列表)。"""
        if not requested:
            return [], []
        try:
            from src.core.managers.tool_manager import get_mcp_manager

            mcp_manager = get_mcp_manager()
            connected = {
                metadata.server_name
                for metadata in mcp_manager.get_connected_server_metadata()
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取 MCP 服务器列表失败：{exc}")
            return [], list(requested)

        valid: list[str] = []
        invalid: list[str] = []
        for name in requested:
            normalized = str(name or "").strip()
            if not normalized:
                continue
            if normalized in connected:
                valid.append(normalized)
            else:
                invalid.append(normalized)
        return valid, invalid

    async def execute(
        self,
        task: Annotated[str, "任务简报：说明要做什么、已知信息、期望结果。要具体，不要模糊。"],
        agent_type: Annotated[
            str,
            "子代理类型：explore / plan / general-purpose / verification",
        ] = "general-purpose",
        context: Annotated[
            str,
            "背景信息：你已经了解的、排除的、尝试过的。可留空。",
        ] = "",
        max_rounds: Annotated[
            int,
            "最大工具调用轮数。0 或不传时使用 life_engine.chatter.sub_agent_default_max_rounds（1-30）。",
        ] = 0,
        run_in_background: Annotated[
            bool,
            "是否后台异步运行。true 立即返回 agent_id，结果回流到 life_engine 事件流；false 同步等待结果。",
        ] = False,
        mcp_servers: Annotated[
            list[str],
            "要委托给 general-purpose 子代理的 MCP 服务器名列表。留空表示不委托任何 MCP 能力。",
        ] | None = None,
    ) -> tuple[bool, str | dict[str, Any]]:
        """启动子代理执行复杂任务。

        Args:
            task: 任务简报。
            agent_type: 子代理类型，默认 general-purpose。
            context: 背景信息，可留空。
            max_rounds: 最大工具调用轮数；0 或不传时使用配置默认值。
            run_in_background: 后台模式开关。
            mcp_servers: 要委托给 general-purpose 子代理的 MCP 服务器名列表。

        Returns:
            成功返回 (True, dict)；失败返回 (False, error_str)。
        """
        if not self._is_sub_agent_enabled():
            return False, "life_chatter 子代理功能未启用（life_engine.chatter.enable_sub_agent=false）"

        task_text = str(task or "").strip()
        if not task_text:
            return False, "task 不能为空"

        try:
            from ..agents.registry import get_agent_type_registry
            from ..agents.runner import AgentRunner
        except Exception as exc:  # noqa: BLE001
            logger.error(f"导入 agents 模块失败：{exc}", exc_info=True)
            return False, f"子代理模块加载失败: {exc}"

        registry = get_agent_type_registry()
        type_def = registry.get(agent_type)
        if type_def is None:
            return False, f"未知子代理类型: {agent_type}"

        requested_mcp = [
            str(name or "").strip()
            for name in (mcp_servers or [])
            if str(name or "").strip()
        ]
        valid_mcp: list[str] = []
        is_read_only = (
            type_def.is_read_only
            or agent_type in {"explore", "plan", "verification"}
        )
        if requested_mcp:
            if is_read_only:
                return False, (
                    f"只读子代理类型 {agent_type} 不支持 MCP 委托；"
                    "MCP 工具未统一标注副作用，请使用 general-purpose。"
                )
            if not self._is_mcp_delegation_enabled():
                return False, "子代理 MCP 委托未启用（life_engine.chatter.sub_agent_allow_mcp=false）"
            valid_mcp, invalid_mcp = self._validate_mcp_server_names(requested_mcp)
            if invalid_mcp:
                return False, {
                    "invalid_mcp_servers": invalid_mcp,
                    "valid_mcp_servers": valid_mcp,
                    "hint": "以下 MCP 服务器未连接或名称错误",
                }

        effective_max_rounds, max_rounds_error = self._resolve_max_rounds(max_rounds)
        if effective_max_rounds is None:
            return False, max_rounds_error

        from dataclasses import replace

        resolved_task_name = self._resolve_task_name(type_def)
        type_def = replace(
            type_def,
            max_rounds=effective_max_rounds,
            model_task_name=resolved_task_name,
        )

        full_context = str(context or "").strip()
        current_stream_id = self.get_current_stream_id()
        trigger_message = self.trigger_message

        # 后台模式使用同一个已解析 definition 和调用上下文，避免任务切换后丢失。
        if run_in_background:
            try:
                coordinator = self._get_or_create_coordinator()
                agent_id = await coordinator.spawn(
                    agent_type=agent_type,
                    task=task_text,
                    context=full_context,
                    extra_mcp_server_names=valid_mcp,
                    agent_type_def=type_def,
                    stream_id=current_stream_id,
                    trigger_message=trigger_message,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"启动后台子代理失败：{exc}", exc_info=True)
                return False, f"启动后台子代理失败: {exc}"

            return True, {
                "action": "run_agent_background",
                "agent_id": agent_id,
                "agent_type": agent_type,
                "mcp_servers": valid_mcp,
                "max_rounds": type_def.max_rounds,
                "model_task_name": type_def.model_task_name,
                "status": "running",
                "task_preview": task_text[:200],
            }

        try:
            runner = AgentRunner(
                plugin=self.plugin,
                agent_type_def=type_def,
                task_prompt=task_text,
                context=full_context,
                extra_mcp_server_names=valid_mcp,
                stream_id=current_stream_id,
                trigger_message=trigger_message,
            )
            result = await runner.run()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"执行子代理失败：{exc}", exc_info=True)
            return False, f"执行子代理失败: {exc}"

        if result.success:
            return True, {
                "action": "run_agent",
                "agent_type": agent_type,
                "mcp_servers": valid_mcp,
                "max_rounds": type_def.max_rounds,
                "model_task_name": type_def.model_task_name,
                "rounds": result.rounds_used,
                "tool_calls": result.tool_use_count,
                "duration_ms": result.duration_ms,
                "result": result.result_text,
            }
        return False, result.result_text

    def _get_or_create_coordinator(self) -> Any:
        """获取或创建 AgentCoordinator 单例，挂在插件实例上。"""
        if bool(getattr(self.plugin, "_agent_coordinator_shutdown", False)):
            raise RuntimeError("插件正在停止，不能启动后台智能体")
        coordinator = getattr(self.plugin, "_agent_coordinator", None)
        if coordinator is None or bool(getattr(coordinator, "is_closed", False)):
            from ..agents.coordinator import AgentCoordinator

            coordinator = AgentCoordinator(self.plugin)
            self.plugin._agent_coordinator = coordinator
        return coordinator
