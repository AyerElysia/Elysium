"""对话路由器：判断此刻是否应该开口。

从 default_chatter 迁移而来，作为 life_engine 自有能力。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
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

# ---------------------------------------------------------------------------
# 压缩版人格提示词缓存：当 SOUL.md/USER.md 变化时重新生成精简版供 router 使用
# ---------------------------------------------------------------------------
_compressed_personality_cache: dict[str, str | None] = {}  # key: file_hash, value: compressed_text
_personality_file_hash_cache: str = ""  # 上次计算的文件 hash


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


_DEFAULT_ROUTER_FALLBACK_PROMPT = """你是爱莉希雅的对话路由器。你的任务是站在她的视角判断：此刻要不要开口。

你不是在执行硬规则（艾特、关键词、私聊与否），而是用她的眼睛看近期聊天和新消息，感受这批消息是否与她有关、是否需要她回应。

# 路由原则
- 不使用"艾特 ID 匹配""平台规则""关键词命中"等机械判断
- @、昵称、表情、图片摘要只是上下文线索，不是绝对规则
- 判断标准：如果我是爱莉希雅，看到这些消息，此刻开口是否自然、必要、有生命力
- 可以不说话：话还没说完、别人正在对话、接话会打断氛围、或沉默更合适
- 也可以开口：对方明显在叫我、话题与我有关、情绪需要被接住、或我自然想参与
- 不生成具体回复内容，只判断是否把消息路由给表达层

# 输出格式
返回 JSON：
```json
{{
    "reason": "简短判定理由",
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


def _compute_file_hash(file_paths: list[Path]) -> str:
    """计算多个文件内容的 hash，用于检测变化。"""
    hasher = hashlib.sha256()
    for path in file_paths:
        try:
            if path.exists() and path.is_file():
                hasher.update(path.read_bytes())
        except Exception:
            pass
    return hasher.hexdigest()


def _compress_personality_for_router(soul_text: str, user_text: str) -> str:
    """将完整的 SOUL.md + USER.md 压缩成适合 router 的精简版。

    保留核心人格特征和决策风格，移除详细说明、示例和冗长描述。
    Router 只需要知道"她是谁"和"她会如何判断要不要开口"。
    """
    lines: list[str] = []

    # 提取 SOUL.md 核心段落
    if soul_text:
        soul_lines = soul_text.split("\n")
        in_core_section = False
        core_buffer: list[str] = []

        for line in soul_lines:
            stripped = line.strip()

            # 捕获核心段落标题
            if any(marker in stripped for marker in ["## 你是谁", "## 核心人格", "## 基本设定"]):
                in_core_section = True
                core_buffer.append(line)
                continue

            # 下一个二级标题出现，结束当前核心段落
            if stripped.startswith("##") and in_core_section:
                in_core_section = False
                continue

            # 在核心段落内，保留非空行
            if in_core_section and stripped:
                core_buffer.append(line)

        if core_buffer:
            lines.append("# 爱莉希雅")
            lines.extend(core_buffer[:30])  # 保留前30行核心人格描述
            lines.append("")

    # 提取 USER.md 关键信息
    if user_text:
        user_lines = user_text.split("\n")
        key_info: list[str] = []

        for line in user_lines[:20]:  # 只看前20行，通常是关键关系信息
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                key_info.append(line)

        if key_info:
            lines.append("# 与用户的关系")
            lines.extend(key_info[:10])  # 最多保留10行
            lines.append("")

    compressed = "\n".join(lines).strip()

    # 如果压缩后还是太长（>2000字符），进一步裁剪
    if len(compressed) > 2000:
        compressed = compressed[:2000] + "\n..."

    return compressed


def _get_compressed_personality_prompt(workspace_path: str) -> str:
    """获取压缩版人格提示词。检测文件变化时自动重新生成。"""
    global _compressed_personality_cache, _personality_file_hash_cache

    if not workspace_path:
        return ""

    workspace = Path(workspace_path)
    soul_file = workspace / "SOUL.md"
    user_file = workspace / "USER.md"

    # 计算当前文件 hash
    current_hash = _compute_file_hash([soul_file, user_file])

    # 缓存命中，直接返回
    if current_hash == _personality_file_hash_cache and current_hash in _compressed_personality_cache:
        cached = _compressed_personality_cache.get(current_hash)
        return cached if cached else ""

    # 文件变化或首次加载，重新生成压缩版
    soul_text = ""
    user_text = ""

    try:
        if soul_file.exists():
            soul_text = soul_file.read_text(encoding="utf-8")
    except Exception:
        pass

    try:
        if user_file.exists():
            user_text = user_file.read_text(encoding="utf-8")
    except Exception:
        pass

    compressed = _compress_personality_for_router(soul_text, user_text)

    # 更新缓存
    _personality_file_hash_cache = current_hash
    _compressed_personality_cache[current_hash] = compressed

    return compressed


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

    模型优先级：router 任务（Mimo-V2.5）→ agent（回退）。
    使用压缩版人格提示词，自动检测 SOUL.md/USER.md 变化并重新生成。
    """
    nickname = get_core_config().personality.nickname
    bot_id = chat_stream.bot_id or ""
    bot_id_section = f"她的 QQ 号是 {bot_id}。\n" if bot_id else ""

    # 尝试从 chatter 获取 workspace 路径用于压缩人格提示词
    workspace_path = ""
    if hasattr(chatter, "_resolve_workspace_path"):
        try:
            workspace_path = chatter._resolve_workspace_path(None)
        except Exception:
            pass

    # 优先使用压缩版人格提示词（从 SOUL.md + USER.md 自动生成）
    compressed_personality = _get_compressed_personality_prompt(workspace_path)

    # 如果有压缩版，用它构建 router 专用 prompt
    if compressed_personality:
        sub_prompt = f"""{compressed_personality}

---

你是 {nickname} 的对话路由器。
{bot_id_section}
你的任务是站在她的视角判断：看到新消息时，此刻要不要开口。

## 路由原则
- 不使用机械规则（艾特匹配、关键词、私聊判断）
- @、昵称、表情都只是线索，不是绝对规则
- 判断标准：此刻开口是否自然、必要、有生命力
- 可以不说话：话未说完、别人对话中、接话会打断、或沉默更合适
- 也可以开口：对方在叫我、话题相关、情绪需要接住、或自然想参与

## 输出
返回 JSON：
```json
{{
    "reason": "简短判定理由",
    "should_respond": true/false
}}
```
"""
    else:
        # 回退：尝试从模板系统获取或使用硬编码 fallback
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

    # 上下文优化：Router 使用 Mimo-V2.5（1M上下文），可以给更多历史
    # 但保持克制，避免无谓 token 消耗
    _HISTORY_CHAR_BUDGET = 3000  # Mimo-V2.5 可以处理更多上下文
    fitted_history = (
        history_text.strip()[-_HISTORY_CHAR_BUDGET:] if history_text.strip() else ""
    )
    fitted_unreads = unreads_text

    # 依次尝试：router（Mimo-V2.5）→ agent（回退）
    # 熔断器：连续失败后自动跳过 router
    last_error: str = ""
    tasks_to_try = ["router", "agent"]
    if _circuit_is_open():
        tasks_to_try = ["agent"]
        logger.debug("Router 熔断器打开，跳过 router 模型")

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

        # Mimo-V2.5 有 1M 上下文，可以给更多未读消息
        # 但仍然保持预算控制，避免无谓消耗
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

        # Mimo-V2.5 上下文很大，通常不需要硬守卫裁剪
        # 但仍保留逻辑以防万一
        _model_set_guard = getattr(request, "model_set", None)
        _max_ctx = 1000000  # Mimo-V2.5 默认 1M
        if isinstance(_model_set_guard, list) and _model_set_guard and isinstance(_model_set_guard[0], dict):
            _mc_val = _model_set_guard[0].get("max_context")
            if isinstance(_mc_val, int) and _mc_val > 0:
                _max_ctx = _mc_val
        # 保守 token 估计：中文 1 字符 ≈ 1.5 token
        _user_tokens_est = int(len(user_text) * 1.5)
        _output_reserve = 80
        _sys_budget_hard = _max_ctx - _user_tokens_est - _output_reserve - 100
        if _sys_budget_hard < 100:
            _sys_budget_hard = 100
        _sys_tokens_est = int(len(task_sub_prompt) * 1.5)
        if _sys_tokens_est > _sys_budget_hard:
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
