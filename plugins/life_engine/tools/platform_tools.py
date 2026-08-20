"""统一平台操作工具。

一个工具覆盖所有平台（QQ / 飞书）的操作能力。
爱莉通过 action='help' 查阅对应平台的能力清单（渐进式披露），
然后调用本工具执行。

飞书路径通过 lark-cli 命令行执行，覆盖 200+ 命令 / 2500+ API 端点。
"""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import Annotated, Any, Self

from src.app.plugin_system.api import log_api
from src.app.plugin_system.api.adapter_api import send_adapter_command
from src.app.plugin_system.base import BaseTool

from ._utils import _get_workspace


logger = log_api.get_logger("life_engine.platform_tools")

# lark-cli 二进制路径
_LARK_CLI_BIN = "lark-cli"

# 飞书命令超时（秒）
_FEISHU_TIMEOUT = 30.0

# 默认 QQ 适配器签名
_DEFAULT_QQ_ADAPTER_SIGN = "napcat_adapter:adapter:napcat_adapter"

# QQ 操作超时（秒）—— 必须大于适配器内部 API 调用超时，
# 否则外层先超时会导致实际成功的操作被报告为失败。
_QQ_TIMEOUT = 30.0

# QQ 危险操作黑名单
_QQ_BLOCKED_ACTIONS: frozenset[str] = frozenset({
    "set_group_leave",
    "delete_friend",
    "set_group_kick",
    "get_cookies",
    "get_csrf_token",
    "get_credentials",
})

# 飞书禁止的命令关键词（安全策略）
_FEISHU_BLOCKED_PATTERNS: tuple[str, ...] = (
    "--yes",           # 禁止自动确认高危操作
    "auth login",      # 禁止重新认证
    "config set",      # 禁止修改配置
    "config init",     # 禁止重置配置
)

_FEISHU_USER_ACTION_ERROR_TYPES = frozenset({"authentication", "authorization"})
_FEISHU_USER_ACTION_ERROR_SUBTYPES = frozenset(
    {
        "token_missing",
        "permission_denied",
        "insufficient_scope",
    }
)


class _PlatformActionResult(str):
    """Human-readable platform result carrying a content-free outcome code."""

    def __new__(
        cls,
        value: str,
        *,
        technical_outcome: str = "",
    ) -> Self:
        instance = super().__new__(cls, value)
        instance.technical_outcome = str(technical_outcome or "")
        return instance


class PlatformActionTool(BaseTool):
    """跨平台统一操作接口：一个工具覆盖 QQ + 飞书全部能力。"""

    tool_name: str = "platform_action"
    tool_description: str = (
        "跨平台统一操作接口。通过 platform + action + params 调用任意已支持的平台 API。\n\n"
        "★ 第一次用某个平台前，先查能力清单：\n"
        "  action='help', platform='qq'     → 查看 QQ 可用操作\n"
        "  action='help', platform='feishu' → 查看飞书可用操作\n\n"
        "参数：\n"
        "- `platform`: 目标平台（'qq' 或 'feishu'，默认 'qq'）\n"
        "- `action`: API 动作名（必填，参见 help 返回的清单）\n"
        "- `params`: 参数字典（JSON 对象，按操作要求填写）\n\n"
        "举例：\n"
        "- 查看QQ能力: action='help', platform='qq'\n"
        "- QQ 群签到: platform='qq', action='set_group_sign', params={'group_id': 123}\n"
        "- 飞书发消息: platform='feishu', action='im +messages-send --chat-id oc_xxx --text 你好'\n"
        "- 飞书查群列表: platform='feishu', action='im +chat-list'"
    )
    chatter_allow: list[str] = ["life_chatter", "life_engine_internal"]

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
                if not params.strip():
                    # 空串视为无参数：放行 help/docs/list 等免参动作，
                    # 避免自救查询被参数校验挡死（真实事故 2026-08-20）。
                    parsed_params = {}
                else:
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

        # 能力清单查询（渐进式披露）
        if action_name.lower() in ("help", "docs", "list"):
            return self._read_skill_doc(platform_name)

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
                timeout=_QQ_TIMEOUT,
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
    # 飞书路径：lark-cli subprocess
    # ------------------------------------------------------------------

    async def _execute_feishu(self, action: str, params: dict[str, Any]) -> tuple[bool, str | dict]:
        """通过 lark-cli 执行飞书操作。

        action 为 lark-cli 命令字符串（不含 'lark-cli' 前缀），例如：
          'im +messages-send --chat-id oc_xxx --text 你好'
          'calendar +agenda'
          'docs +fetch --url https://xxx'
        params 通常为空，保留兼容。
        """
        # 安全检查
        action_lower = action.lower()
        for pattern in _FEISHU_BLOCKED_PATTERNS:
            if pattern in action_lower:
                return False, _PlatformActionResult(
                    f"命令包含被禁止的内容 '{pattern}'（安全策略）",
                    technical_outcome="policy_blocked",
                )

        # 构建命令
        try:
            cmd_parts = [_LARK_CLI_BIN] + shlex.split(action)
        except ValueError as exc:
            return False, f"命令解析失败: {exc}"

        # lark-cli 默认输出 JSON，无需额外指定

        logger.info(f"[platform_action:feishu] 执行: {' '.join(cmd_parts)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_FEISHU_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()  # type: ignore[union-attr]
            return False, f"命令超时（{_FEISHU_TIMEOUT}s）"
        except FileNotFoundError:
            return False, _PlatformActionResult(
                "lark-cli 未安装或不在 PATH 中",
                technical_outcome="tool_unavailable",
            )
        except Exception as exc:
            logger.error(f"[platform_action:feishu] 执行异常: {exc}")
            return False, f"执行失败: {exc}"

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:  # type: ignore[union-attr]
            err_msg = stderr_text or stdout_text or f"exit code {proc.returncode}"
            logger.warning(f"[platform_action:feishu] 失败: {err_msg[:300]}")
            return False, _PlatformActionResult(
                f"命令失败: {err_msg[:500]}",
                technical_outcome=self._classify_feishu_error(err_msg),
            )

        # 尝试解析 JSON 输出
        if stdout_text:
            try:
                data = json.loads(stdout_text)
                logger.info("[platform_action:feishu] 成功")
                return True, data
            except (json.JSONDecodeError, TypeError):
                pass
            # 非 JSON 输出直接返回文本
            return True, stdout_text[:2000]

        return True, {"status": "ok"}

    @staticmethod
    def _classify_feishu_error(raw_error: str) -> str:
        """Classify stable lark-cli JSON errors without guessing from prose."""

        try:
            payload = json.loads(raw_error)
        except (json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        if not isinstance(error, dict):
            return ""

        error_type = str(error.get("type", "") or "").strip().lower()
        error_subtype = str(error.get("subtype", "") or "").strip().lower()
        if (
            error_type in _FEISHU_USER_ACTION_ERROR_TYPES
            and error_subtype in _FEISHU_USER_ACTION_ERROR_SUBTYPES
        ):
            return "user_action_required"
        if error_type == "validation" and error_subtype == "invalid_argument":
            return "invalid_argument"
        return ""

    # ------------------------------------------------------------------
    # 能力清单（渐进式披露）
    # ------------------------------------------------------------------

    def _read_skill_doc(self, platform: str) -> tuple[bool, str | dict]:
        """读取平台对应的能力清单文档（SKILL.md）。"""
        workspace = _get_workspace(self.plugin)
        doc_path = workspace / "skills" / f"{platform}_actions" / "SKILL.md"
        if not doc_path.exists():
            return False, (
                f"没有找到 {platform} 的能力清单文档。\n"
                f"期望路径: skills/{platform}_actions/SKILL.md"
            )
        try:
            content = doc_path.read_text(encoding="utf-8")
            return True, content
        except Exception as exc:
            return False, f"读取能力清单失败: {exc}"

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
