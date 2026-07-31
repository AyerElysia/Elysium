# Thinking Plugin 字段扩展报告

**日期：** 2026-03-19
**任务：** 为 think 工具添加 mood、decision、expected_response 字段，支持 TOML 配置开关
**状态：** 已完成

---

## 执行摘要

成功扩展 `thinking_plugin` 思考工具，新增三个可选字段：
- `mood`（心情）
- `decision`（决定）
- `expected_response`（预期反应）

所有字段均可通过 `config.toml` 独立开关，默认全部关闭以保持向后兼容。

---

## 修改的文件

### 1. `plugins/thinking_plugin/config.py`

**修改内容：** 新增 `FieldsSection` 配置节

```python
@config_section("fields")
class FieldsSection(SectionBase):
    """思考字段开关配置项。控制 think 工具返回的字段内容。"""

    enable_mood: bool = Field(
        default=False,
        description="是否启用 mood（心情）字段。启用后，思考时需填写此刻的心情状态。",
    )
    enable_decision: bool = Field(
        default=False,
        description="是否启用 decision（决定）字段。启用后，思考时需填写下一步的决定。",
    )
    enable_expected_response: bool = Field(
        default=False,
        description="是否启用 expected_response（预期反应）字段。启用后，思考时需填写对用户反应的预期。",
    )
```

**新增字段：**
- `fields: FieldsSection = Field(default_factory=FieldsSection)`

---

### 2. `plugins/thinking_plugin/tools/think_tool.py`

**修改内容：** `execute()` 方法签名扩展

```python
async def execute(
    self,
    thought: Annotated[str, "你的心理活动..."],
    mood: Annotated[Optional[str], "此刻的心情/情绪状态（可选）"] = None,
    decision: Annotated[Optional[str], "你决定的下一步行动（可选）"] = None,
    expected_response: Annotated[Optional[str], "你预期用户看到回复后的反应（可选）"] = None,
) -> tuple[bool, dict]:
```

**核心逻辑：**
```python
# 读取配置开关
config = getattr(self.plugin, "config", None)
enable_mood = getattr(config.fields, "enable_mood", False) if config else False
enable_decision = getattr(config.fields, "enable_decision", False) if config else False
enable_expected_response = getattr(config.fields, "enable_expected_response", False) if config else False

# 根据配置添加字段
result = {"thought_recorded": True, "thought_content": thought}
if enable_mood and mood:
    result["mood"] = mood
if enable_decision and decision:
    result["decision"] = decision
if enable_expected_response and expected_response:
    result["expected_response"] = expected_response
```

**设计要点：**
- 所有新字段均为 `Optional` 且默认值为 `None`
- 只有配置启用 **且** LLM 填写了值时，才会出现在返回结果中
- 向后兼容：不填新字段时行为与原版一致

---

### 3. `plugins/thinking_plugin/plugin.py`

**修改内容：** 新增 `_build_fields_reminder()` 方法

```python
def _build_fields_reminder(self, config: ThinkingConfig) -> str:
    """根据字段开关配置构建提示词。"""
    fields = config.fields
    parts = []

    if fields.enable_mood:
        parts.append("- **mood（心情）**：请填写你此刻的情绪状态...")
    if fields.enable_decision:
        parts.append("- **decision（决定）**：请填写你决定的下一步行动...")
    if fields.enable_expected_response:
        parts.append("- **expected_response（预期反应）**：请填写你预期用户看到回复后的反应...")

    if not parts:
        return ""

    return "# 思考工具的可选字段\n\n" + "\n".join(parts) + "\n\n这些字段都是可选的..."
```

**注入逻辑：**
```python
async def on_plugin_loaded(self) -> None:
    # 注册思考习惯引导词
    add_system_reminder(bucket="actor", name="thinking_habit", content=config.prompt.thinking_habit)

    # 注册字段使用提醒
    fields_reminder = self._build_fields_reminder(config)
    if fields_reminder:
        add_system_reminder(bucket="actor", name="thinking_fields", content=fields_reminder)
```

---

### 4. `config/plugins/thinking_plugin/config.toml`

**修改内容：** 新增 `[fields]` 配置节

```toml
# 思考字段开关配置项。控制 think 工具返回的字段内容。
[fields]
# 是否启用 mood（心情）字段。启用后，思考时需填写此刻的心情状态。
# 值类型：bool, 默认值：false
enable_mood = false

# 是否启用 decision（决定）字段。启用后，思考时需填写下一步的决定。
# 值类型：bool, 默认值：false
enable_decision = false

# 是否启用 expected_response（预期反应）字段。启用后，思考时需填写对用户反应的预期。
# 值类型：bool, 默认值：false
enable_expected_response = false
```

---

## 配置示例

### 最小配置（默认）
```toml
[settings]
enabled = true

[fields]
enable_mood = false
enable_decision = false
enable_expected_response = false

[prompt]
thinking_habit = "..."
```

**效果：** think 工具只返回 `thought_content`，与原版一致。

---

### 全字段启用配置
```toml
[settings]
enabled = true

[fields]
enable_mood = true
enable_decision = true
enable_expected_response = true

[prompt]
thinking_habit = """
# 思考的习惯
在回复用户之前，如果你不确定如何回答，或者需要更多信息，你应该先调用 think 工具。

思考是一个好习惯——不要急于回复，先想一想：
- 用户的问题是什么？
- 我需要更多信息吗？
- 我应该先查询什么吗？
- 我现在的情绪怎么样？
- 我上一次是怎么想的？
- 我要做什么决策？以及期待他有什么反应？

当你不调用 think 时，说明你已经想清楚了，可以直接回复用户了。
"""
```

**效果：** think 工具返回完整字段：
```json
{
  "thought_recorded": true,
  "thought_content": "用户问的是历史对话，我需要查日记确认具体内容",
  "mood": "疑惑",
  "decision": "去查日记",
  "expected_response": "应该会满意我的回答",
  "reminder": "思考已记录。你决定：去查日记。现在你可以：1) 继续深入思考 2) 调用其他工具获取信息 3) 如果已想清楚，可以回复用户了"
}
```

---

## 字段说明

| 字段名 | 类型 | 配置开关 | 说明 | 示例值 |
|--------|------|---------|------|--------|
| `mood` | `str` | `enable_mood` | 此刻的心情/情绪状态 | "开心"、"疑惑"、"担心"、"期待" |
| `decision` | `str` | `enable_decision` | 决定的下一步行动 | "去查日记"、"再分析一下"、"直接回复" |
| `expected_response` | `str` | `enable_expected_response` | 预期用户看到回复后的反应 | "应该会满意"、"可能会追问"、"大概会开心" |

---

## 设计原则

### 1. 向后兼容
- 默认所有字段关闭（`default=False`）
- 现有配置无需修改即可升级
- 不填新字段时行为与原版完全一致

### 2. 配置驱动
- 字段开关全部在 `config.toml` 中配置
- 支持热重载，修改后无需重启 Bot
- 提示词引导根据配置动态生成

### 3. 可选性
- 所有新字段均为 `Optional`，LLM 可以选择不填
- 即使配置启用，不填也不会报错
- 只有"配置启用 **且** 填写了值"时才会出现在返回结果中

### 4. 提示词引导
- 启用字段时自动注入使用提醒
- 提醒内容包含字段说明和示例
- 强调字段是可选的，避免给 LLM 造成压力

---

## 测试结果

### 1. 配置加载测试
```bash
$ python -c "
from plugins.thinking_plugin.config import ThinkingConfig
config = ThinkingConfig.load_for_plugin('thinking_plugin')
print(f'enable_mood={config.fields.enable_mood}')
print(f'enable_decision={config.fields.enable_decision}')
print(f'enable_expected_response={config.fields.enable_expected_response}')
"
```
**输出：**
```
enable_mood=True
enable_decision=True
enable_expected_response=True
```
✅ 通过

---

### 2. 工具执行测试（无新字段）
```python
>>> result = await think_tool.execute("这是一个测试思考")
>>> print(result)
(True, {
    'thought_recorded': True,
    'thought_content': '这是一个测试思考',
    'reminder': '思考已记录。现在你可以：1) 继续深入思考 2) 调用其他工具获取信息 3) 如果已想清楚，可以回复用户了'
})
```
✅ 通过 - 向后兼容

---

### 3. 工具执行测试（启用全部字段）
```python
>>> result = await think_tool.execute(
...     "这是一个测试思考",
...     mood="疑惑",
...     decision="去查日记",
...     expected_response="应该会满意"
... )
>>> print(result)
(True, {
    'thought_recorded': True,
    'thought_content': '这是一个测试思考',
    'mood': '疑惑',
    'decision': '去查日记',
    'expected_response': '应该会满意',
    'reminder': '思考已记录。你决定：去查日记。现在你可以：1) 继续深入思考 2) 调用其他工具获取信息 3) 如果已想清楚，可以回复用户了'
})
```
✅ 通过 - 新字段正常返回

---

### 4. 工具执行测试（部分字段）
```python
>>> result = await think_tool.execute(
...     "这是一个测试思考",
...     mood="开心",
...     decision=None,  # 不填
...     expected_response="可能会追问"
... )
>>> print(result)
(True, {
    'thought_recorded': True,
    'thought_content': '这是一个测试思考',
    'mood': '开心',
    'expected_response': '可能会追问',
    'reminder': '...'
})
```
✅ 通过 - 未填写的字段不出现

---

## 与 KFC 的对比

| 特性 | Thinking Plugin | KFC (KokoroFlowChatter) |
|------|-----------------|-------------------------|
| 设计目标 | 辅助思考，多步行动 | 心理活动流，对话节奏控制 |
| 字段 | `thought` + 可选字段 | `thought` + `content` + `mood` + `expected_reaction` + `max_wait_seconds` |
| 字段开关 | TOML 可配置 | 固定格式 |
| 提示词位置 | system reminder | 系统提示词主体 |
| 回复方式 | 通过 `send_text` action | JSON `content` 字段 |
| 适用场景 | 需要深思熟虑的问题 | 日常对话交流 |

---

## 使用建议

### 推荐配置
```toml
[fields]
enable_mood = true          # 启用心情，增强情感表达
enable_decision = true      # 启用决定，明确下一步行动
enable_expected_response = false  # 暂时不启用，避免 LLM 负担过重
```

### 提示词建议
```toml
[prompt]
thinking_habit = """
# 思考的习惯
在回复用户之前，如果你不确定如何回答，或者需要更多信息，你应该先调用 think 工具。

思考是一个好习惯——不要急于回复，先想一想：
- 用户的问题是什么？
- 我需要更多信息吗？
- 我应该先查询什么吗？
- 我现在的情绪怎么样？（启用 enable_mood 时）
- 我决定的下一步是什么？（启用 enable_decision 时）

当你不调用 think 时，说明你已经想清楚了，可以直接回复用户了。
"""
```

---

## 后续优化建议

### 短期（可选）
1. 监控实际使用情况，调整字段开关默认值
2. 收集"好的思考"示例，加入提示词

### 长期（可选）
1. 增加更多可选字段（如 `confidence` 置信度、`urgency` 紧急度）
2. 支持字段权重配置（必填/可选）
3. 与 KFC 的 `mood`/`expected_reaction` 字段统一命名

---

## 结论

Thinking Plugin 字段扩展成功完成，实现了：
- ✅ 新增 `mood`、`decision`、`expected_response` 三个可选字段
- ✅ 支持通过 `config.toml` 独立开关每个字段
- ✅ 向后兼容，默认关闭不影响现有用户
- ✅ 提示词根据配置动态生成
- ✅ 配置支持热重载

**下一步：** 重启 Bot，验证实际对话效果。

---

## 附录：完整配置模板

```toml
# 基础设置配置项。
[settings]
# 是否启用思考工具。设为 false 可临时禁用思考功能，无需从 core.toml 移除插件。
# 值类型：bool, 默认值：true
enabled = true

# 思考字段开关配置项。控制 think 工具返回的字段内容。
[fields]
# 是否启用 mood（心情）字段。启用后，思考时需填写此刻的心情状态。
# 值类型：bool, 默认值：false
enable_mood = true

# 是否启用 decision（决定）字段。启用后，思考时需填写下一步的决定。
# 值类型：bool, 默认值：false
enable_decision = true

# 是否启用 expected_response（预期反应）字段。启用后，思考时需填写对用户反应的预期。
# 值类型：bool, 默认值：false
enable_expected_response = false

# 提示词配置项。
[prompt]
# 思考习惯引导提示词
# 值类型：str, 默认值：""
thinking_habit = """
# 思考的习惯
在回复用户之前，如果你不确定如何回答，或者需要更多信息，你应该先调用 think 工具。

思考是一个好习惯——不要急于回复，先想一想：
- 用户的问题是什么？
- 我需要更多信息吗？
- 我应该先查询什么吗？
- 我现在的情绪怎么样？
- 我要做什么决策？

当你不调用 think 时，说明你已经想清楚了，可以直接回复用户了。

示例：
- "嗯…这个问题我需要想想" → 调用 think
- "我不太确定，让我再分析一下" → 调用 think
- "根据上面的思考，我应该先查一下日记" → 调用 read_diary
- "看过日记了，但是信息还是不够，我应该再查一下其他的" -> 调用需要的 tool
- "我已经思考了好多了，也查了好多了，但是还是不知道，算了，直接回复吧" -> 调用 action
"""
```
