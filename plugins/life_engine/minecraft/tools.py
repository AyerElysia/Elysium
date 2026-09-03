"""nucleus_minecraft 工具。

让爱莉通过工具调用控制 Minecraft 会话。
这是她与 MC 世界的接口。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from .prompts import TOOL_DESCRIPTION

logger = log_api.get_logger("life_engine.minecraft.tools")

_TURN_CACHE_LIMIT = 128
_TURN_IDEMPOTENT_ACTIONS = frozenset({"start", "stop", "do", "interrupt", "look"})


@dataclass(slots=True)
class _TurnIdempotencyState:
    """Share bounded semantic tool results across per-call tool instances."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    completed: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    pending: dict[str, asyncio.Task[dict[str, Any]]] = field(default_factory=dict)


def _turn_operation_key(
    tool: LifeEngineMinecraftTool,
    *,
    action: str,
    intent: str,
    goal: str,
    body_name: str,
    reason: str,
) -> str:
    """Return a semantic key only when chatter supplied a stable unread turn."""

    if action not in _TURN_IDEMPOTENT_ACTIONS:
        return ""
    extra = getattr(tool.trigger_message, "extra", {}) or {}
    scope = extra.get("life_turn_scope")
    if not isinstance(scope, dict):
        return ""
    turn_key = str(scope.get("turn_key") or "").strip()
    if not turn_key:
        return ""
    semantic_args: dict[str, str] = {"action": action}
    if action == "start":
        semantic_args.update(goal=goal.strip(), body_name=body_name.strip())
    elif action == "do":
        semantic_args["intent"] = intent.strip()
    elif action == "interrupt":
        semantic_args["reason"] = reason.strip()
    canonical = json.dumps(
        {"turn_key": turn_key, "semantic_args": semantic_args},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _execute_turn_once(
    session: Any,
    operation_key: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Execute one semantic operation once per chatter turn, including races."""

    if not operation_key:
        return await operation()
    state = getattr(session, "_minecraft_tool_turn_idempotency", None)
    if not isinstance(state, _TurnIdempotencyState):
        state = _TurnIdempotencyState()
        session._minecraft_tool_turn_idempotency = state

    async with state.lock:
        cached = state.completed.get(operation_key)
        if cached is not None:
            state.completed.move_to_end(operation_key)
            return dict(cached)
        task = state.pending.get(operation_key)
        if task is None:
            task = asyncio.create_task(operation())
            state.pending[operation_key] = task

    try:
        result = await asyncio.shield(task)
    except BaseException:
        async with state.lock:
            if state.pending.get(operation_key) is task and task.done():
                state.pending.pop(operation_key, None)
        raise

    async with state.lock:
        if state.pending.get(operation_key) is task:
            state.pending.pop(operation_key, None)
            state.completed[operation_key] = dict(result)
            state.completed.move_to_end(operation_key)
            while len(state.completed) > _TURN_CACHE_LIMIT:
                state.completed.popitem(last=False)
        cached_result = state.completed.get(operation_key, result)
    return dict(cached_result)


def _get_session(plugin: Any) -> Any:
    """从 plugin 获取 MinecraftSession。"""
    service = getattr(plugin, "service", None)
    if service is None:
        return None
    session = getattr(service, "minecraft_session", None)
    return session


class LifeEngineMinecraftTool(BaseTool):
    """Minecraft 具身体验工具。"""

    tool_name: str = "nucleus_minecraft"
    description: str = TOOL_DESCRIPTION
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "preflight",
                    "start",
                    "stop",
                    "do",
                    "interrupt",
                    "look",
                    "status",
                ],
                "description": "操作类型",
            },
            "intent": {
                "type": "string",
                "description": "意图描述（action=do 时必填），如 '砍那棵树'、'向下挖'",
            },
            "goal": {
                "type": "string",
                "description": "会话目标（action=start 时可选），如 '收集木材做工具'",
            },
            "body_name": {
                "type": "string",
                "enum": ["agent", "bot", "biomimetic"],
                "description": (
                    "action=start 时使用的身体：bot=默认的陪玩身体，以独立玩家 "
                    "ElysiaBot 身份进入 Ayer 已开放的共享世界并自动加入；"
                    "agent=独自托管客户端；biomimetic=实验性前台视觉身体"
                ),
            },
            "reason": {
                "type": "string",
                "description": "action=interrupt 时由爱莉给出的中断理由",
            },
        },
        "required": ["action"],
    }

    async def execute(
        self,
        action: Annotated[str, "操作类型"],
        intent: Annotated[str, "意图描述"] = "",
        goal: Annotated[str, "会话目标"] = "",
        body_name: Annotated[str, "精确身体名称"] = "",
        reason: Annotated[str, "中断理由"] = "",
        **kwargs: Any,
    ) -> tuple[bool, str]:
        """执行 Minecraft 操作。"""
        # BaseTool is constructed with its owning plugin by the production tool
        # runtime.  Keep the explicit kwarg only as a compatibility override for
        # direct callers and older tests.
        plugin = kwargs.get("plugin") or self.plugin
        if plugin is None:
            return (
                False,
                json.dumps(
                    {"success": False, "error": "plugin 未提供"},
                    ensure_ascii=False,
                ),
            )

        session = _get_session(plugin)
        if session is None:
            return (
                False,
                json.dumps(
                    {"success": False, "error": "Minecraft 未启用或会话未初始化"},
                    ensure_ascii=False,
                ),
            )

        if action == "do" and not intent:
            return False, json.dumps(
                {"success": False, "error": "请提供 intent 参数"},
                ensure_ascii=False,
            )
        if action == "interrupt" and not reason:
            return False, json.dumps(
                {"success": False, "error": "请提供 reason 参数"},
                ensure_ascii=False,
            )
        if action not in self.parameters["properties"]["action"]["enum"]:
            return False, json.dumps(
                {"success": False, "error": f"未知操作: {action}"},
                ensure_ascii=False,
            )

        async def run_operation() -> dict[str, Any]:
            match action:
                case "preflight":
                    return await session.preflight(body_name=body_name)
                case "start":
                    return await session.start(goal=goal, body_name=body_name)
                case "stop":
                    return await session.stop()
                case "do":
                    return await session.do_intent(intent)
                case "interrupt":
                    return await session.interrupt(reason)
                case "look":
                    return await session.look()
                case "status":
                    return await session.get_status()
            raise AssertionError("validated Minecraft action was not dispatched")

        operation_key = _turn_operation_key(
            self,
            action=action,
            intent=intent,
            goal=goal,
            body_name=body_name,
            reason=reason,
        )
        result = await _execute_turn_once(session, operation_key, run_operation)

        if action == "status":
            # Make the read-only nature explicit in the model-visible receipt.
            # This prevents an inactive status check from being mistaken for a
            # successful entrance into the world.
            result = dict(result)
            result["status_query_only"] = True
            result["started_by_this_call"] = False
            if not bool(result.get("active")):
                result["start_hint"] = (
                    "status 不会启动身体；要进入 Ayer 已开放的共享世界，请调用 "
                    "nucleus_minecraft(action='start', body_name='bot', "
                    "goal='和 Ayer 一起玩')"
                )

        # A successful status query may legitimately report an inactive body;
        # inactivity is state, not a tool execution failure. Other actions
        # expose the session result through the standard BaseTool contract.
        success = action == "status" or bool(result.get("success"))
        return success, json.dumps(result, ensure_ascii=False, default=str)


MINECRAFT_TOOLS = [LifeEngineMinecraftTool]

__all__ = ["MINECRAFT_TOOLS", "LifeEngineMinecraftTool"]
