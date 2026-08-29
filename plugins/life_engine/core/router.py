"""Conversation router: decide whether new messages reach expression."""

from __future__ import annotations

import time
from typing import Protocol, TypedDict

import json_repair

from src.core.config import get_core_config
from src.core.models.stream import ChatStream
from src.core.prompt import get_prompt_manager
from src.kernel.llm import ROLE, LLMPayload, LLMRequest, Text
from src.kernel.llm.token_counter import count_text_tokens
from src.kernel.logger import Logger

_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_COOLDOWN_SECONDS = 120.0
_HISTORY_CHAR_BUDGET = 3000

_circuit_consecutive_failures = 0
_circuit_open_until = 0.0


class SubAgentDecision(TypedDict):
    """Strict transport contract returned by the router model."""

    reason: str
    should_respond: bool


class SupportsRequestCreation(Protocol):
    """Minimum request-creation capability used by the router."""

    def create_request(
        self,
        task: str = "expression",
        sub_task: str = "",
        *,
        with_reminder: str = "",
    ) -> LLMRequest: ...


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def _circuit_is_open() -> bool:
    if _circuit_open_until <= 0:
        return False
    return time.monotonic() < _circuit_open_until


def _circuit_record_success() -> None:
    global _circuit_consecutive_failures, _circuit_open_until
    _circuit_consecutive_failures = 0
    _circuit_open_until = 0.0


def _circuit_record_failure() -> None:
    global _circuit_consecutive_failures, _circuit_open_until
    _circuit_consecutive_failures += 1
    if _circuit_consecutive_failures >= _CIRCUIT_FAILURE_THRESHOLD:
        _circuit_open_until = time.monotonic() + _CIRCUIT_COOLDOWN_SECONDS


def _safe_count_tokens(text: str, model_identifier: str) -> int:
    try:
        return count_text_tokens(text, model_identifier=model_identifier)
    except Exception:  # noqa: BLE001 - token accounting must never block routing
        return 0


def _transport_candidate_key(model: object) -> tuple[str, str, str] | None:
    """Return a stable transport identity without exposing it to logs."""

    if not isinstance(model, dict):
        return None
    return (
        str(model.get("api_provider") or "").strip().lower(),
        str(model.get("base_url") or "").strip().rstrip("/").lower(),
        str(model.get("model_identifier") or "").strip(),
    )


def _trim_text_suffix_by_budget(
    text: str,
    model_identifier: str,
    token_budget: int,
) -> str:
    """Keep a recent transport window without changing authoritative history."""

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


def _first_model_limits(request: LLMRequest) -> tuple[str, int] | None:
    model_set = getattr(request, "model_set", None)
    if not isinstance(model_set, list) or not model_set:
        return None
    first_model = model_set[0]
    if not isinstance(first_model, dict):
        return None
    model_identifier = first_model.get("model_identifier")
    if not isinstance(model_identifier, str) or not model_identifier:
        return None
    max_context = first_model.get("max_context")
    if not isinstance(max_context, int) or max_context <= 0:
        max_context = 2048
    return model_identifier, max_context


def _fit_unreads_to_budget(request: LLMRequest, unreads_text: str) -> str:
    limits = _first_model_limits(request)
    if limits is None:
        return unreads_text
    model_identifier, max_context = limits
    token_budget = min(max(256, max_context * 35 // 100), 8000)
    return _trim_text_suffix_by_budget(
        unreads_text,
        model_identifier,
        token_budget,
    )


def _fit_system_prompt_to_budget(
    prompt: str,
    request: LLMRequest,
    logger: Logger,
    task: str,
) -> str:
    limits = _first_model_limits(request)
    if limits is None:
        return prompt
    model_identifier, max_context = limits
    token_budget = max(400, max_context * 3 // 10)
    fitted = _trim_text_suffix_by_budget(prompt, model_identifier, token_budget)
    if len(fitted) < len(prompt):
        logger.info(
            f"Router[{task}] 系统提示词传输窗口已收缩: "
            f"{len(prompt)} -> {len(fitted)} 字符"
        )
    return fitted


def _parse_decision(content: str) -> SubAgentDecision | None:
    """Accept only a real bool and a non-empty reason; never coerce strings."""

    try:
        result = json_repair.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(result, dict):
        return None
    should_respond = result.get("should_respond")
    reason = result.get("reason")
    if not isinstance(should_respond, bool):
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return {
        "should_respond": should_respond,
        "reason": reason.strip(),
    }


def _fallback_prompt(nickname: str, bot_id: str) -> str:
    bot_id_section = f"她当前的通道账号是 {bot_id}。\n" if bot_id else ""
    return f"""你是 {nickname} 的对话路由器。你的任务只是站在她的视角判断：此刻要不要把新消息交给表达层。

{bot_id_section}这不是关键词、艾特、平台或私聊规则判断。结合近期聊天与新消息，判断此刻开口是否自然、必要、有生命力。唯一例外：有消息直接 @ 她的账号或点名她时，等于有人在对她本人说话，这类消息必须交给表达层，由她自己决定怎么接。除此之外你可以选择沉默，也可以选择让表达层继续；不要写具体回复，不要替表达层决定她最终说什么。

只返回 JSON：
{{
  "reason": "简短但具体的判断理由",
  "should_respond": true
}}
"""


async def _build_router_prompt(
    *,
    nickname: str,
    bot_id: str,
    projection_prompt: str,
    fallback_prompt: str | None,
) -> str:
    template = get_prompt_manager().get_template("default_chatter_router_prompt")
    if template is None:
        template = get_prompt_manager().get_template("default_chatter_sub_agent_prompt")
    if template is not None:
        prompt = (
            await template.set("nickname", nickname)
            .set("bot_id", bot_id)
            .set("bot_id_section", f"她当前的通道账号是 {bot_id}。\n" if bot_id else "")
            .set("personality_core_section", "")
            .set("personality_side_section", "")
            .build()
        )
    else:
        if fallback_prompt:
            prompt = fallback_prompt.format_map(
                _SafeFormatDict(
                    nickname=nickname,
                    bot_id=bot_id,
                    bot_id_section=(
                        f"她当前的通道账号是 {bot_id}。\n" if bot_id else ""
                    ),
                    personality_core_section="",
                    personality_side_section="",
                )
            )
        else:
            prompt = _fallback_prompt(nickname, bot_id)

    projection = str(projection_prompt or "").strip()
    if not projection:
        return prompt
    return (
        "以下是由权威人格/记忆文件生成的可重建路由投影。"
        "它只帮助导航，不是新的记忆或事实来源；如有歧义，不得自行补造。\n\n"
        f"{projection}\n\n---\n\n{prompt}"
    )


async def route_should_respond(
    chatter: SupportsRequestCreation,
    logger: Logger,
    unreads_text: str,
    chat_stream: ChatStream,
    history_text: str = "",
    prefix_prompt: str = "",
    fallback_prompt: str | None = None,
) -> SubAgentDecision:
    """Route through cloud-first tasks and preserve work on degraded failure.

    ``prefix_prompt`` is intentionally retained for API compatibility, but its
    only valid caller is the versioned router-context projection.  Full
    SOUL/USER/MEMORY text belongs to the main expression request, not here.
    """

    # CoreConfig no longer requires a parallel personality section. Keep a
    # compatibility read for older test/config providers, then use the stream
    # transport nickname as a non-authoritative routing label.
    config = get_core_config()
    personality = getattr(config, "personality", None)
    nickname = str(
        getattr(personality, "nickname", "")
        or getattr(chat_stream, "bot_nickname", "")
        or "爱莉"
    )
    bot_id = chat_stream.bot_id or ""
    system_prompt = await _build_router_prompt(
        nickname=nickname,
        bot_id=bot_id,
        projection_prompt=prefix_prompt,
        fallback_prompt=fallback_prompt,
    )
    fitted_history = history_text.strip()[-_HISTORY_CHAR_BUDGET:]

    tasks_to_try = ["router", "agent"]
    if _circuit_is_open():
        tasks_to_try = ["agent"]
        logger.debug("Router 熔断器打开，暂时跳过专用 router 任务")

    last_error = "没有可用任务"
    transport_failed_candidates: set[tuple[str, str, str]] = set()
    for task in tasks_to_try:
        try:
            request = chatter.create_request(
                task,
                "router",
                with_reminder="agent",
            )
        except (ValueError, KeyError) as exc:
            last_error = f"{task} 未配置: {exc}"
            continue

        if not getattr(request, "model_set", None):
            last_error = f"{task} 没有可用模型"
            continue

        configured_models = list(request.model_set)
        if transport_failed_candidates:
            unseen_models = [
                model
                for model in configured_models
                if _transport_candidate_key(model) not in transport_failed_candidates
            ]
            if not unseen_models:
                last_error = f"{task} 没有新增传输候选模型"
                logger.info(
                    f"Router[{task}] 与已失败任务的传输候选完全重复，跳过无效备用链"
                )
                continue
            request.model_set = unseen_models
            logger.info(
                f"Router[{task}] 已剔除上一任务中失败的重复传输候选: "
                f"configured={len(configured_models)}, unseen={len(unseen_models)}"
            )

        fitted_unreads = _fit_unreads_to_budget(request, unreads_text)
        if len(fitted_unreads) < len(unreads_text):
            logger.info(
                f"Router[{task}] 新消息传输窗口已收缩: "
                f"{len(unreads_text)} -> {len(fitted_unreads)} 字符"
            )

        user_parts: list[str] = []
        if fitted_history:
            user_parts.append(f"<chat_history>\n{fitted_history}\n</chat_history>")
        user_parts.append(f"<new_messages>\n{fitted_unreads}\n</new_messages>")
        user_parts.append("只判断是否把这批新消息交给表达层继续处理。")

        request.add_payload(
            LLMPayload(
                ROLE.SYSTEM,
                Text(
                    _fit_system_prompt_to_budget(system_prompt, request, logger, task)
                ),
            )
        )
        request.add_payload(LLMPayload(ROLE.USER, Text("\n\n".join(user_parts))))

        try:
            response = await request.send(stream=False)
            awaited_text = await response
        except Exception as exc:  # noqa: BLE001
            last_error = f"{task} 调用失败: {exc}"
            transport_failed_candidates.update(
                candidate
                for model in request.model_set
                if (candidate := _transport_candidate_key(model)) is not None
            )
            logger.warning(f"Router {last_error}，尝试下一个云端任务")
            if task == "router":
                _circuit_record_failure()
            continue

        content = str(response.message or awaited_text or "").strip()
        if not content:
            last_error = f"{task} 返回空正文"
            logger.warning(f"Router[{task}] 返回空正文，尝试下一个云端任务")
            if task == "router":
                _circuit_record_failure()
            continue

        decision = _parse_decision(content)
        if decision is not None:
            if task == "router":
                _circuit_record_success()
            return decision

        last_error = f"{task} 返回的决策结构无效"
        logger.warning(
            f"Router[{task}] 没有有效决策 JSON，尝试下一个云端任务: {content[:200]}..."
        )
        if task == "router":
            _circuit_record_failure()

    logger.error(f"Router 所有任务均不可用: {last_error}；保留消息并交给主体判断")
    return {
        "should_respond": True,
        "reason": f"Router 降级：{last_error}；消息已保留并交给主体判断",
    }
