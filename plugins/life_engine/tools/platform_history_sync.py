"""Explicit platform-history synchronization.

This capability is intentionally separate from conversation evidence reads.
Imported rows are a platform cache: synchronization does not mark messages as
unread, advance stream activity, or claim the subject experienced them live.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, ClassVar

from sqlalchemy import select

from src.app.plugin_system.api.adapter_api import send_adapter_command
from src.app.plugin_system.base import BaseTool
from src.core.models.sql_alchemy import ChatStreams, Messages, PersonInfo
from src.kernel.db import get_db_session


def _extract_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("messages", "message_list", "list", "records", "items"):
        nested = value.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    nested = value.get("data")
    return [] if nested is value else _extract_messages(nested)


def _plain_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return "" if message is None else str(message)
    parts: list[str] = []
    for item in message:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "segment")
        data = item.get("data")
        if kind == "text" and isinstance(data, dict):
            parts.append(str(data.get("text") or ""))
        else:
            parts.append(f"[{kind}]")
    return "".join(parts)


class LifeEngineSyncPlatformHistoryTool(BaseTool):
    """Synchronize one explicitly named platform stream into the local cache."""

    tool_name = "sync_platform_history"
    tool_description = (
        "显式同步一个已知 stream_id 的平台历史到本地消息缓存。只返回内容无关的同步回执；"
        "不会检索正文、不会自动选择最近会话，也不会把回补消息标成主体实时经历。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal"]

    async def execute(
        self,
        stream_id: Annotated[str, "必须显式提供的目标 stream_id"],
        count: Annotated[int, "最多同步的消息数，1..100"] = 50,
    ) -> tuple[bool, dict[str, Any] | str]:
        target = str(stream_id or "").strip()
        if not target:
            return False, "stream_id is required"
        cfg = getattr(getattr(self.plugin, "config", None), "history_retrieval", None)
        adapter_signature = str(
            getattr(cfg, "adapter_signature", "napcat_adapter:adapter:napcat_adapter")
            or ""
        ).strip()
        if not adapter_signature:
            return False, "platform history adapter is not configured"
        requested = max(1, min(int(count), 100))

        async with get_db_session() as session:
            result = await session.execute(
                select(ChatStreams).where(ChatStreams.stream_id == target)
            )
            stream = result.scalar_one_or_none()
            if stream is None:
                return False, "stream_id does not exist"
            person_user_id = ""
            if stream.person_id:
                person_result = await session.execute(
                    select(PersonInfo.user_id).where(
                        PersonInfo.person_id == stream.person_id
                    )
                )
                person_user_id = str(person_result.scalar_one_or_none() or "")

        if str(stream.chat_type or "") == "group":
            actions = list(
                getattr(cfg, "group_history_actions", ["get_group_msg_history"]) or []
            )
            params: dict[str, Any] = {
                "group_id": str(stream.group_id or ""),
                "count": requested,
            }
            if not params["group_id"]:
                return False, "group stream has no group_id"
        else:
            actions = list(
                getattr(
                    cfg,
                    "private_history_actions",
                    ["get_friend_msg_history", "get_private_msg_history"],
                )
                or []
            )
            params = {"user_id": person_user_id, "count": requested}
            if not person_user_id:
                return False, "private stream has no platform user identity"

        response: dict[str, Any] | None = None
        selected_action = ""
        timeout = float(getattr(cfg, "adapter_timeout_seconds", 8) or 8)
        for action in actions:
            selected_action = str(action or "").strip()
            if not selected_action:
                continue
            candidate = await send_adapter_command(
                adapter_sign=adapter_signature,
                command_name=selected_action,
                command_data=params,
                timeout=timeout,
            )
            if str(candidate.get("status") or "").lower() == "ok":
                response = candidate
                break
        if response is None:
            return False, "platform history synchronization failed"

        raw_rows = _extract_messages(response.get("data"))[:requested]
        normalized: list[Messages] = []
        message_ids: list[str] = []
        for raw in raw_rows:
            message_id = str(
                raw.get("message_id") or raw.get("id") or raw.get("msg_id") or ""
            ).strip()
            if not message_id:
                continue
            sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
            sender_id = str(
                sender.get("user_id")
                or raw.get("user_id")
                or raw.get("sender_id")
                or ""
            )
            raw_content = raw.get("message", raw.get("raw_message", ""))
            content = (
                raw_content
                if isinstance(raw_content, str)
                else json.dumps(raw_content, ensure_ascii=False)
            )
            timestamp = raw.get("time", raw.get("timestamp", 0.0))
            try:
                occurred_at = float(timestamp)
            except (TypeError, ValueError):
                continue
            message_ids.append(message_id)
            normalized.append(
                Messages(
                    message_id=message_id,
                    stream_id=target,
                    person_id=f"{stream.platform}:{sender_id}" if sender_id else None,
                    time=occurred_at,
                    message_type=str(
                        raw.get("message_type") or raw.get("post_type") or "text"
                    ),
                    content=content,
                    processed_plain_text=_plain_text(raw_content),
                    reply_to=None,
                    platform=str(stream.platform or ""),
                )
            )

        inserted = 0
        if normalized:
            async with get_db_session() as session:
                existing_result = await session.execute(
                    select(Messages.message_id).where(
                        Messages.message_id.in_(message_ids)
                    )
                )
                existing = {str(item) for item in existing_result.scalars().all()}
                fresh = [row for row in normalized if row.message_id not in existing]
                session.add_all(fresh)
                inserted = len(fresh)

        identity_digest = hashlib.sha256(
            "\n".join(sorted(set(message_ids))).encode("utf-8")
        ).hexdigest()
        return True, {
            "schema": "elysium.platform_history_sync_receipt.v1",
            "stream_id": target,
            "adapter_action": selected_action,
            "fetched_count": len(raw_rows),
            "eligible_count": len(normalized),
            "inserted_count": inserted,
            "existing_count": len(normalized) - inserted,
            "message_identity_sha256": identity_digest,
            "experience_semantics": "platform_cache_not_live_experience",
        }


PLATFORM_HISTORY_SYNC_TOOLS = [LifeEngineSyncPlatformHistoryTool]
