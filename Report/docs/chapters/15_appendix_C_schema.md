# 附录 C · 状态持久化结构摘要

> **用途**：说明 `life_engine_context.json` 如何支撑“重启不是重生”和长期状态连续性。完整 Schema 以开源仓库为准：<https://github.com/AyerElysia/Neo-MoFox-Soul>。  
> **典型文件位置**：`data/life_engine_workspace/life_engine_context.json`

---

本附录保留状态持久化的核心结构，不再全文展开 JSON Schema。课程报告需要证明的是：Neo-MoFox-Soul 的连续性不是 prompt 修辞，而是由可落盘、可恢复、可检查的状态文件支撑。

## C.1 顶层结构

```json
{
  "version": 1,
  "state": {},
  "pending_events": [],
  "event_history": [],
  "snn_state": {},
  "neuromod_state": {},
  "dream_state": {},
  "memory_state": {}
}
```

这些字段共同构成 Life Engine 的恢复点。ThoughtStream 的索引通常单独保存在工作区 `thoughts/streams.json` 中，与 `life_engine_context.json` 一起支撑长期状态恢复。系统崩溃或重启后，不需要从空白状态开始，而是从最近一次持久化的心跳、事件序列和内部变量继续运行。

## C.2 核心运行态

| 字段 | 含义 |
|---|---|
| `heartbeat_count` | 已执行心跳次数 |
| `event_sequence` | 全局单调递增事件号 |
| `last_model_reply_at` | 最近一次 LLM 回复时间 |
| `last_external_message_at` | 最近一次外部输入时间 |
| `last_wake_context_at` | 最近一次构建唤醒上下文时间 |
| `life_chatter_context_cursors` / `chatter_context_cursors` | 各聊天流已同步到 LifeChatter 的事件游标 |

这些字段对应无人系统中的运行时状态、任务序列号和通信游标。

## C.3 事件结构

事件是系统感知外部世界和内部状态变化的统一载体。

```json
{
  "sequence": 128,
  "event_type": "external_message",
  "source": "chat_stream",
  "created_at": "2026-05-04T10:30:00+08:00",
  "payload": {
    "stream_id": "group-123",
    "text": "今天继续改课程报告"
  }
}
```

事件流使系统能够把用户消息、工具结果、心跳、梦报告和内部状态变化放入同一时间轴。这是“感知-状态-决策-反馈闭环”的数据基础。

## C.4 SNN 与调质状态

| 状态块 | 典型内容 | 课程意义 |
|---|---|---|
| `snn_state` | tick 计数、膜电位、突触权重、驱动向量 | 快层感知与局部学习 |
| `neuromod_state` | curiosity、sociability、focus、contentment、energy | 慢变量与感知增益 |
| `memory_state` | 节点、边、强度、最近访问时间 | 长期记忆与联想 |
| `dream_state` | 最近做梦时间、梦计数、巩固记录 | 离线经验整理 |
| `thoughts/streams.json` | 思考流标题、好奇心、焦点时间、revision | 潜意识注意力流 |

这些状态块说明 Neo-MoFox-Soul 的“内部状态”不是单一文本摘要，而是一组不同时间尺度的数值变量和图结构。

## C.5 连续性验证方式

可以用以下方式检查状态连续性：

1. 记录运行中的 `heartbeat_count`、`event_sequence` 和调质变量。
2. 停止系统并保留 `life_engine_context.json`。
3. 重启系统后读取同一文件。
4. 确认心跳计数继续增长，事件序列继续递增，SNN/调质/记忆状态不是重新初始化值。

这对应报告中的 C4 不变式：**重启不是重生**。

## C.6 与课程概念的对应

| 课程概念 | 持久化证据 |
|---|---|
| 状态估计 | `state`, `neuromod_state`, `snn_state` |
| 感知历史 | `event_history`, `chatter_context_cursors` |
| 决策连续性 | `pending_events`, `last_wake_context_at` |
| 在线学习 | `snn_state`, `memory_state` |
| 故障恢复 | `version`, `event_sequence`, `heartbeat_count` |

因此，状态文件是本报告最直接的工程证据之一：它把“持续运行”落到了可审查的数据结构上。
