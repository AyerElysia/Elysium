"""Bounded, instance-scoped conversation evidence retrieval.

The authoritative message rows stay in the message store.  This module only
builds a transport projection for one model turn; it never treats retrieved
text as the subject's memory or as persona supervision.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal

from sqlalchemy import func, select

from src.app.plugin_system.base import BaseTool
from src.core.models.sql_alchemy import ChatStreams, Messages
from src.kernel.db import get_db_session

from ..service import LifeEngineService

_SCHEMA = "elysium.conversation_evidence.v1"
_ALGORITHM = "keyset-utf8-v1"
_CURSOR_DOMAIN = b"elysium.conversation_evidence.cursor.v1\0"
_DEFAULT_SCAN_ROWS = 240
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MIN_RESULT_BYTES = 2 * 1024
_DEFAULT_RESULT_BYTES = 8 * 1024
_TARGET_KEY_RE = re.compile(r"^[pg]-[0-9a-fA-F]{6,64}$")
_TASK_DEFAULT_BYTES = {
    "core": 8 * 1024,
    "chat": 16 * 1024,
    "voice_live": 8 * 1024,
    "livestream": 8 * 1024,
    "minecraft": 8 * 1024,
}


class ConversationEvidenceError(ValueError):
    """A content-free, caller-actionable retrieval failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _EvidenceRow:
    position: int
    message_id: str
    stream_id: str
    person_id: str
    occurred_at: float
    message_type: str
    text: str
    reply_to: str
    platform: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_size(value: Any) -> int:
    return len(_canonical_json(value).encode("utf-8"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode_cursor(payload: dict[str, Any]) -> str:
    body = _canonical_json(payload).encode("utf-8")
    checksum = hashlib.sha256(_CURSOR_DOMAIN + body).digest()[:16]
    return base64.urlsafe_b64encode(body + checksum).decode("ascii").rstrip("=")


def _decode_cursor(token: str) -> dict[str, Any]:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        body, checksum = raw[:-16], raw[-16:]
        expected = hashlib.sha256(_CURSOR_DOMAIN + body).digest()[:16]
        if len(body) == 0 or not hmac.compare_digest(checksum, expected):
            raise ValueError
        value = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversationEvidenceError(
            "cursor_invalid", "cursor is invalid or has been modified"
        ) from exc
    if not isinstance(value, dict) or int(value.get("v", 0) or 0) != 1:
        raise ConversationEvidenceError(
            "cursor_invalid", "cursor version is not supported"
        )
    return value


def _utf8_prefix(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, True
    if max_bytes <= 0:
        return "", False
    chunk = encoded[:max_bytes]
    while chunk:
        try:
            return chunk.decode("utf-8"), False
        except UnicodeDecodeError:
            chunk = chunk[:-1]
    return "", False


def _utf8_chunk(text: str, start: int, max_bytes: int) -> tuple[str, int, bool]:
    encoded = text.encode("utf-8")
    if start < 0 or start > len(encoded):
        raise ConversationEvidenceError(
            "cursor_invalid", "read cursor is outside the message"
        )
    end = min(len(encoded), start + max(0, max_bytes))
    while end > start:
        try:
            content = encoded[start:end].decode("utf-8")
            return content, end, end == len(encoded)
        except UnicodeDecodeError:
            end -= 1
    if start == len(encoded):
        return "", start, True
    raise ConversationEvidenceError(
        "byte_boundary_invalid", "read cursor is not on a UTF-8 boundary"
    )


def _message_ref(row: _EvidenceRow) -> str:
    seed = (
        f"{row.position}\0{row.stream_id}\0{row.message_id}\0{_sha256_text(row.text)}"
    )
    return f"msg:{row.position}:{_sha256_text(seed)[:16]}"


def _parse_message_ref(value: str) -> int:
    parts = str(value or "").split(":")
    if (
        len(parts) != 3
        or parts[0] != "msg"
        or not parts[1].isdigit()
        or len(parts[2]) != 16
    ):
        raise ConversationEvidenceError("message_ref_invalid", "message_ref is invalid")
    return int(parts[1])


def _iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat()


class LifeEngineConversationEvidenceTool(BaseTool):
    """Read a bounded projection of conversation evidence."""

    tool_name = "conversation_evidence"
    tool_description = (
        "读取当前意识实例可访问的对话证据。page 返回最近消息；search 在有界扫描页中检索并合并邻近消息；"
        "read 按 message_ref 分块读取一条大消息。结果有 UTF-8 字节硬上限和稳定游标。"
        "它不包含工具调用记录，也不会触发 NapCat 回补；工具历史请查 Trace，平台历史同步使用独立能力。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_chatter", "life_engine_internal"]

    def _cfg(self) -> Any:
        config = getattr(self.plugin, "config", None)
        return getattr(config, "history_retrieval", None)

    def _runtime_task(self, streams: tuple[str, ...]) -> str:
        explicit = str(getattr(self, "_runtime_task_name", "") or "").strip()
        if explicit:
            return explicit
        service = LifeEngineService.get_instance()
        registry = getattr(service, "consciousness_registry", None) if service else None
        if registry is not None and streams:
            owner = registry.get_for_stream(streams[0])
            kind = str(getattr(owner, "kind", "") or "").strip()
            if kind:
                return kind
        return "core"

    def _result_budget(
        self, streams: tuple[str, ...], requested: int
    ) -> tuple[str, int]:
        task = self._runtime_task(streams)
        cfg = self._cfg()
        field = {
            "core": "core_max_result_bytes",
            "chat": "chat_max_result_bytes",
            "voice_live": "voice_max_result_bytes",
            "livestream": "livestream_max_result_bytes",
            "minecraft": "minecraft_max_result_bytes",
        }.get(task, "core_max_result_bytes")
        configured = int(
            getattr(cfg, field, _TASK_DEFAULT_BYTES.get(task, _DEFAULT_RESULT_BYTES))
            or 0
        )
        configured = max(_MIN_RESULT_BYTES, configured)
        desired = configured if int(requested or 0) <= 0 else int(requested)
        return task, max(_MIN_RESULT_BYTES, min(desired, configured))

    def _log_resolution_failure(self, ref: str, exc: Exception) -> None:
        logger = getattr(self.plugin, "logger", None)
        if logger is None:
            import logging

            logger = logging.getLogger("life_engine.tools")
        logger.debug(
            "conversation_evidence target_key resolution failed; "
            "falling back to prefix scan: ref=%s error=%s",
            ref,
            type(exc).__name__,
        )

    async def _resolve_target_key(self, ref: str) -> str | None:
        """把「你可以触达的人和地方」里的 target_key（如 p-20403fdb）解析回完整 stream_id。

        非 target_key 格式（完整 stream_id 等）原样返回；解析失败返回 None。
        兼容模型脑补出的 UUID 形式（如 20403fdb-6f1a-441c-9596-4d7e4f85e3c8）：
        去掉连字符后按 hex 前缀做模糊匹配，避免 stream_not_found。
        """
        if not ref:
            return ref
        if not _TARGET_KEY_RE.match(ref):
            # 可能是模型脑补的 UUID 形式流 ID：去连字符后按前缀匹配真实流。
            compact = re.sub(r"[-]", "", ref)
            if re.fullmatch(r"[0-9a-fA-F]{6,64}", compact):
                resolved = await self._resolve_stream_prefix(compact)
                if resolved is not None:
                    return resolved
            return ref
        try:
            from ..core.send_targets import resolve_send_target_key

            target = await resolve_send_target_key(ref)
            if target is not None and getattr(target, "stream_id", ""):
                return str(target.stream_id)
        except Exception as exc:  # noqa: BLE001 - resolution is best-effort
            self._log_resolution_failure(ref, exc)
        # 兜底：按 stream_id 前缀匹配 chat_streams，优先 platform 非空的真实流。
        prefix = ref.split("-", 1)[1]
        resolved = await self._resolve_stream_prefix(prefix)
        if resolved is not None:
            return resolved
        return None

    async def _resolve_stream_prefix(self, prefix: str) -> str | None:
        """按 stream_id hex 前缀匹配 ChatStreams；仅一个唯一真实流时返回完整 stream_id。"""
        try:
            async with get_db_session() as session:
                result = await session.execute(
                    select(
                        ChatStreams.stream_id,
                        ChatStreams.platform,
                    ).where(ChatStreams.stream_id.like(f"{prefix}%"))
                )
                matches = [
                    (str(stream_id), str(platform or ""))
                    for stream_id, platform in result.all()
                ]
            real = [
                stream_id
                for stream_id, platform in matches
                if platform
                and not stream_id.startswith(f"{prefix}-")
                and stream_id != prefix
            ]
            if len(real) == 1:
                return real[0]
            if not real and len(matches) == 1:
                return matches[0][0]
            return None
        except Exception as exc:  # noqa: BLE001 - best-effort fallback
            self._log_resolution_failure(prefix, exc)
            return None

    async def _resolve_streams(
        self,
        requested: list[str] | None,
        *,
        operation: str = "page",
    ) -> tuple[str, ...]:
        raw = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in (requested or [])
                if str(item or "").strip()
            )
        )
        # 「你可以触达的人和地方」里展示的是 target_key（如 p-20403fdb），
        # 不是完整 stream_id；这里把 target_key 解析回完整 stream_id，
        # 让爱莉在心跳里直接复用提示里的 key 就能读取对话证据。
        explicit: tuple[str, ...] = ()
        for item in raw:
            full = await self._resolve_target_key(item)
            explicit = explicit + ((full or item),)
        explicit = tuple(dict.fromkeys(explicit))
        current = self.get_current_stream_id()
        if not explicit:
            if operation == "search":
                # search 是全库有界检索：心跳态没有当前流时不应报错，
                # 降级为扫描全部真实流（排除 chat_global 占位符）。
                async with get_db_session() as session:
                    result = await session.execute(
                        select(ChatStreams.stream_id).where(
                            ChatStreams.platform.is_not(None),
                            ChatStreams.platform != "",
                            ChatStreams.stream_id != "chat_global",
                        )
                    )
                    all_streams = tuple(
                        str(item)
                        for item in result.scalars().all()
                        if str(item) != "chat_global"
                    )
                if not all_streams:
                    raise ConversationEvidenceError(
                        "stream_required",
                        "no real conversation streams are available for search",
                    )
                return all_streams
            if not current or current == "chat_global":
                raise ConversationEvidenceError(
                    "stream_required",
                    "an internal call without a current conversation must provide "
                    "stream_ids (or a target_key from the reachable list) explicitly",
                )
            explicit = (current,)
        max_streams = int(getattr(self._cfg(), "max_candidate_streams", 12) or 12)
        if len(explicit) > max_streams:
            raise ConversationEvidenceError(
                "too_many_streams",
                "requested stream count exceeds the configured retrieval bound",
            )

        async with get_db_session() as session:
            result = await session.execute(
                select(ChatStreams.stream_id).where(ChatStreams.stream_id.in_(explicit))
            )
            existing = {str(item) for item in result.scalars().all()}
        if existing != set(explicit):
            raise ConversationEvidenceError(
                "stream_not_found", "one or more requested streams do not exist"
            )

        service = LifeEngineService.get_instance()
        registry = getattr(service, "consciousness_registry", None) if service else None
        if registry is not None:
            if current:
                caller = registry.get_for_stream(current)
                caller_id = str(getattr(caller, "instance_id", "") or "")
                if not caller_id:
                    raise ConversationEvidenceError(
                        "instance_unverified",
                        "current consciousness instance is unavailable",
                    )
                for stream_id in explicit:
                    owner = registry.get_for_stream(stream_id)
                    if str(getattr(owner, "instance_id", "") or "") != caller_id:
                        raise ConversationEvidenceError(
                            "cross_instance_denied",
                            "requested conversation belongs to another active consciousness instance",
                        )
            else:
                owner_ids = {
                    str(
                        getattr(registry.get_for_stream(stream_id), "instance_id", "")
                        or ""
                    )
                    for stream_id in explicit
                }
                if "" in owner_ids or len(owner_ids) != 1:
                    raise ConversationEvidenceError(
                        "cross_instance_denied",
                        "an internal retrieval page cannot combine multiple consciousness instances",
                    )
        return explicit

    async def _frontier(self, streams: tuple[str, ...]) -> int:
        async with get_db_session() as session:
            result = await session.execute(
                select(func.max(Messages.id)).where(Messages.stream_id.in_(streams))
            )
            return int(result.scalar_one_or_none() or 0)

    async def _load_rows(
        self,
        *,
        streams: tuple[str, ...],
        frontier: int,
        before: int | None,
        limit: int,
        time_from: float | None,
        time_to: float | None,
        position: int | None = None,
    ) -> list[_EvidenceRow]:
        statement = select(
            Messages.id,
            Messages.message_id,
            Messages.stream_id,
            Messages.person_id,
            Messages.time,
            Messages.message_type,
            Messages.processed_plain_text,
            Messages.content,
            Messages.reply_to,
            Messages.platform,
        ).where(Messages.stream_id.in_(streams))
        if position is not None:
            statement = statement.where(Messages.id == position)
        else:
            statement = statement.where(Messages.id <= frontier)
            if before is not None:
                statement = statement.where(Messages.id < before)
            if time_from is not None:
                statement = statement.where(Messages.time >= time_from)
            if time_to is not None:
                statement = statement.where(Messages.time <= time_to)
            statement = statement.order_by(Messages.id.desc()).limit(limit)

        async with get_db_session() as session:
            result = await session.execute(statement)
            rows = result.all()
        output: list[_EvidenceRow] = []
        for raw in rows:
            item = raw._mapping
            processed = item["processed_plain_text"]
            content = item["content"]
            text = processed if isinstance(processed, str) else str(content or "")
            output.append(
                _EvidenceRow(
                    position=int(item["id"]),
                    message_id=str(item["message_id"] or ""),
                    stream_id=str(item["stream_id"] or ""),
                    person_id=str(item["person_id"] or ""),
                    occurred_at=float(item["time"] or 0.0),
                    message_type=str(item["message_type"] or "unknown"),
                    text=text,
                    reply_to=str(item["reply_to"] or ""),
                    platform=str(item["platform"] or ""),
                )
            )
        return output

    @staticmethod
    def _cursor_contract(
        *,
        operation: str,
        streams: tuple[str, ...],
        query: str,
        use_regex: bool,
        frontier: int,
        before: int | None,
    ) -> dict[str, Any]:
        return {
            "v": 1,
            "op": operation,
            "streams": list(streams),
            "query_sha256": _sha256_text(query),
            "regex": bool(use_regex),
            "frontier": int(frontier),
            "before": before,
        }

    @staticmethod
    def _validate_cursor_contract(
        cursor: dict[str, Any],
        *,
        operation: str,
        streams: tuple[str, ...],
        query: str,
        use_regex: bool,
    ) -> tuple[int, int | None]:
        if (
            cursor.get("op") != operation
            or tuple(cursor.get("streams") or ()) != streams
            or cursor.get("query_sha256") != _sha256_text(query)
            or bool(cursor.get("regex")) != bool(use_regex)
        ):
            raise ConversationEvidenceError(
                "cursor_mismatch", "cursor does not match this retrieval request"
            )
        frontier = cursor.get("frontier")
        before = cursor.get("before")
        if (
            not isinstance(frontier, int)
            or frontier < 0
            or (before is not None and (not isinstance(before, int) or before < 1))
        ):
            raise ConversationEvidenceError(
                "cursor_invalid", "cursor positions are invalid"
            )
        return frontier, before

    @staticmethod
    def _row_item(row: _EvidenceRow, text_budget: int) -> dict[str, Any]:
        content_bytes = len(row.text.encode("utf-8"))
        excerpt, exact = _utf8_prefix(row.text, text_budget)
        item: dict[str, Any] = {
            "message_ref": _message_ref(row),
            "position": row.position,
            "stream_id": row.stream_id,
            "message_id": row.message_id,
            "occurred_at": _iso_time(row.occurred_at),
            "actor_ref": row.person_id or None,
            "message_type": row.message_type,
            "reply_to": row.reply_to or None,
            "platform": row.platform or None,
            "content_sha256": _sha256_text(row.text),
            "content_bytes": content_bytes,
            "exact": exact,
        }
        if excerpt:
            item["text"] = excerpt
        return item

    def _project(
        self,
        *,
        operation: str,
        task: str,
        streams: tuple[str, ...],
        frontier: int,
        rows: list[_EvidenceRow],
        next_cursor: str | None,
        source_has_more: bool,
        scanned_count: int,
        max_bytes: int,
        prebuilt_items: list[dict[str, Any]] | None = None,
    ) -> str:
        original_bytes = sum(len(row.text.encode("utf-8")) for row in rows)
        if prebuilt_items:
            original_bytes += sum(
                int(item.get("content_bytes", 0) or 0) for item in prebuilt_items
            )
        payload: dict[str, Any] = {
            "schema": _SCHEMA,
            "algorithm_version": _ALGORITHM,
            "operation": operation,
            "task": task,
            "authority": "message_store",
            "training_semantics": "external_evidence_not_persona_supervision",
            "scope": {"stream_ids": list(streams), "source_frontier": frontier},
            "items": [],
            "continuation": next_cursor,
            "has_more": bool(source_has_more),
            "stats": {
                "scanned_count": scanned_count,
                "candidate_count": len(rows) + len(prebuilt_items or ()),
                "omitted_count": 0,
                "original_content_bytes": original_bytes,
                "delivered_content_bytes": 0,
                "max_result_bytes": max_bytes,
            },
        }

        if prebuilt_items:
            payload["items"].extend(prebuilt_items)

        omitted = 0
        included_rows: list[_EvidenceRow] = []
        for row in rows:
            descriptor = self._row_item(row, 0)
            if _json_size(payload) + _json_size(descriptor) + 700 > max_bytes:
                omitted += 1
                continue
            payload["items"].append(descriptor)
            included_rows.append(row)

        if included_rows:
            available_text_bytes = max(0, max_bytes - _json_size(payload) - 700)
            per_item_text_bytes = min(2048, available_text_bytes // len(included_rows))
            offset = len(prebuilt_items or ())
            for index, row in enumerate(included_rows, start=offset):
                payload["items"][index] = self._row_item(row, per_item_text_bytes)

        payload["stats"]["omitted_count"] = omitted
        payload["stats"]["delivered_content_bytes"] = sum(
            len(str(item.get("text") or "").encode("utf-8"))
            for item in payload["items"]
        )
        if omitted:
            payload["has_more"] = True

        def _finalize_receipt() -> None:
            projection_basis = {
                key: value
                for key, value in payload.items()
                if key not in {"delivery_id", "projection_sha256", "delivered_bytes"}
            }
            projection_sha256 = _sha256_text(_canonical_json(projection_basis))
            payload["projection_sha256"] = projection_sha256
            payload["delivery_id"] = f"conversation:{projection_sha256[:24]}"
            payload["delivered_bytes"] = 0
            for _ in range(4):
                exact_size = _json_size(payload)
                if payload["delivered_bytes"] == exact_size:
                    break
                payload["delivered_bytes"] = exact_size

        _finalize_receipt()

        while _json_size(payload) > max_bytes and payload["items"]:
            payload["items"].pop()
            payload["stats"]["omitted_count"] += 1
            payload["has_more"] = True
            payload["stats"]["delivered_content_bytes"] = sum(
                len(str(item.get("text") or "").encode("utf-8"))
                for item in payload["items"]
            )
            _finalize_receipt()
        _finalize_receipt()
        rendered = _canonical_json(payload)
        if len(rendered.encode("utf-8")) > max_bytes:
            raise ConversationEvidenceError(
                "budget_too_small",
                "result metadata cannot fit the configured byte budget",
            )
        return rendered

    async def _read(
        self,
        *,
        streams: tuple[str, ...],
        message_ref: str,
        cursor_token: str,
        requested_bytes: int,
    ) -> str:
        position = _parse_message_ref(message_ref)
        rows = await self._load_rows(
            streams=streams,
            frontier=position,
            before=None,
            limit=1,
            time_from=None,
            time_to=None,
            position=position,
        )
        if len(rows) != 1 or _message_ref(rows[0]) != message_ref:
            raise ConversationEvidenceError(
                "message_ref_not_found", "message_ref is not available in this scope"
            )
        row = rows[0]
        task, max_bytes = self._result_budget(streams, requested_bytes)
        offset = 0
        if cursor_token:
            cursor = _decode_cursor(cursor_token)
            if cursor.get("op") != "read" or cursor.get("message_ref") != message_ref:
                raise ConversationEvidenceError(
                    "cursor_mismatch", "read cursor does not match message_ref"
                )
            offset = int(cursor.get("offset", -1))
        chunk_budget = max(1, max_bytes - 1400)
        chunk, next_offset, exact = _utf8_chunk(row.text, offset, chunk_budget)
        item = self._row_item(row, 0)
        item.update(
            {
                "text": chunk,
                "chunk_from_byte": offset,
                "chunk_through_byte": next_offset,
                "exact": exact,
            }
        )
        continuation = (
            None
            if exact
            else _encode_cursor(
                {
                    "v": 1,
                    "op": "read",
                    "message_ref": message_ref,
                    "offset": next_offset,
                }
            )
        )
        return self._project(
            operation="read",
            task=task,
            streams=streams,
            frontier=position,
            rows=[],
            next_cursor=continuation,
            source_has_more=not exact,
            scanned_count=1,
            max_bytes=max_bytes,
            prebuilt_items=[item],
        )

    async def execute(
        self,
        operation: Annotated[
            Literal["page", "search", "read"],
            "page=最近证据，search=有界检索，read=按引用分块读取",
        ] = "page",
        query: Annotated[str, "search 的关键词或正则；page/read 留空"] = "",
        use_regex: Annotated[bool, "search 是否使用正则表达式"] = False,
        stream_ids: Annotated[
            list[str] | None,
            "显式 stream_id；普通对话留空即当前流，内部调用必须显式提供",
        ] = None,
        cursor: Annotated[str, "上一次结果返回的 continuation"] = "",
        message_ref: Annotated[str, "read 操作要读取的稳定消息引用"] = "",
        limit: Annotated[int, "page 的消息数或 search 的命中数上限"] = _DEFAULT_LIMIT,
        context_radius: Annotated[int, "search 每个命中合并的相邻消息数，最多 3"] = 1,
        time_from: Annotated[float | None, "可选 Unix 起始时间"] = None,
        time_to: Annotated[float | None, "可选 Unix 结束时间"] = None,
        max_bytes: Annotated[int, "期望结果字节数；最终仍受任务硬上限约束"] = 0,
    ) -> tuple[bool, str]:
        cfg = self._cfg()
        if cfg is not None and not bool(getattr(cfg, "enabled", True)):
            return False, _canonical_json(
                {
                    "error": {
                        "code": "disabled",
                        "message": "conversation evidence is disabled",
                    }
                }
            )
        try:
            streams = await self._resolve_streams(stream_ids, operation=operation)
            if operation == "read":
                if not message_ref:
                    raise ConversationEvidenceError(
                        "message_ref_required", "read requires message_ref"
                    )
                return True, await self._read(
                    streams=streams,
                    message_ref=message_ref,
                    cursor_token=str(cursor or ""),
                    requested_bytes=max_bytes,
                )

            query_text = str(query or "")
            if operation == "search" and not query_text:
                raise ConversationEvidenceError(
                    "query_required", "search requires a non-empty query"
                )
            matcher: re.Pattern[str] | None = None
            if operation == "search":
                try:
                    matcher = re.compile(
                        query_text if use_regex else re.escape(query_text),
                        re.IGNORECASE,
                    )
                except re.error as exc:
                    raise ConversationEvidenceError(
                        "regex_invalid", "search regular expression is invalid"
                    ) from exc

            resolved_limit = max(
                1,
                min(
                    int(limit),
                    int(getattr(cfg, "tool_max_limit", _MAX_LIMIT) or _MAX_LIMIT),
                    _MAX_LIMIT,
                ),
            )
            task, budget = self._result_budget(streams, max_bytes)
            # Bound the number of descriptors before projection.  This keeps
            # the continuation at the first undelivered source position; a
            # byte-budget overflow must never skip rows silently.
            item_cap = max(1, min(32, (budget - 1600) // 650))
            scan_limit = (
                min(resolved_limit, item_cap)
                if operation == "page"
                else int(
                    getattr(cfg, "max_scan_rows_per_stream", _DEFAULT_SCAN_ROWS)
                    or _DEFAULT_SCAN_ROWS
                )
            )
            scan_limit = max(resolved_limit, min(scan_limit, 2000))
            if operation == "page":
                scan_limit = min(scan_limit, item_cap)
            if cursor:
                decoded = _decode_cursor(cursor)
                frontier, before = self._validate_cursor_contract(
                    decoded,
                    operation=operation,
                    streams=streams,
                    query=query_text,
                    use_regex=use_regex,
                )
            else:
                frontier, before = await self._frontier(streams), None

            loaded = await self._load_rows(
                streams=streams,
                frontier=frontier,
                before=before,
                limit=scan_limit + 1,
                time_from=time_from,
                time_to=time_to,
            )
            source_has_more = len(loaded) > scan_limit
            scanned = loaded[:scan_limit]
            if operation == "page":
                selected = scanned[:resolved_limit]
                consumed_count = len(selected)
            else:
                matched_indexes = [
                    index
                    for index, row in enumerate(scanned)
                    if matcher and matcher.search(row.text)
                ]
                radius = max(0, min(int(context_radius), 3))
                selected_indexes: set[int] = set()
                delivered_matches = 0
                for index in matched_indexes:
                    proposed = selected_indexes | set(
                        range(
                            max(0, index - radius),
                            min(len(scanned), index + radius + 1),
                        )
                    )
                    if len(proposed) > item_cap and selected_indexes:
                        break
                    selected_indexes = set(sorted(proposed)[:item_cap])
                    delivered_matches += 1
                    if (
                        delivered_matches >= resolved_limit
                        or len(selected_indexes) >= item_cap
                    ):
                        break
                selected = [scanned[index] for index in sorted(selected_indexes)]
                all_matches_delivered = delivered_matches == len(matched_indexes)
                if all_matches_delivered:
                    consumed_count = len(scanned)
                elif selected_indexes:
                    consumed_count = max(selected_indexes) + 1
                else:
                    consumed_count = len(scanned)

            next_cursor = None
            needs_continuation = source_has_more or consumed_count < len(scanned)
            if needs_continuation and consumed_count > 0:
                next_cursor = _encode_cursor(
                    self._cursor_contract(
                        operation=operation,
                        streams=streams,
                        query=query_text,
                        use_regex=use_regex,
                        frontier=frontier,
                        before=min(row.position for row in scanned[:consumed_count]),
                    )
                )
            return True, self._project(
                operation=operation,
                task=task,
                streams=streams,
                frontier=frontier,
                rows=selected,
                next_cursor=next_cursor,
                source_has_more=needs_continuation,
                scanned_count=consumed_count,
                max_bytes=budget,
            )
        except ConversationEvidenceError as exc:
            return False, _canonical_json(
                {"error": {"code": exc.code, "message": str(exc)}}
            )
        except Exception:  # noqa: BLE001 - public tool errors are normalized content-free
            return False, _canonical_json(
                {
                    "error": {
                        "code": "evidence_store_unavailable",
                        "message": "conversation evidence store is unavailable",
                    }
                }
            )


CONVERSATION_EVIDENCE_TOOLS = [LifeEngineConversationEvidenceTool]
