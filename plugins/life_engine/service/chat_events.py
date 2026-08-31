"""Stable chat fact projection into the authoritative Life Event ledger."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from src.core.models.message import Message, MessageType

from .event_bus import LifeEvent, LifeEventChannel, LifeEventPriority

_NOTICE_FACTS: dict[str, str] = {
    "friend_recall": "chat.message.recalled",
    "group_recall": "chat.message.recalled",
    "group_admin": "chat.admin.changed",
    "group_ban": "chat.member.muted",
    "group_lift_ban": "chat.member.unmuted",
    "group_whole_ban": "chat.member.muted",
    "group_whole_lift_ban": "chat.member.unmuted",
    "group_decrease": "chat.member.left",
    "group_increase": "chat.member.joined",
    "group_card": "chat.member.updated",
    "group_upload": "chat.file.uploaded",
    "group_msg_emoji_like": "chat.reaction.added",
    "essence": "chat.message.pinned",
    "poke": "chat.poke.received",
    "friend_add": "chat.member.joined",
}


def build_chat_message_event(
    message: Message,
    *,
    direction: str,
    envelope: Mapping[str, Any] | None = None,
    adapter_signature: str = "",
    delivery_status: str | None = None,
) -> LifeEvent:
    """Build one durable fact without treating pre-send notification as delivery."""

    if direction not in {"received", "requested", "delivered"}:
        raise ValueError(
            "chat event direction must be received, requested, or delivered"
        )
    if delivery_status not in {None, "confirmed", "failed", "unknown"}:
        raise ValueError("chat delivery status is invalid")
    extra = message.extra if isinstance(message.extra, dict) else {}
    provider = str(message.platform or extra.get("source") or "unknown").strip()
    event_type = _event_type(
        message,
        direction=direction,
        extra=extra,
        delivery_status=delivery_status,
    )
    message_id = str(message.message_id or "").strip()
    stable_identity = {
        "direction": direction,
        "delivery_status": delivery_status,
        "event_type": event_type,
        "provider": provider,
        "message_id": message_id,
        "stream_id": str(message.stream_id or ""),
    }
    digest = hashlib.sha256(
        json.dumps(stable_identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    actor_id = str(
        extra.get("api_actor_id")
        or message.sender_id
        or ("bot" if direction in {"requested", "delivered"} else "unknown")
    )
    metadata: dict[str, Any] = {
        "actor_type": "platform_user" if direction == "received" else "consciousness",
        "actor_id": actor_id,
        "actor_display_name": str(
            message.sender_cardname or message.sender_name or actor_id
        ),
        "visibility": {"scope": "private", "audience": []},
        "chat": _chat_payload(message, direction=direction, extra=extra),
        "provider_identity": _provider_identity(
            provider,
            message=message,
            envelope=envelope,
            adapter_signature=adapter_signature,
        ),
    }
    receipt = _provider_receipt(extra)
    if receipt:
        metadata["provider_receipt"] = receipt
    activity_lineage = {
        key: str(extra.get(key) or "").strip()
        for key in (
            "conscious_activity_id",
            "tool_call_id",
            "origin_turn_key",
            "origin_stream_id",
        )
        if str(extra.get(key) or "").strip()
    }
    if activity_lineage:
        metadata["conscious_activity_lineage"] = activity_lineage
    source_connection = str(adapter_signature or extra.get("source_connection") or "")
    if source_connection:
        metadata["source_connection"] = source_connection
    return LifeEvent(
        event_id=f"chat_{digest}",
        sequence=0,
        timestamp=_occurred_at(message.time),
        source=adapter_signature or f"{provider}_adapter",
        channel=LifeEventChannel.CHAT.value,
        event_type=event_type,
        content=_content(message),
        stream_id=str(message.stream_id or ""),
        reply_target=(
            {"type": "chat", "id": str(message.stream_id)}
            if message.stream_id
            else None
        ),
        priority=int(LifeEventPriority.NORMAL),
        salience=0.6,
        metadata=metadata,
        occurrence_id=f"chat:{digest}",
        source_instance_id=str(extra.get("consciousness_instance_id") or "chat_global"),
        correlation_id=str(
            extra.get("correlation_id")
            or extra.get("episode_id")
            or extra.get("origin_turn_key")
            or ""
        ),
        causation_id=str(
            extra.get("conscious_activity_id")
            or extra.get("causation_id")
            or ""
        ),
        content_ref=str(extra.get("content_ref") or ""),
    )


def _event_type(
    message: Message,
    *,
    direction: str,
    extra: Mapping[str, Any],
    delivery_status: str | None,
) -> str:
    if direction == "requested":
        return "chat.message.send_requested"
    if direction == "delivered":
        status = delivery_status or "confirmed"
        return f"chat.message.delivery_{status}"
    if message.message_type is not MessageType.UNKNOWN and not extra.get("is_notice"):
        return "chat.message.received"
    notice_kind = str(
        extra.get("notice_type")
        or extra.get("provider_notice_kind")
        or extra.get("feishu_event_type")
        or "unknown"
    ).strip()
    if notice_kind == "notify":
        notice_kind = str(extra.get("sub_type") or "notify")
    return _NOTICE_FACTS.get(notice_kind, "chat.provider_notice.received")


def _chat_payload(
    message: Message,
    *,
    direction: str,
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message_id": str(message.message_id or ""),
        "stream_id": str(message.stream_id or ""),
        "direction": direction,
        "platform": str(message.platform or ""),
        "chat_type": str(message.chat_type or ""),
        "message_type": getattr(
            message.message_type, "value", str(message.message_type)
        ),
        "sender": {
            "id": str(message.sender_id or ""),
            "name": str(message.sender_name or ""),
            "card_name": message.sender_cardname,
            "role": message.sender_role,
        },
        "reply_to": message.reply_to,
        "parts": _parts(message),
        "attachments": [item.to_descriptor() for item in message.attachments],
    }
    provider_kind = str(
        extra.get("notice_type") or extra.get("provider_notice_kind") or ""
    )
    if provider_kind:
        payload["provider_kind"] = provider_kind
    for key in (
        "target_id",
        "group_id",
        "target_group_id",
        "target_user_id",
        "root_message_id",
    ):
        value = extra.get(key)
        if value not in {None, ""}:
            payload[key] = str(value)
    return payload


def _parts(message: Message) -> list[dict[str, Any]]:
    content = _content(message)
    parts: list[dict[str, Any]] = []
    if content:
        parts.append({"type": "text", "text": content})
    for attachment in message.attachments:
        descriptor = attachment.to_descriptor()
        parts.append({"type": attachment.segment_type.value, "attachment": descriptor})
    if not parts:
        parts.append(
            {"type": getattr(message.message_type, "value", "unknown"), "text": ""}
        )
    return parts


def _provider_identity(
    provider: str,
    *,
    message: Message,
    envelope: Mapping[str, Any] | None,
    adapter_signature: str,
) -> dict[str, Any]:
    extra = message.extra if isinstance(message.extra, dict) else {}
    identity: dict[str, Any] = {
        "provider": provider,
        "adapter_signature": adapter_signature or None,
        "message_id": str(message.message_id or ""),
    }
    key_names = {
        "qq": ("group_id", "target_group_id", "target_user_id"),
        "feishu": (
            "feishu_event_id",
            "feishu_message_id",
            "feishu_chat_id",
            "feishu_open_id",
            "feishu_user_id",
            "feishu_union_id",
        ),
    }
    for key in key_names.get(provider.lower(), ()):
        value = extra.get(key)
        if value not in {None, ""}:
            identity[key] = str(value)
    if envelope:
        message_info = envelope.get("message_info")
        if isinstance(message_info, Mapping):
            raw_id = message_info.get("message_id")
            if raw_id not in {None, ""}:
                identity["raw_message_id"] = str(raw_id)
    return {key: value for key, value in identity.items() if value is not None}


def _provider_receipt(extra: Mapping[str, Any]) -> dict[str, Any]:
    receipt = extra.get("provider_receipt")
    if isinstance(receipt, Mapping):
        return {str(key): value for key, value in receipt.items()}
    provider_message_id = extra.get("provider_message_id")
    if provider_message_id not in {None, ""}:
        return {"message_id": str(provider_message_id)}
    return {}


def _content(message: Message) -> str:
    if message.processed_plain_text is not None:
        return str(message.processed_plain_text)
    if isinstance(message.content, str):
        return message.content
    return json.dumps(message.content, ensure_ascii=False, default=str)


def _occurred_at(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromtimestamp(float(value), tz=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().isoformat()


def build_chat_provider_notice_event(
    raw: Mapping[str, Any],
    *,
    adapter_signature: str = "",
) -> LifeEvent:
    """Build a durable provider notice fact from one non-message envelope."""

    message_info = raw.get("message_info")
    info = message_info if isinstance(message_info, Mapping) else {}
    extra_value = info.get("extra")
    extra = extra_value if isinstance(extra_value, Mapping) else {}
    provider = str(info.get("platform") or extra.get("provider") or "unknown")
    notice_kind = str(
        extra.get("notice_type")
        or extra.get("provider_notice_kind")
        or extra.get("feishu_event_type")
        or info.get("message_type")
        or "unknown"
    )
    sub_type = str(extra.get("sub_type") or "")
    mapped_kind = sub_type if notice_kind == "notify" and sub_type else notice_kind
    event_type = _NOTICE_FACTS.get(mapped_kind, "chat.provider_notice.received")
    stream_id = _notice_stream_id(info)
    provider_identity = _notice_provider_identity(
        provider,
        info=info,
        extra=extra,
        adapter_signature=adapter_signature,
    )
    stable_identity = {
        "provider": provider,
        "event_type": event_type,
        "notice_kind": notice_kind,
        "sub_type": sub_type,
        "stream_id": stream_id,
        "provider_identity": provider_identity,
    }
    digest = hashlib.sha256(
        json.dumps(
            stable_identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    user_info_value = info.get("user_info")
    user_info = user_info_value if isinstance(user_info_value, Mapping) else {}
    actor_id = str(user_info.get("user_id") or extra.get("user_id") or "unknown")
    occurred_at = info.get("time") or extra.get("time") or datetime.now(UTC)
    raw_identity_value = extra.get("provider_raw_identity")
    raw_identity = raw_identity_value if isinstance(raw_identity_value, Mapping) else {}
    chat = {
        "message_id": str(info.get("message_id") or f"notice_{digest}"),
        "stream_id": stream_id,
        "direction": "received",
        "platform": provider,
        "chat_type": "group" if info.get("group_info") else "private",
        "message_type": "notice",
        "sender": {
            "id": actor_id,
            "name": str(user_info.get("user_nickname") or actor_id),
            "card_name": user_info.get("user_cardname"),
            "role": user_info.get("user_role"),
        },
        "reply_to": (
            extra.get("message_id")
            or extra.get("target_message_id")
            or raw_identity.get("message_id")
        ),
        "parts": [{"type": "notice", "text": str(extra.get("text_description") or "")}],
        "attachments": [],
        "provider_kind": notice_kind,
    }
    if sub_type:
        chat["provider_sub_type"] = sub_type
    return LifeEvent(
        event_id=f"chat_notice_{digest}",
        sequence=0,
        timestamp=_occurred_at(occurred_at),
        source=adapter_signature or f"{provider}_adapter",
        channel=LifeEventChannel.CHAT.value,
        event_type=event_type,
        content=str(extra.get("text_description") or notice_kind),
        stream_id=stream_id,
        reply_target={"type": "chat", "id": stream_id} if stream_id else None,
        priority=int(LifeEventPriority.NORMAL),
        salience=0.5,
        metadata={
            "actor_type": "platform_user",
            "actor_id": actor_id,
            "actor_display_name": str(user_info.get("user_nickname") or actor_id),
            "visibility": {"scope": "private", "audience": []},
            "chat": chat,
            "provider_identity": provider_identity,
            "provider_notice": {
                "kind": notice_kind,
                "sub_type": sub_type or None,
                "raw_identity": dict(extra.get("provider_raw_identity") or {}),
            },
        },
        occurrence_id=f"chat-notice:{digest}",
        source_instance_id=str(extra.get("consciousness_instance_id") or "chat_global"),
        correlation_id=str(extra.get("correlation_id") or ""),
        causation_id=str(extra.get("causation_id") or ""),
    )


def _notice_stream_id(info: Mapping[str, Any]) -> str:
    group_value = info.get("group_info")
    group = group_value if isinstance(group_value, Mapping) else {}
    user_value = info.get("user_info")
    user = user_value if isinstance(user_value, Mapping) else {}
    platform = str(info.get("platform") or "unknown")
    if group.get("group_id") not in {None, ""}:
        return f"{platform}:group:{group['group_id']}"
    if user.get("user_id") not in {None, ""}:
        return f"{platform}:private:{user['user_id']}"
    return ""


def _notice_provider_identity(
    provider: str,
    *,
    info: Mapping[str, Any],
    extra: Mapping[str, Any],
    adapter_signature: str,
) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "provider": provider,
        "adapter_signature": adapter_signature or None,
        "message_id": str(info.get("message_id") or ""),
    }
    raw_identity = extra.get("provider_raw_identity")
    if isinstance(raw_identity, Mapping):
        identity.update(
            {
                str(key): value
                for key, value in raw_identity.items()
                if value not in {None, ""}
            }
        )
    return {key: value for key, value in identity.items() if value not in {None, ""}}


__all__ = ["build_chat_message_event", "build_chat_provider_notice_event"]
