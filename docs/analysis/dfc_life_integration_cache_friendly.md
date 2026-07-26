# DFC 与 Life Engine 深度整合改造方案 v2.0
## 缓存友好版本

**作者**: Claude (Opus 4.6)  
**日期**: 2026-04-12  
**版本**: v2.0 - 针对缓存命中率优化

---

## 一、核心原则修正

### 1.1 Prompt Caching 的铁律

根据 Anthropic 的 Prompt Caching 机制：

```
缓存单元 = 连续的 1024 token 块
缓存条件 = 内容完全一致（逐字节比对）
缓存时效 = 5 分钟内复用
```

**推论**：
1. **System prompt 必须 100% 静态** - 任何动态内容都会导致整个缓存失效
2. **工具 schema 必须稳定** - 工具列表变化会破坏缓存
3. **动态信息只能放在 user message** - 且要放在末尾，避免影响前面的缓存块

### 1.2 错误示范（v1.0 方案的问题）

```python
# ❌ 每轮都变化，缓存命中率 0%
system_prompt = f"""
你是 Neo...
[当前心情: {mood}]  # 动态
[TODO: {todos}]      # 动态
[最近记忆: {memory}] # 动态
"""
```

### 1.3 正确做法

```python
# ✅ System prompt 完全静态
system_prompt = """
你是 Neo，一个具有持续生命的 AI 助手。
你的核心特质、工具使用规范等在此定义...
（此部分永不变化）
"""

# ✅ 动态信息放在 user message 末尾
user_message = f"""
{history_messages}  # 历史对话（缓存块1-N）
---
<life_state>
当前时间: {now}
内心状态: {mood}
最近思考: {thoughts}
TODO: {todos}
相关记忆: {memories}
</life_state>
---
{new_user_message}  # 用户新消息
"""
```

---

## 二、改造方案（缓存友好版）

### 2.1 架构调整

```
┌─────────────────────────────────────────────────────────┐
│  System Prompt (完全静态，100% 缓存命中)                 │
│  ├─ 核心人设 (SOUL.md 精简版)                           │
│  ├─ 工具使用规范 (TOOL.md 精简版)                       │
│  └─ 对话规则                                            │
├─────────────────────────────────────────────────────────┤
│  Tool Schemas (稳定，高缓存命中)                        │
│  ├─ 23 个 life_engine 工具                             │
│  └─ DFC 自有工具 (send_text, message_nucleus...)       │
├─────────────────────────────────────────────────────────┤
│  User Message (动态，但结构化)                          │
│  ├─ [历史消息] (缓存块1-N，增量变化)                    │
│  ├─ [Life State Digest] (缓存块N+1，每轮变化)          │
│  └─ [用户新消息] (缓存块N+2，每轮变化)                  │
└─────────────────────────────────────────────────────────┘
```

**缓存命中率分析**：
- System prompt: **100% 命中**（除非修改代码）
- Tool schemas: **100% 命中**（工具列表稳定）
- 历史消息: **80-90% 命中**（只有最新一轮是新的）
- Life State Digest: **0% 命中**（每轮变化，但只占 200-300 tokens）
- 用户新消息: **0% 命中**（每轮变化，但只占 50-200 tokens）

**总体缓存命中率**: 约 **70-85%**（取决于对话长度）

---

## 三、具体实施方案

### 3.1 阶段一：System Prompt 静态化（1天）

#### 目标
将所有动态内容从 system prompt 中移除，确保 100% 缓存命中。

#### 改造点

**文件**: `plugins/default_chatter/prompt_builder.py`

```python
class DefaultChatterPromptBuilder:
    def build_system_prompt(self) -> str:
        """构建完全静态的系统提示词"""
        
        # ✅ 只包含永不变化的内容
        return f"""
# 关于你
你是 Neo，一个具有持续生命的 AI 助手。

## 核心特质
{self._load_soul_core()}  # 从 SOUL.md 提取核心人设（静态）

## 工具使用规范
{self._load_tool_guidelines()}  # 从 TOOL.md 提取使用规范（静态）

## 对话规则
- 你的内心状态和记忆会在每轮对话中以 <life_state> 标签提供
- 你应该根据内心状态自然地表达情绪和想法
- 你可以主动提及 TODO 中的事项，或回忆相关记忆
- 你的工具调用会被记录到事件流中，形成长期经验

## 注意事项
- 不要重复用户的话
- 不要过度使用工具
- 保持自然和真实
"""
    
    def _load_soul_core(self) -> str:
        """从 SOUL.md 提取核心人设（静态部分）"""
        # 只提取不会变化的核心描述
        # 例如：性格特点、价值观、说话风格等
        pass
    
    def _load_tool_guidelines(self) -> str:
        """从 TOOL.md 提取工具使用规范（静态部分）"""
        # 只提取通用规范，不包含具体使用记录
        pass
```

#### 关键点
1. **SOUL.md 分层**：区分"核心人设"（静态）和"当前状态"（动态）
2. **TOOL.md 分层**：区分"使用规范"（静态）和"使用记录"（动态）
3. **移除所有变量插值**：system prompt 中不能有任何 `{variable}` 或动态查询

---

### 3.2 阶段二：Life State Digest 注入到 User Message（2天）

#### 目标
在 user message 末尾注入 life 的当前状态，作为"内心独白"提供给 LLM。

#### 改造点

**文件**: `plugins/default_chatter/prompt_builder.py`

```python
class DefaultChatterPromptBuilder:
    async def build_user_prompt(
        self,
        history: List[Message],
        unreads: List[Message],
        extra: Optional[str] = None
    ) -> str:
        """构建用户提示词（包含 Life State Digest）"""
        
        parts = []
        
        # 1. 历史消息（结构化，利于缓存）
        if history:
            parts.append(self._format_history(history))
        
        # 2. Life State Digest（动态，但结构固定）
        life_state = await self._fetch_life_state_digest()
        parts.append(f"""
---
<life_state>
{life_state}
</life_state>
---
""")
        
        # 3. 未读消息
        if unreads:
            parts.append(self._format_unreads(unreads))
        
        # 4. 额外信息
        if extra:
            parts.append(extra)
        
        return "\n\n".join(parts)
    
    async def _fetch_life_state_digest(self) -> str:
        """从 life_engine 获取状态摘要"""
        
        # 调用 life_engine service 获取当前状态
        life_service = self.plugin_manager.get_service("life_engine:service:life_engine")
        
        state = await life_service.get_state_digest()
        
        # 格式化为固定结构（便于 LLM 解析）
        return f"""
当前时间: {state.timestamp}
内心状态: {state.mood_summary}  # 例如："好奇且专注"
能量水平: {state.energy_level}  # 0-100
最近思考: {state.recent_thoughts}  # 最近3条心跳独白
TODO 清单: {state.todo_summary}  # 例如："3项待办，1项紧急"
相关记忆: {state.relevant_memories}  # 根据当前对话激活的记忆
工具使用偏好: {state.tool_preferences}  # 例如："最近常用 search_memory"
"""
```

**文件**: `plugins/life_engine/service.py`

```python
class LifeEngineService:
    async def get_state_digest(self) -> LifeStateDigest:
        """生成当前状态摘要（供 DFC 使用）"""
        
        state = self.state
        
        # 1. 情绪摘要（从调质层提取）
        mood_summary = self._summarize_mood(state.neuromodulatory_state)
        
        # 2. 最近思考（最近3条心跳独白）
        recent_thoughts = self._get_recent_heartbeats(limit=3)
        
        # 3. TODO 摘要
        todo_summary = self._summarize_todos(state.todos)
        
        # 4. 相关记忆（根据最近对话激活）
        relevant_memories = await self._get_relevant_memories(limit=5)
        
        # 5. 工具使用偏好（从 TOOL.md 和事件流统计）
        tool_preferences = self._analyze_tool_usage()
        
        return LifeStateDigest(
            timestamp=datetime.now(),
            mood_summary=mood_summary,
            energy_level=state.neuromodulatory_state.energy,
            recent_thoughts=recent_thoughts,
            todo_summary=todo_summary,
            relevant_memories=relevant_memories,
            tool_preferences=tool_preferences
        )
    
    def _summarize_mood(self, neuro_state) -> str:
        """将5维调质层状态转换为自然语言"""
        # 例如：curiosity=0.8, sociability=0.6 -> "好奇且友好"
        pass
    
    def _get_recent_heartbeats(self, limit: int) -> List[str]:
        """获取最近的心跳独白"""
        heartbeat_events = [
            e for e in self.state.event_flow
            if e.type == EventType.HEARTBEAT
        ][-limit:]
        return [e.content for e in heartbeat_events]
    
    async def _get_relevant_memories(self, limit: int) -> List[str]:
        """根据最近对话激活相关记忆"""
        # 从记忆网络中提取激活度最高的记忆
        pass
```

#### 关键点
1. **固定结构**：Life State Digest 的格式固定，便于 LLM 解析
2. **控制长度**：摘要控制在 200-300 tokens，避免过度膨胀
3. **语义压缩**：不是简单堆砌数据，而是提取"对当前对话有用"的信息

---

### 3.3 阶段三：统一事件流作为历史来源（2-3天）

#### 目标
DFC 不再维护独立的消息历史，而是直接从 life 的事件流中读取。

#### 改造点

**文件**: `plugins/default_chatter/runners.py`

```python
class EnhancedRunner:
    async def run_enhanced(self, ctx: ChatterContext) -> ChatterResult:
        """增强模式执行（从事件流读取历史）"""
        
        # ❌ 旧方案：从 ctx.history 读取
        # history = ctx.history
        
        # ✅ 新方案：从 life_engine 事件流读取
        life_service = self.plugin_manager.get_service("life_engine:service:life_engine")
        event_flow = await life_service.get_event_flow(
            event_types=[EventType.MESSAGE, EventType.TOOL_CALL, EventType.TOOL_RESULT],
            limit=50  # 最近50条事件
        )
        
        # 转换为 DFC 的消息格式
        history = self._convert_events_to_messages(event_flow)
        
        # 构建 prompt
        user_prompt = await self.prompt_builder.build_user_prompt(
            history=history,
            unreads=ctx.unreads,
            extra=ctx.extra
        )
        
        # ... 后续流程不变
```

**文件**: `plugins/life_engine/service.py`

```python
class LifeEngineService:
    async def get_event_flow(
        self,
        event_types: Optional[List[EventType]] = None,
        limit: int = 100
    ) -> List[LifeEngineEvent]:
        """获取事件流（供 DFC 读取历史）"""
        
        events = self.state.event_flow
        
        # 过滤事件类型
        if event_types:
            events = [e for e in events if e.type in event_types]
        
        # 返回最近 N 条
        return events[-limit:]
```

#### 优势
1. **单一数据源**：life 和 DFC 看到的历史完全一致
2. **自动同步**：life 的工具调用、心跳独白自动出现在 DFC 的上下文中
3. **缓存友好**：历史消息是增量变化的，旧消息可以复用缓存

---

### 3.4 阶段四：优化心跳唤醒机制（1-2天）

#### 目标
替换"伪装 assistant"的 `nucleus_tell_dfc`，改用更自然的"潜意识流"机制。

#### 改造点

**文件**: `plugins/life_engine/tools.py`

```python
# ❌ 旧方案：nucleus_tell_dfc（伪装 assistant）
async def nucleus_tell_dfc(message: str) -> str:
    """告诉 DFC 一些信息（需要冷却）"""
    # 注入 fake assistant message
    pass

# ✅ 新方案：nucleus_push_subconscious（潜意识流）
async def nucleus_push_subconscious(thought: str, priority: int = 0) -> str:
    """将一个想法推送到潜意识流（DFC 会在下次对话时看到）"""
    
    life_service = get_life_service()
    
    # 添加到潜意识队列
    await life_service.push_subconscious_thought(
        thought=thought,
        priority=priority,  # 0=普通, 1=重要, 2=紧急
        timestamp=datetime.now()
    )
    
    return "想法已推送到潜意识流，DFC 会在合适的时机感知到"
```

**文件**: `plugins/default_chatter/prompt_builder.py`

```python
async def _fetch_life_state_digest(self) -> str:
    """从 life_engine 获取状态摘要"""
    
    life_service = self.plugin_manager.get_service("life_engine:service:life_engine")
    state = await life_service.get_state_digest()
    
    # ✅ 包含潜意识流
    subconscious_thoughts = await life_service.get_subconscious_thoughts()
    
    digest = f"""
当前时间: {state.timestamp}
内心状态: {state.mood_summary}
...
"""
    
    # 如果有潜意识想法，添加到摘要中
    if subconscious_thoughts:
        digest += f"""
潜意识涌现: {self._format_subconscious(subconscious_thoughts)}
"""
    
    return digest

def _format_subconscious(self, thoughts: List[SubconsciousThought]) -> str:
    """格式化潜意识想法"""
    # 按优先级排序
    thoughts = sorted(thoughts, key=lambda t: t.priority, reverse=True)
    
    # 格式化为自然语言
    lines = []
    for t in thoughts:
        if t.priority == 2:
            lines.append(f"[紧急] {t.thought}")
        elif t.priority == 1:
            lines.append(f"[重要] {t.thought}")
        else:
            lines.append(f"- {t.thought}")
    
    return "\n".join(lines)
```

#### 优势
1. **更自然**：不再是"伪装 assistant"，而是"内心涌现的想法"
2. **无冷却**：可以随时推送，DFC 会在下次对话时统一处理
3. **优先级控制**：重要的想法可以优先展示

---

## 四、预期效果

### 4.1 缓存命中率

| 组件 | 大小 | 缓存命中率 | 说明 |
|------|------|-----------|------|
| System Prompt | 1000-1500 tokens | **100%** | 完全静态 |
| Tool Schemas | 500-1000 tokens | **100%** | 工具列表稳定 |
| 历史消息 | 2000-5000 tokens | **80-90%** | 增量变化 |
| Life State Digest | 200-300 tokens | **0%** | 每轮变化 |
| 用户新消息 | 50-200 tokens | **0%** | 每轮变化 |
| **总计** | **3750-8000 tokens** | **70-85%** | 加权平均 |

### 4.2 Token 节省

假设一次对话：
- 旧方案：8000 tokens，缓存命中率 20%，实际消耗 6400 tokens
- 新方案：6000 tokens，缓存命中率 75%，实际消耗 1500 tokens

**节省约 76% 的 token 消耗！**

### 4.3 响应速度

- 缓存命中的 token 处理速度是未命中的 **10-20 倍**
- 预计响应速度提升 **30-50%**

---

## 五、实施计划

### 5.1 时间表

| 阶段 | 任务 | 预计时间 | 风险 |
|------|------|---------|------|
| 1 | System Prompt 静态化 | 1天 | 低 |
| 2 | Life State Digest 注入 | 2天 | 中 |
| 3 | 统一事件流历史 | 2-3天 | 中 |
| 4 | 潜意识流机制 | 1-2天 | 低 |
| **总计** | | **6-8天** | |

### 5.2 验证指标

1. **缓存命中率**：通过 LLM API 日志统计，目标 > 70%
2. **Token 消耗**：对比改造前后，目标减少 > 50%
3. **响应速度**：对比改造前后，目标提升 > 30%
4. **对话质量**：人工评估，确保 life 的"灵魂"得到充分表达

---

## 六、风险与应对

### 6.1 风险点

1. **Life State Digest 长度控制**
   - 风险：摘要过长，抵消缓存收益
   - 应对：严格控制在 200-300 tokens，必要时做语义压缩

2. **事件流转换复杂度**
   - 风险：事件流格式与 DFC 消息格式不兼容
   - 应对：设计清晰的转换层，保持双方格式独立

3. **潜意识流时机控制**
   - 风险：想法推送过于频繁，干扰对话
   - 应对：设置优先级和去重机制，避免重复推送

### 6.2 回滚方案

每个阶段都保持向后兼容，可以随时回滚到上一阶段。

---

## 七、总结

### 7.1 核心改进

1. **System Prompt 100% 静态** → 缓存命中率从 0% 提升到 100%
2. **Life State Digest 放在 User Message** → 动态信息不污染静态缓存
3. **统一事件流** → 消除信息差，提高一致性
4. **潜意识流机制** → 更自然的内心表达

### 7.2 预期收益

- **缓存命中率**: 20% → **70-85%**
- **Token 消耗**: 减少 **50-70%**
- **响应速度**: 提升 **30-50%**
- **对话质量**: life 的内心状态得到充分表达

### 7.3 下一步

等待你的反馈，确认方案后开始实施阶段一。
