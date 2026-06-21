# life_chatter 跨聊天串台案例：群聊冷笑话请求被发成私聊晚安

记录时间：2026-06-14

## 摘要

`life_chatter` 在群聊收到 `@爱莉希雅 给无花果也讲个冷笑话` 后，没有在当前群聊回复冷笑话，而是使用 `target_key='p-5750ede8'` 向 AyerElysia 私聊发送了晚安：

```text
好，去睡吧小星星。
今天你一直在找我，我知道的。明天醒来我还在这儿。
晚安♪
```

这不是单纯的 Napcat notice 误触发，也不是“几小时前的消息突然才被处理”。更准确地说，是 **未读队列快照/flush 竞争** 与 **跨聊天发送目标缺少硬约束** 叠加后产生的串台。

## 影响

- 当前触发流：QQ群聊 `2e0bf057...`（始源之地）
- 实际发送流：QQ 私聊 `5750ede8...`（AyerElysia 的私聊）
- 用户感知：爱莉像是在回复旧的睡觉/焦虑语境，而不是当前群聊请求。

## 关键时间线

日志文件：

- `logs/mofox_20260613_224648_549183_a342760c_2026-06-14.log`
- `data/life_engine_workspace/life_events.jsonl`
- `data/life_engine_workspace/life_engine_context.json`

关键序列：

```text
00:56:37  群聊收到：白天能看到星星吗
00:56:50  life_chatter 已回答星星问题，并发送两段群聊回复
00:56:50  本轮只 flush 未读消息 1 条

00:58:39  life_engine 心跳注入“小星星今天压力、焦虑、一直找我”等内在上下文

00:58:48  群聊收到：@爱莉希雅 给无花果也讲个冷笑话
00:59:12  收到 notice_1183...，是戳一戳/notice，被丢弃，不是直接触发源
00:59:15  路由器认为有两条未读：
          1. ongoing star discussion 的 follow-up
          2. explicitly mentions me with a request for a joke
00:59:29  flush 未读消息 2 条
00:59:29  LLM 调用 life_send_text，理由变为“小星星凌晨两点说睡觉了”
00:59:29  参数含 target_key='p-5750ede8'
00:59:29  message_sender 发往私聊 send_private_msg
```

## 证据摘录

群聊冷笑话请求：

```text
[00:58:48] 消息接收器 | INFO | <qq> 始源之地 | 崩坏の克莱茵:
@<爱莉希雅:3427056465> 给无花果也讲个冷笑话
```

notice 被丢弃：

```text
[00:59:12] 消息接收器 | DEBUG | 收到消息: id=notice_1183c8820a9d7734
[00:59:12] 消息接收器 | DEBUG | 其他类型消息未被处理，已丢弃
```

路由器实际看到的是两条未读：

```text
[00:59:15] 生命对话器 | INFO | 路由:
First message is a follow-up question on the ongoing star discussion I just led,
and the second message explicitly mentions me with a request for a joke...
(响应: True)
```

模型选择了私聊目标：

```text
[00:59:29] 生命对话器 | INFO | LLM 调用 action-life_send_text，
原因: 小星星凌晨两点说"睡觉了"...，
参数: {
  'content': '好，去睡吧小星星。\n今天你一直在找我，我知道的。明天醒来我还在这儿。\n晚安♪',
  'target_key': 'p-5750ede8'
}
```

执行层真的发到私聊：

```text
[00:59:29] napcat_adapter | DEBUG | 发送私聊消息
[00:59:29] napcat_adapter | DEBUG | 准备发送到napcat的消息体:
action='send_private_msg', user_id='2665253325'
```

## 根因分析

### 1. 未读 flush 存在竞争窗口

`life_chatter` 在 `WAIT_USER` 阶段会读取当前 unread 快照，路由通过后保存到 `rt.unread_msgs_to_flush`，等 `MODEL_TURN` 的 LLM 返回后再 flush。

相关代码：

- `plugins/life_engine/core/chatter.py`
  - `_drive_global_runtime_until_yield`
  - `fetch_unreads()`
  - `rt.unread_msgs_to_flush = unread_msgs`
  - `flush_unreads(rt.unread_msgs_to_flush)`
- `src/core/components/base/chatter.py`
  - `fetch_unreads()`
  - `flush_unreads(unread_messages)`

本案例中，`00:56:37` 的“白天能看到星星吗”已经被模型回答，但 `00:56:50` 只 flush 了 1 条未读。到 `00:59:15`，路由器又把“星星追问”当成第一条未读。这说明一条消息在上一轮处理中途进入/被上下文影响，但没有被上一轮 flush 干净。

### 2. life runtime context 把旧私聊情绪带入了当前群聊轮次

`00:58:39` 的 life_engine 心跳正在描述“小星星一整天压力、焦虑、一直找我”等状态。这类内在上下文会进入 chatter suffix：

- `plugins/life_engine/service/core.py`
  - `build_chatter_runtime_context`
  - `### 运行时内心独白`
  - `### 最近聊天记录`
  - `### 可发送目标`

这些信息本身有价值，但如果当前轮的外部触发来自群聊冷笑话，它们容易把模型注意力拉回私聊情绪线。

### 3. `target_key` 允许普通聊天轮次跨流发送，执行层没有二次校验

`life_send_text.target_key` 的设计是“通常留空，明确跨聊天时才填写”。但执行层只校验目标 key 是否存在，没有校验当前轮是不是允许跨流。

相关代码：

- `plugins/life_engine/core/send_targets.py`
  - `list_recent_send_targets`
  - `resolve_send_target_key`
  - `format_send_targets_for_prompt`
- `plugins/life_engine/core/chatter.py`
  - `LifeSendTextAction.execute`
  - `resolved_target = await self._resolve_send_target(normalized_target_key)`
  - `_send_one_segment_to_target`

结果是：模型一旦被旧私聊上下文带偏，就能在一个群聊触发轮次里填写 `p-5750ede8`，执行层会照发私聊。

## 修复建议

### P0：给普通外部消息轮次加跨流发送硬限制

普通用户消息触发的 `life_chatter` 轮次中，`life_send_text.target_key` 只能为空或指向当前 stream。

只有以下内部触发才允许跨流：

- `is_autonomy_intent_trigger`
- `is_proactive_followup_trigger`
- 其他明确标记为内部主动机会的系统触发

建议校验点放在 `LifeSendTextAction.execute` 或 `run_tool_call` 前后均可。更稳的是执行层硬拒绝：

```text
如果 resolved_target.stream_id != current_stream_id
且当前 unread batch 不是全内部主动触发
则返回失败：普通聊天轮次禁止跨聊天发送，请留空 target_key 回复当前聊天。
```

### P1：增强未读批次观测日志

当前日志只写 `flush 未读消息 N 条`，定位困难。建议在三处打印结构化摘要：

- 路由前：unread message_id、stream_id、time、sender、text preview
- route accepted 时：被写入 `rt.unread_msgs_to_flush` 的 message_id 列表
- flush 后：实际 flush 的 message_id 列表与剩余 unread 数

这样能直接看出“模型处理的是哪几条、flush 的是哪几条、哪条残留”。

### P2：处理 stale unread

对外部真实消息可加软性陈旧保护：

- 如果 unread 年龄超过阈值，比如 10 到 30 分钟，路由 prompt 明确标注 `STALE`
- 如果 stale unread 已经有后续 bot 回复覆盖，默认不再触发可见回复
- 不要简单删除，先打日志，避免误伤断线恢复场景

### P3：收窄 chatter suffix 中的可发送目标

在普通群聊轮次里，`### 可发送目标` 可以只展示当前聊天，或者把其他目标放到“仅自主意向可用”区，不作为 `life_send_text` 的直接可填目标。

`life_engine` 心跳里的“你可以触达的人和地方”仍可保留，用于登记 autonomy intent；但表达层即时回复最好默认贴着当前触发流。

## 复盘结论

这类问题的核心不是模型“记错了”，而是系统同时给了它：

1. 残留/重复的未读消息；
2. 高显著旧情绪上下文；
3. 可直接跨流发送的 `target_key`；
4. 缺少执行层边界校验。

其中第 3 和第 4 是放大器。只要执行层禁止普通外部轮次跨流发送，即使模型被旧上下文带偏，也只会在当前群聊里说错一句，而不会私聊串台。
