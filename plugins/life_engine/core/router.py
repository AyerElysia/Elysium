"""对话路由器：判断此刻是否应该开口。

从 default_chatter 迁移而来，作为 life_engine 自有能力。
"""

from __future__ import annotations

import time
from typing import Protocol, TypedDict

import json_repair

from src.core.config import get_core_config
from src.core.models.stream import ChatStream
from src.core.prompt import get_prompt_manager
from src.kernel.logger import Logger
from src.kernel.llm import LLMPayload, ROLE, Text
from src.kernel.llm import LLMRequest
from src.kernel.llm.token_counter import count_text_tokens


# ---------------------------------------------------------------------------
# 熔断器：本地 router 模型连续失败后自动跳过，避免每条消息都等连接超时。
# ---------------------------------------------------------------------------
_CIRCUIT_FAILURE_THRESHOLD = 3       # 连续失败 N 次后熔断
_CIRCUIT_COOLDOWN_SECONDS = 120.0    # 熔断后冷却时间（秒）

_circuit_consecutive_failures = 0
_circuit_open_until: float = 0.0     # timestamp，在此之前跳过本地 router


def _circuit_is_open() -> bool:
    """熔断器是否处于打开状态（应跳过本地 router）。"""
    if _circuit_open_until <= 0:
        return False
    if time.monotonic() >= _circuit_open_until:
        # 冷却到期，进入半开状态：允许一次探测
        return False
    return True


def _circuit_record_success() -> None:
    """记录成功，重置熔断器。"""
    global _circuit_consecutive_failures, _circuit_open_until
    _circuit_consecutive_failures = 0
    _circuit_open_until = 0.0


def _circuit_record_failure() -> None:
    """记录失败，必要时打开熔断器。"""
    global _circuit_consecutive_failures, _circuit_open_until
    _circuit_consecutive_failures += 1
    if _circuit_consecutive_failures >= _CIRCUIT_FAILURE_THRESHOLD:
        _circuit_open_until = time.monotonic() + _CIRCUIT_COOLDOWN_SECONDS


class SubAgentDecision(TypedDict):
    """路由判定结果。"""

    reason: str
    should_respond: bool


class SupportsRequestCreation(Protocol):
    """支持创建 LLM 请求的最小能力集合。"""

    def create_request(
        self,
        task: str = "expression",
        sub_task: str = "",
        *,
        with_reminder: str = "",
    ) -> LLMRequest: ...


_DEFAULT_ROUTER_FALLBACK_PROMPT = """你是当前主体的对话路由器。
你的任务不是按硬规则拦截消息，而是站在主体自己的视角，结合近期聊天记录和新消息，判断此刻要不要开口。

# 路由原则
- 不要使用"艾特 ID 是否匹配""是否私聊""是否关键词命中"这类机械硬规则替主体做决定。
- @、昵称、平台 ID、群名片、表情、图片摘要都只是上下文线索，不是绝对规则。
- 你只需要判断：如果我是这个主体，看到近期聊天和这批新消息，此刻开口是否自然、必要、有生命力。
- 可以选择不说话：例如话还没说完、别人正在对话、接话会打断氛围、或此刻沉默更合适。
- 也可以选择开口：例如对方明显在叫我、话题与我有关、情绪需要被接住、或我自然想参与。
- 不要替主体生成具体回复内容；只输出是否把这批消息路由给表达层继续处理。

# 输出格式
请务必返回 JSON 格式，如下所示：
```json
{{
    "reason": "简短的判定理由",
    "should_respond": true/false
}}
```
"""


class _SafeFormatDict(dict[str, str]):
    """安全的 format_map 字典，缺失字段返回空字符串。"""

    def __missing__(self, key: str) -> str:
        return ""


def _safe_count_tokens(text: str, model_identifier: str) -> int:
    """安全计算文本 token 数量，失败时返回 0。"""
    try:
        return count_text_tokens(text, model_identifier=model_identifier)
    except Exception:
        return 0


def _trim_text_suffix_by_budget(
    text: str,
    model_identifier: str,
    token_budget: int,
) -> str:
    """保留文本末尾，使其不超过 token 预算。"""
    if not text:
        return text
    total = _safe_count_tokens(text, model_identifier)
    if total == 0 or total <= token_budget:
        return text

    left, right = 0, len(text) - 1
    best = text[-512:]
    while left <= right:
        middle = (left + right) // 2
        suffix = text[middle:]
        token_count = _safe_count_tokens(suffix, model_identifier)
        if token_count == 0 or token_count > token_budget:
            left = middle + 1
            continue
        best = suffix
        right = middle - 1

    return best.strip()


def _fit_unreads_to_sub_agent_budget(
    request: LLMRequest,
    unreads_text: str,
) -> str:
    """将未读消息压缩到模型上下文可控的 token 预算内。"""
    model_set = getattr(request, "model_set", None)
    if not isinstance(model_set, list) or not model_set:
        return unreads_text

    first_model = model_set[0]
    if not isinstance(first_model, dict):
        return unreads_text

    model_identifier = first_model.get("model_identifier")
    if not isinstance(model_identifier, str) or not model_identifier:
        return unreads_text

    max_context = first_model.get("max_context")
    if isinstance(max_context, int) and max_context > 0:
        # 未读消息占 max_context 的 ~35%（2048 → 716 token；大模型 → 上限 8000）
        token_budget = min(max(256, max_context * 35 // 100), 8000)
    else:
        token_budget = 6000

    return _trim_text_suffix_by_budget(unreads_text, model_identifier, token_budget)


def _fit_system_prompt_to_task(
    sub_prompt: str,
    request: LLMRequest,
    logger: Logger,
    task: str,
) -> str:
    """按模型上下文预算裁剪系统提示词（保留末尾路由指令部分）。

    SOUL.md + USER.md + memory 加在一起可能超过万字，远超小模型上下文。
    预算按比例分配：系统提示词占 max_context 的 ~30%，其余留给
    历史、未读、输出和安全余量。
    对前沿大模型（agent，max_context 通常 ≥128k）预算极大，不会裁剪。
    """
    model_set = getattr(request, "model_set", None)
    if not isinstance(model_set, list) or not model_set:
        return sub_prompt
    first_model = model_set[0]
    if not isinstance(first_model, dict):
        return sub_prompt
    model_identifier = first_model.get("model_identifier", "")
    max_context = first_model.get("max_context")
    max_context = max_context if isinstance(max_context, int) and max_context > 0 else 2048

    # 系统提示词预算 = max_context 的 30%（小模型下约 600 token，大模型下极大不会裁剪）
    # 剩余 70% 留给：history(~20%) + unreads(~35%) + output(80) + scaffold(~100) + safety(~5%)
    sys_budget = max(400, max_context * 3 // 10)

    trimmed = _trim_text_suffix_by_budget(sub_prompt, model_identifier, sys_budget)
    if len(trimmed) < len(sub_prompt):
        logger.info(
            f"Router[{task}] 系统提示词已截断: {len(sub_prompt)} -> {len(trimmed)} 字符"
            f"（上下文 {max_context} token，系统提示词预算 {sys_budget} token）"
        )
    return trimmed


async def route_should_respond(
    chatter: SupportsRequestCreation,
    logger: Logger,
    unreads_text: str,
    chat_stream: ChatStream,
    history_text: str = "",
    prefix_prompt: str = "",
    fallback_prompt: str | None = None,
) -> SubAgentDecision:
    """执行路由判断并返回 should_respond 结果。

    模型优先级：本地小模型 router 任务（低延迟）→ sub_actor（回退）。
    主体性提示词与人设前缀保持不变：路由始终站在主体自己的视角判断“此刻要不要开口”，
    而非机械硬规则。换成本地小模型只是去掉“远程前沿大模型”的延迟，不牺牲主体性。
    """
    nickname = get_core_config().personality.nickname
    bot_id = chat_stream.bot_id or ""
    bot_id_section = f"它的 QQ 号是 {bot_id}。\n" if bot_id else ""
    tmpl = get_prompt_manager().get_template("default_chatter_router_prompt")
    if tmpl is None:
        tmpl = get_prompt_manager().get_template("default_chatter_sub_agent_prompt")
    if tmpl:
        sub_prompt = (
            await tmpl
            .set("nickname", nickname)
            .set("bot_id", bot_id)
            .set("bot_id_section", bot_id_section)
            .build()
        )
    else:
        prompt_template = fallback_prompt or _DEFAULT_ROUTER_FALLBACK_PROMPT
        sub_prompt = prompt_template.format_map(
            _SafeFormatDict(
                {
                    "nickname": nickname,
                    "bot_id": bot_id,
                    "bot_id_section": bot_id_section,
                    "personality_core_section": "",
                    "personality_side_section": "",
                }
            )
        )

    prefix_text = str(prefix_prompt or "").strip()
    if prefix_text:
        sub_prompt = f"{prefix_text}\n\n{sub_prompt}"

    # 上下文优化：路由只需近期语境。history 字符预算按 max_context 的 ~15% 估算
    # （中文 1 字符 ≈ 1-1.5 token）。2048 上下文 → 300 字符；大模型 → 上限 1500。
    _HISTORY_CHAR_BUDGET = max(200, min(1500, 2048 * 15 // 100))  # 固定用 router 的 2048 算
    fitted_history = (
        history_text.strip()[-_HISTORY_CHAR_BUDGET:] if history_text.strip() else ""
    )
    fitted_unreads = unreads_text

    # 依次尝试：本地 router 小模型（低延迟）→ agent（回退）
    # 熔断器：本地模型连续失败后自动跳过，避免每条消息都等连接超时
    last_error: str = ""
    tasks_to_try = ["router", "agent"]
    if _circuit_is_open():
        tasks_to_try = ["agent"]
        logger.debug("Router 熔断器打开，跳过本地 router 模型")

    for task in tasks_to_try:
        try:
            request = chatter.create_request(
                task,
                "router",
                with_reminder="agent",
            )
        except (ValueError, KeyError):
            last_error = f"{task} 未配置"
            continue

        if not getattr(request, "model_set", None):
            last_error = f"{task} 无可用模型"
            continue

        # 按该任务模型的上下文预算收紧未读消息
        fitted_unreads_task = _fit_unreads_to_sub_agent_budget(request, unreads_text)
        if len(fitted_unreads_task) < len(fitted_unreads):
            logger.info(
                f"Router[{task}] 输入已截断: {len(fitted_unreads)} -> {len(fitted_unreads_task)} 字符"
            )
        parts: list[str] = []
        if fitted_history:
            parts.append(f"<chat_history>\n{fitted_history.strip()}\n</chat_history>")
        parts.append(f"<new_messages>\n{fitted_unreads_task}\n</new_messages>")
        parts.append("请只判断这批新消息是否应路由给表达层继续处理。")

        task_sub_prompt = _fit_system_prompt_to_task(sub_prompt, request, logger, task)
        user_text = "\n\n".join(parts)

        # 最终硬守卫：token 估算器对中文有系统性低估，逐部件裁剪后仍可能溢出。
        # 用保守比率（1 字符 ≈ 1.5 token）做总量检查，超了直接砍 system prompt。
        _model_set_guard = getattr(request, "model_set", None)
        _max_ctx = 2048
        if isinstance(_model_set_guard, list) and _model_set_guard and isinstance(_model_set_guard[0], dict):
            _mc_val = _model_set_guard[0].get("max_context")
            if isinstance(_mc_val, int) and _mc_val > 0:
                _max_ctx = _mc_val
        # 保守 token 估计：中文 1 字符 ≈ 1.5 token
        _user_tokens_est = int(len(user_text) * 1.5)
        _output_reserve = 80
        _sys_budget_hard = _max_ctx - _user_tokens_est - _output_reserve - 100  # 100 最终安全余量
        if _sys_budget_hard < 100:
            _sys_budget_hard = 100
        _sys_tokens_est = int(len(task_sub_prompt) * 1.5)
        if _sys_tokens_est > _sys_budget_hard:
            # 按字符比率反算允许的字符数
            _allowed_chars = int(_sys_budget_hard / 1.5)
            task_sub_prompt = task_sub_prompt[-_allowed_chars:].strip()
            logger.info(
                f"Router[{task}] 硬守卫截断系统提示词 -> {len(task_sub_prompt)} 字符"
                f"（max_ctx={_max_ctx}, user_est={_user_tokens_est}, sys_hard={_sys_budget_hard}）"
            )

        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(task_sub_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_text)))

        try:
            response = await request.send(stream=False)
            await response
        except Exception as error:  # noqa: BLE001
            last_error = f"{task} 调用失败: {error}"
            logger.warning(f"Router {last_error}，尝试下一个任务")
            if task == "router":
                _circuit_record_failure()
            continue

        content = response.message
        if task == "router":
            _circuit_record_success()
        if not content or not content.strip():
            logger.warning(f"Router[{task}] 返回了空内容，默认进行响应")
            return {"should_respond": True, "reason": "模型未返回判断内容"}

        try:
            result = json_repair.loads(content)
            if isinstance(result, dict):
                return {
                    "should_respond": bool(result.get("should_respond", True)),
                    "reason": result.get("reason", "未提供理由"),
                }
        except Exception as error:  # noqa: BLE001
            logger.debug(f"Router JSON 解析失败: {error} | 内容: {content[:500]}")

        logger.warning(f"Router[{task}] 无法找到有效的 JSON 结构: {content[:200]}...")
        return {"should_respond": True, "reason": "解析 JSON 失败，默认响应"}

    logger.error(f"Router 所有任务均不可用: {last_error}，默认响应")
    return {"should_respond": True, "reason": f"路由模型不可用: {last_error}"}
