# KFC JSON 模式问题分析报告

**日期**: 2026-03-19
**插件**: kokoro_flow_chatter (KokoroFlowChatter / KFC)

---

## 问题概述

KFC 当前使用 **JSON 回复模式 + Tool Calling 混合模式**，导致 LLM 理解负担增加，经常出现"忘记输出 JSON 格式"的问题。

---

## 当前设计

### JSON 模式（主要）
```json
{
  "thought": "她好像心情不好",
  "content": ["怎么了？"],
  "expected_reaction": "可能会撒娇",
  "max_wait_seconds": 120,
  "mood": "担心"
}
```

### Tool Calling 模式（降级兜底）
```
ToolCall(name="kfc_reply", args={"thought": "...", "content": "..."})
```

### 系统提示词位置
JSON 格式要求位于 `KFC_SYSTEM_PROMPT` 第 37-64 行，距离响应生成点较远（recency bias 问题）。

---

## 核心问题

### 问题 1：LLM 遗忘 JSON 格式

**现象**：
```
── ASSISTANT ──
ToolCall(name='action-send_emoji_meme', args={...})
── ASSISTANT ──
好的。
```

LLM 输出工具调用后，下一轮只输出纯文本 "好的。"，没有 JSON 格式。

**原因**：
1. JSON 格式要求在系统提示词靠前位置，LLM 记忆不深（recency bias）
2. 工具调用和 JSON 回复被分成两轮独立响应
3. 两种模式并存增加了理解负担

**已实施的临时修复**：
- 在 `KFC_PERCEIVE_FOLLOWUP_PROMPT` 中重新注入完整 JSON 格式说明
- 当检测到空响应时触发跟进提示

---

### 问题 2：设计冗余

**核心疑问**：既然 ToolResult 和 warmup 机制能保存完整状态，为什么不做成纯 action 模式？

**当前混合模式的问题**：
| 问题 | 描述 |
|------|------|
| 认知负载 | LLM 需要理解"输出 JSON"和"调用工具"两种行为 |
| 格式混淆 | 模型不确定何时用 JSON，何时用 Tool Calling |
| 提示词复杂 | 需要大量说明解释 JSON 格式和工具调用的关系 |

---

## 建议方案：纯 Action 模式

### 设计
```python
ToolCall(name="kfc_reply", args={
    "thought": "她好像心情不好",
    "content": ["怎么了？"],
    "expected_reaction": "可能会撒娇",
    "max_wait_seconds": 120,
    "mood": "担心"
})
```

### 优势
1. **统一接口**：所有输出都是 Tool Calling，符合 LLM 训练数据
2. **减少混淆**：没有"JSON vs Tool"的选择问题
3. **提示词简化**：不需要解释 JSON 格式，只需说明工具参数
4. **连续性不受影响**：warmup 机制同样能重建 ToolCall 历史

### 连续性验证

当前连续性依赖：
```python
# 1. 保存原始响应
session.add_bot_planning(
    thought=result.thought,
    actions=result.actions,
    expected_reaction=result.expected_reaction,
    max_wait_seconds=result.max_wait_seconds,
    raw_response=getattr(response, "message", "") or "",
)

# 2. 热启动重建
for warmup_payload in self._build_warmup_payloads(...):
    request.add_payload(warmup_payload)  # ROLE.ASSISTANT
```

改成 action 后：
- `raw_response` 保存 ToolCall 文本
- warmup 重建 `ROLE.ASSISTANT: ToolCall(...)`
- **LLM 同样能看到之前的 thought、mood 等信息**

---

## 待修改文件清单

| 文件 | 修改内容 |
|------|---------|
| `prompts/templates.py` | 移除 `KFC_SYSTEM_PROMPT` 中的 JSON 格式说明，改为 action 参数说明 |
| `prompts/templates.py` | 移除 `KFC_PERCEIVE_FOLLOWUP_PROMPT` 中的 JSON 格式说明 |
| `reply_json.py` | 保留作为降级兜底（可选） |
| `chatter.py` | `_send_with_perceive_loop()` 简化逻辑 |
| `parser.py` | 优先处理 `kfc_reply` tool call，移除 JSON 优先逻辑 |
| `config.toml` | `blocked_tools` 移除 `send_text`（如果启用 kfc_reply action） |

---

## 风险评估

| 风险 | 描述 | 缓解措施 |
|------|------|---------|
| 兼容性 | 旧版 prompt 可能依赖 JSON 格式 | 保留 JSON 解析作为降级兜底 |
| 模型适配 | 某些模型可能更适应 JSON | 可配置切换模式 |
| 迁移成本 | 需要修改多个文件 | 分阶段实施，先并行后切换 |

---

## 后续行动

1. [ ] 评估 Gemini 模型在当前 JSON 模式下的表现
2. [ ] 如果仍然有问题，实施纯 action 模式改造
3. [ ] 对比两种模式的 LLM 理解率和错误率
4. [ ] 更新提示词编写指南

---

## 参考资料

- `plugins/kokoro_flow_chatter/prompts/templates.py` - 提示词模板
- `plugins/kokoro_flow_chatter/chatter.py` - 核心对话逻辑
- `plugins/kokoro_flow_chatter/parser.py` - 工具调用解析
- `plugins/kokoro_flow_chatter/session.py` - 会话状态管理
- `plugins/kokoro_flow_chatter/reply_json.py` - JSON 提取逻辑
