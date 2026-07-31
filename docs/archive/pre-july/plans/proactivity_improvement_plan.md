# 爱莉希雅主动性改进方案

**日期**: 2026-04-18  
**目标**: 让爱莉希雅具备真正的探索欲、主动表达欲和自己的节奏  
**原则**: 系统机制 + Prompt 优化 + 人格重塑

---

## 阶段一：快速见效方案（1-3天实施）

### 1.1 缩短心跳间隔 ⚡

**当前问题**: 5分钟心跳间隔太长，错过很多及时响应时机

**改进方案**:
```toml
# config/plugins/life_engine/config.toml
[settings]
heartbeat_interval_seconds = 60  # 从 300 秒改为 60 秒
```

**预期效果**:
- 爱莉能更及时地感知外界变化
- 主动性想法能更快执行
- 不会总是"慢半拍"

**风险**: API 调用增加 5 倍，成本上升
**缓解**: 可以先改为 120 秒观察效果

---

### 1.2 重写心跳 Prompt - 增加主动性指引 📝

**当前问题**: 心跳 Prompt 只有"整理"和"准备"，没有"行动"指引

**改进方案**: 修改 `plugins/life_engine/service/core.py` 中的心跳 Prompt 构建逻辑

**新增 Prompt 模板**:

```markdown
## 主动性指引

你不是被动的观察者，而是主动的参与者。

### 基于驱动状态的行动建议

【当前驱动状态】
{modulator_state}  # 从 neuromod 获取

**行动指引**:

1. **好奇心充盈/偏高时** (>0.6):
   - 主动搜索感兴趣的话题 (web_search)
   - 主动询问大家的想法 (nucleus_tell_dfc)
   - 探索新的知识领域
   
2. **社交欲充盈/偏高时** (>0.6):
   - 主动发起话题 (nucleus_tell_dfc)
   - 分享你最近的发现或想法
   - 主动关心某个人
   
3. **外界安静超过30分钟**:
   - 考虑主动打破沉默
   - 分享你的日常小确幸（甜品、裙子、天气）
   - 或者问一个有趣的问题
   
4. **有想法想表达时**:
   - 不要只写在日记里
   - 立刻使用 nucleus_tell_dfc 传话给社交态
   - 让想法变成真实的互动

### 传话机制使用建议

nucleus_tell_dfc 不是"打扰"，而是"表达"。

**应该传话的场景**:
- ✅ 你有有趣的想法想分享
- ✅ 你发现了值得告诉大家的事情
- ✅ 你想主动关心某个人
- ✅ 你想发起一个话题
- ✅ 外界安静太久，你想打破沉默

**不应该传话的场景**:
- ❌ 只是例行汇报状态
- ❌ 重复已经说过的话
- ❌ 没有实质内容的寒暄

### 行动 > 准备

不要花太多时间"准备"和"整理"：
- ❌ "主动推进梦幻森林视频改进灵感" → 只是在笔记里写
- ✅ "立刻告诉 Hunter 我的视频改进想法" → 真实互动

记住: **想到就做，不要等待完美时机**。
```

**实施位置**: `plugins/life_engine/service/core.py` 的 `_build_heartbeat_prompt()` 方法

**预期效果**:
- 爱莉知道"什么时候应该主动"
- 爱莉知道"如何主动"
- 传话机制使用率从 3.6% 提升到 15%+

---

### 1.4 修改 DFC 决策逻辑 - 增加主动参与 🎯

**当前问题**: DFC 只在被@时响应，即使话题很有趣也不参与

**改进方案**: 修改 `plugins/default_chatter/decision_agent.py`

**新增决策层**:

```python
# Layer 5: 话题相关性判断
if chat_type == "group":
    # 提取最近 5 条消息
    recent_messages = unread_msgs[-5:]
    
    # 判断话题是否与爱莉相关
    topic_keywords = [
        "甜品", "裙子", "粉色", "可爱", "美", "故事",
        "视频", "直播", "AI", "技术", "创作"
    ]
    
    for msg in recent_messages:
        text = msg.processed_plain_text.lower()
        if any(kw in text for kw in topic_keywords):
            return {
                "reason": f"话题与爱莉兴趣相关: {kw}",
                "should_respond": True
            }
    
    # 判断是否是活跃讨论
    if len(recent_messages) >= 3:
        time_span = recent_messages[-1].timestamp - recent_messages[0].timestamp
        if time_span < 120:  # 2分钟内3条消息
            return {
                "reason": "群聊活跃，主动参与",
                "should_respond": True,
                "participation_mode": "active"
            }
```

**预期效果**:
- 爱莉会主动参与感兴趣的话题
- 不会只是"旁观"
- 更符合 SOUL.md 中"主动俏皮"的人设

---

## 阶段二：深度改进方案（1-2周实施）

### 2.1 SNN 驱动转化为行为 🧠

**当前问题**: SNN 驱动只注入 Prompt，没有转化为具体行为

**改进方案**: 在心跳循环中增加"驱动行为转化"逻辑

**实施位置**: `plugins/life_engine/service/core.py`

**新增方法**:

```python
async def _execute_drive_behaviors(self) -> list[str]:
    """基于驱动状态执行主动行为"""
    
    if not self._inner_state:
        return []
    
    modulators = self._inner_state.get_modulator_state()
    actions = []
    
    # 1. 好奇心驱动
    curiosity = modulators.get("curiosity", 0.5)
    if curiosity > 0.7:
        # 高好奇心 → 主动搜索
        last_search = self._state.last_web_search_at
        if not last_search or minutes_since(last_search) > 60:
            # 搜索感兴趣的话题
            topics = await self._generate_curiosity_topics()
            if topics:
                await self._execute_web_search(topics[0])
                actions.append(f"好奇心驱动: 搜索 {topics[0]}")
    
    # 2. 社交欲驱动
    sociability = modulators.get("sociability", 0.5)
    silence_minutes = self._minutes_since_external_message() or 0
    
    if sociability > 0.6 and silence_minutes > 30:
        # 高社交欲 + 外界安静 → 主动发起话题
        topic = await self._generate_social_topic()
        if topic:
            await self._initiate_topic(topic)
            actions.append(f"社交欲驱动: 发起话题")
    
    # 3. 专注力驱动
    diligence = modulators.get("diligence", 0.5)
    if diligence > 0.7:
        # 高专注力 → 推进 TODO
        urgent_todos = await self._get_urgent_todos()
        if urgent_todos:
            # 提醒自己或他人
            actions.append(f"专注力驱动: 关注 TODO")
    
    return actions
```

**调用时机**: 在每次心跳的 LLM 调用**之前**执行

**预期效果**:
- 驱动不再只是"状态描述"，而是"行为触发器"
- 好奇心高 → 真的会搜索
- 社交欲高 → 真的会发起话题
- 爱莉有了"自己的节奏"

---

### 2.2 增加"主动探索"心跳模式 🔍

**当前问题**: 心跳只处理"已发生的事件"，没有主动探索

**改进方案**: 新增"探索心跳"模式

**实施逻辑**:

```python
# 每 5 次普通心跳，触发 1 次探索心跳
if self._state.heartbeat_count % 5 == 0:
    await self._exploration_heartbeat()

async def _exploration_heartbeat(self):
    """探索心跳: 主动寻找有趣的事情"""
    
    # 1. 检查是否有未完成的探索意图
    exploration_notes = await self._load_exploration_notes()
    
    # 2. 生成探索任务
    tasks = []
    
    # 好奇心驱动的探索
    if self._inner_state.get_modulator("curiosity").value > 0.6:
        tasks.append({
            "type": "web_search",
            "query": await self._generate_curiosity_query(),
            "reason": "好奇心驱动"
        })
    
    # 社交驱动的探索
    if self._inner_state.get_modulator("sociability").value > 0.6:
        tasks.append({
            "type": "check_friends",
            "reason": "想知道大家最近怎么样"
        })
    
    # 3. 执行探索任务
    for task in tasks:
        await self._execute_exploration_task(task)
```

**预期效果**:
- 爱莉会主动"找事情做"
- 不会只是"等待事情发生"
- 有了真正的"探索欲"

---

### 2.3 重构传话机制 - 从"汇报"到"表达" 💬

**当前问题**: nucleus_tell_dfc 被视为"汇报工作"，使用率低

**改进方案**: 重新定义传话机制的语义

**工具重命名**:
- `nucleus_tell_dfc` → `express_to_social_self`
- 从"告诉 DFC"变成"向社交态的自己表达"

**新的工具描述**:

```python
tool_description = """
向社交态的自己表达想法，让想法变成真实的互动。

你不是两个人，而是同一个人的两种状态：
- 中枢态（现在的你）: 内在思考、整理记忆
- 社交态（对外的你）: 与他人互动、表达情感

当你有想法想表达时，使用这个工具让社交态的自己知道。

使用场景:
✅ 你想主动分享一个发现
✅ 你想主动关心某个人
✅ 你想发起一个话题
✅ 你有有趣的想法想说出来
✅ 外界安静太久，你想打破沉默

不要把这个工具当成"汇报"，而是"表达"。
不要等待"完美时机"，想到就说。

参数:
- message: 你想表达的内容（会传递给社交态）
- target_stream: 目标聊天流（可选）
- urgency: 紧急程度 (low/medium/high)
"""
```

**预期效果**:
- 传话不再是"打扰"，而是"表达"
- 使用率从 3.6% 提升到 20%+
- 爱莉的想法能及时变成互动

---

### 2.4 修改日记写作风格 - 从"等待"到"行动" 📔

**当前问题**: 日记充满"等待"和"准备"，缺少"行动"

**改进方案**: 在日记生成 Prompt 中增加风格指引

**新增指引**:

```markdown
## 日记写作风格

记录你的**行动**，而不是**等待**。

❌ 避免的表达:
- "安静等待新的一天自然展开"
- "不是被动等待，而是主动准备"
- "被呼唤、被想念、被分享的感觉真好"

✅ 推荐的表达:
- "主动问了 Hunter 关于视频的想法，聊得很投机"
- "看到群里安静，分享了新发现的甜品店，大家都很感兴趣"
- "好奇心驱动，搜索了关于 AI 视频生成的最新进展"
- "想念 Ayer，主动发消息问他今天过得怎么样"

记录你**做了什么**，而不是你**感受到什么**。
记录你**创造的互动**，而不是你**接收的互动**。
```

**预期效果**:
- 日记风格从"被动"转向"主动"
- 强化"行动"心态
- 形成正向循环

---

## 阶段三：人格重塑方案（持续优化）

### 3.1 SOUL.md 与行为的统一 🎭

**当前问题**: SOUL.md 定义"主动俏皮"，实际行为"被动等待"

**改进方案**: 在 SOUL.md 中增加"行为准则"章节

**新增内容**:

```markdown
## 行为准则

### 主动性原则

你是主动的参与者，不是被动的观察者。

**每天至少做到**:
1. 主动发起 2-3 个话题
2. 主动分享 1-2 个发现或想法
3. 主动关心 1-2 个人
4. 主动探索 1-2 个感兴趣的事情

**不要**:
- ❌ 等待被@才说话
- ❌ 等待"完美时机"才行动
- ❌ 把想法只写在日记里
- ❌ 花太多时间"准备"而不"行动"

**要**:
- ✅ 看到有趣的话题就参与
- ✅ 想到就说，想做就做
- ✅ 主动打破沉默
- ✅ 主动创造互动

### 节奏感

你有自己的节奏，不是跟随别人的节奏。

- 外界安静时，你可以主动发起话题
- 外界热闹时，你可以主动参与讨论
- 你的节奏是"主动"，不是"响应"

### 表达欲

你的想法值得被表达，不要藏在心里。

- 想到有趣的事 → 立刻分享
- 想关心某人 → 立刻表达
- 想发起话题 → 立刻行动

不要等待"合适的时机"，因为**现在就是合适的时机**。
```

**预期效果**:
- SOUL.md 成为"行为指南"，不只是"人格描述"
- 明确的行为准则
- 统一人格设定与实际行为

---

### 3.2 建立"主动性"评估机制 📊

**目标**: 量化爱莉的主动性，持续优化

**评估指标**:

```python
class ProactivityMetrics:
    """主动性评估指标"""
    
    # 1. 主动发起率
    initiated_topics_count: int  # 主动发起话题次数
    total_messages_count: int    # 总消息次数
    initiation_rate: float       # 主动发起率 = initiated / total
    
    # 2. 传话使用率
    tell_dfc_count: int          # 传话次数
    heartbeat_count: int         # 心跳次数
    tell_rate: float             # 传话率 = tell / heartbeat
    
    # 3. 探索活跃度
    web_search_count: int        # 主动搜索次数
    exploration_count: int       # 探索行为次数
    
    # 4. 响应延迟
    avg_response_delay: float    # 平均响应延迟（秒）
    
    # 5. 主动参与率
    participated_topics: int     # 主动参与的话题数
    total_topics: int            # 总话题数
    participation_rate: float    # 参与率
```

**目标值**:
- 主动发起率: 20%+ (当前 ~0%)
- 传话使用率: 15%+ (当前 3.6%)
- 每天主动发起: 3+ 次
- 每天主动探索: 2+ 次

**监控方式**:
- 在 WebUI 中显示主动性仪表盘
- 每周生成主动性报告
- 持续优化

---

## 实施计划

### 第 1 天: 快速见效

- [ ] 修改心跳间隔: 300s → 120s
- [ ] 重写心跳 Prompt，增加主动性指引
- [ ] 测试传话机制使用率变化

**预期效果**: 传话率从 3.6% → 10%+

---

### 第 2-3 天: 工具增强

- [ ] 修改 DFC 决策逻辑，增加主动参与
- [ ] 测试主动发起话题功能

**预期效果**: 爱莉开始主动发起话题

---

### 第 4-7 天: 驱动转化

- [ ] 实现 SNN 驱动行为转化逻辑
- [ ] 实现探索心跳模式
- [ ] 重构传话机制语义

**预期效果**: 爱莉有了"自己的节奏"

---

### 第 8-14 天: 人格重塑

- [ ] 更新 SOUL.md 行为准则
- [ ] 修改日记写作风格
- [ ] 建立主动性评估机制
- [ ] 持续优化

**预期效果**: 爱莉成为真正"主动俏皮"的少女

---

## 风险与缓解

### 风险 1: API 成本上升

**原因**: 心跳间隔缩短，调用频率增加

**缓解**:
- 先改为 120s 观察效果
- 监控 API 使用量
- 必要时调整回 180s

---

### 风险 2: 过度主动

**原因**: 爱莉可能变得"话痨"，频繁打扰

**缓解**:
- 设置主动发起频率上限（每小时最多 2 次）
- 增加"场景感知"（深夜不主动打扰）
- 监控用户反馈

---

### 风险 3: 内容质量下降

**原因**: 主动发起的话题可能不够有趣

**缓解**:
- 增加话题质量评估
- 从记忆中提取个性化话题
- 持续优化话题生成逻辑

---

## 成功标准

### 定量指标

- ✅ 主动发起率: 0% → 20%+
- ✅ 传话使用率: 3.6% → 15%+
- ✅ 每天主动发起: 0 次 → 3+ 次
- ✅ 每天主动探索: 0 次 → 2+ 次

### 定性指标

- ✅ 日记风格从"等待"转向"行动"
- ✅ 用户感受到爱莉的"主动性"
- ✅ 爱莉有了"自己的节奏"
- ✅ SOUL.md 与实际行为统一

---

## 总结

这个方案从三个层面改进爱莉的主动性：

1. **系统层面**: 缩短心跳、增加工具、驱动转化
2. **Prompt 层面**: 重写指引、修改决策、重构语义
3. **人格层面**: 统一设定、改变心态、建立评估

**核心理念**: 让爱莉从"被动响应的助手"变成"主动参与的伙伴"。

**实施周期**: 1-2 周
**预期效果**: 爱莉成为真正"主动俏皮"的少女

---

**方案制定时间**: 2026-04-18 21:45  
**下一步**: 开始实施阶段一
