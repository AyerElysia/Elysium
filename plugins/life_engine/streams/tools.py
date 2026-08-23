"""Read-only compatibility projection for the retired ThoughtStream archive.

The class remains importable so old snapshots and deterministic replay tools can
explain historical data.  It is never registered in a live consciousness and
cannot map a legacy mutation into the canonical proactive authority.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from .archive import LegacyThoughtStreamArchive
from .legacy_snapshot import LegacySnapshotNotFoundError

logger = log_api.get_logger("life_engine.stream_tools")

StreamAction = Literal["create", "list", "advance", "retire", "reactivate"]

THOUGHT_STREAM_LIST_DEFAULT_PAGE_SIZE = 20
THOUGHT_STREAM_LIST_MAX_PAGE_SIZE = 20
THOUGHT_STREAM_LIST_DEFAULT_MAX_BYTES = 16 * 1024
THOUGHT_STREAM_LIST_MIN_BYTES = 2 * 1024
THOUGHT_STREAM_LIST_MAX_BYTES = 16 * 1024
_LIST_CURSOR_VERSION = 1


class ThoughtStreamProjectionError(ValueError):
    """旧 ThoughtStream 有界查询无法安全继续。"""


def _encode_list_cursor(
    *,
    offset: int,
    source_revision: int,
    include_dormant: bool,
) -> str:
    payload = json.dumps(
        {
            "include_dormant": include_dormant,
            "offset": offset,
            "source_revision": source_revision,
            "version": _LIST_CURSOR_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"{encoded}.{digest}"


def _decode_list_cursor(
    cursor: str,
    *,
    source_revision: int,
    include_dormant: bool,
) -> int:
    try:
        encoded, supplied_digest = cursor.split(".", 1)
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding)
        expected_digest = hashlib.sha256(payload).hexdigest()[:16]
        if not hmac.compare_digest(supplied_digest, expected_digest):
            raise ThoughtStreamProjectionError("cursor integrity check failed")
        decoded = json.loads(payload.decode("utf-8"))
    except ThoughtStreamProjectionError:
        raise
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ThoughtStreamProjectionError("cursor is malformed") from exc

    if not isinstance(decoded, dict):
        raise ThoughtStreamProjectionError("cursor payload must be an object")
    if decoded.get("version") != _LIST_CURSOR_VERSION:
        raise ThoughtStreamProjectionError("cursor version is unsupported")
    if decoded.get("include_dormant") is not include_dormant:
        raise ThoughtStreamProjectionError("cursor filter does not match this query")
    if decoded.get("source_revision") != source_revision:
        raise ThoughtStreamProjectionError(
            "cursor is stale because the ThoughtStream snapshot changed"
        )
    offset = decoded.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ThoughtStreamProjectionError("cursor offset is invalid")
    return offset


def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, int]:
    normalized = str(value or "").replace("\r", " ").replace("\n", " ")
    raw = normalized.encode("utf-8")
    if len(raw) <= max_bytes:
        return normalized, 0

    suffix = "…"
    suffix_bytes = suffix.encode("utf-8")
    clipped = raw[: max(0, max_bytes - len(suffix_bytes))]
    text = clipped.decode("utf-8", errors="ignore")
    delivered_source_bytes = len(text.encode("utf-8"))
    return text + suffix, len(raw) - delivered_source_bytes


def _render_stream_projection(stream) -> tuple[str, int]:
    stream_id, id_omitted = _truncate_utf8(stream.id, 96)
    title, title_omitted = _truncate_utf8(stream.title, 256)
    status_tag = f"[{stream.status}]" if stream.status != "active" else ""
    lines = [
        (
            f"- {stream_id}: {title} {status_tag}"
            f" (好奇心: {stream.curiosity_score:.0%}, 推进: {stream.advance_count}次)"
        )
    ]
    thought_omitted = 0
    if stream.last_thought:
        thought, thought_omitted = _truncate_utf8(stream.last_thought, 512)
        lines.append(f"  最近想法: {thought}")
    return "\n".join(lines), id_omitted + title_omitted + thought_omitted


def _projection_meta(
    *,
    source_revision: int,
    returned: int,
    remaining: int,
    payload_bytes: int,
    omitted_field_bytes: int,
    next_cursor: str,
) -> str:
    return (
        "[projection_meta "
        f"source_revision={source_revision} "
        f"returned={returned} "
        f"remaining={remaining} "
        f"payload_bytes={payload_bytes} "
        f"omitted_field_bytes={omitted_field_bytes} "
        f"has_more={'true' if next_cursor else 'false'} "
        f"next_cursor={next_cursor or '-'}]"
    )


def _validate_list_bounds(*, page_size: int, max_bytes: int) -> None:
    if not 1 <= page_size <= THOUGHT_STREAM_LIST_MAX_PAGE_SIZE:
        raise ThoughtStreamProjectionError(
            f"page_size must be between 1 and {THOUGHT_STREAM_LIST_MAX_PAGE_SIZE}"
        )
    if not THOUGHT_STREAM_LIST_MIN_BYTES <= max_bytes <= THOUGHT_STREAM_LIST_MAX_BYTES:
        raise ThoughtStreamProjectionError(
            "max_bytes must be between "
            f"{THOUGHT_STREAM_LIST_MIN_BYTES} and {THOUGHT_STREAM_LIST_MAX_BYTES}"
        )


def _render_bounded_list(
    manager: LegacyThoughtStreamArchive,
    *,
    include_dormant: bool,
    cursor: str,
    page_size: int,
    max_bytes: int,
) -> str:
    _validate_list_bounds(page_size=page_size, max_bytes=max_bytes)

    streams = manager.list_for_projection(include_dormant=include_dormant)
    source_revision = manager.current_revision
    offset = 0
    if cursor.strip():
        offset = _decode_list_cursor(
            cursor.strip(),
            source_revision=source_revision,
            include_dormant=include_dormant,
        )
    if offset > len(streams):
        raise ThoughtStreamProjectionError("cursor offset is beyond this snapshot")

    accepted: list[str] = []
    omitted_field_bytes = 0
    candidates = streams[offset : offset + page_size]
    for stream in candidates:
        rendered, item_omitted_bytes = _render_stream_projection(stream)
        trial_items = [*accepted, rendered]
        trial_body = "\n".join(trial_items)
        trial_offset = offset + len(trial_items)
        has_more = trial_offset < len(streams)
        next_cursor = (
            _encode_list_cursor(
                offset=trial_offset,
                source_revision=source_revision,
                include_dormant=include_dormant,
            )
            if has_more
            else ""
        )
        trial_omitted = omitted_field_bytes + item_omitted_bytes
        meta = _projection_meta(
            source_revision=source_revision,
            returned=len(trial_items),
            remaining=len(streams) - trial_offset,
            payload_bytes=len(trial_body.encode("utf-8")),
            omitted_field_bytes=trial_omitted,
            next_cursor=next_cursor,
        )
        trial_result = f"{trial_body}\n\n{meta}"
        if len(trial_result.encode("utf-8")) > max_bytes:
            break
        accepted = trial_items
        omitted_field_bytes = trial_omitted

    if streams[offset:] and not accepted:
        raise ThoughtStreamProjectionError(
            "max_bytes cannot fit one complete bounded projection item"
        )

    body = "\n".join(accepted)
    final_offset = offset + len(accepted)
    has_more = final_offset < len(streams)
    next_cursor = (
        _encode_list_cursor(
            offset=final_offset,
            source_revision=source_revision,
            include_dormant=include_dormant,
        )
        if has_more
        else ""
    )
    if not body:
        body = "当前没有符合筛选条件的思考流"
    meta = _projection_meta(
        source_revision=source_revision,
        returned=len(accepted),
        remaining=len(streams) - final_offset,
        payload_bytes=len(body.encode("utf-8")),
        omitted_field_bytes=omitted_field_bytes,
        next_cursor=next_cursor,
    )
    result = f"{body}\n\n{meta}"
    if len(result.encode("utf-8")) > max_bytes:
        raise ThoughtStreamProjectionError("projection metadata exceeded max_bytes")
    return result


def _get_service():
    from ..service.registry import get_life_engine_service

    return get_life_engine_service()


def _get_archive() -> LegacyThoughtStreamArchive | None:
    """Open the exact old snapshot without constructing its mutable manager."""

    service = _get_service()
    if service is None:
        return None
    config = service._cfg()
    workspace = Path(config.settings.workspace_path).resolve()
    try:
        return LegacyThoughtStreamArchive.open(
            workspace / "thoughts" / "streams.json"
        )
    except LegacySnapshotNotFoundError:
        return None


class LifeEngineManageThoughtStreamTool(BaseTool):
    """Deprecated schema restricted to bounded historical reads."""

    tool_name: str = "nucleus_manage_thought_stream"
    tool_description: str = (
        "旧 ThoughtStream 只读归档兼容类，生产环境不注册。新调用只能使用"
        " nucleus_proactive_query / nucleus_proactive_command。"
        "create/advance/retire/reactivate 全部明确失败，也不会被机械迁移成"
        " AttentionThread。list 仅在离线诊断显式注入旧快照 reader 时提供"
        "有界历史页，永不混入 completed。分页使用 cursor、"
        "page_size（最多 20）和 max_bytes（最多 16384）。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal"]

    def __init__(self, plugin) -> None:
        super().__init__(plugin)

    async def execute(
        self,
        action: Annotated[
            StreamAction,
            "旧操作：create / list / advance / retire / reactivate",
        ],
        # create 参数
        title: Annotated[str, "思考流标题（action=create 时必填）"] = "",
        reason: Annotated[str, "为什么这件事引起了你的兴趣（action=create 时可选）"] = "",
        absorb_curiosity: Annotated[bool, "此思考流是否承接当前好奇牵引的刺点（承接后牵引会放下）"] = False,
        # list 参数
        include_dormant: Annotated[bool, "是否包含休眠中的思考流（action=list 时有效）"] = False,
        cursor: Annotated[str, "上一页返回的 next_cursor（action=list 时有效）"] = "",
        page_size: Annotated[
            int, "单页最多返回条数，范围 1-20（action=list 时有效）"
        ] = THOUGHT_STREAM_LIST_DEFAULT_PAGE_SIZE,
        max_bytes: Annotated[
            int, "单页 UTF-8 硬字节预算，范围 2048-16384（action=list 时有效）"
        ] = THOUGHT_STREAM_LIST_DEFAULT_MAX_BYTES,
        # advance 参数
        stream_id: Annotated[str, "思考流ID（action=advance/retire 时必填）"] = "",
        expected_revision: Annotated[
            int,
            "canonical 线索的当前 revision；advance/retire/reactivate 必填",
        ] = 0,
        thought: Annotated[str, "对该话题的最新想法（action=advance 时必填）"] = "",
        curiosity_delta: Annotated[float, "好奇心变化量，正值=更感兴趣，负值=兴趣减退"] = 0.0,
        # retire 参数
        new_status: Annotated[str, "新状态: completed(已得出结论) 或 dormant(暂时搁置)"] = "completed",
        conclusion: Annotated[str, "最终结论或搁置原因（action=retire 时可选）"] = "",
    ) -> tuple[bool, str | dict[str, object]]:
        del (
            title,
            reason,
            absorb_curiosity,
            stream_id,
            expected_revision,
            thought,
            curiosity_delta,
            new_status,
            conclusion,
        )
        try:
            if action == "list":
                try:
                    manager = _get_archive()
                    if manager is None:
                        return False, "旧思考流只读快照未初始化"
                    return True, _render_bounded_list(
                        manager,
                        include_dormant=include_dormant,
                        cursor=cursor,
                        page_size=page_size,
                        max_bytes=max_bytes,
                    )
                except ThoughtStreamProjectionError as exc:
                    return False, f"思考流有界查询失败: {exc}"
            return False, {
                "error": "ThoughtStreamArchiveReadOnly",
                "action": action,
                "authority_committed": False,
                "replacement": "nucleus_proactive_command",
            }

        except Exception as exc:  # noqa: BLE001 - operations-only boundary
            logger.error(
                f"旧思考流只读归档不可用: error_type={type(exc).__name__}"
            )
            return False, {
                "error": type(exc).__name__,
                "operation": "legacy_thought_stream_archive_read",
                "mutated": False,
            }


# Historical class remains importable for deterministic replay diagnostics.
# No live consciousness receives this schema; canonical proactive writes go
# exclusively through ``nucleus_proactive_command``.
STREAM_TOOLS: list[type[BaseTool]] = []
