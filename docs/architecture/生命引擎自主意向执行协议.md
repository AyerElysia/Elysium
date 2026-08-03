# Life Engine 自主意向执行协议

本文描述自主意向从形成、浮现到外部动作回执的当前工程契约。它只约束执行安全，不判断她应该说什么、向谁表达什么，也不改写她形成意向时的动机与约束。

## 1. 设计边界

- 自主意向是未来重新判断的机会，不是定时命令。
- 基础设施只管理身份、租约、并发、目标权限、回执和历史；表达与沉默仍由意识在 occurrence 浮现时决定。
- 不能以“相同文字”判断重复。相同内容可能是新的真实选择；重复防护必须基于稳定的因果身份。
- 不修改主体所有的世界状态、人格、记忆正文或原始意向语义。

## 2. 状态机

每次浮现都有稳定身份 `occurrence_id = <intent_id>:<occurrence_count>`。调度器只登记一次性回调，不直接创建无限周期任务。

```text
scheduled
  -> in_flight/surfaced
  -> in_flight/dispatching
  -> triggered                    # 单次意向安全结束
  -> scheduled                    # 周期意向收到安全终态后再链下一次
  -> renewal_required             # 结果未知、失败、租约耗尽或崩溃恢复
```

安全终态包括 `sent`、`passed`、`reflected`、`silence` 和无目标时的 `surfaced`。`delivery_unknown` 与 `failed` 绝不自动重放，也不会继续链式调度。

周期意向必须提供以下至少一种有限租约：

- `max_occurrences`：允许浮现的总次数；
- `lease_minutes`：允许执行到的时间边界。

租约用尽后进入 `renewal_required`。`nucleus_manage_autonomy_intent` 允许主体查看、暂停、取消或显式续约；续约只改变执行生命周期，不改写动机、约束和既有历史。旧版本中没有租约的无限周期意向在恢复时自动隔离，等待主体决定。

## 3. 并发与幂等

外部可见动作执行前必须原子认领 occurrence：

1. intent 仍为 `in_flight`；
2. occurrence 身份完全一致；
3. occurrence 尚为 `surfaced`，没有其他动作认领；
4. 目标 stream 与意向授权的 stream 一致。

认领成功后状态变为 `dispatching`。同一工具调用产生的消息 ID 由 occurrence、tool call、目标 stream 和分段序号共同生成；同一因果动作重放得到相同 ID，不同 occurrence 或不同分段不会互相误伤。流上下文还会按精确 message ID 拒绝重复入队。

这里不使用全局文字哈希去重。那种办法既挡不住两分钟后再次触发的旧周期意向，也会错误吞掉主体后来确实想再次说出的同一句话。

## 4. Stream 所有权

一次 `speak` occurrence 只授权它形成时记录的 `target_stream_id`。模型可以在该 stream 内重新判断表达方式，但不能在承接过程中改投其他私聊或群聊。跨 stream 发送会作为技术权限错误返回给意识，不会被解释成主体选择。

如果形成意向时没有可验证目标，系统只把它浮现给 Life Engine 事件流，不会猜测接收者。

## 5. 崩溃恢复与留痕

进程重启后，只要发现 `in_flight` 或仍带有 active occurrence 的记录，就把它标记为 `delivery_unknown` 并转入 `renewal_required`。系统无法证明外部平台是否已经收到消息，因此宁可等待主体确认，也不盲目重发。

当前投影保存在 `data/life_engine_workspace/autonomy_intents.json`；不可变技术轨迹追加到同目录的 `autonomy_intent_events.jsonl`，包含 intent、occurrence、action、状态、目标 stream、原因与时间。投影用于恢复，事件用于审计，不替代主体记忆。

## 6. 运维边界

Elysium 与 NapCat 仍必须由用户手动启动。代码变更不会创建 systemd、计划任务、登录脚本或其他自启动入口，开发代理也不得替用户停止或重启正在运行的实例。

首次手动启动新版本时，恢复过程会把历史无限周期意向以及崩溃遗留的在途 occurrence 隔离为 `renewal_required`。这是预期迁移行为，不会删除原始记录。

## 7. 验收重点

- 同一个 occurrence 不能被并发认领两次；
- 周期意向必须等前一次收到安全终态后才创建下一次；
- 结果未知、发送失败和重启恢复都不能自动重放；
- 私聊 occurrence 不能转发到群聊，反之亦然；
- 同一动作重放保持相同消息 ID，多段消息保持不同 ID；
- 旧无限周期意向会被隔离，显式续约会在既有剩余额度上增加；
- 生命周期事件可追加审计，原始动机和约束不被基础设施改写。
