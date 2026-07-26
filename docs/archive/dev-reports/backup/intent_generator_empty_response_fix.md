# 意图生成器空响应问题修复报告

**日期：** 2026-03-19
**状态：** 已完成
**问题：** 意图生成 LLM 返回空响应（message=None），无法生成有效意图

---

## 问题描述

意图生成器在运行时持续返回空响应：
```
[17:51:02] intent_plugin | INFO | 意图生成 LLM 返回空响应 - message=None, call_list=[]
```

---

## 根因分析

### 根本原因：`send()` 默认流式模式 (`stream=True`) 未正确收集响应

`LLMResponse` 的行为：
- **流式模式 (`stream=True`)**：`message` 初始为 `None`，需要通过 `await response` 或 `async for chunk in response` 触发 `_collect_full_response()` 或 `__aiter__()` 来填充 `message`
- **非流式模式 (`stream=False`)**：`message` 在 `send()` 返回时已填充

**意图生成的调用方式：**
```python
# 错误：使用默认 stream=True，但没有 await response 收集内容
response = await llm_request.send()
content = response.message  # None!
```

**对比：media_manager.py 的正确用法：**
```python
# 正确：使用 stream=False，message 直接可用
response = await request.send(stream=False)
description = response.message.strip()
```

### 主回复 vs 意图生成

| 差异点 | 主回复 (default_chatter) | 意图生成 (修复前) |
|--------|-------------------------|------------------|
| 创建方式 | `chatter.create_request("actor", ...)` | `LLMRequest(model_set, "intent_generation")` |
| SYSTEM payload | ✅ chatter system prompt | ❌ **缺失** → 已修复 |
| ContextManager | ✅ 自动创建 | ✅ 已修复 |
| **send() 模式** | ✅ 通过 chatter 内部处理 | ❌ **stream=True 未收集** |

---

## 修复方案

### 修复 1：添加 SYSTEM prompt（已完成）

新增 `INTENT_GENERATOR_SYSTEM_PROMPT` 常量并添加到请求中。

### 修复 2：使用非流式模式

修改 `generate_intents` 方法：
```python
# 修复前
response = await llm_request.send()

# 修复后
response = await llm_request.send(stream=False)
```

---

## 文件修改清单

### `plugins/intent_plugin/intent_generator.py`

**1. 导入 `LLMContextManager`：**
```python
from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text, LLMContextManager
```

**2. 新增 SYSTEM prompt 常量：**
```python
INTENT_GENERATOR_SYSTEM_PROMPT = """...""".strip()
```

**3. 修改 `generate_intents` 方法：**
- 创建 `LLMContextManager`
- 添加 SYSTEM payload
- 添加 USER payload
- **使用 `stream=False` 模式**

```python
context_manager = LLMContextManager(max_payloads=20)
llm_request = LLMRequest(
    model_set=model_set,
    request_name="intent_generation",
    context_manager=context_manager,
)
llm_request.add_payload(LLMPayload(ROLE.SYSTEM, Text(INTENT_GENERATOR_SYSTEM_PROMPT)))
llm_request.add_payload(LLMPayload(ROLE.USER, Text(prompt)))
response = await llm_request.send(stream=False)  # ← 关键修复
content = response.message or ""
```

---

## 验证方法

1. **重启 Bot**
   ```bash
   # 停止 Bot
   Ctrl+C

   # 重新启动
   python -m src.app.main
   ```

2. **观察日志**
   ```
   [DEBUG] intent_plugin | 意图生成使用模型集：[...]
   [DEBUG] intent_plugin | 意图生成 LLM 响应：[...]
   [INFO] intent_plugin | LLM 生成了 X 个意图候选
   ```

3. **预期结果**
   - LLM 不再返回空响应
   - 成功解析出 1-3 个意图候选
   - 意图包含完整的 id、name、description、category、base_priority、goal_objective 字段

---

## 技术总结

### 关键教训

**LLM 请求必须包含 SYSTEM prompt 来定义角色和任务！**

主回复的成功不仅是因为使用了正确的模型，更因为有完整的 prompt 结构：
1. SYSTEM prompt - 定义角色（爱莉希雅）
2. USER prompt - 具体任务
3. TOOL payload - 可用工具

意图生成虽然不需要 TOOL payload，但 SYSTEM prompt 是必需的。

### 修复前后对比

| 项目 | 修复前 | 修复后 |
|------|--------|--------|
| SYSTEM payload | ❌ 缺失 | ✅ 定义角色 |
| ContextManager | ❌ 隐式默认 | ✅ 显式创建 |
| send() 模式 | ❌ stream=True (默认) | ✅ stream=False |
| 请求结构 | 仅 USER payload | SYSTEM + USER |
| 响应状态 | message=None | 正常 JSON |

---

**修复完成时间：** 2026-03-19
**修复版本：** 1.2.0
