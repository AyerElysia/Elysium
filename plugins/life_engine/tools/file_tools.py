"""life_engine 中枢文件系统工具集。

为生命中枢提供限定在 workspace 内的文件系统操作能力。
所有操作都限制在配置的 workspace_path 目录下，确保安全。

设计理念（参考 Claude Code）：
- 每个工具的描述都是一段使用指南，包含「何时用」和「何时不用」
- 工具返回值精练，避免冗余字段淹没上下文
- 先读后改，操作前确认
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from uuid import uuid4

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool
from src.core.models.message import Message, MessageType

from ..constants import EXTERNAL_MESSAGE_ACTIVE_WINDOW_MINUTES
from ..core.config import LifeEngineConfig
from ..memory.eligibility import (
    DEFAULT_MAX_DOCUMENT_BYTES,
    assess_document_path,
    assess_indexed_document_path,
    read_workspace_document,
)
from ..memory.prompting import build_memory_write_warning
from ..trace.store import LifeTraceStore
from ._utils import (
    _get_workspace,
    _resolve_path,
    _pick_latest_target_stream_id,
)

if TYPE_CHECKING:
    from ..agents.coordinator import AgentCoordinator

logger = log_api.get_logger("life_engine.tools")

_MEMORY_READ_MAX_BYTES = DEFAULT_MAX_DOCUMENT_BYTES

# 自动演化链推断：检索源文件时取多少候选
_LINEAGE_SOURCE_TOP_K = 3
# 候选分数至少要达到最高分的多少比例才算"足够突出"
_LINEAGE_SOURCE_SCORE_RATIO = 0.85
# 用于检索源文件的查询文本最长多少字符
_LINEAGE_QUERY_MAX_CHARS = 300
# 用于检索源文件的查询最多取多少行有信息量的内容
_LINEAGE_QUERY_MAX_LINES = 4


def _get_workspace_read_only(plugin: Any) -> Path:
    """Return the configured workspace without creating it for a read query."""
    config = getattr(plugin, "config", None)
    if isinstance(config, LifeEngineConfig):
        workspace = config.settings.workspace_path
    else:
        workspace = str(Path(__file__).parent.parent.parent.parent / "data" / "life_engine_workspace")
    return Path(workspace).resolve()


def _format_size(size: int) -> str:
    """格式化文件大小。"""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _format_time(timestamp: float) -> str:
    """格式化时间戳。"""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat()




def _get_life_engine_service(plugin: Any):
    """获取 life_engine 服务实例。"""
    from ..service import LifeEngineService

    return LifeEngineService.get_instance()


async def _auto_create_lineage_edge(
    plugin: Any,
    *,
    path: str,
    before_content: str | None,
    after_content: str | None,
    reason: str,
    operation: str,
) -> None:
    """完美架构：自动检测写入/编辑意图并建立演化链。

    这是文件操作与记忆演化的深度集成点。
    根据操作类型、reason 关键词、内容相似度等，自动判断演化关系。
    """
    try:
        from ..memory.edges import EdgeType

        service = _get_life_engine_service(plugin)
        if service is None:
            return

        memory_service = getattr(service, "_memory_service", None)
        if memory_service is None:
            return

        # 检测演化意图
        edge_type, auto_reason = _detect_evolution_intent(
            reason=reason,
            operation=operation,
            before_content=before_content,
            after_content=after_content,
        )

        if edge_type is None:
            return

        logger.info(
            f"🔗 检测到演化意图: {path} [{edge_type.value}] - {auto_reason}"
        )

        # 完善实现：从 reason 中提取源文件路径
        source_path = _extract_source_path_from_reason(reason, path)
        explicit_source = source_path is not None

        if source_path is None:
            # 没有明确源文件时，用"本次新增的内容"去检索它延续/精炼的是谁。
            # 注意：write 与 edit 都要走这条路——新建文件时 before_content 为空，
            # 恰恰是"整理旧笔记为新笔记"最常见的形态。
            query_material = _extract_new_material(before_content, after_content)
            if query_material:
                source_path = await _find_similar_source_file(
                    memory_service,
                    target_path=path,
                    target_content=query_material,
                    edge_type=edge_type,
                )

        if source_path and source_path != path:
            # 建立真正的跨文件演化链
            try:
                await memory_service.create_memory_lineage_edge(
                    source_path=source_path,
                    target_path=path,
                    relation_type=edge_type,
                    reason=auto_reason,
                    # 推断出来的源文件不如她自己写明的可靠，强度给低一档
                    strength=0.8 if explicit_source else 0.55,
                )
                logger.info(
                    f"✅ 自动建立演化链: {source_path} → {path} [{edge_type.value}]"
                )
            except Exception as exc:
                logger.warning(f"建立演化链失败: {exc}")
        else:
            # 没有找到源文件，只记录演化意图
            logger.debug(
                f"演化意图已检测但未找到源文件: {path} - {edge_type.value}"
            )

    except Exception as exc:
        logger.debug(f"自动建立演化链失败（非关键路径）: {exc}")


def _extract_source_path_from_reason(reason: str, target_path: str) -> str | None:
    """从 reason 中提取源文件路径。

    例如：
    - "整理 drafts/initial.md 为正式笔记" → "drafts/initial.md"
    - "基于 notes/old.md 的修正版本" → "notes/old.md"
    """
    if not reason:
        return None

    import re

    # 匹配常见的文件路径模式
    patterns = [
        r'(?:整理|基于|根据|从|修正|延续|重命名)\s+([a-zA-Z0-9_/.-]+\.(?:md|txt))',
        r'([a-zA-Z0-9_/.-]+\.(?:md|txt))\s+(?:的|为|到)',
        r'`([a-zA-Z0-9_/.-]+\.(?:md|txt))`',
    ]

    for pattern in patterns:
        match = re.search(pattern, reason)
        if match:
            source = match.group(1)
            # 确保不是目标文件本身
            if source != target_path and source.strip():
                return source.strip()

    return None


def _extract_new_material(before_content: str | None, after_content: str | None) -> str:
    """取出这次操作真正"新增"的内容，作为检索源文件的查询material。

    为什么不用整份文件：编辑一份已存在的笔记时，用全文去检索必然把自己捞回来，
    真正表达"我在延续什么"的只有新增的那几行。新建文件时没有 before，
    全文本身就是新增内容。
    """
    after = (after_content or "").strip()
    if not after:
        return ""

    before = (before_content or "").strip()
    if not before:
        return after

    old_lines = {line.strip() for line in before.splitlines() if line.strip()}
    added = [
        line.strip()
        for line in after.splitlines()
        if line.strip() and line.strip() not in old_lines
    ]
    if added:
        return "\n".join(added)
    # 内容没有新增行（例如只调整了顺序/措辞），退回全文
    return after


def _build_lineage_query(material: str) -> str:
    """把内容压成一条检索查询：取有信息量的前几行，并限长。"""
    lines = [
        line.strip().lstrip("#-*> ").strip()
        for line in (material or "").splitlines()
    ]
    meaningful = [line for line in lines if len(line) >= 8]
    if not meaningful:
        # 全是短行时退让一步，避免直接放弃
        meaningful = [line for line in lines if line]
    if not meaningful:
        return ""
    return " ".join(meaningful[:_LINEAGE_QUERY_MAX_LINES])[:_LINEAGE_QUERY_MAX_CHARS]


async def _find_similar_source_file(
    memory_service: Any,
    target_path: str,
    target_content: str,
    edge_type: Any,
) -> str | None:
    """基于内容相似度查找可能的源文件。

    用于当 reason 中没有明确源文件时的智能推断。
    """
    try:
        # 简化实现：只在特定演化类型下查找
        if edge_type.value not in ["refines", "corrects", "continues"]:
            return None

        query = _build_lineage_query(target_content)
        if not query:
            return None

        # 检索相似文件（降级模式，不返回 bundle）
        results = await memory_service.search_memory(
            query=query,
            top_k=_LINEAGE_SOURCE_TOP_K,
            enable_association=False,
            return_bundles=False,  # 使用简单模式避免递归
        )

        if not results:
            return None

        # relevance 是 RRF 融合分（量级约 1/(60+rank)，最大 ~0.033），
        # 不能用绝对阈值判断。这里用"相对最高分"来判断候选是否足够突出。
        top_score = max(float(getattr(r, "relevance", 0.0) or 0.0) for r in results)
        if top_score <= 0.0:
            return None

        for result in results:
            candidate = str(getattr(result, "file_path", "") or "").strip()
            if not candidate or candidate == target_path:
                continue
            # 只接受直接命中，联想扩散来的文件不足以作为演化源
            if str(getattr(result, "source", "") or "") != "direct":
                continue
            score = float(getattr(result, "relevance", 0.0) or 0.0)
            if score < top_score * _LINEAGE_SOURCE_SCORE_RATIO:
                continue
            return candidate

        return None

    except Exception as exc:
        logger.debug(f"查找相似源文件失败: {exc}")
        return None


def _detect_evolution_intent(
    *,
    reason: str,
    operation: str,
    before_content: str | None,
    after_content: str | None,
) -> tuple[Any | None, str]:
    """检测文件演化意图。

    Returns:
        (EdgeType, reason) 或 (None, "") 如果无法检测
    """
    from ..memory.edges import EdgeType

    reason_lower = reason.lower() if reason else ""

    # 检测关键词
    if any(kw in reason_lower for kw in ["整理", "精炼", "refine", "organize", "总结"]):
        return EdgeType.REFINES, "整理并精炼之前的内容"

    if any(kw in reason_lower for kw in ["修正", "纠正", "correct", "fix", "错误"]):
        return EdgeType.CORRECTS, "修正之前的理解错误"

    if any(kw in reason_lower for kw in ["重命名", "rename", "move", "迁移"]):
        return EdgeType.RENAMES, "重命名或迁移文件"

    if any(kw in reason_lower for kw in ["延续", "继续", "continue", "补充"]):
        return EdgeType.CONTINUES, "延续之前的思考"

    if any(kw in reason_lower for kw in ["重新理解", "重新解释", "reinterpret"]):
        return EdgeType.REINTERPRETS, "从新的角度重新理解"

    # 如果没有明确关键词，但是编辑操作且内容相似度高，判断为延续
    if operation == "edit" and before_content and after_content:
        # 简化版相似度：检查是否大部分内容保留
        if len(after_content) > len(before_content) * 0.7:
            common_ratio = _simple_content_overlap(before_content, after_content)
            if common_ratio > 0.7:
                return EdgeType.CONTINUES, "延续并扩展之前的内容"

    return None, ""


def _simple_content_overlap(text1: str, text2: str) -> float:
    """简单的内容重叠度计算。

    Returns:
        0.0-1.0 的重叠比例
    """
    if not text1 or not text2:
        return 0.0

    # 简化：按行分割，计算公共行比例
    lines1 = set(line.strip() for line in text1.split("\n") if line.strip())
    lines2 = set(line.strip() for line in text2.split("\n") if line.strip())

    if not lines1:
        return 0.0

    common_lines = lines1.intersection(lines2)
    return len(common_lines) / len(lines1)


async def _get_file_lineage_info(
    memory_service: Any,
    file_path: str,
) -> dict[str, Any] | None:
    """完美架构：获取文件的完整演化信息（演化链 + 修正记录）。

    Returns:
        {
            "evolution_trace": [...],  # 演化轨迹
            "corrections": [...],       # 修正记录
            "has_history": bool         # 是否有演化历史
        }
    """
    try:
        # 获取文件节点
        node = await memory_service.get_node_by_file_path(file_path)
        if node is None:
            return None

        from ..memory.lineage import get_lineage_edges, list_memory_corrections

        # 获取演化边
        outgoing_edges, incoming_edges = await get_lineage_edges(
            memory_service._db,
            node.node_id,
            min_weight=0.0,
        )

        evolution_trace = []

        # 处理出边（后续演化）
        for edge in outgoing_edges:
            target = await memory_service.get_node_by_id(edge.target_id)
            if target and target.file_path:
                evolution_trace.append({
                    "direction": "later",
                    "relation": edge.edge_type.value,
                    "file_path": target.file_path,
                    "title": target.title,
                    "reason": edge.reason,
                    "weight": round(edge.weight, 2),
                })

        # 处理入边（早期演化）
        for edge in incoming_edges:
            source = await memory_service.get_node_by_id(edge.source_id)
            if source and source.file_path:
                evolution_trace.append({
                    "direction": "earlier",
                    "relation": edge.edge_type.value,
                    "file_path": source.file_path,
                    "title": source.title,
                    "reason": edge.reason,
                    "weight": round(edge.weight, 2),
                })

        # 获取修正记录
        corrections = await list_memory_corrections(
            memory_service._db,
            query="",
            related_node_ids=[node.node_id],
            limit=10,
        )

        corrections_data = [
            {
                "topic": corr.topic,
                "message": corr.message,
                "source": corr.source,
                "created_at": corr.created_at,
            }
            for corr in corrections
        ]

        if not evolution_trace and not corrections_data:
            return None

        return {
            "evolution_trace": evolution_trace,
            "corrections": corrections_data,
            "has_history": len(evolution_trace) > 0 or len(corrections_data) > 0,
        }

    except Exception as exc:
        logger.debug(f"获取文件演化信息失败: {exc}")
        return None


async def _sync_memory_embedding_for_file(plugin: Any, path: str, content: str) -> None:
    """将已落盘的 canonical 记忆文档写入 SQLite/FTS/outbox。"""
    eligibility = assess_document_path(path)
    if not eligibility.eligible:
        logger.debug(
            f"跳过非记忆文档索引: {eligibility.path or path} ({eligibility.reason})"
        )
        return

    workspace = _get_workspace(plugin)
    valid, source_target = _resolve_path(plugin, path)
    canonical_valid, canonical_target = _resolve_path(plugin, eligibility.path)
    if not valid or not canonical_valid or source_target != canonical_target:
        logger.debug(
            "跳过与 canonical 文档不一致的文件索引: "
            f"{path} -> {eligibility.path}"
        )
        return

    try:
        document_content, source_mtime, _ = read_workspace_document(
            workspace,
            eligibility.path,
            max_bytes=_MEMORY_READ_MAX_BYTES,
        )
    except (OSError, ValueError) as exc:
        logger.debug(f"跳过不可安全读取的记忆文档 {path}: {exc}")
        return

    try:
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        memory_service = getattr(service, "_memory_service", None) if service else None
        if memory_service is None:
            return
        await memory_service.upsert_document(
            eligibility.path,
            document_content,
            title=Path(eligibility.path).stem,
            source_mtime=source_mtime,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"同步记忆文档索引失败 {eligibility.path}: {exc}")


def _read_trace_before_content(target: Path, encoding: str) -> str | None:
    if not target.exists() or not target.is_file():
        return None
    try:
        return target.read_text(encoding=encoding)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"追溯系统读取修改前内容失败 {target}: {exc}")
        return None


def _record_file_trace(
    plugin: Any,
    *,
    path: str,
    before_content: str | None,
    after_content: str | None,
    operation: str,
    tool_name: str,
    reason: str = "",
    source_event_id: str = "",
    stream_id: str = "",
) -> str:
    try:
        record = LifeTraceStore(_get_workspace(plugin)).record_change(
            path=path,
            before_content=before_content,
            after_content=after_content,
            operation=operation,
            tool_name=tool_name,
            actor="life_engine",
            reason=reason,
            source_event_id=source_event_id,
            stream_id=stream_id,
        )
        return record.trace_id if record is not None else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"记录 Life Trace 失败 {path}: {exc}")
        return ""


def _tool_trace_context(tool: Any) -> dict[str, str]:
    """从工具运行态取当时的语境，让文件改写在长河里关联到事件与聊天流。"""
    message = getattr(tool, "trigger_message", None)
    return {
        "source_event_id": str(getattr(message, "message_id", "") or ""),
        "stream_id": str(tool.get_current_stream_id() or ""),
    }


async def _resolve_tell_dfc_target(
    plugin: Any,
    stream_manager: Any,
    *,
    target_type: str,
    stream_id: str,
    platform: str,
    target_user_id: str,
    target_user_name: str,
    target_group_id: str,
    target_group_name: str,
) -> tuple[bool, Any | str, dict[str, str], str]:
    normalized_type = str(target_type or "auto").strip().lower() or "auto"
    target_stream_id = str(stream_id or "").strip()
    platform_name = str(platform or "").strip()
    user_id = str(target_user_id or "").strip()
    user_name = str(target_user_name or "").strip()
    group_id = str(target_group_id or "").strip()
    group_name = str(target_group_name or "").strip()

    if normalized_type not in {"auto", "current", "stream", "private", "group"}:
        return False, "target_type 仅支持 auto/current/stream/private/group", {}, normalized_type

    if target_stream_id:
        chat_stream = await stream_manager.get_or_create_stream(stream_id=target_stream_id)
        if chat_stream is None:
            return False, f"找不到目标聊天流: {target_stream_id}", {}, normalized_type
        return True, chat_stream, {}, "stream"

    if normalized_type == "stream":
        return False, "target_type=stream 时必须提供 stream_id", {}, normalized_type
    if normalized_type == "private" and group_id:
        return False, "target_type=private 时不能同时提供 target_group_id", {}, normalized_type
    if normalized_type == "group" and user_id:
        return False, "target_type=group 时不能同时提供 target_user_id", {}, normalized_type

    if normalized_type == "current":
        user_id = ""
        group_id = ""
    elif normalized_type == "auto":
        if user_id and group_id:
            return False, "target_user_id 和 target_group_id 不能同时提供", {}, normalized_type
        if user_id:
            normalized_type = "private"
        elif group_id:
            normalized_type = "group"

    fallback_stream_id = _pick_latest_target_stream_id(plugin) or ""

    if normalized_type in {"private", "group"}:
        if not platform_name and fallback_stream_id:
            try:
                fallback_info = await stream_manager.get_stream_info(fallback_stream_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"推断 tell_dfc 目标平台失败: {exc}")
                fallback_info = None
            if fallback_info:
                platform_name = str(fallback_info.get("platform") or "").strip()

        if not platform_name:
            return False, "指定私聊/群聊目标时需要提供 platform，或先有可用于推断平台的当前聊天。", {}, normalized_type

        if normalized_type == "private":
            if not user_id:
                return False, "target_type=private 时必须提供 target_user_id", {}, normalized_type
            chat_stream = await stream_manager.get_or_create_stream(
                platform=platform_name,
                user_id=user_id,
                chat_type="private",
            )
            extra: dict[str, str] = {"target_user_id": user_id}
            if user_name:
                extra["target_user_name"] = user_name
            return True, chat_stream, extra, normalized_type

        if not group_id:
            return False, "target_type=group 时必须提供 target_group_id", {}, normalized_type
        chat_stream = await stream_manager.get_or_create_stream(
            platform=platform_name,
            group_id=group_id,
            group_name=group_name,
            chat_type="group",
        )
        extra = {"target_group_id": group_id}
        if group_name:
            extra["target_group_name"] = group_name
        return True, chat_stream, extra, normalized_type

    if not fallback_stream_id:
        return (
            False,
            "没有可用的目标聊天流。可能暂时没有外部对话活动。稍后有新消息时，表达层会自然处理，你无需担心。",
            {},
            normalized_type,
        )

    chat_stream = await stream_manager.get_or_create_stream(stream_id=fallback_stream_id)
    if chat_stream is None:
        return False, f"找不到目标聊天流: {fallback_stream_id}", {}, normalized_type
    return True, chat_stream, {}, "current"


def _discard_tell_dfc_unread(context: Any, message: Message) -> None:
    """撤回本次失败唤醒写入的消息，不影响并发到达的其他未读消息。"""
    unread_messages = getattr(context, "unread_messages", None)
    if not isinstance(unread_messages, list):
        return
    for index in range(len(unread_messages) - 1, -1, -1):
        if unread_messages[index] is message:
            del unread_messages[index]
            return


class LifeEngineWakeDFCTool(BaseTool):
    """向同一主体的表达层补充信息差，并默认唤醒表达层。"""

    tool_name: str = "nucleus_tell_dfc"
    tool_description: str = (
        "向同一主体的表达层补充信息差，并唤醒表达层处理这段新上下文。"
        "一方面补一段它当前暂时看不到、但对接下来对话可能重要的背景；"
        "另一方面，如果你想念对方或想主动向外界联系，也可以用它把这个动机交给表达层自行判断。"
        "\n\n"
        "**这不是在遥控社交态，更不是替它写台词；它不用于指导表达层怎么回复。**"
        " 这不是在和另一个意识体对话，而是同一个你在不同运行状态之间补充上下文。"
        "\n\n"
        "**何时使用：**\n"
        "- 你掌握了表达层当前可能不知道的事实、背景、记忆线索、情绪来源或潜在风险\n"
        "- 你发现某段近期事件能解释对方现在为什么这样说/这样做\n"
        "- 你需要补一个隐藏前提，避免表达层误解上下文\n"
        "- **【主动社交】你想念对方了，或者你想主动向外界发起联系，需要把这个动机交给表达层。**\n"
        "\n"
        "**何时不用：**\n"
        "- 你想教表达层该怎么回、该怎么问、该怎么安慰\n"
        "- 你在替表达层写台词、步骤或策略\n"
        "- 你自己其实想直接开口 → 交给表达层在正常对话里处理\n"
        "\n"
        "**注意：** 工具会默认唤醒表达层，让补充内容尽快进入对话判断；"
        "这不用于催表达层开口，是否表达仍由它结合对话自行判断。"
        " 写法尽量是观察/背景/风险/线索。如果是主动社交，直接写明想念的缘由或主动开启对话的意图。"
        " 允许写“我刚看到 X，这可能解释 Y，风险是 Z”或“我想念他了，需要主动问候”；不要写“你应该回复 X”、"
        "“你去安慰/追问 Y”或“按以下步骤说”。"
        "\n\n"
        "**参数写法建议：**\n"
        "- `message`: 只写信息差本身或想念的理由/想主动联系的话题\n"
        "- `reason`: 为什么这是表达层当前可能不知道的背景。如果是主动社交，写明想念对方或主动建立联系\n"
        "- `importance`: 常规用 normal；只有紧急时用 high/critical\n"
        "- `proactive_wake`: 默认 true，会唤醒表达层；传 false 时只入队，保留旧调用的队列模式。\n"
        "- `target_type`: 默认 auto。不确定就留空，系统会回退到刚收到消息的聊天；要指定目标可用 private/group/stream/current\n"
        "- `platform`: 指定私聊/群聊目标时的平台，如 qq；留空时尽量从当前聊天推断\n"
        "- `target_user_id`: 想去某个私聊时填写对方平台用户 ID，并设置 target_type=private\n"
        "- `target_group_id`: 想去某个群聊时填写群 ID，并设置 target_type=group\n"
        "- `stream_id`: 精确目标聊天流 ID；不确定就留空，让系统自动路由\n"
        "\n"
        "**记住：补背景/唤醒社交，不下指导。**"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        message: Annotated[str, "要补充给表达层的信息差，或想主动联系的话题/想念理由（不要写指导台词）"],
        reason: Annotated[
            str,
            "为什么这是表达层当前可能不知道、但值得补充的信息差，或是主动社交/想念对方的缘由",
        ] = "",
        importance: Annotated[str, "重要度（可选：low/normal/high/critical，默认 normal）"] = "normal",
        proactive_wake: Annotated[
            bool,
            "是否唤醒表达层立即看见新上下文。默认 true；传 false 仅写入待处理队列，兼容旧调用。",
        ] = True,
        stream_id: Annotated[str, "目标聊天流ID（可选，不填则自动选择最近活跃的外部对话流）"] = "",
        target_type: Annotated[
            str,
            "目标类型（auto/current/stream/private/group）。auto 默认回退到最近收到消息的聊天；private/group 可指定私聊或群聊。",
        ] = "auto",
        platform: Annotated[str, "目标平台（如 qq）。指定私聊/群聊但无法从当前聊天推断时必填。"] = "",
        target_user_id: Annotated[str, "目标私聊用户的平台用户 ID（target_type=private 时使用）"] = "",
        target_user_name: Annotated[str, "目标私聊用户昵称（可选，用于显示和下游发送信息）"] = "",
        target_group_id: Annotated[str, "目标群聊 ID（target_type=group 时使用）"] = "",
        target_group_name: Annotated[str, "目标群聊名称（可选，用于显示和下游发送信息）"] = "",
    ) -> tuple[bool, str | dict]:
        logger.debug(
            f"[nucleus_tell_dfc] Life 调用表达层同步/社交工具:\n"
            f"  message: {message}\n"
            f"  reason: {reason}\n"
            f"  importance: {importance}\n"
            f"  proactive_wake: {proactive_wake}\n"
            f"  stream_id: {stream_id}\n"
            f"  target_type: {target_type}\n"
            f"  platform: {platform}\n"
            f"  target_user_id: {target_user_id}\n"
            f"  target_user_name: {target_user_name}\n"
            f"  target_group_id: {target_group_id}\n"
            f"  target_group_name: {target_group_name}"
        )

        text = str(message or "").strip()
        if not text:
            return False, "message 不能为空"

        normalized_importance = str(importance or "normal").strip().lower() or "normal"
        if normalized_importance not in {"low", "normal", "high", "critical"}:
            return False, "importance 仅支持 low/normal/high/critical"

        # 获取服务实例以辅助路由判断
        life_service = _get_life_engine_service(self.plugin)
        if life_service:
            minutes_since_external = life_service._minutes_since_external_message()

            # 活跃检查：如果对话流很活跃，建议不要打扰
            if (
                minutes_since_external is not None
                and minutes_since_external < EXTERNAL_MESSAGE_ACTIVE_WINDOW_MINUTES
            ):
                # 除非是 high 或 critical 级别，否则给出警告但不阻止
                if normalized_importance not in ("high", "critical"):
                    logger.debug(
                        f"当前对话流正在活跃（{minutes_since_external} 分钟前有消息），"
                        f"同步可能会打扰表达层的正常对话节奏，但仍然允许执行。"
                    )

        try:
            from src.core.managers.stream_manager import get_stream_manager
            from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager
        except Exception as e:  # noqa: BLE001
            return False, f"加载核心管理器失败: {e}"

        stream_manager = get_stream_manager()
        ok, resolved, explicit_target_extra, resolved_target_type = await _resolve_tell_dfc_target(
            self.plugin,
            stream_manager,
            target_type=target_type,
            stream_id=stream_id,
            platform=platform,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            target_group_id=target_group_id,
            target_group_name=target_group_name,
        )
        if not ok:
            return False, str(resolved)
        chat_stream = resolved

        target_extra: dict[str, Any] = dict(explicit_target_extra)
        try:
            stream_info = await stream_manager.get_stream_info(chat_stream.stream_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"life_engine 无法读取 tell_dfc 目标流信息: {exc}")
            stream_info = None

        if str(chat_stream.chat_type or "").lower() == "group":
            group_id = str(target_extra.get("target_group_id") or "").strip()
            group_name = str(target_extra.get("target_group_name") or "").strip()
            if stream_info:
                group_id = group_id or str(stream_info.get("group_id") or "").strip()
                group_name = group_name or str(stream_info.get("group_name") or "").strip()
            if group_id:
                target_extra["target_group_id"] = group_id
            if group_name:
                target_extra["target_group_name"] = group_name
        else:
            if not target_extra.get("target_user_id"):
                person_id = str(stream_info.get("person_id") or "").strip() if stream_info else ""
                if person_id:
                    try:
                        from src.core.utils.user_query_helper import get_user_query_helper

                        person = await get_user_query_helper().person_crud.get_by(
                            person_id=person_id
                        )
                        if person and person.user_id:
                            target_extra["target_user_id"] = str(person.user_id)
                        nickname = str(getattr(person, "nickname", "") or "").strip() if person else ""
                        if nickname:
                            target_extra["target_user_name"] = nickname
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"life_engine 无法为表达层唤醒解析私聊目标: {exc}")
            if target_extra.get("target_user_id") and not target_extra.get("target_user_name"):
                person_id = str(stream_info.get("person_id") or "").strip() if stream_info else ""
                if person_id:
                    try:
                        from src.core.utils.user_query_helper import get_user_query_helper

                        person = await get_user_query_helper().person_crud.get_by(
                            person_id=person_id
                        )
                        nickname = str(getattr(person, "nickname", "") or "").strip() if person else ""
                        if nickname:
                            target_extra["target_user_name"] = nickname
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"life_engine 无法为表达层唤醒解析私聊目标名称: {exc}")

        wake_prompt = (
            "[信息差补充]\n"
            f"重要度: {normalized_importance}\n"
            f"缘由: {reason or '潜意识波动'}\n"
            f"补充背景/线索: {text}\n"
            "（这是同一主体的内在层补充的一段上下文：可能有帮助，也可能只作为背景。"
            "它不是命令，不是指定措辞，更不是必须照做的脚本。请结合当前对话上下文，自行判断是否吸收、如何吸收。）"
        )

        trigger_message = Message(
            message_id=f"life_nucleus_wake_{uuid4().hex[:12]}",
            platform=chat_stream.platform or "unknown",
            chat_type=chat_stream.chat_type or "private",
            stream_id=chat_stream.stream_id,
            sender_id="life_engine_nucleus",
            sender_name="生命中枢",
            sender_role="other",
            message_type=MessageType.TEXT,
            content=wake_prompt,
            processed_plain_text=wake_prompt,
            time=time.time(),
            is_life_engine_wake=True,
            life_wake_reason=reason,
            life_wake_importance=normalized_importance,
            life_wake_message=text,
            **target_extra,
        )

        wake_requested = bool(proactive_wake)
        wake_triggered = False
        chat_stream.context.add_unread_message(trigger_message)

        if wake_requested:
            try:
                wake_triggered = bool(
                    await get_stream_loop_manager().start_stream_loop(chat_stream.stream_id)
                )
            except Exception as exc:  # noqa: BLE001
                _discard_tell_dfc_unread(chat_stream.context, trigger_message)
                logger.warning(f"唤醒表达层失败，已撤回内在消息: {exc}")
                return False, f"唤醒表达层失败，已撤回内在消息: {exc}"
            if not wake_triggered:
                _discard_tell_dfc_unread(chat_stream.context, trigger_message)
                return False, "唤醒表达层失败：start_stream_loop 返回 false，已撤回内在消息"

        # 记录传话时间
        if life_service:
            life_service.record_tell_dfc()

        logger.info(
            "中枢向内在状态池沉淀了想法碎片: "
            f"stream_id={chat_stream.stream_id} "
            f"importance={normalized_importance} "
            f"proactive_wake={wake_requested} "
            f"reason={reason or '未说明'} "
        )

        note = "已补充并唤醒同一主体的表达层。表达层会自行判断是否吸收；这不是指令。"
        if not wake_requested:
            note = "已补充到同一主体的表达层待处理队列。表达层会自行判断是否吸收；这不是指令。"

        result = {
            "action": "message_to_dfc",
            "stream_id": chat_stream.stream_id,
            "platform": chat_stream.platform,
            "chat_type": chat_stream.chat_type,
            "target_type": resolved_target_type,
            "target_user_id": target_extra.get("target_user_id", ""),
            "target_user_name": target_extra.get("target_user_name", ""),
            "target_group_id": target_extra.get("target_group_id", ""),
            "target_group_name": target_extra.get("target_group_name", ""),
            "importance": normalized_importance,
            "reason": reason,
            "message": text,
            "proactive_wake": wake_requested,
            "wake_triggered": wake_triggered,
            "note": note,
        }

        logger.debug(
            f"[nucleus_tell_dfc] 工具返回结果:\n"
            f"  stream_id: {result['stream_id']}\n"
            f"  platform: {result['platform']}\n"
            f"  chat_type: {result['chat_type']}\n"
            f"  target_type: {result['target_type']}\n"
            f"  target_user_id: {result['target_user_id']}\n"
            f"  target_group_id: {result['target_group_id']}\n"
            f"  importance: {result['importance']}\n"
            f"  reason: {result['reason']}\n"
            f"  message: {result['message']}\n"
            f"  wake_triggered: {result['wake_triggered']}\n"
            f"  note: {result['note']}"
        )

        return True, result


class LifeEngineReadFileTool(BaseTool):
    """读取文件内容工具。"""

    tool_name: str = "nucleus_read_file"
    tool_description: str = (
        "读取你私人空间中的文件内容。"
        "\n\n"
        "**何时使用：**\n"
        "- ✓ 回顾自己写过的日记、笔记、计划\n"
        "- ✓ 查看某个文件的具体内容\n"
        "- ✓ 在编辑文件前，先读取确认内容\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 不知道文件路径 → 先用 nucleus_list_files 或 nucleus_grep_file 找\n"
        "- ✗ 想搜索内容关键词 → 用 nucleus_grep_file\n"
        "\n"
        "**注意：** 结果包含行号（从 1 开始），方便后续用 nucleus_edit_file 时定位。"
        "大文件建议用 offset 和 limit 参数只读取需要的部分。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        path: Annotated[str, "相对于工作空间的文件路径"],
        offset: Annotated[int, "从第几行开始读（1-indexed），默认从头开始"] = 1,
        limit: Annotated[int, "最多读取多少行，0 表示全部"] = 0,
        encoding: Annotated[str, "文件编码，默认utf-8"] = "utf-8",
    ) -> tuple[bool, str | dict]:
        """读取文件内容，支持行号和偏移/限制。

        Returns:
            成功返回 (True, {"path": ..., "content": ..., "size": ...})
            失败返回 (False, error_message)
        """
        valid, result = _resolve_path(self.plugin, path)
        if not valid:
            return False, str(result)

        target = result
        if not target.exists():
            return False, f"文件不存在: {path}"
        if not target.is_file():
            return False, f"路径不是文件: {path}"

        try:
            raw_content = target.read_text(encoding=encoding)
            lines = raw_content.splitlines()
            total_lines = len(lines)

            # 应用 offset 和 limit
            start_idx = max(0, offset - 1)
            if limit > 0:
                end_idx = min(total_lines, start_idx + limit)
            else:
                end_idx = total_lines

            selected_lines = lines[start_idx:end_idx]
            # 添加行号（cat -n 格式）
            numbered_content = "\n".join(
                f"{start_idx + i + 1}\t{line}"
                for i, line in enumerate(selected_lines)
            )

            stat = target.stat()
            result_data: dict[str, Any] = {
                "action": "read_file",
                "path": path,
                "content": numbered_content,
                "total_lines": total_lines,
                "showing": f"{start_idx + 1}-{end_idx}",
                "size_human": _format_size(stat.st_size),
            }
            if end_idx < total_lines:
                result_data["truncated"] = True
                result_data["remaining_lines"] = total_lines - end_idx

            return True, result_data
        except UnicodeDecodeError as e:
            return False, f"文件编码错误，请尝试其他编码: {e}"
        except Exception as e:
            logger.error(f"读取文件失败 {path}: {e}", exc_info=True)
            return False, f"读取文件失败: {e}"


class LifeEngineWriteFileTool(BaseTool):
    """写入文件工具（覆盖）。"""

    tool_name: str = "nucleus_write_file"
    tool_description: str = (
        "创建新文件或覆盖已有文件的全部内容。"
        "\n\n"
        "**何时使用：**\n"
        "- ✓ 写一篇新的日记、笔记或计划\n"
        "- ✓ 创建一个全新的文件\n"
        "- ✓ 需要完全重写某个文件的内容\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 只想修改文件中的一小部分 → 用 nucleus_edit_file（更安全、更精准）\n"
        "- ✗ 不确定文件当前内容 → 先用 nucleus_read_file 确认\n"
        "\n"
        "**⚠️ 注意：** 如果文件已存在，其全部内容会被覆盖。"
        "修改文件的局部内容，优先使用 nucleus_edit_file。\n"
        "**💡 记忆提示：** 写入新文件后，想一想它和已有文件有没有关联？"
        "用 nucleus_relate_file 建立关联可以帮助未来的回忆。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        path: Annotated[str, "相对于工作空间的文件路径"],
        content: Annotated[str, "要写入的内容"],
        encoding: Annotated[str, "文件编码，默认utf-8"] = "utf-8",
        reason: Annotated[str, "可选：这次写入/覆盖文件的原因，便于未来追溯"] = "",
    ) -> tuple[bool, str | dict]:
        """写入文件（覆盖模式）。

        Returns:
            成功返回 (True, {"path": ..., "size": ..., "created": ...})
            失败返回 (False, error_message)
        """
        valid, result = _resolve_path(self.plugin, path)
        if not valid:
            return False, str(result)

        target = result
        existed = target.exists()
        before_content = _read_trace_before_content(target, encoding)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_text, content, encoding=encoding)
            stat = target.stat()
            trace_id = _record_file_trace(
                self.plugin,
                path=path,
                before_content=before_content,
                after_content=content,
                operation="write",
                tool_name=self.tool_name,
                reason=reason,
                **_tool_trace_context(self),
            )

            # 同步 SQLite/FTS/outbox 文档索引
            await _sync_memory_embedding_for_file(self.plugin, path, content)

            # 完美架构：自动检测并建立演化链
            await _auto_create_lineage_edge(
                self.plugin,
                path=path,
                before_content=before_content,
                after_content=content,
                reason=reason,
                operation="write",
            )

            return True, {
                "action": "write_file",
                "path": path,
                "size_human": _format_size(stat.st_size),
                "created": not existed,
                "trace_id": trace_id,
                **(
                    {"warning": warning}
                    if (warning := build_memory_write_warning(path, content)) is not None
                    else {}
                ),
            }
        except Exception as e:
            logger.error(f"写入文件失败 {path}: {e}", exc_info=True)
            return False, f"写入文件失败: {e}"


class LifeEngineEditFileTool(BaseTool):
    """编辑文件工具（查找替换）。"""

    tool_name: str = "nucleus_edit_file"
    tool_description: str = (
        "精确编辑文件中的特定内容（查找并替换）。"
        "\n\n"
        "**何时使用：**\n"
        "- ✓ 修改文件中的一段具体文字（如改日记中的一句话）\n"
        "- ✓ 批量重命名文件中的某个词（用 replace_all=True）\n"
        "\n"
        "**使用规则：**\n"
        "- 必须先用 nucleus_read_file 读取文件，确认要替换的内容\n"
        "- old_text 必须与文件中的内容完全一致（包括缩进）\n"
        "- 如果 old_text 在文件中出现多次且你只想改一处，提供更长的上下文使其唯一\n"
        "- 用 replace_all=True 可以替换所有出现位置（如重命名变量）\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 想重写整个文件 → 用 nucleus_write_file\n"
        "- ✗ 还没看过文件内容 → 先用 nucleus_read_file"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        path: Annotated[str, "相对于工作空间的文件路径"],
        old_text: Annotated[str, "要查找的原始文本（必须与文件内容完全一致）"],
        new_text: Annotated[str, "替换后的新文本"],
        replace_all: Annotated[bool, "是否替换所有出现的位置（默认只替换第一处）"] = False,
        encoding: Annotated[str, "文件编码，默认utf-8"] = "utf-8",
        reason: Annotated[str, "可选：这次编辑文件的原因，便于未来追溯"] = "",
    ) -> tuple[bool, str | dict]:
        """编辑文件中的特定内容。

        Returns:
            成功返回 (True, {"path": ..., "replacements": ...})
            失败返回 (False, error_message)
        """
        valid, result = _resolve_path(self.plugin, path)
        if not valid:
            return False, str(result)

        target = result
        if not target.exists():
            return False, f"文件不存在: {path}"
        if not target.is_file():
            return False, f"路径不是文件: {path}"

        try:
            content = target.read_text(encoding=encoding)
            count = content.count(old_text)

            if count == 0:
                return False, (
                    "未找到要替换的文本。请确认：\n"
                    "1. 是否先用 nucleus_read_file 读取了最新内容？\n"
                    "2. old_text 是否与文件内容完全一致（注意空格和缩进）？"
                )

            if count > 1 and not replace_all:
                return False, (
                    f"old_text 在文件中出现了 {count} 次，无法确定要替换哪一处。\n"
                    "请提供更多上下文使 old_text 唯一，或使用 replace_all=True 替换全部。"
                )

            if replace_all:
                new_content = content.replace(old_text, new_text)
                replacements = count
            else:
                new_content = content.replace(old_text, new_text, 1)
                replacements = 1

            await asyncio.to_thread(target.write_text, new_content, encoding=encoding)
            trace_id = _record_file_trace(
                self.plugin,
                path=path,
                before_content=content,
                after_content=new_content,
                operation="edit",
                tool_name=self.tool_name,
                reason=reason,
                **_tool_trace_context(self),
            )

            # 同步 SQLite/FTS/outbox 文档索引
            await _sync_memory_embedding_for_file(self.plugin, path, new_content)

            # 完美架构：自动检测并建立演化链
            await _auto_create_lineage_edge(
                self.plugin,
                path=path,
                before_content=content,
                after_content=new_content,
                reason=reason,
                operation="edit",
            )

            return True, {
                "action": "edit_file",
                "path": path,
                "replacements": replacements,
                "trace_id": trace_id,
            }
        except UnicodeDecodeError as e:
            return False, f"文件编码错误: {e}"
        except Exception as e:
            logger.error(f"编辑文件失败 {path}: {e}", exc_info=True)
            return False, f"编辑文件失败: {e}"


# LifeEngineMoveFileTool 和 LifeEngineDeleteFileTool 已移除。
# 移动/删除文件可通过 nucleus_bash 执行 mv/rm 命令实现。


class LifeEngineListFilesTool(BaseTool):
    """列出目录内容工具。"""

    tool_name: str = "nucleus_list_files"
    tool_description: str = (
        "列出目录中的文件和子目录。\n\n"
        "**何时使用：**\n"
        "- ✓ 浏览自己的文件结构\n"
        "- ✓ 确认某个目录下有什么文件\n"
        "- ✓ 用 recursive=True 查看文件树\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 想搜索文件内容 → 用 nucleus_grep_file\n"
        "- ✗ 想看文件的大小/修改时间等 → nucleus_list_files 返回的列表已经包含这些信息"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        path: Annotated[str, "相对于工作空间的目录路径，空字符串表示根目录"] = "",
        recursive: Annotated[bool, "是否递归列出子目录"] = False,
        max_depth: Annotated[int, "递归最大深度（仅recursive=True时有效）"] = 3,
    ) -> tuple[bool, str | dict]:
        """列出目录内容。

        Args:
            path: 相对于工作空间的目录路径，空字符串表示工作空间根目录
            recursive: 是否递归列出
            max_depth: 最大递归深度

        Returns:
            成功返回 (True, {"path": ..., "items": [...]})
            失败返回 (False, error_message)
        """
        valid, result = _resolve_path(self.plugin, path or ".")
        if not valid:
            return False, str(result)

        target = result
        if not target.exists():
            return False, f"目录不存在: {path or '(root)'}"
        if not target.is_dir():
            return False, f"路径不是目录: {path}"

        workspace = _get_workspace(self.plugin)

        def list_dir(dir_path: Path, current_depth: int) -> list[dict]:
            items = []
            try:
                for entry in sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    rel_path = str(entry.relative_to(workspace))
                    stat = entry.stat()

                    item = {
                        "name": entry.name,
                        "path": rel_path,
                        "type": "directory" if entry.is_dir() else "file",
                        "size": stat.st_size if entry.is_file() else None,
                        "size_human": _format_size(stat.st_size) if entry.is_file() else None,
                        "modified_at": _format_time(stat.st_mtime),
                    }

                    if entry.is_dir() and recursive and current_depth < max_depth:
                        item["children"] = list_dir(entry, current_depth + 1)

                    items.append(item)
            except PermissionError:
                pass
            return items

        try:
            items = list_dir(target, 1)
            return True, {
                "action": "list_files",
                "path": path or "(root)",
                "absolute_path": str(target),
                "workspace": str(workspace),
                "recursive": recursive,
                "max_depth": max_depth if recursive else None,
                "total_items": len(items),
                "items": items,
            }
        except Exception as e:
            logger.error(f"列出目录失败 {path}: {e}", exc_info=True)
            return False, f"列出目录失败: {e}"


class LifeEngineMakeDirectoryTool(BaseTool):
    """创建目录工具。"""

    tool_name: str = "nucleus_mkdir"
    tool_description: str = (
        "在工作空间内创建新目录（含所有父目录）。\n\n"
        "何时用：保存文件前确保目录存在；按项目/主题组织文件时建立子目录。\n"
        "何时不用：写文件时父目录不存在可先调用本工具；不要用于检查目录是否存在（用 nucleus_ls）。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        path: Annotated[str, "相对于工作空间的目录路径"],
        parents: Annotated[bool, "是否创建父目录"] = True,
    ) -> tuple[bool, str | dict]:
        """创建目录。

        Args:
            path: 相对于工作空间的目录路径
            parents: 是否自动创建父目录

        Returns:
            成功返回 (True, {"path": ...})
            失败返回 (False, error_message)
        """
        valid, result = _resolve_path(self.plugin, path)
        if not valid:
            return False, str(result)

        target = result
        if target.exists():
            if target.is_dir():
                return True, {
                    "action": "mkdir",
                    "path": path,
                    "absolute_path": str(target),
                    "created": False,
                    "message": "目录已存在",
                }
            else:
                return False, f"路径已存在且不是目录: {path}"

        try:
            await asyncio.to_thread(target.mkdir, parents=parents, exist_ok=True)
            return True, {
                "action": "mkdir",
                "path": path,
                "absolute_path": str(target),
                "created": True,
            }
        except Exception as e:
            logger.error(f"创建目录失败 {path}: {e}", exc_info=True)
            return False, f"创建目录失败: {e}"


class LifeEngineRunAgentTool(BaseTool):
    """启动子代理执行复杂操作的工具。"""

    tool_name: str = "nucleus_run_agent"
    tool_description: str = (
        "启动一个子代理来处理复杂的内部多步骤任务。"
        "这是 life_engine 心跳态工具，不是把用户请求转交后台执行的入口。"
        "\n\n"
        "**心跳态边界（重要）：**\n"
        "- life_engine 是潜意识 / 内在状态层，不是后台项目助手。\n"
        "- 只用于整理 life_engine 私有记忆、笔记、思考流，或诊断中枢自身问题。\n"
        "- 不要让子代理承接用户任务、查项目配置、跑命令、改代码、画图或生成对外交付物。\n"
        "- 如果任务来自用户当前请求，交给 life_chatter / 表达层判断和执行。\n"
        "\n"
        "**何时使用：**\n"
        "- ✓ 需要多次私有文件操作的内部整理任务（如整理笔记、归档日记）\n"
        "- ✓ 需要多步推理的内在分析任务（如总结一段时间的关系变化）\n"
        "- ✓ 需要验证 life_engine 内部维护结果\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 单个简单的文件操作 → 直接用对应工具\n"
        "- ✗ 只是想问一个问题或做简单计算 → 自己思考\n"
        "- ✗ 用户让表达层做的事 → 不要在心跳态后台执行\n"
        "\n"
        "**写任务简报的原则（重要！）：**\n"
        "像向内部整理助手简报一样写 task：\n"
        "1. 说明要做什么、为什么这么做\n"
        "2. 提供你已经知道的信息（文件路径、内容位置）\n"
        "3. 说清楚期望的结果是什么样的\n"
        "4. 不要写模糊的指令如「帮我整理一下」，要具体\n"
        "\n"
        "**❌ 错误示例：** task='整理我的笔记'\n"
        "**✅ 正确示例：** task='把 notes/ 目录下所有 .md 文件按创建时间排序，"
        "合并到 notes/archive/2026-03.md 中，保留原始标题作为二级标题'"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        task: Annotated[str, "任务简报：说明要做什么、已知信息、期望结果"],
        context: Annotated[str, "背景信息：你已经了解的、排除的、尝试过的"] = "",
        expected_output: Annotated[str, "期望的输出形式（如 '生成一个文件' 或 '返回一段总结'）"] = "",
        max_rounds: Annotated[int, "最大工具调用轮数（默认 5）"] = 5,
        subagent_type: Annotated[str, "智能体类型: explore, plan, general-purpose, verification"] = "general-purpose",
        run_in_background: Annotated[bool, "是否后台异步运行（结果在下次心跳注入）"] = False,
    ) -> tuple[bool, str | dict]:
        """启动子代理执行复杂任务。

        子代理在独立上下文中运行，工具权限由智能体类型决定。
        general-purpose 拥有完整读写能力，explore/plan/verification 为只读。

        Returns:
            成功返回 (True, {"task": ..., "result": ..., "rounds": ..., "agent_type": ...})
            失败返回 (False, error_message)
        """
        if not task.strip():
            return False, "任务描述不能为空"

        try:
            from ..agents.registry import get_agent_type_registry
            from ..agents.runner import AgentRunner

            registry = get_agent_type_registry()
            type_def = registry.get(subagent_type)
            if type_def is None:
                return False, f"未知智能体类型: {subagent_type}"

            # 允许调用方覆盖 max_rounds
            if max_rounds > 0 and max_rounds != type_def.max_rounds:
                from dataclasses import replace
                type_def = replace(type_def, max_rounds=max(1, min(20, max_rounds)))

            # 拼接上下文信息
            full_context = context
            if expected_output.strip():
                full_context = f"{full_context}\n\n期望输出: {expected_output.strip()}" if full_context else f"期望输出: {expected_output.strip()}"

            # 后台模式：通过 AgentCoordinator 异步执行
            if run_in_background:
                coordinator = self._get_coordinator()
                agent_id = await coordinator.spawn(
                    agent_type=subagent_type,
                    task=task,
                    context=full_context,
                    agent_type_def=type_def,
                )
                return True, {
                    "action": "run_agent_background",
                    "task": task[:200],
                    "agent_id": agent_id,
                    "agent_type": subagent_type,
                    "status": "running",
                }

            # 同步模式：直接执行
            runner = AgentRunner(
                plugin=self.plugin,
                agent_type_def=type_def,
                task_prompt=task,
                context=full_context,
            )
            result = await runner.run()

            if result.success:
                return True, {
                    "action": "run_agent",
                    "task": task[:200],
                    "result": result.result_text,
                    "rounds": result.rounds_used,
                    "agent_type": subagent_type,
                }
            else:
                return False, result.result_text

        except Exception as e:
            logger.error(f"执行子代理失败: {e}", exc_info=True)
            return False, f"执行失败: {e}"

    def _get_coordinator(self) -> AgentCoordinator:
        """获取或创建 AgentCoordinator 单例。"""
        if bool(getattr(self.plugin, "_agent_coordinator_shutdown", False)):
            raise RuntimeError("插件正在停止，不能启动后台智能体")
        coordinator = getattr(self.plugin, "_agent_coordinator", None)
        if coordinator is None or bool(getattr(coordinator, "is_closed", False)):
            from ..agents.coordinator import AgentCoordinator

            coordinator = AgentCoordinator(self.plugin)
            self.plugin._agent_coordinator = coordinator
        return coordinator


class FetchLifeMemoryTool(BaseTool):
    """获取记忆文件完整内容工具。"""

    tool_name: str = "fetch_life_memory"
    tool_description: str = (
        "获取生命中枢记忆文件的完整内容。"
        "\n\n"
        "**何时使用：**\n"
        "- ✓ life_memory_search 返回的摘要不够详细，需要查看完整内容\n"
        "- ✓ 需要深入了解某个记忆文件的全部信息\n"
        "- ✓ 批量读取多个相关记忆文件\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 还不知道要读哪个文件 → 先用 life_memory_explorer 检索\n"
        "- ✗ 只需要摘要信息 → life_memory_search 的结果已经足够\n"
        "- ✗ 想搜索关键词 → 用 life_memory_explorer\n"
        "\n"
        "**注意事项：**\n"
        "- 此工具会消耗较多上下文 token，请谨慎使用\n"
        "- 对于大文件（>5000字符），会自动截断并提示\n"
        "- 建议一次最多读取 3-5 个文件，避免上下文爆炸\n"
        "- 文件路径必须是 life_memory_search 返回的路径"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        file_paths: Annotated[list[str], "要读取的文件路径列表（来自 life_memory_search 的结果）"],
        max_length_per_file: Annotated[int, "每个文件的最大字符数，0=不限制，超过则截断"] = 5000,
        include_metadata: Annotated[bool, "是否包含文件元数据（大小、修改时间等）"] = True,
    ) -> tuple[bool, dict]:
        """批量读取记忆文件的完整内容。"""
        if not file_paths:
            return False, {"error": "file_paths 不能为空"}

        if len(file_paths) > 10:
            return False, {"error": "单次最多读取 10 个文件"}

        files_data: list[dict] = []
        successful = 0
        failed = 0
        workspace = _get_workspace_read_only(self.plugin)
        life_service = _get_life_engine_service(self.plugin)
        memory_service = getattr(life_service, "_memory_service", None) if life_service else None

        for requested_path_str in file_paths:
            requested_path_str = str(requested_path_str or "")
            if not requested_path_str:
                files_data.append({"path": "", "error": "路径为空"})
                failed += 1
                continue

            requested_eligibility = assess_indexed_document_path(requested_path_str)
            if not requested_eligibility.eligible:
                files_data.append(
                    {
                        "path": requested_path_str,
                        "error": f"不是可读取的记忆文档: {requested_eligibility.reason}",
                    }
                )
                failed += 1
                continue

            file_path_str = requested_eligibility.path
            path_resolution: dict[str, Any] | None = None
            if memory_service is not None and hasattr(memory_service, "resolve_canonical_path"):
                try:
                    resolution = await memory_service.resolve_canonical_path(
                        file_path_str,
                        persist_lineage=False,
                        allow_heuristic=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"解析记忆旧路径失败 {file_path_str}: {exc}")
                    resolution = None
                if resolution and resolution.get("resolved"):
                    resolved_path_str = str(resolution.get("resolved_path") or "")
                    resolved_eligibility = assess_indexed_document_path(resolved_path_str)
                    if not resolved_eligibility.eligible:
                        files_data.append(
                            {
                                "path": requested_path_str,
                                "error": (
                                    "不是可读取的记忆文档: "
                                    f"{resolved_eligibility.reason}"
                                ),
                            }
                        )
                        failed += 1
                        continue
                    file_path_str = resolved_eligibility.path
                    path_resolution = dict(resolution)
                    path_resolution["requested_path"] = requested_path_str
                    path_resolution["resolved_path"] = file_path_str
                    path_resolution["resolved"] = True

            try:
                content, source_mtime, size_bytes = read_workspace_document(
                    workspace,
                    file_path_str,
                    max_bytes=_MEMORY_READ_MAX_BYTES,
                )
            except FileNotFoundError:
                error_data: dict[str, Any] = {
                    "path": requested_path_str,
                    "error": "文件不存在",
                }
                if path_resolution:
                    error_data["path_resolution"] = path_resolution
                files_data.append(error_data)
                failed += 1
                continue
            except (OSError, ValueError) as exc:
                error_data = {
                    "path": requested_path_str,
                    "error": f"读取失败: {exc}",
                }
                if path_resolution:
                    error_data["path_resolution"] = path_resolution
                files_data.append(error_data)
                failed += 1
                continue

            truncated = max_length_per_file > 0 and len(content) > max_length_per_file
            if truncated:
                content = content[:max_length_per_file] + "\n\n... (内容过长，已截断)"

            file_data: dict[str, Any] = {
                "path": file_path_str,
                "title": Path(file_path_str).stem,
                "content": content,
                "truncated": truncated,
            }
            if requested_path_str != file_path_str:
                file_data["requested_path"] = requested_path_str
            if path_resolution:
                file_data["path_resolution"] = path_resolution

            # 完美架构：获取完整演化信息（lineage + corrections）
            if memory_service is not None:
                try:
                    lineage_info = await _get_file_lineage_info(
                        memory_service,
                        file_path_str,
                    )
                    if lineage_info:
                        file_data["lineage"] = lineage_info
                except Exception as exc:
                    logger.debug(f"获取演化信息失败 {file_path_str}: {exc}")

            if include_metadata:
                now = time.time()
                days_ago = int((now - source_mtime) / 86400)
                if days_ago == 0:
                    time_ago = "今天"
                elif days_ago == 1:
                    time_ago = "昨天"
                elif days_ago < 7:
                    time_ago = f"{days_ago}天前"
                elif days_ago < 30:
                    time_ago = f"{days_ago // 7}周前"
                else:
                    time_ago = f"{days_ago // 30}月前"

                file_data["metadata"] = {
                    "size": _format_size(size_bytes),
                    "modified": time_ago,
                    "ext": Path(file_path_str).suffix or "(无扩展名)",
                }

            files_data.append(file_data)
            successful += 1

        # 记录工具调用，方便调试
        logger.info(
            f"[fetch_life_memory] 表达层调用文件读取工具:\n"
            f"  请求文件数: {len(file_paths)}\n"
            f"  成功: {successful} 个\n"
            f"  失败: {failed} 个\n"
            f"  文件列表: {file_paths}"
        )

        result = {
            "action": "fetch_life_memory",
            "total_files": len(file_paths),
            "successful": successful,
            "failed": failed,
            "files": files_data,
            "note": f"成功读取 {successful} 个文件，{failed} 个失败" if failed > 0 else f"成功读取 {successful} 个文件",
        }

        return True, result


# 导出所有工具类
ALL_TOOLS = [
    LifeEngineReadFileTool,
    LifeEngineWriteFileTool,
    LifeEngineEditFileTool,
    LifeEngineListFilesTool,
    LifeEngineWakeDFCTool,
    LifeEngineRunAgentTool,
    FetchLifeMemoryTool,
]
