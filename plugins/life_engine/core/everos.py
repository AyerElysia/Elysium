"""EverOS bridge for life_engine long-term memory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.core.models.message import Message

from .config import LifeEngineConfig

logger = get_logger("life_engine.everos", display="life_engine.everos")

_PATH_SAFE_RE = re.compile(r"^[a-zA-Z0-9_.@+-]+$")
_PATH_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_.@+-]+")
_EVEROS_ID_HASH_LENGTH = 12
_DATA_URI_RE = re.compile(r"^data:[^,;]+(?:;[^,]*)*;base64,", re.IGNORECASE)
_BASE64_BLOB_RE = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
_BASE64_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{128,}={0,2}(?![A-Za-z0-9+/_=-])")
_DATA_URI_TOKEN_RE = re.compile(r"data:[^\s,;]+(?:;[^\s,;]+)*;base64,[^\s]+", re.IGNORECASE)
_MAX_EVEROS_TIMESTAMP_MS = 32_503_680_000_000  # 3000-01-01 UTC
_SAFE_STRUCTURED_TEXT_KEYS = frozenset(
    {"text", "content", "message", "caption", "description", "title", "summary", "transcript"}
)
_STRUCTURED_DATA_KEYS = frozenset({"data", "value"})
_STRUCTURED_SKIP_KEYS = frozenset(
    {
        "base64",
        "image_base64",
        "audio_base64",
        "video_base64",
        "url",
        "uri",
        "path",
        "file",
        "file_path",
        "raw",
        "raw_data",
        "media",
        "media_list",
        "attachments",
        "extra",
        "metadata",
    }
)
_TEXT_SEGMENT_TYPES = frozenset({"text", "plain", "markdown", "rich_text"})
_MEDIA_MESSAGE_LABELS = {
    "image": "[图片消息]",
    "emoji": "[表情消息]",
    "voice": "[语音消息]",
    "audio": "[语音消息]",
    "record": "[语音消息]",
    "video": "[视频消息]",
    "file": "[文件消息]",
}


def sanitize_everos_id(value: Any, *, fallback: str = "neo", max_length: int = 128) -> str:
    """Return a collision-resistant EverOS path-safe identifier."""
    if max_length < 1:
        raise ValueError("max_length must be positive")

    fallback_id = _PATH_UNSAFE_RE.sub("_", str(fallback or "").strip()).strip("_")
    if not fallback_id or fallback_id in {".", ".."}:
        fallback_id = "neo"
    fallback_id = fallback_id[:max_length]
    if fallback_id in {".", ".."}:
        fallback_id = "neo"[:max_length]

    raw_text = str(value or "")
    text = raw_text.strip()
    if not text or text == "..":
        return fallback_id

    sanitized = _PATH_UNSAFE_RE.sub("_", text).strip("_")
    if not sanitized or sanitized in {".", ".."}:
        sanitized = fallback_id
    if sanitized == raw_text and len(sanitized) <= max_length:
        return sanitized

    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:_EVEROS_ID_HASH_LENGTH]
    if max_length <= len(digest):
        return digest[:max_length]
    prefix_length = max_length - len(digest) - 1
    return f"{sanitized[:prefix_length]}-{digest}" if prefix_length else digest


def build_everos_identity(
    platform: Any,
    value: Any,
    *,
    fallback: str,
    max_length: int = 128,
) -> str:
    """Build a platform-qualified EverOS identity from a raw platform ID."""
    platform_id = sanitize_everos_id(platform, fallback="unknown", max_length=32)
    suffix_budget = max(1, max_length - len(platform_id) - 1)
    value_id = sanitize_everos_id(value, fallback=fallback, max_length=suffix_budget)
    prefix = f"{platform_id}@"
    if value_id.startswith(prefix):
        return value_id
    return f"{prefix}{value_id}"


def _everos_cfg(cfg: LifeEngineConfig | None) -> Any | None:
    return getattr(cfg, "everos", None) if cfg is not None else None


def is_everos_enabled(cfg: LifeEngineConfig | None) -> bool:
    everos = _everos_cfg(cfg)
    return bool(everos is not None and getattr(everos, "enabled", False))


def is_everos_message_sync_enabled(
    cfg: LifeEngineConfig | None,
    direction: str,
) -> bool:
    everos = _everos_cfg(cfg)
    if not is_everos_enabled(cfg) or not bool(getattr(everos, "sync_messages", True)):
        return False
    if direction == "sent" and not bool(getattr(everos, "sync_sent_messages", True)):
        return False
    return True


def is_everos_recall_enabled(cfg: LifeEngineConfig | None) -> bool:
    everos = _everos_cfg(cfg)
    return is_everos_enabled(cfg) and bool(getattr(everos, "recall_to_chatter", True))


def _resolve_endpoint(base_url: str, endpoint: str) -> str:
    return f"{str(base_url or '').rstrip('/')}/{endpoint.lstrip('/')}"


def _message_type_value(message: Message) -> str:
    message_type = getattr(message, "message_type", "")
    return str(getattr(message_type, "value", message_type) or "").strip().lower()


def _looks_like_raw_media_data(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _DATA_URI_RE.match(text):
        return True
    compact = re.sub(r"\s+", "", text)
    return len(compact) >= 128 and len(compact) % 4 == 0 and bool(_BASE64_BLOB_RE.fullmatch(compact))


def _sanitize_everos_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or _looks_like_raw_media_data(text):
        return ""
    text = _DATA_URI_TOKEN_RE.sub("[媒体数据已省略]", text)
    return _BASE64_TOKEN_RE.sub("[媒体数据已省略]", text).strip()


def _extract_safe_structured_text(value: Any) -> str:
    """Extract only explicit text fields; never stringify opaque message payloads."""
    parts: list[str] = []
    seen: set[str] = set()

    def add(candidate: Any) -> None:
        text = _sanitize_everos_text(candidate)
        if text and text not in seen:
            seen.add(text)
            parts.append(text)

    def walk(item: Any, *, allow_text: bool = False) -> None:
        if isinstance(item, str):
            if allow_text:
                add(item)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                walk(child, allow_text=allow_text)
            return
        if not isinstance(item, dict):
            return

        segment_type = str(item.get("type") or item.get("kind") or "").strip().lower()
        is_text_segment = segment_type in _TEXT_SEGMENT_TYPES
        for raw_key, child in item.items():
            key = str(raw_key or "").strip().lower()
            if key in _STRUCTURED_SKIP_KEYS:
                continue
            if key in _SAFE_STRUCTURED_TEXT_KEYS:
                walk(child, allow_text=True)
            elif key in _STRUCTURED_DATA_KEYS and is_text_segment:
                walk(child, allow_text=True)
            elif isinstance(child, (dict, list, tuple)):
                walk(child)

    walk(value, allow_text=isinstance(value, (list, tuple)))
    return "\n".join(parts)


def _message_content_text(message: Message) -> str:
    plain = _sanitize_everos_text(getattr(message, "processed_plain_text", ""))
    if plain:
        return plain

    message_type = _message_type_value(message)
    content = getattr(message, "content", "")
    if isinstance(content, str) and message_type not in _MEDIA_MESSAGE_LABELS:
        text = _sanitize_everos_text(content)
    elif isinstance(content, str):
        text = ""
    else:
        text = _extract_safe_structured_text(content)
    if text:
        return text

    return _MEDIA_MESSAGE_LABELS.get(message_type, "")


def _message_timestamp_ms(message: Message) -> int:
    raw_time = getattr(message, "time", None)
    try:
        seconds = raw_time.timestamp() if isinstance(raw_time, datetime) else float(raw_time)
        timestamp = int(seconds * 1000)
        if not math.isfinite(seconds) or timestamp <= 0 or timestamp > _MAX_EVEROS_TIMESTAMP_MS:
            raise ValueError("invalid timestamp")
        return timestamp
    except (OverflowError, TypeError, ValueError):
        return int(time.time() * 1000)


def build_everos_add_payload(
    cfg: LifeEngineConfig,
    message: Message,
    *,
    direction: str = "received",
) -> dict[str, Any] | None:
    """Build the EverOS ``/memory/add`` payload for one Neo message."""
    if not is_everos_message_sync_enabled(cfg, direction):
        return None

    content = _message_content_text(message)
    if not content:
        return None

    everos = cfg.everos
    platform = str(getattr(message, "platform", "") or "").strip()
    sender_role = str(getattr(message, "sender_role", "") or "").lower()
    role = "assistant" if direction == "sent" or sender_role in {"bot", "assistant"} else "user"
    sender_fallback = "neo_bot" if role == "assistant" else "neo_user"
    sender_id = build_everos_identity(
        platform,
        getattr(message, "sender_id", "") or sender_fallback,
        fallback=sender_fallback,
    )
    session_id = build_everos_identity(
        platform,
        getattr(message, "stream_id", "") or "neo_session",
        fallback="neo_session",
    )
    sender_name = _sanitize_everos_text(
        getattr(message, "sender_cardname", None)
        or getattr(message, "sender_name", "")
    ) or sender_id

    return {
        "session_id": session_id,
        "app_id": sanitize_everos_id(
            getattr(everos, "app_id", "neo_mofox"),
            fallback="neo_mofox",
        ),
        "project_id": sanitize_everos_id(
            getattr(everos, "project_id", "default"),
            fallback="default",
        ),
        "messages": [
            {
                "sender_id": sender_id,
                "sender_name": sender_name,
                "role": role,
                "timestamp": _message_timestamp_ms(message),
                "content": content,
            }
        ],
    }


def build_everos_search_payload(
    cfg: LifeEngineConfig,
    *,
    query: str,
    user_id: str,
    platform: str = "",
) -> dict[str, Any] | None:
    """Build the EverOS ``/memory/search`` payload."""
    if not is_everos_recall_enabled(cfg):
        return None
    query_text = _sanitize_everos_text(query)
    if not query_text:
        return None

    everos = cfg.everos
    return {
        "user_id": build_everos_identity(platform, user_id, fallback="neo_user"),
        "app_id": sanitize_everos_id(
            getattr(everos, "app_id", "neo_mofox"),
            fallback="neo_mofox",
        ),
        "project_id": sanitize_everos_id(
            getattr(everos, "project_id", "default"),
            fallback="default",
        ),
        "query": query_text,
        "method": str(getattr(everos, "search_method", "hybrid") or "hybrid"),
        "top_k": int(getattr(everos, "top_k", 5) or 5),
        "include_profile": bool(getattr(everos, "include_profile", True)),
    }


def _sync_post_json(url: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Neo-MoFox life_engine EverOS bridge",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"EverOS HTTP {exc.code}: {raw[:500]}") from exc
    except Exception as exc:
        raise RuntimeError(f"EverOS request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("EverOS response is not a JSON object")
    return data


def _request_timeout(cfg: LifeEngineConfig, attr: str) -> float:
    everos = cfg.everos
    fallback = float(getattr(everos, "timeout_seconds", 3.0) or 3.0)
    return float(getattr(everos, attr, fallback) or fallback)


async def _post_json(
    cfg: LifeEngineConfig,
    endpoint: str,
    payload: dict[str, Any],
    *,
    timeout_attr: str = "timeout_seconds",
) -> dict[str, Any]:
    everos = cfg.everos
    url = _resolve_endpoint(str(getattr(everos, "base_url", "") or ""), endpoint)
    timeout = _request_timeout(cfg, timeout_attr)
    return await asyncio.to_thread(_sync_post_json, url, payload, timeout)


async def sync_message_to_everos(
    cfg: LifeEngineConfig,
    message: Message,
    *,
    direction: str = "received",
) -> None:
    """Best-effort sync of one Neo message into EverOS."""
    payload = build_everos_add_payload(cfg, message, direction=direction)
    if payload is None:
        return
    try:
        await _post_json(cfg, "/api/v1/memory/add", payload, timeout_attr="write_timeout_seconds")
        if bool(getattr(cfg.everos, "flush_after_add", False)):
            flush_payload = {
                "session_id": payload["session_id"],
                "app_id": payload["app_id"],
                "project_id": payload["project_id"],
            }
            await _post_json(cfg, "/api/v1/memory/flush", flush_payload, timeout_attr="write_timeout_seconds")
    except Exception as exc:
        logger.warning(f"EverOS 消息同步失败，已忽略: {exc}")


def _shorten(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def format_everos_recall_block(response: dict[str, Any], *, max_chars: int = 1800) -> str:
    """Format EverOS search response into a compact chatter context block."""
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return ""

    lines: list[str] = []
    profiles = data.get("profiles") if isinstance(data.get("profiles"), list) else []
    for profile in profiles[:1]:
        if not isinstance(profile, dict):
            continue
        profile_data = profile.get("profile_data")
        if profile_data:
            lines.append(f"- 用户画像：{_shorten(json.dumps(profile_data, ensure_ascii=False, default=str), 360)}")

    episodes = data.get("episodes") if isinstance(data.get("episodes"), list) else []
    for episode in episodes[:5]:
        if not isinstance(episode, dict):
            continue
        summary = episode.get("summary") or episode.get("subject") or episode.get("episode")
        detail = episode.get("episode") or ""
        text = _shorten(summary, 220)
        if detail and detail != summary:
            text = f"{text} | {_shorten(detail, 260)}"
        facts = episode.get("atomic_facts") if isinstance(episode.get("atomic_facts"), list) else []
        fact_texts = [
            _shorten(fact.get("content"), 120)
            for fact in facts[:2]
            if isinstance(fact, dict) and fact.get("content")
        ]
        if fact_texts:
            text = f"{text} | 事实：{'；'.join(fact_texts)}"
        lines.append(f"- 过往片段：{text}")

    unprocessed = data.get("unprocessed_messages") if isinstance(data.get("unprocessed_messages"), list) else []
    for item in unprocessed[:2]:
        if not isinstance(item, dict):
            continue
        lines.append(f"- 未沉淀消息：{_shorten(item.get('content'), 220)}")

    if not lines:
        return ""

    block = "### EverOS 长期记忆召回\n" + "\n".join(lines)
    return _shorten(block, max_chars)


async def recall_everos_for_chatter(
    cfg: LifeEngineConfig,
    *,
    query: str,
    user_id: str,
    platform: str = "",
) -> str:
    """Best-effort EverOS recall formatted for the life_chatter suffix."""
    payload = build_everos_search_payload(
        cfg,
        query=query,
        user_id=user_id,
        platform=platform,
    )
    if payload is None:
        return ""
    try:
        response = await _post_json(cfg, "/api/v1/memory/search", payload, timeout_attr="recall_timeout_seconds")
    except Exception as exc:
        logger.warning(f"EverOS 记忆召回失败，已忽略: {exc}")
        return ""
    max_chars = int(getattr(cfg.everos, "max_recall_chars", 1800) or 1800)
    return format_everos_recall_block(response, max_chars=max_chars)
