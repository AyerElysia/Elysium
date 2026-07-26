# 意图动态生成插件修复报告

**日期：** 2026-03-19
**状态：** 已完成

---

## 问题描述

重启 Bot 后测试意图动态生成功能，出现以下错误：

```
[02:08:02] intent_plugin | WARNING | 意图生成失败：'LLMResponse' object has no attribute 'text_content'
```

---

## 根本原因

`LLMResponse` 类没有 `text_content()` 方法。文本内容存储在 `message` 属性中。

查看 `src/kernel/llm/response.py` 可知：

```python
@dataclass(slots=True)
class LLMResponse:
    """LLM Response，支持流式和非流式响应"""

    message: str | None = None  # 文本内容存储在此
    call_list: list[ToolCall] | None = None
    ...
```

---

## 修复内容

### 文件：`plugins/intent_plugin/intent_generator.py`

**修复 1：导入路径修正**

```python
# 修复前（错误）
from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text
# get_model_config 在方法内部动态导入，且路径错误

# 修复后（正确）
from src.kernel.logger import get_logger
from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text
from src.core.config import get_model_config  # 正确导入路径
```

**修复 2：获取响应文本**

```python
# 修复前（错误）
response = await llm_request.send()
content = response.text_content()  # LLMResponse 没有此方法

# 修复后（正确）
response = await llm_request.send()
content = response.message or ""  # message 属性存储文本内容
```

---

## 完整修复后的代码片段

```python
async def generate_intents(
    self,
    situation: "Situation",
    recent_messages: list[str] = None,
) -> list[Intent]:
    """根据情境生成意图候选。"""
    prompt = self._build_prompt(situation, recent_messages or [])

    try:
        # 从配置中获取模型集，优先使用配置的 model_task，否则使用 actor
        model_config = get_model_config()
        model_set = model_config.get_task(self.model_task)
        if not model_set:
            model_set = model_config.get_task("actor")

        llm_request = LLMRequest(model_set, "intent_generation")
        llm_request.add_payload(LLMPayload(ROLE.USER, Text(prompt)))
        response = await llm_request.send()

        # 修复：使用 response.message 而非 response.text_content()
        content = response.message or ""
        intents = self._parse_response(content)

        if intents:
            logger.info(f"LLM 生成了 {len(intents)} 个意图候选")
        else:
            logger.debug("LLM 未生成有效意图")

        return intents

    except Exception as e:
        logger.warning(f"意图生成失败：{e}")
        return []
```

---

## 验证步骤

### 步骤 1：模块导入测试

```bash
python -c "from plugins.intent_plugin.intent_generator import IntentGenerator; print('导入成功!')"
```

输出：
```
导入成功!
```

### 步骤 2：完整模块链测试

```bash
python -c "
from plugins.intent_plugin.intent_engine import IntentEngine, Situation, create_goal_from_intent
from plugins.intent_plugin.intent_generator import IntentGenerator
from plugins.intent_plugin.config import IntentConfig
print('所有模块导入成功!')
"
```

输出：
```
所有模块导入成功!
```

### 步骤 3：配置加载测试

```bash
python -c "
from plugins.intent_plugin.config import IntentConfig
c = IntentConfig.load_for_plugin('intent_plugin')
print('配置加载成功:')
print('  social.enabled:', c.social.enabled)
print('  emotional.enabled:', c.emotional.enabled)
print('  growth.enabled:', c.growth.enabled)
print('  generation.model_task:', c.generation.model_task)
"
```

输出：
```
配置加载成功:
  social.enabled: True
  emotional.enabled: True
  growth.enabled: True
  generation.model_task: actor
```

---

## 错误汇总

本次重构过程中遇到并修复的所有错误：

| 序号 | 错误 | 原因 | 修复 |
|------|------|------|------|
| 1 | `KeyError: '\n    "id"'` | Prompt 模板中 JSON 示例的大括号被 `.format()` 误解析 | 转义 JSON 大括号 `{{}}` |
| 2 | `TOMLDecodeError: Illegal character '\n'` | `config.toml` 中 `model_task` 值断行 | 合并为单行 |
| 3 | `model_set 必须是非空 list` | 传入字符串而非 `model_set` 对象 | 从 `get_model_config().get_task()` 获取 |
| 4 | `event_manager | ERROR` 无详细日志 | 异常处理使用 `logger.debug()` | 改为 `logger.error()` |
| 5 | `cannot import name 'get_model_config'` | 导入路径错误 | 从 `src.core.config` 导入 |
| 6 | `'LLMResponse' object has no attribute 'text_content'` | API 方法不存在 | 使用 `response.message` 属性 |

---

## 下一步

1. **重启 Bot** - 加载修复后的代码
2. **私聊测试** - 与爱莉希雅对话，观察意图生成效果
3. **日志监控** - 检查以下日志输出：
   - `LLM 生成了 N 个意图候选` - 意图生成成功
   - `已更新 System Reminder: {objective}` - 目标已注入

---

## 代码审查清单

已检查的文件：

| 文件 | 状态 |
|------|------|
| `intent_generator.py` | 已修复导入和 API 调用 |
| `intent_engine.py` | 无问题 |
| `goal_manager.py` | 无问题 |
| `goal_tracker.py` | 无问题 |
| `models.py` | 无问题 |
| `default_intents.py` | 无问题 |
| `config.py` | 无问题 |
| `config.toml` | 无问题 |

---

**修复完成时间：** 2026-03-19
**版本：** 2.0.1
**状态：** 已完成，等待重启测试
