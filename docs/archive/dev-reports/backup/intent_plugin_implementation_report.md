# 自主意图与短期目标插件 - 实现报告

**文档版本**: 1.1
**创建日期**: 2026-03-18
**更新日期**: 2026-03-18
**作者**: Claude

---

## 1. 实现概述

### 1.1 项目背景

为 Neo-MoFox 聊天机器人添加**自主意图与短期目标系统**，使模型具备内在驱动力，能够：

- 基于对话情境自动生成"想要做某事"的意图
- 将抽象意图拆解为可执行的具体步骤
- 后台监控目标进度，动态调整优先级
- 通过 System Reminder 自然融入对话，不暴露系统痕迹

### 1.2 实现状态

| 阶段 | 内容 | 状态 |
|------|------|------|
| **Phase 1 MVP** | 核心框架 + 6 个预定义意图 | ✅ 已完成 |
| **Phase 2** | 情境分析增强 + 进度追踪优化 | ⏳ 待实现 |
| **Phase 3** | 冲突解决 + 多目标协作 | ⏳ 待实现 |
| **Phase 4** | 持久化 + 跨对话连续性 | ⏳ 待实现 |

---

## 2. 文件清单

### 2.1 插件代码 (11 个文件)

| 文件 | 行数 | 职责 |
|------|------|------|
| `plugins/intent_plugin/manifest.json` | 24 | 插件清单，注册组件 |
| `plugins/intent_plugin/__init__.py` | 3 | 模块入口 |
| `plugins/intent_plugin/plugin.py` | 86 | 插件入口类，生命周期管理 |
| `plugins/intent_plugin/models.py` | 158 | 数据结构定义 |
| `plugins/intent_plugin/config.py` | 108 | 配置类定义 |
| `plugins/intent_plugin/default_intents.py` | 162 | 6 个预定义意图 + 步骤模板 |
| `plugins/intent_plugin/intent_engine.py` | 288 | 意图生成引擎 |
| `plugins/intent_plugin/goal_manager.py` | 150 | 目标管理器 |
| `plugins/intent_plugin/goal_tracker.py` | 297 | 目标追踪器 (EventHandler) |
| `plugins/intent_plugin/service.py` | 153 | 对外服务接口 |
| `config/plugins/intent_plugin/config.toml` | 76 | 配置文件 |

**总计**: ~1500 行代码

---

## 3. 架构设计

### 3.1 系统架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      Actor Model Context                     │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ System Reminder (动态注入)                             │  │
│  │ "你现在有个小小心愿：想知道她今天过得怎么样..."           │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                   IntentPlugin Components                    │
│                                                               │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────┐  │
│  │ IntentEngine    │───→│ GoalManager     │───→│ Service │  │
│  │ 意图生成器       │    │ 目标管理器       │    │ 状态    │  │
│  └─────────────────┘    └─────────────────┘    └─────────┘  │
│           ↑                     ↓                           │
│  ┌─────────────────┐    ┌─────────────────┐                 │
│  │ ContextAnalyzer │    │ GoalTracker     │                 │
│  │ 情境分析器       │    │ 执行追踪器       │                 │
│  └─────────────────┘    └─────────────────┘                 │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ EventHandler (订阅 ON_CHATTER_STEP 事件)                  ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

### 3.2 组件职责

| 组件 | 类型 | 职责 |
|------|------|------|
| `IntentEngine` | Service | 基于情境生成意图，计算动态优先级 |
| `GoalManager` | Service | 目标分解、调度、冲突解决 |
| `GoalTracker` | EventHandler | 追踪执行进度，更新 System Reminder |
| `IntentService` | Service | 状态管理、对外接口 |

---

## 4. 数据结构

### 4.1 意图 (Intent)

```python
@dataclass
class Intent:
    id: str                          # 唯一标识，如 "social_curiosity"
    name: str                        # 显示名称，如 "了解用户"
    description: str                 # 详细描述
    category: str                    # 分类：social/emotional/growth
    trigger_context: dict            # 触发情境
    trigger_conditions: list[str]    # 触发条件列表
    base_priority: int               # 基础优先级 (1-10)
    dynamic_boost: dict[str, int]    # 情境加成
    expiry_messages: int             # 过期消息数
    expiry_seconds: int              # 过期秒数
    goal_templates: list[str]        # 目标模板
```

### 4.2 目标 (Goal)

```python
@dataclass
class Goal:
    id: str                          # 唯一标识 (UUID)
    intent_id: str                   # 关联意图 ID
    intent_name: str                 # 意图名称
    objective: str                   # 目标描述
    steps: list[GoalStep]            # 执行步骤
    current_step: int                # 当前步骤索引
    status: GoalStatus               # 状态枚举
    priority: int                    # 优先级
    created_at: datetime             # 创建时间
    updated_at: datetime             # 更新时间
    trigger_context: dict            # 触发情境快照
```

### 4.3 步骤 (GoalStep)

```python
@dataclass
class GoalStep:
    index: int                       # 步骤索引
    action: str                      # 行动描述
    keywords: list[str]              # 完成关键词
    optional: bool                   # 是否可选
```

### 4.4 状态枚举 (GoalStatus)

```python
class GoalStatus(Enum):
    PENDING = "pending"              # 待启动
    ACTIVE = "active"                # 进行中
    COMPLETED = "completed"          # 已完成
    ABANDONED = "abandoned"          # 已放弃
    FAILED = "failed"                # 失败
    PAUSED = "paused"                # 已暂停
```

---

## 5. 核心模块实现

### 5.1 情境分析 (`GoalTracker._analyze_situation`)

检测 9 种情境信号：

| 信号 | 检测方法 | 触发示例 |
|------|----------|----------|
| `is_new_user` | 对话轮次 ≤ 2 | 首次聊天 |
| `negative_emotion` | 负面情绪词 | 难过/伤心/烦/生气 |
| `tired` | 疲惫词 | 累/困/没精神 |
| `confused` | 困惑词 | 不知道/不懂/不明白 |
| `user_mention_detail` | 个人信息词 | 我叫/喜欢/想要/计划 |
| `user_choice` | 选择词 | 选择/选/更喜欢 |
| `user_opinion` | 观点词 | 觉得/认为/感觉 |
| `flat_mood` | 短句/语气词 | 嗯/哦/好的/还行 |
| `deep_conversation` | (预留) | - |

### 5.2 意图生成 (`IntentEngine.generate_candidates`)

**筛选流程**：
```
1. 检查配置开关 → 2. 检查情境匹配 → 3. 检查高优先级目标
→ 4. 检查相同意图 → 5. 检查冷却 → 6. 计算动态优先级
```

**动态优先级公式**：
```
final_priority = min(base_priority + Σ(context_boost), 10)
```

### 5.3 目标分解 (`decompose_to_steps`)

根据意图名称匹配步骤模板：

```python
GOAL_STEP_TEMPLATES = {
    "问问": [
        ("用轻松的方式开启话题", ["今天", "怎么样", "如何"], False),
        ("倾听并回应用户回答", ["开心", "累", "还行", "不错"], False),
        ("自然结束或延续话题", [], True),
    ],
    "记住": [
        ("引导用户分享更多", ["喜欢", "讨厌", "想要"], False),
        ("确认信息并表达理解", ["对", "是的", "没错"], False),
    ],
}
```

### 5.4 进度追踪 (`GoalTracker._check_goal_progress`)

**关键词匹配机制**：
```python
def _check_step_completed(self, step: GoalStep, text: str) -> bool:
    if not step.keywords:
        return True  # 没有关键词，默认完成

    for keyword in step.keywords:
        if keyword.lower() in text:
            return True

    return False
```

### 5.5 System Reminder 注入

**注入位置**：`actor` bucket，key 为 `当前小想法`

**输出格式**：
```markdown
## 💭 你现在的小想法

你心里想着：**{objective}**

具体来说，可以试着：{step_hint}

（自然地聊就好，不用太刻意~）
```

---

## 6. 预定义意图库

### 6.1 社交类 (2 个)

| 意图 ID | 名称 | 基础优先级 | 触发条件 | 动态加成 |
|--------|------|------------|----------|----------|
| `social_curiosity` | 了解用户 | 5 | 新用户/平淡氛围 | 新用户 +2, 平淡 +1 |
| `remember_details` | 记住细节 | 6 | 用户提到个人信息 | 喜好 +2, 计划 +1 |

### 6.2 情感类 (2 个)

| 意图 ID | 名称 | 基础优先级 | 触发条件 | 动态加成 |
|--------|------|------------|----------|----------|
| `emotional_support` | 情感支持 | 9 | 负面情绪/疲惫 | 负面 +1, 疲惫 +1 |
| `create_surprise` | 制造惊喜 | 4 | 平淡氛围 | 平淡 +2 |

### 6.3 成长类 (2 个)

| 意图 ID | 名称 | 基础优先级 | 触发条件 | 动态加成 |
|--------|------|------------|----------|----------|
| `learn_preference` | 学习喜好 | 5 | 用户表达观点 | 观点 +2 |
| `build_memory` | 构建回忆 | 6 | 深度对话 | 深度 +2 |

---

## 7. 配置系统

### 7.1 配置结构

```toml
# 意图开关
[social_intents]
social_curiosity = true
remember_details = true

[emotional_intents]
emotional_support = true
create_surprise = true
detect_mood = true

[growth_intents]
learn_preference = true
build_memory = true
knowledge_sharing = false

# 全局设置
[settings]
max_active_intents = 3
min_priority_threshold = 4
goal_timeout_messages = 20
goal_timeout_seconds = 600
intent_cooldown_messages = 30

# Reminder 配置
[reminder]
show_current_goal = true
show_progress_hint = true
tone_style = "natural"
```

### 7.2 热重载支持

配置类继承自 `BaseConfig`，支持运行时修改后自动生效。

---

## 8. 工作流程

### 8.1 完整生命周期

```
[用户发送消息]
       ↓
[ON_CHATTER_STEP 事件触发]
       ↓
[GoalTracker.execute()]
  ├─ 获取 stream_id 和聊天上下文
  ├─ 分析情境 (Situation)
  ├─ GoalManager.update_goals()
  │    ├─ 清理完成的目标
  │    ├─ 减少冷却计数
  │    ├─ 生成候选意图
  │    ├─ 创建新目标
  │    └─ 重新排序
  ├─ 检查目标进度
  │    └─ 关键词匹配检测步骤完成
  └─ 更新 System Reminder
       └─ 注入到 actor bucket
```

### 8.2 意图触发示例

**场景**：用户说 "今天好累啊"

```
1. GoalTracker 检测到 negative_emotion=true, tired=true
2. 情境传递到 IntentEngine
3. EMOTIONAL_SUPPORT 意图被触发：
   - 基础优先级 9
   - 负面情绪加成 +1
   - 疲惫加成 +1
   - 最终优先级 = 10
4. 创建目标："安慰一下她"
5. 分解步骤：
   - 步骤 1: 表达关心 (关键词：累/辛苦/不容易)
   - 步骤 2: 提供支持 (关键词：休息/放松/帮忙)
   - 步骤 3: 转移注意力 (可选)
6. 更新 System Reminder:
   "你现在有个小想法：安慰一下她
    具体来说，可以试着：表达关心"
7. 下次 LLM 调用时，模型会自然地说出关心的话
```

---

## 9. 质量保障

### 9.1 代码检查

```bash
# Ruff lint 检查
uv run ruff check plugins/intent_plugin/
# 结果：All checks passed!

# 代码格式化
uv run ruff format plugins/intent_plugin/
# 结果：6 files reformatted, 3 files left unchanged
```

### 9.2 设计原则

| 原则 | 实现方式 |
|------|----------|
| **自然不刻意** | System Reminder 用"小想法"语气，不暴露系统痕迹 |
| **可打断可恢复** | 高优先级意图可覆盖低优先级，冷却机制防止重复 |
| **优先级动态** | 情境触发时动态提升优先级 |
| **配置可扩展** | TOML 配置控制开关、阈值、冷却时间 |
| **类型安全** | 完整的类型注解 + dataclass |

---

## 10. 待实现功能

### Phase 2: 情境分析增强

- [ ] 时间感知（沉默时长检测）
- [ ] 情绪强度分级（轻微/中等/强烈）
- [ ] 话题连续性分析
- [ ] 用户参与度检测

### Phase 3: 冲突解决

- [ ] 多目标优先级协商
- [ ] 目标合并策略
- [ ] 资源竞争处理

### Phase 4: 持久化

- [ ] 目标状态序列化到 JSON
- [ ] 跨对话恢复
- [ ] 历史统计分析

---

## 11. 使用示例

### 11.1 查询活跃目标

```python
from plugins.intent_plugin.service import IntentService

service = IntentService()
goals = service.get_active_goals()

for goal in goals:
    print(f"目标：{goal.objective}")
    print(f"进度：{goal.current_step}/{len(goal.steps)}")
    print(f"优先级：{goal.priority}")
```

### 11.2 查询统计信息

```python
stats = service.get_statistics()
print(f"总创建：{stats['total_created']}")
print(f"已完成：{stats['completed']}")
print(f"已放弃：{stats['abandoned']}")
print(f"完成率：{stats['completion_rate']:.2%}")
```

---

## 12. 后续计划

| 阶段 | 目标 | 预计工作量 |
|------|------|------------|
| **测试验证** | 加载插件，验证意图触发 | 1-2 小时 |
| **Phase 2** | 情境分析增强 | 4-6 小时 |
| **Phase 3** | 冲突解决机制 | 4-6 小时 |
| **Phase 4** | 持久化支持 | 6-8 小时 |

---

## 13. 总结

Phase 1 MVP 已完成核心框架搭建，包括：

- ✅ 完整的组件架构（Engine + Manager + Tracker + Service）
- ✅ 6 个预定义意图覆盖社交/情感/成长三类
- ✅ 情境关键词检测机制
- ✅ 动态优先级计算
- ✅ 目标步骤分解与追踪
- ✅ System Reminder 自然注入
- ✅ 配置化开关和参数
- ✅ 通过 Ruff 代码质量检查

下一步将进入实际测试阶段，验证插件在 Neo-MoFox 中的运行效果。

---

## 14. 附录：问题排查与解决

### 14.1 插件未被识别的问题

**问题描述**: 插件加载列表中没有显示 `intent_plugin`

**原因分析**:

1. **manifest.json 格式不匹配**
   - 初始版本使用了旧的组件注册格式
   - Neo-MoFox 使用新的 `include` 数组格式，每个元素是对象而非字符串

2. **导入路径错误**
   - 错误：`from src.app.plugin_system.decorators import register_plugin`
   - 正确：`from src.app.plugin_system.base import register_plugin`

3. **Situation 类导入错误**
   - `Situation` 类定义在 `intent_engine.py`，不是 `models.py`
   - 需要修正导入路径

**解决方案**:

1. 重写 `manifest.json` 为新格式：
```json
{
  "include": [
    {
      "component_type": "service",
      "component_name": "intent_service",
      "dependencies": [],
      "enabled": true
    },
    {
      "component_type": "event_handler",
      "component_name": "goal_tracker",
      "dependencies": [],
      "enabled": true
    }
  ]
}
```

2. 修正 `plugin.py` 导入：
```python
from src.app.plugin_system.base import BasePlugin, register_plugin
```

3. 修正 `goal_tracker.py` 导入：
```python
from .models import Goal
from .intent_engine import IntentEngine, Situation
```

**验证方法**:
```bash
# 测试插件导入
python -c "from plugins.intent_plugin.plugin import IntentPlugin; print('OK')"

# 运行 lint 检查
uv run ruff check plugins/intent_plugin/
```

---

**附录**: 相关文档

- [技术方案](intent_plugin_design.md) - 完整 12 章设计文档
- [配置文件](../config/plugins/intent_plugin/config.toml)
- [源代码](../plugins/intent_plugin/)
