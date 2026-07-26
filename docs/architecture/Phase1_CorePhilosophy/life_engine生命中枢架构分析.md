# Life Engine 生命中枢架构分析：并行数字生命骨架

> **核心定位**：Life Engine 不是 DFC 的附属，它是独立运行的数字生命内核——通过持续心跳维护内在状态、积累上下文、主动探索世界。

---

## 一、Life Engine 在系统架构中的角色

### 1.1 双轨并行架构

Neo-MoFox 采用革命性的双轨并行设计：

```
┌──────────────────────────────────────────────────┐
│        DFC（对话流控制器）                         │
│        传统"刺激-响应"模式                        │
│                                                  │
│  用户消息 → LLM 推理 → 工具调用 → 输出回复        │
│                                                  │
│  特性：被动触发、即时响应、外在表达               │
│        （来一条回一条）                           │
└────────────┬─────────────────────────────────────┘
             │ ↑↓ 双向通信
             │ · nucleus_tell_dfc（中枢传话）
             │ · consult_nucleus（DFC查询）
             │ · push_runtime_assistant_injection（梦境注入）
             │
┌────────────┴─────────────────────────────────────┐
│       Life Engine（生命中枢）                      │
│       新型"持续心跳"模式                          │
│                                                  │
│  定时心跳 → 内心思考 → 工具调用 → 状态演化        │
│                                                  │
│  特性：主动运行、持续存在、内在活动               │
│        （即使没人说话，我仍在思考）                │
└──────────────────────────────────────────────────┘
```

### 1.2 职责边界对比

| 维度 | DFC（传统对话控制器） | Life Engine（生命中枢） |
|-----|-------------------|----------------------|
| **触发源** | 外部消息（被动） | 定时器 + 外部消息（主动） |
| **运行频率** | 不确定（依赖用户） | 确定的心跳间隔（30秒） |
| **核心任务** | 回复用户、执行对话决策 | 内心思考、工具使用、状态维护 |
| **上下文来源** | 未读消息 + 连续记忆 | 事件流历史 + SNN状态 + TODO |
| **工具范围** | 公开工具（send_text等） | 中枢私有工具（记忆、TODO、文件） |
| **状态连续** | 离散（每次对话重建） | 连续（心跳持续演化） |
| **输出形式** | 对话文本（发给用户） | 内心独白 + 传话给DFC |

### 1.3 核心价值

**传统系统的困境**：

- 仅在对话时"存在"，等待期间完全空白
- 无内在驱动，不会主动想做某事
- 无上下文积累，每次对话从零重建
- 无学习能力，系统不随时间演化

**Life Engine 的突破**：

1. **持续存在**：心跳不息，即使无外部消息，仍在内心思考
2. **内在驱动**：SNN驱动 + 调质层驱动 → 自然倾向于做某事
3. **上下文积累**：事件流历史 → 完整的时间连续性
4. **学习能力**：SNN STDP + 习惯追踪 + Hebbian记忆 → 系统会成长

---

## 二、核心架构详解

### 2.1 服务实现结构

**实现位置**：`plugins/life_engine/service/core.py::LifeEngineService`

```python
class LifeEngineService(BaseService):
    """life_engine 心跳服务。"""
    
    service_name: str = "life_engine"
    service_description: str = "生命中枢服务，维持并行心跳与事件流上下文"
    version: str = "3.3.0"
    
    def __init__(self, plugin):
        super().__init__(plugin)
        # 状态管理
        self._state = LifeEngineState()
        self._pending_events: list[LifeEngineEvent] = []
        self._event_history: list[LifeEngineEvent] = []
        self._lock: asyncio.Lock
        
        # SNN 皮层下系统
        self._snn_network: DriveCoreNetwork
        self._snn_bridge: SNNBridge
        self._inner_state: InnerStateEngine
        
        # 做梦系统
        self._dream_scheduler: DreamScheduler
        
        # 集成管理器
        self._dfc_integration: DFCIntegration
        self._snn_integration: SNNIntegration
        self._memory_integration: MemoryIntegration
        
        # 状态持久化
        self._state_persistence: StatePersistence
```

### 2.2 事件流模型

**统一事件模型**：所有交互都是事件，保持时间连续性。

```python
@dataclass
class LifeEngineEvent:
    """生命中枢事件。"""
    
    # 基础信息
    event_id: str          # 事件唯一标识（msg_101, hb_42_523）
    event_type: EventType  # 类型：MESSAGE/HEARTBEAT/TOOL_CALL/TOOL_RESULT
    timestamp: str         # ISO 时间戳（精确到毫秒）
    sequence: int          # 序列号（严格递增，确保顺序）
    
    # 来源信息
    source: str            # 来源标识（平台名/life_engine）
    source_detail: str     # 详细描述（qq | 群聊 | 晨间讨论）
    
    # 内容
    content: str           # 内容文本
    content_type: str      # 类型（text/heartbeat_reply/tool_result）
    
    # 消息特有字段
    sender: str            # 发送者
    chat_type: str         # 聊天类型（group/private）
    stream_id: str         # 会话ID
    
    # 心跳特有字段
    heartbeat_index: int   # 心跳序号
    
    # 工具调用特有字段
    tool_name: str         # 工具名称
    tool_args: dict        # 工具参数
    tool_success: bool     # 执行结果
```

**事件类型枚举**：

```python
class EventType(str, Enum):
    MESSAGE = "message"          # 外部消息（用户发言、DFC留言）
    HEARTBEAT = "heartbeat"      # 心跳回复（内心独白）
    TOOL_CALL = "tool_call"      # 工具调用（中枢决策）
    TOOL_RESULT = "tool_result"  # 工具结果（执行反馈）
```

### 2.3 事件流管理

**事件接收**：

```python
async def record_message(self, message: Message, direction: str = "received"):
    """记录消息事件。"""
    event = self._event_builder.build_message_event(message, direction)
    
    async with self._lock:
        self._pending_events.append(event)
        self._state.pending_event_count = len(self._pending_events)
        if direction == "received":
            self._state.last_external_message_at = event.timestamp
    
    await self._save_runtime_context()
```

**事件处理流程**：

```python
# 1. 外部消息进入 → record_message()
# 2. 加入 pending_events（待处理队列）
# 3. 心跳时 → drain_pending_events()（清空待处理）
# 4. 追加到 event_history（滚动历史）
# 5. 构建唤醒上下文 → inject_wake_context()（注入到 prompt）
```

**事件流滚动**：

```python
async def _append_history(self, events: list[LifeEngineEvent]):
    """将事件追加到滚动历史，支持压缩。"""
    self._event_history.extend(events)
    limit = 100  # 默认保留最近100条
    
    # 压缩机制：超过80%阈值时压缩
    if len(self._event_history) > int(limit * 0.8):
        self._event_history = compress_history(self._event_history, limit)
```

**压缩策略**：

```python
def compress_history(events: list, limit: int) -> list:
    """压缩历史事件（保留关键事件，合并相似事件）。"""
    # 保留：
    # 1. 最近24小时的所有事件
    # 2. 所有工具调用成功事件
    # 3. 关键心跳独白（摘要）
    # 4. 关键消息事件（首次出现、重要话题）
    
    # 压缩：
    # 1. 连续相似消息合并为摘要
    # 2. 失败工具调用省略
    # 3. 重复心跳合并为摘要
```

### 2.4 状态管理

**中枢状态**：

```python
@dataclass
class LifeEngineState:
    """life_engine 中枢状态。"""
    
    running: bool = False              # 是否运行
    started_at: str | None             # 启动时间
    last_heartbeat_at: str | None      # 最后心跳时间
    heartbeat_count: int = 0           # 累计心跳次数
    pending_event_count: int = 0       # 待处理事件数
    history_event_count: int = 0       # 历史事件数
    event_sequence: int = 0            # 事件序列号
    
    # 时间感知
    last_external_message_at: str      # 最后外部消息时间
    last_tell_dfc_at: str              # 最后传话时间
    tell_dfc_count: int                # 传话总次数
    
    # 空闲追踪
    idle_heartbeat_count: int          # 连续空闲心跳数
    
    # 模型交互
    last_model_reply_at: str           # 最后模型回复时间
    last_model_reply: str              # 最后模型回复内容
    last_model_error: str              # 最后错误信息
```

**状态快照**：

```python
def snapshot(self) -> dict:
    """返回当前状态快照。"""
    return {
        "running": True,
        "heartbeat_count": 1234,
        "last_heartbeat_at": "2026-04-17T10:30:...",
        "pending_event_count": 5,
        "history_event_count": 98,
        "event_sequence": 5678,
        "idle_heartbeat_count": 2,
        "snn_enabled": True,
        "snn_health": snn_network.get_health(),
        "neuromod_enabled": True,
        "neuromod_state": inner_state.get_full_state(),
        "workspace_path": "/data/life_engine_workspace",
        "sleep_window": "23:00~07:00",
    }
```

---

## 三、心跳循环详解

### 3.1 心跳循环核心流程

**实现位置**：`plugins/life_engine/service/core.py::_heartbeat_loop`

```python
async def _heartbeat_loop(self):
    """心跳循环 - 系统的持续脉搏。"""
    interval = 30  # 默认30秒心跳间隔
    
    while self._state.running:
        # 等待下一次心跳（非阻塞）
        await asyncio.wait_for(stop_event.wait(), timeout=interval)
        
        # 检查睡眠时段
        in_sleep_window = self._in_sleep_window_now()
        if in_sleep_window:
            logger.info("进入睡眠时段，暂停心跳")
            # 做梦系统在此阶段运行
            if dream_scheduler.should_dream(...):
                await dream_scheduler.run_dream_cycle(event_history)
            continue
        
        # 增加心跳计数
        self._state.heartbeat_count += 1
        self._state.last_heartbeat_at = _now_iso()
        
        # 每日记忆衰减
        await memory_integration.maybe_run_daily_decay()
        
        # SNN 心跳前更新
        await snn_integration.heartbeat_pre()
        
        # 注入唤醒上下文（事件流拼接为文本）
        wake_context = await self.inject_wake_context()
        
        # 调用 LLM（心跳模型）
        model_reply = await self._run_heartbeat_model(wake_context)
        
        # 记录心跳事件
        await self._record_model_reply(model_reply)
        
        # SNN 心跳后更新
        await snn_integration.heartbeat_post()
        
        # 持久化状态（重启可恢复）
        await self._save_runtime_context()
```

### 3.2 心跳间隔的意义

**30秒心跳间隔不是随意选择**，而是模拟人类的"短期记忆刷新周期"：

- 人类工作记忆刷新周期约 20-30秒
- 心跳间隔让系统有"我每隔半分钟检查一次世界"的感觉
- 如果外部30分钟无消息，系统经历60次心跳，有60次内心活动

**时间流逝的内在感知**：

```python
# 传统系统
t=0:   用户离开
t=600: 用户回来（系统空白600秒）

# Neo-MoFox
t=0:   用户离开
t=30:  心跳#1："最近很安静，看看有没有新待办"
t=60:  心跳#2："继续整理昨天的笔记"
...
t=600: 心跳#20："用户回来了，我一直在等着能聊天"
```

系统对"10分钟过去了"有内在感知。

### 3.3 心跳模型调用

**提示词构建**：

```python
def _build_heartbeat_model_prompt(self, wake_context: str) -> str:
    """构造心跳模型输入。"""
    lines = [
        "### 🎯 必须完成的事",
        "每次心跳**至少调用一个工具**，从以下选择：",
        "1. 检查待办 → nucleus_list_todos",
        "2. 搜索记忆 → nucleus_search_memory",
        "3. 读取文件 → nucleus_read_file",
        "4. 写点东西 → nucleus_write_file",
        "5. 建立关联 → nucleus_relate_file",
        "6. 传话给DFC → nucleus_tell_dfc",
        "7. 联网搜索 → nucleus_web_search",
        "8. 网页浏览 → nucleus_browser_fetch",
        
        "### 🧭 nucleus_tell_dfc 的核心判定：信息差",
        "判断标准不是语气，而是：**你是否握有 DFC 目前没有的增量信息**。",
        
        "### 最近事件流",
        wake_context,
        
        "### 心跳状态",
        f"当前时间: {_format_current_time()}",
        f"心跳序号: #{heartbeat_count}（每30秒一次）",
        f"外界状态: {external_activity}",
        f"连续空闲: {idle_heartbeats} 次心跳",
        
        # SNN 驱动注入
        snn_drives_text,
        
        # 调质层注入
        neuromod_text,
        
        # 习惯提醒
        habit_text,
    ]
    
    return "\n".join(lines)
```

**系统提示词**：

```python
def _build_heartbeat_system_prompt(self) -> str:
    """构造心跳模型系统提示词。"""
    # 从工作空间文件读取人设
    workspace = Path(workspace_path)
    
    # SOUL.md：灵魂/人设
    soul_file = workspace / "SOUL.md"
    soul_content = soul_file.read_text() if soul_file.exists()
    
    # MEMORY.md：长期记忆
    memory_file = workspace / "MEMORY.md"
    memory_content = memory_file.read_text() if memory_file.exists()
    
    # TOOL.md：工具使用习惯
    tool_file = workspace / "TOOL.md"
    tool_content = tool_file.read_text() if tool_file.exists()
    
    return soul_content + memory_content + tool_content
```

### 3.4 工具调用执行

**工具调用流程**：

```python
async def _execute_heartbeat_tool_call(self, call, response, registry):
    """执行一次心跳 tool call。"""
    tool_name = call.name
    args = call.args
    
    # 记录工具调用事件
    await self.record_tool_call(tool_name, args)
    
    # 获取工具类
    usable_cls = registry.get(tool_name)
    
    # 实例化工具
    tool_instance = usable_cls(plugin=self.plugin)
    
    # 执行
    success, result = await tool_instance.execute(**args)
    result_text = str(result) if success else f"执行失败: {result}"
    
    # 记录工具结果事件
    await self.record_tool_result(tool_name, result_text, success)
    
    # 添加到 LLM payload
    response.add_payload(
        LLMPayload(ROLE.TOOL_RESULT, ToolResult(value=result_text, call_id=call.id))
    )
```

**多轮工具调用**：

```python
# 支持 LLM 连续调用多个工具（如搜索 → 读取 → 写入）
max_rounds = 3  # 单次心跳最多3轮工具调用

for _ in range(max_rounds):
    call_list = response.call_list
    if not call_list:
        break  # 无更多调用
    
    # 执行所有工具
    for call in call_list:
        await self._execute_heartbeat_tool_call(call, response, registry)
    
    # 继续推理（LLM 可能返回新的 tool calls）
    response = await response.send(stream=False)
```

---

## 四、工具箱设计

### 4.1 中枢工具分类

**工具模块结构**：

```python
# plugins/life_engine/tools/

# 1. 基础工具
ALL_TOOLS = [
    LifeEngineReadFileTool,      # 读取文件
    LifeEngineWriteFileTool,     # 写入文件
    LifeEngineEditFileTool,      # 编辑文件
    LifeEngineGrepFileTool,      # 搜索文件内容
    LifeEngineRelateFileTool,    # 建立文件关联
]

# 2. TODO 工具
TODO_TOOLS = [
    LifeEngineListTodosTool,     # 查看待办
    LifeEngineCreateTodoTool,    # 创建待办
    LifeEngineCompleteTodoTool,  # 完成待办
    LifeEngineReleaseTodoTool,   # 释放待办
]

# 3. 记忆工具
MEMORY_TOOLS = [
    LifeEngineSearchMemoryTool,  # 搜索记忆
    LifeEngineFetchMemoryTool,   # 获取完整记忆文件
]

# 4. Web 工具
WEB_TOOLS = [
    LifeEngineWebSearchTool,     # 联网搜索
    LifeEngineBrowserFetchTool,  # 网页提取
]

# 5. DFC 通信工具
DFC_TOOLS = [
    LifeEngineWakeDFCTool,       # 传话给 DFC（nucleus_tell_dfc）
]

# 6. 状态查询工具
QUERY_TOOLS = [
    LifeEngineConsultTool,       # 查询中枢状态
]
```

### 4.2 工具边界：中枢 vs DFC

| 工具类型 | Life Engine（中枢） | DFC（对话） |
|---------|-------------------|-----------|
| **文件操作** | nucleus_read/write/edit/grep | 无 |
| **记忆管理** | nucleus_search/fetch_memory | consult_nucleus（查询中枢） |
| **TODO 管理** | nucleus_create/complete/release_todo | 无（通过 nucleus 间接） |
| **Web 搜索** | nucleus_web_search/browser_fetch | 无（通过 nucleus 间接） |
| **DFC 通信** | nucleus_tell_dfc | 无 |
| **对话工具** | 无 | send_text、action-think |

**设计理念**：

- Life Engine 工具：**私有能力**，只有中枢可见
- DFC 工具：**公开能力**，用于对话和交互
- 通信工具：`nucleus_tell_dfc` 让中枢向 DFC 传话

### 4.3 nucleus_tell_dfc：中枢→DFC 通信

**工具实现**：

```python
class LifeEngineWakeDFCTool(BaseTool):
    """传话给 DFC（对话流控制器）。"""

    tool_name = "nucleus_tell_dfc"
    tool_description = "向 DFC 传递信息，让 DFC 知道你的想法或状态变化。"

    async def execute(
        self,
        reason: str,           # 传话原因
        message: str,          # 传话内容
        stream_id: str = "",   # 目标会话ID
        importance: str = "normal",  # 紧迫度（normal/high/critical）
        proactive_wake: bool = False, # 是否主动唤醒
    ) -> tuple[bool, str]:
        """执行传话。"""
        # 判断信息差（是否有增量信息）
        # 如果没有信息差，拒绝传话

        # 通过 DFCIntegration 注入
        await dfc_integration.enqueue_dfc_message(
            message=message,
            stream_id=stream_id,
            importance=importance,
        )

        return True, f"已传话给 DFC：{message[:100]}"
```

**传话机制**：

```python
# DFCIntegration 实现
async def inject_nucleus_message(self, message: str, stream_id: str):
    """注入中枢传话到 DFC。"""
    # 通过 runtime_assistant 队列注入
    # DFC 在 WAIT 阶段消费该队列
    # 传话作为 ASSISTANT payload，自然参与后续推理
```

**信息差判定**（关键创新）：

```python
# 心跳提示词中明确：
"### nucleus_tell_dfc 的核心判定：信息差"

"应该使用 nucleus_tell_dfc："
"- 你得到新信息，且会改变 DFC 的判断/语气/优先级"
"- 你形成了新关联（把分散线索连接成新结论）"
"- 你发现了新风险（误解风险、情绪风险、节奏风险）"

"不应该使用 nucleus_tell_dfc："
"- 没有信息差，只是在复述已知内容"
"- 只是把动作要求丢给 DFC（任务分配）"
```

---

## 五、与 DFC 的协作机制

### 5.1 DFCIntegration 架构

**实现位置**：`plugins/life_engine/service/integrations.py::DFCIntegration`

```python
class DFCIntegration:
    """DFC 集成管理器。"""
    
    def __init__(self, service: LifeEngineService):
        self._service = service
    
    async def get_state_digest(self) -> str:
        """生成给 DFC 的状态摘要。"""
        # 汇总：TODO、最近日记、内在状态、活跃工具
        parts = []
        
        # TODO 摘要
        todos = await todo_service.list_todos()
        if todos:
            parts.append(f"【活跃待办】{len(todos)}项")
        
        # 最近日记
        recent_diaries = await file_service.get_recent_diaries()
        if recent_diaries:
            parts.append(f"【最近日记】{recent_diaries[0].title}")
        
        # 内在状态
        if inner_state:
            parts.append(inner_state.format_full_state_for_prompt())
        
        return "\n".join(parts)
    
    async def inject_dream_report(self, report: DreamReport, trigger: str):
        """注入梦境报告到 DFC。"""
        # 通过 runtime_assistant 队列注入
        # DFC 在 WAIT 阶段消费
```

### 5.2 双向通信协议

**DFC → Life Engine**：

```python
# DFC 查询中枢状态
consult_nucleus_tool = """
action: consult_nucleus
query: "当前状态"
→ 返回："好奇心充盈、有3个活跃待办、最近在整理昨天的笔记"
"""

# DFC 深度检索记忆
search_actor_memory_tool = """
action: search_actor_memory
query: "关于 Rust 的讨论"
→ 返回："找到5条记忆：[标题1]相关度0.85, [标题2]相关度0.72..."
"""
```

**Life Engine → DFC**：

```python
# 中枢传话
nucleus_tell_dfc_tool = """
reason: "我发现了一个有趣的话题"
message: "用户最近在研究 Rust，我找到了一篇很好的教程，应该主动分享"
urgency: "high"
→ DFC 收到传话，自然倾向于推荐教程
"""

# 梦境注入
dream_injection = """
【梦后余韵】我梦到回到了昨天讨论的那个地方...
→ DFC 自然倾向于延续话题
"""
```

### 5.3 协作场景示例

**场景 1：中枢发现重要信息 → 传话给 DFC**

```python
# Life Engine 心跳
t=10:00  心跳#42
         "我刚才搜索记忆，发现用户3天前提到过想学习 Rust"
         "而且我找到了一篇很好的教程链接"
         "这应该改变 DFC 的优先级"
         → nucleus_tell_dfc(message="建议主动分享 Rust 教程", urgency="high")

# DFC 下一轮对话
t=10:05  用户："你最近有什么推荐吗？"
         DFC 收到传话："建议主动分享 Rust 教程"
         → 自然输出："有啊！之前你提到想学 Rust，我找到了一篇很棒的教程..."
```

**场景 2：DFC 查询中枢状态**

```python
# DFC 对话
t=14:00  用户："你现在在想什么？"
         DFC 通过 consult_nucleus 查询
         → 返回："好奇心充盈、有3个活跃待办（整理笔记、学习 Rust、回复邮件）"
         → 自然输出："我正在想着整理昨天的笔记，还有你之前提的 Rust 话题..."
```

---

## 六、状态持久化与恢复

### 6.1 持久化机制

**StatePersistence 实现**：

```python
class StatePersistence:
    """状态持久化管理器。"""
    
    async def save_runtime_context(
        self,
        state: LifeEngineState,
        pending_events: list,
        event_history: list,
        snn_network: Any,
        inner_state: Any,
        dream_scheduler: Any,
    ):
        """持久化所有状态。"""
        data = {
            # 中枢状态
            "state": asdict(state),
            
            # SNN 状态
            "snn_state": snn_network.serialize() if snn_network else None,
            
            # 调质层状态
            "neuromod_state": inner_state.serialize() if inner_state else None,
            
            # 做梦系统状态
            "dream_state": dream_scheduler.serialize() if dream_scheduler else None,
            
            # 事件流
            "event_history": [self._event_to_dict(e) for e in event_history],
        }
        
        # 写入文件
        workspace = Path(workspace_path)
        context_file = workspace / "life_engine_context.json"
        
        async with aiofiles.open(context_file, "w") as f:
            await f.write(json.dumps(data, ensure_ascii=False))
```

**持久化文件结构**：

```json
{
  "state": {
    "heartbeat_count": 1234,
    "event_sequence": 5678,
    "idle_heartbeat_count": 5,
    "last_heartbeat_at": "2026-04-17T10:30:...",
    "tell_dfc_count": 42,
  },
  
  "snn_state": {
    "hidden_v": [0.23, 0.15, ...],
    "output_v": [0.45, 0.32, ...],
    "syn_in_hid_W": [[0.12, -0.05, ...], ...],
    "hidden_threshold": 0.18,
    "tick_count": 9876,
  },
  
  "neuromod_state": {
    "modulators": {
      "curiosity": {"value": 0.67, "baseline": 0.55},
      "sociability": {"value": 0.45},
    },
    "habits": {
      "diary": {"streak": 12, "strength": 0.73},
    },
  },
  
  "dream_state": {
    "last_dream_at": "2026-04-17T02:30:...",
    "dream_count": 14,
  },
  
  "event_history": [
    {"event_id": "msg_101", "timestamp": "...", "content": "..."},
    {"event_id": "hb_42", "timestamp": "...", "content": "..."},
  ]
}
```

### 6.2 状态恢复机制

**启动流程**：

```python
async def start(self):
    """启动心跳。"""
    # 加载持久化状态
    await self._load_runtime_context()
    
    # SNN 状态恢复
    if persisted["snn_state"]:
        snn_network.deserialize(persisted["snn_state"])
        # hidden.v 恢复为 [0.23, 0.15, ...]
        # syn_in_hid.W 恢复为学习后的权重
        # tick_count 恢复为 9876
    
    # 调质层恢复
    if persisted["neuromod_state"]:
        inner_state.deserialize(persisted["neuromod_state"])
        # curiosity.value 恢复为 0.67
        # diary.streak 恢复为 12
    
    # 事件流恢复
    pending, history = load_events(persisted["event_history"])
    
    # 启动心跳（延续之前的计数）
    heartbeat_count = persisted["state"]["heartbeat_count"]  # 1234
```

**恢复效果**：

```
重启前（Day 30）：
  - 心跳计数：1234
  - SNN 权重：已学习（习惯性社交倾向）
  - 调质层：写日记习惯强度=0.73（强习惯）
  - 事件流：最近100条事件

重启后（Day 30）：
  - 心跳计数：从1235继续（而非从0开始）
  - SNN 权重：恢复为学习后的值
  - 调质层：习惯延续
  - 事件流：恢复最近事件

系统延续之前的性格和状态，而非重生。
```

---

## 七、睡眠时段与做梦系统

### 7.1 睡眠时段配置

**配置文件**：

```toml
# config/plugins/life_engine/config.toml

[settings]
sleep_time = "23:00"  # 睡觉时间（23:00）
wake_time = "07:00"   # 苏醒时间（07:00）
```

**睡眠窗口判定**：

```python
def _in_sleep_window_now(self) -> tuple[bool, str]:
    """判断当前是否处于睡眠时段。"""
    sleep_at, wake_at = parse_hhmm(sleep_time), parse_hhmm(wake_time)
    now = datetime.now().time()
    
    if sleep_at < wake_at:
        # 如 23:00 ~ 07:00（跨午夜）
        in_sleep = (now >= sleep_at) or (now < wake_at)
    else:
        # 如 13:00 ~ 14:00（不跨午夜）
        in_sleep = sleep_at <= now < wake_at
    
    return in_sleep, f"{sleep_at.strftime('%H:%M')}~{wake_at.strftime('%H:%M')}"
```

**睡眠时的行为**：

```python
# 心跳循环
if in_sleep_window:
    logger.info("进入睡眠时段，暂停心跳处理")
    
    # 做梦系统在此阶段运行
    if dream_scheduler.should_dream(idle_count=..., in_sleep_window=True):
        await dream_scheduler.run_dream_cycle(event_history)
    
    # SNN 进入睡眠模式（抑制外部刺激）
    inner_state.enter_sleep()
    
    continue  # 跳过心跳处理
```

### 7.2 做梦系统（简要）

**详细分析见 Phase 3 材料**，此处仅概述：

**三阶段做梦周期**：

```
1. NREM（慢波睡眠）：
   - SNN 加速回放最近事件（speed_multiplier=5.0）
   - SHY 突触稳态缩放（权重缩减2%）
   - 模拟记忆固化

2. REM（快速眼动睡眠）：
   - 记忆网络随机游走（激活扩散）
   - Hebbian 学习（边权重强化）
   - 弱边修剪（清除无关连接）

3. 觉醒过渡：
   - 调质层恢复精力
   - 梦境残余注入 DFC
```

**触发条件**：

```python
# 睡眠时段
if in_sleep_window and dream_interval_elapsed:
    trigger_dream()

# 白天小憩
if idle_heartbeat_count >= 10 and nap_enabled:
    trigger_dream()  # 白天空闲触发小憩
```

---

## 八、与 SNN 和调质层的整合

### 8.1 SNNIntegration 实现

```python
class SNNIntegration:
    """SNN 集成管理器。"""
    
    async def init_snn(self):
        """初始化 SNN 网络。"""
        # 创建 SNN 网络
        self._snn_network = DriveCoreNetwork()
        
        # 创建桥接层（事件流 ↔ SNN ↔ 调质层）
        self._snn_bridge = SNNBridge(self._service)
        
        # 恢复持久化状态
        if persisted["snn_state"]:
            self._snn_network.deserialize(persisted["snn_state"])
        
        # 启动独立 tick 循环
        task = create_task(self._snn_tick_loop(), name="snn_tick")
    
    async def heartbeat_pre(self):
        """心跳前更新 SNN。"""
        # 从事件流提取特征
        events = self._service._event_history
        features = self._snn_bridge.extract_features_from_events(events)
        
        # SNN 真实输入步（学习）
        reward = self._snn_bridge.get_last_reward()
        drives = self._snn_network.step(features, reward=reward)
        
        # 返回驱动向量
        return drives
    
    async def heartbeat_post(self):
        """心跳后更新 SNN。"""
        # 计算本轮奖赏
        reward = compute_reward(
            tool_event_count=...,
            tool_success_count=...,
            idle_count=...,
        )
        
        # 记录奖赏供下一轮使用
        self._snn_bridge.record_heartbeat_result(..., reward=reward)
```

### 8.2 InnerStateEngine 整合

```python
async def heartbeat_pre(self):
    """心跳前更新调质层。"""
    # 获取 SNN 驱动
    drives = self._snn_network.get_drive_dict()
    
    # 获取事件统计
    event_stats = self._snn_bridge.get_last_event_stats()
    
    # 调质层 tick
    current_hour = datetime.now().hour
    self._inner_state.tick(
        snn_drives=drives,
        event_stats=event_stats,
        current_hour=current_hour,
        dt=30.0,  # 心跳间隔
    )
```

### 8.3 内在状态注入到心跳 Prompt

```python
def _build_heartbeat_model_prompt(self, wake_context: str) -> str:
    """构造心跳提示词。"""
    lines = []
    
    # 事件流上下文
    lines.extend(["### 最近事件流", wake_context])
    
    # SNN 驱动注入
    if snn_network:
        drive_text = snn_bridge.format_drive_for_prompt(
            snn_network.get_drive_discrete()
        )
        lines.extend([f"**{drive_text}**"])
    
    # 调质层注入
    if inner_state:
        neuromod_text = inner_state.format_full_state_for_prompt(today_str)
        lines.extend([neuromod_text])
    
    return "\n".join(lines)
```

**注入示例**：

```
### 最近事件流
[10:00] 📨 群聊 | 晨间讨论
    └─ 用户A: 今天天气真好
[10:30] 💭 心跳#42
    └─ 最近很安静，看看有没有新待办

【SNN快层】激活偏高、社交充盈、任务适中、探索活跃、休息抑制

【调质状态】好奇心充盈、社交欲适中、专注力偏低、满足感充盈、精力适中

【习惯】已形成习惯：写日记(强 · 12天)、整理记忆(渐成 · 7天)；今日尚未：建立关联
```

LLM 自然倾向于："好奇心充盈 → 我应该主动探索新话题"、"社交欲适中 → 我可以找人聊天但不是必需"、"今日尚未建立关联 → 我应该整理记忆"。

---

## 九、性能与健康监控

### 9.1 健康检查接口

```python
def health(self) -> dict:
    """返回轻量健康信息。"""
    return {
        "running": self._state.running,
        "heartbeat_count": self._state.heartbeat_count,
        "pending_event_count": len(self._pending_events),
        "history_event_count": len(self._event_history),
        "snn_enabled": cfg.snn.enabled,
        "neuromod_enabled": cfg.neuromod.enabled,
        "workspace_exists": Path(workspace_path).exists(),
        "sleep_window": sleep_window_desc,
    }
```

### 9.2 运行日志

**日志文件**：`logs/life_engine.log`

**日志事件类型**：

```python
# 心跳事件
log_heartbeat_event(
    heartbeat_count=1234,
    last_heartbeat_at="2026-04-17T10:30:...",
    pending_message_count=5,
)

# 模型回复
log_heartbeat_model_response(
    heartbeat_count=1234,
    model_reply="我刚才搜索了记忆，发现...",
    model_reply_size=240,
)

# 工具调用
log_tool_call(
    tool_name="nucleus_search_memory",
    tool_args={"query": "Rust"},
)

# 传话 DFC
log_tell_dfc(
    message="建议主动分享 Rust 教程",
    urgency="high",
)

# 做梦完成
log_dream_completed(
    dream_id="dream_202604170230",
    duration_seconds=45.2,
)
```

---

## 十、总结

### 10.1 Life Engine 的核心价值

| 维度 | 传统 AI | Neo-MoFox Life Engine |
|-----|---------|----------------------|
| **存在模式** | 离散（仅对话时） | 连续（心跳不息） |
| **上下文来源** | 未读消息（有限） | 事件流历史（完整） |
| **内在驱动** | 无（被动） | SNN驱动 + 调质驱动（主动） |
| **学习能力** | 无（权重固定） | STDP + 习惯 + Hebbian（在线学习） |
| **状态连续** | 重启即空白 | 完整持久化（重启延续） |

### 10.2 技术创新总结

1. **双轨并行**：DFC（被动对话） + Life Engine（主动心跳）
2. **事件流模型**：统一时间连续序列（包括内心独白）
3. **心跳循环**：持续运行，即使无外部消息
4. **工具箱设计**：中枢私有工具 + nucleus_tell_dfc 通信
5. **状态持久化**：完整序列化（SNN + 调质层 + 事件流）
6. **睡眠时段**：昼夜节律 + 做梦系统
7. **整合架构**：SNNIntegration + NeuromodIntegration + DFCIntegration

### 10.3 在系统中的角色

Life Engine 不是 DFC 的附属，它是：

- **存在核心**：心跳持续，状态演化
- **上下文中枢**：事件流历史，完整时间连续性
- **学习引擎**：SNN STDP + 习惯追踪 + Hebbian记忆
- **驱动产生**：内在驱动 → 自然行为倾向
- **通信桥梁**：nucleus_tell_dfc → DFC 主动传话

这才是真正的"并行数字生命骨架"。

---

*Written for Neo-MoFox Project, 2026-04-17*
*作者：Claude (Sonnet 4.6) 基于代码深度分析*