# 附录 D · 可观测性 API 摘要

> **用途**：说明 Neo-MoFox-Soul 如何把内部状态暴露给调试、监控和课程验收。完整端点实现以开源仓库为准：<https://github.com/AyerElysia/Neo-MoFox-Soul>。
> **主要实现位置**：`plugins/life_engine/monitor/router.py`

---

本附录不再列出完整 REST 文档和所有字段示例，只保留课程报告需要的可观测性证据。可观测性是 Neo-MoFox-Soul 与单纯聊天 prompt 的关键区别：系统内部变量可以被读取、比较和复查。

## D.1 快照接口

| 项目 | 内容 |
|---|---|
| 端点 | `GET /message_timeline/api/snapshot` |
| 作用 | 获取当前 Life Engine 状态、事件流、聊天流和摘要 |
| 课程意义 | 证明系统存在可观察的内部状态，而不是只输出对话文本 |

典型返回结构：

```json
{
  "generated_at": "2026-05-04T10:30:00+08:00",
  "life": {
    "state": {
      "heartbeat_count": 42,
      "event_sequence": 128
    },
    "inner_state": {
      "snn_health": {},
      "neuromod_state": {},
      "dream_state": {}
    },
    "recent_events": []
  },
  "summary": {
    "active_stream_count": 1,
    "pending_life_events": 0
  }
}
```

## D.2 调质状态观察

调质状态用于观察系统的慢变量：

```json
{
  "curiosity": {"value": 0.62, "baseline": 0.55},
  "sociability": {"value": 0.48, "baseline": 0.50},
  "focus": {"value": 0.71, "baseline": 0.60},
  "contentment": {"value": 0.65, "baseline": 0.60},
  "energy": {"value": 0.82, "baseline": 0.75}
}
```

这些变量对应报告中的“自适应感知增益”。它们不是 LLM 临时生成的形容词，而是随心跳和事件持续变化的数值状态。

## D.3 SNN 健康状态

SNN 监控字段通常包括：

| 字段 | 含义 |
|---|---|
| `tick_count` | SNN 已运行 tick 数 |
| `drives` | arousal、valence、social、task、exploration、rest 等驱动 |
| `hidden_v_mean` | 隐藏层膜电位均值 |
| `syn_in_hid_mean` | 输入到隐藏层突触均值 |
| `syn_hid_out_mean` | 隐藏层到输出层突触均值 |

这些字段用于判断快层是否在运行、是否发生权重更新、是否存在异常饱和或停滞。

## D.4 事件流与聊天流

快照接口同时暴露：

| 数据块 | 作用 |
|---|---|
| `pending_events` | 尚未处理的 Life Engine 事件 |
| `recent_events` | 最近事件历史 |
| `latest_event` | 最新事件 |
| `streams` | 当前聊天流、活跃状态、未读数和最近消息 |

这使系统可以被当作一个事件驱动的无人系统来观察：输入、状态更新、内部决策和输出都在同一时间线上留下记录。

## D.5 最小调试命令

```bash
curl http://localhost:8080/message_timeline/api/snapshot
```

查看调质状态：

```bash
curl -s http://localhost:8080/message_timeline/api/snapshot | jq '.life.inner_state.neuromod_state'
```

查看心跳计数：

```bash
curl -s http://localhost:8080/message_timeline/api/snapshot | jq '.life.state.heartbeat_count'
```

## D.6 与课程验收的关系

| 验收问题 | 可观测证据 |
|---|---|
| 系统是否持续运行 | `heartbeat_count` 持续增长 |
| 系统是否有内部状态 | `neuromod_state`, `snn_health` |
| 系统是否处理事件流 | `event_sequence`, `recent_events` |
| 系统是否能恢复 | 快照状态与持久化文件一致 |
| 系统是否只是 prompt 包装 | 能否观察到 LLM 外的状态变量 |

因此，可观测性 API 是本报告的重要补充：它让“持续感知与自主状态演化”可以被实际检查，而不是只靠文本描述。
