# Thinking Plugin 问题排查与修复总结

**日期：** 2026-03-18
**问题：** thinking_plugin 加载失败 + LLM 不调用 think 工具

---

## 问题时间线

| 时间 | 问题 | 状态 |
|-----|------|------|
| 20:21 | 插件加载失败：`Invalid statement (at line 13, column 1)` | 已修复 |
| 20:19 | LLM 不调用 think 工具（之前会话遗留问题） | 已修复 |

---

## 修复 1：Prompt API 使用错误

### 症状
- 插件加载成功，但 LLM 从不调用 `think` 工具
- 日志显示可用组件 9/9，但 only `action-send_emoji_meme` 和 `action-send_text` 被调用

### 根本原因
`plugin.py` 使用了不存在的 API：
```python
# ❌ 错误：register_extra 方法不存在
get_prompt_manager().register_extra(
    key="thinking_habit",
    text=config.prompt.thinking_habit,
)
```

### 修复方案
```python
# ✅ 正确：使用 add_system_reminder
from src.app.plugin_system.api.prompt_api import add_system_reminder

add_system_reminder(
    bucket="actor",
    name="thinking_habit",
    content=config.prompt.thinking_habit,
)
```

### 原理
1. `add_system_reminder()` 将内容存储到 actor bucket
2. `default_chatter.create_request("actor", with_reminder="actor")` 自动加载
3. Reminders 被注入到 LLM system prompt 中

---

## 修复 2：TOML 格式错误（重复发生）

### 症状
```
插件加载失败：thinking_plugin - Invalid statement (at line 13, column 1)
```

### 根本原因
配置文件 `config/plugins/thinking_plugin/config.toml` 第 10-26 行包含格式错误的注释：

```toml
# ❌ 错误：注释中包含未闭合的三引号
# 值类型：str, 默认值："""

# 思考的习惯
...
"""
thinking_habit = """
```

TOML 解析器将 `# 值类型：str, 默认值："""` 中的 `"""` 误认为是多行字符串的开始，导致后续内容解析错乱。

### 修复方案
删除错误的注释块，保持简洁：

```toml
# ✅ 正确：注释中不出现三引号
# 思考习惯引导提示词
thinking_habit = """
# 思考的习惯
在回复用户之前，如果你不确定如何回答，或者需要更多信息，你应该先调用 `think` 工具。

思考是一个好习惯——不要急于回复，先想一想：
- 用户的问题是什么？
- 我需要更多信息吗？
- 我应该先查询什么吗？

当你不调用 think 时，说明你已经想清楚了，可以直接回复用户了。

示例：
- "嗯…这个问题我需要想想" → 调用 think
- "我不太确定，让我分析一下" → 调用 think
- "根据上面的思考，我应该先查一下日记" → 调用 read_diary
"""
```

### 如何避免
- **TOML 注释中绝对不能出现 `"""` 三引号**
- 如果需要描述多行字符串默认值，用文字说明即可
- 不确定时，参考项目中其他正常的 config.toml 文件（如 `omni_vision_plugin/config.toml`）

---

## 验证命令

```bash
# 1. 测试配置加载
python -c "
from plugins.thinking_plugin.config import ThinkingConfig
c = ThinkingConfig.load_for_plugin('thinking_plugin')
print('Config OK, enabled:', c.settings.enabled)
"

# 2. 测试 Manifest 加载
python -c "
from src.core.components.loader import load_manifest
import asyncio
async def test():
    m = await load_manifest('plugins/thinking_plugin')
    print('Manifest OK:', m.name)
asyncio.run(test())
"
```

---

## 经验教训

### TOML 配置文件规范
1. 注释中不要出现三引号 `"""`
2. 多行字符串必须用 `"""` 包裹，且独占一行
3. 参考已有正确示例，不要凭空创造格式

### Prompt API 使用规范
1. 注入额外提示词：使用 `add_system_reminder(bucket, name, content)`
2. 注册模板：使用 `get_or_create(name, template, policies)`
3. 不要使用 `get_prompt_manager().register_extra()` — 此方法不存在

### 开发流程规范
1. 修改配置后**必须**运行测试验证
2. 遇到重复错误时，先检查是否是同一类问题
3. 及时将经验写入 memory 持久化，避免重复犯错

---

## 相关文件

- 修复代码：`plugins/thinking_plugin/plugin.py`
- 配置文件：`config/plugins/thinking_plugin/config.toml`
- 实施报告：`report/thinking_plugin_implementation_report.md`
- Memory 记录：
  - `feedback_prompt_api_usage.md`
  - `reference_prompt_api_patterns.md`
