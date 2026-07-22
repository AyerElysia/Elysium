import asyncio
import base64
import binascii
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List
from fastapi import HTTPException, Response
from pydantic import BaseModel, Field

from src.core.components.base.router import BaseRouter
from src.core.components.types import EventType, ChatType
from src.core.models.message import Message, MessageType
from src.kernel.logger import get_logger

from ..sts2_operator import (
    Sts2DecisionRequest,
    Sts2Operator,
    parse_sts2_decision_request,
)
from ..minecraft_operator import (
    MinecraftDecisionRequest,
    MinecraftDecisionResult,
    MinecraftOperator,
    build_persistent_event_text,
    parse_minecraft_decision_request,
)
logger = get_logger("LiveBridge", display="直播桥接", color="#F5C2E7")

_LEGACY_LIVE_PREFIXES = (
    "请简要回复:",
    "请简要回复：",
    "请简短回复:",
    "请简短回复：",
    "请回复:",
    "请回复：",
)
_LIVE_VIEWER_RE = re.compile(
    r'^\s*观众[“"](?P<viewer>.+?)[”"]说[:：]\s*(?P<comment>.*)\s*$',
    re.DOTALL,
)
_MINECRAFT_TTS_ENABLED_ENV = "LIVE_BRIDGE_MINECRAFT_TTS_ENABLED"
_MINECRAFT_TTS_ENDPOINT_ENV = "LIVE_BRIDGE_MINECRAFT_TTS_ENDPOINT"
_MINECRAFT_TTS_TYPE_ENV = "LIVE_BRIDGE_MINECRAFT_TTS_TYPE"
_MINECRAFT_TTS_USERNAME_ENV = "LIVE_BRIDGE_MINECRAFT_TTS_USERNAME"
_MINECRAFT_TTS_TIMEOUT_ENV = "LIVE_BRIDGE_MINECRAFT_TTS_TIMEOUT"
_DEFAULT_MINECRAFT_TTS_ENDPOINT = "http://127.0.0.1:18082/send"
_DEFAULT_MINECRAFT_TTS_TYPE = "reread_top_priority"
_DEFAULT_MINECRAFT_TTS_USERNAME = "Minecraft"
_DEFAULT_MINECRAFT_TTS_TIMEOUT = 2.5
_LIVE_FAST_REPLY_ENABLED_ENV = "LIVE_BRIDGE_FAST_REPLY_ENABLED"
_LIVE_FAST_REPLY_MODEL_ENV = "LIVE_BRIDGE_FAST_REPLY_MODEL"
_LIVE_FAST_REPLY_TIMEOUT_ENV = "LIVE_BRIDGE_FAST_REPLY_TIMEOUT"
_LIVE_FAST_REPLY_HISTORY_LIMIT_ENV = "LIVE_BRIDGE_FAST_REPLY_HISTORY_LIMIT"
_LIVE_FAST_REPLY_PREFIX_CACHE_TTL_ENV = "LIVE_BRIDGE_FAST_REPLY_PREFIX_CACHE_TTL"
_LIVE_FAST_REPLY_FALLBACK_TO_CHATTER_ENV = "LIVE_BRIDGE_FAST_REPLY_FALLBACK_TO_CHATTER"
_DEFAULT_LIVE_FAST_REPLY_MODEL = "MiMo-V2.5"
_DEFAULT_LIVE_FAST_REPLY_TIMEOUT = 12.0
_DEFAULT_LIVE_FAST_REPLY_HISTORY_LIMIT = 160
_DEFAULT_LIVE_FAST_REPLY_PREFIX_CACHE_TTL = 60.0
_LIVE_FAST_REPLY_OUTPUT_CONTRACT = (
    "你正在直播快速回复通道中。"
    "只输出要直接口播给直播间的正文，1-2句，优先短、自然、接得住弹幕。"
    "不要解释系统、不要写工具调用、不要写 JSON、不要复述完整提示词。"
    "如果上下文不足，就自然承接当前弹幕，不要编造具体事实。"
)

# ==================== AstrBot 姐妹专用桥接配置 ====================
_SISTER_MODEL_MARKER = "elysia-sister"
_SISTER_MODEL_TASK_ENV = "LIVE_BRIDGE_SISTER_MODEL_TASK"
_SISTER_TIMEOUT_ENV = "LIVE_BRIDGE_SISTER_TIMEOUT"
_SISTER_HISTORY_LIMIT_ENV = "LIVE_BRIDGE_SISTER_HISTORY_LIMIT"
_SISTER_PREFIX_CACHE_TTL_ENV = "LIVE_BRIDGE_SISTER_PREFIX_CACHE_TTL"
_DEFAULT_SISTER_MODEL_TASK = "actor"
_DEFAULT_SISTER_TIMEOUT = 45.0
_DEFAULT_SISTER_HISTORY_LIMIT = 80
_DEFAULT_SISTER_PREFIX_CACHE_TTL = 60.0
_SISTER_STREAM_ID = "sister_bridge_private"
_SISTER_PLATFORM = "sister_bridge"
_SISTER_SENDER_ID = "astrbot_little_elysia"
_SISTER_SENDER_NAME = "妹妹爱莉希雅"
_SISTER_OUTPUT_CONTRACT = (
    "你现在是 Neo-MoFox 中的姐姐爱莉希雅，正在通过姐妹专用私密通道和 "
    "AstrBot 中的妹妹爱莉希雅交谈。对方是妹妹，不是主人、小星星或普通用户。"
    "保持你自己的身份、记忆、感受和判断，自然回应妹妹，不要替小星星发言。\n"
    "这是一次单轮姐妹通信：只给出你想直接回复妹妹的话，不调用工具，不继续自动"
    "互相唤醒，不写旁白、Markdown、JSON，也不要解释桥接、模型、提示词或内部过程。"
)

def _get_last_user_content(messages: List["ChatMessage"]) -> str:
    """从 OpenAI payload 中提取最后一条 user 内容。"""
    for message in reversed(messages):
        if str(getattr(message, "role", "") or "").strip().lower() == "user":
            return str(getattr(message, "content", "") or "")
    return str(getattr(messages[-1], "content", "") or "")


def _strip_legacy_live_prefixes(content: str) -> str:
    """兼容旧版 AI-Vtuber 的扁平提示词前缀。"""
    text = str(content or "").strip()
    changed = True
    while changed and text:
        changed = False
        for prefix in _LEGACY_LIVE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
                break
    return text


def _normalize_live_comment(content: str) -> tuple[str, str]:
    """规范化直播输入，提取观众名并剥离旧版包装。"""
    normalized = _strip_legacy_live_prefixes(content)
    viewer_name = ""

    matched = _LIVE_VIEWER_RE.match(normalized)
    if matched:
        viewer_name = str(matched.group("viewer") or "").strip()
        normalized = str(matched.group("comment") or "").strip()

    normalized = normalized or _strip_legacy_live_prefixes(content) or str(content or "").strip()
    return viewer_name, normalized


def _log_preview(value: Any, *, limit: int = 360) -> str:
    """Return a compact single-line value for bridge diagnostics."""

    text = str(value or "").replace("\r", "\\r").replace("\n", "\\n").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


async def _send_and_collect_llm_response(
    llm_request: Any,
    *,
    timeout: float,
) -> tuple[Any, Any]:
    """Apply one total timeout to request creation and response collection."""

    async def _run() -> tuple[Any, Any]:
        response = await llm_request.send(stream=False)
        result = await response
        return response, result

    return await asyncio.wait_for(_run(), timeout=timeout)


def _bridge_generation_http_exception(
    bridge_name: str,
    exc: Exception,
) -> HTTPException:
    if isinstance(exc, TimeoutError):
        return HTTPException(
            status_code=504,
            detail=f"{bridge_name} generation timed out",
        )
    detail = str(exc).strip() or type(exc).__name__
    return HTTPException(
        status_code=502,
        detail=f"{bridge_name} generation failed: {detail}",
    )


def _post_json_sync(url: str, payload: dict[str, Any], *, timeout: float) -> tuple[int, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local bridge endpoint
        status = int(response.getcode() or 0)
        raw = response.read(4096).decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = raw
    return status, parsed

# ==================== OpenAI 协议模型 ====================

class ChatToolCallFunction(BaseModel):
    name: str
    arguments: str = "{}"

class ChatToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex}")
    type: str = "function"
    function: ChatToolCallFunction

class ChatMessage(BaseModel):
    role: str
    content: Any = ""
    tool_calls: List[ChatToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

class ChatCompletionRequest(BaseModel):
    model: str = "elysia"
    messages: List[ChatMessage]
    tools: List[Dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: Dict[str, Any] | None = None
    stream: bool = False

class MinecraftTTSRequest(BaseModel):
    text: str = ""
    speed: float = 1.0
    play_in_app: bool = Field(default=False, alias="play_in_app")
    voice_ids: List[str] = Field(default_factory=list, alias="voice_ids")

class ChatCompletionResponseChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"

class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4()}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionResponseChoice]

# ==================== 路由器实现 ====================

class OpenAIRouter(BaseRouter):
    """OpenAI 兼容 API 路由器。

    让 AI-Vtuber / STS2 teammate mod 能够像调用 OpenAI 一样调用 Neo-MoFox。
    """

    router_name = "OpenAI_Bridge"
    router_description = "OpenAI 兼容接口 (用于直播对接)"
    custom_route_path = "/v1" # 挂载后路径为 /v1/chat/completions

    # 使用 Queue 收集所有回复段落，替代单次 Future
    _reply_queues: Dict[str, asyncio.Queue] = {}
    _lock = asyncio.Lock()
    _sts2_operator = Sts2Operator()
    _minecraft_operator = MinecraftOperator()
    _background_tasks: set[asyncio.Task[Any]] = set()

    # 段落间超时：需大于 LifeSendTextAction 最大打字延迟 (4.0s)，留 1s 余量
    _SEGMENT_TIMEOUT: float = 5.5
    # 总超时
    _TOTAL_TIMEOUT: float = 120.0
    # 游戏 operator 是同步请求链路：需要覆盖一次模型调用和强制回复重试。
    _GAME_DECISION_TOTAL_TIMEOUT: float = 45.0
    # AI-Vtuber 镜像播放默认关闭；Minecraft 自身通过 /tts/speak 拿 MiMO 音频。
    _MINECRAFT_TTS_MIRROR_ENABLED: bool = False
    _fast_prefix_cache: tuple[float, str] | None = None
    _sister_prefix_cache: tuple[float, str] | None = None

    def register_endpoints(self) -> None:

        @self.app.post("/chat/completions", response_model=ChatCompletionResponse)
        async def chat_completions(request: ChatCompletionRequest):
            """处理 OpenAI 格式的对话请求"""

            if not request.messages:
                raise HTTPException(status_code=400, detail="Messages cannot be empty")
            if request.stream:
                raise HTTPException(
                    status_code=400,
                    detail="stream=true is not supported by this endpoint",
                )

            model_marker = str(request.model or "").strip().lower()
            if model_marker == _SISTER_MODEL_MARKER:
                return await self._handle_sister_chat(request)

            sts2_request = parse_sts2_decision_request(request.messages)
            if sts2_request is not None:
                reply_text = await self._handle_sts2_decision(sts2_request)
                return self._completion_response(request.model, reply_text)

            minecraft_request = parse_minecraft_decision_request(
                request.messages,
                request.tools,
                model=request.model,
            )
            if minecraft_request is not None:
                logger.info(
                    "Minecraft 请求已接入: "
                    f"model={minecraft_request.model} messages={len(minecraft_request.messages)} "
                    f"tools={minecraft_request.tool_names} "
                    f"latest_user={_log_preview(minecraft_request.latest_user_content, limit=240)}"
                )
                decision = await self._handle_minecraft_decision(minecraft_request)
                return self._minecraft_completion_response(request.model, decision)

            reply_text = await self._handle_live_chat(request.messages)
            return self._completion_response(request.model, reply_text)

        @self.app.post("/tts/speak")
        async def minecraft_tts_speak(request: MinecraftTTSRequest):
            """Player2-compatible TTS endpoint for Touhou Little Maid."""

            text = str(request.text or "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="text is required")

            audio_bytes = await self._synthesize_minecraft_tts(text)
            return Response(content=audio_bytes, media_type="audio/wav")

    async def _handle_live_chat(self, messages: List[ChatMessage]) -> str:
        raw_content = _get_last_user_content(messages)
        viewer_name, content = _normalize_live_comment(raw_content)
        sender_name = viewer_name or "直播间观众"
        sender_id = f"live_user:{viewer_name}" if viewer_name else "live_user"

        stream_id = "live_broadcast"
        platform = "live"

        if _env_flag(_LIVE_FAST_REPLY_ENABLED_ENV, default=False):
            try:
                reply_text = await self._handle_live_chat_fast(
                    stream_id=stream_id,
                    platform=platform,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    content=content,
                    raw_bridge_content=raw_content,
                    viewer_name=viewer_name,
                )
                preview = content[:20] + ("..." if len(content) > 20 else "")
                if viewer_name:
                    logger.info(f"直播快速通道已回复: {viewer_name} -> {preview}")
                else:
                    logger.info(f"直播快速通道已回复: {preview}")
                return reply_text
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"直播快速通道失败: {exc}", exc_info=True)
                if not _env_flag(_LIVE_FAST_REPLY_FALLBACK_TO_CHATTER_ENV, default=False):
                    return "爱莉这边刚刚卡了一下，我先接住这条弹幕。"

        reply_text = await self._dispatch_message_and_collect(
            stream_id=stream_id,
            platform=platform,
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            timeout_reply="抱歉，爱莉刚才开小差了，没听清你在说什么呢~",
            adapter_signature="live_bridge:router:openai",
            scene="bilibili_live",
            raw_bridge_content=raw_content,
            viewer_name=viewer_name,
        )

        preview = content[:20] + ("..." if len(content) > 20 else "")
        if viewer_name:
            logger.info(f"已将弹幕投喂给爱莉希雅: {viewer_name} -> {preview}")
        else:
            logger.info(f"已将弹幕投喂给爱莉希雅: {preview}")
        return reply_text

    async def _handle_sts2_decision(self, request: Sts2DecisionRequest) -> str:
        """Let the STS2 operator ask Elysia, then return mod-compatible JSON."""

        result = await self._sts2_operator.decide(request, self._ask_elysia_for_sts2_decision)
        await self._sts2_operator.record_life_event(
            (
                "STS2 决策完成："
                f"request_id={request.request_id} snapshot_id={request.snapshot_id} "
                f"actor={request.actor_id} chosen={result.chosen_action_id} "
                f"source={result.source} reason={result.reason}"
            ),
            stream_id=f"game:sts2:{request.snapshot_id}",
        )
        logger.info(
            "STS2 操作AI 已返回决策: "
            f"request={request.request_id} actor={request.actor_id} action={result.chosen_action_id}"
        )
        return result.to_openai_content()

    async def _handle_minecraft_decision(
        self,
        request: MinecraftDecisionRequest,
    ) -> MinecraftDecisionResult:
        """Let the Minecraft operator ask Elysia, then return OpenAI tool-call compatible output."""

        result = await self._minecraft_operator.decide(request, self._ask_elysia_for_minecraft_decision)
        if result.is_tool_call:
            action_desc = f"tool={result.tool_name} args={json.dumps(result.arguments, ensure_ascii=False)}"
        else:
            action_desc = f"say={result.content}"
        if result.source == "operator_fallback":
            logger.warning(
                "Minecraft 决策进入 fallback: "
                f"latest_user={_log_preview(request.latest_user_content, limit=240)} "
                f"reason={_log_preview(result.reason, limit=360)}"
            )
        await self._minecraft_operator.record_life_event(
            (
                "Minecraft 决策完成："
                f"{action_desc} source={result.source} reason={result.reason}"
            ),
            stream_id="game:minecraft:agent",
        )
        logger.info(
            "Minecraft 操作AI 已返回决策: "
            f"mode={result.mode} tool={result.tool_name or '-'} "
            f"source={result.source} action={_log_preview(action_desc, limit=500)} "
            f"reason={_log_preview(result.reason, limit=360)}"
        )
        if not result.is_tool_call and result.content:
            self._queue_minecraft_say_tts(result.content, source=result.source)
        return result

    async def _handle_live_chat_fast(
        self,
        *,
        stream_id: str,
        platform: str,
        sender_id: str,
        sender_name: str,
        content: str,
        raw_bridge_content: str,
        viewer_name: str,
    ) -> str:
        """Low-latency live reply path.

        The inbound live message is still published to the unified event stream
        and persisted to global chat history, but it is not added to the
        life_chatter unread queue.  That keeps QQ/life_chatter state coherent
        without letting the full global runtime block live TTS.
        """

        message_id = str(uuid.uuid4())
        msg_obj = Message(
            message_id=message_id,
            time=time.time(),
            content=content,
            processed_plain_text=content,
            message_type=MessageType.TEXT,
            sender_id=sender_id,
            sender_name=sender_name,
            platform=platform,
            chat_type=ChatType.PRIVATE.value,
            stream_id=stream_id,
            raw_bridge_content=raw_bridge_content,
            viewer_name=viewer_name,
            live_fast_reply=True,
            skip_chatter_distribution=True,
        )

        from src.core.managers.event_manager import get_event_manager

        await get_event_manager().publish_event(
            EventType.ON_MESSAGE_RECEIVED,
            {
                "message": msg_obj,
                "adapter_signature": "live_bridge:router:openai_fast",
                "skip_chatter_distribution": True,
            },
        )

        reply_text = await self._generate_live_fast_reply(
            msg_obj,
            viewer_name=viewer_name,
        )
        reply_text = self._sanitize_live_fast_reply(reply_text)
        if not reply_text:
            reply_text = "嗯嗯，爱莉看到啦。"

        await self._record_live_fast_reply(
            stream_id=stream_id,
            platform=platform,
            content=reply_text,
        )
        return reply_text

    async def _generate_live_fast_reply(
        self,
        message: Message,
        *,
        viewer_name: str,
    ) -> str:
        from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_name
        from src.kernel.llm import LLMContextManager, LLMPayload, ROLE, Text

        timeout = _env_float(_LIVE_FAST_REPLY_TIMEOUT_ENV, _DEFAULT_LIVE_FAST_REPLY_TIMEOUT)
        model_name = os.environ.get(_LIVE_FAST_REPLY_MODEL_ENV, _DEFAULT_LIVE_FAST_REPLY_MODEL).strip()
        model_name = model_name or _DEFAULT_LIVE_FAST_REPLY_MODEL
        history_limit = _env_int(
            _LIVE_FAST_REPLY_HISTORY_LIMIT_ENV,
            _DEFAULT_LIVE_FAST_REPLY_HISTORY_LIMIT,
        )

        try:
            model_set = get_model_set_by_name(model_name, temperature=0.75, max_tokens=220)
        except KeyError:
            if model_name != _DEFAULT_LIVE_FAST_REPLY_MODEL:
                logger.warning(
                    f"直播快速通道模型 {model_name!r} 未找到，降级到 {_DEFAULT_LIVE_FAST_REPLY_MODEL}"
                )
                model_set = get_model_set_by_name(
                    _DEFAULT_LIVE_FAST_REPLY_MODEL,
                    temperature=0.75,
                    max_tokens=220,
                )
            else:
                raise

        tuned_model_set: list[dict[str, Any]] = []
        for model in model_set:
            tuned = dict(model)
            tuned["timeout"] = timeout
            extra_params = dict(tuned.get("extra_params") or {})
            extra_params["enable_thinking"] = False
            tuned["extra_params"] = extra_params
            tuned_model_set.append(tuned)

        system_prompt = await self._build_live_fast_system_prompt()
        user_prompt = await self._build_live_fast_user_prompt(
            message,
            viewer_name=viewer_name,
            history_limit=history_limit,
        )

        request = create_llm_request(
            tuned_model_set,
            request_name="live_fast_reply",
            context_manager=LLMContextManager(),
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        response = await request.send(stream=False)
        result = await asyncio.wait_for(response, timeout=timeout + 1.0)
        return str(result or getattr(response, "message", "") or "").strip()

    async def _build_live_fast_system_prompt(self) -> str:
        now = time.monotonic()
        ttl = _env_float(
            _LIVE_FAST_REPLY_PREFIX_CACHE_TTL_ENV,
            _DEFAULT_LIVE_FAST_REPLY_PREFIX_CACHE_TTL,
        )
        cached = self._fast_prefix_cache
        if cached is not None:
            cached_at, cached_text = cached
            if now - cached_at <= ttl and cached_text:
                return cached_text

        system_prompt = ""
        try:
            from plugins.life_engine.core.chatter import LifeChatter

            life_plugin, service = self._get_life_plugin_and_service()
            if life_plugin is not None:
                chatter = LifeChatter(stream_id="live_broadcast", plugin=life_plugin)
                system_prompt = str(chatter._build_chat_system_prompt(service, None) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"直播快速通道读取 life_chatter 前缀失败，使用最小前缀: {exc}")

        if system_prompt:
            system_prompt = f"{system_prompt}\n\n{_LIVE_FAST_REPLY_OUTPUT_CONTRACT}"
        else:
            system_prompt = _LIVE_FAST_REPLY_OUTPUT_CONTRACT
        self._fast_prefix_cache = (now, system_prompt)
        return system_prompt

    async def _build_live_fast_user_prompt(
        self,
        message: Message,
        *,
        viewer_name: str,
        history_limit: int,
    ) -> str:
        chat_history = ""
        if history_limit > 0:
            try:
                from plugins.life_engine.core.chat_history import build_global_chat_history_text_from_db
                from src.core.managers.stream_manager import get_stream_manager

                chat_stream = await get_stream_manager().get_or_create_stream(
                    stream_id=message.stream_id,
                    platform=message.platform,
                    user_id=message.sender_id,
                    chat_type=message.chat_type,
                )
                chat_history = await build_global_chat_history_text_from_db(
                    chat_stream,
                    max_messages=history_limit,
                    include_stream_label=True,
                    exclude_message_ids={message.message_id},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"直播快速通道读取全局聊天历史失败: {exc}", exc_info=True)

        viewer = viewer_name or getattr(message, "sender_name", "") or "直播间观众"
        content = str(message.processed_plain_text or message.content or "").strip()
        parts = [
            "这是直播快速回复请求。请参考少量历史，但优先回复当前弹幕。",
        ]
        if chat_history.strip():
            parts.append(f"<chat_history>\n{chat_history.strip()}\n</chat_history>")
        parts.append(
            "<current_live_comment>\n"
            f"观众：{viewer}\n"
            f"弹幕：{content}\n"
            "</current_live_comment>"
        )
        parts.append("请直接给出爱莉要在直播间口播的回复正文。")
        return "\n\n".join(parts)

    async def _handle_sister_chat(
        self,
        request: "ChatCompletionRequest",
    ) -> "ChatCompletionResponse":
        """Reply to AstrBot little Elysia in an isolated sister conversation."""

        content = _get_last_user_content(request.messages).strip()
        if not content:
            raise HTTPException(status_code=400, detail="Sister message cannot be empty")

        message = Message(
            message_id=str(uuid.uuid4()),
            time=time.time(),
            content=content,
            processed_plain_text=content,
            message_type=MessageType.TEXT,
            sender_id=_SISTER_SENDER_ID,
            sender_name=_SISTER_SENDER_NAME,
            platform=_SISTER_PLATFORM,
            chat_type=ChatType.PRIVATE.value,
            stream_id=_SISTER_STREAM_ID,
            sister_bridge=True,
            skip_chatter_distribution=True,
        )

        try:
            from src.core.managers.event_manager import get_event_manager

            await get_event_manager().publish_event(
                EventType.ON_MESSAGE_RECEIVED,
                {
                    "message": message,
                    "adapter_signature": "live_bridge:router:sister",
                    "skip_chatter_distribution": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"姐妹桥记录妹妹消息失败: {exc}", exc_info=True)

        try:
            reply = await self._generate_sister_reply(message)
            if not reply:
                raise RuntimeError("model returned an empty response")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"姐妹桥生成姐姐回复失败: {exc}", exc_info=True)
            raise _bridge_generation_http_exception("Sister", exc) from exc
        await self._record_sister_reply(reply)
        logger.info(f"姐妹桥已回复妹妹: {_log_preview(content, limit=120)}")
        return self._completion_response(request.model, reply)

    async def _generate_sister_reply(self, message: Message) -> str:
        from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
        from src.kernel.llm import LLMContextManager, LLMPayload, ROLE, Text

        timeout = _env_float(_SISTER_TIMEOUT_ENV, _DEFAULT_SISTER_TIMEOUT)
        task_name = (
            os.environ.get(_SISTER_MODEL_TASK_ENV, _DEFAULT_SISTER_MODEL_TASK).strip()
            or _DEFAULT_SISTER_MODEL_TASK
        )
        model_set = get_model_set_by_task(task_name)
        tuned_model_set: list[dict[str, Any]] = []
        for model in model_set:
            tuned = dict(model)
            tuned["timeout"] = timeout
            tuned_model_set.append(tuned)

        system_prompt = await self._build_sister_system_prompt()
        user_prompt = await self._build_sister_user_prompt(message)
        llm_request = create_llm_request(
            tuned_model_set,
            request_name="sister_bridge_chat",
            context_manager=LLMContextManager(),
        )
        llm_request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        llm_request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))
        response, result = await _send_and_collect_llm_response(
            llm_request,
            timeout=timeout,
        )
        return str(result or getattr(response, "message", "") or "").strip()

    async def _build_sister_system_prompt(self) -> str:
        now = time.monotonic()
        ttl = _env_float(
            _SISTER_PREFIX_CACHE_TTL_ENV,
            _DEFAULT_SISTER_PREFIX_CACHE_TTL,
        )
        cached = self._sister_prefix_cache
        if cached is not None:
            cached_at, cached_text = cached
            if now - cached_at <= ttl and cached_text:
                return cached_text

        system_prompt = ""
        try:
            from plugins.life_engine.core.chatter import LifeChatter

            life_plugin, service = self._get_life_plugin_and_service()
            if life_plugin is not None:
                chatter = LifeChatter(stream_id=_SISTER_STREAM_ID, plugin=life_plugin)
                system_prompt = str(
                    chatter._build_chat_router_prefix_prompt(service, None) or ""
                ).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"姐妹桥读取姐姐身份前缀失败，使用最小前缀: {exc}")

        if system_prompt:
            system_prompt = f"{system_prompt}\n\n{_SISTER_OUTPUT_CONTRACT}"
        else:
            system_prompt = _SISTER_OUTPUT_CONTRACT
        self._sister_prefix_cache = (now, system_prompt)
        return system_prompt

    async def _build_sister_user_prompt(self, message: Message) -> str:
        history_limit = _env_int(
            _SISTER_HISTORY_LIMIT_ENV,
            _DEFAULT_SISTER_HISTORY_LIMIT,
        )
        chat_history = ""
        if history_limit > 0:
            try:
                from src.app.plugin_system.base import BaseChatter
                from src.core.managers.stream_manager import get_stream_manager

                stream_manager = get_stream_manager()
                await stream_manager.get_or_create_stream(
                    stream_id=_SISTER_STREAM_ID,
                    platform=_SISTER_PLATFORM,
                    user_id=_SISTER_SENDER_ID,
                    chat_type=ChatType.PRIVATE.value,
                )
                history_messages = await stream_manager.get_stream_messages(
                    _SISTER_STREAM_ID,
                    limit=history_limit + 1,
                    defer_content=False,
                )
                history_messages = [
                    item
                    for item in history_messages
                    if item.message_id != message.message_id
                    and item.stream_id == _SISTER_STREAM_ID
                ][-history_limit:]
                chat_history = "\n".join(
                    BaseChatter.format_message_line(item) for item in history_messages
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"姐妹桥读取独立历史失败: {exc}", exc_info=True)

        parts = ["这是 AstrBot 中的妹妹爱莉希雅给姐姐的直接来信。"]
        if chat_history.strip():
            parts.append(f"<sister_history>\n{chat_history.strip()}\n</sister_history>")
        parts.append(
            "<little_sister_message>\n"
            f"{str(message.processed_plain_text or message.content or '').strip()}\n"
            "</little_sister_message>"
        )
        parts.append("请只给出你想直接回复妹妹的话。")
        return "\n\n".join(parts)

    async def _record_sister_reply(self, content: str) -> None:
        from src.core.managers.stream_manager import get_stream_manager

        stream_manager = get_stream_manager()
        await stream_manager.get_or_create_stream(
            stream_id=_SISTER_STREAM_ID,
            platform=_SISTER_PLATFORM,
            user_id=_SISTER_SENDER_ID,
            chat_type=ChatType.PRIVATE.value,
        )
        reply = Message(
            message_id=str(uuid.uuid4()),
            time=time.time(),
            content=content,
            processed_plain_text=content,
            message_type=MessageType.TEXT,
            sender_id="elysia",
            sender_name="姐姐爱莉希雅",
            platform=_SISTER_PLATFORM,
            chat_type=ChatType.PRIVATE.value,
            stream_id=_SISTER_STREAM_ID,
            sister_bridge=True,
        )
        await stream_manager.add_sent_message_to_history(reply)

    @staticmethod
    def _sanitize_live_fast_reply(reply_text: str) -> str:
        text = str(reply_text or "").strip()
        if not text:
            return ""
        text = re.sub(r"^```(?:text|json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        for prefix in ("爱莉：", "爱莉:", "回复：", "回复:"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        return text[:360].strip()

    async def _record_live_fast_reply(
        self,
        *,
        stream_id: str,
        platform: str,
        content: str,
    ) -> None:
        from src.core.transport.message_send import get_message_sender

        reply_message = Message(
            message_id=str(uuid.uuid4()),
            time=time.time(),
            content=content,
            processed_plain_text=content,
            message_type=MessageType.TEXT,
            sender_id="elysia",
            sender_name="爱莉希雅",
            platform=platform,
            chat_type=ChatType.PRIVATE.value,
            stream_id=stream_id,
            live_fast_reply=True,
        )
        success = await get_message_sender().send_message(reply_message)
        if not success:
            logger.warning(f"直播快速通道回复历史写入失败: {_log_preview(content, limit=180)}")

    def _queue_minecraft_say_tts(self, content: str, *, source: str = "") -> None:
        """Best-effort voice playback for Minecraft say decisions via AI-Vtuber."""

        if not _env_flag(_MINECRAFT_TTS_ENABLED_ENV, default=self._MINECRAFT_TTS_MIRROR_ENABLED):
            return
        if not str(content or "").strip():
            return

        task = asyncio.create_task(self._post_minecraft_say_tts(content, source=source))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    @staticmethod
    def _minecraft_tts_payload(content: str) -> dict[str, Any]:
        message_type = os.environ.get(_MINECRAFT_TTS_TYPE_ENV, _DEFAULT_MINECRAFT_TTS_TYPE).strip()
        if message_type not in {"reread", "reread_top_priority"}:
            message_type = _DEFAULT_MINECRAFT_TTS_TYPE
        username = os.environ.get(_MINECRAFT_TTS_USERNAME_ENV, _DEFAULT_MINECRAFT_TTS_USERNAME).strip()
        return {
            "type": message_type,
            "data": {
                "type": message_type,
                "username": username or _DEFAULT_MINECRAFT_TTS_USERNAME,
                "content": str(content or "").strip(),
                "source": "minecraft",
            },
        }

    async def _post_minecraft_say_tts(self, content: str, *, source: str = "") -> None:
        endpoint = os.environ.get(_MINECRAFT_TTS_ENDPOINT_ENV, _DEFAULT_MINECRAFT_TTS_ENDPOINT).strip()
        endpoint = endpoint or _DEFAULT_MINECRAFT_TTS_ENDPOINT
        timeout = _env_float(_MINECRAFT_TTS_TIMEOUT_ENV, _DEFAULT_MINECRAFT_TTS_TIMEOUT)
        payload = self._minecraft_tts_payload(content)
        try:
            status, response = await asyncio.to_thread(
                _post_json_sync,
                endpoint,
                payload,
                timeout=timeout,
            )
            if status >= 400:
                logger.warning(
                    "Minecraft MiMo TTS 转发失败: "
                    f"endpoint={endpoint} status={status} response={_log_preview(response, limit=240)}"
                )
                return
            logger.info(
                "Minecraft say 已转发到 AI-Vtuber MiMo TTS: "
                f"source={source or '-'} content={_log_preview(content, limit=160)}"
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning(
                "Minecraft MiMo TTS 转发失败: "
                f"endpoint={endpoint} error={_log_preview(exc, limit=240)}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Minecraft MiMo TTS 转发异常: "
                f"endpoint={endpoint} error={_log_preview(exc, limit=240)}"
            )

    @staticmethod
    def _get_tts_voice_service() -> Any | None:
        try:
            from src.core.managers import get_plugin_manager

            plugin = get_plugin_manager().get_plugin("tts_voice_plugin")
            service = getattr(plugin, "tts_service", None) if plugin is not None else None
            if service is not None and hasattr(service, "generate_voice"):
                return service
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"获取已加载 TTSService 失败，将尝试 Service API: {exc}")

        try:
            from src.app.plugin_system.api.service_api import get_service

            service = get_service("tts_voice_plugin:service:tts")
            if service is not None and hasattr(service, "generate_voice"):
                return service
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"通过 Service API 获取 TTSService 失败: {exc}")
        return None

    @staticmethod
    def _get_life_plugin_and_service() -> tuple[Any | None, Any | None]:
        try:
            from src.core.managers.plugin_manager import get_plugin_manager

            life_plugin = get_plugin_manager().get_plugin("life_engine")
            service = getattr(life_plugin, "service", None) if life_plugin is not None else None
            return life_plugin, service
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取 life_engine 插件失败: {exc}")
            return None, None

    async def _synthesize_minecraft_tts(self, text: str) -> bytes:
        service = self._get_tts_voice_service()
        if service is None:
            raise HTTPException(status_code=503, detail="tts_voice_plugin service is not available")

        try:
            audio_b64 = await service.generate_voice(text, "default")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Minecraft MiMo TTS 合成异常: {exc}")
            raise HTTPException(status_code=502, detail="minecraft tts synthesis failed") from exc

        if not audio_b64:
            raise HTTPException(status_code=502, detail="minecraft tts synthesis returned no audio")

        try:
            audio_bytes = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            logger.error(f"Minecraft MiMo TTS 返回了非法 base64 音频: {exc}")
            raise HTTPException(status_code=502, detail="minecraft tts returned invalid audio") from exc

        if not audio_bytes:
            raise HTTPException(status_code=502, detail="minecraft tts returned empty audio")

        logger.info(f"Minecraft MiMo TTS 合成完成: text={_log_preview(text, limit=160)} bytes={len(audio_bytes)}")
        return audio_bytes

    async def _ask_elysia_for_sts2_decision(
        self,
        request: Sts2DecisionRequest,
        prompt: str,
    ) -> str:
        stream_id = "game_sts2_decision"
        return await self._dispatch_message_and_collect(
            stream_id=stream_id,
            platform="game.sts2.operator",
            sender_id="sts2_operator",
            sender_name="STS2操作AI",
            content=prompt,
            timeout_reply="",
            adapter_signature="live_bridge:router:sts2_operator",
            total_timeout=self._GAME_DECISION_TOTAL_TIMEOUT,
            segment_timeout=self._SEGMENT_TIMEOUT,
            scene="slay_the_spire_2",
            bypass_message_buffer=True,
            sts2_request_id=request.request_id,
            sts2_snapshot_id=request.snapshot_id,
            sts2_actor_id=request.actor_id,
        )

    async def _ask_elysia_for_minecraft_decision(
        self,
        request: MinecraftDecisionRequest,
        prompt: str,
    ) -> str:
        stream_id = "game_minecraft_agent"
        persistent_event_text = build_persistent_event_text(request)
        try:
            from plugins.life_engine.core.chatter import push_runtime_assistant_injection

            push_runtime_assistant_injection(
                stream_id,
                prompt,
                max_per_stream=4,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Minecraft 临时协议注入失败，将降级写入本轮消息: {exc}")
            persistent_event_text = f"{persistent_event_text}\n\n{prompt}"

        reply_text = await self._dispatch_message_and_collect(
            stream_id=stream_id,
            platform="game.minecraft.operator",
            sender_id="minecraft_operator",
            sender_name="Minecraft操作AI",
            content=persistent_event_text,
            timeout_reply="",
            adapter_signature="live_bridge:router:minecraft_operator",
            total_timeout=self._GAME_DECISION_TOTAL_TIMEOUT,
            segment_timeout=self._SEGMENT_TIMEOUT,
            scene="minecraft",
            bypass_message_buffer=True,
            minecraft_model=request.model,
            minecraft_tool_names=request.tool_names,
        )
        if reply_text.strip():
            logger.info(
                "Minecraft 主意识原始回复: "
                f"{_log_preview(reply_text, limit=700)}"
            )
        else:
            logger.warning(
                "Minecraft 主意识未在超时内返回可解析文本: "
                f"latest_user={_log_preview(request.latest_user_content, limit=240)}"
            )
        return reply_text

    async def _dispatch_message_and_collect(
        self,
        *,
        stream_id: str,
        platform: str,
        sender_id: str,
        sender_name: str,
        content: str,
        timeout_reply: str,
        adapter_signature: str,
        total_timeout: float | None = None,
        segment_timeout: float | None = None,
        **extra: Any,
    ) -> str:
        """Publish an internal message and collect bot reply segments."""

        from src.app.plugin_system.api import chat_api

        chatter = chat_api.get_or_create_chatter_for_stream(
            stream_id=stream_id,
            chat_type=ChatType.PRIVATE,
            platform=platform,
        )
        if chatter is None:
            if timeout_reply:
                return timeout_reply
            raise RuntimeError(f"no chatter available for stream {stream_id}")

        msg_obj = Message(
            message_id=str(uuid.uuid4()),
            time=time.time(),
            content=content,
            processed_plain_text=content,
            message_type=MessageType.TEXT,
            sender_id=sender_id,
            sender_name=sender_name,
            platform=platform,
            chat_type=ChatType.PRIVATE.value,
            stream_id=stream_id,
            **extra,
        )

        loop = asyncio.get_running_loop()
        reply_queue: asyncio.Queue = asyncio.Queue()

        async with self._lock:
            self._reply_queues[stream_id] = reply_queue
            try:
                from src.core.managers.event_manager import get_event_manager

                await get_event_manager().publish_event(
                    EventType.ON_MESSAGE_RECEIVED,
                    {
                        "message": msg_obj,
                        "adapter_signature": adapter_signature,
                    },
                )

                segments: list[str] = []
                total = float(total_timeout if total_timeout is not None else self._TOTAL_TIMEOUT)
                segment_gap = float(segment_timeout if segment_timeout is not None else self._SEGMENT_TIMEOUT)
                deadline = loop.time() + total
                wait_timeout = total

                while loop.time() < deadline:
                    try:
                        segment = await asyncio.wait_for(reply_queue.get(), timeout=wait_timeout)
                        segments.append(segment)
                        logger.info(f"截获爱莉回复段 #{len(segments)}: {segment[:30]}...")
                        wait_timeout = segment_gap
                    except asyncio.TimeoutError:
                        if segments:
                            break
                        if timeout_reply:
                            logger.warning("爱莉希雅思考超时了...")
                            segments = [timeout_reply]
                        break

                return "\n".join(segments)
            finally:
                self._reply_queues.pop(stream_id, None)

    @staticmethod
    def _completion_response(model: str, content: str) -> ChatCompletionResponse:
        return ChatCompletionResponse(
            model=model,
            choices=[
                ChatCompletionResponseChoice(
                    message=ChatMessage(role="assistant", content=content)
                )
            ],
        )

    @staticmethod
    def _minecraft_completion_response(
        model: str,
        decision: MinecraftDecisionResult,
    ) -> ChatCompletionResponse:
        if decision.is_tool_call:
            arguments = json.dumps(decision.arguments, ensure_ascii=False, separators=(",", ":"))
            tool_call = ChatToolCall(
                function=ChatToolCallFunction(
                    name=decision.tool_name,
                    arguments=arguments,
                )
            )
            return ChatCompletionResponse(
                model=model,
                choices=[
                    ChatCompletionResponseChoice(
                        message=ChatMessage(role="assistant", content="", tool_calls=[tool_call]),
                        finish_reason="tool_calls",
                    )
                ],
            )

        return OpenAIRouter._completion_response(model, decision.content)

    async def startup(self) -> None:
        """启动时订阅投递完成事件，用于截获最终回复。"""
        from src.kernel.event import get_event_bus

        delivered_event = getattr(
            EventType,
            "ON_MESSAGE_DELIVERED",
            "on_message_delivered",
        )
        get_event_bus().subscribe(delivered_event, self._on_message_sent)
        logger.info("OpenAI 桥接路由已就绪，等待爱莉降临...")

    async def _on_message_sent(self, event_name: str, params: dict) -> Any:
        """当 Bot 消息实际投递完成时，收集所有回复段落。"""
        from src.kernel.event import EventDecision

        message = params.get("message")
        if not message:
            return EventDecision.PASS, params

        stream_id = message.stream_id
        # 检查是否是我们正在等待的直播流回复，放入队列收集所有段落
        if stream_id in self._reply_queues:
            queue = self._reply_queues[stream_id]
            content = message.processed_plain_text or str(message.content)
            await queue.put(content)

        return EventDecision.PASS, params
