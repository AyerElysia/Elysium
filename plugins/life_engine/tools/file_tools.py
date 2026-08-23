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
import difflib
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..core.config import LifeEngineConfig
from ..memory.eligibility import (
    DEFAULT_MAX_DOCUMENT_BYTES,
    assess_document_path,
    assess_indexed_document_path,
    read_workspace_document,
)
from ..memory.prompting import build_memory_write_warning
from ._utils import (
    _get_workspace,
    _resolve_path,
)
from .bounded_projection import (
    BoundedContinuationError,
    project_bounded_items,
    project_bounded_text,
    sha256_json,
)

if TYPE_CHECKING:
    from ..agents.coordinator import AgentCoordinator

logger = log_api.get_logger("life_engine.tools")

_MEMORY_READ_MAX_BYTES = DEFAULT_MAX_DOCUMENT_BYTES
_SUBJECT_AUTHORITY_PATHS = frozenset({"SOUL.md", "USER.md", "MEMORY.md"})
_PROACTIVE_PATH_DEFAULTS = {
    "local_database_path": "runtime/proactive/proactive.sqlite3",
    "local_authority_state_path": "runtime/proactive/authority.json",
    "backend_binding_path": "runtime/proactive/backend-binding.json",
}
_RETIRED_IMMUTABLE_PATHS = frozenset({"thoughts/streams.json"})


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


def _notify_router_context_source_changed(plugin: Any, path: str) -> None:
    """Wake the derived Router projection after an authoritative file write."""

    service = _get_life_engine_service(plugin)
    notify = getattr(service, "notify_router_context_source_changed", None)
    if callable(notify):
        notify(path)


async def _record_memory_artifact_version(
    plugin: Any,
    *,
    path: str,
    before_content: str | None,
    after_content: str,
    operation: str,
    reason: str,
    trace_id: str,
    source_event_id: str,
    stream_id: str,
) -> str:
    """Persist immutable before/after versions for a memory document."""

    eligibility = assess_document_path(path)
    if not eligibility.eligible:
        return ""
    service = _get_life_engine_service(plugin)
    memory_service = getattr(service, "_memory_service", None) if service else None
    if memory_service is None:
        return ""
    logical_key = eligibility.path
    history = await memory_service.get_memory_artifact_history(logical_key)
    if before_content is not None and not history:
        await memory_service.version_memory_artifact(
            logical_key=logical_key,
            artifact_kind="workspace_memory_document",
            content=before_content,
            authored_by="life_engine",
            stream_scope=stream_id,
            predicate="captured_before_change",
            reason="首次接入版本账本时保存修改前内容",
            metadata={
                "captured_before_change": True,
                "source_event_id": source_event_id,
                "trace_id": trace_id,
            },
        )
    before_lines = (before_content or "").splitlines(keepends=True)
    after_lines = after_content.splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{logical_key}@before",
            tofile=f"{logical_key}@after",
        )
    )
    version = await memory_service.version_memory_artifact(
        logical_key=logical_key,
        artifact_kind="workspace_memory_document",
        content=after_content,
        authored_by="life_engine",
        stream_scope=stream_id,
        predicate=f"file_{operation}",
        reason=reason,
        metadata={
            "operation": operation,
            "reason": reason,
            "source_event_id": source_event_id,
            "trace_id": trace_id,
            "diff_from_parent": diff,
        },
    )
    return version.artifact_id


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

        # 获取演化边
        outgoing_edges, incoming_edges = await memory_service.read_lineage_edges(
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
        corrections = await memory_service.read_memory_corrections(
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


async def _record_file_trace(
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
        service = getattr(plugin, "service", None)
        get_store = getattr(service, "life_trace_store", None)
        if not callable(get_store):
            raise RuntimeError("LifeTraceServiceUnavailable")
        record = await get_store().record_change(
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
        if bool(getattr(service, "_selectable_storage_enabled", False)):
            raise
        logger.warning(f"记录 Life Trace 失败 {path}: {exc}")
        return ""


def _tool_trace_context(tool: Any) -> dict[str, str]:
    """从工具运行态取当时的语境，让文件改写在长河里关联到事件与聊天流。"""
    message = getattr(tool, "trigger_message", None)
    return {
        "source_event_id": str(getattr(message, "message_id", "") or ""),
        "stream_id": str(tool.get_current_stream_id() or ""),
    }


def _subject_authority_path(plugin: Any, target: Path) -> str | None:
    """Recognize exact authority files after symlink/path resolution."""

    workspace = _get_workspace(plugin)
    try:
        relative = target.resolve().relative_to(workspace).as_posix()
    except ValueError:
        return None
    return relative if relative in _SUBJECT_AUTHORITY_PATHS else None


def _subject_direct_mutation_error(path: str) -> str:
    review_tool = (
        "nucleus_memory_continuity_review"
        if path == "MEMORY.md"
        else "nucleus_review_subject_document"
    )
    return (
        f"SubjectAuthorityDirectMutationBlocked: `{path}` 是统一主体权威的一部分，"
        f"通用 file 工具不能直接修改。请使用 {review_tool} "
        "查看精确 revision、记录保持不变/稍后再看，或在正式 Subject Authority "
        "可用后提交候选并另行作出接受决定。"
    )


def _workspace_relative(workspace: Path, target: Path) -> str | None:
    try:
        return target.resolve().relative_to(workspace).as_posix()
    except ValueError:
        return None


def _configured_proactive_paths(plugin: Any) -> dict[str, Path]:
    """Resolve configured authority files without creating any target."""

    workspace = _get_workspace(plugin)
    config = getattr(plugin, "config", None)
    proactive = (
        config.proactive
        if isinstance(config, LifeEngineConfig)
        else None
    )
    resolved: dict[str, Path] = {}
    for field_name, default in _PROACTIVE_PATH_DEFAULTS.items():
        value = str(getattr(proactive, field_name, default) or default).strip()
        candidate = Path(value)
        candidate = (
            candidate.resolve()
            if candidate.is_absolute()
            else (workspace / candidate).resolve()
        )
        try:
            candidate.relative_to(workspace)
        except ValueError:
            # Runtime startup rejects this invalid configuration.  It cannot
            # authorize a generic tool to touch outside-workspace state.
            continue
        resolved[field_name] = candidate
    return resolved


def _workspace_authority_mutation_path(
    plugin: Any,
    target: Path,
) -> tuple[str, str] | None:
    """Recognize runtime-owned files after symlink/path resolution."""

    workspace = _get_workspace(plugin)
    exact_target = target.resolve()
    relative = _workspace_relative(workspace, exact_target)
    if relative is None:
        return None
    if relative in _RETIRED_IMMUTABLE_PATHS:
        return relative, "retired_thought_stream_archive"

    paths = _configured_proactive_paths(plugin)
    database = paths.get("local_database_path")
    if database is not None:
        database_family = {
            database,
            database.with_name(database.name + "-wal"),
            database.with_name(database.name + "-shm"),
            database.with_name(database.name + "-journal"),
        }
        if exact_target in database_family:
            return relative, "proactive_database"

    authority = paths.get("local_authority_state_path")
    if authority is not None:
        authority_family = {
            authority,
            authority.with_suffix(".writer.lock"),
            authority.with_suffix(authority.suffix + ".lock"),
        }
        if exact_target in authority_family:
            return relative, "proactive_authority_registry"

    binding = paths.get("backend_binding_path")
    if binding is not None:
        binding_family = {
            binding,
            binding.with_suffix(binding.suffix + ".lock"),
        }
        if exact_target in binding_family:
            return relative, "proactive_backend_binding"

    for owner in paths.values():
        if (
            exact_target.parent == owner.parent
            and exact_target.name.startswith(f".{owner.name}.")
            and exact_target.name.endswith(".tmp")
        ):
            return relative, "proactive_atomic_state"
    return None


def _workspace_authority_mutation_error(path: str, owner: str) -> str:
    return (
        f"WorkspaceAuthorityMutationBlocked: `{path}` 由 {owner} 独占管理。"
        "通用 file 工具和内部子代理不得创建、覆盖或编辑该路径；"
        "请使用 nucleus_proactive_query / nucleus_proactive_command，"
        "旧 ThoughtStream 归档则只能只读。"
    )


class LifeEngineWakeDFCTool(BaseTool):
    """Fail-closed shell for historical direct calls of the retired wake tool."""

    tool_name: str = "nucleus_tell_dfc"
    tool_description: str = (
        "Retired compatibility shell. It is not registered for model use and cannot "
        "select a current, recent, named, account, group, or stream target. Shared "
        "context uses canonical projections; proactive contact uses InitiativeSeed, "
        "explicit reachability, and an audience/surface-bound outreach decision."
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        message: Annotated[str, "Historical context text"],
        reason: Annotated[str, "Historical reason"] = "",
        importance: Annotated[str, "Historical importance"] = "normal",
        proactive_wake: Annotated[bool, "Historical wake flag"] = True,
        stream_id: Annotated[str, "Historical stream id"] = "",
        target_type: Annotated[str, "Historical target type"] = "auto",
        platform: Annotated[str, "Historical platform"] = "",
        target_user_id: Annotated[str, "Historical user id"] = "",
        target_user_name: Annotated[str, "Historical user name"] = "",
        target_group_id: Annotated[str, "Historical group id"] = "",
        target_group_name: Annotated[str, "Historical group name"] = "",
    ) -> tuple[bool, str | dict]:
        del (
            message,
            reason,
            importance,
            proactive_wake,
            stream_id,
            target_type,
            platform,
            target_user_id,
            target_user_name,
            target_group_id,
            target_group_name,
        )
        return (
            False,
            "LegacyRecentStreamWakeRetired: shared context uses canonical projections; "
            "proactive contact requires explicit audience_ref and surface_ref.",
        )


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
        continuation: Annotated[
            str,
            "Optional continuation returned by the previous read page",
        ] = "",
        max_bytes: Annotated[
            int | None,
            "Optional result byte budget; the task hard cap still applies",
        ] = None,
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
            stat_before = target.stat()
            raw_bytes = target.read_bytes()
            stat_after = target.stat()
            if (
                stat_before.st_size != stat_after.st_size
                or stat_before.st_mtime_ns != stat_after.st_mtime_ns
            ):
                return False, "file changed while the read page was prepared"
            raw_content = raw_bytes.decode(encoding)
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

            stat = stat_after
            workspace = _get_workspace_read_only(self.plugin)
            normalized_path = str(target.relative_to(workspace))
            file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            base_payload: dict[str, Any] = {
                "action": "read_file",
                "path": path,
                "normalized_path": normalized_path,
                "total_lines": total_lines,
                "showing": f"{start_idx + 1}-{end_idx}",
                "size_human": _format_size(stat.st_size),
                "source_file_bytes": stat.st_size,
                "file_content_sha256": file_sha256,
            }
            if end_idx < total_lines:
                base_payload["source_selection_truncated"] = True
                base_payload["remaining_lines"] = total_lines - end_idx

            result_data = project_bounded_text(
                projection_name="workspace-file-read",
                task_name=getattr(self, "_runtime_task_name", ""),
                requested_max_bytes=max_bytes,
                binding={
                    "path": normalized_path,
                    "offset": int(offset),
                    "limit": int(limit),
                    "encoding": str(encoding),
                },
                frontier={
                    "path": normalized_path,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "content_sha256": file_sha256,
                },
                base_payload=base_payload,
                content=numbered_content,
                content_ref=(
                    f"workspace-file:{normalized_path}:sha256:{file_sha256}"
                ),
                continuation=continuation,
            )
            if len(str(result_data).encode("utf-8")) > result_data["budget_bytes"]:
                return False, "read file projection exceeded its byte budget"

            return True, result_data
        except BoundedContinuationError as e:
            logger.warning(
                "读取文件续读游标已拒绝: "
                f"error_type={type(e).__name__}"
            )
            return False, f"读取文件失败: {e}"
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
        "SOUL.md、USER.md、MEMORY.md 不能由本工具直接修改；请走主体复盘候选与显式决定链。\n"
        "**💡 记忆提示：** 写入新文件后，想一想它和已有文件有没有关联？"
        "如需由当前主体明确表达关系，请使用 nucleus_relations(action=add)。"
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
        subject_path = _subject_authority_path(self.plugin, target)
        if subject_path is not None:
            return False, _subject_direct_mutation_error(subject_path)
        reserved = _workspace_authority_mutation_path(self.plugin, target)
        if reserved is not None:
            return False, _workspace_authority_mutation_error(*reserved)
        existed = target.exists()
        before_content = _read_trace_before_content(target, encoding)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            trace_context = _tool_trace_context(self)
            await asyncio.to_thread(target.write_text, content, encoding=encoding)
            stat = target.stat()
            trace_id = await _record_file_trace(
                self.plugin,
                path=path,
                before_content=before_content,
                after_content=content,
                operation="write",
                tool_name=self.tool_name,
                reason=reason,
                **trace_context,
            )

            artifact_id = await _record_memory_artifact_version(
                self.plugin,
                path=path,
                before_content=before_content,
                after_content=content,
                operation="write",
                reason=reason,
                trace_id=trace_id,
                **trace_context,
            )

            _notify_router_context_source_changed(self.plugin, path)

            # 同步 SQLite/FTS/outbox 文档索引
            await _sync_memory_embedding_for_file(self.plugin, path, content)

            return True, {
                "action": "write_file",
                "path": path,
                "size_human": _format_size(stat.st_size),
                "created": not existed,
                "trace_id": trace_id,
                "artifact_version_id": artifact_id,
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
        "- ✗ 还没看过文件内容 → 先用 nucleus_read_file\n"
        "- ✗ 修改 SOUL.md、USER.md、MEMORY.md → 用主体复盘候选与显式决定链"
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
        subject_path = _subject_authority_path(self.plugin, target)
        if subject_path is not None:
            return False, _subject_direct_mutation_error(subject_path)
        reserved = _workspace_authority_mutation_path(self.plugin, target)
        if reserved is not None:
            return False, _workspace_authority_mutation_error(*reserved)
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

            trace_context = _tool_trace_context(self)
            await asyncio.to_thread(
                target.write_text,
                new_content,
                encoding=encoding,
            )
            trace_id = await _record_file_trace(
                self.plugin,
                path=path,
                before_content=content,
                after_content=new_content,
                operation="edit",
                tool_name=self.tool_name,
                reason=reason,
                **trace_context,
            )

            artifact_id = await _record_memory_artifact_version(
                self.plugin,
                path=path,
                before_content=content,
                after_content=new_content,
                operation="edit",
                reason=reason,
                trace_id=trace_id,
                **trace_context,
            )

            _notify_router_context_source_changed(self.plugin, path)

            # 同步 SQLite/FTS/outbox 文档索引
            await _sync_memory_embedding_for_file(self.plugin, path, new_content)

            return True, {
                "action": "edit_file",
                "path": path,
                "replacements": replacements,
                "trace_id": trace_id,
                "artifact_version_id": artifact_id,
            }
        except UnicodeDecodeError as e:
            return False, f"文件编码错误: {e}"
        except Exception as e:
            logger.error(f"编辑文件失败 {path}: {e}", exc_info=True)
            return False, f"编辑文件失败: {e}"


# LifeEngineMoveFileTool 和 LifeEngineDeleteFileTool 已移除；只读 sandbox
# 中的 nucleus_bash 也不能移动或删除 workspace 文件。


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
        continuation: Annotated[
            str,
            "Optional continuation returned by the previous directory page",
        ] = "",
        max_bytes: Annotated[
            int | None,
            "Optional result byte budget; the task hard cap still applies",
        ] = None,
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

        directory_frontier: list[dict[str, Any]] = []

        def list_dir(dir_path: Path, current_depth: int) -> list[dict]:
            items = []
            try:
                dir_stat_before = dir_path.stat()
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
                dir_stat_after = dir_path.stat()
                if (
                    dir_stat_before.st_mtime_ns != dir_stat_after.st_mtime_ns
                    or dir_stat_before.st_size != dir_stat_after.st_size
                ):
                    raise RuntimeError(
                        "directory changed while the list page was prepared"
                    )
                directory_frontier.append(
                    {
                        "path": str(dir_path.relative_to(workspace)),
                        "mtime_ns": dir_stat_after.st_mtime_ns,
                        "size": dir_stat_after.st_size,
                    }
                )
            except PermissionError:
                pass
            return items

        try:
            items = list_dir(target, 1)

            def flatten(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
                flattened: list[dict[str, Any]] = []
                for entry in entries:
                    current = {
                        key: value
                        for key, value in entry.items()
                        if key != "children"
                    }
                    flattened.append(current)
                    children = entry.get("children")
                    if isinstance(children, list):
                        flattened.extend(flatten(children))
                return flattened

            source_items = flatten(items)
            normalized_root = str(target.relative_to(workspace)) or "."
            item_refs = []
            for item in source_items:
                item_hash = sha256_json(item)
                item_refs.append(
                    f"workspace-entry:{item.get('path') or 'unknown'}:sha256:{item_hash}"
                )
            result = project_bounded_items(
                projection_name="workspace-file-list",
                task_name=getattr(self, "_runtime_task_name", ""),
                requested_max_bytes=max_bytes,
                binding={
                    "root": normalized_root,
                    "pattern": "",
                    "recursive": bool(recursive),
                    "max_depth": int(max_depth),
                },
                frontier={
                    "directories": sorted(
                        directory_frontier,
                        key=lambda item: str(item.get("path") or ""),
                    ),
                    "items_sha256": sha256_json(source_items),
                },
                base_payload={
                    "action": "list_files",
                    "path": path or "(root)",
                    "normalized_root": normalized_root,
                    "recursive": recursive,
                    "max_depth": max_depth if recursive else None,
                    "total_items": len(source_items),
                },
                items_key="items",
                items=source_items,
                item_refs=item_refs,
                continuation=continuation,
                compact=True,
            )
            if len(str(result).encode("utf-8")) > result["budget_bytes"]:
                return False, "list files projection exceeded its byte budget"
            return True, result
        except BoundedContinuationError as e:
            logger.warning(
                "列出目录续读游标已拒绝: "
                f"error_type={type(e).__name__}"
            )
            return False, f"列出目录失败: {e}"
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
        reserved = _workspace_authority_mutation_path(self.plugin, target)
        if reserved is not None:
            return False, _workspace_authority_mutation_error(*reserved)
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
        "- 只用于整理 life_engine 私有记忆、普通笔记，或诊断中枢自身问题。\n"
        "- 主动状态只能通过统一 proactive 工具读写；旧 ThoughtStream 归档只能只读。\n"
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
                    "task": task,
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
                    "task": task,
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
        "- 返回经过安全大小校验的完整文档，不会静默截断记忆\n"
        "- 如需控制上下文，应先缩小 file_paths，而不是切断文档内容\n"
        "- 文件路径必须是 life_memory_search 返回的路径"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        file_paths: Annotated[list[str], "要读取的文件路径列表（来自 life_memory_search 的结果）"],
        max_length_per_file: Annotated[int, "兼容参数；记忆文档不再按字符数截断"] = 0,
        include_metadata: Annotated[bool, "是否包含文件元数据（大小、修改时间等）"] = True,
    ) -> tuple[bool, dict]:
        """批量读取记忆文件的完整内容。"""
        if not file_paths:
            return False, {"error": "file_paths 不能为空"}

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

            file_data: dict[str, Any] = {
                "path": file_path_str,
                "title": Path(file_path_str).stem,
                "content": content,
                "truncated": False,
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
    LifeEngineRunAgentTool,
    FetchLifeMemoryTool,
]
