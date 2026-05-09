import asyncio
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

# ==================== OpenAI 协议模型 ====================

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = "elysia"
    messages: List[ChatMessage]
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

    # 段落间超时：需大于 LifeSendTextAction 最大打字延迟 (4.0s)，留 1s 余量
    _SEGMENT_TIMEOUT: float = 5.5
    # 总超时
    _TOTAL_TIMEOUT: float = 120.0

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
            total_timeout=8.0,
            segment_timeout=1.25,
            scene="slay_the_spire_2",
            sts2_request_id=request.request_id,
            sts2_snapshot_id=request.snapshot_id,
            sts2_actor_id=request.actor_id,
        )

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
