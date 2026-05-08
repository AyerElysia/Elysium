"""life_engine 聊天历史检索与回补工具。"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from src.app.plugin_system.api import log_api
from src.app.plugin_system.api.adapter_api import send_adapter_command
from src.core.components import BaseTool
from src.core.models.message import Message
from src.core.models.sql_alchemy import ChatStreams
from src.kernel.db import QueryBuilder

from ..core.config import LifeEngineConfig
from ..service import LifeEngineService
from ._utils import _pick_latest_target_stream_id

logger = log_api.get_logger("life_engine.chat_history_tools")

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _parse_time_bound(raw: str | float | int | None) -> float | None:
    """解析时间边界（支持 Unix 时间戳和 ISO 时间）。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip()
    if not text:
        return None

    try:
        return float(text)
    except ValueError:
        pass

    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _to_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat()
    except Exception:
        return None


def _message_time(message: Message) -> float | None:
    raw = getattr(message, "time", None)
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _message_text(message: Message) -> str:
    processed = getattr(message, "processed_plain_text", None)
    if isinstance(processed, str) and processed.strip():
        return processed
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return str(content)


def _normalize_chat_type(raw: str | None) -> str:
    value = str(raw or "").lower().strip()
    if value in {"group", "private", "discuss"}:
        return value
    return value or "unknown"


def _dedupe_key(item: dict[str, Any]) -> str:
    platform = str(item.get("platform") or "")
    stream_id = str(item.get("stream_id") or "")
    message_id = str(item.get("message_id") or "")
    if message_id:
        return f"{platform}|{stream_id}|{message_id}"

    seed = (
        f"{platform}|{stream_id}|{item.get('time_ts')}|"
        f"{item.get('sender_id')}|{item.get('content')}"
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()  # noqa: S324
    return f"{platform}|{stream_id}|fallback|{digest}"


def _extract_backfill_text(raw_message: dict[str, Any]) -> str:
    raw_text = raw_message.get("raw_message")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text

    message = raw_message.get("message")
    if isinstance(message, str):
        return message

    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            seg_type = str(segment.get("type") or "")
            seg_data = segment.get("data")
            if seg_type == "text" and isinstance(seg_data, dict):
                parts.append(str(seg_data.get("text") or ""))
            else:
                parts.append(f"[{seg_type or 'segment'}]")
        text = "".join(parts).strip()
        if text:
            return text

    if message is None:
        return ""
    return str(message)


def _extract_backfill_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []

    for key in ("messages", "message_list", "list", "records", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    nested = data.get("data")
    if nested is data:
        return []
    return _extract_backfill_list(nested)


class LifeEngineFetchChatHistoryTool(BaseTool):
    """检索聊天历史，并按需进行 NapCat 回补。"""

    tool_name: str = "fetch_chat_history"
    tool_description: str = (
        "检索聊天历史消息。默认只检索当前聊天流；如果当前不是聊天态，则自动选择最近外部聊天流。需要跨流时必须显式传 cross_stream=true "
        "或 stream_ids。支持关键词/正则、时间范围、上下文窗口。"
        "默认优先本地历史；只有 source_mode='napcat' 或 force_backfill=true 时才通过 NapCat 回补。"
    )
    chatter_allow: list[str] = ["life_chatter"]

    def _history_cfg(self) -> Any:
        config = getattr(self.plugin, "config", None)
        if isinstance(config, LifeEngineConfig):
            return config.history_retrieval
        return None

    def _resolved_limit(self, requested: int) -> int:
        cfg = self._history_cfg()
        default_limit = int(getattr(cfg, "tool_default_limit", _DEFAULT_LIMIT) or _DEFAULT_LIMIT)
        max_limit = int(getattr(cfg, "tool_max_limit", _MAX_LIMIT) or _MAX_LIMIT)
        if requested <= 0:
            return max(1, min(default_limit, max_limit))
        return max(1, min(int(requested), max_limit))

    async def _resolve_stream_candidates(
        self,
        *,
        stream_ids: list[str],
        cross_stream: bool,
        platform: str,
    ) -> list[dict[str, Any]]:
        cfg = self._history_cfg()
        max_candidates = int(getattr(cfg, "max_candidate_streams", 12) or 12)
        cleaned_ids = [sid.strip() for sid in stream_ids if str(sid or "").strip()]

        if cleaned_ids:
            records = await QueryBuilder(ChatStreams).filter(stream_id__in=cleaned_ids).all()
            record_map = {str(rec.stream_id): rec for rec in records}
            resolved: list[dict[str, Any]] = []
            for stream_id in cleaned_ids:
                rec = record_map.get(stream_id)
                if rec is None:
                    resolved.append(
                        {
                            "stream_id": stream_id,
                            "platform": platform,
                            "chat_type": "unknown",
                            "group_id": "",
                            "group_name": "",
                        }
                    )
                    continue
                resolved.append(
                    {
                        "stream_id": str(rec.stream_id),
                        "platform": str(rec.platform or ""),
                        "chat_type": _normalize_chat_type(rec.chat_type),
                        "group_id": str(rec.group_id or ""),
                        "group_name": str(rec.group_name or ""),
                    }
                )
            return resolved

        chat_stream = getattr(self, "chat_stream", None)
        current_stream_id = str(getattr(chat_stream, "stream_id", "") or "")
        current_platform = str(getattr(chat_stream, "platform", "") or "")

        if not cross_stream:
            target_stream = current_stream_id.strip()
            if not target_stream:
                target_stream = _pick_latest_target_stream_id(self.plugin) or ""
            if not target_stream:
                return []
            record = await QueryBuilder(ChatStreams).filter(stream_id=target_stream).first()
            if record is None:
                return [
                    {
                        "stream_id": target_stream,
                        "platform": current_platform or platform,
                        "chat_type": _normalize_chat_type(getattr(chat_stream, "chat_type", "unknown")),
                        "group_id": "",
                        "group_name": "",
                    }
                ]
            return [
                {
                    "stream_id": str(record.stream_id),
                    "platform": str(record.platform or ""),
                    "chat_type": _normalize_chat_type(record.chat_type),
                    "group_id": str(record.group_id or ""),
                    "group_name": str(record.group_name or ""),
                }
            ]

        query = QueryBuilder(ChatStreams).order_by("-last_active_time").limit(max(1, max_candidates))
        target_platform = (platform or current_platform).strip()
        if target_platform:
            query = query.filter(platform=target_platform)
        records = await query.all()
        return [
            {
                "stream_id": str(rec.stream_id),
                "platform": str(rec.platform or ""),
                "chat_type": _normalize_chat_type(rec.chat_type),
                "group_id": str(rec.group_id or ""),
                "group_name": str(rec.group_name or ""),
            }
            for rec in records
        ]

    async def _collect_local_matches(
        self,
        *,
        stream_candidates: list[dict[str, Any]],
        matcher: re.Pattern[str] | None,
        query: str,
        time_from_ts: float | None,
        time_to_ts: float | None,
        context_before: int,
        context_after: int,
    ) -> tuple[list[dict[str, Any]], dict[str, list[Message]]]:
        from src.core.managers.stream_manager import get_stream_manager

        cfg = self._history_cfg()
        per_stream_limit = int(getattr(cfg, "max_scan_rows_per_stream", 240) or 240)
        per_stream_limit = max(20, per_stream_limit)

        stream_manager = get_stream_manager()
        local_messages_by_stream: dict[str, list[Message]] = {}
        local_matches: list[dict[str, Any]] = []

        for stream_meta in stream_candidates:
            stream_id = str(stream_meta.get("stream_id") or "")
            if not stream_id:
                continue

            try:
                stream_messages = await stream_manager.get_stream_messages(
                    stream_id=stream_id,
                    limit=per_stream_limit,
                    offset=0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"读取 stream 历史失败: stream_id={stream_id}, error={exc}")
                continue

            local_messages_by_stream[stream_id] = stream_messages
            for idx, message in enumerate(stream_messages):
                ts = _message_time(message)
                if ts is None:
                    continue
                if time_from_ts is not None and ts < time_from_ts:
                    continue
                if time_to_ts is not None and ts > time_to_ts:
                    continue

                text = _message_text(message)
                if query:
                    if matcher is None:
                        continue
                    if not matcher.search(text):
                        continue

                lower = max(0, idx - max(0, context_before))
                upper = min(len(stream_messages), idx + 1 + max(0, context_after))
                before_items = [
                    self._brief_message_payload(stream_messages[i])
                    for i in range(lower, idx)
                ]
                after_items = [
                    self._brief_message_payload(stream_messages[i])
                    for i in range(idx + 1, upper)
                ]

                local_matches.append(
                    self._runtime_message_payload(
                        message=message,
                        stream_meta=stream_meta,
                        source="local_db",
                        context_before=before_items,
                        context_after=after_items,
                    )
                )

        return local_matches, local_messages_by_stream

    @staticmethod
    def _brief_message_payload(message: Message) -> dict[str, Any]:
        ts = _message_time(message)
        return {
            "message_id": str(getattr(message, "message_id", "") or ""),
            "time_ts": ts,
            "time": _to_iso(ts),
            "sender_id": str(getattr(message, "sender_id", "") or ""),
            "sender_name": str(getattr(message, "sender_name", "") or ""),
            "sender_role": str(getattr(message, "sender_role", "") or ""),
            "content": _message_text(message),
        }

    @staticmethod
    def _runtime_message_payload(
        *,
        message: Message,
        stream_meta: dict[str, Any],
        source: str,
        context_before: list[dict[str, Any]],
        context_after: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ts = _message_time(message)
        payload = message.to_dict() if hasattr(message, "to_dict") else {}
        return {
            "source": source,
            "platform": str(getattr(message, "platform", "") or stream_meta.get("platform") or ""),
            "chat_type": _normalize_chat_type(
                str(getattr(message, "chat_type", "") or stream_meta.get("chat_type") or "")
            ),
            "stream_id": str(getattr(message, "stream_id", "") or stream_meta.get("stream_id") or ""),
            "stream_name": str(stream_meta.get("group_name") or ""),
            "message_id": str(getattr(message, "message_id", "") or ""),
            "time_ts": ts,
            "time": _to_iso(ts),
            "sender_id": str(getattr(message, "sender_id", "") or ""),
            "sender_name": str(getattr(message, "sender_name", "") or ""),
            "sender_role": str(getattr(message, "sender_role", "") or ""),
            "content": _message_text(message),
            "content_full": str(getattr(message, "content", "") or ""),
            "payload": payload,
            "context_before": context_before,
            "context_after": context_after,
        }

    async def _collect_backfill_matches(
        self,
        *,
        stream_candidates: list[dict[str, Any]],
        local_messages_by_stream: dict[str, list[Message]],
        requested_limit: int,
        time_from_ts: float | None,
        time_to_ts: float | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cfg = self._history_cfg()
        adapter_signature = str(
            getattr(cfg, "adapter_signature", "napcat_adapter:adapter:napcat_adapter") or ""
        ).strip()
        timeout_seconds = float(getattr(cfg, "adapter_timeout_seconds", 8) or 8)
        group_actions = list(getattr(cfg, "group_history_actions", ["get_group_msg_history"]) or [])
        private_actions = list(
            getattr(cfg, "private_history_actions", ["get_friend_msg_history", "get_private_msg_history"]) or []
        )

        if not adapter_signature:
            return [], [{"status": "skipped", "reason": "adapter_signature 为空"}]

        backfill_items: list[dict[str, Any]] = []
        backfill_logs: list[dict[str, Any]] = []

        for stream_meta in stream_candidates:
            stream_id = str(stream_meta.get("stream_id") or "")
            chat_type = _normalize_chat_type(str(stream_meta.get("chat_type") or ""))
            stream_local_messages = local_messages_by_stream.get(stream_id, [])
            actions = group_actions if chat_type == "group" else private_actions
            if not actions:
                continue

            param_variants = self._build_backfill_param_variants(
                stream_meta=stream_meta,
                stream_messages=stream_local_messages,
                requested_limit=requested_limit,
            )
            if not param_variants:
                backfill_logs.append(
                    {
                        "stream_id": stream_id,
                        "chat_type": chat_type,
                        "status": "skipped",
                        "reason": "缺少回补参数（可能无法识别群号/用户ID）",
                    }
                )
                continue

            stream_success = False
            for action in actions:
                action_name = str(action or "").strip()
                if not action_name:
                    continue
                for params in param_variants:
                    try:
                        response = await send_adapter_command(
                            adapter_sign=adapter_signature,
                            command_name=action_name,
                            command_data=params,
                            timeout=timeout_seconds,
                        )
                    except Exception as exc:  # noqa: BLE001
                        backfill_logs.append(
                            {
                                "stream_id": stream_id,
                                "action": action_name,
                                "params": params,
                                "status": "error",
                                "error": str(exc),
                            }
                        )
                        continue

                    status = str(response.get("status") or "").lower()
                    data = response.get("data")
                    raw_messages = _extract_backfill_list(data)
                    backfill_logs.append(
                        {
                            "stream_id": stream_id,
                            "action": action_name,
                            "params": params,
                            "status": status or "unknown",
                            "raw_count": len(raw_messages),
                        }
                    )

                    if status != "ok" or not raw_messages:
                        continue

                    normalized = self._normalize_backfill_messages(
                        raw_messages=raw_messages,
                        stream_meta=stream_meta,
                        action_name=action_name,
                        time_from_ts=time_from_ts,
                        time_to_ts=time_to_ts,
                    )
                    if not normalized:
                        continue
                    backfill_items.extend(normalized)
                    stream_success = True
                    break

                if stream_success:
                    break

        return backfill_items, backfill_logs

    @staticmethod
    def _build_backfill_param_variants(
        *,
        stream_meta: dict[str, Any],
        stream_messages: list[Message],
        requested_limit: int,
    ) -> list[dict[str, Any]]:
        chat_type = _normalize_chat_type(str(stream_meta.get("chat_type") or ""))
        variants: list[dict[str, Any]] = []
        count = max(1, min(int(requested_limit), 100))
        latest_id = ""
        if stream_messages:
            latest_id = str(getattr(stream_messages[-1], "message_id", "") or "")

        if chat_type == "group":
            group_id = str(stream_meta.get("group_id") or "").strip()
            if not group_id:
                return []
            variants.append({"group_id": group_id, "count": count})
            if group_id.isdigit():
                variants.append({"group_id": int(group_id), "count": count})
            if latest_id.isdigit():
                seq = int(latest_id)
                variants.append({"group_id": group_id, "count": count, "message_seq": seq})
                variants.append({"group_id": group_id, "count": count, "msg_seq": seq})
            return variants

        # private / discuss fallback 统一按 user_id 尝试
        user_id = ""
        for message in reversed(stream_messages):
            sender_id = str(getattr(message, "sender_id", "") or "").strip()
            sender_role = str(getattr(message, "sender_role", "") or "").lower().strip()
            if sender_id and sender_role != "bot":
                user_id = sender_id
                break
        if not user_id:
            for message in reversed(stream_messages):
                sender_id = str(getattr(message, "sender_id", "") or "").strip()
                if sender_id:
                    user_id = sender_id
                    break
        if not user_id:
            return []

        variants.append({"user_id": user_id, "count": count})
        if user_id.isdigit():
            numeric_uid = int(user_id)
            variants.append({"user_id": numeric_uid, "count": count})
            variants.append({"friend_uid": numeric_uid, "count": count})
        if latest_id.isdigit():
            variants.append({"user_id": user_id, "count": count, "message_seq": int(latest_id)})
        return variants

    @staticmethod
    def _normalize_backfill_messages(
        *,
        raw_messages: list[dict[str, Any]],
        stream_meta: dict[str, Any],
        action_name: str,
        time_from_ts: float | None,
        time_to_ts: float | None,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for raw in raw_messages:
            if not isinstance(raw, dict):
                continue
            ts = _parse_time_bound(raw.get("time") or raw.get("timestamp"))
            if ts is not None:
                if time_from_ts is not None and ts < time_from_ts:
                    continue
                if time_to_ts is not None and ts > time_to_ts:
                    continue

            sender = raw.get("sender")
            sender_id = ""
            sender_name = ""
            if isinstance(sender, dict):
                sender_id = str(sender.get("user_id") or sender.get("id") or "")
                sender_name = str(sender.get("card") or sender.get("nickname") or "")
            else:
                sender_id = str(raw.get("user_id") or raw.get("sender_id") or "")
                sender_name = str(raw.get("sender_name") or "")

            content = _extract_backfill_text(raw)
            normalized.append(
                {
                    "source": "adapter_backfill",
                    "platform": str(stream_meta.get("platform") or "napcat"),
                    "chat_type": _normalize_chat_type(str(stream_meta.get("chat_type") or "")),
                    "stream_id": str(stream_meta.get("stream_id") or ""),
                    "stream_name": str(stream_meta.get("group_name") or ""),
                    "message_id": str(raw.get("message_id") or raw.get("id") or raw.get("msg_id") or ""),
                    "time_ts": ts,
                    "time": _to_iso(ts),
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "sender_role": str(raw.get("sender_role") or ""),
                    "content": content,
                    "content_full": str(raw.get("raw_message") or raw.get("message") or content),
                    "payload": raw,
                    "context_before": [],
                    "context_after": [],
                    "backfill_action": action_name,
                }
            )
        return normalized

    async def _collect_tool_events(
        self,
        *,
        time_from_ts: float | None,
        time_to_ts: float | None,
        cap: int,
    ) -> list[dict[str, Any]]:
        service = LifeEngineService.get_instance()
        if service is None:
            return []

        async with service._get_lock():
            events = list(getattr(service, "_event_history", []))
            events.extend(list(getattr(service, "_pending_events", [])))

        tool_events: list[dict[str, Any]] = []
        for event in events:
            event_type = str(getattr(event, "event_type", "") or "")
            if event_type not in {"tool_call", "tool_result"}:
                continue

            ts = _parse_time_bound(getattr(event, "timestamp", None))
            if ts is not None:
                if time_from_ts is not None and ts < time_from_ts:
                    continue
                if time_to_ts is not None and ts > time_to_ts:
                    continue

            tool_events.append(
                {
                    "source": "life_event",
                    "event_id": str(getattr(event, "event_id", "") or ""),
                    "event_type": event_type,
                    "time_ts": ts,
                    "time": str(getattr(event, "timestamp", "") or ""),
                    "sequence": int(getattr(event, "sequence", 0) or 0),
                    "tool_name": str(getattr(event, "tool_name", "") or ""),
                    "tool_args": getattr(event, "tool_args", None) or {},
                    "tool_success": getattr(event, "tool_success", None),
                    "content": str(getattr(event, "content", "") or ""),
                    "stream_id": str(getattr(event, "stream_id", "") or ""),
                }
            )

        tool_events.sort(key=lambda item: float(item.get("time_ts") or 0.0), reverse=True)
        return tool_events[: max(0, cap)]

    async def execute(
        self,
        query: Annotated[str, "关键词或正则表达式（留空表示仅按时间/流范围拉取历史）"] = "",
        use_regex: Annotated[bool, "是否按正则表达式匹配 query"] = False,
        case_insensitive: Annotated[bool, "匹配时是否忽略大小写"] = True,
        stream_ids: Annotated[list[str] | None, "限定检索的 stream_id 列表；为空时自动选择"] = None,
        cross_stream: Annotated[bool | None, "是否跨多个 stream 检索；None 使用配置默认值"] = None,
        platform: Annotated[str, "限定平台（如 qq/napcat）；为空表示不限制"] = "",
        time_from: Annotated[str, "起始时间（Unix 时间戳或 ISO）"] = "",
        time_to: Annotated[str, "结束时间（Unix 时间戳或 ISO）"] = "",
        limit: Annotated[int, "返回条数上限"] = 20,
        context_before: Annotated[int, "每条命中前置上下文条数（仅本地库）"] = 2,
        context_after: Annotated[int, "每条命中后置上下文条数（仅本地库）"] = 2,
        source_mode: Annotated[
            Literal["auto", "local_db", "napcat"],
            "来源模式：auto=本地优先不足回补，local_db=仅本地，napcat=仅适配器",
        ] = "auto",
        force_backfill: Annotated[bool, "在 auto 模式下是否强制尝试适配器回补"] = False,
        include_tool_calls: Annotated[bool, "是否附带 life 的 tool_call/tool_result 事件"] = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        cfg = self._history_cfg()
        if cfg is not None and not bool(getattr(cfg, "enabled", True)):
            return False, "history_retrieval 已在配置中禁用"

        resolved_limit = self._resolved_limit(limit)
        cross_stream_default = bool(getattr(cfg, "default_cross_stream", False)) if cfg is not None else False
        resolved_cross_stream = cross_stream_default if cross_stream is None else bool(cross_stream)
        explicit_stream_ids = [str(sid or "").strip() for sid in list(stream_ids or []) if str(sid or "").strip()]
        if len(explicit_stream_ids) > 1:
            resolved_cross_stream = True

        query_text = str(query or "")
        flags = re.IGNORECASE if case_insensitive else 0
        matcher: re.Pattern[str] | None
        if query_text:
            try:
                matcher = re.compile(query_text if use_regex else re.escape(query_text), flags)
            except re.error as exc:
                return False, f"正则表达式错误: {exc}"
        else:
            matcher = None

        time_from_ts = _parse_time_bound(time_from)
        time_to_ts = _parse_time_bound(time_to)
        if (
            time_from_ts is not None
            and time_to_ts is not None
            and time_from_ts > time_to_ts
        ):
            return False, "time_from 不能大于 time_to"

        candidates = await self._resolve_stream_candidates(
            stream_ids=explicit_stream_ids,
            cross_stream=resolved_cross_stream,
            platform=str(platform or "").strip(),
        )
        if not candidates:
            return False, "没有可检索的聊天流（请提供 stream_ids，或启用 cross_stream）"

        local_matches: list[dict[str, Any]] = []
        local_messages_by_stream: dict[str, list[Message]] = {}
        if source_mode in {"auto", "local_db"}:
            local_matches, local_messages_by_stream = await self._collect_local_matches(
                stream_candidates=candidates,
                matcher=matcher,
                query=query_text,
                time_from_ts=time_from_ts,
                time_to_ts=time_to_ts,
                context_before=max(0, int(context_before)),
                context_after=max(0, int(context_after)),
            )

        need_backfill = source_mode == "napcat"
        if source_mode == "auto":
            need_backfill = bool(force_backfill)

        backfill_matches: list[dict[str, Any]] = []
        backfill_logs: list[dict[str, Any]] = []
        if need_backfill:
            backfill_matches, backfill_logs = await self._collect_backfill_matches(
                stream_candidates=candidates,
                local_messages_by_stream=local_messages_by_stream,
                requested_limit=resolved_limit,
                time_from_ts=time_from_ts,
                time_to_ts=time_to_ts,
            )
            if query_text and matcher is not None:
                backfill_matches = [
                    item for item in backfill_matches if matcher.search(str(item.get("content") or ""))
                ]

        merged = list(local_matches) + list(backfill_matches)
        merged.sort(key=lambda item: float(item.get("time_ts") or 0.0), reverse=True)

        deduped: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for item in merged:
            key = _dedupe_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(item)
            if len(deduped) >= resolved_limit:
                break

        tool_events: list[dict[str, Any]] = []
        if include_tool_calls:
            tool_events = await self._collect_tool_events(
                time_from_ts=time_from_ts,
                time_to_ts=time_to_ts,
                cap=max(resolved_limit, 20),
            )

        result = {
            "action": "fetch_chat_history",
            "query": query_text,
            "use_regex": bool(use_regex),
            "source_mode": source_mode,
            "scope": "cross_stream" if resolved_cross_stream else "current_stream",
            "cross_stream": resolved_cross_stream,
            "stream_ids": [str(item.get("stream_id") or "") for item in candidates],
            "matches": deduped,
            "tool_events": tool_events,
            "stats": {
                "resolved_limit": resolved_limit,
                "candidate_streams": len(candidates),
                "local_matches": len(local_matches),
                "backfill_matches": len(backfill_matches),
                "returned_matches": len(deduped),
                "tool_events": len(tool_events),
                "backfill_attempted": bool(need_backfill),
                "backfill_logs": backfill_logs[-12:],
            },
        }
        return True, result


CHAT_HISTORY_TOOLS = [
    LifeEngineFetchChatHistoryTool,
]
