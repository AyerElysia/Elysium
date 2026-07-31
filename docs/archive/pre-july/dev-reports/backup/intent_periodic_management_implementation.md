# 意图周期性管理功能实现报告

**日期：** 2026-03-19
**状态：** 已完成
**版本：** 1.3.0

---

## 功能说明

为解决意图生成器每轮对话都生成新意图导致的过度干预问题，新增**周期性意图管理**功能：

- 支持配置意图生成间隔（每 N 条消息触发一次）
- 基于现有意图列表，LLM 可决定添加/更新/保留/删除操作
- 保留向后兼容接口，现有代码无需修改

---

## 核心设计

### 触发流程

```
用户发送消息
    ↓
ON_CHATTER_STEP 事件触发
    ↓
GoalTracker 更新消息计数
    ↓
检查是否到达生成间隔 ──── 否 ────→ 跳过生成，继续追踪
    ↓ 是
调用 IntentEngine.manage_intents()
    ↓
LLM 分析情境 + 现有意图列表
    ↓
生成操作列表 (add/update/keep/remove)
    ↓
验证、过滤、排序
    ↓
仅返回 add 操作的意图
    ↓
创建新目标并添加到活跃列表
    ↓
重置消息计数器
```

### 操作类型

| 操作类型 | 说明 | 使用场景 |
|----------|------|----------|
| **add** | 添加新意图 | 发现新的对话方向或目标 |
| **update** | 更新意图 | 现有目标有进展，更新进度 |
| **keep** | 保留意图 | 目标仍在进行中，无需修改 |
| **remove** | 删除意图 | 目标已完成或不再相关 |

> **注：** 当前版本仅实际处理 `add` 操作，其他操作类型为未来扩展保留。

---

## 文件修改清单

### 1. `plugins/intent_plugin/config.py`
新增配置字段：

```python
@config_section("generation")
class GenerationSection(SectionBase):
    intent_generation_interval: int = Field(
        default=3,
        description="意图生成间隔（每 N 条消息触发一次意图生成）",
    )
```

---

### 2. `config/plugins/intent_plugin/config.toml`
新增配置项：

```toml
[generation]
intent_generation_interval = 3
```

---

### 3. `plugins/intent_plugin/intent_generator.py`

#### 修改 1：更新方法签名
```python
async def generate_intents(
    self,
    situation: "Situation",
    recent_messages: list[str] = None,
    active_goals: list["Goal"] = None,  # 新增参数
) -> list[Intent]:
```

#### 修改 2：更新 `_build_prompt` 方法
```python
def _build_prompt(
    self,
    situation: "Situation",
    recent_messages: list[str],
    active_goals: list["Goal"] = None,  # 新增参数
) -> str:
    # 构建活跃意图上下文
    active_goals_context = "无活跃目标"
    if active_goals:
        goal_parts = []
        for goal in active_goals[:5]:
            step_count = len(goal.steps) if goal.steps else 0
            goal_parts.append(
                f"  - [{goal.intent_name}] {goal.objective} "
                f"(进度：{goal.current_step}/{step_count})"
            )
        active_goals_context = "\n".join(goal_parts)

    prompt = INTENT_GENERATION_PROMPT.format(
        situation_context=situation_context,
        active_goals_context=active_goals_context,
    )
```

#### 修改 3：更新 `INTENT_GENERATION_PROMPT`
新增操作类型说明和活跃目标上下文占位符：

```
## 操作类型
你可以对意图进行以下操作：
- **add** - 添加新意图：发现新的对话方向或目标
- **update** - 更新意图：现有目标有进展，更新进度
- **keep** - 保留意图：目标仍在进行中，无需修改
- **remove** - 删除意图：目标已完成或不再相关

## 当前情境
{situation_context}

## 当前活跃目标
{active_goals_context}
```

#### 修改 4：更新 `_parse_response` 方法
```python
def _parse_response(self, content: str) -> list[Intent]:
    # ... JSON 解析 ...

    intents = []
    for item in data:
        operation = item.get("operation", "add")

        # 只处理 add 操作返回 Intent 对象（保持向后兼容）
        if operation == "add":
            intent = self._create_intent(item)
            if intent:
                intents.append(intent)

    return intents
```

#### 修改 5：清理导入
```python
# 修改前
from .intent_engine import IntentOperation, Goal

# 修改后
from .intent_engine import Goal
```

---

### 4. `plugins/intent_plugin/intent_engine.py`

#### 修改 1：重构 `manage_intents` 方法
```python
async def manage_intents(
    self,
    situation: Situation,
    active_goals: list[Goal],
) -> list[IntentOperation]:
    # 1. 使用 LLM 生成意图候选（只返回 add 操作的意图）
    candidate_intents = await self.intent_generator.generate_intents(
        situation=situation,
        recent_messages=situation.recent_messages,
        active_goals=active_goals,
    )

    if not candidate_intents:
        logger.debug("LLM 未生成有效意图")
        return []

    # 2. 将意图转换为 add 操作
    operations = []
    for intent in candidate_intents:
        op = IntentOperation(operation="add", intent=intent)
        operations.append(op)

    # 3. 验证和过滤操作
    # 4. 应用优先级排序
    # ...
```

#### 修改 2：保留 `generate_candidates` 向后兼容
```python
async def generate_candidates(
    self,
    situation: Situation,
    active_goals: list[Goal],
) -> list[Intent]:
    """生成候选意图列表（LLM 驱动）- 旧接口，兼容调用"""
    operations = await self.manage_intents(situation, active_goals)
    # 只返回新增的意图
    return [op.intent for op in operations if op.operation == "add" and op.intent]
```

---

### 5. `plugins/intent_plugin/goal_tracker.py`

#### 修改 1：更新 `execute` 方法
```python
async def execute(
    self,
    event_name: str,
    params: dict[str, Any],
) -> tuple[EventDecision, dict[str, Any]]:
    stream_id = params.get("stream_id")
    if not stream_id:
        return EventDecision.SUCCESS, params

    # 更新消息计数
    self._message_counts[stream_id] = self._message_counts.get(stream_id, 0) + 1
    current_count = self._message_counts[stream_id]

    # 检查是否到达意图生成间隔
    should_generate_intents = self._should_generate_intents(stream_id)

    if should_generate_intents:
        # 更新目标列表（可能触发新意图）
        await self.goal_manager.update_goals(
            situation,
            max_active=self._get_max_active_intents(),
        )
        # 重置计数器
        self._message_counts[stream_id] = 0
        logger.debug(f"已触发意图生成（第 {current_count} 条消息）")
    else:
        logger.debug(f"意图生成冷却中（{current_count}/{self._get_intent_interval()}）")

    # 检查每个活跃目标的进度
    self._check_goal_progress(context)
    await self._update_system_reminder(stream_id)

    return EventDecision.SUCCESS, params
```

#### 修改 2：新增辅助方法
```python
def _get_intent_interval(self) -> int:
    """获取意图生成间隔"""
    if self.intent_engine.config and hasattr(self.intent_engine.config, "generation"):
        return self.intent_engine.config.generation.intent_generation_interval
    return 3  # 默认值

def _should_generate_intents(self, stream_id: str) -> bool:
    """检查是否应该生成意图"""
    current_count = self._message_counts.get(stream_id, 0)
    interval = self._get_intent_interval()
    return current_count >= interval
```

---

## 配置说明

### 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `generation.intent_generation_interval` | int | 3 | 意图生成间隔（每 N 条消息触发一次） |

### 修改配置

编辑 `config/plugins/intent_plugin/config.toml`：

```toml
[generation]
intent_generation_interval = 5  # 每 5 条消息生成一次意图
```

### 推荐配置

| 场景 | 推荐值 | 说明 |
|------|--------|------|
| 活跃对话 | 3-5 | 平衡干预频率和响应速度 |
| 深度对话 | 5-8 | 减少打断，让对话自然流动 |
| 新用户 | 2-3 | 更积极地生成引导意图 |

---

## 验证方法

### 1. 重启 Bot
```bash
# 停止 Bot
Ctrl+C

# 重新启动
python -m src.app.main
```

### 2. 观察日志
```
[DEBUG] intent_plugin | 意图生成冷却中（1/3）
[DEBUG] intent_plugin | 意图生成冷却中（2/3）
[DEBUG] intent_plugin | 已触发意图生成（第 3 条消息）
[INFO] intent_plugin | 生成 2 个意图操作：['add', 'add']
```

### 3. 验证间隔生效
- 发送 3 条消息，观察第 3 条消息后是否触发意图生成
- 检查日志中 `意图生成冷却中` 和 `已触发意图生成` 的交替出现

---

## 技术细节

### 计数器隔离
消息计数器按 `stream_id` 隔离，确保不同对话流互不干扰：
```python
self._message_counts: dict[str, int] = {}  # stream_id -> count
```

### 向后兼容性
- `generate_candidates()` 方法保持不变，内部调用 `manage_intents()` 并过滤
- `generate_intents()` 返回类型保持 `list[Intent]`
- 现有代码无需修改即可享受新功能

### 扩展性
`IntentOperation` 支持四种操作类型，为未来扩展预留空间：
```python
@dataclass
class IntentOperation:
    operation: Literal["add", "update", "keep", "remove"]
    intent: Intent | None = None
    intent_id: str | None = None
    progress: int | None = None
    reason: str | None = None
```

---

## 与原版本的对比

| 项目 | 修复前 | 修复后 |
|------|--------|--------|
| 生成频率 | 每轮对话 | 每 N 轮对话（可配置） |
| 意图上下文 | 无现有意图信息 | 包含活跃目标列表 |
| 操作类型 | 仅支持 add | 支持 add/update/keep/remove |
| 计数器 | 全局单一计数 | 按 stream_id 隔离 |
| 重置机制 | 无 | 触发后自动重置 |

---

## 注意事项

1. **间隔值不宜过小** - 过小（如 1-2）会导致频繁生成，失去周期性意义
2. **间隔值不宜过大** - 过大（如 10+）会导致意图更新滞后
3. **stream_id 隔离** - 不同对话流独立计数，互不影响
4. **重置时机** - 触发意图生成后立即重置计数器，确保间隔准确

---

## 下一步建议

1. **支持 update/remove 操作** - 当前仅处理 add 操作，可扩展为支持完整操作类型
2. **动态调整间隔** - 根据对话活跃度自动调整间隔值
3. **意图优先级衰减** - 长时间未推进的意图自动降低优先级
4. **用户反馈整合** - 根据用户对意图的响应调整生成策略

---

**完成时间：** 2026-03-19
**版本：** 1.3.0
