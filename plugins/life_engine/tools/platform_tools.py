"""统一平台操作工具。

一个工具覆盖所有平台（QQ / 飞书）的操作能力。
爱莉通过对应的 skill（qq_actions / feishu_actions）查阅可用操作清单，
然后调用本工具执行。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from src.app.plugin_system.api.adapter_api import send_adapter_command
from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool


logger = log_api.get_logger("life_engine.platform_tools")

# 默认 QQ 适配器签名
_DEFAULT_QQ_ADAPTER_SIGN = "napcat_adapter:adapter:napcat_adapter"

# QQ 危险操作黑名单
_QQ_BLOCKED_ACTIONS: frozenset[str] = frozenset({
    "set_group_leave",
    "delete_friend",
    "set_group_kick",
    "get_cookies",
    "get_csrf_token",
    "get_credentials",
})

# 飞书危险操作黑名单
_FEISHU_BLOCKED_ACTIONS: frozenset[str] = frozenset({
    "delete_chat",
    "remove_all_members",
})


class PlatformActionTool(BaseTool):
    """跨平台统一操作接口：一个工具覆盖 QQ + 飞书全部能力。"""

    tool_name: str = "platform_action"
    tool_description: str = (
        "跨平台统一操作接口。通过 platform + action + params 调用任意已支持的平台 API。\n\n"
        "使用前请先查阅对应平台的 skill：\n"
        "- QQ: get_skill('qq_actions')\n"
        "- 飞书: get_skill('feishu_actions')\n\n"
        "参数：\n"
        "- `platform`: 目标平台（'qq' 或 'feishu'，默认 'qq'）\n"
        "- `action`: API 动作名（必填，参见对应 skill）\n"
        "- `params`: 参数字典（JSON 对象，按操作要求填写）\n\n"
        "举例：\n"
        "- QQ 群签到: platform='qq', action='set_group_sign', params={'group_id': 123}\n"
        "- 飞书发消息: platform='feishu', action='send_text', params={'chat_id': 'oc_xxx', 'text': '你好'}"
    )
    chatter_allow: list[str] = ["chat", "life_engine_internal"]

    async def execute(
        self,
        action: Annotated[str, "API 动作名（参见对应平台 skill）"],
        params: Annotated[dict[str, Any] | str, "操作参数（JSON 对象或 JSON 字符串）"] | None = None,
        platform: Annotated[str, "目标平台: 'qq' 或 'feishu'"] = "qq",
    ) -> tuple[bool, str | dict]:
        """执行平台 API 操作。"""

        # 解析 params
        parsed_params: dict[str, Any] = {}
        if params is not None:
            if isinstance(params, str):
                try:
                    parsed_params = json.loads(params)
                except (json.JSONDecodeError, TypeError):
                    return False, f"params 不是有效的 JSON: {params[:200]}"
            elif isinstance(params, dict):
                parsed_params = params
            else:
                return False, "params 必须是 JSON 对象或 JSON 字符串"

        action_name = action.strip()
        if not action_name:
            return False, "action 不能为空"

        platform_name = platform.strip().lower()
        if platform_name not in ("qq", "feishu"):
            return False, f"不支持的平台: {platform}（支持: qq, feishu）"

        if platform_name == "qq":
            return await self._execute_qq(action_name, parsed_params)
        return await self._execute_feishu(action_name, parsed_params)

    # ------------------------------------------------------------------
    # QQ 路径：send_adapter_command 透传
    # ------------------------------------------------------------------

    async def _execute_qq(self, action: str, params: dict[str, Any]) -> tuple[bool, str | dict]:
        if action in _QQ_BLOCKED_ACTIONS:
            return False, f"操作 '{action}' 已被安全策略禁止（不可逆操作需专门决策流程）"

        adapter_sign = self._get_qq_adapter_sign()
        if not adapter_sign:
            return False, "QQ 适配器未配置或不可用"

        try:
            response = await send_adapter_command(
                adapter_sign=adapter_sign,
                command_name=action,
                command_data=params,
                timeout=15.0,
            )
        except Exception as exc:
            logger.error(f"[platform_action:qq] 执行失败: {action}, error={exc}")
            return False, f"执行失败: {exc}"

        if isinstance(response, dict):
            status = response.get("status", "")
            if status == "ok" or response.get("retcode") == 0:
                data = response.get("data", response)
                logger.info(f"[platform_action:qq] 成功: {action}")
                return True, data if data else {"status": "ok"}
            msg = response.get("message") or response.get("msg") or str(response)
            logger.warning(f"[platform_action:qq] 失败: {action} -> {msg}")
            return False, f"操作失败: {msg}"

        return True, str(response)

    # ------------------------------------------------------------------
    # 飞书路径：直接调用适配器 execute_action
    # ------------------------------------------------------------------

    async def _execute_feishu(self, action: str, params: dict[str, Any]) -> tuple[bool, str | dict]:
        if action in _FEISHU_BLOCKED_ACTIONS:
            return False, f"操作 '{action}' 已被安全策略禁止"

        try:
            from plugins.feishu_adapter.adapter import get_feishu_adapter
            adapter = get_feishu_adapter()
        except Exception as exc:
            return False, f"飞书适配器加载失败: {exc}"

        if adapter is None:
            return False, "飞书适配器未启动"

        try:
            result = await adapter.execute_action(action, params)
        except Exception as exc:
            logger.error(f"[platform_action:feishu] 执行失败: {action}, error={exc}")
            return False, f"执行失败: {exc}"

        if isinstance(result, dict):
            status = result.get("status", "")
            if status == "ok":
                data = result.get("data", result)
                logger.info(f"[platform_action:feishu] 成功: {action}")
                return True, data if data else {"status": "ok"}
            msg = result.get("message") or str(result)
            logger.warning(f"[platform_action:feishu] 失败: {action} -> {msg}")
            return False, f"操作失败: {msg}"

        return True, str(result)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _get_qq_adapter_sign(self) -> str:
        """从配置获取 QQ 适配器签名。"""
        try:
            from ..service.registry import get_life_engine_service
            service = get_life_engine_service()
            if service:
                cfg = getattr(service, "config", None)
                if cfg:
                    history_cfg = getattr(cfg, "chat_history", None)
                    if history_cfg:
                        sign = getattr(history_cfg, "adapter_signature", "")
                        if sign:
                            return str(sign)
        except Exception:
            pass
        return _DEFAULT_QQ_ADAPTER_SIGN


PLATFORM_TOOLS = [
    PlatformActionTool,
]

__all__ = [
    "PlatformActionTool",
    "PLATFORM_TOOLS",
]
