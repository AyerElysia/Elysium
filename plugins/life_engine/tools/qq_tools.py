"""QQ 统一操作工具。

通过 adapter_command 透传机制，一个工具覆盖 NapCat 适配器的全部 API。
爱莉通过 skill「qq_actions」查阅可用操作清单，然后调用本工具执行。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from src.app.plugin_system.api.adapter_api import send_adapter_command
from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool


logger = log_api.get_logger("life_engine.qq_tools")

# 默认适配器签名（与 chat_history_tools 保持一致）
_DEFAULT_ADAPTER_SIGN = "napcat_adapter:adapter:napcat_adapter"

# 危险操作黑名单：禁止通过本工具执行
_BLOCKED_ACTIONS: frozenset[str] = frozenset({
    "set_group_leave",       # 退群/解散
    "delete_friend",         # 删好友
    "set_group_kick",        # 踢人（需通过专门决策）
    "get_cookies",           # 凭证安全
    "get_csrf_token",
    "get_credentials",
})


class QQActionTool(BaseTool):
    """QQ 统一操作接口：一个工具覆盖所有 QQ 平台能力。"""

    tool_name: str = "qq_action"
    tool_description: str = (
        "QQ 平台统一操作接口。通过 action + params 调用任意已支持的 QQ API。\n\n"
        "使用前请先通过 get_skill('qq_actions') 查看完整操作清单和参数说明。\n"
        "常见操作举例：\n"
        "- 查看群公告: action='_get_group_notice', params={'group_id': 123456}\n"
        "- 发送群公告: action='_send_group_notice', params={'group_id': 123456, 'content': '...'}\n"
        "- 群签到: action='set_group_sign', params={'group_id': 123456}\n"
        "- 戳一戳: action='send_poke', params={'user_id': 111, 'group_id': 123456}\n"
        "- 设置群名片: action='set_group_card', params={'group_id': 123456, 'user_id': 111, 'card': '昵称'}\n\n"
        "参数：\n"
        "- `action`: API 动作名（必填，参见 skill 清单）\n"
        "- `params`: 参数字典（JSON 对象，按操作要求填写）"
    )
    chatter_allow: list[str] = ["chat", "life_engine_internal"]

    async def execute(
        self,
        action: Annotated[str, "API 动作名，如 '_get_group_notice'、'send_poke'、'set_group_sign'"],
        params: Annotated[dict[str, Any] | str, "操作参数（JSON 对象或 JSON 字符串）"] | None = None,
    ) -> tuple[bool, str | dict]:
        """执行 QQ API 操作。"""

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

        # 安全检查
        if action_name in _BLOCKED_ACTIONS:
            return False, (
                f"操作 '{action_name}' 已被安全策略禁止。"
                "这类操作涉及不可逆后果，需要通过专门的决策流程执行。"
            )

        # 获取适配器签名
        adapter_sign = self._get_adapter_sign()
        if not adapter_sign:
            return False, "适配器未配置或不可用"

        # 执行
        try:
            response = await send_adapter_command(
                adapter_sign=adapter_sign,
                command_name=action_name,
                command_data=parsed_params,
                timeout=15.0,
            )
        except Exception as exc:
            logger.error(f"[qq_action] 执行失败: {action_name}, error={exc}")
            return False, f"执行失败: {exc}"

        # 解析响应
        if isinstance(response, dict):
            status = response.get("status", "")
            if status == "ok" or response.get("retcode") == 0:
                data = response.get("data", response)
                logger.info(f"[qq_action] 成功: {action_name}")
                return True, data if data else {"status": "ok"}
            else:
                msg = response.get("message") or response.get("msg") or str(response)
                logger.warning(f"[qq_action] 失败: {action_name} -> {msg}")
                return False, f"操作失败: {msg}"

        return True, str(response)

    def _get_adapter_sign(self) -> str:
        """从配置获取适配器签名。"""
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
        return _DEFAULT_ADAPTER_SIGN


QQ_TOOLS = [
    QQActionTool,
]

__all__ = [
    "QQActionTool",
    "QQ_TOOLS",
]
