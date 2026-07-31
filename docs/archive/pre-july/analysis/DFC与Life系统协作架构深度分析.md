# DFC 与 Life 系统协作架构深度分析

> 日期: 2026-04-11
> 分析者: Copilot (Claude Opus 4.6)

## 一、现状全景

### 1.1 两个系统的信息不对称

| 维度 | Life Engine (中枢) | DFC (聊天态) |
|------|-------------------|--------------|
| 运行模式 | 每30秒心跳，持续并行 | 收到消息时触发，请求-响应 |
| 上下文 | 100事件滚动窗口 + 历史摘要 (~22K chars) | 聊天历史 (~500-1000 tokens) + 未读消息 |
| System Prompt | SOUL.md + MEMORY.md + TOOL.md (~16K chars) | 人格核心/侧面 + 身份 + 背景 + 风格 (~1500 tokens) |
| 工具数 | 22个 (文件/TODO/记忆/搜索/浏览器/通信) | 3-5个 (send_text/think/pass/web_search/browser) |
| 感知范围 | 所有聊天流消息 + SNN驱动 + 调质状态 + 日记 + 记忆 + TODO | 当前聊天流消息 + 薄薄一层潜意识提醒 |
| 模型 | Claude Opus 4.6 (推测为life任务) | 对应actor任务的模型 |

**核心矛盾**：Life 是"大脑"，拥有全局视野和深度理解；DFC 是"嘴巴"，直接面对用户但几乎是盲的。

### 1.2 现有通信通道

Life → DFC 的三条通道：

**通道A：潜意识同步 (SystemReminderStore)**
```
life_engine._sync_subconscious_state()
  ↓
SystemReminderStore["actor"]["subconscious"] = text
  ↓
DFC.create_request(with_reminder="actor")
  ↓
context_manager.reminder(text) → 注入到首个 USER block 首段
```
内容格式：
```
【此刻的内心】最近一次心跳独白
【最近在做的事】最近5次工具调用
【近期的思绪】最近3次心跳摘要
```
**问题**：~150-300 tokens，信息密度低，且缺少对话引导。

**通道B：nucleus_tell_dfc (消息队列)**
```
life_engine 调用 nucleus_tell_dfc(message)
  ↓
创建一条 trigger_message 加入 DFC 未读队列
  ↓
等待 DFC 自然唤醒时消费
```
**问题**：被动，且只是一条文本消息，不是结构化的上下文注入。

**通道C：运行时 Assistant 注入**
```
其他插件(如proactive_message_plugin)调用
  push_runtime_assistant_injection(stream_id, content)
  ↓
DFC 在 WAIT_USER 阶段消费，注入为 ASSISTANT payload
```
**问题**：Life engine 自身并没有使用这个通道。

### 1.3 DFC Prompt 结构分析

Enhanced模式下，DFC 的 LLM 请求结构如下：

```
┌───────────────────────────────────────────────┐
│ [SYSTEM] 人格核心 + 身份 + 背景 + 风格 + 安全  │ ← 静态，~1500 tokens
│          + 主题引导                             │    可缓存 ✅
├───────────────────────────────────────────────┤
│ [TOOL] action-send_text schema                  │ ← 静态，~800 tokens
│ [TOOL] action-think schema                      │    可缓存 ✅
│ [TOOL] action-pass_and_wait schema              │
│ [TOOL] tool-nucleus_web_search schema           │
│ [TOOL] tool-nucleus_browser_fetch schema        │
├───────────────────────────────────────────────┤
│ [USER#1]                                        │
│   <system_reminder>                             │ ← 🔴 动态内容在最前面！
│   潜意识同步文本                                  │    每次 runner 重启时变化
│   </system_reminder>                            │    → 破坏后续所有缓存
│                                                 │
│   # 历史消息                                     │ ← 半静态，~500-1000 tokens
│   【HH:MM】<sender> content                     │    本应可缓存但被 reminder 拖累
│   ...                                           │
│   # 新收到的消息                                  │ ← 动态，~100-400 tokens
│   【HH:MM】<sender> content                     │
│   # 额外信息                                     │
│   行为提醒：...                                   │
├───────────────────────────────────────────────┤
│ [ASSISTANT] 模型回复                             │
│ [TOOL_RESULT] 工具执行结果                       │
│ [USER#2] 下一批未读消息                          │ ← 后续轮次
│ ...                                             │
└───────────────────────────────────────────────┘
```

### 1.4 发现的关键问题

#### 🔴 问题1：Reminder 内容陈旧

```python
# runners.py:385 — 只在 runner 初始化时读取一次！
request = chatter.create_request("actor", with_reminder="actor")
# 之后 while True 循环中，reminder 永远是初始化时的快照
```

Life engine 每30秒更新 SystemReminderStore，但 DFC 的 enhanced runner 只在初始化时读取一次。如果 runner 运行了10分钟没重启，DFC 看到的"潜意识"是10分钟前的。

**影响**：Life 的最新心情、最新思考、最新行动，DFC 完全不知道。

#### 🔴 问题2：缓存结构反模式

Reminder（动态内容）被注入到第一个 USER block 的**最前面**。对于支持 prefix caching 的模型（如 Anthropic），这意味着：

```
SYSTEM (静态) → 命中缓存 ✅
TOOL (静态)   → 命中缓存 ✅
USER#1 前缀 = reminder (动态) → ❌ 缓存失效！
USER#1 剩余 = history + unreads → ❌ 被拖累
```

**理想结构应该是**：静态 → 半静态 → 动态，从前到后依次递增变化频率。

#### 🟡 问题3：信息密度过低

当前潜意识同步只有三个块：
- 最近独白（1条心跳回复）
- 最近工具调用（5条）
- 近期思绪（3条心跳摘要）

缺失：
- SNN 驱动状态（虽然注入了 life 的 prompt，但没有传给 DFC）
- 调质层状态（同上）
- 当前活跃 TODO
- 相关日记上下文
- 对话引导/指令（"最近对XX感兴趣，可以多聊"）

#### 🟡 问题4：DFC 无法按需查询 Life

当 DFC 在回复用户时，如果需要 Life 的记忆、日记、TODO 等信息，没有任何工具可以调用。DFC 只能靠固定注入的薄薄一层潜意识。

#### 🟠 问题5：Group Chat Sub-Agent 额外开销

每条群消息都触发一次 sub-agent LLM 调用（~1000 tokens），仅用于判断是否回复。且 sub-agent 也没有 Life 的上下文，判断依据有限。

#### 🟠 问题6：传统请求-响应模式

DFC 的运行模式：
```
收到消息 → Sub-agent判断 → 构建prompt → LLM响应 → 执行工具 → 返回
```

没有"思考时间"，没有"在回复之前先查阅自己的记忆"，没有"参考自己最近的日记来决定语气"。

## 二、根因分析

DFC 和 Life 的信息鸿沟，根源在于**它们是两个独立的 LLM 调用链路**，唯一的桥梁是 SystemReminderStore 中一小段文本。

```
           ┌─── Life Engine LLM ──┐
           │  丰富上下文 (38K chars) │
           │  22 个工具              │
           │  SNN + 调质状态         │
           └───────┬───────────────┘
                   │ (仅 ~200 tokens 通过 SystemReminderStore 传递)
                   ▼
           ┌─── DFC LLM ──────────┐
           │  聊天历史 (~1K tokens) │
           │  3-5 个工具            │
           │  薄薄潜意识            │
           └───────────────────────┘
```

Life 想了很多、知道很多、感受了很多，但能传给 DFC 的只有一小段文字。这就像一个人的大脑有丰富的思考，但嘴巴只能看到一张便条。

## 三、改造方向

### 核心理念：DFC 从"独立聊天机器人"变为"Life 的表达层"

不是让 DFC 自己想怎么说，而是让 Life 告诉 DFC 该怎么感受、该关注什么、该用什么语气，然后 DFC 负责把这些转化为自然的对话。

### 架构目标

1. **信息流畅通**：Life 的丰富理解能实时流入 DFC
2. **上下文干净**：DFC 不加载冗余信息，Life 的内部事件不污染聊天上下文
3. **缓存友好**：静态内容在前、动态内容在后，最大化 prefix cache 命中
4. **按需查询**：DFC 可以在需要时向 Life 查询特定信息
5. **Token 高效**：减少重复加载，减少冗余 prompt 段

---

*此文档为架构分析，具体改造方案见 plan/ 目录下的对应文件。*
