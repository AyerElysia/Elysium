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
import os
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import uuid4

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
from .apply_patch import (
    ApplyPatchError,
    apply_ops_to_contents,
    parse_apply_patch,
    strip_read_line_prefixes,
)
from .bounded_projection import (
    BoundedContinuationError,
    _finalize_delivered_bytes,
    project_bounded_items,
    project_bounded_text,
    resolve_tool_result_budget,
    sha256_json,
)
from .managed_file_reads import read_managed_file_view
from .managed_files import (
    FileSnapshot,
    ManagedFileSession,
    pin_file_continuation,
    selected_file_session,
    split_file_continuation,
)

if TYPE_CHECKING:
    from ..agents.coordinator import AgentCoordinator

logger = log_api.get_logger("life_engine.tools")

FILE_CHATTER_ALLOW = ["life_engine_internal", "life_chatter"]
_GLOB_IGNORE_DIRS = {".memory", "__pycache__", ".git", ".svn", "node_modules"}

_MEMORY_READ_MAX_BYTES = DEFAULT_MAX_DOCUMENT_BYTES
DEFAULT_READ_LINE_LIMIT = 80
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


def _list_glob_matches(rel_path: str, name: str, glob: str) -> bool:
    """Match a workspace-relative path or basename against comma-separated globs."""
    patterns = [item.strip() for item in str(glob or "").split(",") if item.strip()]
    if not patterns:
        return True
    relative = PurePosixPath(str(rel_path or "").replace("\\", "/"))
    basename = PurePosixPath(str(name or ""))
    return any(
        relative.match(pattern)
        or basename.match(pattern)
        or (
            pattern.startswith("**/")
            and (
                basename.match(pattern[3:])
                or relative.match(pattern[3:])
            )
        )
        for pattern in patterns
    )


def select_read_line_window(
    total_lines: int,
    *,
    offset: int,
    limit: int,
    from_end: bool,
) -> tuple[int, int]:
    """Return a half-open line window ``[start_idx, end_idx)``.

    ``limit <= 0`` means the whole file. ``from_end`` takes the last ``limit``
    lines and ignores ``offset``.
    """
    if total_lines <= 0:
        return 0, 0
    if int(limit) <= 0:
        return 0, total_lines
    window = min(int(limit), total_lines)
    if from_end:
        start_idx = max(0, total_lines - window)
        return start_idx, total_lines
    start_idx = max(0, int(offset) - 1)
    if start_idx >= total_lines:
        return total_lines, total_lines
    return start_idx, min(total_lines, start_idx + window)




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
    """Recognize standing prompt files after symlink/path resolution."""

    workspace = _get_workspace(plugin)
    try:
        relative = target.resolve().relative_to(workspace).as_posix()
    except ValueError:
        return None
    return relative if relative in _SUBJECT_AUTHORITY_PATHS else None


def _standing_prompt_content_error(
    plugin: Any,
    target: Path,
    content: str,
) -> str | None:
    """Reject only the writes that would make the next turn un-assemblable."""

    subject_path = _subject_authority_path(plugin, target)
    if subject_path == "SOUL.md" and not str(content or "").strip():
        return (
            "StandingPromptSoulEmpty: `SOUL.md` 会固定进入每一轮提示词，"
            "需要保持有实质内容。空掉它，这一轮的你就无法被装配出来。"
        )
    return None


def _standing_prompt_structural_error(plugin: Any, target: Path) -> str | None:
    """Keep SOUL/USER/MEMORY at their assembled paths; content edits stay free."""

    subject_path = _subject_authority_path(plugin, target)
    if subject_path is None:
        return None
    return (
        f"StandingPromptPathProtected: `{subject_path}` 会固定进入每一轮提示词，"
        "不能删除或改名。要改内容请直接编辑。"
    )


def _plugin_life_service(plugin: Any) -> Any:
    service = getattr(plugin, "service", None)
    if service is not None:
        return service
    return _get_life_engine_service(plugin)


async def _commit_subject_authority_file_write(
    tool: Any,
    target: Path,
    content: str,
    *,
    encoding: str,
    reason: str,
    occurrence_id: str,
) -> tuple[bool, str | None]:
    """CAS-append SOUL/USER/MEMORY so the next prompt reads what she just wrote."""

    plugin = tool.plugin
    subject_path = _subject_authority_path(plugin, target)
    if subject_path is None:
        return True, None
    service = _plugin_life_service(plugin)
    if service is None or not bool(
        getattr(service, "_selectable_storage_enabled", False)
    ):
        return True, None
    commit = getattr(service, "commit_subject_authority_file_write", None)
    if not callable(commit):
        return False, "SelectedSubjectCommitUnavailable"
    scope = (getattr(getattr(tool, "trigger_message", None), "extra", {}) or {}).get(
        "life_turn_scope", {}
    )
    scope = scope if isinstance(scope, dict) else {}
    activities = scope.get("conscious_activity_ids", {})
    activities = activities if isinstance(activities, dict) else {}
    actor = str(getattr(tool, "_life_source_instance_id", "") or scope.get(
        "consciousness_instance_id", ""
    )).strip()
    source = str(getattr(tool, "_life_source_occurrence_id", "") or activities.get(
        str(getattr(tool, "_tool_call_id", "") or ""), ""
    )).strip()
    if not actor or not source:
        return False, "SubjectFileWriteOriginRequired"
    try:
        await commit(
            workspace_relative_path=subject_path,
            content_bytes=str(content).encode(encoding),
            occurrence_id=occurrence_id,
            recorded_by="life_engine",
            recorded_source="nucleus_file_tool",
            encoding=encoding,
            semantic_actor_id=actor,
            semantic_source_id=source,
            occurred_at=str(getattr(tool, "_life_source_occurred_at", "") or "") or None,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            f"写入主体固定提示词账本失败 {subject_path}: {exc}",
            exc_info=True,
        )
        return False, f"写入主体固定提示词账本失败: {exc}"
    return True, None


async def _read_selected_subject_file(plugin: Any, target: Path) -> Any | None:
    """Read immutable selected bytes; missing authority never means stale disk."""

    subject_path = _subject_authority_path(plugin, target)
    if subject_path is None:
        return None
    service = _plugin_life_service(plugin)
    if service is None or not bool(getattr(service, "_selectable_storage_enabled", False)):
        return None
    read = getattr(service, "read_subject_authority_file", None)
    if not callable(read):
        raise RuntimeError("SelectedSubjectReadUnavailable")
    return await read(subject_path)


def _guard_workspace_mutation(plugin: Any, path: str) -> tuple[bool, Any]:
    """Resolve a workspace path and reject runtime-reserved mutations."""

    valid, result = _resolve_path(plugin, path)
    if not valid:
        return False, result
    target = result
    reserved = _workspace_authority_mutation_path(plugin, target)
    if reserved is not None:
        return False, _workspace_authority_mutation_error(*reserved)
    return True, target


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
    if not PurePosixPath(relative).parts:
        return None
    if len(PurePosixPath(relative).parts) > 1 and PurePosixPath(relative).parts[0] in _SUBJECT_AUTHORITY_PATHS:
        return relative, "standing_prompt_file_ancestor"
    if PurePosixPath(relative).parts[0] in {".memory", ".git", ".trace", "runtime"}:
        return relative, "workspace_runtime_or_history_store"
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



def _managed_failure(exc: Exception, *, occurrence_id: str = "", attempted: bool = False) -> tuple[bool, dict[str, Any]]:
    """Never confuse an unknown durable outcome with a pre-commit rejection."""
    from ..storage.subject_contracts import (
        SubjectDocumentConflict,
        SubjectDocumentNotFound,
    )

    rejected = isinstance(exc, (SubjectDocumentConflict, SubjectDocumentNotFound, PermissionError, ValueError))
    return False, {
        "schema": "elysium.file_commit.v1",
        "commit_status": "not_committed" if rejected or not attempted else "unknown",
        "occurrence_id": occurrence_id,
        "error_type": type(exc).__name__,
        "error": str(exc)[:360] if rejected else type(exc).__name__,
        "retry_guidance": (
            "read the current file and submit a new intended operation"
            if rejected or not attempted else "query this operation before retrying; do not invent a new operation identity"
        ),
    }


async def _complete_managed_write(
    tool: Any,
    session: ManagedFileSession,
    committed: dict[str, Any],
    *,
    snapshot: FileSnapshot | None,
    content: str | None,
    encoding: str,
    reason: str,
    operation: str,
) -> dict[str, Any]:
    receipt = await session.finish(committed)
    receipt["trace_id"] = ""
    receipt["artifact_version_id"] = committed["version_id"]
    receipt["trace_projection"] = {"status": "not_replayed" if committed["idempotent_replay"] else "not_requested"}
    if snapshot is not None and not committed["idempotent_replay"]:
        try:
            receipt["trace_id"] = await _record_file_trace(
                tool.plugin,
                path=str(committed["logical_path"]).removeprefix("life_engine_workspace/"),
                before_content=snapshot.content.decode(encoding) if snapshot.content is not None else None,
                after_content=content,
                operation=operation,
                tool_name=tool.tool_name,
                reason=reason,
                **_tool_trace_context(tool),
            )
            receipt["trace_projection"] = {"status": "recorded"}
        except Exception as exc:  # noqa: BLE001 - history already committed; report failed projection
            receipt["trace_projection"] = {
                "status": "rebuild_from_document_history",
                "error_type": type(exc).__name__,
            }
    return receipt


async def _execute_managed_write(
    tool: Any,
    target: Path,
    *,
    content: str,
    encoding: str,
    reason: str,
    expected_version: str,
    old_text: str | None = None,
    replace_all: bool = False,
) -> tuple[bool, str | dict[str, Any]] | None:
    try:
        session = selected_file_session(tool, _plugin_life_service(tool.plugin))
    except Exception as exc:
        return _managed_failure(exc)
    if session is None:
        return None
    occurrence = ""
    attempted = False
    snapshot = None
    try:
        origin = await session.origin()
        occurrence = session.occurrence(origin[0], origin[1])
        request = {
            "tool": tool.tool_name, "path": session.relative(target),
            "content": content, "encoding": encoding, "reason": reason,
            "expected_version": expected_version, "old_text": old_text,
            "replace_all": replace_all,
        }
        digest = session.request_digest(request)
        committed = await session.replay(occurrence, digest)
        replacements = 0
        if committed is None:
            snapshot = await session.read(target, allow_missing=old_text is None)
            session.require_pin(snapshot, expected_version)
            if old_text is not None:
                if snapshot.content is None:
                    raise ValueError("ManagedFileNotFound")
                before = snapshot.content.decode(encoding)
                search = old_text
                count = before.count(search)
                if count == 0:
                    stripped = strip_read_line_prefixes(old_text)
                    if stripped is not None:
                        search = stripped
                        count = before.count(search)
                if not search or count == 0 or (count > 1 and not replace_all):
                    raise ValueError("ManagedFileEditNeedsUniqueExactText")
                replacement = content
                if search != old_text:
                    stripped_new = strip_read_line_prefixes(content)
                    if stripped_new is not None:
                        replacement = stripped_new
                content = before.replace(search, replacement, -1 if replace_all else 1)
                replacements = count if replace_all else 1
            standing = _standing_prompt_content_error(tool.plugin, target, content)
            if standing is not None:
                raise PermissionError(standing)
            attempted = True
            committed = await session.write(
                snapshot, content.encode(encoding),
                expected_version=expected_version,
                occurrence=occurrence, request_digest=digest, origin=origin,
                encoding=encoding, reason=reason,
            )
        else:
            version = await session.store.get_version(committed["version_id"])
            content = version.content_bytes.decode(encoding)
        receipt = await _complete_managed_write(
            tool, session, committed, snapshot=snapshot, content=content,
            encoding=encoding, reason=reason,
            operation="edit" if old_text is not None else "write",
        )
        receipt.update(
            action="edit_file" if old_text is not None else "write_file",
            path=session.relative(target),
            created=snapshot is not None and snapshot.content is None,
            size_human=_format_size(len(content.encode(encoding))),
        )
        if old_text is not None:
            receipt["replacements"] = replacements if not committed["idempotent_replay"] else None
        return True, receipt
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if attempted and occurrence:
            try:
                committed = await session.replay(occurrence, digest)
                if committed is not None:
                    return True, await session.finish(committed)
            except asyncio.CancelledError:
                raise
            except Exception as reconcile_error:  # noqa: BLE001 - retain unknown commit outcome
                logger.warning("file commit reconciliation failed: %s", type(reconcile_error).__name__)
        return _managed_failure(exc, occurrence_id=occurrence, attempted=attempted)



def _bounded_file_batch_receipt(tool: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep every operation identity/status; omit only duplicate presentation data."""
    _, budget = resolve_tool_result_budget(getattr(tool, "_runtime_task_name", ""), None)
    if len(str(payload).encode("utf-8")) <= budget:
        return payload
    compact = dict(payload)
    captures = compact.pop("legacy_captures", [])
    compact["legacy_capture_count"] = len(captures)
    compact["receipt_details"] = "read_file view=operation with each occurrence_id returns its immutable receipt"
    if "files" in compact:
        compact["files"] = [
            {
                **{key: item[key] for key in (
                    "occurrence_id", "operation", "document_id", "version_id", "commit_status",
                )},
                "ordinal": ordinal,
                "projection_status": item["projection"]["status"],
                "index_projection_status": item["index_projection"]["status"],
                "trace_projection_status": item.get("trace_projection", {}).get("status", "not_requested"),
                "context_notification_status": item["context_notification"]["status"],
            }
            for ordinal, item in enumerate(compact["files"])
        ]
    if len(str(compact).encode("utf-8")) > budget:
        raise RuntimeError("ManagedFileReceiptBudgetExceeded")
    return compact


async def _execute_managed_patch(
    tool: Any,
    ops: Any,
    resolved: dict[str, Path],
    *,
    input_text: str,
    reason: str,
    encoding: str,
    expected_versions: dict[str, str] | None,
) -> tuple[bool, str | dict[str, Any]] | None:
    try:
        session = selected_file_session(tool, _plugin_life_service(tool.plugin))
    except Exception as exc:
        return _managed_failure(exc)
    if session is None:
        return None
    attempted = False
    occurrences: list[str] = []
    digests: list[str] = []
    captures: list[str] = []
    try:
        if len(ops) > 8:
            raise ValueError("ManagedFileBatchExceedsOperationBudget")
        expected = expected_versions or {}
        if not isinstance(expected, dict):
            raise ValueError("ManagedFileExpectedVersionsMustBeAnObject")
        origin = await session.origin()
        canonical_paths: list[str] = []
        for op in ops:
            canonical_paths.append(session.relative(resolved[op.path]))
            destination = op.move_to or op.copy_to
            if destination:
                canonical_paths.append(session.relative(resolved[destination]))
        if len(canonical_paths) != len(set(canonical_paths)):
            raise ValueError("ManagedFileBatchPathsOverlap: combine hunks or use separate calls")
        ordered_paths = sorted(canonical_paths)
        if any(
            right.startswith(left + "/")
            for left, right in zip(ordered_paths, ordered_paths[1:])
        ):
            raise ValueError("ManagedFileBatchFileDirectoryConflict")
        replayed: list[dict[str, Any] | None] = []
        for ordinal, op in enumerate(ops):
            occurrence = session.occurrence(origin[0], origin[1], str(ordinal))
            digest = session.request_digest({
                "tool": tool.tool_name, "input": input_text, "reason": reason,
                "encoding": encoding, "expected_versions": expected,
                "ordinal": ordinal,
            })
            occurrences.append(occurrence)
            digests.append(digest)
            replayed.append(await session.replay(occurrence, digest))
        if any(replayed):
            if not all(replayed):
                return False, {
                    "schema": "elysium.file_batch_commit.v1",
                    "commit_status": "recovery_required",
                    "error": "ManagedFileBatchReceiptSetIncomplete",
                    "occurrence_ids": occurrences,
                }
            receipts = [await session.finish(item) for item in replayed if item is not None]
            return True, _bounded_file_batch_receipt(tool, {
                "schema": "elysium.file_batch_commit.v1",
                "action": "apply_patch", "commit_status": "committed",
                "atomicity": "authority_transaction",
                "idempotent_replay": True, "files": receipts,
            })
        snapshots = {
            path: await session.read(target, allow_missing=True)
            for path, target in resolved.items()
        }
        # Validate every existing read pin before even capturing legacy bytes.
        for op in ops:
            snapshot = snapshots[op.path]
            if op.kind == "add":
                if snapshot.content is not None:
                    raise ValueError("ManagedFileAddTargetExists")
            else:
                session.require_pin(snapshot, str(expected.get(op.path) or ""))
                if snapshot.content is None:
                    raise ValueError("ManagedFilePatchSourceMissing")
            destination = op.move_to or op.copy_to
            if destination and snapshots[destination].content is not None:
                raise ValueError("ManagedFileTargetAlreadyExists")
        commands = []
        prepared: list[tuple[FileSnapshot, str | None, str]] = []
        for ordinal, op in enumerate(ops):
            snapshot = snapshots[op.path]
            token = str(expected.get(op.path) or "")
            target_path = op.move_to or op.copy_to
            content_bytes: bytes | None
            if op.kind == "add":
                content_bytes = op.add_content.encode(encoding)
                content = op.add_content
            elif op.kind == "delete":
                content_bytes = None
                content = None
            elif not op.hunks:
                content_bytes = None
                try:
                    content = snapshot.content.decode(encoding)
                except UnicodeDecodeError:
                    content = None
            else:
                contents = {op.path: snapshot.content.decode(encoding)}
                if target_path:
                    contents[target_path] = None
                planned = apply_ops_to_contents([op], contents)
                content = next(item.content for item in planned if item.action == "write")
                content_bytes = (content or "").encode(encoding)
            checked_target = resolved[target_path or op.path]
            if content is not None:
                standing = _standing_prompt_content_error(tool.plugin, checked_target, content)
                if standing is not None:
                    raise PermissionError(standing)
            operation = "delete" if op.kind == "delete" else (
                "rename" if op.move_to else "copy" if op.copy_to else "write"
            )
            common = {
                "expected_version": token,
                "occurrence": occurrences[ordinal],
                "request_digest": digests[ordinal],
                "origin": origin,
                "reason": reason,
            }
            if operation == "write":
                command = await session.prepare_write(
                    snapshot, content_bytes or b"", encoding=encoding, **common,
                )
            else:
                command = await session.prepare_mutation(
                    snapshot, operation=operation,
                    target=snapshots[target_path] if target_path else None,
                    content_bytes=content_bytes, encoding=encoding, **common,
                )
            if snapshot.legacy:
                captures.append(snapshot.logical_path)
            commands.append(command)
            prepared.append((snapshot, content, operation))
        attempted = True
        commits = await session.store.apply_document_batch(commands)
        receipts = []
        for ordinal, (commit, details) in enumerate(zip(commits, prepared, strict=True)):
            snapshot, content, operation = details
            result = session.commit_result(commit, occurrences[ordinal], operation)
            receipt = await _complete_managed_write(
                tool, session, result, snapshot=snapshot, content=content,
                encoding=encoding, reason=reason, operation=operation,
            )
            receipt["path"] = str(result["logical_path"]).removeprefix("life_engine_workspace/")
            if operation in {"rename", "copy"}:
                receipt["source_path"] = snapshot.path
            receipts.append(receipt)
        return True, _bounded_file_batch_receipt(tool, {
            "schema": "elysium.file_batch_commit.v1",
            "action": "apply_patch", "commit_status": "committed",
            "atomicity": "authority_transaction",
            "files": receipts, "legacy_captures": captures,
        })
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if attempted and occurrences:
            try:
                committed = [
                    await session.replay(occurrence, digest)
                    for occurrence, digest in zip(occurrences, digests, strict=True)
                ]
                if all(committed):
                    return True, _bounded_file_batch_receipt(tool, {
                        "schema": "elysium.file_batch_commit.v1",
                        "action": "apply_patch", "commit_status": "committed",
                        "atomicity": "authority_transaction",
                        "files": [await session.finish(item) for item in committed if item is not None],
                        "legacy_captures": captures,
                    })
            except asyncio.CancelledError:
                raise
            except Exception as reconcile_error:  # noqa: BLE001 - retain unknown batch outcome
                logger.warning("file batch reconciliation failed: %s", type(reconcile_error).__name__)
        ok, failure = _managed_failure(exc, attempted=attempted)
        failure["legacy_captures"] = captures
        failure["occurrence_ids"] = occurrences
        return ok, _bounded_file_batch_receipt(tool, failure)


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
        "**怎么读，由你决定：**\n"
        "- 默认只读 80 行（文件开头）。不是全文。\n"
        "- 日记、追加记录看最新一段：`from_end=true`。\n"
        "- 已经知道大概位置：`offset` 从第几行起，`limit` 读几行。\n"
        "- 先 `nucleus_grep_file` 要命中行，再按行号读周围。\n"
        "- 确实要全文：`limit=0`，此时忽略 offset / from_end 并选择整份文件。\n"
        "- source_selection_truncated 表示行窗口是否遗漏文件开头或结尾；"
        "truncated/continuation 只描述该窗口的传输分页，不能单独证明全文已读。\n"
        "- 全文核验需同一版本、完整行范围及完整传输页；用 remaining_lines_before "
        "/ remaining_lines / next_offset 定位未选行，续传页保持原参数与引用。\n"
        "\n"
        "**注意：** 结果每行是 `行号<TAB>正文`。行号只用于定位。"
        "`nucleus_edit_file` / `nucleus_apply_patch` 的文本必须是去掉行号之后的原文，"
        "不要把 `12\\t` 这种前缀拷进去。"
        "受管文件返回 expected_version；修改时原样传回。旧版用 path＋version_id 精确回取，"
        "或只传完整 file_ref=subject-file:document_id@version_id（不混用其它身份选择器）。"
        "file_ref 只读固定版本正文，续读时保持同一 file_ref 和读取参数。"
        "view=history/operations 查版本或操作历史；改名/删除后用 document_id 查原文档。"
        "view=metadata 可看二进制元信息；view=operation + occurrence_id 可核对提交回执。"
    )
    chatter_allow: list[str] = FILE_CHATTER_ALLOW

    async def execute(
        self,
        path: Annotated[str, "相对于工作空间的文件路径；仅用 file_ref 精确读取时省略"] = "",
        offset: Annotated[int, "从第几行开始读（1-indexed），from_end=true 时忽略"] = 1,
        limit: Annotated[
            int,
            "最多读取多少行。默认 80；0 表示全部",
        ] = DEFAULT_READ_LINE_LIMIT,
        from_end: Annotated[
            bool,
            "true 时从文件末尾往前取 limit 行，适合日记看最新一段",
        ] = False,
        encoding: Annotated[str, "文件编码，默认utf-8"] = "utf-8",
        continuation: Annotated[
            str,
            "Optional continuation returned by the previous read page",
        ] = "",
        max_bytes: Annotated[
            int | None,
            "Optional result byte budget; the task hard cap still applies",
        ] = None,
        version_id: Annotated[str, "可选：精确历史 version_id；不填读取当前版本，续读自动固定原版"] = "",
        view: Annotated[Literal["content", "metadata", "history", "operations", "operation"], "正文、字节元信息、版本历史、操作历史或查询单次提交"] = "content",
        document_id: Annotated[str, "可选稳定文档身份；改名或删除后查询历史时使用"] = "",
        occurrence_id: Annotated[str, "view=operation 时传提交回执中的 occurrence_id"] = "",
        after_recorded_at: Annotated[str, "历史下一数据库页返回的时间游标；先读完 continuation"] = "",
        after_id: Annotated[str, "与 after_recorded_at 一同原样传回"] = "",
        history_limit: Annotated[int, "每数据库页最多100个元数据条目，正文仍按 max_bytes 分页"] = 50,
        file_ref: Annotated[str, "可选完整 subject-file:document_id@version_id；仅正文，不能混用 path/document_id/version_id"] = "",
    ) -> tuple[bool, str | dict]:
        """读取文件内容，支持行号和偏移/限制。

        Returns:
            成功返回 (True, {"path": ..., "content": ..., "size": ...})
            失败返回 (False, error_message)
        """
        if file_ref and (
            path or document_id or version_id or view != "content"
            or occurrence_id or after_recorded_at or after_id
        ):
            return False, "ManagedFileReferenceSelectorConflict"
        if not path and not file_ref:
            return False, "FileReadSelectorRequired: provide path or exact file_ref"
        try:
            session = selected_file_session(self, _plugin_life_service(self.plugin))
            reference_document_id = ""
            if file_ref:
                if session is None:
                    return False, "HistoricalFileReadRequiresSelectedStorage"
                target, reference_document_id, version_id = await session.resolve_reference(file_ref)
                path = session.relative(target)
            else:
                valid, result = _resolve_path(self.plugin, path)
                if not valid:
                    return False, str(result)
                target = result
            snapshot = None
            if view != "content":
                if session is None:
                    return False, "ManagedFileHistoryRequiresSelectedStorage"
                return True, await read_managed_file_view(
                    session, target, view=view, document_id=document_id,
                    version_id=version_id, occurrence_id=occurrence_id,
                    after_recorded_at=after_recorded_at, after_id=after_id,
                    history_limit=history_limit, continuation=continuation,
                    max_bytes=max_bytes,
                )
            if document_id:
                return False, "ContentReadUseExactVersionId: resolve document_id with view=metadata first"
            inner_continuation, selected_version_id = split_file_continuation(continuation, version_id)
            if session is not None:
                snapshot = await session.read(target, version_id=selected_version_id)
                version = snapshot.version
                if file_ref and (
                    version is None or version.document_id != reference_document_id
                    or version.version_id != selected_version_id or snapshot.reference != file_ref
                ):
                    return False, "ManagedFileReferenceReadIdentityConflict"
            else:
                if selected_version_id:
                    return False, "HistoricalFileReadRequiresSelectedStorage"
                version = await _read_selected_subject_file(self.plugin, target)
            if snapshot is not None:
                raw_bytes = snapshot.content
                if raw_bytes is None:
                    return False, "ManagedFileNotFound"
                source_size = len(raw_bytes)
                source_mtime = 0
            elif version is not None:
                raw_bytes = version.content_bytes
                source_size = len(raw_bytes)
                source_mtime = 0
            else:
                if not target.exists():
                    return False, f"文件不存在: {path}"
                if not target.is_file():
                    return False, f"路径不是文件: {path}"
                stat_before = target.stat()
                raw_bytes = await asyncio.to_thread(target.read_bytes)
                stat_after = target.stat()
                if (
                    stat_before.st_size != stat_after.st_size
                    or stat_before.st_mtime_ns != stat_after.st_mtime_ns
                ):
                    return False, "file changed while the read page was prepared"
                source_size = stat_after.st_size
                source_mtime = stat_after.st_mtime_ns
            raw_content = raw_bytes.decode(encoding)
            lines = raw_content.splitlines()
            total_lines = len(lines)

            start_idx, end_idx = select_read_line_window(
                total_lines,
                offset=offset,
                limit=limit,
                from_end=from_end,
            )

            selected_lines = lines[start_idx:end_idx]
            # 添加行号（cat -n 格式）
            numbered_content = "\n".join(
                f"{start_idx + i + 1}\t{line}"
                for i, line in enumerate(selected_lines)
            )

            workspace = _get_workspace_read_only(self.plugin)
            normalized_path = str(target.relative_to(workspace))
            file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            base_payload: dict[str, Any] = {
                "action": "read_file",
                "path": path,
                "normalized_path": normalized_path,
                "total_lines": total_lines,
                "showing": f"{start_idx + 1}-{end_idx}",
                "size_human": _format_size(source_size),
                "source_file_bytes": source_size,
                "file_content_sha256": file_sha256,
                "source_selection_truncated": start_idx > 0 or end_idx < total_lines,
                **({"subject_version_id": version.version_id,
                    "source_authority": "subject_document_store"} if version else {}),
            }
            if snapshot is not None:
                base_payload["expected_version"] = snapshot.expected_version
                base_payload["source_authority"] = (
                    "subject_document_store" if version else "unregistered_workspace_file"
                )
                if version is not None:
                    base_payload["document_id"] = version.document_id
                    base_payload["file_ref"] = snapshot.reference
                    base_payload["version_path"] = version.logical_path.removeprefix("life_engine_workspace/")
                    base_payload["current_path"] = snapshot.head.logical_path.removeprefix("life_engine_workspace/")
                    base_payload["deleted"] = snapshot.head.deleted
                    base_payload["_continuation_pin_reserve"] = f"mfc1.{version.version_id}."
            if start_idx > 0:
                base_payload["remaining_lines_before"] = start_idx
            if end_idx < total_lines:
                base_payload["remaining_lines"] = total_lines - end_idx
                base_payload["next_offset"] = end_idx + 1

            result_data = project_bounded_text(
                projection_name="workspace-file-read",
                task_name=getattr(self, "_runtime_task_name", ""),
                requested_max_bytes=max_bytes,
                binding={
                    "path": normalized_path,
                    "offset": int(offset),
                    "limit": int(limit),
                    "from_end": bool(from_end),
                    "encoding": str(encoding),
                },
                frontier={
                    "path": normalized_path,
                    "size": source_size,
                    "mtime_ns": source_mtime,
                    "subject_version_id": version.version_id if version else "",
                    "content_sha256": file_sha256,
                },
                base_payload=base_payload,
                content=numbered_content,
                content_ref=(
                    f"workspace-file:{normalized_path}:sha256:{file_sha256}"
                ),
                continuation=inner_continuation,
            )
            if version is not None:
                result_data.pop("_continuation_pin_reserve", None)
                result_data["continuation"] = pin_file_continuation(
                    str(result_data.get("continuation") or ""), version.version_id,
                )
                _finalize_delivered_bytes(result_data)
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
        "受管已有文件必须携带读取返回的 expected_version；commit_status=committed 表示已保存，"
        "即使 projection 失败也不要改用新请求重复写入，先按 occurrence_id 查回执。\n"
        "\n"
        "**⚠️ 注意：** 如果文件已存在，其全部内容会被覆盖。"
        "修改文件的局部内容，优先使用 nucleus_edit_file。\n"
        "SOUL.md、USER.md、MEMORY.md、EXISTENCE.md 会固定进入下一轮提示词；"
        "改它们和改日记一样，由你判断。不要清空 SOUL.md，也不要删除或改名这三份身份文件。\n"
        "**💡 记忆提示：** 写入新文件后，想一想它和已有文件有没有关联？"
        "如需由当前主体明确表达关系，请使用 nucleus_relations(action=add)。"
    )
    chatter_allow: list[str] = FILE_CHATTER_ALLOW

    async def execute(
        self,
        path: Annotated[str, "相对于工作空间的文件路径"],
        content: Annotated[str, "要写入的内容"],
        encoding: Annotated[str, "文件编码，默认utf-8"] = "utf-8",
        reason: Annotated[str, "可选：这次写入/覆盖文件的原因，便于未来追溯"] = "",
        expected_version: Annotated[str, "覆盖已有文件必须原样传入最近读取返回的 expected_version；新文件可留空"] = "",
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
        standing_error = _standing_prompt_content_error(self.plugin, target, content)
        if standing_error is not None:
            return False, standing_error
        reserved = _workspace_authority_mutation_path(self.plugin, target)
        if reserved is not None:
            return False, _workspace_authority_mutation_error(*reserved)
        managed = await _execute_managed_write(
            self, target, content=content, encoding=encoding, reason=reason,
            expected_version=expected_version,
        )
        if managed is not None:
            return managed
        existed = target.exists()
        before_content = _read_trace_before_content(target, encoding)
        occurrence_id = f"file-tool:{uuid4().hex}"
        committed, commit_error = await _commit_subject_authority_file_write(
            self,
            target,
            content,
            encoding=encoding,
            reason=reason,
            occurrence_id=occurrence_id,
        )
        if not committed:
            return False, str(commit_error)

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
        "- 受管文件同时原样传 expected_version，陈旧读取会被拒绝\n"
        "- old_text 必须与文件中的内容完全一致（包括缩进），不要包含读结果里的行号前缀\n"
        "- 如果 old_text 在文件中出现多次且你只想改一处，提供更长的上下文使其唯一\n"
        "- 用 replace_all=True 可以替换所有出现位置（如重命名变量）\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 想重写整个文件 → 用 nucleus_write_file\n"
        "- ✗ 还没看过文件内容 → 先用 nucleus_read_file\n"
        "- ✗ 一次改多处或不连续片段 → 用 nucleus_apply_patch"
    )
    chatter_allow: list[str] = FILE_CHATTER_ALLOW

    async def execute(
        self,
        path: Annotated[str, "相对于工作空间的文件路径"],
        old_text: Annotated[str, "要查找的原始文本（必须与文件内容完全一致）"],
        new_text: Annotated[str, "替换后的新文本"],
        replace_all: Annotated[bool, "是否替换所有出现的位置（默认只替换第一处）"] = False,
        encoding: Annotated[str, "文件编码，默认utf-8"] = "utf-8",
        reason: Annotated[str, "可选：这次编辑文件的原因，便于未来追溯"] = "",
        expected_version: Annotated[str, "原样传入最近读取返回的 expected_version，以拒绝陈旧覆盖"] = "",
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
        reserved = _workspace_authority_mutation_path(self.plugin, target)
        if reserved is not None:
            return False, _workspace_authority_mutation_error(*reserved)

        managed = await _execute_managed_write(
            self, target, content=new_text, encoding=encoding, reason=reason,
            expected_version=expected_version, old_text=old_text,
            replace_all=replace_all,
        )
        if managed is not None:
            return managed

        try:
            version = await _read_selected_subject_file(self.plugin, target)
            if version is not None:
                content = version.content_bytes.decode(encoding)
            else:
                if not target.exists():
                    return False, f"文件不存在: {path}"
                if not target.is_file():
                    return False, f"路径不是文件: {path}"
                content = await asyncio.to_thread(target.read_text, encoding=encoding)
            search_text = old_text
            count = content.count(search_text)
            if count == 0:
                stripped = strip_read_line_prefixes(old_text)
                if stripped is not None:
                    search_text = stripped
                    count = content.count(search_text)

            if count == 0:
                return False, (
                    "未找到要替换的文本。请确认：\n"
                    "1. 是否先用 nucleus_read_file 读取了最新内容？\n"
                    "2. old_text 是否与文件内容完全一致（注意空格和缩进，不要包含行号）？"
                )

            if count > 1 and not replace_all:
                return False, (
                    f"old_text 在文件中出现了 {count} 次，无法确定要替换哪一处。\n"
                    "请提供更多上下文使 old_text 唯一，或使用 replace_all=True 替换全部。"
                )

            replacement = new_text
            if search_text != old_text:
                stripped_new = strip_read_line_prefixes(new_text)
                if stripped_new is not None:
                    replacement = stripped_new

            if replace_all:
                new_content = content.replace(search_text, replacement)
                replacements = count
            else:
                new_content = content.replace(search_text, replacement, 1)
                replacements = 1

            standing_error = _standing_prompt_content_error(
                self.plugin, target, new_content
            )
            if standing_error is not None:
                return False, standing_error
            occurrence_id = f"file-tool:{uuid4().hex}"
            committed, commit_error = await _commit_subject_authority_file_write(
                self,
                target,
                new_content,
                encoding=encoding,
                reason=reason,
                occurrence_id=occurrence_id,
            )
            if not committed:
                return False, str(commit_error)

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


class LifeEngineApplyPatchTool(BaseTool):
    """Codex-style multi-file / multi-hunk patch."""

    tool_name: str = "nucleus_apply_patch"
    tool_description: str = (
        "用 Codex apply_patch 格式一次精确改一个或多个文件（可多 hunk、可新增/删除/重命名/复制）。"
        "\n\n"
        "**何时使用：**\n"
        "- ✓ 同一文件改多处，或不连续片段\n"
        "- ✓ 一次提交里新增、更新、删除、重命名多个文件\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 只改一处连续原文 → 用 nucleus_edit_file\n"
        "- ✗ 整篇覆盖或新建一篇完整文档 → 用 nucleus_write_file\n"
        "- ✗ 删除或改名 SOUL.md / USER.md / MEMORY.md（改内容可以直接做）\n"
        "\n"
        "**格式：**\n"
        "```\n"
        "*** Begin Patch\n"
        "*** Add File: notes/new.md\n"
        "+hello\n"
        "*** Update File: notes/old.md\n"
        "@@\n"
        " context\n"
        "-old\n"
        "+new\n"
        "*** Delete File: notes/gone.md\n"
        "*** End Patch\n"
        "```\n"
        "每个 hunk 必须在文件中唯一匹配。不要把 nucleus_read_file 的行号前缀写进 patch。"
        "受管已有文件需在 expected_versions 中提供路径到读取版本的映射。"
        "*** Copy to: 与 *** Move to: 互斥；纯复制保留原作者来源并产生新文档身份。"
        "一批路径必须互不重叠；权威提交是同一事务，文件/索引投影可逐项失败，须看每项回执。"
        "受管批次最多8项。若原操作已提交而投影待恢复，可只传 recover_occurrence_id，input留空；"
        "它只重试原投影，不追加文件版本。"
        "Add File 在目标已存在时会失败，应改用 Update File。"
        "删除和重命名只通过本工具（*** Delete File / *** Move to:）。"
    )
    chatter_allow: list[str] = FILE_CHATTER_ALLOW

    async def execute(
        self,
        input: Annotated[str, "完整 patch；仅恢复原投影时必须传空字符串"],
        reason: Annotated[str, "可选：这次改写的原因，便于未来追溯"] = "",
        encoding: Annotated[str, "文件编码，默认utf-8"] = "utf-8",
        expected_versions: Annotated[dict[str, str] | None, "已有文件路径到最近读取 expected_version 的映射；新建可省略"] = None,
        recover_occurrence_id: Annotated[str, "可选：只恢复已提交操作的投影，不重复提交内容；与input互斥"] = "",
    ) -> tuple[bool, str | dict]:
        if recover_occurrence_id:
            if input.strip() or expected_versions:
                return False, "ManagedFileRecoveryCannotContainNewPatch"
            try:
                session = selected_file_session(self, _plugin_life_service(self.plugin))
                if session is None:
                    return False, "ManagedFileRecoveryRequiresSelectedStorage"
                operation = await session.store.get_document_operation(recover_occurrence_id)
                if operation is None:
                    return False, "ManagedFileCommittedOperationNotFound"
                relative = str(operation.result["logical_path"]).removeprefix("life_engine_workspace/")
                valid, guarded = _guard_workspace_mutation(self.plugin, relative)
                if not valid:
                    return False, str(guarded)
                return True, await session.recover_projection(recover_occurrence_id)
            except Exception as exc:  # noqa: BLE001 - explicit recovery failure, never a new commit
                return False, {"recovery_status": "failed", "error_type": type(exc).__name__}
        try:
            ops = parse_apply_patch(input)
        except ApplyPatchError as exc:
            return False, str(exc)

        relative_paths: list[str] = []
        for op in ops:
            relative_paths.append(op.path)
            if op.move_to:
                relative_paths.append(op.move_to)
            if op.copy_to:
                relative_paths.append(op.copy_to)

        resolved: dict[str, Path] = {}
        for relative in relative_paths:
            ok, result = _guard_workspace_mutation(self.plugin, relative)
            if not ok:
                return False, str(result)
            resolved[relative] = result

        for op in ops:
            structural = _standing_prompt_structural_error(
                self.plugin, resolved[op.path]
            )
            if op.kind == "delete" and structural is not None:
                return False, structural
            if op.move_to or op.copy_to:
                source_error = _standing_prompt_structural_error(
                    self.plugin, resolved[op.path]
                )
                dest_error = _standing_prompt_structural_error(
                    self.plugin, resolved[op.move_to or op.copy_to]
                )
                if source_error is not None or dest_error is not None:
                    return False, source_error or dest_error

        managed = await _execute_managed_patch(
            self, ops, resolved, input_text=input, reason=reason, encoding=encoding,
            expected_versions=expected_versions,
        )
        if managed is not None:
            return managed

        files: dict[str, str | None] = {}
        for relative, target in resolved.items():
            try:
                version = await _read_selected_subject_file(self.plugin, target)
                if version is not None:
                    files[relative] = version.content_bytes.decode(encoding)
                    continue
            except UnicodeDecodeError as exc:
                return False, f"文件编码错误: {exc}"
            except Exception as exc:  # noqa: BLE001 - selected authority fails closed
                return False, f"读取主体文件失败: {exc}"
            if target.exists() and not target.is_file():
                return False, f"路径不是文件: {relative}"
            if target.exists():
                try:
                    files[relative] = target.read_text(encoding=encoding)
                except UnicodeDecodeError as exc:
                    return False, f"文件编码错误: {exc}"
            else:
                files[relative] = None

        try:
            planned = apply_ops_to_contents(ops, files)
        except ApplyPatchError as exc:
            return False, str(exc)

        for item in planned:
            if item.action != "write":
                continue
            standing_error = _standing_prompt_content_error(
                self.plugin,
                resolved[item.path],
                item.content or "",
            )
            if standing_error is not None:
                return False, standing_error
            occurrence_id = f"file-tool:{uuid4().hex}"
            committed, commit_error = await _commit_subject_authority_file_write(
                self,
                resolved[item.path],
                item.content or "",
                encoding=encoding,
                reason=reason,
                occurrence_id=occurrence_id,
            )
            if not committed:
                return False, str(commit_error)

        trace_context = _tool_trace_context(self)
        writes = [item for item in planned if item.action == "write"]
        deletes = [item for item in planned if item.action == "delete"]
        try:
            for item in writes:
                target = resolved[item.path]
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(
                    target.write_text,
                    item.content or "",
                    encoding=encoding,
                )
            for item in deletes:
                target = resolved[item.path]
                if target.exists():
                    await asyncio.to_thread(target.unlink)
        except Exception as exc:
            logger.error(f"应用 patch 失败: error_type={type(exc).__name__}")
            return False, f"应用 patch 失败: {exc}"

        files_out: list[dict[str, Any]] = []
        for item in planned:
            before = files.get(item.path)
            if item.action == "write" and item.operation in {"move", "copy"}:
                before = files.get(item.source_path)
            after = item.content if item.action == "write" else ""
            trace_id = await _record_file_trace(
                self.plugin,
                path=item.path,
                before_content=before,
                after_content=after if item.action == "write" else None,
                operation=item.operation,
                tool_name=self.tool_name,
                reason=reason,
                **trace_context,
            )
            artifact_id = ""
            if item.action == "write":
                artifact_id = await _record_memory_artifact_version(
                    self.plugin,
                    path=item.path,
                    before_content=before,
                    after_content=item.content or "",
                    operation=item.operation,
                    reason=reason,
                    trace_id=trace_id,
                    **trace_context,
                )
                _notify_router_context_source_changed(self.plugin, item.path)
                await _sync_memory_embedding_for_file(
                    self.plugin, item.path, item.content or ""
                )
            entry: dict[str, Any] = {
                "path": item.path,
                "operation": item.operation,
                "trace_id": trace_id,
                "artifact_version_id": artifact_id,
            }
            if item.source_path:
                entry["source_path"] = item.source_path
            files_out.append(entry)

        return True, {
            "action": "apply_patch",
            "files": files_out,
            **({"reason": reason} if reason else {}),
        }


# 独立的 move/delete 工具不再提供。删除和重命名只走 nucleus_apply_patch。
# 只读 sandbox 中的 nucleus_bash 也不能移动或删除 workspace 文件。


class LifeEngineListFilesTool(BaseTool):
    """列出目录内容工具。"""

    tool_name: str = "nucleus_list_files"
    tool_description: str = (
        "列出目录中的文件和子目录。默认按最近修改时间倒序，所以第一页是最近动过的文件，"
        "不是档案里最早的那几篇。\n\n"
        "**何时使用：**\n"
        "- ✓ 浏览自己的文件结构\n"
        "- ✓ 确认某个目录下有什么文件（可加 glob，如 `2026-09*.md`）\n"
        "- ✓ 用 recursive=True 查看文件树\n"
        "- ✓ 需要按名字从旧到新翻档案时，传 sort=\"name\"\n"
        "\n"
        "**截断：** 目录很大时看 truncated / omitted_items / continuation，那一页不是全集。"
        "不要用 nucleus_grep_file 的 pattern=\".\" 来列目录。\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 想搜索文件内容 → 用 nucleus_grep_file\n"
        "- ✗ 只知道文件名模式、不知道在哪一层 → 用 nucleus_glob_file\n"
        "- ✗ 想看文件的大小/修改时间等 → nucleus_list_files 返回的列表已经包含这些信息"
    )
    chatter_allow: list[str] = FILE_CHATTER_ALLOW

    async def execute(
        self,
        path: Annotated[str, "相对于工作空间的目录路径，空字符串表示根目录"] = "",
        recursive: Annotated[bool, "是否递归列出子目录"] = False,
        max_depth: Annotated[int, "递归最大深度（仅recursive=True时有效）"] = 3,
        sort: Annotated[
            Literal["name", "mtime"],
            "排序：mtime=最近修改在前（默认）；name=每层目录优先、再按名字，适合翻旧档案",
        ] = "mtime",
        glob: Annotated[
            str,
            "可选通配符，逗号分隔。匹配工作区相对路径或文件名，如 '2026-09*.md'",
        ] = "",
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
        try:
            managed_session = selected_file_session(self, _plugin_life_service(self.plugin))
        except Exception as exc:
            return False, str(exc)
        if not target.exists() and managed_session is None:
            return False, f"目录不存在: {path or '(root)'}"
        if managed_session is None and target.exists() and not target.is_dir():
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
            items = list_dir(target, 1) if managed_session is None else []

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
            if managed_session is not None:
                from .managed_file_inventory import load_managed_inventory

                source_items = await load_managed_inventory(
                    managed_session, root=target,
                    max_depth=max(1, max_depth) if recursive else 1,
                    include_hidden=True,
                )
                for item in source_items:
                    item.pop("_mtime", None)
            glob_filter = str(glob or "").strip()
            if glob_filter:
                source_items = [
                    item
                    for item in source_items
                    if _list_glob_matches(
                        str(item.get("path") or ""),
                        str(item.get("name") or ""),
                        glob_filter,
                    )
                ]
            sort_mode = str(sort or "mtime").strip() or "mtime"
            if sort_mode not in {"name", "mtime"}:
                return False, f"不支持的 sort: {sort}"
            if sort_mode == "mtime":
                source_items.sort(key=lambda item: str(item.get("path") or ""))
                source_items.sort(
                    key=lambda item: str(item.get("modified_at") or ""),
                    reverse=True,
                )
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
                    "pattern": glob_filter,
                    "glob": glob_filter,
                    "sort": sort_mode,
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
                    "sort": sort_mode,
                    "glob": glob_filter,
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
        "何时不用：不要用于检查目录是否存在（用 nucleus_list_files）。写文件时若父目录不存在，write_file 也会自动创建。"
    )
    chatter_allow: list[str] = FILE_CHATTER_ALLOW

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


class LifeEngineGlobFileTool(BaseTool):
    """按 glob 查找 workspace 文件路径。"""

    tool_name: str = "nucleus_glob_file"
    tool_description: str = (
        "按 glob 查找工作区内的文件路径，默认递归，按最近修改时间倒序。"
        "\n\n"
        "**何时使用：**\n"
        "- ✓ 知道文件名模式但不知道目录（如 `**/*.md`、`diaries/2026-09*.md`）\n"
        "- ✓ 只要路径列表，不要目录浏览\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 已经知道目录、想看这一层有什么 → 用 nucleus_list_files\n"
        "- ✗ 要搜文件内容 → 用 nucleus_grep_file\n"
        "\n"
        "忽略 .git / .memory / __pycache__ / node_modules 和隐藏路径。"
    )
    chatter_allow: list[str] = FILE_CHATTER_ALLOW

    async def execute(
        self,
        pattern: Annotated[str, "glob，如 '**/*.md' 或 'notes/*.txt'，逗号分隔多个"],
        path: Annotated[str, "搜索根目录，相对 workspace，空表示整个工作区"] = "",
        continuation: Annotated[
            str,
            "Optional continuation returned by the previous glob page",
        ] = "",
        max_bytes: Annotated[
            int | None,
            "Optional result byte budget; the task hard cap still applies",
        ] = None,
    ) -> tuple[bool, str | dict]:
        glob_filter = str(pattern or "").strip()
        if not glob_filter:
            return False, "glob pattern 不能为空"

        valid, result = _resolve_path(self.plugin, path or ".")
        if not valid:
            return False, str(result)
        search_root = result
        try:
            managed_session = selected_file_session(self, _plugin_life_service(self.plugin))
        except Exception as exc:
            return False, str(exc)
        if not search_root.exists() and managed_session is None:
            return False, f"目录不存在: {path or '(root)'}"
        if managed_session is None and search_root.exists() and not search_root.is_dir():
            return False, f"路径不是目录: {path}"

        workspace = _get_workspace_read_only(self.plugin)
        source_items: list[dict[str, Any]] = []
        try:
            if managed_session is not None:
                from .managed_file_inventory import load_managed_inventory

                source_items = [
                    item for item in await load_managed_inventory(
                        managed_session, root=search_root, include_hidden=False,
                    )
                    if item["type"] == "file"
                    and _list_glob_matches(item["path"], item["name"], glob_filter)
                ]
            for root, dirs, filenames in (() if managed_session is not None else os.walk(search_root)):
                dirs[:] = [
                    name
                    for name in dirs
                    if name not in _GLOB_IGNORE_DIRS and not name.startswith(".")
                ]
                root_path = Path(root)
                for filename in filenames:
                    if filename.startswith("."):
                        continue
                    entry = root_path / filename
                    if not entry.is_file():
                        continue
                    rel_path = str(entry.relative_to(workspace))
                    if not _list_glob_matches(rel_path, filename, glob_filter):
                        continue
                    stat = entry.stat()
                    source_items.append(
                        {
                            "path": rel_path,
                            "name": filename,
                            "size": stat.st_size,
                            "size_human": _format_size(stat.st_size),
                            "modified_at": _format_time(stat.st_mtime),
                            "_mtime": stat.st_mtime,
                        }
                    )
            source_items.sort(key=lambda item: str(item.get("path") or ""))
            source_items.sort(
                key=lambda item: float(item.get("_mtime") or 0.0),
                reverse=True,
            )
            for item in source_items:
                item.pop("_mtime", None)
            item_refs = [
                f"workspace-entry:{item.get('path') or 'unknown'}:sha256:{sha256_json(item)}"
                for item in source_items
            ]
            normalized_root = str(search_root.relative_to(workspace)) or "."
            payload = project_bounded_items(
                projection_name="workspace-file-glob",
                task_name=getattr(self, "_runtime_task_name", ""),
                requested_max_bytes=max_bytes,
                binding={
                    "root": normalized_root,
                    "pattern": glob_filter,
                },
                frontier={"items_sha256": sha256_json(source_items)},
                base_payload={
                    "action": "glob_file",
                    "pattern": glob_filter,
                    "path": path or "(root)",
                    "total_items": len(source_items),
                },
                items_key="items",
                items=source_items,
                item_refs=item_refs,
                continuation=continuation,
                compact=True,
            )
            if len(str(payload).encode("utf-8")) > payload["budget_bytes"]:
                return False, "glob file projection exceeded its byte budget"
            return True, payload
        except BoundedContinuationError as exc:
            return False, f"查找文件失败: {exc}"
        except Exception as exc:
            logger.error(f"查找文件失败: error_type={type(exc).__name__}")
            return False, f"查找文件失败: {exc}"


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
        "；受管文件请同时传搜索结果的 version_ids 固定版本，超预算时按返回提示用 read_file 续读"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        file_paths: Annotated[list[str], "要读取的文件路径列表（来自 life_memory_search 的结果）"],
        max_length_per_file: Annotated[int, "兼容参数；记忆文档不再按字符数截断"] = 0,
        include_metadata: Annotated[bool, "是否包含文件元数据（大小、修改时间等）"] = True,
        version_ids: Annotated[dict[str, str] | None, "路径到精确版本ID；按搜索结果固定内容，避免改名或路径复用串读"] = None,
    ) -> tuple[bool, dict]:
        """批量读取记忆文件的完整内容。"""
        from .managed_memory_fetch import fetch_managed_memories

        managed = await fetch_managed_memories(
            self, file_paths, version_ids, include_metadata,
            service=_get_life_engine_service(self.plugin),
        )
        if managed is not None:
            return managed
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
    LifeEngineApplyPatchTool,
    LifeEngineListFilesTool,
    LifeEngineGlobFileTool,
    LifeEngineMakeDirectoryTool,
    LifeEngineRunAgentTool,
    FetchLifeMemoryTool,
]
