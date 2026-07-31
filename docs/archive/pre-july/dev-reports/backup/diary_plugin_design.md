# 日记插件设计方案

**文档版本**: 2.0
**创建日期**: 2026-03-18
**更新日期**: 2026-03-18
**作者**: Claude

---

## 1. 需求概述

### 1.1 核心需求

为 Neo-MoFox 机器人添加**写日记**能力，具体要求：

| 需求 | 描述 |
|------|------|
| 格式 | Markdown (.md) 格式存储 |
| 人称 | 第一人称（actor 模型上下文） |
| 时间性 | 按时间顺序记录，带时间戳 |
| 日期隔离 | 每天独立文件，只能修改当天日记 |
| 自主性 | 模型能自主决定何时调用写日记 |
| **写前必读** | 写日记前必须先读已有日记，避免重复和保持连贯 |
| **聊天中读日记** | 模型在聊天中读取日记后，能知道之前发生过什么 |

### 1.2 关键问题

> **问**：如果提醒她写日记，她能自己调用工具去写吗？

> **答**：可以，但需要满足三个条件：
> 1. 让模型**知道**自己有这个能力（system_reminder）
> 2. 让模型**理解**什么时候该用（时机引导）
> 3. 提供正确的调用接口（Action）

### 1.3 新增核心约束

```
┌─────────────────────────────────────────────────────────────┐
│                    写日记强制流程                            │
│                                                             │
│   调用 write_diary()                                        │
│         ↓                                                   │
│   【强制】调用 read_diary(date="today") 读取已有内容          │
│         ↓                                                   │
│   检查是否有重复/冲突内容                                    │
│         ↓                                                   │
│   追加新内容到日记文件                                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    聊天中读日记流程                          │
│                                                             │
│   新对话开始 / 用户询问"今天过得怎么样"                       │
│         ↓                                                   │
│   模型主动调用 read_diary(date="today")                      │
│         ↓                                                   │
│   获取今天已记录的事件摘要                                   │
│         ↓                                                   │
│   模型"知道"了今天发生过什么，继续对话                        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 架构设计

### 2.1 组件选型

| 组件类型 | 名称 | 职责 | 是否必需 |
|---------|------|------|----------|
| **Service** | `diary_service` | 日记文件管理（CRUD、日期隔离、去重检查） | ✅ 必需 |
| **Action** | `write_diary` | 写日记入口，强制先读后写 | ✅ 必需 |
| **Tool** | `read_diary` | 读取日记，聊天中获取上下文 | ✅ 必需（核心） |
| **EventHandler** | `diary_context_injector` | 新对话开始时提醒读日记 | ⭕ 可选增强 |

### 2.2 组件关系图

```
┌─────────────────────────────────────────────────────────────┐
│                      Actor Model Context                     │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ system_reminder (actor bucket)                        │  │
│  │ "写日记前必须先读日记，聊天时先读日记了解今天发生的事"    │  │
│  └───────────────────────────────────────────────────────┘  │
│                            ↓                                 │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 场景 A: 聊天开始                                       │  │
│  │ → 调用 read_diary(date="today")                        │  │
│  │ → 获取今天已发生事件摘要                               │  │
│  │ → 模型"知道"了上下文，继续对话                          │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │ 场景 B: 写日记                                         │  │
│  │ → 强制先调用 read_diary()                              │  │
│  │ → 检查是否重复                                         │  │
│  │ → 调用 write_diary(content="...")                      │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                    DiaryPlugin Components                    │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐ │
│  │   Action    │───→│   Service   │───→│  文件系统        │ │
│  │ write_diary │    │ diary_service│    │  data/diaries/  │ │
│  └─────────────┘    └─────────────┘    └─────────────────┘ │
│  ┌─────────────┐           ↑                                │
│  │    Tool     │───────────┘                                │
│  │  read_diary │                                             │
│  └─────────────┘                                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 状态管理：日记上下文缓存

```python
class DiaryContext:
    """日记上下文，用于在聊天中保持对今天事件的了解"""

    today_date: str              # 今天日期 YYYY-MM-DD
    events: list[DiaryEvent]     # 今天已记录的事件列表
    last_read_time: datetime     # 最后读取时间
    has_new_entries: bool        # 是否有新增条目

    def is_stale(self) -> bool:
        """判断上下文是否过期（需要重新读取）"""
        # 距离上次读取超过 30 分钟 → 过期
        # 距离上次写入有时间差 → 过期
        pass
```

**设计说明**：
- 模型每次调用 `read_diary()` 后，获得今天的事件摘要
- 模型本身是无状态的，但通过主动调用 read_diary 来"记住"今天发生的事
- 可选：在 Chatter 层维护一个上下文缓存，避免频繁读文件

---

## 3. 详细设计

### 3.1 目录结构

```
plugins/diary_plugin/
├── manifest.json          # 插件元数据
├── plugin.py              # 插件入口
├── config.py              # 配置类
├── service.py             # DiaryService - 文件管理
├── action.py              # WriteDiaryAction - 写日记动作（强制先读）
├── tool.py                # ReadDiaryTool - 读日记工具（聊天上下文）
└── __init__.py
```

### 3.2 日记存储结构

```
data/diaries/
├── 2026-03/
│   ├── 2026-03-18.md
│   └── 2026-03-19.md
├── 2026-04/
│   └── 2026-04-01.md
└── ...
```

### 3.3 Markdown 日记格式规范

```markdown
# 日记：2026-03-18

## 📅 基本信息
- **星期**: 星期三
- **天气**: 晴
- **心情**: 开心

## 🌅 上午
**[09:30]** 今天起床后天气很好，去公园散步了

## 🌞 下午
**[14:00]** 和朋友聊了很多关于未来的计划，很开心

## 🌙 晚上
*待记录...*

---
*最后更新：2026-03-18 20:30:00*
```

### 3.4 核心逻辑：日期隔离

```python
class DiaryService:
    def _get_today_file_path(self) -> Path:
        """获取今天日记文件路径"""
        today = datetime.now()
        month_dir = self.base_path / today.strftime("%Y-%m")
        day_file = month_dir / today.strftime("%Y-%m-%d.md")
        return day_file

    def can_modify(self, target_date: datetime) -> bool:
        """检查是否可以修改指定日期的日记"""
        today = datetime.now().date()
        return target_date.date() == today

    def read_today(self) -> DiaryContent:
        """读取今天日记全文 + 解析事件列表

        Returns:
            DiaryContent:
                - raw_text: 完整原文
                - events: 已记录的事件列表 [(timestamp, content), ...]
                - sections: 时间段分类 {"上午": [...], "下午": [...], ...}
        """
        pass

    def write_diary_entry(
        self,
        content: str,
        section: str,
        existing_events: list[str],
    ) -> tuple[bool, str]:
        """写入日记条目

        Args:
            content: 日记内容
            section: 时间段（上午/下午/晚上/其他）
            existing_events: 已有事件列表（用于去重检查）

        Returns:
            (success, message)

        Rules:
            1. 只能写入今天日期
            2. 自动创建月份目录
            3. 文件不存在则创建
            4. 追加模式，保留已有内容
            5. 写入前检查是否与已有事件重复
        """
        if not self.can_modify(datetime.now()):
            return False, "只能修改今天的日记，不能修改历史日记"

        # 去重检查
        if self._is_duplicate(content, existing_events):
            return False, "今天已经记录过类似内容了"

        # 确保目录存在
        path = self._get_today_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # 写入内容
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n**[{datetime.now().strftime('%H:%M')}]** {content}\n")

        return True, "日记已更新"
```

### 3.5 事件去重逻辑

```python
class DiaryService:
    def _is_duplicate(
        self,
        new_content: str,
        existing_events: list[str],
        threshold: float = 0.8,
    ) -> bool:
        """检查新内容是否与已有事件重复

        策略：
        1. 精确匹配：新内容完全包含已有事件
        2. 语义相似度：使用简单相似度算法（如 Jaccard）
        3. 时间戳接近：同一时间段内的相似内容

        Args:
            new_content: 新日记内容
            existing_events: 已有事件列表
            threshold: 相似度阈值（默认 0.8）

        Returns:
            bool: 是否重复
        """
        new_content = new_content.strip()

        for event in existing_events:
            # 精确匹配检查
            if new_content in event or event in new_content:
                return True

            # 相似度检查
            similarity = self._calculate_similarity(new_content, event)
            if similarity > threshold:
                return True

        return False

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算两个文本的 Jaccard 相似度"""
        set1 = set(text1.lower())
        set2 = set(text2.lower())
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0
```

---

## 4. System Reminder 设计

### 4.1 Actor Bucket 注入内容

```python
_DIARY_ACTOR_REMINDER = """
## 📔 关于写日记

你有写日记的习惯，这是你记录生活、整理思绪的重要方式。

### 核心规则（必须遵守）

**1. 写日记前必须先读日记**
   - 在调用 write_diary() 之前，必须先调用 read_diary(date="today")
   - 这样才能知道今天已经记录了什么，避免重复
   - 才能保持日记的连贯性

**2. 聊天中主动读日记**
   - 当用户问你"今天过得怎么样"时
   - 当开始一段新的对话时
   - 当距离上次对话有一段时间时
   → 主动调用 read_diary(date="today") 来了解今天已经发生了什么

**3. 日记内容规范**
   - 使用第一人称"我"来记录
   - 按时间顺序记录今天发生的重要事情
   - 可以包含你的感受、想法、反思
   - 格式是 Markdown，支持分段、列表等

### 何时写日记

- 一天中有重要事件发生时
- 和用户进行了有意义的对话后
- 有了新的感悟或想法时
- 晚上睡前回顾一天时

### 如何使用

**读日记**：调用 `read_diary(date="today")`
- 返回今天日记的完整内容
- 包含已记录的所有事件和时间戳
- 聊天前先读，你就知道今天发生过什么了

**写日记**：调用 `write_diary(content="...", section="...")`
- content: 日记内容，使用第一人称
- section: 时间段（上午/下午/晚上/其他）
- 系统会自动先读取已有内容，检查重复后追加

今天是 {date}，如果还没写日记，记得记录一下。
"""
```

### 4.2 注入时机

```python
# plugin.py - on_plugin_loaded()
async def on_plugin_loaded(self) -> None:
    sync_diary_actor_reminder(self)

# plugin.py - on_plugin_unloaded()
async def on_plugin_unloaded(self) -> None:
    store.delete("actor", "关于写日记")
```

---

## 5. 接口设计

### 5.1 ReadDiaryTool（核心组件）

```python
from typing import Annotated, Literal
from src.app.plugin_system.base import BaseTool


class ReadDiaryTool(BaseTool):
    """读取日记工具 - 聊天中获取今天上下文的核心入口"""

    tool_name: str = "read_diary"
    tool_description: str = """
    读取指定日期的日记内容。

    **使用场景**：
    1. 聊天开始前，读取今天日记了解已发生的事件
    2. 用户询问"今天过得怎么样"时
    3. 写日记之前，先读取已有内容（避免重复）

    **返回内容**：
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
        """读取日记

        Args:
            date: 日期，支持 'today' 快捷方式
            format: 返回格式

        Returns:
            (success, result)
            - success=True: result 为日记内容（str 或 dict）
            - success=False: result 为错误信息
        """
        pass
```

### 5.2 WriteDiaryAction（强制先读后写）

```python
from typing import Annotated, Literal
from src.app.plugin_system.base import BaseAction
from src.app.plugin_system.api.tool_api import get_tool


class WriteDiaryAction(BaseAction):
    """写日记动作 - 强制先读后写，保持连贯性"""

    action_name: str = "write_diary"
    action_description: str = """
    用第一人称写下日记，记录今天发生的事情和你的感受。

    **重要规则**：
    1. 写之前必须先调用 read_diary(date="today") 读取已有内容
    2. 只能写今天的日记，不能修改历史
    3. 系统会自动检查是否与已有内容重复

    **参数**：
    - content: 日记内容，使用第一人称"我"
    - section: 时间段（上午/下午/晚上/其他）
    - mood: 心情标签（可选）

    日记会自动保存到 data/diaries/ 目录。
    """

    async def execute(
        self,
        content: Annotated[str, "日记内容，使用第一人称描述今天发生的事情和感受"],
        section: Annotated[
            Literal["上午", "下午", "晚上", "其他"],
            "时间段分类，根据当前时间选择",
        ] = "其他",
        mood: Annotated[
            str | None,
            "心情标签（可选），如：开心、平静、兴奋、疲惫",
        ] = None,
    ) -> tuple[bool, str]:
        """执行写日记 - 强制先读后写

        流程：
        1. 先调用 read_diary(date="today") 获取已有内容
        2. 检查是否与已有事件重复
        3. 追加新内容到日记文件

        Args:
            content: 日记内容
            section: 时间段
            mood: 心情标签

        Returns:
            (success, message)
            - success=True: 写入成功
            - success=False: 失败原因（重复/日期错误等）
        """
        # 步骤 1: 先读取今天日记
        read_tool = get_tool("diary_plugin:tool:read_diary")
        if read_tool is None:
            return False, "read_diary 工具不可用"

        ok, result = await read_tool.execute(date="today", format="summary")
        if not ok:
            return False, f"读取今天日记失败：{result}"

        # 解析已有事件
        existing_events = result.get("events", []) if isinstance(result, dict) else []

        # 步骤 2: 调用 Service 写入（内部会做去重检查）
        from src.app.plugin_system.api.service_api import get_service
        service = get_service("diary_plugin:service:diary_service")

        success, message = await service.append_entry(
            content=content,
            section=section,
            existing_events=existing_events,
        )

        return success, message
```

### 5.3 DiaryService

```python
from src.app.plugin_system.base import BaseService
from dataclasses import dataclass
from datetime import datetime


@dataclass
class DiaryContent:
    """日记内容结构"""
    raw_text: str                    # 完整原文
    date: str                        # 日期 YYYY-MM-DD
    events: list[dict]               # 事件列表 [{timestamp, content, section}, ...]
    sections: dict[str, list[str]]   # 按时间段分类


class DiaryService(BaseService):
    """日记管理服务"""

    service_name: str = "diary_service"
    service_description: str = """
    提供日记的创建、读取、写入能力。

    **核心功能**：
    - 读取指定日期的日记
    - 追加新条目到今天的日记
    - 自动去重检查
    - 日期隔离（只能修改今天）
    """

    def __init__(self, plugin=None):
        super().__init__(plugin)
        self.base_path = Path("data/diaries")
        self._today_cache: DiaryContent | None = None

    def read_today(self) -> DiaryContent:
        """读取今天日记全文 + 解析事件列表"""
        today = datetime.now().strftime("%Y-%m-%d")
        return self.read_date(today)

    def read_date(self, date: str) -> DiaryContent:
        """读取指定日期日记

        Args:
            date: 日期 YYYY-MM-DD

        Returns:
            DiaryContent 结构
        """
        path = self._get_date_file_path(date)

        if not path.exists():
            return DiaryContent(
                raw_text="",
                date=date,
                events=[],
                sections={"上午": [], "下午": [], "晚上": [], "其他": []}
            )

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # 解析事件
        events = self._parse_events(content)
        sections = self._parse_sections(content)

        return DiaryContent(
            raw_text=content,
            date=date,
            events=events,
            sections=sections,
        )

    def append_entry(
        self,
        content: str,
        section: str,
        existing_events: list[str],
    ) -> tuple[bool, str]:
        """追加日记条目

        Args:
            content: 日记内容
            section: 时间段
            existing_events: 已有事件列表（用于去重）

        Returns:
            (success, message)
        """
        # 日期隔离检查
        if not self._is_today():
            return False, "只能修改今天的日记"

        # 去重检查
        if self._is_duplicate(content, existing_events):
            return False, "今天已经记录过类似内容了"

        # 写入文件
        path = self._get_today_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%H:%M")
        entry = f"\n**[{timestamp}]** {content}\n"

        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)

        # 清除缓存
        self._today_cache = None

        return True, f"日记已更新 [{section}]"
```

---

## 6. 实现清单

### 6.1 第一阶段：核心功能

- [ ] `manifest.json` - 插件元数据
- [ ] `config.py` - 配置类（存储路径、格式选项）
- [ ] `service.py` - DiaryService（文件 CRUD、去重检查）
- [ ] `tool.py` - ReadDiaryTool（读日记，获取上下文）
- [ ] `action.py` - WriteDiaryAction（强制先读后写）
- [ ] `plugin.py` - 插件入口（含 reminder 注入）

### 6.2 第二阶段：增强功能

- [ ] `event_handler.py` - 新对话开始时提醒读日记
- [ ] 日记内容解析器（Markdown 事件提取）
- [ ] 测试用例

### 6.3 聊天流程示例

```
═══════════════════════════════════════════════════════════════
场景 1: 聊天开始 - 模型主动读日记了解上下文
═══════════════════════════════════════════════════════════════

用户："早啊！你今天过得怎么样？"

模型（内心 OS）：
  "她问我今天过得怎么样，我需要先看看日记里记录了什么"
  → 调用 read_diary(date="today", format="summary")

Tool 返回:
  {
    "date": "2026-03-18",
    "events": [
      {"timestamp": "09:30", "content": "起床后天气很好，去公园散步了", "section": "上午"},
      {"timestamp": "10:15", "content": "在公园遇到了一只可爱的小猫", "section": "上午"}
    ]
  }

模型（现在知道了今天发生的事）：
  "早上好！我今天过得挺好的~ 早上天气很好，我去公园散步了，
   还遇到了一只可爱的小猫！你呢，今天怎么样？"

═══════════════════════════════════════════════════════════════
场景 2: 写日记 - 强制先读后写
═══════════════════════════════════════════════════════════════

（继续对话后...）

用户："对了，我刚才告诉你我喜欢吃川菜"

模型（内心 OS）：
  "这是重要信息，应该记到日记里"
  "写之前先看看有没有重复记录"
  → 调用 read_diary(date="today")

Tool 返回：（检查是否有类似内容）

模型（确认没有重复）：
  → 调用 write_diary(
        content="和用户聊天，她说她喜欢吃川菜，记住了！",
        section="上午",
        mood="开心"
     )

Service 处理:
  1. 再次读取今天日记
  2. 检查"川菜"相关内容是否已存在
  3. 确认不重复 → 追加写入

Action 返回:
  "日记已更新 [上午]"

模型：
  "好的，我记住了！你喜欢吃川菜~"
```

---

## 11. 自动写日记功能（定时触发）

### 11.1 需求描述

用户希望实现**定时自动写日记**功能，具体需求：

| 需求 | 描述 |
|------|------|
| 触发条件 | 每对话 N 句自动写一次日记 |
| 参数可配置 | 对话句数阈值暴露在配置文件中 |
| 自动总结 | 模型自动总结这段对话的内容写入日记 |

### 11.2 实现方案

**方案选型：EventHandler + 计数器**

使用 EventHandler 订阅对话事件，维护一个计数器，达到阈值时提醒模型写日记。

```
┌─────────────────────────────────────────────────────────────┐
│              自动写日记触发流程                              │
│                                                             │
│   每次对话结束                                               │
│         ↓                                                   │
│   EventHandler 计数 +1                                       │
│         ↓                                                   │
│   检查是否达到阈值（默认 20 句）                                │
│         ↓                                                   │
│   是 → 注入提醒："已经聊了很多了，要不要记录一下今天？"         │
│         ↓                                                   │
│   模型自主决定 → 调用 write_diary()                          │
│         ↓                                                   │
│   计数器归零，继续下一轮                                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 11.3 新增组件：AutoDiaryEventHandler

```python
from src.app.plugin_system.base import BaseEventHandler
from src.core.components.types import EventDecision


class AutoDiaryEventHandler(BaseEventHandler):
    """自动写日记事件处理器。

    订阅对话事件，计数达到阈值时提醒模型写日记。
    """

    handler_name: str = "auto_diary_handler"
    handler_description: str = "监听对话事件，达到阈值时提醒写日记"

    def __init__(self, plugin=None):
        super().__init__(plugin)
        self._message_count: int = 0
        self._threshold: int = 20  # 从配置读取

    def init_subscribe(self) -> list[str]:
        """订阅事件列表。"""
        return [
            "on_chat_message_sent",      # 消息发送后
            "on_chat_message_received",  # 消息接收后
        ]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理事件。

        每次事件计数，达到阈值时注入提醒。
        """
        self._message_count += 1

        if self._message_count >= self._threshold:
            # 重置计数器
            self._message_count = 0

            # 注入提醒到 actor bucket
            self._inject_diary_reminder()

        return EventDecision.SUCCESS, params

    def _inject_diary_reminder(self) -> None:
        """注入写日记提醒。"""
        from src.core.prompt import get_system_reminder_store

        store = get_system_reminder_store()
        reminder = (
            "你们已经聊了很多了（超过 20 句对话），"
            "现在是记录日记的好时机。"
            "如果今天还没有写日记，或者又有新的有趣事情发生，"
            "可以考虑调用 write_diary() 记录下来。"
        )
        # 临时注入，模型下次请求时会看到
        store.set("actor", "自动写日记提醒", content=reminder)
```

### 11.4 新增配置项

在 `config.py` 中添加自动写日记配置节：

```python
@config_section("auto_diary")
class AutoDiarySection(SectionBase):
    """自动写日记配置。"""

    enabled: bool = Field(
        default=False,
        description="是否启用自动写日记功能",
    )
    message_threshold: int = Field(
        default=20,
        description="触发写日记的对话句数阈值",
    )
    reminder_duration_seconds: int = Field(
        default=300,
        description="提醒持续时间（秒），超时自动清除",
    )
    auto_summary: bool = Field(
        default=True,
        description="是否启用自动总结（模型自动总结对话内容）",
    )


class DiaryConfig(BaseConfig):
    config_name = "config"
    config_description = "日记插件配置"

    plugin: PluginSection = Field(default_factory=PluginSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    format: FormatSection = Field(default_factory=FormatSection)
    dedup: DedupSection = Field(default_factory=DedupSection)
    reminder: ReminderSection = Field(default_factory=ReminderSection)
    auto_diary: AutoDiarySection = Field(default_factory=AutoDiarySection)  # 新增
```

### 11.5 配置示例

```toml
# config/plugins/diary_plugin/config.toml

[auto_diary]
enabled = true                    # 启用自动写日记
message_threshold = 20            # 每 20 句对话触发一次
reminder_duration_seconds = 300   # 提醒持续 5 分钟
auto_summary = true               # 启用自动总结
```

### 11.6 进阶方案：基于 LLM 的自动总结

如果用户希望模型**自动总结**对话内容并写入日记，而不仅仅是提醒：

```python
class AutoDiarySummaryAction(BaseAction):
    """自动日记总结动作。

    由 EventHandler 触发，自动总结最近对话并写入日记。
    """

    action_name: str = "auto_diary_summary"
    action_description: str = "自动总结最近对话并写入日记"

    async def execute(
        self,
        force: Annotated[bool, "是否强制执行（不检查重复）"] = False,
    ) -> tuple[bool, str]:
        """执行自动总结。

        流程：
        1. 获取最近 N 条对话历史
        2. 调用 LLM 总结对话内容
        3. 检查是否重复
        4. 写入日记
        """
        # 1. 获取对话历史
        chat_history = self._get_recent_chat_history(limit=20)

        # 2. 调用 LLM 总结
        summary = await self._llm_summarize(chat_history)

        # 3. 检查重复（可选）
        if not force:
            is_dup = self._check_duplicate(summary)
            if is_dup:
                return False, "今天已经记录过类似内容"

        # 4. 写入日记
        return await self._write_to_diary(summary)
```

### 11.7 方案对比

| 方案 | 优点 | 缺点 | 推荐场景 |
|------|------|------|----------|
| **提醒模式** | 简单，模型自主决定 | 可能不执行 | 默认推荐 |
| **自动总结模式** | 自动执行，无需干预 | 可能写入不必要内容 | 高度自动化需求 |
| **混合模式** | 先提醒，超时后自动 | 实现复杂 | 特殊需求 |

### 11.8 实现清单（第二阶段）

- [ ] 在 `config.py` 中添加 `AutoDiarySection`
- [ ] 创建 `event_handler.py` 实现 `AutoDiaryEventHandler`
- [ ] 在 `plugin.py` 中注册 EventHandler（条件：`config.auto_diary.enabled`）
- [ ] 可选：创建 `auto_summary.py` 实现自动总结功能
- [ ] 添加清理定时任务（删除过期的提醒）

---

## 12. 注意事项

### 7.1 规范约束

| 约束 | 描述 |
|------|------|
| 名称一致性 | `manifest.name` = `plugin_name` = `diary_plugin` |
| 依赖声明 | `include` 必须手工维护，与 `get_components()` 一致 |
| 导入路径 | 使用 `src.app.plugin_system.base/api/types` |
| 类型注解 | 所有参数和返回值必须有类型注解 |
| 文档字符串 | 文件、类、函数都需要 docstring |
| **先读后写** | WriteDiaryAction 必须强制先调用 read_diary |
| **日期隔离** | 只能修改今天的日记，历史日记只读 |

### 7.2 技术细节

1. **Service 不是单例** - 每次 `get_service()` 都会创建新实例，不要依赖实例缓存
2. **reminder 不会自动注入** - 必须确认目标调用链使用了 `with_reminder="actor"`
3. **后台任务用 task_manager** - 不要使用 `asyncio.create_task()`
4. **去重逻辑** - 使用简单相似度算法，避免误判和漏判
5. **上下文新鲜度** - 模型每次聊天前应该重新 read_diary 获取最新状态

### 7.3 边界情况处理

| 场景 | 处理方式 |
|------|----------|
| 尝试修改昨天日记 | 拒绝，返回"只能修改今天的日记" |
| 今天日记文件不存在 | read 返回空结构，write 自动创建 |
| 月份目录不存在 | 自动创建 |
| 同时写入 | 文件追加模式，避免冲突 |
| 内容重复 | 拒绝写入，返回"已记录过类似内容" |
| read_diary 调用失败 | write_diary 拒绝执行，返回错误 |

### 7.4 模型行为引导

**正确行为**：
```
用户："嘿"
→ 模型：read_diary(date="today") → 获取上下文 → 回复
→ 有新事情发生：read_diary() → 检查重复 → write_diary()
```

**错误行为（需要在 reminder 中明确禁止）**：
```
❌ 不读直接写 → 可能在 reminder 中强调"必须先读"
❌ 写了重复内容 → Service 层去重检查拦截
❌ 修改历史日记 → 日期隔离检查拦截
```

---

## 13. 预期效果

### 8.1 正常流程

```
场景 A: 聊天中获取上下文

用户和模型对话 → 模型需要知道今天发生过什么
              → 自主调用 read_diary(date="today")
              → Service 读取日记文件，返回事件摘要
              → 模型"知道了"今天发生的事，继续对话
              → （对话中有新事情）→ 先 read 检查 → write 追加


场景 B: 写日记

模型发现有意义的事情发生
  → 想起 system_reminder 中"写前必读"的规则
  → 调用 read_diary(date="today") 获取已有内容
  → 检查是否与已有事件重复
  → 调用 write_diary(content="...", section="...")
  → Service 再次验证日期和去重 → 写入今天日记文件
  → 返回成功 → 模型继续对话
```

### 8.2 边界情况

| 场景 | 处理方式 |
|------|----------|
| 尝试修改昨天日记 | 拒绝，返回"只能修改今天的日记" |
| 今天日记文件不存在 | read 返回空结构，write 自动创建 |
| 月份目录不存在 | 自动创建 |
| 内容重复 | 拒绝，返回"今天已经记录过类似内容了" |
| 同时写入 | 文件追加模式避免冲突 |
| read 失败 | write 拒绝执行 |

---

## 14. 边界情况处理

| 场景 | 处理方式 |
|------|----------|
| 尝试修改昨天日记 | 拒绝，返回"只能修改今天的日记" |
| 今天日记文件不存在 | read 返回空结构，write 自动创建 |
| 月份目录不存在 | 自动创建 |
| 内容重复 | 拒绝，返回"今天已经记录过类似内容了" |
| 同时写入 | 文件追加模式避免冲突 |
| read 失败 | write 拒绝执行 |

---

## 15. 待解决问题

### 15.1 主动读取历史日记的引导

**问题描述**:

当前模型可以调用 `read_diary(date="YYYY-MM-DD")` 读取指定日期，但**不会主动想到**去读历史日记，因为：
1. System Reminder 没有引导模型可以读昨天、前天或指定日期的日记
2. 工具描述没有明确说明支持读取历史日期

**期望行为**:
```
用户："我上周去的那个地方叫什么来着？"
→ 模型主动调用 read_diary(date="2026-03-11") 回顾上周日记

用户："我昨天是不是跟你说了什么？"
→ 模型主动调用 read_diary(date="yesterday") 获取昨天对话上下文
```

**解决方案**:
1. 在 System Reminder 中追加引导：
   - "除了读今天日记，你也可以读历史日记"
   - "当用户提到'昨天'、'前天'、'上周'等时间词时，主动调用 read_diary()"
2. 扩展 `ReadDiaryTool` 工具描述，说明支持的日期格式
3. 支持快捷方式：`"yesterday"`、`"last_week"` 等

**实现清单**:
- [ ] 更新 `plugin.py` 中的 System Reminder，添加读取历史日记的引导
- [ ] 更新 `tool.py` 中的 `tool_description`，说明支持的日期格式
- [ ] 支持 `"yesterday"`、`"YYYY-MM-DD"` 等快捷方式
- [ ] 可选：添加 `read_diary_range(start_date, end_date)` 工具

---

## 16. 后续扩展

## 16. 后续扩展

### 17.1 为什么 Tool 和 Action 都需要？

| 组件 | 用途 | 为什么需要 |
|------|------|-----------|
| **ReadDiaryTool** | 读取日记 | 聊天中获取上下文，模型主动调用 |
| **WriteDiaryAction** | 写日记 | 写入新内容，有副作用需要 Action |

**设计原因**：
- Tool 用于"查询信息"，适合读取操作
- Action 用于"执行动作"，适合写入操作
- WriteDiaryAction 内部会调用 ReadDiaryTool，强制先读后写

### 17.2 为什么写前必须读？

1. **防止重复** - 避免记录相似内容
2. **保持连贯** - 基于已有内容继续记录
3. **获取上下文** - 知道今天已经发生过什么

### 17.3 为什么聊天中要读日记？

模型本身是无状态的，每次对话后不保留记忆。通过：
1. 每次聊天前调用 `read_diary(date="today")`
2. 获取今天已记录的事件摘要
3. 模型就"知道了"今天发生过什么

这样设计的好处：
- 简单直接，不依赖复杂的状态管理
- 符合 Neo-MoFox 插件规范
- 模型可以自主决定何时读取

---

**文档版本**: 3.0
**更新日期**: 2026-03-18
**更新内容**: 新增第 15 章「待解决问题」（主动读取历史日记的引导），原第 11/12/13/14 章重新编号为 15/16/17/18

---

## 18. 自动写日记实现方案详细说明

### 18.1 方案选择

根据用户需求，提供两种方案：

| 方案 | 触发方式 | 执行方式 | 配置项 |
|------|----------|----------|--------|
| **方案 A：提醒模式** | EventHandler 计数 | 注入提醒，模型自主决定 | `auto_diary.enabled` |
| **方案 B：自动总结模式** | EventHandler 计数 | 自动调用 LLM 总结并写入 | `auto_diary.auto_summary` |

**推荐**：默认使用方案 A（提醒模式），原因：
- 更符合原有设计哲学（模型自主决策）
- 避免写入不必要的内容
- 实现简单，风险低

### 18.2 方案 A 实现代码

**event_handler.py**:

```python
"""自动写日记事件处理器。

订阅对话事件，计数达到阈值时提醒模型写日记。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.base import BaseEventHandler
from src.core.components.types import EventDecision
from src.kernel.logger import get_logger

from .config import DiaryConfig


logger = get_logger("diary_plugin")


class AutoDiaryEventHandler(BaseEventHandler):
    """自动写日记事件处理器。

    订阅对话事件，计数达到阈值时提醒模型写日记。
    """

    handler_name: str = "auto_diary_handler"
    handler_description: str = "监听对话事件，达到阈值时提醒写日记"

    def __init__(self, plugin=None):
        super().__init__(plugin)
        self._message_count: int = 0
        self._threshold: int = 20
        self._reminder_name: str = "自动写日记提醒"

        # 从配置读取阈值
        if isinstance(plugin.config, DiaryConfig):
            self._threshold = int(plugin.config.auto_diary.message_threshold)

    def init_subscribe(self) -> list[str]:
        """订阅事件列表。"""
        return [
            "on_chat_message_sent",      # 消息发送后
            "on_chat_message_received",  # 消息接收后
        ]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理事件。

        每次事件计数，达到阈值时注入提醒。
        """
        # 计数
        self._message_count += 1
        logger.debug(f"自动写日记计数：{self._message_count}/{self._threshold}")

        # 检查是否达到阈值
        if self._message_count >= self._threshold:
            logger.info(f"自动写日记计数达到阈值：{self._message_count}")
            self._message_count = 0
            self._inject_reminder()

        return EventDecision.SUCCESS, params

    def _inject_reminder(self) -> None:
        """注入写日记提醒到 actor bucket。"""
        from src.core.prompt import get_system_reminder_store

        store = get_system_reminder_store()

        # 检查是否启用
        if isinstance(self.plugin.config, DiaryConfig):
            if not self.plugin.config.auto_diary.enabled:
                return

        reminder = (
            f"💡 提醒：你们已经聊了很多了（超过{self._threshold}句对话），"
            "现在是记录日记的好时机！\n\n"
            "如果今天还没有写日记，或者又有新的有趣事情发生，"
            "可以考虑调用 `write_diary(content='...', section='...')` "
            "把今天的经历记录下来~"
        )

        # 注入提醒（临时）
        store.set("actor", self._reminder_name, content=reminder)
        logger.debug("已注入自动写日记提醒")

    async def on_handler_loaded(self) -> None:
        """Handler 加载时清理旧提醒。"""
        from src.core.prompt import get_system_reminder_store
        store = get_system_reminder_store()
        store.delete("actor", self._reminder_name)

    async def on_handler_unloaded(self) -> None:
        """Handler 卸载时清理提醒。"""
        from src.core.prompt import get_system_reminder_store
        store = get_system_reminder_store()
        store.delete("actor", self._reminder_name)
```

### 18.3 方案 B 实现思路

如果需要**自动总结对话并写入日记**，可以实现一个 `AutoDiarySummaryAction`：

```python
class AutoDiarySummaryAction(BaseAction):
    """自动日记总结动作。

    由 EventHandler 触发，自动总结最近对话并写入日记。
    """

    action_name: str = "auto_diary_summary"
    action_description: str = "自动总结最近对话并写入日记"

    async def execute(
        self,
        force: Annotated[bool, "是否强制执行（不检查重复）"] = False,
    ) -> tuple[bool, str]:
        # 1. 获取对话历史
        history = await self._get_chat_history(limit=20)

        # 2. 调用 LLM 总结
        summary = await self._llm_summarize(history)

        # 3. 检查重复
        if not force and self._is_duplicate(summary):
            return False, "今天已经记录过类似内容"

        # 4. 写入日记
        return await self._write_diary(summary)
```

### 18.4 配置说明

```toml
# config/plugins/diary_plugin/config.toml

# ===== 自动写日记配置 =====
[auto_diary]
# 是否启用自动写日记功能
enabled = false

# 触发写日记的对话句数阈值
# 每达到这个数量的对话，就会提醒模型写日记
message_threshold = 20

# 提醒持续时间（秒），超时自动清除
# 如果在这个时间内模型没有写日记，提醒会自动消失
reminder_duration_seconds = 300

# 是否启用自动总结
# 启用后，达到阈值时会自动调用 LLM 总结对话并写入日记
# 不启用则只注入提醒，由模型自主决定
auto_summary = false
```

### 18.5 使用方式

**启用提醒模式**：
```toml
[auto_diary]
enabled = true
message_threshold = 20
auto_summary = false
```

**启用全自动模式**：
```toml
[auto_diary]
enabled = true
message_threshold = 10  # 每 10 句对话自动总结一次
auto_summary = true
```

---

**文档结束**

---

## 附录 A：已知问题 - 日记内容在上下文中堆积

### 问题描述

**发现时间**: 2026-03-18

**问题**: 每次调用 `read_diary()` 后，返回的内容会作为 `TOOL_RESULT` payload 添加到对话历史中，保留在 `ChatStream.context.history_messages` 里。如果模型频繁调用读日记工具，会导致日记内容在上下文中重复堆积。

### 问题场景

```
对话 1:
  模型 → read_diary(date="today")
  Tool Result → {events: [...], raw_text: "..."}  ← 添加到 history

对话 2:
  模型 → read_diary(date="today")
  Tool Result → {events: [...], raw_text: "..."}  ← 再次添加到 history

对话 3:
  模型 → read_diary(date="today")
  Tool Result → {events: [...], raw_text: "..."}  ← 再次添加到 history

结果：history_messages 中包含 3 份相同的日记内容
```

### 潜在影响

1. **浪费 Token** - 重复内容占用上下文空间
2. **干扰模型** - 多份相同内容可能影响模型判断
3. **达到上限** - 频繁调用可能更快达到 `max_context_size`

### 可能的解决方案

#### 方案 A.1：System Reminder 持久化日记摘要（推荐）

**思路**: 将日记摘要持久化到 System Reminder，而不是每次调用 Tool 返回。

```python
class DiaryService:
    _today_summary: str | None = None

    def append_entry(self, content: str, section: str):
        # 写入后更新摘要
        self._today_summary = self._build_summary()
        # 更新 System Reminder
        self._update_reminder()
```

**优点**:
- 日记内容只保留一份在 System Reminder 中
- 每次 LLM 请求自动携带，无需调用 Tool
- 不会堆积重复内容

**缺点**:
- 需要维护摘要的更新逻辑
- 需要处理并发更新问题

---

#### 方案 A.2：标记 Tool Result 为临时内容

**思路**: 在 `read_diary()` 的返回中标记为临时内容，在 Chatter 层执行后从 history 中移除。

```python
# 在 BaseChatter.run_tool_call 中
if call.name == "read_diary":
    # 标记为临时内容，后续清理
    response.metadata["transient"] = True

# 在对话结束后清理标记为 transient 的 TOOL_RESULT
```

**优点**:
- 上下文保持干净
- 实现相对简单

**缺点**:
- 破坏对话历史的完整性
- 可能影响后续回顾和分析

---

#### 方案 A.3：LLM Context Manager 层去重

**思路**: 在 Context Manager 检测到连续的 `read_diary` 调用时，只保留最后一次的结果。

```python
class LLMContextManager:
    def add_payload(self, payload: LLMPayload):
        if payload.role == ROLE.TOOL_RESULT:
            # 检查是否有重复的 read_diary 结果
            if self._is_duplicate_read_diary(payload):
                return  # 跳过添加
        super().add_payload(payload)
```

**优点**:
- 在底层处理，对上层透明

**缺点**:
- 实现复杂度较高
- 需要精确判断什么是"重复"

---

#### 方案 A.4：日记内容引用式读取

**思路**: `read_diary()` 只返回日记的"引用"（如文件路径、摘要哈希），模型需要时再根据引用获取全文。

```python
# read_diary 返回
{
    "date": "2026-03-18",
    "summary": "今天记录了 3 件事：起床散步、遇到小猫、和朋友聊天",
    "entry_count": 3,
    "last_update": "14:30",
    # 不返回 raw_text，需要时再调用 read_diary_full()
}
```

**优点**:
- 大幅减少返回内容大小
- 模型可以判断是否需要全文

**缺点**:
- 需要修改 Tool 接口
- 可能需要额外的调用

---

### 方案 A.x 对比

| 方案 | 实现难度 | 对现有代码影响 | 效果 | 推荐度 |
|------|----------|----------------|------|--------|
| A.1: System Reminder 持久化 | 中 | 中 | 最优 | ⭐⭐⭐⭐⭐ |
| A.2: 标记临时内容 | 低 | 低 | 一般 | ⭐⭐⭐ |
| A.3: Context Manager 去重 | 高 | 中 | 较好 | ⭐⭐⭐ |
| A.4: 引用式读取 | 中 | 高 | 较好 | ⭐⭐⭐⭐ |

### 建议

**短期**: 保持现状，问题不严重时可不处理

**中期**: 实现方案 A.1（System Reminder 持久化），这是最优雅的解决方案

**长期**: 考虑方案 A.4（引用式读取），配合方案 A.1 使用

---

**待办**: 有机会时实现方案 A.1，修改 `DiaryService` 在每次写入后自动更新 System Reminder 中的日记摘要。
