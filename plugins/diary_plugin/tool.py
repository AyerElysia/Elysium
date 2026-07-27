"""读取日记工具实现。"""

from __future__ import annotations

from typing import Annotated, Literal

from src.app.plugin_system.base import BaseTool
from src.kernel.logger import get_logger

from .service import DiaryService


logger = get_logger("diary_plugin")


class ReadDiaryTool(BaseTool):
    """读取日记工具。"""

    tool_name: str = "read_diary"
    tool_description: str = """
    读取指定日期的日记内容。

    使用场景：
    - 自己想知道今天或者前几日发生了什么时

    """

    """
    读取指定日期的日记内容。

    使用场景：
    - 用户询问"今天过得怎么样"时


    返回内容：
    - 日记全文（Markdown 格式）
    - 已记录的事件列表（带时间戳）
    - 各时间段的摘要

    """

    async def execute(
        self,
        date: Annotated[
            str | None,
            "日期，格式：YYYY-MM-DD 或 'today'，为空时默认读取今天",
        ] = None,
        format: Annotated[
            Literal["full", "summary"],
            "返回格式：full=完整日记，summary=事件摘要列表",
        ] = "full",
    ) -> tuple[bool, str | dict]:
        """读取日记。"""

        if date is None or date == "today":
            date = None
            display_date = "今天"
        else:
            try:
                from datetime import datetime

                datetime.strptime(date, "%Y-%m-%d")
                display_date = date
            except ValueError:
                return False, f"日期格式错误：{date}，请使用 YYYY-MM-DD 格式或 'today'"

        service = self._get_service()
        if service is None:
            return False, "diary_service 未加载"

        try:
            if date is None:
                diary_content = service.read_today()
            else:
                diary_content = service.read_date(date)

            if format == "summary":
                summary = service.get_today_summary()
                return True, summary

            if not diary_content.exists:
                return True, {
                    "date": diary_content.date,
                    "exists": False,
                    "message": f"{display_date} 还没有日记，开始记录吧！",
                    "raw_text": "",
                    "events": [],
                }

            return True, {
                "date": diary_content.date,
                "exists": True,
                "raw_text": diary_content.raw_text,
                "events": [
                    {
                        "timestamp": event.timestamp,
                        "content": event.content,
                        "section": event.section,
                    }
                    for event in diary_content.events
                ],
            }

        except Exception as exc:
            logger.error(f"读取日记失败：{exc}")
            return False, f"读取日记失败：{exc}"

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
