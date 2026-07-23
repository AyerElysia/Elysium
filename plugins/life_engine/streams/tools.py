"""ThoughtStream 工具集（合一版）。

为中枢提供持久兴趣线索管理能力，通过单一工具 + action 参数
替代原先的 4 个独立工具，减少 prompt 中工具描述占用。
"""

from __future__ import annotations

from typing import Annotated, Literal

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from .manager import ThoughtStreamManager

logger = log_api.get_logger("life_engine.stream_tools")

StreamAction = Literal["create", "list", "advance", "retire"]


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
        " 参数：include_dormant（是否包含休眠中的，默认 false）"
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
                if include_dormant:
                    streams = manager.list_all()
                else:
                    streams = manager.list_active()

                if not streams:
                    return True, "当前没有活跃的思考流"

                lines: list[str] = []
                for ts in streams:
                    status_tag = f"[{ts.status}]" if ts.status != "active" else ""
                    lines.append(
                        f"- {ts.id}: {ts.title} {status_tag}"
                        f" (好奇心: {ts.curiosity_score:.0%}, 推进: {ts.advance_count}次)"
                    )
                    if ts.last_thought:
                        lines.append(f"  最近想法: {ts.last_thought[:150]}")

                return True, "\n".join(lines)

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
