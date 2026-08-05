"""ThoughtStream 工具集（合一版）。

为中枢提供持久兴趣线索管理能力，通过单一工具 + action 参数
替代原先的 4 个独立工具，减少 prompt 中工具描述占用。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Annotated, Literal

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from .manager import ThoughtStreamManager

logger = log_api.get_logger("life_engine.stream_tools")

StreamAction = Literal["create", "list", "advance", "retire"]

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


def _render_bounded_list(
    manager: ThoughtStreamManager,
    *,
    include_dormant: bool,
    cursor: str,
    page_size: int,
    max_bytes: int,
) -> str:
    if not 1 <= page_size <= THOUGHT_STREAM_LIST_MAX_PAGE_SIZE:
        raise ThoughtStreamProjectionError(
            f"page_size must be between 1 and {THOUGHT_STREAM_LIST_MAX_PAGE_SIZE}"
        )
    if not THOUGHT_STREAM_LIST_MIN_BYTES <= max_bytes <= THOUGHT_STREAM_LIST_MAX_BYTES:
        raise ThoughtStreamProjectionError(
            "max_bytes must be between "
            f"{THOUGHT_STREAM_LIST_MIN_BYTES} and {THOUGHT_STREAM_LIST_MAX_BYTES}"
        )

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


def _get_manager() -> ThoughtStreamManager | None:
    """获取 ThoughtStreamManager 实例。"""
    service = _get_service()
    if service is None or service._thought_manager is None:
        return None
    return service._thought_manager



def _record_river_moment(*, kind: str, summary: str, operation: str, reason: str = "") -> None:
    """转折点入长河；长河故障绝不影响思考流操作。"""
    try:
        service = _get_service()
        recorder = getattr(service, "_record_life_moment", None) if service else None
        if recorder is not None:
            recorder(kind=kind, summary=summary, operation=operation, reason=reason)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"长河留痕失败: {e}")


async def _absorb_curiosity_signal(stream_title: str = "") -> None:
    """承接当前好奇牵引：刺点已被思考流接住，异步好奇层放下它。"""
    try:
        service = _get_service()
        if service is None:
            return
        engine = service._get_curiosity_engine()
        signal = await engine.load_signal()
        if signal.active:
            await engine.clear()
            _record_river_moment(
                kind="curiosity",
                summary=f"好奇刺点被思考流「{stream_title}」承接：{signal.anchor[:120]}",
                operation="absorbed",
            )
            logger.info(f"好奇牵引已被思考流承接并放下: {signal.anchor}")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"承接好奇牵引失败: {e}")


class LifeEngineManageThoughtStreamTool(BaseTool):
    """思考流管理工具（创建/列出/推进/结束 合一）。"""

    tool_name: str = "nucleus_manage_thought_stream"
    tool_description: str = (
        "管理持久思考流——你持续在意的兴趣或问题。"
        "这不是待办事项，而是'我最近一直在琢磨这件事'。"
        "\n\n"
        "**action=create** — 创建新的思考流。遇到有趣的话题、未解答的疑问、或反复出现的想法时使用。"
        " 参数：title（必填）、reason（为什么感兴趣，可选）、"
        "absorb_curiosity（若此思考流承接的是当前好奇牵引的刺点，设为 true，承接后牵引会放下）"
        "\n\n"
        "**action=list** — 列出当前活跃的思考流，用于选择接下来想深入哪条线索。"
        " 参数：include_dormant（是否包含休眠中的，默认 false；不会包含 completed）、"
        "cursor（上一页游标）、page_size（最多 20）、max_bytes（最多 16384）。"
        "返回值始终带有 projection_meta；has_more=true 时使用 next_cursor 继续读取。"
        "\n\n"
        "**action=advance** — 推进一条思考流，记录你对该话题的最新想法。"
        " 这是内心独白的核心：围绕你在意的事情深入思考。"
        " 参数：stream_id（必填）、thought（最新想法，必填）、curiosity_delta（好奇心变化量，可选）"
        "\n\n"
        "**action=retire** — 结束或休眠一条思考流。有了结论或暂时不再感兴趣时使用。"
        " 参数：stream_id（必填）、new_status（completed/dormant）、conclusion（结论或搁置原因，可选）"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    def __init__(self, plugin) -> None:
        super().__init__(plugin)

    async def execute(
        self,
        action: Annotated[StreamAction, "操作：create / list / advance / retire"],
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
        thought: Annotated[str, "对该话题的最新想法（action=advance 时必填）"] = "",
        curiosity_delta: Annotated[float, "好奇心变化量，正值=更感兴趣，负值=兴趣减退"] = 0.0,
        # retire 参数
        new_status: Annotated[str, "新状态: completed(已得出结论) 或 dormant(暂时搁置)"] = "completed",
        conclusion: Annotated[str, "最终结论或搁置原因（action=retire 时可选）"] = "",
    ) -> tuple[bool, str]:
        manager = _get_manager()
        if manager is None:
            return False, "思考流服务未初始化"

        try:
            if action == "create":
                if not title or not title.strip():
                    return False, "title 不能为空"
                ts = manager.create(title=title.strip(), reason=reason.strip())
                if absorb_curiosity:
                    await _absorb_curiosity_signal(stream_title=ts.title)
                return True, (
                    f"已创建思考流「{ts.title}」({ts.id})，"
                    f"当前活跃思考流: {len(manager.list_active())}"
                    + ("；好奇牵引已承接放下" if absorb_curiosity else "")
                )

            if action == "list":
                try:
                    return True, _render_bounded_list(
                        manager,
                        include_dormant=include_dormant,
                        cursor=cursor,
                        page_size=page_size,
                        max_bytes=max_bytes,
                    )
                except ThoughtStreamProjectionError as exc:
                    return False, f"思考流有界查询失败: {exc}"

            if action == "advance":
                if not stream_id or not stream_id.strip():
                    return False, "stream_id 不能为空"
                if not thought or not thought.strip():
                    return False, "thought 不能为空"
                success, msg = manager.advance(
                    stream_id=stream_id.strip(),
                    thought=thought.strip(),
                    curiosity_delta=curiosity_delta,
                )
                if success:
                    # 探索本身有回报
                    pass
                return success, msg

            if action == "retire":
                if not stream_id or not stream_id.strip():
                    return False, "stream_id 不能为空"
                if new_status not in ("completed", "dormant"):
                    return False, "new_status 必须是 'completed' 或 'dormant'"
                target = next(
                    (ts for ts in manager.list_all() if ts.id == stream_id.strip()), None
                )
                stream_title = target.title if target else stream_id.strip()
                success, msg = manager.retire(
                    stream_id=stream_id.strip(),
                    new_status=new_status,
                    conclusion=conclusion.strip() if conclusion else "",
                )
                if success and new_status == "completed":
                    pass
                if success:
                    verb = "闭合" if new_status == "completed" else "搁置"
                    detail = conclusion.strip() if conclusion and conclusion.strip() else "（无结论）"
                    _record_river_moment(
                        kind="thought_stream",
                        summary=f"{verb}思考流「{stream_title}」：{detail[:120]}",
                        operation=new_status,
                    )
                return success, msg

            return False, f"未知 action: {action}，请使用 create/list/advance/retire"

        except Exception as e:
            logger.error(f"思考流操作失败: {e}", exc_info=True)
            return False, f"思考流操作失败: {e}"


# 工具注册列表
STREAM_TOOLS = [
    LifeEngineManageThoughtStreamTool,
]
