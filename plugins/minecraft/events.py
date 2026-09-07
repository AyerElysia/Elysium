"""Minecraft event schemas mapped into the shared append-only Life Event ledger."""
from __future__ import annotations

import json
from typing import Any
from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent


def _shorten_text(value: str, *, max_length: int) -> str:
    # Keep legacy presentation bytes stable too: replay compares the whole event.
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= max_length else normalized[:max_length - 1] + "…"


class MinecraftEventBuilder:
    """Build full authoritative payloads; sequence allocation belongs to the ledger."""

    def build_minecraft_consciousness_decision_event(
        self,
        decision: dict[str, Any],
        context_reference: dict[str, Any],
    ) -> LifeEngineEvent:
        """Build an attributed, idempotent scene decision before body action."""

        decision_id = str(decision.get("decision_id") or "").strip()
        if not decision_id:
            raise ValueError("Minecraft consciousness decision_id must not be empty")
        if decision.get("schema") not in {
            "minecraft.consciousness_decision.v1",
            "minecraft.consciousness_decision.v2",
        }:
            raise ValueError("unknown Minecraft consciousness decision schema")
        if (
            context_reference.get("schema")
            != "minecraft.consciousness_turn_reference.v1"
        ):
            raise ValueError("unknown Minecraft consciousness context schema")
        stream_id = str(context_reference.get("stream_id") or "").strip()
        instance_id = str(context_reference.get("instance_id") or "").strip()
        session_id = str(context_reference.get("session_id") or "").strip()
        if not stream_id or not instance_id or not session_id:
            raise ValueError(
                "Minecraft consciousness decision attribution is incomplete"
            )
        authored_at = str(decision.get("authored_at") or "").strip()
        if not authored_at:
            raise ValueError("Minecraft consciousness authored_at must not be empty")
        raw = json.dumps(
            {
                "decision": dict(decision),
                "context_reference": dict(context_reference),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        kind = str(decision.get("kind") or "").strip()
        intention = str(decision.get("intention") or "").strip()
        reason = str(decision.get("reason") or "").strip()
        visible = json.dumps(
            {
                "decision_id": decision_id,
                "kind": kind,
                "intention": intention,
                "speech": str(decision.get("speech") or ""),
                "task": decision.get("task"),
                "reason": reason,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        seq = 0
        return LifeEngineEvent(
            event_id=decision_id,
            event_type=EventType.CONSCIOUS_ACTIVITY,
            timestamp=authored_at,
            sequence=seq,
            source="minecraft_consciousness",
            source_detail=(
                "Minecraft 场景意识 | "
                f"session={session_id} | instance={instance_id} | kind={kind}"
            ),
            content=_shorten_text(visible, max_length=1200),
            content_type="minecraft_consciousness_decision",
            stream_id=stream_id,
            occurrence_id=decision_id,
            source_instance_id=instance_id,
            correlation_id=session_id,
            content_ref=f"minecraft-consciousness-decision:{decision_id}",
            raw_content=raw,
        )

    def build_minecraft_body_event(
        self,
        body_event: dict[str, Any],
        context_reference: dict[str, Any],
    ) -> LifeEngineEvent:
        """Build one exact game occurrence from the authenticated body stream."""

        if body_event.get("schema") != "minecraft.body_event.v1":
            raise ValueError("unknown Minecraft body event schema")
        if context_reference.get("schema") != "minecraft.body_event_context.v1":
            raise ValueError("unknown Minecraft body event context schema")
        event_id = str(body_event.get("event_id") or "").strip()
        kind = str(body_event.get("kind") or "").strip()
        occurred_at = str(body_event.get("occurred_at") or "").strip()
        game_instance_id = str(body_event.get("instance_id") or "").strip()
        stream_id = str(context_reference.get("stream_id") or "").strip()
        instance_id = str(context_reference.get("instance_id") or "").strip()
        session_id = str(context_reference.get("session_id") or "").strip()
        if not event_id or not kind.startswith("minecraft.") or not occurred_at:
            raise ValueError("Minecraft body event identity is incomplete")
        if not game_instance_id or not stream_id or not instance_id or not session_id:
            raise ValueError("Minecraft body event attribution is incomplete")
        payload = body_event.get("payload")
        if not isinstance(payload, dict):
            raise TypeError("Minecraft body event payload must be an object")
        raw = json.dumps(
            {
                "body_event": dict(body_event),
                "context_reference": dict(context_reference),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(raw.encode("utf-8")) > 24 * 1024:
            raise ValueError("Minecraft body event record exceeds its durable bound")

        is_inbound_chat = kind in {
            "minecraft.chat.received",
            "minecraft.whisper.received",
        }
        sender = str(payload.get("username") or "").strip()
        message = str(payload.get("message") or "").strip()
        if is_inbound_chat and not message:
            raise ValueError("Minecraft inbound chat event has no message")
        visible = (
            message
            if is_inbound_chat
            else json.dumps(
                {
                    "event_id": event_id,
                    "kind": kind,
                    "payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        seq = 0
        return LifeEngineEvent(
            event_id=event_id,
            event_type=(
                EventType.MESSAGE if is_inbound_chat else EventType.CONSCIOUS_ACTIVITY
            ),
            timestamp=occurred_at,
            sequence=seq,
            source="minecraft",
            source_detail=(
                "Minecraft 身体事件 | "
                f"session={session_id} | body_instance={game_instance_id} | kind={kind}"
            ),
            content=_shorten_text(visible, max_length=1200),
            content_type=kind,
            sender=sender or None,
            sender_id=sender or None,
            chat_type="minecraft" if is_inbound_chat else None,
            stream_id=stream_id,
            occurrence_id=event_id,
            source_instance_id=instance_id,
            correlation_id=session_id,
            content_ref=f"minecraft-body-event:{event_id}",
            raw_content=raw,
        )
