# 内驱力系统（Inner Drives System）

> 文档状态：权威文档，与代码同步截至 2026-07-31。
> 代码位置：`plugins/life_engine/curiosity/`（285 行）+ `drives/`（212 行）+ `narrative/`（309 行）+ `streams/`（696 行）。
> 本文是内驱力专题的权威文档；凡与本文冲突，以本文和当前代码为准。

---

## 0. 一句话定位

内驱力系统是数字生命的**自主动机层**：好奇心引擎发现"值得靠近的刺点"，思考流维持"一直在琢磨的线索"，冲动引擎将内部状态转化为"你可能想做"的建议，叙事沉淀器让她回望来路并用自己的语言讲述——四者协作产生自主意向，送入心跳决策，但永远只是建议，不是命令。

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      心跳 Prompt 装配                             │
│  注入：好奇牵引 + 思考流进展 + 冲动建议 + 叙事邀请               │
├────────────┬───────────────┬────────────────┬───────────────────┤
│  好奇心引擎 │   思考流       │   冲动引擎      │   叙事沉淀器      │
│ CuriosityEngine│ThoughtStream-│ ImpulseEngine │ NarrativeStore   │
│            │  Manager      │                │                   │
│ 异步判断    │  持久兴趣      │  状态→建议     │  回望→讲述        │
│ "有刺点吗" │  "在琢磨什么"  │  "你可能想"    │  "来路意味什么"   │
├────────────┴───────────────┴────────────────┴───────────────────┤
│                      设计哲学                                     │
│  产生建议，不产生命令。LLM 保留最终判断权。                        │
│  主体性不可侵犯：系统只摆出素材，她决定如何回应。                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. 好奇心引擎（CuriosityEngine）

**文件**：`curiosity/engine.py`（282 行）

### 2.1 定位

> 这个模块只维护"值得靠近的疑点/刺点"，不直接行动、不发消息、不调用工具。
> 它的输出作为 life_chatter 的 transient suffix 被主体看到，由表达层自行决定是否追问、观察、搜索或放下。

### 2.2 触发时机

由 `LifeEngineService._schedule_curiosity_review()` 在消息到达后异步调度（非阻塞后台任务）。

### 2.3 工作流程

```
消息/事件到达
    │
    ▼ 异步后台
收集上下文（最近聊天 + 新事件 + 前一次好奇信号）
    │
    ▼ LLM 判断（life_curiosity 任务）
解析 CuriositySignal
    │
    ▼ 持久化
workspace/.life_curiosity/signal.json
    │
    ▼ 下次心跳/chatter 时
format_for_prompt() → 注入 suffix
```

### 2.4 CuriositySignal 数据模型

```python
@dataclass
class CuriositySignal:
    active: bool          # 是否有活跃刺点
    anchor: str           # 刺点锚点（20字以内）
    why: str              # 为什么让主体想再看一眼
    unknown: str          # 还没有闭合的地方
    approach: str         # 怎样轻轻靠近（不是命令）
    confidence: float     # 置信度 [0, 1]
    tags: list[str]       # 可选标签
    source_event_id: str  # 来源事件
    source_stream_id: str # 来源聊天流
```

### 2.5 好奇准则

| 值得好奇 | 不值得好奇 |
| --- | --- |
| 表层说法和深层意味不一致 | 已经足够明确的问候 |
| 预期和实际反应不一致 | 纯命令执行 |
| 反复出现却没有被理解 | 无关噪声 |
| 用户在暗示但没有说透 | 只会把主体拖进机械服务状态的细枝末节 |
| 对象有可被再次观察的细节 | |

### 2.6 Prompt 注入格式

```markdown
### 好奇牵引
这是同一主体的异步好奇过程留下的轻量观察，不是命令；是否靠近由你自己决定。
- 刺点：她说的"还好"可能不是真的还好
- 牵引：语气和用词与平时不同
- 未闭合：她是否在回避什么
- 可轻轻靠近：下次对话时留意她的措辞变化
```

---

## 3. 思考流（ThoughtStreamManager）

**文件**：`streams/manager.py`（~450 行）+ `streams/models.py`（~100 行）

### 3.1 定位

> 不是 TODO（任务），不是 Project（项目），而是"我最近一直在琢磨这件事"的持久兴趣。
> 给爱莉在心跳间有事可想、有事可追踪。

### 3.2 ThoughtStream 数据模型

```python
@dataclass
class ThoughtStream:
    id: str
    title: str                    # 人类可读标题
    created_at: str               # 创建时间
    last_advanced_at: str         # 上次推进时间
    advance_count: int            # 推进次数
    curiosity_score: float        # 好奇心强度 [0, 1]（半衰期衰减）
    last_thought: str             # 最近一次内心独白
    related_memories: list[str]   # 关联记忆节点 ID
    status: str                   # active / dormant / completed
    last_focused_at: str          # 上次进入注意力焦点
    last_decay_at: str            # 衰减锚点
    revision: int                 # 单调递增版本号
```

### 3.3 生命周期

```
create() → active
    │
    ├── advance()：推进（新想法/联想/沉淀）→ curiosity_score 回升
    │
    ├── 超过 dormancy_hours 未推进 → dormant（自动休眠）
    │
    ├── reactivate()：重新激活
    │
    └── complete()：思考闭合 → completed
```

### 3.4 好奇心衰减

采用**半衰期衰减**模型：

```
curiosity_score × 0.5^(hours / half_life)
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `half_life_hours` | 12.0 | 半衰期（12小时后强度减半） |
| `curiosity_floor` | 0.15 | 衰减下限（不会完全消失） |

衰减是 lazy 的：只在访问时计算，不后台轮询。

### 3.5 注意力焦点

- `last_focused_at`：独立于 advance，标记"上次真正关注这条流"
- `is_focused(window_minutes)`：判断是否在焦点窗口内
- 心跳 prompt 只注入焦点内的思考流进展

### 3.6 容量控制

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `max_active` | 5 | 最大活跃思考流数 |
| `dormancy_hours` | 24 | 超时自动休眠 |

超出 max_active 时，curiosity_score 最低的流被强制休眠。

### 3.7 持久化

```
workspace/thoughts/
├── streams.json    ← 索引（所有流的元数据）
└── {id}.md         ← 每条流的详细思考记录
```

### 3.8 与 Chatter 的集成

- **Delta 追踪**：`revision` 单调递增，chatter 通过 cursor 只注入新增进展
- **Prompt 注入**：活跃思考流的 title + last_thought + curiosity_score

---

## 4. 冲动引擎（ImpulseEngine）

**文件**：`drives/impulse.py`（~100 行）+ `drives/rules.py`（~110 行）

### 4.1 设计哲学

> 产生建议，不产生命令。LLM 保留最终判断权。

### 4.2 工作机制

```
心跳装配时
    │
    ▼ evaluate(neuromod_state, context)
遍历所有 ImpulseRule
    │
    ├── condition(state, context) == True?
    ├── check_cooldown() == True?
    │
    ▼ 两者都满足
生成 ImpulseSuggestion
    │
    ▼ format_for_prompt()
注入心跳 prompt
```

### 4.3 默认规则集

| 规则 | 条件 | 建议 | 冷却 |
| --- | --- | --- | --- |
| `thought_deepen` | 有活跃思考流 | 继续深入、联想或沉淀 | 20min |
| `curiosity_engage` | 有好奇刺点 | 靠近它、开思考流承接、或放下 | 45min |
| `learning_reflect` | 有学习进展 | 看看新验证的领悟或技能目录 | 60min |
| `river_consolidate` | 有待沉淀的长河留痕 | 回望并写下它对你意味着什么 | 120min |
| `intent_review` | 有自主意向 | 看看延迟意向，决定是否调整 | 90min |
| `todo_attend` | 有紧急/逾期 TODO | 承诺提醒（不是命令执行） | 45min |

### 4.4 Prompt 注入格式

```markdown
### 内在冲动

基于当前状态，你可能想：

- 你有未完成的思考流，也许可以继续深入、联想或沉淀
- 好奇层留下了刺点；如果你在意，可以靠近它

（这些只是建议，你可以选择遵循或不遵循。）
```

---

## 5. 叙事沉淀器（NarrativeStore）

**文件**：`narrative/store.py`（~200 行）

### 5.1 设计哲学

> 可言说法则：**沉淀必须经过她的语言。** 系统只做两件事——
> 摆出长河里还没被讲述过的转折点（pending），和保管她写下的叙事（consolidate）。
> 系统绝不替她总结人生；"没什么值得说的"（quiet）也是一次完整的回望。

### 5.2 工作流程

```
长河（Life Trace）积累留痕
    │
    ▼ pending_moments()
筛出未被讲述的转折点（cursor 之后）
    │
    ▼ 冲动引擎建议 / 主体主动
她用自己的语言回望
    │
    ▼ consolidate(text, quiet)
保管叙事 + 推进游标
    │
    ├── quiet=True → 只推进游标，不写自传
    └── quiet=False → 追加到 autobiography.md
```

### 5.3 NarrativeEntry 数据模型

```python
@dataclass
class NarrativeEntry:
    entry_id: str        # 唯一标识
    timestamp: str       # 写入时间
    period_start: str    # 回望区间起点
    period_end: str      # 回望区间终点
    moment_count: int    # 涉及的转折点数
    quiet: bool          # 是否为安静回望（无话可说）
    text: str            # 她写下的叙事文本
```

### 5.4 存储结构

```
workspace/
├── .life_narrative/
│   ├── entries.jsonl    ← append-only 叙事记录
│   └── state.json       ← 游标状态（cursor_timestamp）
└── narrative/
    └── autobiography.md ← 自传正文（她可读可引用）
```

### 5.5 防递归设计

叙事自己的入河记录（`kind=narrative`）永远不算待沉淀素材，否则讲述本身会催生下一次讲述。

### 5.6 防催促设计

`mark_invited()` 记录回望邀请的呈现时间，防止心跳反复催促。

---

## 6. 四者协作

```
消息到达 ──→ 好奇心引擎（异步判断刺点）
                    │
                    ▼
心跳装配 ←── 思考流（持久兴趣 + 衰减）
    │              │
    │              ▼ advance()
    │         好奇心回升
    │
    ├── 冲动引擎（评估状态 → 建议）
    │       │
    │       ├── "有刺点 → 靠近"
    │       ├── "有思考流 → 深入"
    │       └── "有长河留痕 → 回望"
    │
    └── 叙事沉淀器（摆出 pending → 她讲述）
```

**关键约束**：所有输出都是建议/观察/素材，不是命令。最终行动权完全在 LLM（主体）手中。

---

## 7. 配置

通过 `config/plugins/life_engine/config.toml` 配置：

```toml
[streams]
enabled = true
max_active_streams = 5
dormancy_threshold_hours = 24
curiosity_decay_half_life_hours = 12.0
curiosity_floor = 0.15

[drives]
enabled = true

[curiosity]
enabled = true
max_prompt_chars = 1200
```

---

## 8. 文件索引

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `curiosity/engine.py` | 282 | 好奇心引擎（CuriosityEngine + CuriositySignal） |
| `drives/impulse.py` | ~100 | 冲动引擎（ImpulseEngine + ImpulseRule） |
| `drives/rules.py` | ~110 | 默认冲动规则集（6 条） |
| `narrative/store.py` | ~200 | 叙事沉淀器（NarrativeStore + NarrativeEntry） |
| `narrative/tools.py` | ~100 | 叙事工具（nucleus_write_narrative） |
| `streams/manager.py` | ~450 | 思考流管理器（CRUD + 衰减 + 持久化） |
| `streams/models.py` | ~100 | ThoughtStream 数据模型 |
| `streams/tools.py` | ~150 | 思考流工具（nucleus_manage_thought_stream） |
