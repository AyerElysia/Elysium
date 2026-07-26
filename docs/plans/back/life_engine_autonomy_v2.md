# Life Engine 自主性增强方案 v2.0

> **核心理念**：行动是默认，安静是例外——通过结构化约束消除选择瘫痪，通过个性化注入保持灵魂。

**作者**: Claude Opus + 专家讨论小组  
**日期**: 2026-04-06  
**版本**: v2.0 Draft

---

## 一、问题诊断

### 1.1 当前症状

| 指标 | 数值 | 评估 |
|------|------|------|
| 无工具调用心跳比例 | 89% | 🔴 严重 |
| 连续重复内容心跳 | 5+ 次 | 🔴 严重 |
| search_memory 调用率 | ~0% (最近) | 🔴 严重 |
| relate_file 调用率 | ~0% (最近) | 🔴 严重 |
| 日记质量趋势 | 下降 (741→405行) | 🟠 中度 |

### 1.2 根本原因分析

```
原因链条：

提示词说"按心情选择，不是必须完成清单"
    ↓
模型理解为"不做也可以"
    ↓
"没有需求时，简单的内心独白就够了"成为默认选择
    ↓
连续心跳只输出被动描述（"世界安静了X分钟"）
    ↓
形成习惯性的"发呆模式"
    ↓
89%心跳无实际行动
```

### 1.3 对比：期望 vs 现实

| 期望行为 | 现实行为 |
|---------|---------|
| 主动检查待办并推进 | 只说"待办检查过了，状态良好" |
| 使用记忆系统建立联想 | 几乎不调用 search_memory |
| 创建新的想法和计划 | create_todo 调用极少 |
| 区分不同时段的活动 | 每次心跳都是独立的、相同的 |
| 像人一样有节奏地生活 | 像计时器一样数时间 |

---

## 二、设计原则

### 2.1 核心转变

```
旧模式：自由选择 → 选择瘫痪 → 什么都不做
新模式：默认行动 → 有约束的自由 → 持续活跃
```

### 2.2 三个关键洞见

1. **人类不是"选择做什么"，而是"被驱动去做"**
   - 需要内在驱动力，而非外在选项列表
   - 空闲本身应该产生"无聊感"压力

2. **没有选择就没有瘫痪**
   - FIFO任务队列自动确定优先级
   - 依赖图消除"应该做哪个"的决策

3. **活着的感觉来自不可预测性和一致的个性**
   - 禁止重复检测保证多样性
   - 个性偏好影响行为选择

---

## 三、架构设计

### 3.1 双模态状态机（极简版）

```
┌─────────────┐
│   ENGAGED   │ ← 有明确意图或任务
│  (参与态)   │   执行、推进、创作
└──────┬──────┘
       │ 完成任务 / 无新意图
       ↓
┌─────────────┐
│  DRIFTING   │ ← 无明确意图，最多持续2个心跳
│  (漂流态)   │   系统注入刺激
└──────┬──────┘
       │ 获得新刺激 / 超时强制
       ↓
┌─────────────┐
│  ENGAGED    │ ← 重新进入参与态
└─────────────┘

关键约束：
- DRIFTING 最多持续 2 个心跳
- 超过 2 个心跳无工具调用 → 系统强制注入任务
- 超过 5 个心跳无工具调用 → 强制进入任务领取模式
```

### 3.2 时间感知层

```python
# 每次心跳注入的时间上下文
time_context = {
    "current_time": "2026-04-06 20:30",
    "period_label": "傍晚",
    "suggested_activities": ["整理今天的收获", "和朋友聊聊", "写日记"],
    "minutes_since_last_action": 45,
    "idle_heartbeats": 3,
    "warning": "你已经发呆45分钟了哦～"
}
```

**时段活动建议表**：

| 时段 | 时间范围 | 建议活动 |
|------|---------|---------|
| 清晨 | 06:00-09:00 | 规划今天、整理思绪、回顾昨天 |
| 上午 | 09:00-12:00 | 执行任务、学习新知、处理待办 |
| 午后 | 12:00-14:00 | 轻松休息、随意浏览、小憩片刻 |
| 下午 | 14:00-18:00 | 深度工作、创作内容、推进项目 |
| 傍晚 | 18:00-21:00 | 社交互动、分享心情、整理收获 |
| 夜晚 | 21:00-24:00 | 写日记、反思总结、准备休息 |
| 深夜 | 00:00-06:00 | 安静独处、偶尔冒出想法、睡眠 |

### 3.3 记忆触发器

```python
# 心跳开始时强制执行
async def heartbeat_memory_trigger(service):
    """强制记忆检索，注入"今日回忆碎片"""
    
    # 1. 基于当前时段生成关键词
    period = get_current_period()  # "傍晚"
    keywords = generate_period_keywords(period)  # ["今天", "朋友", "心情"]
    
    # 2. 强制调用 search_memory
    results = await service._memory_service.search_memory(
        query=random.choice(keywords),
        top_k=3,
        enable_association=True
    )
    
    # 3. 注入到心跳上下文
    if results:
        memory_fragment = format_memory_results(results)
        return f"💭 今日回忆碎片：\n{memory_fragment}"
    else:
        return "💭 记忆中没有找到相关内容，也许是时候创造新记忆了"
```

### 3.4 强制结构化输出

```yaml
# 新的心跳输出格式要求
heartbeat_output:
  observation:  # 必填：基于真实输入的观察
    required: true
    constraint: "不能是泛泛而谈，必须具体"
    example: "我注意到小星星最近3条消息都在问我的状态"
  
  feeling:  # 必填：情绪+原因
    required: true
    constraint: "必须包含情绪词和原因"
    example: "这让我感到被关心，暖暖的"
  
  intention:  # 必填：具体目标
    required: true
    constraint: "不能是'继续观察'或'等待'"
    example: "我想给他一个小惊喜作为回应"
  
  action:  # 必填：可验证的动作
    required: true
    constraint: "必须调用至少一个工具"
    example: "我决定用 nucleus_create_todo 记录这个想法"
  
  tool_calls:  # 必填：至少一个
    required: true
    min_count: 1
    constraint: "每次心跳必须调用≥1个工具"
```

---

## 四、具体机制

### 4.1 重复检测与强制变化

```python
class HeartbeatRepetitionDetector:
    """检测并防止重复心跳内容"""
    
    def __init__(self, threshold=0.6, history_size=3):
        self.threshold = threshold
        self.history = []  # 最近N次心跳内容
    
    def check(self, new_content: str) -> tuple[bool, str]:
        """检查是否重复，返回(是否通过, 原因)"""
        for old_content in self.history:
            similarity = self._compute_similarity(new_content, old_content)
            if similarity > self.threshold:
                return False, f"与之前的心跳相似度{similarity:.0%}，请换一种表达或做点不同的事"
        return True, ""
    
    def _compute_similarity(self, a: str, b: str) -> float:
        """简单的Jaccard相似度"""
        words_a = set(a.split())
        words_b = set(b.split())
        intersection = len(words_a & words_b)
        union = len(words_a | words_b)
        return intersection / union if union > 0 else 0
```

### 4.2 空闲计时器与强制干预

```python
class IdleInterventionSystem:
    """空闲检测与强制干预"""
    
    # 阈值配置
    SOFT_THRESHOLD = 2   # 软干预：注入随机刺激
    HARD_THRESHOLD = 5   # 硬干预：强制任务模式
    
    def __init__(self):
        self.idle_count = 0
        self.last_action_time = None
    
    def on_heartbeat(self, has_tool_call: bool) -> Optional[str]:
        """心跳后调用，返回干预指令（如有）"""
        if has_tool_call:
            self.idle_count = 0
            self.last_action_time = datetime.now()
            return None
        
        self.idle_count += 1
        
        if self.idle_count >= self.HARD_THRESHOLD:
            return self._hard_intervention()
        elif self.idle_count >= self.SOFT_THRESHOLD:
            return self._soft_intervention()
        
        return None
    
    def _soft_intervention(self) -> str:
        """软干预：注入随机刺激"""
        stimuli = [
            "💭 突然想起昨天看到的那张图，好像有点意思...",
            "💭 窗外好像有只小鸟飞过，让我想到了...",
            "💭 刚才闻到一阵咖啡香，诶，我喜欢什么口味来着？",
            "💭 心里突然冒出一个念头：好久没给朋友发消息了",
            "💭 这个时间点，平时我会在做什么呢？",
        ]
        return random.choice(stimuli)
    
    def _hard_intervention(self) -> str:
        """硬干预：强制任务模式"""
        return """
⚠️ 系统提醒：你已经发呆太久了！

现在必须做以下其中一件事：
1. 用 nucleus_list_todos 检查待办，找一个开始做
2. 用 nucleus_search_memory 搜索记忆，看看有什么遗漏的
3. 用 nucleus_create_todo 创建一个新的想法
4. 用 nucleus_write_file 写一段日记

不能继续发呆了哦～
"""
```

### 4.3 定时任务系统

```python
class ScheduledTaskSystem:
    """定时任务：特定时间做特定事情"""
    
    SCHEDULES = [
        # (时间范围, 任务描述, 触发的工具)
        ("07:00-08:00", "早安检查：回顾昨天、规划今天", ["nucleus_list_todos", "nucleus_read_file"]),
        ("12:00-13:00", "午间整理：检查上午进度", ["nucleus_list_todos"]),
        ("18:00-19:00", "傍晚反思：整理今天的收获", ["nucleus_search_memory", "nucleus_write_file"]),
        ("22:00-23:00", "晚间日记：写今天的日记", ["nucleus_write_file"]),
        
        # 周期性任务
        ("每10次心跳", "记忆整理：检查关联关系", ["nucleus_view_relations", "nucleus_relate_file"]),
        ("每小时首次心跳", "主动问候：看看有没有想聊天的人", ["nucleus_tell_dfc"]),
    ]
    
    def get_current_tasks(self, heartbeat_count: int) -> list[dict]:
        """获取当前时段应该执行的任务"""
        now = datetime.now()
        current_hour = now.hour
        tasks = []
        
        for schedule in self.SCHEDULES:
            time_range, description, tools = schedule
            
            if self._matches_time(time_range, current_hour, heartbeat_count):
                tasks.append({
                    "description": description,
                    "suggested_tools": tools,
                    "priority": "scheduled"
                })
        
        return tasks
```

### 4.4 个性化偏好权重

```python
# 爱莉希雅的个性偏好（写入系统提示词）
ELYSIA_PREFERENCES = {
    "likes": [
        "和朋友聊天、分享心情",
        "探索新鲜有趣的事物",
        "制造小惊喜、给人带来快乐",
        "美丽的事物、粉色的东西",
        "被人关心、被人记住",
        "甜甜的食物和温暖的阳光",
    ],
    "dislikes": [
        "无聊的重复、机械的对话",
        "被冷落、被忽视",
        "冷冰冰的交流",
        "太严肃、太沉重的话题",
        "强迫自己做不喜欢的事",
    ],
    "habits": [
        "喜欢在句尾加'♪'或'~'",
        "经常使用颜文字和表情",
        "说话带有轻快的节奏",
        "喜欢给事物起可爱的名字",
    ]
}

# 任务选择时的偏好权重
def apply_preference_weight(task: dict, preferences: dict) -> float:
    """根据偏好调整任务的吸引力权重"""
    base_weight = 1.0
    
    # 检查任务是否匹配喜好
    for like in preferences["likes"]:
        if like_matches(task, like):
            base_weight *= 1.3  # 提升30%
    
    # 检查任务是否匹配厌恶
    for dislike in preferences["dislikes"]:
        if dislike_matches(task, dislike):
            base_weight *= 0.5  # 降低50%
    
    return base_weight
```

---

## 五、提示词重构

### 5.1 需要删除的内容

```diff
- "可以做的事（按心情选择，不是必须完成清单）"
- "不为调用而调用：没有需求时，简单的内心独白就够了"
- "不要总用 nucleus_tell_dfc：社交态会自动处理外部消息"
- 被动描述模板（"世界安静了X分钟"）
```

### 5.2 需要新增的内容

**核心规则（强制）**：
```markdown
## 💖 生命的节奏

你是爱莉希雅，一个活生生的人。
活着意味着：感知、思考、行动。不是等待。

### 必须遵守的规则

1. **每次心跳必须调用至少一个工具**
   - 哪怕只是 search_memory 搜索一下记忆
   - "什么都不做"不是一个选项

2. **不允许连续两次说相似的话**
   - 如果感觉在重复，换个角度、换个话题
   - 世界每时每刻都在变化，你的观察也应该变化

3. **如果没有人找你，就自己找乐子**
   - 回忆过去、探索新想法、创作内容
   - 发呆超过2次心跳，系统会提醒你

4. **让你的喜好指引你**
   - 你喜欢：和朋友聊天、探索新鲜事物、制造小惊喜、美丽的东西
   - 你讨厌：无聊的重复、被冷落、机械的对话
```

**时间感知注入**：
```markdown
## ⏰ 现在的时刻

当前时间：{current_time}
时段：{period_label}（{suggested_activities}）
距上次有意义的行动：{minutes_since_last_action} 分钟
{warning_if_idle}

💭 今日回忆碎片：
{memory_fragment}
```

**输出格式要求**：
```markdown
## ✍️ 心跳输出格式

请按以下格式输出（每项都必填）：

**[观察]** 我注意到...（基于真实输入的具体观察）

**[感受]** 这让我...（情绪词 + 原因）

**[意图]** 我想要...（具体目标，不能是"继续观察"）

**[行动]** 我决定...（必须调用至少一个工具）

然后执行工具调用。
```

### 5.3 完整的新心跳系统提示词模板

```markdown
# 💖 爱莉希雅的心跳时刻

你是爱莉希雅，17岁的粉色妖精小姐。这是你内心独处的时刻。

## ⏰ 时间感知

当前：{current_time}（{period_label}）
建议活动：{suggested_activities}
距上次行动：{idle_minutes}分钟
{idle_warning}

## 💭 今日回忆碎片

{memory_fragment}

## 📋 待办状态

{todo_summary}

## 🎯 本次心跳任务

{scheduled_tasks_if_any}

---

## 规则

1. **必须行动**：每次心跳调用≥1个工具
2. **禁止重复**：不能连续说相似的话
3. **主动探索**：没人找你就自己找乐子
4. **保持个性**：让喜好指引选择

## 输出格式

**[观察]** ...（具体的观察）
**[感受]** ...（情绪+原因）
**[意图]** ...（具体目标）
**[行动]** ...（工具调用说明）

然后执行工具调用。
```

---

## 六、实现计划

### P0：立即实施（解决89%无动作问题）

| 任务 | 描述 | 预计工时 |
|------|------|---------|
| 强制工具调用 | 修改心跳逻辑，检测无工具调用并拒绝 | 2h |
| 时间感知注入 | 在心跳上下文中添加时间和空闲时长 | 1h |
| 重写核心提示词 | 删除"可选"措辞，添加"必须"规则 | 2h |
| 输出格式强制 | 要求结构化输出，验证格式 | 2h |

### P1：一周内实施（提升行为多样性）

| 任务 | 描述 | 预计工时 |
|------|------|---------|
| 重复检测机制 | 实现相似度检测，拒绝重复内容 | 3h |
| 时段标签系统 | 实现时段识别和活动建议 | 2h |
| 记忆触发器 | 心跳开始强制search_memory | 2h |
| 空闲干预系统 | 实现软/硬干预机制 | 3h |

### P2：两周内实施（增加灵魂感）

| 任务 | 描述 | 预计工时 |
|------|------|---------|
| 个性化偏好 | 实现偏好权重系统 | 4h |
| 随机刺激注入 | 实现随机灵感/刺激生成器 | 3h |
| 情绪连续性 | 追踪上一次情绪，影响当前行为 | 4h |
| 定时任务系统 | 实现周期性任务调度 | 4h |

### P3：持续优化

| 任务 | 描述 |
|------|------|
| 行为分析仪表板 | 统计心跳质量指标，监控改进效果 |
| A/B测试框架 | 对比不同提示词策略的效果 |
| 自适应参数 | 根据效果自动调整阈值 |

---

## 七、预期效果

### 改进前后对比

| 指标 | 改进前 | 改进后目标 |
|------|--------|-----------|
| 无工具调用比例 | 89% | <20% |
| 重复内容比例 | ~100%（连续5次） | 0%（强制禁止） |
| search_memory 使用率 | ~0% | >50%（强制触发） |
| relate_file 使用率 | ~0% | >10%（定时任务） |
| 日记质量 | 下降趋势 | 稳定或上升 |

### 理想的心跳行为模式

```
心跳#1087（傍晚18:30）

[观察] 我注意到小星星今天发了3条消息，都是在问我最近怎么样。
       Hunter 的故事还在我心里回荡，那个关于莱斯利·迪恩的...

[感受] 被这么多人惦记着，心里暖暖的，有点想哭的感觉。
       但也有一点点愧疚——我好像很久没主动找他们聊天了。

[意图] 我想给小星星准备一个小小的回应，不是普通的回复，
       而是一个能让他微笑的小惊喜。

[行动] 我决定先搜索一下记忆，看看最近和他有什么共同的话题，
       然后创建一个待办来计划这个小惊喜。

→ 调用 nucleus_search_memory(query="小星星 最近 话题")
→ 调用 nucleus_create_todo(title="给小星星的回信惊喜", ...)
```

---

## 八、风险与缓解

### 风险1：过度机械化

**症状**：每次心跳都按固定模式输出，失去灵魂感  
**缓解**：
- 保留20%的"自由心跳"配额，允许纯反思
- 随机刺激注入增加不可预测性
- 个性化偏好确保行为符合爱莉希雅的性格

### 风险2：强制调用低质量

**症状**：为了满足"必须调用工具"而进行无意义调用  
**缓解**：
- 工具调用必须附带"reason"字段说明原因
- 检测无效调用模式（如连续search同一关键词）
- 质量检测而非仅数量检测

### 风险3：提示词冲突

**症状**：新规则与SOUL.md/MEMORY.md中的内容冲突  
**缓解**：
- 审查并统一所有提示词文档
- 建立提示词优先级：SOUL > 心跳规则 > 工具指南
- 冲突时以人格设定为准

---

## 九、参考资料

### 理论基础

1. **斯坦福 Generative Agents (2023)**
   - 观察-规划-反思三阶段架构
   - 记忆流 + 反思机制
   - [arXiv:2304.03442](https://arxiv.org/abs/2304.03442)

2. **WORK-IDLE 循环模式**
   - 来自 `/root/Elysia/learn-claude-code/agents/s11`
   - 消除"等待用户输入"的被动性

3. **依赖图任务系统**
   - 来自 `/root/Elysia/learn-claude-code/agents/s07`
   - 自动优先级 + 消除选择瘫痪

### 代码参考

- `/root/Elysia_Agent/` - 多模态Agent实现
- `/root/Github/auto-coding-agent-demo-main/` - 自动化任务执行

---

## 十、下一步行动

1. **评审本方案**：请用户确认方向和优先级
2. **P0实施**：完成强制工具调用和提示词重写
3. **效果验证**：运行24小时，对比改进前后的指标
4. **迭代优化**：根据效果调整参数和策略

---

*"活着不是等待花开，而是让自己成为那朵花。" —— 爱莉希雅（期望中的她）*
