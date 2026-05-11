import asyncio
import json
import re
import time
import uuid
from typing import Any, Dict, List
from fastapi import HTTPException
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

class ChatCompletionRequest(BaseModel):
    model: str = "elysia"
    messages: List[ChatMessage]
    tools: List[Dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: Dict[str, Any] | None = None
    stream: bool = False

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

    # 段落间超时：需大于 LifeSendTextAction 最大打字延迟 (4.0s)，留 1s 余量
    _SEGMENT_TIMEOUT: float = 5.5
    # 总超时
    _TOTAL_TIMEOUT: float = 120.0
    # 游戏 operator 是同步请求链路：需要覆盖一次模型调用和强制回复重试。
    _GAME_DECISION_TOTAL_TIMEOUT: float = 45.0

    def register_endpoints(self) -> None:

        @self.app.post("/chat/completions", response_model=ChatCompletionResponse)
        async def chat_completions(request: ChatCompletionRequest):
            """处理 OpenAI 格式的对话请求"""

            if not request.messages:
                raise HTTPException(status_code=400, detail="Messages cannot be empty")

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

    async def _handle_live_chat(self, messages: List[ChatMessage]) -> str:
        raw_content = _get_last_user_content(messages)
        viewer_name, content = _normalize_live_comment(raw_content)
        sender_name = viewer_name or "直播间观众"
        sender_id = f"live_user:{viewer_name}" if viewer_name else "live_user"

        stream_id = "live_broadcast"
        platform = "live"

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
        return result

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
        """启动时订阅发送事件，用于截获回复"""
        from src.kernel.event import get_event_bus
        get_event_bus().subscribe(EventType.ON_MESSAGE_SENT, self._on_message_sent)
        logger.info("OpenAI 桥接路由已就绪，等待爱莉降临...")

    async def _on_message_sent(self, event_name: str, params: dict) -> Any:
        """当 Bot 发出消息时触发，收集所有回复段落"""
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
