"""Bridge identity, WorldState and durable voice history into one episode."""

from __future__ import annotations

import json
import uuid
from typing import Any

from src.core.config.core_config import get_core_config
from src.core.models.message import Message, MessageType

from .runtime_store import VoiceEpisodeStore


class ContextBridge:
    """Build isolated context and publish final voice turns to LifeEngine."""

    def __init__(self, config: Any, consciousness: Any, store: VoiceEpisodeStore) -> None:
        self._config = config
        self._consciousness = consciousness
        self._store = store

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
        world_state = self._consciousness.render_world_state()
        if world_state:
            parts.append("[完整 WorldState]\n" + world_state)
        transcript = self._store.transcript()
        if transcript:
            parts.append("[本意识实例已发生的完整语音历史]\n" + json.dumps(transcript, ensure_ascii=False, indent=2))
        instructions = str(self._config.full_duplex.instructions or "").strip()
        if instructions:
            parts.append("[用户配置的附加指令]\n" + instructions)
        return "\n\n".join(parts)

    async def record_transcript(self, role: str, text: str, *, provider_event_id: str = "") -> None:
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
        from plugins.life_engine.service.registry import get_life_engine_service

        service = get_life_engine_service()
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
            sender_id=self._config.session.user_id if is_user else self._consciousness.instance_id,
            sender_name=self._config.session.user_name if is_user else personality.nickname,
            platform="voice_live",
            chat_type="private",
            stream_id=self._consciousness.stream_id,
            extra={
                "episode_id": self._store.episode_id,
                "consciousness_instance_id": self._consciousness.instance_id,
                "provider_event_id": provider_event_id,
            },
        )
        await service.record_message(message, direction="received" if is_user else "sent")
