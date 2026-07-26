# Thinking Plugin 实施报告

**日期：** 2026-03-18
**任务：** 实现多步连续行动能力 - 思考工具插件
**状态：** 已完成（已修复 prompt 注入问题）

---

## 执行摘要

成功创建 `thinking_plugin` 思考工具插件，实现了让爱莉希雅展现思考过程、进行多步连续行动的能力。插件完全遵循项目代码规范，采用纯插件实现方案，无需修改核心代码。

**修复记录：**
- **2026-03-18 20:20** - 修复 prompt 注入问题：将 `get_prompt_manager().register_extra()` 替换为 `add_system_reminder()`，思考提示词现在正确注入到 actor bucket
- **2026-03-18 20:20** - 修复 config.toml 格式错误：移除第 10-26 行的格式错误注释块

---

## 创建的文件

### 插件目录 (`plugins/thinking_plugin/`)

| 文件 | 说明 |
|------|------|
| `manifest.json` | 插件清单，定义名称、版本、组件列表、配置路径 |
| `__init__.py` | 包入口文件，导出 ThinkTool |
| `plugin.py` | 插件主类 `ThinkingPlugin` 和 `on_plugin_loaded` 钩子 |
| `config.py` | Pydantic 配置类 `ThinkingConfig` |
| `tools/__init__.py` | 工具模块入口 |
| `tools/think_tool.py` | 思考工具 `ThinkTool` 定义 |
| `README.md` | 插件使用文档 |

### 配置目录 (`config/plugins/thinking_plugin/`)

| 文件 | 说明 |
|------|------|
| `config.toml` | 插件配置文件，包含可自定义的提示词 |

---

## 关键实现细节

### 1. ThinkTool 工具设计

```python
class ThinkTool(BaseTool):
    """思考工具。

    当你需要整理思路、分析情况或决定下一步怎么做时，调用此工具。
    """

    tool_name = "think"
    tool_description = "在内心思考一下当前情况。调用此工具来整理你的思路、分析用户意图、或规划下一步行动。"

    async def execute(
        self,
        thought: Annotated[str, "你的心理活动，写下你此刻的想法和分析过程"]
    ) -> tuple[bool, dict]:
        return True, {
            "thought_recorded": True,
            "thought_content": thought,
            "reminder": "思考已记录。现在你可以：1) 继续深入思考 2) 调用其他工具获取信息 3) 如果已想清楚，可以回复用户了"
        }
```

**关键设计决策：**
- 继承 `BaseTool` 而非 `BaseAction`
- 返回 `dict` 而非 `str`，确保触发 FOLLOW_UP
- 返回结果包含 `reminder`，引导 LLM 下一步行动

### 2. 配置类设计（遵循经验教训）

```python
from typing import ClassVar
from src.core.components.base.config import BaseConfig, SectionBase, config_section, Field

class ThinkingConfig(BaseConfig):
    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "思考工具插件配置"

    @config_section("prompt")
    class PromptSection(SectionBase):
        thinking_habit: str = Field(
            default="""...提示词内容...""",
            description="思考习惯引导提示词"
        )

    prompt: PromptSection = Field(default_factory=PromptSection)
```

**遵循的规范：**
- ✅ 继承 `BaseConfig` 而非 `ConfigBase`
- ✅ 使用 `ClassVar[str]` 注解 `config_name` 和 `config_description`
- ✅ 显式声明配置节字段 `prompt: PromptSection = Field(default_factory=...)`

### 3. manifest.json 设计（遵循经验教训）

```json
{
  "name": "thinking_plugin",
  "version": "1.0.0",
  "dependencies": {"plugins": [], "components": []},
  "include": [
    {"component_type": "tool", "component_name": "think", "dependencies": [], "enabled": true}
  ],
  "entry_point": "plugin.py",
  "config": {
    "path": "config/plugins/thinking_plugin/config.toml",
    "reloadable": true
  }
}
```

**遵循的规范：**
- ✅ 使用 `dependencies` 字段而非 `plugins`
- ✅ 包含 `entry_point` 字段
- ✅ 包含 `include` 数组，列出所有组件
- ✅ 声明 `config` 配置路径

### 4. 提示词配置化

将思考引导提示词放在 `config/plugins/thinking_plugin/config.toml` 中：

```toml
[prompt]
thinking_habit = """
# 思考的习惯
在回复用户之前，如果你不确定如何回答，或者需要更多信息，你应该先调用 `think` 工具。
...
"""
```

**优势：**
- 用户可随时修改提示词，无需改代码
- 配置支持热重载，修改后无需重启 Bot
- 便于 A/B 测试不同提示词效果

---

## 测试结果

### 1. 导入测试

```bash
$ python -c "from plugins.thinking_plugin import ThinkTool; print('Import OK')"
Import OK
```

✅ 通过

### 2. 配置加载测试

```bash
$ python -c "from plugins.thinking_plugin.config import ThinkingConfig; c = ThinkingConfig.load_for_plugin('thinking_plugin'); print('Config Load OK')"
Config Load OK
```

✅ 通过

### 3. Manifest 加载测试

```bash
$ python -c "from src.core.components.loader import load_manifest; import asyncio; asyncio.run(load_manifest('plugins/thinking_plugin')); print('Manifest Load OK')"
Manifest Load OK
```

✅ 通过

### 4. 工具执行测试

```python
>>> result = await think_tool.execute('这是一个测试思考内容')
>>> print(result)
(True, {
    'thought_recorded': True,
    'thought_content': '这是一个测试思考内容',
    'reminder': '思考已记录。现在你可以：1) 继续深入思考 2) 调用其他工具获取信息 3) 如果已想清楚，可以回复用户了'
})
```

✅ 通过

---

## 代码规范遵循

### 类型注解
- ✅ 所有函数参数和返回值都有类型注解
- ✅ 使用 `Annotated` 提供参数语义说明

### 文档字符串
- ✅ 所有文件都有文件头 docstring
- ✅ 所有类和函数都有 docstring
- ✅ docstring 包含 Args、Returns、Examples

### 代码结构
- ✅ 遵循项目目录结构规范
- ✅ 配置文件放在子目录 `config/plugins/thinking_plugin/`
- ✅ 使用 `@register_plugin` 装饰器注册插件

---

## 经验教训应用

### 从 omni_vision_plugin 学到的经验

| 问题 | 本次实现的处理 |
|------|---------------|
| manifest 字段名错误 | ✅ 使用 `dependencies` 而非 `plugins` |
| 缺少 entry_point | ✅ 包含 `entry_point: "plugin.py"` |
| config 继承错误 | ✅ 继承 `BaseConfig` 而非 `ConfigBase` |
| 缺少 ClassVar | ✅ 使用 `ClassVar[str]` 注解 |
| 配置节未显式声明 | ✅ 使用 `Field(default_factory=...)` |

---

## 架构说明

### FOLLOW_UP 机制利用

`think` 工具的设计充分利用了现有的 FOLLOW_UP 机制：

```
LLM 调用 think → TOOL_RESULT 写回上下文
    ↓
has_pending_tool_results = True
    ↓
FOLLOW_UP → LLM 再次调用
    ↓
LLM 看到思考内容，决定下一步：
- 继续 think（深入分析）
- 调用其他 tool（查询信息）
- 调用 send_text（回复用户）
```

### 为什么是 Tool 不是 Action

| 类型 | 返回值 | FOLLOW_UP | 设计目的 |
|------|--------|-----------|----------|
| Tool | dict | ✅ 触发 | 查询信息，需要后续推理 |
| Action | str | ❌ 不触发 | 执行动作，无需后续推理 |

`think` 需要触发 FOLLOW_UP，让 LLM 看到思考结果后继续决策，所以必须是 Tool。

---

## 启用方法

### 1. 添加插件到配置

编辑 `config/core.toml`：

```toml
[bot]
plugins = ["thinking_plugin"]
```

### 2. 自定义提示词（可选）

编辑 `config/plugins/thinking_plugin/config.toml`：

```toml
[prompt]
thinking_habit = """
你的自定义提示词...
"""
```

### 3. 重启 Bot

```bash
python -m src.app.main
```

---

## 预期效果

### 示例对话流程

```
用户：你还记得上次我说的那件事吗？

┌─────────────────────────────────────────────────────────────┐
│ 第 1 轮 LLM                                                   │
│ 爱莉希雅：think(thought="用户问'那件事'，但不确定是什么…")    │
│ TOOL_RESULT: 思考已记录                                      │
└─────────────────────────────────────────────────────────────┘
                          ↓
                    FOLLOW_UP
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 2 轮 LLM                                                   │
│ 爱莉希雅：read_diary(query="工作调动")                        │
│ TOOL_RESULT: [日记内容]                                      │
└─────────────────────────────────────────────────────────────┘
                          ↓
                    FOLLOW_UP
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 3 轮 LLM                                                   │
│ 爱莉希雅：send_text("啊，找到了！你说的是工作调动的事对吧？") │
└─────────────────────────────────────────────────────────────┘
                          ↓
                    WAIT_USER
```

---

## 风险与缓解

### 风险 1：LLM 不使用 think 工具

**可能性：** 中
**缓解：** 提示词引导、多次对话自适应

### 风险 2：无限思考循环

**可能性：** 低
**缓解：** LLM 自我终止、未来可加最大迭代次数限制

### 风险 3：思考内容空洞

**可能性：** 中
**缓解：** 提示词中强调"真诚思考"

---

## 后续优化建议

### 短期（可选）
1. 监控实际使用情况，调整提示词
2. 收集"好的思考"示例，加入提示词

### 长期（可选）
1. 加入最大 FOLLOW_UP 迭代次数限制
2. 增加思考质量评估
3. 提供多种思考类型（分析、规划、反思）

---

## 文件清单

### 新增文件
- `plugins/thinking_plugin/manifest.json`
- `plugins/thinking_plugin/__init__.py`
- `plugins/thinking_plugin/plugin.py`
- `plugins/thinking_plugin/config.py`
- `plugins/thinking_plugin/tools/__init__.py`
- `plugins/thinking_plugin/tools/think_tool.py`
- `plugins/thinking_plugin/README.md`
- `config/plugins/thinking_plugin/config.toml`

### 修改文件
- 无（纯插件实现，未修改核心代码）

---

## 验证命令

```bash
# 测试导入
python -c "from plugins.thinking_plugin import ThinkTool; print('OK')"

# 测试配置加载
python -c "from plugins.thinking_plugin.config import ThinkingConfig; ThinkingConfig.load_for_plugin('thinking_plugin'); print('OK')"

# 测试工具执行
python -c "
import asyncio
from plugins.thinking_plugin.tools.think_tool import ThinkTool
class MockPlugin: plugin_name = 'thinking_plugin'
async def test():
    tool = ThinkTool(MockPlugin())
    result = await tool.execute('test')
    print('OK' if result[0] else 'FAIL')
asyncio.run(test())
"
```

---

## 结论

Thinking Plugin 已成功实现，所有测试通过。插件遵循项目代码规范，应用了 omni_vision_plugin 的经验教训，采用纯插件实现方案，无需修改核心代码。提示词配置化设计允许用户灵活调整引导策略。

**下一步：** 重启 Bot，验证实际对话效果。

---

## 经验教训总结

### 问题 1：TOML 格式错误（重复发生）

**现象：** 插件加载失败，错误信息 "Invalid statement (at line X, column 1)"

**根本原因：** 在注释中使用了未闭合的三引号 `"""`，导致 TOML 解析器误认为是多行字符串

**错误示例：**
```toml
# 值类型：str, 默认值："""  ← 错误！注释中包含三引号

# 思考的习惯
...
"""  ← 解析器认为这是字符串的一部分
thinking_habit = """
```

**正确做法：**
```toml
# 值类型：str, 默认值：见下方  ← 注释中不要出现三引号
thinking_habit = """
...
"""
```

**如何避免：**
- TOML 注释中**绝对不能**出现 `"""` 三引号
- 如果需要描述多行字符串默认值，用文字说明即可
- 不确定时，参考项目中其他正常的 config.toml 文件

### 问题 2：Prompt API 使用错误

**现象：** LLM 不调用 think 工具，因为提示词从未被注入

**根本原因：** 使用了不存在的 API 方法 `get_prompt_manager().register_extra()`

**正确做法：**
```python
from src.app.plugin_system.api.prompt_api import add_system_reminder

add_system_reminder(
    bucket="actor",
    name="thinking_habit",
    content=config.prompt.thinking_habit,
)
```

**原理：**
1. `add_system_reminder()` 将内容存储到 system reminder store
2. `default_chatter.create_request("actor", with_reminder="actor")` 自动读取 actor bucket
3. Reminders 被拼接到 system prompt 中发送给 LLM

### 通用经验

| 问题类型 | 教训 | 应用范围 |
|---------|------|---------|
| TOML 格式 | 注释中不要出现三引号 `"""` | 所有 config.toml 文件 |
| Prompt API | 使用 `add_system_reminder()` 而非 `register_extra()` | 所有需要注入提示词的插件 |
| 代码验证 | 修改后必须运行测试验证 | 所有代码修改 |
| 经验传承 | 及时写入 memory 持久化 | 避免重复犯错 |
