# 意图插件重构报告

**日期：** 2026-03-19
**状态：** 已完成

---

## 执行摘要

成功重构 `intent_plugin`，从**预定义意图模版**改为**LLM 动态生成**模式。

**核心变化：** 爱莉希雅现在可以根据对话情境自由生成意图，而不是从预定义列表中选择。

---

## 设计变更

### 改造前（预设模版）

```
预定义意图列表 → 情境匹配筛选 → 触发固定意图
```

**问题：**
- 意图是固定的 6 个（了解用户、记住细节、情感支持、制造惊喜、学习喜好、构建回忆）
- 每个意图有预设的 trigger_conditions 和 goal_templates
- 爱莉希雅只能在既定框架内选择，无法自由创造

### 改造后（动态生成）

```
对话情境分析 → LLM 生成意图候选 → 过滤排序 → 创建目标
```

**优势：**
- 意图由 LLM 根据实时情境生成，无固定模版
- 意图分类（social/emotional/growth）仅作为方向指引，不是限制
- 爱莉希雅可以创造任意符合情境的短期目标

---

## 文件修改清单

### 新增文件

#### 1. `plugins/intent_plugin/intent_generator.py`
LLM 驱动的意图生成器。

**核心类：** `IntentGenerator`
- `generate_intents()` - 调用 LLM 生成意图候选
- `_build_prompt()` - 构建情境提示词
- `_parse_response()` - 解析 LLM JSON 响应
- `_create_intent()` - 从字典创建 Intent 对象

**提示词模板：** 包含意图分类说明、生成规则、输出格式示例

---

### 修改文件

#### 2. `plugins/intent_plugin/default_intents.py`
**变更：** 移除所有预定义意图，仅保留分类提示

```python
# 之前：6 个预定义意图（SOCIAL_CURIOSITY, REMEMBER_DETAILS, 等）
# 之后：仅保留 INTENT_CATEGORY_HINTS 字典
INTENT_CATEGORY_HINTS = {
    "social": {"name": "社交类", "description": "...", "examples": [...]},
    "emotional": {...},
    "growth": {...},
}
PREDEFINED_INTENTS = []  # 空列表，向后兼容
```

---

#### 3. `plugins/intent_plugin/intent_engine.py`
**变更：** 从筛选器改为生成器

**核心修改：**
- `IntentEngine.__init__()` - 初始化 IntentGenerator（从配置读取 model_task）
- `generate_candidates()` - 改为 async 方法，调用 LLM 生成意图
- `_is_intent_enabled()` - 使用新的配置结构（category.enabled）
- `_calculate_priority()` - 简化为情境加成计算
- `get_intent_by_id()` - 从缓存中查找（动态生成的意图无全局注册）

**删除：**
- `_should_trigger()` - 不再需要复杂的触发条件检查
- `_has_higher_priority_goal()` - 简化为名称匹配
- `_check_trigger_conditions()` - 动态意图无预设条件

---

#### 4. `plugins/intent_plugin/goal_manager.py`
**变更：** `update_goals()` 改为 async 方法

```python
# 之前：
def update_goals(self, situation, max_active=3):
    candidate_intents = self.intent_engine.generate_candidates(...)

# 之后：
async def update_goals(self, situation, max_active=3):
    candidate_intents = await self.intent_engine.generate_candidates(...)
```

---

#### 5. `plugins/intent_plugin/goal_tracker.py`
**变更：** 异步调用 `update_goals()`

**修改：**
- `execute()` - 添加 await 调用 `update_goals()`
- `_build_reminder()` - 简化文案（动态意图无预设步骤）

---

#### 6. `plugins/intent_plugin/config.py`
**变更：** 从具体意图开关改为类别级别配置

```python
# 之前：
social_intents.social_curiosity: bool
social_intents.remember_details: bool
emotional_intents.emotional_support: bool
...

# 之后：
social.enabled: bool
social.weight: float
emotional.enabled: bool
emotional.weight: float
growth.enabled: bool
growth.weight: float
generation.model_task: str
generation.max_candidates: int
generation.diversity_temperature: float
```

---

#### 7. `config/plugins/intent_plugin/config.toml`
**变更：** 适配新的配置结构

```toml
# 之前：
[social_intents]
social_curiosity = true
remember_details = true

# 之后：
[social]
enabled = true
weight = 1.0

[generation]
model_task = "actor"
max_candidates = 3
diversity_temperature = 0.7
```

---

#### 8. `plugins/intent_plugin/plugin.py`
**变更：** 更新类描述

```python
class IntentPlugin(BasePlugin):
    """自主意图与短期目标系统，让模型具备内在驱动力。
    使用 LLM 动态生成意图，而非预定义模版，让爱莉希雅自由生成短期目标。
    """
```

---

#### 9. `plugins/intent_plugin/manifest.json`
**变更：** 版本号升级到 2.0.0，更新描述

```json
{
  "version": "2.0.0",
  "description": "自主意图与短期目标系统（LLM 动态生成模式）- 让爱莉希雅自由生成短期目标，而非预定义模版"
}
```

---

## 核心流程对比

### 改造前
```
1. Situation 分析 → 检测情境信号
2. 遍历 PREDEFINED_INTENTS → 检查 trigger_conditions
3. 匹配成功 → 计算优先级
4. 从 goal_templates 中选择 → 创建 Goal
5. 注入 System Reminder → 爱莉希雅看到固定提示
```

### 改造后
```
1. Situation 分析 → 检测情境信号
2. 构建提示词 → 调用 LLM 生成意图
3. 解析 JSON 响应 → 创建 Intent 对象
4. 过滤排序 → 应用类别开关和优先级阈值
5. 从 intent.description 创建 Goal → 注入 System Reminder
```

---

## 配置变更说明

### 新增配置项

| 配置路径 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `[generation].model_task` | str | "actor" | LLM 模型任务名 |
| `[generation].max_candidates` | int | 3 | 每次最多生成意图数 |
| `[generation].diversity_temperature` | float | 0.7 | 生成多样性温度 |
| `[social].weight` | float | 1.0 | 社交类权重（预留） |
| `[emotional].weight` | float | 1.0 | 情感类权重（预留） |
| `[growth].weight` | float | 1.0 | 成长类权重（预留） |

### 移除配置项

- `[social_intents].social_curiosity`
- `[social_intents].remember_details`
- `[emotional_intents].emotional_support`
- `[emotional_intents].create_surprise`
- `[emotional_intents].detect_mood`
- `[growth_intents].learn_preference`
- `[growth_intents].build_memory`
- `[growth_intents].knowledge_sharing`

---

## 意图生成示例

### 情境：用户表示疲惫
```
当前情境：
- 用户表示疲惫
- 用户表现出负面情绪

最近对话摘要：
- 今天好累啊
- 工作了一整天

LLM 可能生成：
[
  {
    "id": "comfort_user",
    "name": "安慰用户",
    "description": "给用户一些温暖的安慰",
    "category": "emotional",
    "base_priority": 8,
    "goal_objective": "帮助用户放松下来"
  },
  {
    "id": "suggest_rest",
    "name": "建议休息",
    "description": "建议用户早点休息",
    "category": "emotional",
    "base_priority": 7,
    "goal_objective": "让用户去休息"
  }
]
```

### 情境：新用户首次对话
```
当前情境：
- 这是新用户的首次对话

最近对话摘要：
- 你好
- 我是新来的

LLM 可能生成：
[
  {
    "id": "welcome_user",
    "name": "欢迎用户",
    "description": "欢迎新用户并表达友好",
    "category": "social",
    "base_priority": 7,
    "goal_objective": "让用户感到受欢迎"
  },
  {
    "id": "ask_interests",
    "name": "询问兴趣",
    "description": "了解用户的兴趣爱好",
    "category": "social",
    "base_priority": 6,
    "goal_objective": "了解用户喜欢什么"
  }
]
```

---

## 优势与局限

### 优势

1. **灵活性** - 不再受限于 6 个固定意图，可以应对各种情境
2. **自然性** - 意图由 LLM 生成，更符合对话流动
3. **可扩展** - 添加新意图类型无需修改代码，只需调整提示词
4. **个性化** - 不同模型/配置下可能生成不同风格的意图

### 局限

1. **不可预测** - LLM 生成的意图可能不稳定
2. **依赖 LLM** - 需要消耗额外的 LLM 调用
3. **调试困难** - 动态生成比固定模版更难追踪问题

---

## 故障修复

### 2026-03-19 修复记录

#### 问题 1：提示词模板格式错误
**症状：** `KeyError: '\n    "id"'`
**原因：** `INTENT_GENERATION_PROMPT` 中的示例输出包含 `{}` 字符，被 `.format()` 误解析为占位符
**修复：** 将示例中的 `{` 和 `}` 转义为 `{{` 和 `}}`

#### 问题 2：配置文件格式错误
**症状：** `TOMLDecodeError: Illegal character '\n'`
**原因：** `config.toml` 中 `model_task = "actor"` 被分成两行
**修复：** 合并为单行，并确保 `enabled = true`

#### 问题 3：model_set 获取失败
**症状：** `WARNING | 意图生成失败：model_set 必须是非空 list`
**原因：** `LLMRequest` 需要 `model_set` 对象，而非字符串
**修复：** 使用 `get_model_config().get_task(self.model_task)` 获取模型集

#### 问题 4：异常处理增强
**症状：** 事件处理器执行失败，无详细日志
**原因：** `execute()` 和 `update_goals()` 未捕获异常
**修复：** 添加 `try/except` 块，记录详细堆栈追踪

---

## 下一步

1. **重启 Bot** - 让框架加载重构后的插件
2. **私聊测试** - 观察意图生成效果
3. **日志监控** - 检查 `LLM 生成了 N 个意图候选` 日志
4. **调优提示词** - 根据实际效果调整 `INTENT_GENERATION_PROMPT`

---

## 验证命令

```bash
# 测试模块导入
python -c "
from plugins.intent_plugin.intent_engine import IntentEngine, Situation
from plugins.intent_plugin.intent_generator import IntentGenerator
from plugins.intent_plugin.config import IntentConfig
print('All imports OK!')
"

# 测试配置加载
python -c "
from plugins.intent_plugin.config import IntentConfig
c = IntentConfig.load_for_plugin('intent_plugin')
print('Config OK:')
print('  social.enabled:', c.social.enabled)
print('  emotional.enabled:', c.emotional.enabled)
print('  growth.enabled:', c.growth.enabled)
print('  generation.model_task:', c.generation.model_task)
"
```

---

## 结论

意图插件已完成从**预设模版**到**LLM 动态生成**的重构。

**关键区别：**
- **KFC (kokoro_flow_chatter)** = 心流（moment-to-moment，当下这一秒的内心活动）
- **Intent Plugin** = 持久化短期目标（跨越多轮对话的目标导向）

**核心设计理念：** 提供方向指引（意图分类），而非固定模版（预设意图）。爱莉希雅可以自由决定接下来几轮对话想要完成什么。

---

**重构完成时间：** 2026-03-18
**版本：** 2.0.0
