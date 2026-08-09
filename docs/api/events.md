# API v1 事件目录

> 本文档是 `/api/v1` 公共接口对外可观察事件类型的权威目录，与阶段三
> `src/app/api/v1/` 及 Life Event 账本合同配套。所有事件进入统一
> `Life Event` 时间线，通过 `GET /api/v1/events`、`/events/stream` 与
> `event-subscriptions/validate` 对外暴露。
>
> 事件 `event_type` 使用点分层技术前缀标识来源，**不用于认知分类**；
> 类型本身不隐含意义、价值或学习裁决（见 `AGENTS.md` 认知零规则）。
> 目录中的事件类型是稳定的技术观察事实，不是封闭认知类别。

## 1. 事件信封与通用字段

`EventEnvelope`（`src/app/api/v1/schemas/events.py`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | string | 全局稳定事件身份 |
| `sequence` | int ≥ 1 | 本节点账本单调序号 |
| `origin_node_id` | string | 事件来源节点 |
| `origin_sequence` | int ≥ 0 | 来源节点内序号 |
| `occurred_at` / `recorded_at` / `published_at` | datetime | 时间投影，统一输出 `Z` 结尾 ISO 8601 |
| `actor` | object | `type`/`id`/`display_name` 脱敏投影 |
| `source` | object | `component`/`connection` |
| `channel` | string | 事件通道（chat / life / tool / agent / proactive / system） |
| `event_type` | string | 点分层技术标识 |
| `consciousness_instance_id` | string? | 所属意识实例 |
| `stream_id` | string? | 关联会话流 |
| `reply_target` | object? | 脱敏回复目标 |
| `correlation_id` / `causation_id` | string? | 关联与因果链 |
| `visibility` | object | `scope`/`audience` 资源授权投影 |
| `payload_hash` | string | `sha256:<64 hex>` 内容完整性 |
| `payload` | object? | 仅在授权后返回 |
| `detail_url` | string | 继续读取链接 |

## 2. 消息与聊天通道（channel: chat）

| event_type | 说明 | 备注 |
| --- | --- | --- |
| `chat.message.received` | 收到入站消息，进入权威时间线 | 消息事实（收方） |
| `chat.message.delivery_confirmed` | 出站消息投递被平台确认 | 消息事实（发方） |
| `chat.message.delivery_failed` | 出站消息投递失败 | 对应 receipt 状态 `failed` |
| `chat.message.delivery_unknown` | 投递结果不可判（超时等） | 对应 receipt 状态 `unknown`，禁止自动重发 |
| `chat.message.read` | 消息已读 | 对应 receipt 状态 `read` |

> 聊天历史投影（`ChatQueryService`）只把 `chat.message.received` 与
> `chat.message.delivery_confirmed` 作为消息事实展示；其余回执类事件保留在
> 时间线中供审计，不重复展示为独立消息。

## 3. 直播通道（channel: life / system，来源 plugins/livestream）

| event_type | 说明 |
| --- | --- |
| `livestream.session_started` | 直播场次开始 |
| `livestream.session_stopped` | 直播场次停止 |
| `livestream.session_interrupted` | 直播场次被打断 |
| `livestream.platform.danmaku_received` | 收到平台弹幕 |
| `livestream.platform.gift_received` | 收到礼物 |
| `livestream.platform.superchat_received` | 收到醒目留言（SC） |
| `livestream.platform.guard_received` | 收到舰长/守护 |
| `livestream.platform.like_received` | 收到点赞 |
| `livestream.platform.entry_received` | 收到入场 |
| `livestream.speech_requested` | 语音表达请求 |
| `livestream.danmaku_sent` / `livestream.danmaku_failed` / `livestream.danmaku_unknown` | 发弹幕结果 |
| `livestream.stage.dispatch` / `livestream.stage.receipt` | 舞台指令与回执（幂等） |

> 具体类型以 `plugins/livestream/ledger.py` 与 `src/app/api/v1/livestream.py`
> 实际写出为准；查询侧以本目录为过滤参考。

## 4. 语音通话通道（channel: life / system，来源 plugins/voice_live）

| event_type | 说明 |
| --- | --- |
| `voice_call.created` | 建立通话记录（只落元数据，不存原始音频） |
| `voice_call.resumed` | 会话恢复 |
| `voice_call.interrupted` | 会话打断 |
| `voice_call.ended` | 会话结束 |
| `voice_call.text` | 通话过程文本片段 |
| `voice_call.transcript.final` | 最终转写片段 |
| `voice_call.ticket_issued` | 签发 WebSocket 单次 ticket |

> 存储层与历史接口**均不保存原始录音对象**；对外只暴露元数据与文本转写
> （见 `AGENTS.md` 与阶段三 25.6 验收）。

## 5. 桌游通道（channel: life，来源 plugins/werewolf_game）

| event_type | 说明 |
| --- | --- |
| `tabletop.werewolf.room_created` | 房间创建 |
| `tabletop.werewolf.snapshot_committed` | 房间状态快照落账（可恢复） |
| `tabletop.werewolf.action_applied` | 玩家/裁判动作已应用 |
| `tabletop.werewolf.action_rejected` | 动作被引擎拒绝（非法/冲突） |
| `tabletop.werewolf.room_ended` | 房间结束 |

> 复盘与恢复只从新 API 权威 ledger 读取，不扫描旧内存房间。

## 6. 潜意识 / 运行状态通道（channel: life / system，来源 life_engine）

| event_type | 说明 |
| --- | --- |
| `consciousness.activated` | 意识实例激活 |
| `consciousness.suspended` | 意识实例挂起 |
| `consciousness.state` | 意识状态变化 |
| `session.created` / `session.ready` / `session.closed` | 会话生命周期 |
| `heartbeat` | 心跳观察（不推进业务 cursor） |
| `perception.projected` | 感知投影 |
| `tool.started` / `tool.completed` / `tool.failed` | 工具执行轨迹（channel: tool） |

> 心跳不推进消费游标；事件目录不把以上类型当作认知分类依据。

## 7. 历史缺口与恢复

- 历史缺口显式失败为 `409 history_gap`，并携带 `recovery.action = restart_from_cursor`
  与可继续读取的 `recovery.cursor`；
- SSE 流断线可用 `Last-Event-ID` 或 `cursor` 恢复，`cursor` 与
  `Last-Event-ID` 不一致返回 `409 cursor_conflict`；
- 所有事件可重放且幂等；同 `occurrence` 同内容重放幂等，同 `occurrence`
  异内容报冲突。
