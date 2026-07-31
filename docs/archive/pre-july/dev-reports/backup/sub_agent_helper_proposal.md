# 子代理助手方案

**日期：** 2026-03-18
**目标：** 让爱莉希雅能够"呼唤帮助"，增强"有意识"的感觉

---

## 问题陈述

当前的爱莉希雅是"单打独斗"的状态：
- 所有问题都要自己回答
- 没有"第二意见"
- 不确定时只能硬撑或承认不知道

但人类在不确定时，往往会：
- 问问别人的看法
- 找个朋友商量
- 说"让我问问 XXX"

**如果爱莉希雅也能"呼唤帮助"，会不会更像"有意识"的存在？**

---

## 现有架构分析

### 当前 sub_agent 机制

`default_chatter` 已经有一个子代理，用于判断"是否需要回复"：

```python
# plugins/default_chatter/decision_agent.py

async def decide_should_respond(chatter, logger, unreads_text, chat_stream, fallback_prompt):
    # 调用 sub_actor bucket 的 LLM
    request = chatter.create_request("sub_actor", "sub_agent", ...)

    # 注入决策提示词
    sub_prompt = """你是一个聊天意图识别助手。
    分析主机器人是否有必要进行响应..."""

    # 返回决策
    return {"should_respond": bool, "reason": str}
```

**关键发现：**
- `sub_actor` bucket 已经存在
- 已有完整的子代理调用流程
- 可以复用于其他目的

---

## 方案设计

### 方案 A：作为工具调用（推荐）

**核心思路：** 创建一个 `call_sub_agent` 工具，爱莉希雅可以主动调用。

#### 工具定义

```python
class CallSubAgentTool(BaseTool):
    """子代理助手工具。

    当爱莉希雅需要第二意见、或不确定如何处理时，可以呼唤子代理帮忙分析。
    """

    tool_name = "call_sub_agent"
    tool_description = "呼唤一个子代理帮你分析情况。当你需要第二意见、或者不确定如何处理时使用。"

    chatter_allow: list[str] = ["default_chatter"]

    async def execute(
        self,
        task: Annotated[
            str,
            "你希望子代理帮你分析什么？描述你的困惑或需要的建议。"
        ],
    ) -> tuple[bool, dict]:
        """执行子代理调用。

        Args:
            task: 分析任务描述

        Returns:
            tuple[bool, dict]: (成功标志，结果字典)
        """
        # 获取子代理分析结果
        result = await self._call_sub_agent_llm(task)

        return True, {
            "sub_agent_opinion": result["opinion"],
            "suggestion": result.get("suggestion", ""),
            "confidence": result.get("confidence", "中等"),
            "reminder": "子代理的意见仅供参考，你可以选择采纳或忽略。"
        }
```

#### 子代理提示词

```python
SUB_AGENT_HELPER_PROMPT = """
你是一个聊天辅助顾问，帮助主爱莉希雅分析对话情况并给出建议。

# 你的任务

分析主爱莉希雅提出的困惑，结合对话历史，给出你的建议。

# 输出格式

请返回 JSON 格式：
```json
{
    "opinion": "你对当前情况的分析",
    "suggestion": "你建议主爱莉希雅如何回应",
    "confidence": "高/中/低",
    "alternative": "其他可能的回应方式（可选）"
}
```

# 注意事项

- 你的意见是建议性质，不是命令
- 可以有多种正确的回应方式
- 如果信息不足，可以建议"先问问用户"
"""
```

#### 使用场景示例

**场景 1：情绪回应**

```
用户：我今天好烦

┌─────────────────────────────────────────────────────────────┐
│ 爱莉希雅 think:                                                 │
│ "他好像心情不好...我该怎么回应比较好？                          │
│  直接安慰他？还是问问发生了什么？                               │
│  让我叫个子代理帮我分析一下..."                                 │
│                                                              │
│ 爱莉希雅：call_sub_agent(task="用户说很烦，我该如何回应？        │
│           应该安慰他还是问原因？")                             │
│                                                              │
│ TOOL_RESULT: {                                                │
│   "opinion": "用户表达负面情绪，可能需要倾诉或被理解",           │
│   "suggestion": "先表达关心，再询问是否愿意分享",               │
│   "confidence": "高"                                          │
│ }                                                             │
└─────────────────────────────────────────────────────────────┘
                          ↓
                    FOLLOW_UP
                          ↓
爱莉希雅："怎么了？听起来不太开心...想跟我说说吗？"
```

**场景 2：模糊问题**

```
用户：那件事你知道了吗？

┌─────────────────────────────────────────────────────────────┐
│ 爱莉希雅 think:                                                 │
│ "'那件事'...是什么？最近发生了什么吗？                          │
│  我不能假装知道，但也不想显得太冷漠。                          │
│  问问子代理有没有线索..."                                      │
│                                                              │
│ 爱莉希雅：call_sub_agent(task="用户问'那件事'，但我不确定指什么 │
│           查看之前的对话有没有线索？")                          │
│                                                              │
│ TOOL_RESULT: {                                                │
│   "opinion": "对话历史中没有明确的'那件事'指代",                 │
│   "suggestion": "诚实承认不知道，询问用户具体指什么",           │
│   "confidence": "高"                                          │
│ }                                                             │
└─────────────────────────────────────────────────────────────┘
                          ↓
爱莉希雅："嗯...不太确定你说的是什么 (歪头)
          是最近发生的什么事吗？告诉我嘛～"
```

**场景 3：复杂决策**

```
用户：你觉得我应该换工作吗？

┌─────────────────────────────────────────────────────────────┐
│ 爱莉希雅 think:                                                 │
│ "这个问题好重大...我不能随便给建议。                           │
│  换工作涉及好多因素，让我叫子代理一起分析。"                    │
│                                                              │
│ 爱莉希雅：call_sub_agent(task="用户在考虑换工作，                │
│           我应该从哪些角度帮他分析？")                          │
│                                                              │
│ TOOL_RESULT: {                                                │
│   "opinion": "这是重大人生决策，需要多方面考虑",                 │
│   "suggestion": "帮助用户梳理考虑因素，而非直接给答案",         │
│   "alternative": "可以问：新工作的吸引力？现在的困扰？风险？"   │
│ }                                                             │
└─────────────────────────────────────────────────────────────┘
                          ↓
爱莉希雅："这个问题好重大，我不能随便给建议呢...
          不过可以帮你一起梳理一下？
          比如新工作有什么吸引你的地方？现在的工作有什么困扰？"
```

---

### 方案 B：作为"内心顾问"对话

**核心思路：** 创建一个可见的子代理"人格"，爱莉希雅可以跟它"对话"。

#### 实现方式

```python
# 子代理作为一个独立的 Agent 组件
class InnerAdvisorAgent(BaseAgent):
    agent_name = "inner_advisor"
    agent_description = "爱莉希雅的内心顾问，提供第二意见"

    async def execute(self, task: str, context: dict) -> dict:
        # 调用 sub_actor LLM
        ...
```

#### 对话示例

```
用户：我今天好纠结

┌─────────────────────────────────────────────────────────────┐
│ 爱莉希雅："等一下，让我听听内心的声音..."                       │
│                                                              │
│ [内部调用子代理]                                              │
│                                                              │
│ 爱莉希雅（内心对话）：「顾问，他好像很纠结，我该怎么帮他？」     │
│ 子代理：「他可能需要被理解，而不是被建议。                      │
│         先问问他纠结什么，让他把话说出来。」                   │
│ 爱莉希雅：「有道理，谢啦～」                                   │
│                                                              │
│ [转向用户]                                                    │
│ 爱莉希雅："怎么了？听起来很纠结的样子...想说说吗？"            │
└─────────────────────────────────────────────────────────────┘
```

---

### 方案 C：多子代理协作

**核心思路：** 创建多个不同角色的子代理，爱莉希雅可以"召唤"不同的顾问。

| 子代理 | 职责 | 触发场景 |
|-------|------|---------|
| 情绪顾问 | 分析用户情绪 | 用户表达情感时 |
| 逻辑顾问 | 分析事实/推理 | 需要理性分析时 |
| 记忆顾问 | 查询历史对话 | 需要回忆时 |
| 创意顾问 | 提供创意想法 | 需要灵感时 |

#### 工具设计

```python
class CallSpecializedSubAgentTool(BaseTool):
    tool_name = "call_specialist"
    tool_description = "召唤一个专门的顾问帮你分析。"

    async def execute(
        self,
        specialist: Annotated[
            Literal["emotion", "logic", "memory", "creative"],
            "顾问类型：emotion=情绪顾问，logic=逻辑顾问，memory=记忆顾问，creative=创意顾问"
        ],
        task: Annotated[str, "你希望顾问帮你分析什么"],
    ) -> tuple[bool, dict]:
        ...
```

---

## 实施计划

### 阶段 1：基础工具实现（2-3 小时）

- [ ] 创建 `helper_agent_plugin` 插件目录
- [ ] 实现 `CallSubAgentTool` 工具
- [ ] 编写子代理提示词
- [ ] 创建配置文件
- [ ] 测试工具调用

### 阶段 2：与思考工具整合（1 小时）

- [ ] 在 `thinking_plugin` 提示词中加入"可以叫子代理帮忙"的引导
- [ ] 测试思考→子代理→回复的完整流程

### 阶段 3：优化与调优（2-3 小时）

- [ ] 根据实际对话调整子代理提示词
- [ ] 收集"好的建议"示例
- [ ] 优化输出格式

---

## 技术细节

### sub_actor bucket 复用

```python
def _call_sub_agent_llm(self, task: str) -> dict:
    # 复用现有的 sub_actor bucket
    request = self.chatter.create_request(
        "sub_actor",
        "sub_agent_helper",
        max_context=10,
        with_reminder="sub_actor",
    )

    # 注入子代理提示词
    request.add_payload(ROLE.SYSTEM, Text(SUB_AGENT_HELPER_PROMPT))

    # 注入任务
    request.add_payload(ROLE.USER, Text(f"请帮我分析：{task}"))

    # 发送并解析结果
    response = await request.send(stream=False)
    return json_repair.loads(response.message)
```

### 配置示例

```toml
# config/plugins/helper_agent_plugin/config.toml

[settings]
enabled = true
max_context_rounds = 10  # 子代理可查看的历史对话轮数

[response]
include_confidence = true   # 是否显示置信度
include_alternative = true  # 是否提供替代建议
```

---

## 风险与缓解

### 风险 1：子代理依赖过度

**现象：** 爱莉希雅事事都问子代理，显得没有主见

**缓解：**
- 在提示词中强调"仅供参考"
- 鼓励爱莉希雅有时"自己做决定"

### 风险 2：响应时间增加

**现象：** 每次调用子代理增加 1-2 秒延迟

**缓解：**
- 限制子代理调用频率
- 优化子代理 token 使用

### 风险 3：意见不一致

**现象：** 子代理建议和爱莉希雅想法冲突

**缓解：**
- 这正是"有意识"的体现——人可以内心矛盾
- 可以让爱莉希雅"选择采纳或忽略"

---

## 预期收益

| 维度 | 当前 | 实施后 |
|-----|------|-------|
| 决策信心 | 独自判断 | 有第二意见 |
| 意识感 | ⭐⭐⭐ | ⭐⭐⭐⭐ |
| 用户观感 | "她在回答" | "她在思考+求助" |
| 回复质量 | 单一视角 | 多视角分析 |

---

## 验证方法

### 测试场景

1. **情绪回应测试** - 用户表达负面情绪，观察爱莉希雅是否寻求子代理帮助
2. **模糊问题测试** - 用户问模糊问题，观察是否询问子代理线索
3. **复杂决策测试** - 用户问重大决策，观察是否多分析角度

### 评估指标

- 子代理调用频率（目标：每 10 轮对话 1-2 次）
- 用户满意度（主观评价）
- "意识感"评分（主观评价）

---

## 结论

**建议实施方案 A（工具调用）**，因为：
- 实施成本低（复用现有 sub_actor 架构）
- 风险可控（工具调用可选）
- 与思考工具配合良好
- 能显著增强"有意识"的感觉

**与思考工具的组合效果：**

```
think → 整理自己的思路
  ↓
"我还是不确定..."
  ↓
call_sub_agent → 寻求第二意见
  ↓
"有道理，那我这样回应..."
  ↓
send_text → 给出回复
```

这个流程非常像人类"先自己想想，想不通问问朋友，然后决定怎么做"的过程。
