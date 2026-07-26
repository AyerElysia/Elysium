# 日记插件实现报告

**文档版本**: 2.0
**完成日期**: 2026-03-18
**更新日期**: 2026-03-18
**作者**: Claude

---

## 更新记录

### v3.0 (2026-03-18) 自动写日记功能

**新增功能**: 自动总结对话并写入日记

**需求**:
- 每对话达到阈值后自动总结对话内容
- 总结内容使用第一人称，与 actor 拥有相同的上下文
- 自动写入日记，无需模型手动干预
- 参数暴露在配置文件中可调整

**实现方案**:
- 新增 `AutoDiarySection` 配置节，包含 `enabled`、`message_threshold`、`summary_message_count`、`auto_summary` 配置项
- 创建 `AutoDiaryEventHandler` 订阅 `ON_CHATTER_STEP` 事件
- 创建 `AutoDiarySummaryAction` 执行对话总结和写入
- 按 `stream_id` 隔离计数器，达到阈值时自动调用 LLM 总结
- 使用 `get_stream_manager()` 获取与 actor 相同的聊天流上下文
- 使用 `actor` 模型任务配置调用 LLM，确保上下文一致性
- 总结后自动检查重复并写入日记

**修改文件**:
- `config.py` - 添加 `AutoDiarySection` 配置节，新增 `summary_message_count` 和 `auto_summary` 字段
- `event_handler.py` - 完全重写为自动总结模式，新增 `_auto_summary()`、`_llm_summarize()` 方法
- `auto_summary_action.py` - 新建独立 Action 类，封装总结逻辑
- `plugin.py` - 注册 EventHandler 组件
- `manifest.json` - 声明 EventHandler 组件
- `config.toml` - 添加 `auto_diary` 配置节

**核心代码**:
```python
class AutoDiaryEventHandler(BaseEventHandler):
    """自动写日记事件处理器 - 达到阈值自动总结对话"""
    handler_name: str = "auto_diary_handler"
    weight: int = 5
    init_subscribe: list[EventType | str] = [EventType.ON_CHATTER_STEP]

    async def execute(self, event_name: str, params: dict[str, Any]):
        # 1. 获取 stream_id
        # 2. 计数器 +1
        # 3. 检查是否达到阈值
        # 4. 达到阈值时调用 _auto_summary() 自动总结
        # 5. 重置计数器
        return EventDecision.SUCCESS, params

    async def _auto_summary(self, stream_id: str, summary_count: int) -> None:
        """执行自动总结并写入日记"""
        # 获取聊天流上下文（与 actor 相同的上下文）
        stream_manager = get_stream_manager()
        chat_stream = stream_manager._streams.get(stream_id)
        context = chat_stream.context

        # 获取最近 N 条对话历史
        all_messages = list(context.history_messages) + list(context.unread_messages)
        recent_messages = all_messages[-summary_count:]

        # 格式化为文本
        history_lines = [f"{sender}: {content}" for msg in recent_messages]

        # 调用 LLM 总结（使用 actor 模型配置）
        summary = await self._llm_summarize(history_lines)

        # 检查重复并写入日记
        if not self._is_duplicate(summary):
            await self._write_diary(summary)
```

**配置示例**:
```toml
[auto_diary]
enabled = true
message_threshold = 5              # 触发阈值（测试时可改小）
summary_message_count = 10         # 总结最近 N 条对话
auto_summary = true                # 启用自动总结模式
```

**关键改进**:
- **上下文一致性**: 通过 `get_stream_manager()` 访问与 actor 相同的 `ChatStream.context`
- **自动执行**: 无需模型干预，系统自动总结并写入
- **去重检查**: 使用 Jaccard 相似度检查与今天已有日记的重复度
- **第一人称输出**: LLM 系统提示要求使用"我"作为主语

---

### v2.0 (2026-03-18) Bug 修复

**问题**: WriteDiaryAction 执行时报错 `No module named 'src.app.plugin_system.api.tool_api'`

**原因**:
- `src.app.plugin_system.api.tool_api` 模块在 Neo-MoFox 中不存在
- Action 中尝试通过 tool_api 获取 Tool 实例导致导入失败

**修复方案**:
- 修改 WriteDiaryAction._read_today() 方法，直接调用 DiaryService.get_today_summary()
- 移除对不存在的 tool_api 的依赖
- 简化 WriteDiaryAction 的实现，直接从 Service 层获取数据

**修改内容**:
```python
# 修复前（错误）
from src.app.plugin_system.api.tool_api import get_tool
tool = get_tool("diary_plugin:tool:read_diary")
ok, result = await tool.execute(date="today", format="summary")

# 修复后（正确）
service = self._get_service()  # 获取 DiaryService
summary = service.get_today_summary()  # 直接调用 Service 方法
```

---

## 1. 工作概述

本次工作为 Neo-MoFox 聊天机器人系统实现了一个**日记插件**，使模型具备写日记的能力。

### 1.1 核心需求

| 需求 | 实现方式 |
|------|----------|
| Markdown 格式存储 | 日记以.md 格式保存到 `data/diaries/` 目录 |
| 第一人称（actor 上下文） | 通过 system_reminder 注入引导语 |
| 时间性记录 | 每条记录自动添加时间戳 `[HH:MM]` |
| 日期隔离 | 只能修改今天日记，历史日记只读 |
| 写前必读 | WriteDiaryAction 强制先调用 read_diary() |
| 聊天中读日记 | ReadDiaryTool 提供上下文获取能力 |

---

## 2. 实现文件清单

### 2.1 插件目录结构

```
plugins/diary_plugin/
├── manifest.json          # 插件元数据配置
├── plugin.py              # 插件入口类和 reminder 注入
├── config.py              # 配置类定义
├── service.py             # 日记管理服务（核心逻辑）
├── tool.py                # 读取日记工具
├── action.py              # 写日记动作（强制先读后写）
├── event_handler.py       # 自动写日记事件处理器
└── __init__.py            # 包初始化
```

### 2.2 文件说明

| 文件 | 行数 | 职责 |
|------|------|------|
| `manifest.json` | 38 行 | 插件元数据、组件声明、依赖关系 |
| `__init__.py` | 8 行 | 包文档字符串 |
| `config.py` | 125 行 | 6 个配置节（plugin/storage/format/dedup/reminder/auto_diary） |
| `service.py` | 290 行 | DiaryService：文件 CRUD、去重检查、事件解析 |
| `tool.py` | 115 行 | ReadDiaryTool：读取日记入口 |
| `action.py` | 230 行 | WriteDiaryAction：写日记入口（强制先读） |
| `event_handler.py` | 317 行 | AutoDiaryEventHandler：自动写日记总结 + AutoDiarySummaryAction：独立总结动作 |
| `plugin.py` | 155 行 | DiaryPlugin：插件入口、reminder 注入 |

**总计**: 约 1400 行代码

---

## 3. 核心功能实现

### 3.1 DiaryService（服务层）

**职责**: 日记文件管理、去重检查、事件解析

**主要方法**:

| 方法 | 功能 |
|------|------|
| `read_today()` | 读取今天日记 |
| `read_date(date)` | 读取指定日期日记 |
| `append_entry(content, section)` | 追加日记条目 |
| `can_modify(date)` | 检查是否可修改 |
| `_is_duplicate(content, events)` | 重复检查（Jaccard 相似度） |
| `_parse_events(content)` | 解析事件列表 |
| `get_today_summary()` | 获取日记摘要 |

**关键代码片段**:

```python
def can_modify(self, date: str) -> tuple[bool, str]:
    """检查是否可以修改指定日期的日记。"""
    if not self._is_today(date):
        return False, "只能修改今天的日记，不能修改历史日记"
    return True, "可以修改"
```

### 3.2 ReadDiaryTool（工具层）

**职责**: 聊天中获取今天上下文的核心入口

**使用场景**:
1. 聊天开始前，了解今天已发生的事件
2. 用户询问"今天过得怎么样"时
3. 写日记之前，先读取已有内容

**接口**:
```python
async def execute(
    self,
    date: str | None = None,  # 支持 'today' 快捷方式
    format: Literal["full", "summary"] = "full",
) -> tuple[bool, str | dict]:
```

### 3.3 WriteDiaryAction（动作层）

**职责**: 写日记动作，强制先读后写

**核心流程**:
```
1. 内容验证 → 2. 调用 read_diary() → 3. 检查重复 → 4. 调用 Service 写入
```

**强制先读后写实现**:
```python
async def execute(self, content: str, section: str, mood: str | None = None):
    # 步骤 1: 先读取今天日记（强制先读后写）
    read_ok, read_result = await self._read_today()
    if not read_ok:
        return False, f"读取今天日记失败：{read_result}"

    # 步骤 2: 检查重复
    dedup_result = self._check_duplicate(content, existing_events)
    if dedup_result["is_duplicate"]:
        return False, f"今天已经记录过类似内容了：{similar}"

    # 步骤 3: 调用 Service 写入
    success, message = service.append_entry(content=content, section=section)
    return success, message
```

### 3.4 DiaryPlugin（插件入口）

**职责**: 插件注册、组件管理、reminder 注入

**System Reminder 内容**:
```
## 📔 关于写日记

### 核心规则（必须遵守）

1. 写日记前必须先读日记
   - 在调用 write_diary() 之前，必须先调用 read_diary(date="today")

2. 聊天中主动读日记
   - 当用户问你"今天过得怎么样"时
   - 当开始一段新的对话时
   → 主动调用 read_diary(date="today")

3. 日记内容规范
   - 使用第一人称"我"来记录
   - 按时间顺序记录
```

---

## 4. 配置系统

### 4.1 配置节

| 配置节 | 说明 | 默认值 |
|--------|------|--------|
| `plugin` | 插件主配置 | enabled=true |
| `storage` | 存储路径配置 | base_path="data/diaries" |
| `format` | 日记格式配置 | enable_header=true |
| `dedup` | 去重配置 | threshold=0.8 |
| `reminder` | Reminder 配置 | bucket="actor" |
| `auto_diary` | 自动写日记配置 | enabled=true, threshold=5, summary_count=10 |

### 4.2 配置示例

```toml
# config/plugins/diary_plugin/config.toml

[plugin]
enabled = true
inject_system_prompt = true

[storage]
base_path = "data/diaries"
date_format = "%Y-%m"
file_format = "%Y-%m-%d.md"

[format]
enable_header = true
enable_section = true
time_format = "%H:%M"
default_section = "其他"

[dedup]
enabled = true
similarity_threshold = 0.8
min_content_length = 5

[reminder]
bucket = "actor"
name = "关于写日记"
custom_instructions = ""

[auto_diary]
enabled = true
message_threshold = 5              # 触发自动总结的消息数量阈值
summary_message_count = 10         # 总结最近 N 条对话
auto_summary = true                # 是否启用自动总结模式
```

---

## 5. 日记存储格式

### 5.1 目录结构

```
data/diaries/
├── 2026-03/
│   ├── 2026-03-18.md
│   └── 2026-03-19.md
├── 2026-04/
│   └── 2026-04-01.md
└── ...
```

### 5.2 日记文件格式

```markdown
# 日记：2026-03-18

**[09:30]** 今天起床后天气很好，去公园散步了

**[10:15]** 在公园遇到了一只可爱的小猫

**[14:00]** 和朋友聊了很多关于未来的计划，很开心

**[20:00]** 回顾今天，感觉收获满满
```

---

## 6. 使用流程示例

### 6.1 场景一：聊天中获取上下文

```
用户："早啊！你今天过得怎么样？"

模型（内心 OS）：
  "她问我今天过得怎么样，我需要先看看日记里记录了什么"
  → 调用 read_diary(date="today")

Tool 返回:
  {
    "date": "2026-03-18",
    "events": [
      {"timestamp": "09:30", "content": "起床后天气很好，去公园散步了"},
      {"timestamp": "10:15", "content": "在公园遇到了一只可爱的小猫"}
    ]
  }

模型（现在知道了今天发生的事）：
  "早上好！我今天过得挺好的~ 早上天气很好，我去公园散步了，
   还遇到了一只可爱的小猫！你呢，今天怎么样？"
```

### 6.2 场景二：写日记（强制先读后写）

```
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
```

---

## 7. 边界情况处理

| 场景 | 处理方式 |
|------|----------|
| 尝试修改昨天日记 | 拒绝，返回"只能修改今天的日记" |
| 今天日记文件不存在 | read 返回空结构，write 自动创建 |
| 月份目录不存在 | 自动创建 |
| 内容重复 | 拒绝，返回"已记录过类似内容" |
| 同时写入 | 文件追加模式避免冲突 |
| read 失败 | write 拒绝执行 |

---

## 8. 技术亮点

### 8.1 写前必读机制

WriteDiaryAction 中强制执行先读后写：
1. 调用 read_diary() 获取已有内容
2. 解析事件列表
3. Jaccard 相似度检查重复
4. 确认不重复后才写入

### 8.2 去重算法

使用 Jaccard 相似度进行文本比对：
```python
def _calculate_similarity(text1, text2):
    set1 = set(text1.lower())
    set2 = set(text2.lower())
    intersection = len(set1 & set2)
    union = len(set1 | set2)
    return intersection / union if union > 0 else 0.0
```

### 8.3 事件解析

正则表达式匹配时间戳格式：
```python
pattern = r"\*\*\[(\d{2}:\d{2})\]\*\*\s*(.+?)(?=\n\*\*\[|\Z)"
```

### 8.4 时间段自动分类

根据时间戳自动归类到上午/下午/晚上：
- 5:00-12:00 → 上午
- 12:00-18:00 → 下午
- 18:00-23:00 → 晚上
- 其他 → 其他

---

## 9. 符合的规范

### 9.1 Neo-MoFox 插件规范

| 规范 | 实现情况 |
|------|----------|
| 名称一致性 | manifest.name = plugin_name = "diary_plugin" |
| @register_plugin | 已使用 |
| get_components() 返回类 | 正确返回类列表 |
| 配置放入 configs | DiaryConfig 在 configs 中声明 |
| 组件名称属性 | 正确定义 tool_name/action_name/service_name |
| 依赖声明 | include 字段与 get_components() 一致 |
| 导入路径 | 使用 src.app.plugin_system.base/api/types |
| 类型注解 | 所有参数和返回值有注解 |
| 文档字符串 | 文件、类、函数都有 docstring |

### 9.2 代码质量标准

- PEP 8 风格指南合规
- 类型注解完整
- 文档字符串完整
- 无 fallback 滥用
- 使用 task_manager（如需后台任务）

---

## 10. 未实现的功能（后续扩展）

以下功能在设计文档中提到，但本次实现未包含：

1. **新对话开始自动提醒读日记** - 可在 EventHandler 中扩展 `ON_MESSAGE_RECEIVED` 事件
2. **日记头部信息** - 星期、天气、心情等基本信息头
3. **向量相似度去重** - 当前使用 Jaccard 相似度，可升级为向量相似度
4. **日记回顾** - 定期提醒回顾 N 天前的日记
5. **导出功能** - 导出为 PDF/HTML
6. **多角色日记** - 支持不同 actor 人设的独立日记

---

## 11. 测试建议

### 11.1 功能测试

```bash
# 1. 测试读取今天日记（空文件）
read_diary(date="today")

# 2. 测试写入日记
write_diary(content="测试内容", section="上午")

# 3. 测试重复检查
write_diary(content="测试内容", section="上午")  # 应拒绝

# 4. 测试日期隔离
write_diary(content="测试", section="其他", date="2026-03-17")  # 应拒绝
```

### 11.2 集成测试

1. 启动 Neo-MoFox 主程序
2. 加载 diary_plugin
3. 验证 system_reminder 是否注入
4. 通过对话测试读/写日记功能

---

## 12. 总结

本次实现的日记插件完整满足需求规格：

1. **日记存储** - Markdown 格式，按日期组织目录
2. **第一人称** - 通过 system_reminder 引导
3. **时间性** - 每条记录带时间戳
4. **日期隔离** - 只能修改今天日记
5. **写前必读** - 强制执行先读后写
6. **聊天上下文** - ReadDiaryTool 提供上下文获取
7. **自动写日记** - EventHandler 订阅对话事件，达到阈值自动调用 LLM 总结对话并写入日记，使用与 actor 相同的上下文确保一致性

插件遵循 Neo-MoFox 规范，代码质量良好，后续可根据需要扩展功能。

---

**报告结束**
