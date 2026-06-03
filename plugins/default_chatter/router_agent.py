"""Default Chatter 路由器模块。"""

from __future__ import annotations

import json_repair

from src.core.config import get_core_config
from src.core.models.stream import ChatStream
from src.core.prompt import get_prompt_manager
from src.kernel.logger import Logger
from src.kernel.llm import LLMPayload, ROLE, Text
from src.kernel.llm import LLMRequest
from src.kernel.llm.token_counter import count_text_tokens

from .type_defs import SubAgentDecision, SupportsRequestCreation


_DEFAULT_ROUTER_FALLBACK_PROMPT = """你是当前主体的对话路由器。
你的任务不是按硬规则拦截消息，而是站在主体自己的视角，结合近期聊天记录和新消息，判断此刻要不要开口。

# 路由原则
- 不要使用“艾特 ID 是否匹配”“是否私聊”“是否关键词命中”这类机械硬规则替主体做决定。
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
    """保留文本尾部内容并控制在 token 预算内。"""
    if token_budget <= 0 or not text:
        return ""

    total_tokens = _safe_count_tokens(text, model_identifier)
    if total_tokens <= token_budget:
        return text

    lines = text.splitlines()
    kept_reversed: list[str] = []
    used_tokens = 0
    for line in reversed(lines):
        line_tokens = _safe_count_tokens(line, model_identifier)
        if kept_reversed and used_tokens + line_tokens > token_budget:
            break
        kept_reversed.append(line)
        used_tokens += line_tokens

    candidate = "\n".join(reversed(kept_reversed)).strip()
    if candidate and _safe_count_tokens(candidate, model_identifier) <= token_budget:
        return candidate

    left = 0
    right = len(text)
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
    """将未读消息压缩到 sub-agent 可控 token 预算内。"""
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
        token_budget = min(max(1024, max_context // 4), 8000)
    else:
        token_budget = 6000

    return _trim_text_suffix_by_budget(unreads_text, model_identifier, token_budget)


async def route_should_respond(
    chatter: SupportsRequestCreation,
    logger: Logger,
    unreads_text: str,
    chat_stream: ChatStream,
    history_text: str = "",
    prefix_prompt: str = "",
    fallback_prompt: str | None = None,
) -> SubAgentDecision:
    """执行路由判断并返回 should_respond 结果。"""
    try:
        request = chatter.create_request(
            "sub_actor",
            "router",
            with_reminder="sub_actor",
        )
    except (ValueError, KeyError):
        return {"should_respond": True, "reason": "未找到 sub_actor 配置，默认响应"}

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

    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(sub_prompt)))

    fitted_unreads = _fit_unreads_to_sub_agent_budget(request, unreads_text)
    if len(fitted_unreads) < len(unreads_text):
        logger.info(
            "Router 输入已截断以控制上下文长度: "
            f"{len(unreads_text)} -> {len(fitted_unreads)} 字符"
        )

    user_parts: list[str] = []
    if history_text.strip():
        user_parts.append(f"<chat_history>\n{history_text.strip()}\n</chat_history>")
    user_parts.append(f"<new_messages>\n{fitted_unreads}\n</new_messages>")
    user_parts.append("请只判断这批新消息是否应路由给表达层继续处理。")
    request.add_payload(LLMPayload(ROLE.USER, Text("\n\n".join(user_parts))))

    try:
        response = await request.send(stream=False)
        await response

        content = response.message
        if not content or not content.strip():
            logger.warning("Router 返回了空内容，默认进行响应")
            return {"should_respond": True, "reason": "模型未返回判断内容"}

        try:
            result = json_repair.loads(content)
            if isinstance(result, dict):
                return {
                    "should_respond": bool(result.get("should_respond", True)),
                    "reason": result.get("reason", "未提供理由"),
                }
        except Exception as error:
            logger.debug(f"Router JSON 解析失败: {error} | 内容: {content[:500]}")

        logger.warning(f"Router 无法找到有效的 JSON 结构: {content[:200]}...")
        return {"should_respond": True, "reason": "解析 JSON 失败，默认响应"}
    except Exception as error:
        logger.error(f"Router 路由过程异常: {error}", exc_info=True)
        return {"should_respond": True, "reason": f"执行异常: {error}"}


async def decide_should_respond(
    chatter: SupportsRequestCreation,
    logger: Logger,
    unreads_text: str,
    chat_stream: ChatStream,
    fallback_prompt: str | None = None,
) -> SubAgentDecision:
    """兼容旧入口。新代码请使用 route_should_respond。"""

    return await route_should_respond(
        chatter=chatter,
        logger=logger,
        unreads_text=unreads_text,
        chat_stream=chat_stream,
        fallback_prompt=fallback_prompt,
    )
