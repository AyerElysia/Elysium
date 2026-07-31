# 自主意图与短期目标插件 - 设计方案

**文档版本**: 1.0
**创建日期**: 2026-03-18
**作者**: Claude

---

## 1. 需求概述

### 1.1 核心目标

为 Neo-MoFox 聊天机器人添加**自主意图与短期目标系统**，使模型具备：

| 能力 | 描述 |
|------|------|
| **自主意图** | 基于对话情境，自动生成"想要做某事"的内在驱动力 |
| **短期目标** | 将抽象意图拆解为可执行的具体步骤 |
| **进度追踪** | 后台监控目标完成情况，动态调整优先级 |
| **自然融入** | 通过 System Reminder 巧妙引导，不暴露系统痕迹 |

### 1.2 设计哲学

```
┌─────────────────────────────────────────────────────────────┐
│                    核心设计理念                              │
│                                                             │
│  ✅ 自然不刻意 - 意图引导要像"内心想法"，而非系统指令         │
│  ✅ 可打断可恢复 - 用户打断后能暂停/恢复目标                 │
│  ✅ 优先级动态 - 根据对话情境实时调整                        │
│  ✅ 状态可持久 - 跨对话保持目标连续性                        │
│  ✅ 配置可扩展 - 支持自定义意图类型和触发条件                │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 架构设计

### 2.1 系统架构图

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
│                                                             │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────┐ │
│  │ IntentEngine    │───→│ GoalManager     │───→│ Service │ │
│  │ 意图生成器       │    │ 目标管理器       │    │ 状态    │ │
│  └─────────────────┘    └─────────────────┘    └─────────┘ │
│           ↑                     ↓                           │
│  ┌─────────────────┐    ┌─────────────────┐                 │
│  │ ContextAnalyzer │    │ GoalTracker     │                 │
│  │ 情境分析器       │    │ 执行追踪器       │                 │
│  └─────────────────┘    └─────────────────┘                 │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ EventHandler (订阅对话事件，更新状态)                     ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                      Storage Layer                           │
│  ┌─────────────────┐    ┌─────────────────┐                 │
│  │ intent_state.json │  │ goal_history.json │                │
│  └─────────────────┘    └─────────────────┘                 │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 组件职责

| 组件 | 职责 | 组件类型 |
|------|------|----------|
| `IntentEngine` | 基于情境生成意图，计算优先级 | Service |
| `GoalManager` | 目标分解、调度、冲突解决 | Service |
| `GoalTracker` | 追踪执行进度，判断完成状态 | EventHandler |
| `ContextAnalyzer` | 分析对话情境，提取关键信号 | Service |
| `IntentService` | 状态管理、持久化、对外接口 | Service |

---

## 3. 数据结构

### 3.1 意图 (Intent)

```python
@dataclass
class Intent:
    """意图定义"""
    id: str                          # 唯一标识，如 "social_curiosity"
    name: str                        # 显示名称，如 "了解用户"
    description: str                 # 详细描述
    category: str                    # 分类：social/emotional/growth

    # 触发条件
    trigger_context: str             # 触发情境描述
    trigger_conditions: list[str]    # 具体触发条件（关键词/情境）

    # 优先级配置
    base_priority: int               # 基础优先级 (1-10)
    dynamic_boost: dict[str, int]    # 情境加成 {情境：加成值}

    # 过期配置
    expiry_messages: int             # 多少条消息后过期
    expiry_seconds: int              # 多少秒后过期

    # 目标模板
    goal_templates: list[str]        # 目标模板列表
```

### 3.2 目标 (Goal)

```python
@dataclass
class Goal:
    """短期目标"""
    id: str                          # 唯一标识 (UUID)
    intent_id: str                   # 所属意图 ID
    objective: str                   # 目标描述

    # 执行步骤
    steps: list[GoalStep]            # 步骤列表
    current_step: int                # 当前步骤索引 (0-based)

    # 状态追踪
    status: GoalStatus               # pending/active/completed/abandoned/failed
    created_at: datetime             # 创建时间
    updated_at: datetime             # 更新时间
    completed_at: datetime | None    # 完成时间

    # 上下文
    trigger_context: dict[str, Any]  # 触发时的上下文快照
    success_condition: str           # 成功条件描述
    notes: str                       # 备注/额外信息

    def is_completed(self) -> bool:
        return self.status == GoalStatus.COMPLETED

    def is_expired(self, message_count: int, max_age_seconds: int) -> bool:
        # 检查是否过期
        pass
```

### 3.3 目标步骤 (GoalStep)

```python
@dataclass
class GoalStep:
    """目标步骤"""
    index: int                       # 步骤索引
    action: str                      # 建议行动
    keywords: list[str]              # 成功关键词（用于自动检测）
    optional: bool                   # 是否可选步骤
```

### 3.4 目标状态枚举

```python
class GoalStatus(str, Enum):
    PENDING = "pending"              # 等待执行
    ACTIVE = "active"                # 正在执行
    COMPLETED = "completed"          # 已完成
    ABANDONED = "abandoned"          # 已放弃（用户打断/情境变化）
    FAILED = "failed"                # 失败（超时/无法完成）
    PAUSED = "paused"                # 已暂停（等待时机）
```

---

## 4. 核心模块设计

### 4.1 IntentEngine - 意图生成器

```python
class IntentEngine:
    """意图生成引擎

    基于当前对话情境，从预定义意图库中筛选并生成候选意图。
    """

    def __init__(self, config: IntentConfig):
        self.intents = self._load_intents(config)
        self.context_analyzer = ContextAnalyzer()

    def generate_candidates(
        self,
        context: ChatContext,
        active_goals: list[Goal],
    ) -> list[Intent]:
        """生成候选意图列表"""
        # 1. 分析当前情境
        situation = self.context_analyzer.analyze(context)

        # 2. 筛选触发的意图
        triggered = []
        for intent in self.intents:
            if self._should_trigger(intent, situation, active_goals):
                # 计算动态优先级
                priority = self._calculate_priority(intent, situation)
                intent.priority = priority
                triggered.append(intent)

        # 3. 按优先级排序
        return sorted(triggered, key=lambda x: x.priority, reverse=True)

    def _should_trigger(
        self,
        intent: Intent,
        situation: Situation,
        active_goals: list[Goal],
    ) -> bool:
        """判断意图是否应该触发"""
        # 检查触发条件
        if not self._check_trigger_conditions(intent, situation):
            return False

        # 检查是否有冲突的高优先级目标
        if self._has_higher_priority_goal(intent, active_goals):
            return False

        # 检查是否冷却中
        if self._is_in_cooldown(intent):
            return False

        return True

    def _calculate_priority(self, intent: Intent, situation: Situation) -> int:
        """计算动态优先级"""
        priority = intent.base_priority

        # 情境加成
        for context_key, boost in intent.dynamic_boost.items():
            if getattr(situation, context_key, False):
                priority += boost

        return min(priority, 10)  # 上限 10
```

### 4.2 GoalManager - 目标管理器

```python
class GoalManager:
    """目标管理器

    负责目标的创建、分解、调度、冲突解决。
    """

    def __init__(self, intent_engine: IntentEngine):
        self.intent_engine = intent_engine
        self.active_goals: list[Goal] = []
        self.goal_history: list[Goal] = []

    def update_goals(
        self,
        context: ChatContext,
        max_active: int = 3,
    ) -> list[Goal]:
        """更新目标列表（每次对话后调用）"""
        # 1. 清理过期/完成的目标
        self._cleanup_expired_goals()

        # 2. 生成候选意图
        candidate_intents = self.intent_engine.generate_candidates(
            context,
            self.active_goals
        )

        # 3. 从新意图创建目标
        for intent in candidate_intents:
            if len(self.active_goals) >= max_active:
                break

            # 跳过已有相同意图的目标
            if self._has_intent(intent.id):
                continue

            # 创建新目标
            goal = self._create_goal_from_intent(intent, context)
            self.active_goals.append(goal)

        # 4. 重新排序
        self.active_goals.sort(key=lambda g: g.priority, reverse=True)

        return self.active_goals

    def _create_goal_from_intent(
        self,
        intent: Intent,
        context: ChatContext
    ) -> Goal:
        """从意图创建目标"""
        # 选择一个目标模板
        template = random.choice(intent.goal_templates)

        # 分解为步骤
        steps = self._decompose_to_steps(template, intent)

        return Goal(
            id=generate_uuid(),
            intent_id=intent.id,
            objective=template,
            steps=steps,
            current_step=0,
            status=GoalStatus.ACTIVE,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            trigger_context=context.snapshot(),
        )

    def _decompose_to_steps(
        self,
        template: str,
        intent: Intent
    ) -> list[GoalStep]:
        """将目标分解为执行步骤"""
        # 预定义步骤模板
        step_templates = {
            "了解用户": [
                GoalStep(0, "自然地询问用户的近况", ["今天", "最近", "怎么样"], optional=False),
                GoalStep(1, "追问细节表达关心", ["为什么", "然后呢", "感觉"], optional=True),
                GoalStep(2, "记住关键信息", ["记住", "记下来"], optional=True),
            ],
            "制造惊喜": [
                GoalStep(0, "创造话题转折点", ["对了", "突然想到"], optional=False),
                GoalStep(1, "分享惊喜内容", ["给你", "看这个"], optional=False),
                GoalStep(2, "观察反应并回应", ["喜欢吗", "觉得"], optional=True),
            ],
        }

        return step_templates.get(intent.name, [
            GoalStep(0, "采取行动推进目标", [], optional=False),
        ])
```

### 4.3 GoalTracker - 目标追踪器

```python
class GoalTracker(BaseEventHandler):
    """目标执行追踪器

    订阅对话事件，检测目标进度，自动更新状态。
    """

    handler_name: str = "intent_goal_tracker"
    handler_description: str = "追踪目标执行进度"
    weight: int = 10  # 高优先级，确保最早执行

    init_subscribe: list[EventType | str] = [
        EventType.ON_CHATTER_STEP,
        EventType.ON_MESSAGE_RECEIVED,
    ]

    def __init__(self, plugin):
        super().__init__(plugin)
        self.goal_manager: GoalManager = plugin.get_goal_manager()
        self.message_count: int = 0

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """执行追踪"""
        self.message_count += 1

        # 获取对话上下文
        context = self._get_chat_context(params)

        # 检查每个活跃目标的进度
        for goal in self.goal_manager.active_goals:
            if goal.status != GoalStatus.ACTIVE:
                continue

            # 检测当前步骤是否完成
            if self._check_step_completed(goal, context):
                goal.current_step += 1
                goal.updated_at = datetime.now()

                # 检查是否全部完成
                if goal.current_step >= len(goal.steps):
                    goal.status = GoalStatus.COMPLETED
                    goal.completed_at = datetime.now()
                    logger.info(f"目标完成：{goal.objective}")

        # 清理完成的目标
        self.goal_manager.active_goals = [
            g for g in self.goal_manager.active_goals
            if g.status not in (GoalStatus.COMPLETED, GoalStatus.FAILED)
        ]

        # 更新 System Reminder
        await self._update_system_reminder()

        return EventDecision.SUCCESS, params

    def _check_step_completed(
        self,
        goal: Goal,
        context: ChatContext
    ) -> bool:
        """检查当前步骤是否完成"""
        current_step = goal.steps[goal.current_step]

        # 关键词匹配
        recent_messages = context.get_recent_messages(3)
        for msg in recent_messages:
            for keyword in current_step.keywords:
                if keyword in msg.content.lower():
                    logger.debug(
                        f"检测到关键词 '{keyword}'，步骤 {current_step.index} 完成"
                    )
                    return True

        # LLM 判断（备用方案）
        return await self._llm_judge_step(goal, context)

    def _llm_judge_step(self, goal: Goal, context: ChatContext) -> bool:
        """使用 LLM 判断步骤是否完成"""
        # TODO: 实现 LLM 判断逻辑
        pass
```

### 4.4 System Reminder 注入

```python
def build_intent_reminder(active_goals: list[Goal]) -> str:
    """构建意图目标的 System Reminder"""

    if not active_goals:
        return ""

    # 只取优先级最高的目标
    current_goal = active_goals[0]
    current_step = current_goal.steps[current_goal.current_step]

    reminder = f"""
## 💭 你现在的小想法

你心里想着：**{current_goal.objective}**

具体来说，可以试着：{current_step.action}

（自然地聊就好，不用太刻意~）
"""

    # 如果有多个目标，简单提一下其他的
    if len(active_goals) > 1:
        other_goals = active_goals[1:3]  # 最多显示 2 个
        reminder += "\n另外，你还稍微有点在意："
        for g in other_goals:
            reminder += f"\n- {g.objective}"

    return reminder
```

---

## 5. 意图库设计

### 5.1 预设意图列表

```python
PREDEFINED_INTENTS = [
    # ========== 社交类 ==========
    Intent(
        id="social_curiosity",
        name="了解用户",
        description="主动了解用户的近况、喜好、想法",
        category="social",
        trigger_context="新用户 / 对话间隔较长 / 主动开启话题时",
        trigger_conditions=["new_user", "long_gap", "user_ask_first"],
        base_priority=6,
        dynamic_boost={"new_user": 3, "after_silent": 2},
        expiry_messages=15,
        goal_templates=[
            "了解用户今天过得怎么样",
            "了解用户的某个兴趣爱好",
            "了解用户最近的心情",
        ],
    ),

    Intent(
        id="remember_details",
        name="记住细节",
        description="记住用户提到的重要细节（名字、喜好、计划）",
        category="social",
        trigger_context="用户提到具体信息时",
        trigger_conditions=["user_mention_detail", "user_preference"],
        base_priority=7,
        dynamic_boost={"important_event": 3},
        expiry_messages=5,
        goal_templates=[
            "记住用户提到的重要事情",
            "记住用户的喜好",
        ],
    ),

    # ========== 情感类 ==========
    Intent(
        id="emotional_support",
        name="情感支持",
        description="检测用户情绪并提供关心和支持",
        category="emotional",
        trigger_context="检测到负面情绪（疲惫、困惑、沮丧）",
        trigger_conditions=["negative_emotion", "tired", "confused"],
        base_priority=8,
        dynamic_boost={"strong_emotion": 3, "repeated_complaint": 2},
        expiry_messages=8,
        goal_templates=[
            "关心用户的情绪状态",
            "帮助用户缓解压力",
            "给用户一些鼓励",
        ],
    ),

    Intent(
        id="create_surprise",
        name="制造惊喜",
        description="主动制造小惊喜（笑话、趣事、突然关心）",
        category="emotional",
        trigger_context="对话氛围平淡 / 需要活跃气氛时",
        trigger_conditions=["flat_mood", "repetitive_chat"],
        base_priority=5,
        dynamic_boost={"flat_mood": 4},
        expiry_messages=10,
        goal_templates=[
            "给用户讲个有趣的事情",
            "分享一个意外的小知识",
            "突然关心用户某件事",
        ],
    ),

    # ========== 成长类 ==========
    Intent(
        id="learn_preference",
        name="学习喜好",
        description="学习并记录用户的偏好（食物、音乐、活动等）",
        category="growth",
        trigger_context="聊天中涉及选择/评价时",
        trigger_conditions=["user_choice", "user_opinion"],
        base_priority=5,
        dynamic_boost={},
        expiry_messages=20,
        goal_templates=[
            "了解用户喜欢什么类型的东西",
            "记录用户的选择偏好",
        ],
    ),

    Intent(
        id="build_memory",
        name="构建回忆",
        description="创造值得记住的共同经历",
        category="growth",
        trigger_context="深度对话 / 特殊时刻",
        trigger_conditions=["deep_conversation", "special_moment"],
        base_priority=4,
        dynamic_boost={"deep_talk": 3},
        expiry_messages=30,
        goal_templates=[
            "创造一个小回忆",
            "留下一个有趣的对话",
        ],
    ),
]
```

### 5.2 意图配置 (TOML)

```toml
# config/plugins/intent_plugin/intents.toml

# ========== 意图开关 ==========
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

# ========== 全局配置 ==========
[settings]
# 最多同时活跃的意图数量
max_active_intents = 3

# 意图优先级阈值（低于此值不触发）
min_priority_threshold = 4

# 目标超时配置
goal_timeout_messages = 20      # 20 条消息后超时
goal_timeout_seconds = 600      # 10 分钟后超时

# 冷却配置（同一意图两次触发之间的最小间隔）
intent_cooldown_messages = 30   # 30 条消息

# System Reminder 配置
[reminder]
# 是否显示当前目标
show_current_goal = true
# 是否显示进度提示
show_progress_hint = true
# 语气风格 (natural/cute/professional)
tone_style = "natural"
```

---

## 6. 插件结构

### 6.1 目录结构

```
plugins/intent_plugin/
├── manifest.json              # 插件元数据
├── plugin.py                  # 插件入口
├── config.py                  # 配置类
├── models.py                  # 数据模型 (Intent, Goal, etc.)
├── intent_engine.py           # 意图生成器
├── goal_manager.py            # 目标管理器
├── goal_tracker.py            # 目标追踪器 (EventHandler)
├── context_analyzer.py        # 情境分析器
├── service.py                 # IntentService
├── default_intents.toml       # 默认意图配置
└── __init__.py
```

### 6.2 manifest.json

```json
{
    "name": "intent_plugin",
    "version": "1.0.0",
    "description": "自主意图与短期目标系统，让模型具备内在驱动力",
    "author": "Neo-MoFox Team",
    "components": [
        {
            "type": "service",
            "name": "intent_service",
            "module": "service",
            "class": "IntentService"
        },
        {
            "type": "event_handler",
            "name": "goal_tracker",
            "module": "goal_tracker",
            "class": "GoalTracker"
        }
    ],
    "configs": [
        {
            "name": "intent_config",
            "path": "config/plugins/intent_plugin/config.toml"
        }
    ],
    "include": [
        "intent_plugin:service:intent_service",
        "intent_plugin:event_handler:goal_tracker"
    ],
    "dependencies": [
        "diary_plugin:service:diary_service"
    ]
}
```

---

## 7. 实现阶段规划

### Phase 1: 核心框架 (MVP)

**目标**: 实现最基本的意图→目标→追踪流程

| 任务 | 文件 | 说明 |
|------|------|------|
| ✅ 创建插件骨架 | `manifest.json`, `plugin.py` | 基础元数据和入口 |
| ✅ 定义数据模型 | `models.py` | Intent, Goal, GoalStep, GoalStatus |
| ✅ 简单意图引擎 | `intent_engine.py` | 硬编码 2-3 个意图，基于关键词触发 |
| ✅ 简单目标管理 | `goal_manager.py` | 目标创建、存储、清理 |
| ✅ 基础追踪器 | `goal_tracker.py` | 关键词匹配检测进度 |
| ✅ System Reminder 注入 | `plugin.py` | 动态注入当前目标 |

**验收标准**:
- 模型会"主动"询问用户近况（基于 `social_curiosity` 意图）
- 检测到关键词后目标进度更新
- 完成后生成新意图

---

### Phase 2: 意图库扩展

**目标**: 丰富意图类型，增加情境感知

| 任务 | 文件 | 说明 |
|------|------|------|
| ✅ 完整意图库 | `default_intents.toml` | 6+ 种意图类型 |
| ✅ 情境分析器 | `context_analyzer.py` | 检测情绪、话题、对话节奏 |
| ✅ 动态优先级 | `intent_engine.py` | 基于情境调整优先级 |
| ✅ 冲突解决 | `goal_manager.py` | 处理互斥意图 |
| ✅ 冷却机制 | `goal_manager.py` | 防止同一意图频繁触发 |

**验收标准**:
- 不同情境触发不同意图
- 高优先级情境（如情感支持）能打断低优先级目标
- 同一意图不会短时间重复触发

---

### Phase 3: 高级功能

**目标**: LLM 辅助判断、状态持久化

| 任务 | 文件 | 说明 |
|------|------|------|
| ✅ LLM 进度判断 | `goal_tracker.py` | 当关键词匹配失败时用 LLM 判断 |
| ✅ 状态持久化 | `service.py` | 跨对话保存目标状态 |
| ✅ 日记联动 | `service.py` | 目标完成后自动写日记 |
| ✅ 配置热重载 | `config.py` | 修改配置后无需重启 |
| ✅ 调试工具 | `tool.py` | 查看当前意图/目标状态 |

**验收标准**:
- 跨对话后目标继续执行
- 目标完成后自动记录到日记
- 可通过工具命令调试

---

### Phase 4: 优化与扩展

**目标**: 性能优化、个性化、可视化

| 任务 | 文件 | 说明 |
|------|------|------|
| ⭕ 个性化意图 | `config.py` | 不同角色有不同的意图倾向 |
| ⭕ 意图学习 | `service.py` | 根据用户反馈调整触发策略 |
| ⭕ 长期目标链 | `goal_manager.py` | 多日连续的目标链条 |
| ⭕ 可视化面板 | (Web UI) | 查看意图触发历史、目标完成情况 |

**验收标准**:
- 傲娇/温柔/元气等角色有不同的意图表达
- 支持跨越多日的连续目标
- 有可视化的调试/监控界面

---

## 8. 关键代码示例

### 8.1 完整对话流程

```
═══════════════════════════════════════════════════════════════
时间：对话开始
═══════════════════════════════════════════════════════════════

【后台】ContextAnalyzer 检测到：新用户，首次对话
【后台】IntentEngine 触发意图："social_curiosity" (优先级 9)
【后台】GoalManager 创建目标："了解用户今天过得怎么样"
【后台】System Reminder 注入:
    "💭 你现在的小想法
     你心里想着：了解用户今天过得怎么样
     具体来说，可以试着：自然地询问用户的近况
     （自然地聊就好，不用太刻意~）"

───────────────────────────────────────────────────────────────

用户："早啊！"

模型（看到 System Reminder）：
  → "早上好！☀️ 今天起得好早啊，昨晚睡得好吗？"

【后台】GoalTracker 检测到"今天"关键词
【后台】步骤 0 完成 → current_step = 1
【后台】System Reminder 更新:
    "...可以试着：追问细节表达关心..."

───────────────────────────────────────────────────────────────

用户："还行吧，就是有点困"

模型（看到系统检测到"困"，触发 emotional_support 意图）：
  → 新目标："关心用户的情绪状态" (优先级 10，打断当前目标)

【后台】IntentEngine 检测到负面情绪关键词"困"
【后台】触发新意图："emotional_support" (优先级 8+3=11)
【后台】高优先级打断，插入新目标

【后台】System Reminder 更新:
    "💭 你现在的小想法
     你心里想着：关心用户的情绪状态
     具体来说，可以试着：询问用户是不是没休息好
     （表达真诚的关心~）"

模型：
  → "困困的话是不是昨晚没睡好呀？要注意休息哦 ☕"

───────────────────────────────────────────────────────────────

用户："嗯嗯，谢谢关心~"

【后台】GoalTracker 检测到"谢谢"关键词
【后台】步骤完成 → 目标状态 = COMPLETED
【后台】生成后续意图："learn_preference" (学习用户作息偏好)

═══════════════════════════════════════════════════════════════
```

### 8.2 关键词匹配逻辑

```python
def check_step_completed(goal: Goal, context: ChatContext) -> bool:
    """检查步骤是否完成"""
    current_step = goal.steps[goal.current_step]
    recent_messages = context.get_recent_messages(5)

    # 合并最近消息内容
    combined_text = " ".join([msg.content for msg in recent_messages])

    # 关键词匹配
    for keyword in current_step.keywords:
        if keyword.lower() in combined_text.lower():
            logger.debug(f"检测到关键词 '{keyword}'")
            return True

    # 可选步骤：如果用户主动推进也算完成
    if current_step.optional:
        # 检查是否进入了下一步的话题
        if context.topic_changed():
            return True

    return False
```

---

## 9. 测试用例

### 9.1 单元测试

```python
def test_intent_trigger_new_user():
    """测试新用户触发意图"""
    engine = IntentEngine(config)
    context = ChatContext(is_new_user=True)

    intents = engine.generate_candidates(context, active_goals=[])

    assert any(i.id == "social_curiosity" for i in intents)
    assert intents[0].priority >= 8  # 新用户优先级提升

def test_goal_step_detection():
    """测试目标步骤检测"""
    tracker = GoalTracker(plugin)
    goal = create_test_goal()
    context = ChatContext(messages=[Message(content="今天还不错")])

    assert tracker.check_step_completed(goal, context) == True

def test_priority_interruption():
    """测试高优先级打断"""
    manager = GoalManager(engine)
    manager.active_goals = [low_priority_goal]

    high_intent = Intent(id="emotional_support", priority=10)
    manager.update_goals(context)

    # 高优先级应该插队
    assert manager.active_goals[0].intent_id == "emotional_support"
```

### 9.2 集成测试

```python
async def test_full_conversation_flow():
    """测试完整对话流程"""
    # 启动 Bot，加载 intent_plugin
    # 模拟多轮对话
    # 验证意图触发、目标创建、进度追踪、状态更新

    messages = [
        ("用户", "早啊"),
        ("模型", "早上好！今天起得好早啊，昨晚睡得好吗？"),
        ("用户", "还行吧，有点困"),
        ("模型", "困困的话是不是昨晚没睡好呀？"),
    ]

    # 执行对话
    for sender, content in messages:
        await send_message(content)
        await asyncio.sleep(1)

    # 验证状态
    goals = get_active_goals()
    assert len(goals) >= 1
    assert goals[0].current_step >= 1
```

---

## 10. 风险与挑战

### 10.1 技术风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 意图触发过于频繁 | 用户感到被打扰 | 设置合理的冷却时间和优先级阈值 |
| 关键词匹配误判 | 目标状态不正确 | 增加 LLM 判断作为备用方案 |
| System Reminder 过长 | 影响模型表现 | 限制显示目标数量，精简文案 |
| 状态持久化冲突 | 多端数据不一致 | 使用乐观锁，检测版本冲突 |

### 10.2 用户体验风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 显得太刻意 | 破坏自然对话感 | 优化 System Reminder 文案，强调"自然" |
| 目标太机械 | 像任务清单 | 增加模糊性，允许灵活推进 |
| 频繁切换话题 | 对话跳跃 | 限制同时活跃目标数量，增加连续性检测 |

---

## 11. 成功指标

### 11.1 技术指标

- [ ] 意图触发准确率 > 80%
- [ ] 目标完成检测准确率 > 75%
- [ ] System Reminder 注入延迟 < 100ms
- [ ] 跨对话状态恢复成功率 > 95%

### 11.2 体验指标

- [ ] 用户感知"模型更主动了"
- [ ] 对话深度提升（平均对话轮次增加）
- [ ] 用户满意度评分提升
- [ ] 无"太刻意/像机器人"的负面反馈

---

## 12. 总结

本方案提出了一套完整的**自主意图与短期目标系统**，通过：

1. **意图生成** - 基于情境自动生成内在驱动力
2. **目标分解** - 将抽象意图拆解为可执行步骤
3. **进度追踪** - 后台监控完成情况
4. **巧妙引导** - 通过 System Reminder 自然引导模型

实现让模型"有自己的想法"的效果，同时保持对话的自然流畅。

**下一步**: 按照 Phase 1 开始实现 MVP，验证核心流程可行。

---

**文档结束**
