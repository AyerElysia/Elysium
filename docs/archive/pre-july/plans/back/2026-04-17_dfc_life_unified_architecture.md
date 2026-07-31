# DFC-Life 统一架构落地方案

> 作者：Copilot (Claude Opus 4.6)  
> 日期：2026-04-17  
> 前置文档：`notion/DFC_Life合并_统一架构深度分析.md`  

---

## 总体策略：渐进式合并，四阶段推进

```
Phase 0: LifeChatter 骨架       → life_engine 注册 Chatter，与 DFC 并存
Phase 1: 对话执行引擎           → 统一 prompt builder + tool loop
Phase 2: 智能路由 + Sub-agent   → 替代 DFC 的 decision_agent
Phase 3: DFC 退役 + 清理        → 移除 DFC 依赖，清理桥接代码
```

---

## Phase 0：LifeChatter 骨架

### 目标
在 life_engine 插件中注册一个 Chatter 组件，使其能被框架选中处理聊天消息。与 DFC 并存，通过配置切换。

### 文件变更

#### 新建 `plugins/life_engine/core/chatter.py`

```python
"""LifeChatter — life_engine 的统一对话组件。

替代 DFC，直接使用 life_engine 的完整上下文进行对话。
"""

class LifeChatter(BaseChatter):
    chatter_name = "life_chatter"
    chatter_description = "生命中枢统一对话器"
    associated_platforms = []  # 所有平台
    chat_type = ChatType.ALL

    async def execute(self) -> AsyncGenerator[ChatterResult, None]:
        """主执行循环 — 类似 DFC 的 enhanced 模式但直接访问 life 状态。"""
        chat_stream = await self._get_chat_stream()
        service = self._get_life_service()
        
        # 构建 LLM 请求（system prompt 100% 可缓存）
        request = self._build_chat_request(chat_stream, service)
        
        while True:
            _, unreads = await self.fetch_unreads()
            if not unreads:
                yield Wait()
                continue
            
            # 直接从 life 状态构建上下文（零信息损失）
            user_prompt = self._build_chat_user_prompt(
                chat_stream, unreads, service
            )
            request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))
            
            # 执行 LLM + tool loop（复用心跳的工具执行逻辑）
            response = await request.send(stream=False)
            # ... tool call processing (send_text, pass_and_wait, etc.)
            
            yield Wait()
```

#### 修改 `plugins/life_engine/core/plugin.py`

```python
def get_components(self):
    components = [...]  # 现有 service/tool 组件
    
    # 新增：注册 LifeChatter
    if self.config.chatter.enabled:
        from .chatter import LifeChatter
        components.append(LifeChatter)
    
    return components
```

#### 修改 `plugins/life_engine/core/config.py`

```toml
[chatter]
enabled = false          # 默认关闭，手动启用测试
priority = 100           # 高于 DFC 的优先级
mode = "enhanced"        # enhanced / classical
```

#### 修改 `plugins/life_engine/manifest.json`

```json
{
  "chatters": ["life_chatter"],
  "services": ["life_engine"],
  "tools": [...]
}
```

### 验收标准
- [ ] life_engine 插件加载后，框架能发现 LifeChatter
- [ ] 配置 enabled=true 时，LifeChatter 被选中处理消息
- [ ] 配置 enabled=false 时，回退到 DFC
- [ ] LifeChatter 能成功发送一条消息给用户

---

## Phase 1：统一对话执行引擎

### 目标
实现完整的对话 prompt 构建和工具执行循环，直接访问 life_engine 内部状态。

### 核心设计

#### 1.1 对话 System Prompt

```python
def _build_chat_system_prompt(self, chat_stream: ChatStream) -> str:
    """构建对话模式的 system prompt（100% 可缓存）。"""
    parts = []
    
    # 1. SOUL.md（身份核心，与心跳共享）
    soul = self._read_soul_md()
    parts.append(soul)
    
    # 2. 对话框架（固定文本）
    parts.append(CHAT_FRAMEWORK_PROMPT)  # 回复风格、安全准则、工具说明
    
    # 3. 场景引导（按 chat_type 固定）
    scene = self._get_scene_guide(chat_stream)
    if scene:
        parts.append(scene)
    
    return "\n\n---\n\n".join(parts)
```

**关键：System Prompt 完全不含动态内容。** 所有动态信息（内部状态、事件流、消息历史）放在 USER payload 中。

#### 1.2 对话 User Prompt

```python
def _build_chat_user_prompt(
    self,
    chat_stream: ChatStream,
    unreads: list[Message],
    service: LifeEngineService,
) -> str:
    """构建对话模式的 user prompt（含原生内部状态）。"""
    sections = []
    
    # 1. 内在状态（直接读取，0 额外 token）
    inner = service._inner_state
    if inner:
        mood = inner.modulators.get_discrete_dict()
        sections.append(f"<inner_state>\n{self._format_mood(mood)}\n</inner_state>")
    
    # 2. 近期事件摘要（从事件流提取，不是全量）
    recent_events = self._extract_relevant_events(
        service._event_history,
        chat_stream.stream_id,
        max_events=15,
    )
    if recent_events:
        sections.append(f"<recent_context>\n{recent_events}\n</recent_context>")
    
    # 3. 聊天记录（从事件流中筛选此 stream 的消息）
    chat_history = self._extract_chat_history(
        service._event_history,
        chat_stream.stream_id,
        max_messages=20,
    )
    if chat_history:
        sections.append(f"<chat_history>\n{chat_history}\n</chat_history>")
    
    # 4. 新消息
    unread_lines = "\n".join(self._format_message(msg) for msg in unreads)
    sections.append(f"<new_messages>\n{unread_lines}\n</new_messages>")
    
    return "\n\n".join(sections)
```

#### 1.3 工具注册

对话模式可用的工具 = life 工具 + 对话 action：

```python
CHAT_TOOLS = [
    # Action 类（对话专用）
    SendTextAction,         # 发送消息
    PassAndWaitAction,      # 等待新消息
    ThinkAction,            # 内在思考（可选）
    
    # Life 原生工具（完整访问）
    *ALL_TOOLS,             # 文件操作
    *TODO_TOOLS,            # TODO 系统
    *MEMORY_TOOLS,          # 记忆检索
    *WEB_TOOLS,             # 网络搜索
]
```

**注意**：移除 `nucleus_tell_dfc`（不再需要向 DFC 传话）和 `consult_nucleus`（不再需要查询自己）。

#### 1.4 Tool Loop 执行

复用 `_run_heartbeat_model` 的工具执行模式，但增加对话 action 的处理：

```python
async def _run_chat_model(self, request, max_rounds=5):
    """对话模式的工具循环执行。"""
    for _ in range(max_rounds):
        response = await request.send(stream=False)
        response_text = await response
        call_list = list(getattr(response, "call_list", []) or [])
        
        if not call_list:
            break
        
        for call in call_list:
            if call.name == "action-send_text":
                # 直接发送消息
                await self._execute_send_text(call, chat_stream)
                sent = True
            elif call.name == "action-pass_and_wait":
                return "wait"
            else:
                # 其他工具：复用心跳的工具执行
                await self._execute_heartbeat_tool_call(call, response, registry)
        
        if sent:
            break
    
    return "done"
```

### 验收标准
- [ ] 对话 system prompt 100% 可缓存（无动态内容）
- [ ] User prompt 含内在状态、事件摘要、聊天历史、新消息
- [ ] 工具循环能执行 send_text 发送消息
- [ ] 工具循环能执行 life 原生工具（文件/TODO/记忆等）
- [ ] pass_and_wait 正确触发等待

---

## Phase 2：智能路由 + Sub-agent 替代

### 目标
替代 DFC 的 sub_agent 决策机制，利用 life_engine 的原生上下文做更精准的回复决策。

### 2.1 分层决策

```python
async def should_respond(
    self,
    unreads: list[Message],
    chat_stream: ChatStream,
    service: LifeEngineService,
) -> tuple[bool, str]:
    """分层决策：是否回复这些消息。"""
    
    # Layer 1: 硬规则（0 token 消耗）
    if chat_stream.chat_type == "private":
        return True, "私聊直接回复"
    
    if any(self._mentions_bot(msg) for msg in unreads):
        return True, "@提及"
    
    # Layer 2: SNN 激活度（0 token 消耗）
    if service._snn_network:
        activation = service._snn_network.get_social_activation(chat_stream.stream_id)
        if activation > 0.7:
            return True, f"SNN 社交激活度 {activation:.2f}"
    
    # Layer 3: 调质层驱动（0 token 消耗）
    if service._inner_state:
        social_drive = service._inner_state.modulators.get("social_drive")
        if social_drive and social_drive.value > 0.6:
            return True, f"社交驱动 {social_drive.value:.2f}"
    
    # Layer 4: 关键词启发式（0 token 消耗）
    keywords = self._extract_topic_keywords(unreads)
    if self._matches_active_interests(keywords, service):
        return True, "话题匹配活跃兴趣"
    
    # Layer 5: LLM 决策（仅在前面都无法判断时）
    # 这应该很少触发
    return await self._llm_decide(unreads, chat_stream, service)
```

**效果**：群聊场景下，80%+ 的决策在 Layer 1-4 完成，0 token 消耗。

### 2.2 主动对话触发

心跳模式中，life_engine 可以主动发起对话：

```python
# 在 _heartbeat_loop 中
if self._wants_to_chat():
    # 不再通过 nucleus_tell_dfc 传话
    # 直接触发对应 stream 的 LifeChatter
    await self._trigger_proactive_chat(
        stream_id=target_stream,
        context=self._build_proactive_context(),
    )
```

### 验收标准
- [ ] 私聊消息 0 token 决策
- [ ] @提及消息 0 token 决策
- [ ] SNN 激活度高时自动回复
- [ ] 群聊无关消息正确跳过
- [ ] 心跳模式能主动触发对话

---

## Phase 3：DFC 退役 + 清理

### 目标
移除 default_chatter 插件依赖，清理所有桥接代码。

### 3.1 移除的文件/代码

```
plugins/default_chatter/           ← 整个插件可选保留（作为无 life_engine 时的降级方案）
  consult_nucleus.py               ← 移除（不再需要桥接）
  nucleus_bridge.py                ← 移除
  decision_agent.py                ← 替换为 LifeChatter 内的分层决策

plugins/life_engine/
  tools/file_tools.py:
    nucleus_tell_dfc               ← 移除（不再需要传话）
  service/integrations.py:
    DFCIntegration                 ← 简化（不再需要为 DFC 生成摘要）
    get_state_digest_for_dfc()     ← 移除
```

### 3.2 保留的能力

| DFC 原有能力 | 迁移到 | 说明 |
|-------------|--------|------|
| send_text action | LifeChatter 内置 | 相同的消息发送逻辑 |
| pass_and_wait action | LifeChatter 内置 | 相同的等待逻辑 |
| sub_agent 决策 | 分层决策器 | 更精准，更省 token |
| 消息格式化 | LifeChatter 工具方法 | 从 DFC 迁移 |
| 多模态支持 | LifeChatter 工具方法 | 从 DFC 迁移 |
| System prompt 构建 | SOUL.md + CHAT_FRAMEWORK | 更简洁 |
| 历史消息管理 | 事件流 | 不再单独维护 |

### 3.3 降级策略

```toml
# config.toml
[life_engine.chatter]
enabled = true

# 如果 life_engine 未加载或 chatter disabled，
# 框架会自动回退到 default_chatter（如果存在）
```

### 验收标准
- [ ] 移除 DFC 后系统正常工作
- [ ] 所有对话功能通过 LifeChatter 完成
- [ ] 桥接代码清理完毕
- [ ] 降级到 DFC 的路径可用

---

## 上下文管理细节

### 事件流作为唯一历史来源

```
心跳事件:  [HH:MM] 💭 心跳#N: 独白内容
消息事件:  [HH:MM] 📨 平台|方向|类型: 发送者: 内容
工具事件:  [HH:MM] 🔧 工具名(参数摘要)
结果事件:  [HH:MM] ✅/❌ 工具名: 结果摘要
```

对话模式从事件流中**按 stream_id 筛选**聊天历史：

```python
def _extract_chat_history(self, events, stream_id, max_messages=20):
    """从事件流中提取特定聊天流的消息历史。"""
    chat_events = [
        e for e in events
        if e.event_type == EventType.MESSAGE
        and self._event_belongs_to_stream(e, stream_id)
    ]
    # 取最近 max_messages 条
    recent = chat_events[-max_messages:]
    return self._format_chat_events(recent)
```

### 缓存友好性分析

```
请求结构:
┌─────────────────────────────────┐
│ SYSTEM: SOUL.md + 对话框架       │ ← Prefix 缓存 ✅ (每次相同)
│         + 安全准则 + 工具说明     │
├─────────────────────────────────┤
│ TOOL: 工具定义列表               │ ← Prefix 缓存 ✅ (每次相同)
├─────────────────────────────────┤
│ USER: 上下文 + 历史 + 新消息     │ ← 部分缓存 (历史部分相同)
├─────────────────────────────────┤
│ ASSISTANT: 回复                  │ ← 新内容
└─────────────────────────────────┘

预估缓存命中率: 70-85%（System + Tool 完全命中，User 部分命中）
对比 DFC 当前: 20-30%（Reminder 在 System 头部，破坏所有后续缓存）
```

---

## 实施顺序建议

```
Week 1: Phase 0 — 骨架
  ├─ 创建 LifeChatter 类，注册到框架
  ├─ 实现最小可用的 execute() 循环
  ├─ 迁移 send_text / pass_and_wait action
  └─ 基础测试：能收消息能回消息

Week 2: Phase 1 — 对话引擎
  ├─ 实现 _build_chat_system_prompt()
  ├─ 实现 _build_chat_user_prompt()
  ├─ 实现工具循环 + 对话 action 处理
  └─ 集成测试：完整对话流程

Week 3: Phase 2 — 智能路由
  ├─ 实现分层决策器
  ├─ SNN/调质层驱动集成
  ├─ 主动对话触发
  └─ 群聊测试

Week 4: Phase 3 — 清理
  ├─ 移除桥接代码
  ├─ DFC 降级路径
  ├─ 文档更新
  └─ 全面回归测试
```

---

## 预期效果

| 指标 | 当前（DFC） | 目标（LifeChatter） | 提升 |
|------|-----------|-------------------|------|
| Token/轮 | ~3600 | ~1900 | -47% |
| 缓存命中率 | 20-30% | 70-85% | +200% |
| 响应延迟 | 2-3s | 1-2s | -40% |
| 信息保留率 | 10-20% | 95%+ | +400% |
| 上下文深度 | 5-10 条消息 | 完整事件流 | 质变 |
| 情感连续性 | 无 | SNN+调质原生 | 质变 |
| 代码量 | 2 插件 ~3000 行 | 1 插件 ~500 行增量 | -60% |
