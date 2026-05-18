"""Default Chatter 子代理决策模块。"""

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


_DEFAULT_SUB_AGENT_FALLBACK_PROMPT = """你是一个聊天意图识别助手。
你的任务是分析新收到的聊天消息，结合历史上下文，判断主机器人是否有必要进行响应。

# 关于主机器人
主机器人的名字是 {nickname}。
{bot_id_section}
# 判定准则
你应该在以下情况判定为 "需要回复" (should_respond = true)：
1. 消息中明确提到了机器人的名字({nickname})或代称。
2. 消息中存在艾特(@)行为，且艾特的 QQ 号必须是 {bot_id}。
3. 消息内容与当前正在进行的话题高度相关，需要机器人进一步说明、回答或参与。
4. 对方在表达某种需要回应的情绪（如问候、告别、称赞、抱怨等）。

你应该在以下情况判定为 "不需要回复" (should_respond = false)：
1. 消息是群聊中的闲聊，且机器人并非话题参与者。
2. 消息艾特了其他人（QQ 号不是 {bot_id}）。
3. 明显是一连串消息中的中间部分，可以继续等待后续。
4. 检测到是其他 Bot 的自动回复或无意义的刷屏消息。
5. 只有单个表情且不携带任何需要回复的语义。

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


async def decide_should_respond(
    chatter: SupportsRequestCreation,
    logger: Logger,
    unreads_text: str,
    chat_stream: ChatStream,
    fallback_prompt: str | None = None,
) -> SubAgentDecision:
    """执行子代理决策并返回 should_respond 结果。"""
    try:
        request = chatter.create_request(
            "sub_actor",
            "sub_agent",
            with_reminder="sub_actor",
        )
    except (ValueError, KeyError):
        return {"should_respond": True, "reason": "未找到 sub_actor 配置，默认响应"}

    nickname = get_core_config().personality.nickname
    bot_id = chat_stream.bot_id or ""
    bot_id_section = f"它的 QQ 号是 {bot_id}。\n" if bot_id else ""
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
        prompt_template = fallback_prompt or _DEFAULT_SUB_AGENT_FALLBACK_PROMPT
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

    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(sub_prompt)))

    fitted_unreads = _fit_unreads_to_sub_agent_budget(request, unreads_text)
    if len(fitted_unreads) < len(unreads_text):
        logger.info(
            "Sub-agent 输入已截断以控制上下文长度: "
            f"{len(unreads_text)} -> {len(fitted_unreads)} 字符"
        )

    request.add_payload(
        LLMPayload(ROLE.USER, Text(f"【新收到待判定消息】\n{fitted_unreads}"))
    )

    try:
        response = await request.send(stream=False)
        await response

        content = response.message
        if not content or not content.strip():
            logger.warning("Sub-agent 返回了空内容，默认进行响应")
            return {"should_respond": True, "reason": "模型未返回判断内容"}

        try:
            result = json_repair.loads(content)
            if isinstance(result, dict):
                return {
                    "should_respond": bool(result.get("should_respond", True)),
                    "reason": result.get("reason", "未提供理由"),
                }
        except Exception as error:
            logger.debug(f"Sub-agent JSON 解析失败: {error} | 内容: {content[:500]}")

        logger.warning(f"Sub-agent 无法找到有效的 JSON 结构: {content[:200]}...")
        return {"should_respond": True, "reason": "解析 JSON 失败，默认响应"}
    except Exception as error:
        logger.error(f"Sub-agent 决策过程异常: {error}", exc_info=True)
        return {"should_respond": True, "reason": f"执行异常: {error}"}
