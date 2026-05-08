"""life_engine 中枢文件系统工具集。

为生命中枢提供限定在 workspace 内的文件系统操作能力。
所有操作都限制在配置的 workspace_path 目录下，确保安全。

设计理念（参考 Claude Code）：
- 每个工具的描述都是一段使用指南，包含「何时用」和「何时不用」
- 工具返回值精练，避免冗余字段淹没上下文
- 先读后改，操作前确认
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from src.core.components import BaseTool
from src.app.plugin_system.api import log_api
from src.core.models.message import Message, MessageType

from ..constants import (
    EXTERNAL_MESSAGE_ACTIVE_WINDOW_MINUTES,
    PROACTIVE_WAKE_MIN_REASON_CHARS,
    PROACTIVE_WAKE_MIN_SEGMENTS,
    PROACTIVE_WAKE_REQUIRED_IMPORTANCE,
    PROACTIVE_WAKE_KEYWORDS,
)
from ..memory.prompting import build_memory_write_warning
from ._utils import (
    _get_workspace,
    _resolve_path,
    _load_life_context_events,
    _pick_latest_target_stream_id,
)


logger = log_api.get_logger("life_engine.tools")

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


async def _sync_memory_embedding_for_file(plugin: Any, path: str, content: str) -> None:
    """同步文件内容到记忆系统（公共函数，消除重复）。"""
    try:
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        if service and service._memory_service:
            await service._memory_service.sync_embedding(path, content)
    except Exception as e:
        logger.warning(f"同步记忆 embedding 失败 {path}: {e}")


def _is_detailed_proactive_wake_reason(reason: str) -> bool:
    """检查主动唤醒理由是否足够明确和详尽。"""
    text = " ".join(str(reason or "").split())
    if len(text) < PROACTIVE_WAKE_MIN_REASON_CHARS:
        return False
    segments = [seg.strip() for seg in re.split(r"[。！？!?\n；;]", text) if seg.strip()]
    if len(segments) < PROACTIVE_WAKE_MIN_SEGMENTS:
        return False
    return any(keyword in text for keyword in PROACTIVE_WAKE_KEYWORDS)


class LifeEngineWakeDFCTool(BaseTool):
    """向同一主体的表达层补充信息差。"""

    tool_name: str = "nucleus_tell_dfc"
    tool_description: str = (
        "向同一主体的表达层补充信息差——补一段它当前暂时看不到、但对接下来对话可能重要的背景。"
        "\n\n"
        "**这不是在遥控社交态，更不是替它写台词；它不用于指导表达层怎么回复。**"
        " 这不是在和另一个意识体对话，而是同一个你在不同运行状态之间补充上下文。"
        "\n\n"
        "**何时使用：**\n"
        "- 你掌握了表达层当前可能不知道的事实、背景、记忆线索、情绪来源或潜在风险\n"
        "- 你发现某段近期事件能解释对方现在为什么这样说/这样做\n"
        "- 你需要补一个隐藏前提，避免表达层误解上下文\n"
        "\n"
        "**何时不用：**\n"
        "- 你想教表达层该怎么回、该怎么问、该怎么安慰\n"
        "- 你在替表达层写台词、步骤或策略\n"
        "- 你自己其实想直接开口 → 交给表达层在正常对话里处理\n"
        "\n"
        "**注意：** 默认只入队，不主动唤醒表达层。补充内容会在表达层下次处理对话时作为背景自然被看见。"
        " 写法尽量是观察/背景/风险/线索，不要写命令句或示范回复。"
        "\n\n"
        "**参数写法建议：**\n"
        "- `message`: 只写信息差本身：你发现了什么、这说明什么、可能影响什么\n"
        "- `reason`: 为什么这是表达层当前可能不知道、但值得补充的信息差\n"
        "- `importance`: 常规用 normal；只有紧急时用 high/critical\n"
        "- `proactive_wake`: 默认 false。仅在 high/critical 且 reason 详尽时允许 true\n"
        "- `stream_id`: 不确定就留空，让系统自动路由\n"
        "\n"
        "**记住：补背景，不下指导。**"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        message: Annotated[str, "要补充给表达层的信息差：事实/背景/线索/风险，不要写指导台词"],
        reason: Annotated[
            str,
            "为什么这是表达层当前可能不知道、但值得补充的信息差",
        ] = "",
        importance: Annotated[str, "重要度（可选：low/normal/high/critical，默认 normal）"] = "normal",
        proactive_wake: Annotated[
            bool,
            "是否主动唤醒表达层立即响应。默认 false。仅在 high/critical 且 reason 明确详尽时允许 true。",
        ] = False,
        stream_id: Annotated[str, "目标聊天流ID（可选，不填则自动选择最近活跃的外部对话流）"] = "",
    ) -> tuple[bool, str | dict]:
        # 记录工具调用参数，方便调试
        logger.info(
            f"[nucleus_tell_dfc] Life 调用表达层同步工具:\n"
            f"  message: {message}\n"
            f"  reason: {reason}\n"
            f"  importance: {importance}\n"
            f"  proactive_wake: {proactive_wake}\n"
            f"  stream_id: {stream_id}"
        )

        text = str(message or "").strip()
        if not text:
            return False, "message 不能为空"

        normalized_importance = str(importance or "normal").strip().lower() or "normal"
        if normalized_importance not in {"low", "normal", "high", "critical"}:
            return False, "importance 仅支持 low/normal/high/critical"

        if proactive_wake:
            if normalized_importance not in PROACTIVE_WAKE_REQUIRED_IMPORTANCE:
                return False, "proactive_wake=true 仅允许在 high/critical 使用，平时请保持关闭。"
            if not _is_detailed_proactive_wake_reason(reason):
                return (
                    False,
                    f"proactive_wake=true 必须提供明确详尽的 reason（至少 {PROACTIVE_WAKE_MIN_REASON_CHARS} 字，且需包含信息差与影响说明）。",
                )

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
                    logger.info(
                        f"当前对话流正在活跃（{minutes_since_external} 分钟前有消息），"
                        f"同步可能会打扰表达层的正常对话节奏，但仍然允许执行。"
                    )

        target_stream_id = str(stream_id or "").strip()
        if not target_stream_id:
            target_stream_id = _pick_latest_target_stream_id(self.plugin) or ""
        if not target_stream_id:
            return (
                False,
                "没有可用的目标聊天流。可能暂时没有外部对话活动。稍后有新消息时，表达层会自然处理，你无需担心。",
            )

        try:
            from src.core.managers.stream_manager import get_stream_manager
            from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager
        except Exception as e:  # noqa: BLE001
            return False, f"加载核心管理器失败: {e}"

        stream_manager = get_stream_manager()
        chat_stream = await stream_manager.get_or_create_stream(stream_id=target_stream_id)
        if chat_stream is None:
            return False, f"找不到目标聊天流: {target_stream_id}"

        target_extra: dict[str, Any] = {}
        try:
            stream_info = await stream_manager.get_stream_info(chat_stream.stream_id)
        except Exception:
            stream_info = None

        if str(chat_stream.chat_type or "").lower() == "group":
            group_id = ""
            group_name = ""
            if stream_info:
                group_id = str(stream_info.get("group_id") or "").strip()
                group_name = str(stream_info.get("group_name") or "").strip()
            if group_id:
                target_extra["target_group_id"] = group_id
            if group_name:
                target_extra["target_group_name"] = group_name
        else:
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

        chat_stream.context.add_unread_message(trigger_message)

        wake_triggered = False
        if proactive_wake:
            try:
                wake_triggered = bool(
                    await get_stream_loop_manager().start_stream_loop(chat_stream.stream_id)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"主动唤醒表达层失败，将保留未读内在消息: {exc}")
                return False, f"已写入内在队列，但主动唤醒失败: {exc}"
            if not wake_triggered:
                return False, "已写入内在队列，但主动唤醒失败：start_stream_loop 返回 false"

        # 记录传话时间
        if life_service:
            life_service.record_tell_dfc()

        logger.info(
            "中枢向内在状态池沉淀了想法碎片: "
            f"stream_id={chat_stream.stream_id} "
            f"importance={normalized_importance} "
            f"reason={reason or '未说明'} "
        )

        note = "已补充到同一主体的表达层待处理队列。表达层会自行判断是否吸收；这不是指令。"
        if proactive_wake:
            note = "已补充并主动唤醒同一主体的表达层。请仅在存在高优先级信息差且必须即时介入时使用此模式。"

        result = {
            "action": "message_to_dfc",
            "stream_id": chat_stream.stream_id,
            "platform": chat_stream.platform,
            "chat_type": chat_stream.chat_type,
            "importance": normalized_importance,
            "reason": reason,
            "message": text,
            "proactive_wake": proactive_wake,
            "wake_triggered": wake_triggered,
            "note": note,
        }

        # 记录工具返回结果，方便调试
        logger.info(
            f"[nucleus_tell_dfc] 工具返回结果:\n"
            f"  stream_id: {result['stream_id']}\n"
            f"  platform: {result['platform']}\n"
            f"  chat_type: {result['chat_type']}\n"
            f"  importance: {result['importance']}\n"
            f"  reason: {result['reason']}\n"
            f"  message: {result['message']}\n"
            f"  proactive_wake: {result['proactive_wake']}\n"
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

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding=encoding)
            stat = target.stat()

            # 触发记忆系统同步 embedding
            await _sync_memory_embedding_for_file(self.plugin, path, content)

            return True, {
                "action": "write_file",
                "path": path,
                "size_human": _format_size(stat.st_size),
                "created": not existed,
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

            target.write_text(new_content, encoding=encoding)

            # 触发记忆系统同步 embedding
            await _sync_memory_embedding_for_file(self.plugin, path, new_content)

            return True, {
                "action": "edit_file",
                "path": path,
                "replacements": replacements,
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
            target.mkdir(parents=parents, exist_ok=True)
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
        "启动一个子代理来处理复杂的多步骤任务。子代理在独立的上下文中运行。"
        "\n\n"
        "**何时使用：**\n"
        "- ✓ 需要多次文件操作的复杂任务（如整理笔记、批量修改）\n"
        "- ✓ 需要多步推理的分析任务（如总结一段时间的变化）\n"
        "- ✓ 需要专注执行的独立任务（如写一篇日记）\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 单个简单的文件操作 → 直接用对应工具\n"
        "- ✗ 只是想问一个问题或做简单计算 → 自己思考\n"
        "\n"
        "**写任务简报的原则（重要！）：**\n"
        "像向刚进门的聪明同事简报一样写 task：\n"
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
            from ..agents.coordinator import AgentCoordinator

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
        if not hasattr(self.plugin, "_agent_coordinator"):
            from ..agents.coordinator import AgentCoordinator
            self.plugin._agent_coordinator = AgentCoordinator(self.plugin)
        return self.plugin._agent_coordinator


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
        life_service = _get_life_engine_service(self.plugin)
        memory_service = getattr(life_service, "_memory_service", None) if life_service else None

        for file_path_str in file_paths:
            file_path_str = str(file_path_str or "").strip()
            if not file_path_str:
                files_data.append({"path": "", "error": "路径为空"})
                failed += 1
                continue
            requested_path_str = file_path_str
            path_resolution: dict[str, Any] | None = None

            # 验证路径安全性
            ok, resolved = _resolve_path(self.plugin, file_path_str)
            if not ok:
                files_data.append({"path": file_path_str, "error": str(resolved)})
                failed += 1
                continue

            target_path = resolved
            if not target_path.exists():
                if memory_service is not None and hasattr(memory_service, "resolve_canonical_path"):
                    try:
                        resolution = await memory_service.resolve_canonical_path(file_path_str)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"解析记忆旧路径失败 {file_path_str}: {exc}")
                        resolution = None
                    if resolution and resolution.get("resolved"):
                        resolved_path_str = str(resolution.get("resolved_path") or "").strip()
                        ok, resolved_target = _resolve_path(self.plugin, resolved_path_str)
                        if ok and resolved_target.exists() and resolved_target.is_file():
                            target_path = resolved_target
                            file_path_str = resolved_path_str
                            path_resolution = resolution

                if not target_path.exists():
                    error_data = {"path": requested_path_str, "error": "文件不存在"}
                    if path_resolution:
                        error_data["path_resolution"] = path_resolution
                    files_data.append(error_data)
                    failed += 1
                    continue

            if requested_path_str != file_path_str and path_resolution is None:
                path_resolution = {
                    "requested_path": requested_path_str,
                    "resolved_path": file_path_str,
                    "resolved": True,
                    "note": "请求路径已解析到当前文件",
                }

            if not target_path.exists():
                files_data.append({"path": file_path_str, "error": "文件不存在"})
                failed += 1
                continue

            if not target_path.is_file():
                files_data.append({"path": file_path_str, "error": "不是文件"})
                failed += 1
                continue

            # 读取文件内容
            try:
                # 检查文件大小，防止内存溢出
                stat = target_path.stat()
                MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
                if stat.st_size > MAX_FILE_SIZE:
                    files_data.append({
                        "path": file_path_str,
                        "error": f"文件过大 ({stat.st_size / 1024 / 1024:.1f}MB)，超过限制 ({MAX_FILE_SIZE / 1024 / 1024:.1f}MB)"
                    })
                    failed += 1
                    continue

                # 如果设置了长度限制，只读取需要的部分
                truncated = False
                if max_length_per_file > 0:
                    with target_path.open('r', encoding='utf-8', errors='replace') as f:
                        content = f.read(max_length_per_file + 1)
                        truncated = len(content) > max_length_per_file
                        if truncated:
                            content = content[:max_length_per_file] + "\n\n... (内容过长，已截断)"
                else:
                    content = target_path.read_text(encoding="utf-8", errors='replace')

                file_data = {
                    "path": file_path_str,
                    "title": target_path.stem,
                    "content": content,
                    "truncated": truncated,
                }
                if requested_path_str != file_path_str:
                    file_data["requested_path"] = requested_path_str
                if path_resolution:
                    file_data["path_resolution"] = path_resolution

                # 添加元数据
                if include_metadata:
                    size = stat.st_size
                    for unit in ["B", "KB", "MB", "GB"]:
                        if size < 1024:
                            size_str = f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
                            break
                        size /= 1024
                    else:
                        # 防御性编程：处理超大文件（>1TB）
                        size_str = f"{size:.1f}TB"

                    now = time.time()
                    days_ago = int((now - stat.st_mtime) / 86400)
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
                        "size": size_str,
                        "modified": time_ago,
                        "ext": target_path.suffix or "(无扩展名)",
                    }

                files_data.append(file_data)
                successful += 1

            except Exception as e:
                files_data.append({"path": file_path_str, "error": f"读取失败: {e}"})
                failed += 1

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
    LifeEngineMakeDirectoryTool,
    LifeEngineWakeDFCTool,
    LifeEngineRunAgentTool,
    FetchLifeMemoryTool,
]
