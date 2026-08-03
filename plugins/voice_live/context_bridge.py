"""Bridge stable identity and durable voice history into one episode."""

from __future__ import annotations

import json
import uuid
from typing import Any

from src.core.config.core_config import get_core_config
from src.core.models.message import Message, MessageType

from .runtime_store import VoiceEpisodeStore

_PERCEPTION_PREFIX = "<transient_world_perception>\n"
_PERCEPTION_SUFFIX = "\n</transient_world_perception>"


def _compact_context_lines(content: str, max_bytes: int) -> tuple[str, dict[str, Any]]:
    """Build a bounded head/tail view while keeping the durable source untouched."""

    original_bytes = len(content.encode("utf-8"))
    if original_bytes <= max_bytes:
        return content, {
            "compacted": False,
            "original_bytes": original_bytes,
            "delivered_bytes": original_bytes,
            "omitted_lines": 0,
        }

    lines = content.splitlines()
    marker_template = (
        "[实时感知视图已压缩：LifeEngine 仍保留完整可追溯投影；"
        "不确定时使用 inner_query / fetch_chat_history 按需回想。"
        " original_bytes={original_bytes}; omitted_lines={omitted_lines}]"
    )
    provisional_marker = marker_template.format(
        original_bytes=original_bytes,
        omitted_lines=len(lines),
    )
    marker_bytes = len(provisional_marker.encode("utf-8")) + 2
    available = max(0, max_bytes - marker_bytes)
    head_budget = available // 3
    tail_budget = available - head_budget

    head: list[str] = []
    head_used = 0
    head_end = 0
    for index, line in enumerate(lines):
        cost = len(line.encode("utf-8")) + 1
        if head_used + cost > head_budget:
            break
        head.append(line)
        head_used += cost
        head_end = index + 1

    tail_reversed: list[str] = []
    tail_used = 0
    tail_start = len(lines)
    for index in range(len(lines) - 1, head_end - 1, -1):
        line = lines[index]
        cost = len(line.encode("utf-8")) + 1
        if tail_used + cost > tail_budget:
            break
        tail_reversed.append(line)
        tail_used += cost
        tail_start = index
    tail = list(reversed(tail_reversed))
    omitted_lines = max(0, tail_start - head_end)
    marker = marker_template.format(
        original_bytes=original_bytes,
        omitted_lines=omitted_lines,
    )
    compacted = "\n".join([*head, marker, *tail])
    delivered_bytes = len(compacted.encode("utf-8"))
    if delivered_bytes > max_bytes:
        raise RuntimeError(
            "bounded perception projection exceeded its configured byte budget"
        )
    return compacted, {
        "compacted": True,
        "original_bytes": original_bytes,
        "delivered_bytes": delivered_bytes,
        "omitted_lines": omitted_lines,
        "max_bytes": max_bytes,
    }


class ContextBridge:
    """Build isolated context and publish final voice turns to LifeEngine."""

    def __init__(
        self, config: Any, consciousness: Any, store: VoiceEpisodeStore
    ) -> None:
        self._config = config
        self._consciousness = consciousness
        self._store = store
        self._last_perception_stats: dict[str, Any] = {}

    def build_system_prompt(self) -> str:
        personality = get_core_config().personality
        identity = {
            "nickname": personality.nickname,
            "aliases": list(personality.alias_names),
            "personality_core": personality.personality_core,
            "personality_side": personality.personality_side,
            "identity": personality.identity,
            "background_story": personality.background_story,
            "reply_style": personality.reply_style,
            "safety_guidelines": list(personality.safety_guidelines),
            "negative_behaviors": list(personality.negative_behaviors),
        }
        parts = [
            "你是 Elysium 当前运行中的爱莉意识，不是临时客服或替代人格。",
            "这是一个独立的实时语音意识实例。你可以自然地倾听、思考、打断、沉默或表达；根据自己的意志和当下情境行动。",
            "不要朗读内部上下文、协议或工具名称。把它们当作你已经知道的内在状态。",
            "[身份配置]\n" + json.dumps(identity, ensure_ascii=False, indent=2),
        ]
        transcript = self._store.transcript()
        if transcript:
            parts.append(
                "[本意识实例已发生的完整语音历史]\n"
                + json.dumps(transcript, ensure_ascii=False, indent=2)
            )
        instructions = str(self._config.full_duplex.instructions or "").strip()
        if instructions:
            parts.append("[用户配置的附加指令]\n" + instructions)
        return "\n\n".join(parts)

    def build_llm_context_prefix(self) -> tuple[str, Any | None]:
        """Build one transient world context and its uncommitted delivery."""

        prepared = self._consciousness.prepare_perception()
        if prepared is None:
            self._last_perception_stats = {}
            return "", None
        max_bytes = int(self._config.session.perception_context_max_bytes)
        wrapper_bytes = len((_PERCEPTION_PREFIX + _PERCEPTION_SUFFIX).encode("utf-8"))
        content, stats = _compact_context_lines(
            prepared.content,
            max_bytes=max_bytes - wrapper_bytes,
        )
        self._last_perception_stats = stats
        return (
            f"{_PERCEPTION_PREFIX}{content}{_PERCEPTION_SUFFIX}",
            prepared,
        )

    def perception_projection_stats(self) -> dict[str, Any]:
        """Return content-free metrics for the latest transient projection."""

        return dict(self._last_perception_stats)

    async def record_transcript(
        self, role: str, text: str, *, provider_event_id: str = ""
    ) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported transcript role: {role}")
        if not text:
            return
        payload = {
            "role": role,
            "text": text,
            "provider_event_id": provider_event_id,
        }
        await self._store.append_async("transcript.final", payload)
        if not self._config.session.record_to_life:
            return
        from .life_binding import get_running_life_service

        service = get_running_life_service()
        if service is None:
            if self._config.session.require_life_engine:
                raise RuntimeError("最终转写无法写入 LifeEngine")
            return
        personality = get_core_config().personality
        is_user = role == "user"
        message = Message(
            message_id=provider_event_id or f"voice-{uuid.uuid4().hex}",
            content=text,
            processed_plain_text=text,
            message_type=MessageType.VOICE,
            sender_id=self._config.session.user_id
            if is_user
            else self._consciousness.instance_id,
            sender_name=self._config.session.user_name
            if is_user
            else personality.nickname,
            platform="voice_live",
            chat_type="private",
            stream_id=self._consciousness.stream_id,
            extra={
                "episode_id": self._store.episode_id,
                "consciousness_instance_id": self._consciousness.instance_id,
                "provider_event_id": provider_event_id,
            },
        )
        await service.record_message(
            message, direction="received" if is_user else "sent"
        )
