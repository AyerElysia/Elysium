# KFC 可用组件过滤问题排查报告

**日期**: 2026-03-18
**问题**: kokoro_flow_chatter 可用组件被错误过滤（10/12）
**状态**: ✅ 已修复

---

## 1. 问题现象

日志显示：
```
[22:33:01] chatter | INFO | [2e0bf05760fc8a32d4800bc133d5af63fb0d91c0eb6d1b7a16c3e3fc6c1a350c]
移除组件：kokoro_flow_chatter:action:kfc_reply(chatter 不匹配（允许：kokoro_flow_chatter）)
移除组件：kokoro_flow_chatter:action:do_nothing(chatter 不匹配（允许：kokoro_flow_chatter）)
可用组件：10/12
```

**问题**：
- `kfc_reply`和 `do_nothing` 两个 Action 被过滤掉了
- 过滤原因显示"chatter 不匹配"，但允许列表中明明包含 `kokoro_flow_chatter`
- 可用组件从 12 个减少到 10 个

---

## 2. 问题根源分析

### 2.1 组件过滤机制

在 `src/core/components/base/chatter.py` 的 `modify_llm_usables()` 方法中（第 258-270 行）：

```python
chatter_allow = getattr(usable_cls, "chatter_allow", [])
if chatter_allow:
    chatter_signature = self.get_signature()
    allowed = self.chatter_name in chatter_allow
    if chatter_signature and not allowed:
        allowed = chatter_signature in chatter_allow

    if not allowed:
        allow_str = ", ".join(chatter_allow)
        reason = f"chatter 不匹配（允许：{allow_str}）"
        removals.append((signature, reason))
        logger.debug(f"[移除组件] {signature}：{reason}")
        continue
```

### 2.2 核心问题：签名匹配逻辑

组件签名格式为：`plugin_name:component_type:component_name`

所以 `kokoro_flow_chatter` Chatter 的完整签名是：
```
kokoro_flow_chatter:chatter:kokoro_flow_chatter
```

过滤逻辑检查两次：
1. `self.chatter_name in chatter_allow` - 检查简称（如 `kokoro_flow_chatter`）
2. `chatter_signature in chatter_allow` - 检查完整签名（如 `kokoro_flow_chatter:chatter:kokoro_flow_chatter`）

**问题**：Action 的 `chatter_allow` 只配置了简称，没有配置完整签名！

### 2.3 受影响的组件

| 组件 | 文件 | 问题配置 | 修复 |
|------|------|----------|------|
| ThinkTool | `thinking_plugin/tools/think_tool.py` | `["default_chatter"]` | ✅ 添加 `kokoro_flow_chatter` |
| QueryTimeTool | `time_awareness_plugin/tools/query_time.py` | `["default_chatter"]` | ✅ 添加 `kokoro_flow_chatter` |
| KFCReplyAction | `kokoro_flow_chatter/actions/reply.py` | `["kokoro_flow_chatter"]` | ✅ 添加完整签名 |
| DoNothingAction | `kokoro_flow_chatter/actions/do_nothing.py` | `["kokoro_flow_chatter"]` | ✅ 添加完整签名 |

---

## 3. 修复方案

### 3.1 修复 ThinkTool

**文件**: `plugins/thinking_plugin/tools/think_tool.py`

**修复**:
```python
# 修改前
chatter_allow: list[str] = ["default_chatter"]

# 修改后
chatter_allow: list[str] = ["default_chatter", "kokoro_flow_chatter"]
```

### 3.2 修复 QueryTimeTool

**文件**: `plugins/time_awareness_plugin/tools/query_time.py`

**修复**:
```python
# 修改前
chatter_allow: list[str] = ["default_chatter"]

# 修改后
chatter_allow: list[str] = ["default_chatter", "kokoro_flow_chatter"]
```

### 3.3 修复 Action 组件（完整签名）

**文件**: `plugins/kokoro_flow_chatter/actions/reply.py` 和 `do_nothing.py`

**修复**:
```python
# 修改前
chatter_allow: list[str] = ["kokoro_flow_chatter"]

# 修改后
chatter_allow: list[str] = ["kokoro_flow_chatter", "kokoro_flow_chatter:chatter:kokoro_flow_chatter"]
```

---

## 4. 已修复的文件清单

| 序号 | 文件路径 | 修复内容 | 状态 |
|------|----------|----------|------|
| 1 | `plugins/thinking_plugin/tools/think_tool.py` | 添加 `kokoro_flow_chatter` 到 `chatter_allow` | ✅ 已修复 |
| 2 | `plugins/time_awareness_plugin/tools/query_time.py` | 添加 `kokoro_flow_chatter` 到 `chatter_allow` | ✅ 已修复 |
| 3 | `plugins/kokoro_flow_chatter/actions/reply.py` | 添加完整签名到 `chatter_allow` | ✅ 已修复 |
| 4 | `plugins/kokoro_flow_chatter/actions/do_nothing.py` | 添加完整签名到 `chatter_allow` | ✅ 已修复 |

## 5. 为什么之前爱莉说"没有这个工具"

用户问："爱莉爱莉，你有 kfc 工具了么"

爱莉回答："我现在还没有这个工具哦♪"

**原因**:

1. **kfc_reply 是 Action，不是 Tool**
   - Action 不会出现在 LLM 的 tool calling 列表中
   - Action 是由 Chatter 内部逻辑调用的，不是由 LLM 直接调用的

2. **LLM 看不到 kfc_reply**
   - LLM 只能看到 Tool 类型的组件
   - kfc_reply 被定义为 `BaseAction` 子类
   - 所以爱莉希雅（LLM）无法"看到"这个工具

3. **设计哲学差异**
   - KFC 使用心理活动流模式
   - LLM 只需要调用：`think`、`send_emoji_meme`、`send_tts_voice`
   - 回复文本由 Chatter 内部根据 LLM 输出决定，不是由 LLM 直接调用

### KFC 的设计流程

```
用户消息
  → LLM 感知阶段（输出内心感受）
  → LLM 决策阶段（调用工具：think/emoji/tts）
  → Chatter 根据工具执行结果决定回复
  → 发送消息给对方
```

`kfc_reply` 是在最后一步由 Chatter 自动调用的，不是由 LLM 主动选择的。

---

## 6. 验证步骤

1. **重启 Bot**
   ```bash
   # 停止并重新启动 Neo-MoFox
   ```

2. **检查日志**
   ```
   应该看到：可用组件：12/12
   而不是：可用组件：10/12
   ```

3. **测试对话**
   - 问爱莉："你现在能思考了吗？"
   - 爱莉应该能够调用 `think` 工具
   - 问爱莉："现在几点了？"
   - 爱莉应该能够调用 `query_time` 工具

---

## 7. 总结

### 根本原因

组件签名格式为 `plugin_name:component_type:component_name`，例如：
```
kokoro_flow_chatter:chatter:kokoro_flow_chatter
```

过滤逻辑检查两次：
1. 简称匹配：`kokoro_flow_chatter`
2. 完整签名匹配：`kokoro_flow_chatter:chatter:kokoro_flow_chatter`

**配置时必须同时包含两种格式**，否则会导致过滤失败。

### 修复状态

所有 4 个受影响的组件已修复，重启 Bot 后应该能看到"可用组件：12/12"。

### 长期建议

1. **默认 chatter_allow 应该是空列表**（表示不限制）
   ```python
   chatter_allow: list[str] = []  # 空列表表示所有 chatter 都可用
   ```

2. **只有在确实需要限制时才指定 chatter**

3. **考虑修改过滤逻辑，同时匹配简称和完整签名**
   - 当前逻辑要求同时匹配两种格式
   - 可以改为：只要匹配简称或完整签名之一即可
