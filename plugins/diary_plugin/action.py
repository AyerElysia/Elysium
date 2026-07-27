"""写日记动作实现。

写日记前强制先读取已有内容，防止重复和保持连贯。
"""

from __future__ import annotations

from typing import Annotated, Literal

from src.app.plugin_system.base import BaseAction
from src.kernel.logger import get_logger

from .service import DiaryService
from .tool import ReadDiaryTool


logger = get_logger("diary_plugin")


class WriteDiaryAction(BaseAction):
    """写日记动作。"""

    action_name: str = "write_diary"
    action_description: str = """
    用第一人称写下日记，记录今天发生的事情和你的感受。

    重要规则：
    1. 写之前必须先调用 read_diary(date="today") 读取已有内容
    2. 只能写今天的日记，不能修改历史
    3. 系统会自动检查是否与已有内容重复

    参数：
    - content: 日记内容，使用第一人称"我"
    - section: 时间段（上午/下午/晚上/其他）
    - mood: 心情标签（可选）
    - model_task: 指定使用的任务模型名称（如 "diary"），为空时使用默认模型

    日记会自动保存到 data/diaries/ 目录。
    """

    async def execute(
        self,
        content: Annotated[
            str,
            "日记内容，使用第一人称描述今天发生的事情和感受",
        ],
        section: Annotated[
            Literal["上午", "下午", "晚上", "其他"],
            "时间段分类，根据当前时间选择",
        ] = "其他",
        mood: Annotated[
            str | None,
            "心情标签（可选），如：开心、平静、兴奋、疲惫",
        ] = None,
        model_task: Annotated[
            str | None,
            "指定写日记使用的任务模型名称（对应 model.toml 中的 [model_tasks.xxx]），为空时使用默认配置",
        ] = None,
    ) -> tuple[bool, str]:
        """执行写日记。"""

        if not content or not content.strip():
            return False, "日记内容不能为空"

        content = content.strip()

        read_ok, read_result = await self._read_today()
        if not read_ok:
            return False, f"读取今天日记失败：{read_result}"

        existing_events = self._parse_events_from_result(read_result)
        dedup_result = self._check_duplicate(content, existing_events)
        if dedup_result["is_duplicate"]:
            similar = dedup_result.get("similar_content", "")
            return False, f"今天已经记录过类似内容了：{similar}"

        service = self._get_service()
        if service is None:
            return False, "diary_service 未加载"

        success, message = service.append_entry(
            content=content,
            section=section,
        )

        if success:
            if mood:
                message = f"{message} [心情：{mood}]"
            logger.info(f"日记已写入：{content[:50]}...")
        else:
            logger.warning(f"日记写入失败：{message}")

        return success, message

    async def _read_today(self) -> tuple[bool, dict | str]:
        """读取今天日记。"""

        service = self._get_service()
        if service is None:
            return False, "diary_service 未加载"

        try:
            summary = service.get_today_summary()
            return True, summary
        except Exception as exc:
            logger.error(f"读取日记失败：{exc}")
            return False, str(exc)

    def _parse_events_from_result(self, result: dict | str) -> list[dict]:
        """从 read_diary 返回结果中解析事件列表。"""

        if isinstance(result, str):
            return []
        if not isinstance(result, dict):
            return []

        events = result.get("events", [])
        if isinstance(events, list):
            return events

        raw_text = result.get("raw_text", "")
        if raw_text:
            return self._parse_events_from_text(raw_text)

        return []

    def _parse_events_from_text(self, text: str) -> list[dict]:
        """从日记文本中解析事件。"""

        import re

        events = []
        pattern = r"\*\*\[(\d{2}:\d{2})\]\*\*\s*(.+?)(?=\n\*\*\[|\Z)"

        for match in re.finditer(pattern, text, re.DOTALL):
            events.append(
                {
                    "timestamp": match.group(1),
                    "content": match.group(2).strip(),
                }
            )

        return events

    def _check_duplicate(
        self,
        content: str,
        existing_events: list[dict],
    ) -> dict:
        """检查内容是否重复。"""

        content_lower = content.lower().strip()
        if len(content_lower) < 5:
            return {"is_duplicate": False, "similar_content": ""}

        event_contents = [
            event.get("content", "").lower().strip()
            for event in existing_events
            if isinstance(event, dict)
        ]

        for event_content in event_contents:
            if not event_content:
                continue
            if content_lower in event_content or event_content in content_lower:
                return {
                    "is_duplicate": True,
                    "similar_content": event_content,
                }

        for event_content in event_contents:
            if not event_content:
                continue
            similarity = self._calculate_similarity(content_lower, event_content)
            if similarity > 0.8:
                return {
                    "is_duplicate": True,
                    "similar_content": event_content,
                }

        return {"is_duplicate": False, "similar_content": ""}

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算两个文本的 Jaccard 相似度。"""

        set1 = set(text1)
        set2 = set(text2)
        if not set1 or not set2:
            return 0.0

        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0

    def _get_read_tool(self) -> ReadDiaryTool | None:
        """获取 ReadDiaryTool 实例（已废弃，直接使用 service）。"""

        return None

    def _get_service(self) -> DiaryService | None:
        """获取 DiaryService 实例。"""

        from src.app.plugin_system.api.service_api import get_service

        service = get_service("diary_plugin:service:diary_service")
        if service is None:
            return None

        if not isinstance(service, DiaryService):
            logger.error("获取到错误的 service 类型")
            return None

        return service
