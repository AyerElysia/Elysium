"""日记与连续记忆服务实现。"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.app.plugin_system.base import BaseService
from src.kernel.logger import get_logger

from .config import DiaryConfig
from .prompts import (
    build_continuous_memory_compression_prompt,
    build_shared_persona_prompt,
)


logger = get_logger("diary_plugin")

_SUPPORTED_CHAT_TYPES = ("private", "group", "discuss")
_MEMORY_VERSION = 1
_MEMORY_LOCKS: dict[str, asyncio.Lock] = {}


def _now_iso() -> str:
    """返回带时区的 ISO 时间。"""

    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class DiaryEvent:
    """单条日记事件。"""

    timestamp: str
    content: str
    section: str


@dataclass
class DiaryContent:
    """日记内容结构。"""

    raw_text: str
    date: str
    events: list[DiaryEvent] = field(default_factory=list)
    sections: dict[str, list[DiaryEvent]] = field(default_factory=dict)
    exists: bool = True


@dataclass
class ContinuousMemoryEntry:
    """连续记忆原始条目。"""

    entry_id: str
    created_at: str
    diary_date: str
    section: str
    content: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContinuousMemoryEntry":
        """从字典反序列化。"""

        return cls(
            entry_id=str(data.get("entry_id", "")),
            created_at=str(data.get("created_at", "")),
            diary_date=str(data.get("diary_date", "")),
            section=str(data.get("section", "其他")),
            content=str(data.get("content", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""

        return {
            "entry_id": self.entry_id,
            "created_at": self.created_at,
            "diary_date": self.diary_date,
            "section": self.section,
            "content": self.content,
        }


@dataclass
class ContinuousMemorySummary:
    """连续记忆压缩摘要。"""

    summary_id: str
    level: int
    created_at: str
    source_ids: list[str]
    content: str

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        level: int,
    ) -> "ContinuousMemorySummary":
        """从字典反序列化。"""

        return cls(
            summary_id=str(data.get("summary_id", "")),
            level=level,
            created_at=str(data.get("created_at", "")),
            source_ids=[str(item) for item in data.get("source_ids", [])],
            content=str(data.get("content", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""

        return {
            "summary_id": self.summary_id,
            "created_at": self.created_at,
            "source_ids": list(self.source_ids),
            "content": self.content,
        }


@dataclass
class ContinuousMemory:
    """单个聊天流的连续记忆。"""

    stream_id: str
    chat_type: str
    platform: str = ""
    stream_name: str = ""
    updated_at: str = ""
    raw_entries: list[ContinuousMemoryEntry] = field(default_factory=list)
    summaries_by_level: dict[int, list[ContinuousMemorySummary]] = field(
        default_factory=dict
    )

    @classmethod
    def empty(
        cls,
        *,
        stream_id: str,
        chat_type: str,
        platform: str = "",
        stream_name: str = "",
    ) -> "ContinuousMemory":
        """创建空连续记忆。"""

        return cls(
            stream_id=stream_id,
            chat_type=chat_type,
            platform=platform,
            stream_name=stream_name,
            updated_at=_now_iso(),
            raw_entries=[],
            summaries_by_level={},
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContinuousMemory":
        """从字典反序列化。"""

        layers = data.get("layers", {})
        raw_entries = [
            ContinuousMemoryEntry.from_dict(item)
            for item in layers.get("raw", [])
            if isinstance(item, dict)
        ]
        summaries_by_level: dict[int, list[ContinuousMemorySummary]] = {}
        for key, items in layers.items():
            if key == "raw" or not isinstance(items, list):
                continue
            match = re.fullmatch(r"L(\d+)", str(key))
            if not match:
                continue
            level = int(match.group(1))
            summaries_by_level[level] = [
                ContinuousMemorySummary.from_dict(item, level=level)
                for item in items
                if isinstance(item, dict)
            ]

        return cls(
            stream_id=str(data.get("stream_id", "")),
            chat_type=str(data.get("chat_type", "private")),
            platform=str(data.get("platform", "")),
            stream_name=str(data.get("stream_name", "")),
            updated_at=str(data.get("updated_at", "")),
            raw_entries=raw_entries,
            summaries_by_level=summaries_by_level,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""

        layers: dict[str, list[dict[str, Any]]] = {
            "raw": [entry.to_dict() for entry in self.raw_entries],
        }
        for level in sorted(self.summaries_by_level):
            layers[f"L{level}"] = [
                summary.to_dict() for summary in self.summaries_by_level[level]
            ]

        return {
            "version": _MEMORY_VERSION,
            "stream_id": self.stream_id,
            "chat_type": self.chat_type,
            "platform": self.platform,
            "stream_name": self.stream_name,
            "updated_at": self.updated_at,
            "layers": layers,
        }

    def has_content(self) -> bool:
        """判断是否有任意连续记忆内容。"""

        if self.raw_entries:
            return True
        return any(items for items in self.summaries_by_level.values())


class DiaryService(BaseService):
    """日记管理服务。"""

    service_name: str = "diary_service"
    service_description: str = """
    日记管理服务，提供按天日记读写能力，并附带按聊天流隔离的连续记忆能力。

    核心功能：
    - 读取指定日期的日记
    - 追加新条目到今天的日记
    - 自动去重检查
    - 连续记忆同步与压缩
    """
    version: str = "2.0.0"

    def _cfg(self) -> DiaryConfig:
        """获取插件配置实例。"""

        cfg = self.plugin.config
        if not isinstance(cfg, DiaryConfig):
            raise RuntimeError("diary_plugin config 未正确加载")
        return cfg

    def _get_diary_base_path(self) -> Path:
        """获取日记存储根目录。"""

        return Path(self._cfg().storage.base_path)

    def _get_date_file_path(self, date: str) -> Path:
        """获取指定日期日记文件路径。"""

        try:
            date_obj = datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"日期格式错误：{date}，应为 YYYY-MM-DD") from exc

        month_dir = self._get_diary_base_path() / date_obj.strftime(
            self._cfg().storage.date_format
        )
        return month_dir / date_obj.strftime(self._cfg().storage.file_format)

    def _get_today_file_path(self) -> Path:
        """获取今天日记文件路径。"""

        today = datetime.now().strftime("%Y-%m-%d")
        return self._get_date_file_path(today)

    def _is_today(self, date: str | None = None) -> bool:
        """检查指定日期是否为今天。"""

        if date is None:
            return True

        try:
            date_obj = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return False

        return date_obj == datetime.now().date()

    def can_modify(self, date: str) -> tuple[bool, str]:
        """检查是否可以修改指定日期的日记。"""

        if not self._is_today(date):
            return False, "只能修改今天的日记，不能修改历史日记"
        return True, "可以修改"

    def read_today(self) -> DiaryContent:
        """读取今天日记全文。"""

        today = datetime.now().strftime("%Y-%m-%d")
        return self.read_date(today)

    def read_date(self, date: str) -> DiaryContent:
        """读取指定日期日记。"""

        path = self._get_date_file_path(date)
        if not path.exists():
            return DiaryContent(
                raw_text="",
                date=date,
                events=[],
                sections={"上午": [], "下午": [], "晚上": [], "其他": []},
                exists=False,
            )

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error(f"读取日记文件失败：{path} - {exc}")
            return DiaryContent(
                raw_text="",
                date=date,
                events=[],
                sections={},
                exists=False,
            )

        events = self._parse_events(content)
        sections = self._parse_sections(events)
        return DiaryContent(
            raw_text=content,
            date=date,
            events=events,
            sections=sections,
            exists=True,
        )

    def _parse_events(self, content: str) -> list[DiaryEvent]:
        """解析日记内容为事件列表。"""

        events: list[DiaryEvent] = []
        pattern = r"\*\*\[(\d{2}:\d{2})\]\*\*\s*(.+?)(?=\n\*\*\[|\Z)"

        for match in re.finditer(pattern, content, re.DOTALL):
            timestamp = match.group(1)
            text = match.group(2).strip()
            if not text:
                continue
            section = self._get_section_by_time(timestamp)
            events.append(
                DiaryEvent(
                    timestamp=timestamp,
                    content=text,
                    section=section,
                )
            )

        return events

    def _get_section_by_time(self, time_str: str) -> str:
        """根据时间判断时间段。"""

        try:
            hour = int(time_str.split(":")[0])
        except (ValueError, IndexError):
            return "其他"

        if 5 <= hour < 12:
            return "上午"
        if 12 <= hour < 18:
            return "下午"
        if 18 <= hour < 23:
            return "晚上"
        return "其他"

    def _parse_sections(self, events: list[DiaryEvent]) -> dict[str, list[DiaryEvent]]:
        """按时间段组织事件。"""

        sections: dict[str, list[DiaryEvent]] = {
            "上午": [],
            "下午": [],
            "晚上": [],
            "其他": [],
        }
        for event in events:
            if event.section in sections:
                sections[event.section].append(event)
        return sections

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算两个文本的 Jaccard 相似度。"""

        set1 = set(text1.lower())
        set2 = set(text2.lower())
        if not set1 or not set2:
            return 0.0
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0

    def _is_duplicate(
        self,
        new_content: str,
        existing_events: list[DiaryEvent],
        threshold: float | None = None,
    ) -> tuple[bool, str | None]:
        """检查新日记内容是否重复。"""

        dedup_cfg = self._cfg().dedup
        if not dedup_cfg.enabled:
            return False, None

        if threshold is None:
            threshold = dedup_cfg.similarity_threshold

        if len(new_content.strip()) < dedup_cfg.min_content_length:
            return False, None

        new_content_lower = new_content.lower().strip()
        for event in existing_events:
            event_content = event.content.lower().strip()
            if new_content_lower in event_content or event_content in new_content_lower:
                return True, event.content

            similarity = self._calculate_similarity(new_content_lower, event_content)
            if similarity > threshold:
                return True, event.content

        return False, None

    def append_entry(
        self,
        content: str,
        section: str = "其他",
        date: str | None = None,
    ) -> tuple[bool, str]:
        """追加日记条目。"""

        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        can_modify, reason = self.can_modify(date)
        if not can_modify:
            return False, reason

        today_content = self.read_today()
        is_dup, similar_content = self._is_duplicate(content, today_content.events)
        if is_dup:
            return False, f"今天已经记录过类似内容了：{similar_content}"

        path = self._get_today_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime(self._cfg().format.time_format)
        entry = f"\n**[{timestamp}]** {content}\n"

        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(entry)
        except Exception as exc:
            logger.error(f"写入日记失败：{exc}")
            return False, f"写入失败：{exc}"

        logger.info(f"日记已更新 [{section}] {timestamp}")
        return True, f"日记已更新 [{section}]"

    def get_today_summary(self) -> dict[str, Any]:
        """获取今天日记摘要。"""

        today_content = self.read_today()
        events_data = [
            {
                "timestamp": event.timestamp,
                "content": event.content,
                "section": event.section,
            }
            for event in today_content.events
        ]
        sections_summary = {
            section: [event.content for event in events]
            for section, events in today_content.sections.items()
        }
        return {
            "date": today_content.date,
            "exists": today_content.exists,
            "event_count": len(today_content.events),
            "events": events_data,
            "sections": sections_summary,
            "raw_text": today_content.raw_text,
        }

    def _normalize_chat_type(self, chat_type: str | None) -> str:
        """规范化聊天类型。"""

        raw = str(chat_type or "").lower()
        if raw in _SUPPORTED_CHAT_TYPES:
            return raw
        if raw == "guild":
            return "group"
        return "private"

    def _get_continuous_memory_base_path(self) -> Path:
        """获取连续记忆根目录。"""

        return Path(self._cfg().continuous_memory.base_path)

    def _get_continuous_memory_subdir(self, chat_type: str) -> str:
        """获取聊天类型对应的连续记忆子目录。"""

        cfg = self._cfg().continuous_memory
        normalized = self._normalize_chat_type(chat_type)
        if normalized == "group":
            return cfg.group_subdir
        if normalized == "discuss":
            return cfg.discuss_subdir
        return cfg.private_subdir

    def _get_continuous_memory_path(self, stream_id: str, chat_type: str) -> Path:
        """获取指定聊天流的连续记忆文件路径。"""

        path = (
            self._get_continuous_memory_base_path()
            / self._get_continuous_memory_subdir(chat_type)
            / f"{stream_id}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _find_existing_continuous_memory_path(
        self,
        stream_id: str,
    ) -> tuple[str, Path] | None:
        """查找已存在的连续记忆文件。"""

        for chat_type in _SUPPORTED_CHAT_TYPES:
            path = self._get_continuous_memory_path(stream_id, chat_type)
            if path.exists():
                return chat_type, path
        return None

    def _load_continuous_memory_from_path(
        self,
        path: Path,
        *,
        stream_id: str,
        chat_type: str,
        platform: str = "",
        stream_name: str = "",
    ) -> ContinuousMemory:
        """从路径加载连续记忆。"""

        if not path.exists():
            return ContinuousMemory.empty(
                stream_id=stream_id,
                chat_type=self._normalize_chat_type(chat_type),
                platform=platform,
                stream_name=stream_name,
            )

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(f"读取连续记忆失败：{path} - {exc}")
            return ContinuousMemory.empty(
                stream_id=stream_id,
                chat_type=self._normalize_chat_type(chat_type),
                platform=platform,
                stream_name=stream_name,
            )

        memory = ContinuousMemory.from_dict(data)
        if not memory.stream_id:
            memory.stream_id = stream_id
        if not memory.chat_type:
            memory.chat_type = self._normalize_chat_type(chat_type)
        if platform and not memory.platform:
            memory.platform = platform
        if stream_name and not memory.stream_name:
            memory.stream_name = stream_name
        return memory

    def get_continuous_memory(
        self,
        stream_id: str,
        chat_type: str | None = None,
        *,
        platform: str = "",
        stream_name: str = "",
    ) -> ContinuousMemory:
        """读取指定聊天流的连续记忆。"""

        normalized = self._normalize_chat_type(chat_type)
        if chat_type is None:
            found = self._find_existing_continuous_memory_path(stream_id)
            if found is not None:
                normalized, path = found
                return self._load_continuous_memory_from_path(
                    path,
                    stream_id=stream_id,
                    chat_type=normalized,
                    platform=platform,
                    stream_name=stream_name,
                )

        path = self._get_continuous_memory_path(stream_id, normalized)
        return self._load_continuous_memory_from_path(
            path,
            stream_id=stream_id,
            chat_type=normalized,
            platform=platform,
            stream_name=stream_name,
        )

    def _save_continuous_memory(self, memory: ContinuousMemory) -> None:
        """保存连续记忆。"""

        self._enforce_continuous_memory_top_level_limit(memory)
        memory.updated_at = _now_iso()
        path = self._get_continuous_memory_path(memory.stream_id, memory.chat_type)
        path.write_text(
            json.dumps(memory.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _enforce_continuous_memory_top_level_limit(
        self,
        memory: ContinuousMemory,
    ) -> bool:
        """按配置裁剪最高压缩层条目数，返回是否发生变更。"""

        max_items = max(0, int(self._cfg().continuous_memory.max_items_top_level))
        if max_items <= 0:
            return False

        top_level = max(1, int(self._cfg().continuous_memory.max_levels))
        top_summaries = memory.summaries_by_level.get(top_level)
        if not top_summaries or len(top_summaries) <= max_items:
            return False

        overflow = len(top_summaries) - max_items
        del top_summaries[:overflow]
        logger.debug(
            f"[{memory.stream_id[:8]}] L{top_level} 超限，已裁剪 {overflow} 条最旧摘要"
        )
        return True

    def _get_lock(self, stream_id: str) -> asyncio.Lock:
        """按聊天流获取连续记忆锁。"""

        lock = _MEMORY_LOCKS.get(stream_id)
        if lock is None:
            lock = asyncio.Lock()
            _MEMORY_LOCKS[stream_id] = lock
        return lock

    def _trim_runtime_history_messages_on_memory_update(self, stream_id: str) -> int:
        """连续记忆更新后，裁剪聊天流历史中的最旧消息。"""

        trim_count = max(
            0,
            int(self._cfg().continuous_memory.payload_history_trim_count_on_update),
        )
        if trim_count <= 0:
            return 0

        try:
            from src.core.managers import get_stream_manager

            stream = get_stream_manager()._streams.get(stream_id)
            if stream is None:
                return 0

            context = getattr(stream, "context", None)
            if context is None:
                return 0

            history_messages = getattr(context, "history_messages", None)
            if not isinstance(history_messages, list) or not history_messages:
                return 0

            # 仅在“可裁剪空间”足够时裁剪，避免小窗口被一次清空。
            removable = max(0, len(history_messages) - trim_count)
            if removable <= 0:
                return 0

            dropped = min(trim_count, removable)
            del history_messages[:dropped]
            return dropped
        except Exception as exc:
            logger.debug(f"[{stream_id[:8]}] 裁剪历史消息失败：{exc}")
            return 0

    async def _call_llm_for_continuous_memory_compression(
        self,
        *,
        source_texts: list[str],
        target_level: int,
        chat_type: str = "private",
        platform: str = "",
        stream_name: str = "",
        bot_id: str = "",
        bot_nickname: str = "",
    ) -> str:
        """调用 LLM 压缩连续记忆。"""

        from src.core.config import get_model_config
        from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text

        cfg = self._cfg()
        task_name = (
            cfg.continuous_memory.compression_model_task.strip()
            or cfg.model.task_name
        )

        try:
            model_set = get_model_config().get_task(task_name)
        except KeyError:
            logger.warning(f"未找到连续记忆压缩模型：{task_name}")
            return ""

        if not model_set:
            return ""

        batch_text = "\n".join(f"- {item}" for item in source_texts if item.strip())
        request = LLMRequest(model_set, "continuous_memory_compression")
        shared_persona_prompt = ""
        if cfg.plugin.inherit_default_chatter_persona_prompt:
            shared_persona_prompt = await build_shared_persona_prompt(
                platform=platform,
                chat_type=chat_type,
                bot_nickname=bot_nickname,
                bot_id=bot_id,
            )
        request.add_payload(
            LLMPayload(
                ROLE.SYSTEM,
                Text(
                    build_continuous_memory_compression_prompt(
                        target_level,
                        shared_persona_prompt=shared_persona_prompt,
                        strict_identity_name_lock=cfg.plugin.strict_identity_name_lock,
                    )
                ),
            )
        )
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(f"请压缩以下自动写出的日记项：\n\n{batch_text}"),
            )
        )

        try:
            response = await request.send()
            result = await response if not response.message else response.message
        except Exception as exc:
            logger.error(f"连续记忆压缩失败：{exc}")
            return ""

        if not result:
            return ""
        return str(result).strip().replace("**", "").replace("*", "")

    def _get_summary_items(
        self,
        memory: ContinuousMemory,
        *,
        level: int,
    ) -> list[ContinuousMemorySummary]:
        """获取某一层摘要列表。"""

        return memory.summaries_by_level.setdefault(level, [])

    async def _compress_one_batch(
        self,
        memory: ContinuousMemory,
        *,
        source_level: int,
        target_level: int,
        bot_id: str = "",
        bot_nickname: str = "",
    ) -> bool:
        """压缩一批连续记忆。"""

        batch_size = max(1, int(self._cfg().continuous_memory.batch_size))
        if source_level == 0:
            if len(memory.raw_entries) < batch_size:
                return False
            batch_entries = list(memory.raw_entries[:batch_size])
            source_texts = [entry.content for entry in batch_entries]
            source_ids = [entry.entry_id for entry in batch_entries]
        else:
            summaries = self._get_summary_items(memory, level=source_level)
            if len(summaries) < batch_size:
                return False
            batch_summaries = list(summaries[:batch_size])
            source_texts = [summary.content for summary in batch_summaries]
            source_ids = [summary.summary_id for summary in batch_summaries]

        call_kwargs: dict[str, Any] = {
            "source_texts": source_texts,
            "target_level": target_level,
        }
        if (
            memory.chat_type != "private"
            or memory.platform
            or memory.stream_name
            or bot_id
            or bot_nickname
        ):
            call_kwargs.update(
                {
                    "chat_type": memory.chat_type,
                    "platform": memory.platform,
                    "stream_name": memory.stream_name,
                    "bot_id": bot_id,
                    "bot_nickname": bot_nickname,
                }
            )

        compressed = await self._call_llm_for_continuous_memory_compression(
            **call_kwargs,
        )
        if not compressed:
            return False

        if source_level == 0:
            del memory.raw_entries[:batch_size]
        else:
            del memory.summaries_by_level[source_level][:batch_size]

        summary = ContinuousMemorySummary(
            summary_id=f"l{target_level}_{uuid4().hex}",
            level=target_level,
            created_at=_now_iso(),
            source_ids=source_ids,
            content=compressed,
        )
        memory.summaries_by_level.setdefault(target_level, []).append(summary)
        logger.info(
            f"[{memory.stream_id[:8]}] 连续记忆压缩完成: "
            f"{'raw' if source_level == 0 else f'L{source_level}'} -> L{target_level}"
        )
        return True

    async def _cascade_compress_continuous_memory(
        self,
        memory: ContinuousMemory,
        *,
        bot_id: str = "",
        bot_nickname: str = "",
    ) -> bool:
        """执行级联压缩。"""

        changed = False
        max_levels = max(1, int(self._cfg().continuous_memory.max_levels))
        progress = True

        while progress:
            progress = False
            for source_level in range(0, max_levels):
                target_level = source_level + 1
                if target_level > max_levels:
                    break
                compressed = await self._compress_one_batch(
                    memory,
                    source_level=source_level,
                    target_level=target_level,
                    bot_id=bot_id,
                    bot_nickname=bot_nickname,
                )
                if compressed:
                    changed = True
                    progress = True
                    break

        return changed

    async def append_continuous_memory_entry(
        self,
        stream_id: str,
        chat_type: str,
        content: str,
        *,
        section: str = "其他",
        platform: str = "",
        stream_name: str = "",
        bot_id: str = "",
        bot_nickname: str = "",
        diary_date: str | None = None,
    ) -> tuple[bool, str]:
        """向连续记忆空间追加一条新原始记忆。

        该方法专门用于同步“自动写出的日记项”。
        """

        cfg = self._cfg().continuous_memory
        if not cfg.enabled:
            return False, "连续记忆功能未启用"

        normalized_content = content.strip()
        if not normalized_content:
            return False, "连续记忆内容不能为空"

        normalized_chat_type = self._normalize_chat_type(chat_type)
        async with self._get_lock(stream_id):
            memory = self.get_continuous_memory(
                stream_id,
                normalized_chat_type,
                platform=platform,
                stream_name=stream_name,
            )
            memory.chat_type = normalized_chat_type
            if platform:
                memory.platform = platform
            if stream_name:
                memory.stream_name = stream_name

            entry = ContinuousMemoryEntry(
                entry_id=f"raw_{uuid4().hex}",
                created_at=_now_iso(),
                diary_date=diary_date or datetime.now().strftime("%Y-%m-%d"),
                section=section,
                content=normalized_content,
            )
            memory.raw_entries.append(entry)
            await self._cascade_compress_continuous_memory(
                memory,
                bot_id=bot_id,
                bot_nickname=bot_nickname,
            )
            self._save_continuous_memory(memory)
        dropped = self._trim_runtime_history_messages_on_memory_update(stream_id)

        logger.info(
            f"[{stream_id[:8]}] 连续记忆原始条目已同步，历史裁剪={dropped}"
        )
        return True, "连续记忆已同步"

    def _format_continuous_memory_time(self, iso_time: str) -> str:
        """按配置格式化连续记忆时间。"""

        try:
            return datetime.fromisoformat(iso_time).strftime("%m-%d %H:%M")
        except ValueError:
            return iso_time

    def render_continuous_memory_for_prompt(
        self,
        stream_id: str,
        chat_type: str | None = None,
    ) -> str:
        """渲染连续记忆为 prompt 注入块。"""

        memory = self.get_continuous_memory(stream_id, chat_type)
        if not memory.has_content():
            return ""

        cfg = self._cfg().continuous_memory
        lines = [
            "## 连续记忆",
            "",
            "以下内容来自当前聊天流的连续记忆空间，请把它视为你已经记住的上下文。",
        ]

        for level in sorted(memory.summaries_by_level.keys(), reverse=True):
            summaries = memory.summaries_by_level[level]
            if not summaries:
                continue
            lines.extend(["", f"### 压缩记忆・L{level}"])
            for summary in summaries[-cfg.summary_limit_per_level :]:
                lines.append(f"- {summary.content}")

        raw_entries = memory.raw_entries[-cfg.recent_entry_limit :]
        if cfg.include_recent_entries_in_prompt and raw_entries:
            lines.extend(["", "### 近期详细记忆"])
            for entry in raw_entries:
                lines.append(
                    f"- [{self._format_continuous_memory_time(entry.created_at)}] {entry.content}"
                )

        return "\n".join(lines).strip()

    def get_continuous_memory_summary(
        self,
        stream_id: str,
        chat_type: str | None = None,
    ) -> dict[str, Any]:
        """获取连续记忆摘要。"""

        memory = self.get_continuous_memory(stream_id, chat_type)
        return {
            "stream_id": memory.stream_id,
            "chat_type": memory.chat_type,
            "platform": memory.platform,
            "stream_name": memory.stream_name,
            "updated_at": memory.updated_at,
            "raw_count": len(memory.raw_entries),
            "levels": {
                f"L{level}": len(items)
                for level, items in sorted(memory.summaries_by_level.items())
            },
            "prompt_text": self.render_continuous_memory_for_prompt(stream_id, chat_type),
        }
