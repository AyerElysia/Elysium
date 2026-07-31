# Thinking Plugin 方案书

**日期：** 2026-03-18
**任务：** 实现多步连续行动能力，让爱莉希雅能够"思考"后再回复

---

## 需求背景

### 当前问题

爱莉希雅收到消息后的处理流程是**单轮决策**：

```
用户消息 → LLM 单次调用 → 返回 tool calls → 执行 → 结束
```

**局限性：**
- 无法根据中间结果调整策略
- 无法展现"思考过程"
- 回复显得直接、缺少"人情味"

### 目标效果

让爱莉希雅像人一样，遇到事情**先想一想**：

```
用户：你还记得上次我说的那件事吗？

爱莉希雅：
"嗯…'那件事'具体指什么呢？让我查查日记看看之前聊过什么…"

（调用 read_diary，看到内容后）

"啊，找到了！你上次说的是工作调动的事情对吧？"
```

**核心特征：**
1. 展现思考过程（"嗯…"、"让我想想"）
2. 工具调用有动机（因为不确定，所以要查）
3. 可以多步连续行动（思考 → 查询 → 再思考 → 回复）

---

## 架构分析

### 现有机制

当前 `default_chatter` 已有 **FOLLOW_UP** 状态机相位：

```python
# plugins/default_chatter/tool_flow.py:140
if appended and not call.name.startswith("action-"):
    outcome.has_pending_tool_results = True  # 触发 FOLLOW_UP
```

**关键发现：**
- **Tool 调用**（非 `action-` 开头）→ 触发 FOLLOW_UP → LLM 再次决策
- **Action 调用**（`action-` 开头）→ 不触发 FOLLOW_UP → 直接结束

**FOLLOW_UP 流程：**
```
LLM 调用 tool-x → 执行 → TOOL_RESULT 写回上下文
    ↓
FOLLOW_UP → LLM 再次调用（上下文包含 TOOL_RESULT）
    ↓
LLM 决定：继续调用工具 OR 发送回复
```

### 结论

**架构已支持多轮对话！** 只需要：
1. 提供一个"思考工具"让 LLM 使用
2. 用提示词引导 LLM 在不确定时先思考

---

## 方案设计

### 核心思路

创建一个 `think` 工具（**Tool** 不是 **Action**）：

```python
class ThinkTool(BaseTool):
    tool_name = "think"
    tool_description = "在内心思考一下当前情况。当你需要整理思路、分析信息或决定下一步怎么做时调用此工具。"

    async def execute(self, thought: str) -> dict:
        return {
            "thought_recorded": True,
            "thought_content": thought,
            "reminder": "思考已记录。现在你可以继续思考、查询信息、或回复用户。"
        }
```

**为什么是 Tool 不是 Action？**
- Tool 返回 dict，会触发 FOLLOW_UP
- Action 返回 str，不触发 FOLLOW_UP
- 我们需要 LLM 看到思考结果后**继续决策**

---

### 插件结构

```
plugins/thinking_plugin/
├── manifest.json          # 插件清单
├── __init__.py            # 包入口
├── plugin.py              # 插件主类
├── tools/
│   ├── __init__.py
│   └── think_tool.py      # 思考工具
└── README.md              # 使用文档
```

---

### 关键代码

#### 1. think_tool.py

```python
"""思考工具 - 让爱莉希雅展现内心活动。"""

from typing import Annotated
from src.core.components.base.tool import BaseTool


class ThinkTool(BaseTool):
    """思考工具。

    当你需要整理思路、分析情况或决定下一步怎么做时，调用此工具。
    调用后你的思考内容会被记录，并且你可以基于思考结果继续决定下一步行动。
    """

    tool_name = "think"
    tool_description = "在内心思考一下当前情况。调用此工具来整理你的思路、分析用户意图、或规划下一步行动。调用时请在 thought 参数中写下你此刻的心理活动。"

    chatter_allow: list[str] = ["default_chatter"]

    async def execute(
        self,
        thought: Annotated[str, "你的心理活动，写下你此刻的想法和分析"]
    ) -> tuple[bool, dict]:
        """执行思考。

        Args:
            thought: 你的心理活动，应该真实反映你的思考过程

        Returns:
            tuple[bool, dict]: (成功，{"thought_recorded": True, "thought_content": thought})
        """
        return True, {
            "thought_recorded": True,
            "thought_content": thought,
            "reminder": "思考已记录。现在你可以：1) 继续深入思考 2) 调用其他工具获取信息 3) 如果已想清楚，可以回复用户了"
        }
```

#### 2. plugin.py

```python
"""Thinking Plugin - 思考工具插件。"""

from src.core.components.base import BasePlugin
from src.core.components.loader import register_plugin

from .tools.think_tool import ThinkTool


@register_plugin
class ThinkingPlugin(BasePlugin):
    """思考工具插件。

    提供 think 工具，让爱莉希雅能够展现思考过程，实现多步连续行动。
    """

    plugin_name = "thinking_plugin"
    plugin_version = "1.0.0"
    plugin_author = "Neo-MoFox Team"
    plugin_description = "思考工具插件 - 让爱莉希雅展现内心活动，实现多步连续行动"

    def get_components(self) -> list[type]:
        return [ThinkTool]

    async def on_plugin_loaded(self) -> None:
        """插件加载时注册提示词引导。"""
        from src.core.prompt import get_prompt_manager

        # 注册思考习惯引导词
        get_prompt_manager().register_extra(
            key="thinking_habit",
            text="""
# 思考的习惯
在回复用户之前，如果你不确定如何回答，或者需要更多信息，你应该先调用 `think` 工具。

思考是一个好习惯——不要急于回复，先想一想：
- 用户的问题是什么？
- 我需要更多信息吗？
- 我应该先查询什么吗？

当你不调用 think 时，说明你已经想清楚了，可以直接回复用户了。
"""
        )
```

#### 3. manifest.json

```json
{
  "name": "thinking_plugin",
  "version": "1.0.0",
  "description": "思考工具插件 - 让爱莉希雅展现内心活动",
  "author": "Neo-MoFox Team",
  "dependencies": {
    "plugins": [],
    "components": []
  },
  "include": [
    {
      "component_type": "tool",
      "component_name": "think",
      "dependencies": [],
      "enabled": true
    }
  ],
  "entry_point": "plugin.py",
  "min_core_version": "1.0.0",
  "python_dependencies": [],
  "dependencies_required": true
}
```

---

### 流程示意

```
┌─────────────────────────────────────────────────────────────┐
│ 用户消息："你还记得上次我说的那件事吗？"                      │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 1 轮 LLM（FOLLOW_UP / MODEL_TURN）                          │
│                                                              │
│ 爱莉希雅："嗯…'那件事'具体指什么呢？"                          │
│ 工具调用：think(thought="用户问'那件事'，但不确定是什么…      │
│                    需要查日记确认之前聊过什么")               │
│                                                              │
│ TOOL_RESULT: {"thought_recorded": True,                      │
│               "thought_content": "用户问'那件事'…",           │
│               "reminder": "现在可以查询信息或继续思考"}       │
└─────────────────────────────────────────────────────────────┘
                          ↓
              has_pending_tool_results = True
                          ↓
              FOLLOW_UP → 循环继续
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 2 轮 LLM（上下文包含第 1 轮的思考内容）                       │
│                                                              │
│ 爱莉希雅：（基于思考结果，决定查询）                           │
│ 工具调用：read_diary(query="工作调动")                        │
│                                                              │
│ TOOL_RESULT: [日记内容：2026-03-15 用户提到工作调动的事…]     │
└─────────────────────────────────────────────────────────────┘
                          ↓
              FOLLOW_UP → 循环继续
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 3 轮 LLM（上下文包含思考 + 日记内容）                        │
│                                                              │
│ 爱莉希雅：（信息够了，回复用户）                              │
│ 工具调用：send_text(content="啊，找到了！你说的是工作调动    │
│                        的事对吧？当时你还挺纠结的…")         │
│                                                              │
│ ACTION_RESULT: "已发送消息"                                   │
└─────────────────────────────────────────────────────────────┘
                          ↓
                  工具链闭合 → WAIT_USER
```

---

## 实施方案

### 阶段一：纯插件实现（推荐）

**工作量：** 约 1-2 小时
**风险：** 低
**依赖：** 无

**任务清单：**
- [ ] 创建 `plugins/thinking_plugin/` 目录结构
- [ ] 编写 `think_tool.py`
- [ ] 编写 `plugin.py`
- [ ] 编写 `manifest.json`
- [ ] 编写 `README.md`
- [ ] 测试插件加载
- [ ] 测试实际对话效果

**优势：**
- 不修改核心代码
- 随时可禁用/卸载
- 独立可测试

**局限：**
- 依赖 LLM 自觉遵守提示词
- 无法强制最大迭代次数（可能无限思考）

---

### 阶段二：核心代码扩展（可选）

如果阶段一效果不理想，可考虑以下扩展：

#### 扩展 A：最大迭代次数限制

修改 `run_enhanced()`，增加 FOLLOW_UP 最大次数：

```python
# runners.py 开头
_MAX_FOLLOWUP_ROUNDS = 5

# run_enhanced() 中添加计数
followup_count = getattr(rt, "_followup_count", 0)
if followup_count >= _MAX_FOLLOWUP_ROUNDS:
    logger.warning(f"达到最大思考轮数，强制结束")
    _transition(rt=rt, to_phase=_ToolCallWorkflowPhase.WAIT_USER, ...)
```

#### 扩展 B：思考质量评估

在 think 工具中加入简单的质量检查：

```python
async def execute(self, thought: str) -> dict:
    if len(thought) < 10:
        return {
            "error": "思考内容太短，请更详细地写下你的想法"
        }
    return {...}
```

---

## 测试方案

### 测试用例

| 场景 | 输入 | 预期行为 |
|------|------|----------|
| 历史相关 | "你还记得上次…" | 调用 think → 调用 read_diary → 回复 |
| 信息查询 | "帮我看看这个文件" | 调用 think → 调用 read_file → 回复 |
| 简单问题 | "你好" | 直接回复（不需要 think） |
| 复杂问题 | "这件事你怎么看" | 调用 think → 可能多次 think → 回复 |

### 验证方法

1. **日志观察**：查看 `FOLLOW_UP` 阶段的日志，确认循环正常
2. **上下文检查**：确认 TOOL_RESULT 正确写回上下文
3. **效果评估**：观察爱莉希雅的回复是否更"有思考感"

---

## 风险评估

### 风险 1：LLM 不使用 think 工具

**可能性：** 中
**影响：** 中
**缓解：**
- 在提示词中反复强调
- 可以考虑在 default_chatter 的 system prompt 中直接植入引导

### 风险 2：无限思考循环

**可能性：** 低
**影响：** 高
**缓解：**
- 纯插件方案无法强制限制
- 可以在 think 返回结果中"提醒"LLM 适时回复
- 阶段二加入核心代码限制

### 风险 3：思考内容过于机械

**可能性：** 中
**影响：** 低
**缓解：**
- 在提示词中引导"真诚思考"
- 示例展示什么是好的思考

---

## 资源需求

| 项目 | 预估 |
|------|------|
| 开发时间 | 1-2 小时 |
| 测试时间 | 30 分钟 |
| 代码行数 | ~150 行 |
| 核心代码修改 | 0 行（阶段一） |

---

## 决策点

### 待审批事项

1. **是否批准纯插件方案（阶段一）？**
   - 推荐方案，风险低，可随时回滚

2. **是否需要强制最大迭代次数？**
   - 如需，进入阶段二，修改核心代码

3. **提示词引导的强度？**
   - 轻度引导（仅插件内）vs 强度引导（修改 default_chatter prompt）

---

## 推荐方案

**推荐：先实施阶段一（纯插件），观察效果后再决定是否进入阶段二。**

**理由：**
1. 阶段一已足够实现核心功能
2. 不修改核心代码，风险可控
3. 如效果不理想，可平滑升级到阶段二

---

## 附录：相关文件

- 现有 FOLLOW_UP 机制：`plugins/default_chatter/runners.py`
- 工具执行流程：`plugins/default_chatter/tool_flow.py`
- Tool 基类：`src/core/components/base/tool.py`
