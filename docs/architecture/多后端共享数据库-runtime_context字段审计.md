# `runtime_context/global` 字段审计（多后端共享数据库阶段 0）

> 文档状态：阶段 0 交付物（字段清单与迁移目标），基于当前代码静态审计，不代表已切换。
> 规范依据：《Elysium 多后端共享数据库架构设计与实施规范.md》第 8 节与第 17 节阶段 0。
> 审计对象：`plugins/life_engine/service/state_manager.py::StatePersistence.save_runtime_context` 构造的
> `namespace="life_engine.runtime_context", state_key="global"` full snapshot（`version=2`）。

## 1. 审计对象

当前 full snapshot 由三部分组成：

```text
{
  "version": 2,
  "state": { ... 22 个运行态字段 ... },
  "pending_events": [LifeEngineEvent...],
  "event_history": [LifeEngineEvent...],
}
```

保存路径：`LifeEngineService._save_runtime_context` -> `StatePersistence.save_runtime_context` ->
`runtime_store.put_state(ns=life_engine.runtime_context, key=global, expected_revision=本地 revision)`。

加载路径：`load_runtime_context` 读取整条记录并恢复 `LifeEngineState`、pending 与 history。

任意字段变化都会使整份 payload revision 失效，这正是规范第 1 节描述的陈旧快照级联冲突的根源。

## 2. 字段清单与迁移矩阵

| 字段 | 类型 | 权威 owner | 并发方式 | 合并合同 | 迁移目标（规范 8.6） |
| --- | --- | --- | --- | --- | --- |
| `heartbeat_count` | int | heartbeat 提交者 | 单序列 operation | 只增，operation 去重 | `life_engine.heartbeat:<instance_id>` |
| `event_sequence` | int | 事件写入者 | 单序列 | 只增，缺口必须显式失败 | 保留在 global 技术 checkpoint |
| `heartbeat_context_cursor` | int | heartbeat 提交者 | 单序列 | 连续推进，不倒退不越缺口 | `life_engine.heartbeat:<instance_id>` |
| `subconscious_summary` | object | 潜意识压缩器 | 合并投影 | 相同内容去重；覆盖范围取并集 | 保留在 global（可重建投影） |
| `last_model_reply_at` | str | heartbeat 提交者 | 单序列 | 最近一次成功，无合并 | `life_engine.heartbeat:<instance_id>` |
| `last_model_reply` | str | heartbeat 提交者 | 单序列 | 最近一次成功，无合并 | `life_engine.heartbeat:<instance_id>` |
| `last_model_error` | str | heartbeat 提交者 | 单序列 | 失败证据追加 | `life_engine.heartbeat:<instance_id>` |
| `last_wake_context_at` | str | 唤醒执行者 | 最近写入 | 最近一次，无合并 | 保留在 global 技术 checkpoint |
| `last_wake_context_size` | int | 唤醒执行者 | 最近写入 | 最近一次，无合并 | 保留在 global 技术 checkpoint |
| `last_external_message_at` | str | 消息事实层 | 幂等事实 | 同一 occurrence 去重 | 可由 Life Event 派生，global 仅留技术镜像 |
| `self_pause_until` | str | 暂停裁决者 | 最近写入 | 最近一次，无合并 | `life_engine.pause:<instance_id>` |
| `self_pause_started_at` | str | 暂停裁决者 | 最近写入 | 最近一次，无合并 | `life_engine.pause:<instance_id>` |
| `self_pause_reason` | str | 暂停裁决者 | 最近写入 | 最近一次，无合并 | `life_engine.pause:<instance_id>` |
| `self_pause_duration_minutes` | int | 暂停裁决者 | 最近写入 | 最近一次，无合并 | `life_engine.pause:<instance_id>` |
| `self_pause_checkpoint_minutes` | int | 暂停裁决者 | 最近写入 | 最近一次，无合并 | `life_engine.pause:<instance_id>` |
| `consecutive_rest_count` | int | 休息调度 | 原子计数 | 原子增量 | 保留在 global 技术 checkpoint |
| `last_leisure_seen_at` | str | 休息调度 | 最近写入 | 最近一次，无合并 | 保留在 global 技术 checkpoint |
| `chatter_context_cursors` | dict[str,int] | stream turn 提交者 | 按 stream CAS | 独立 stream 各自推进，不越缺口 | `life_engine.chatter_cursor:<stream_id>:<consumer_id>` |
| `chatter_thought_cursors` | dict[str,int] | stream turn 提交者 | 按 stream CAS | 独立 stream 各自推进，不越缺口 | `life_engine.thought_cursor:<stream_id>:<consumer_id>` |
| `last_chatter_think_by_stream` | dict[str,dict] | stream turn 提交者 | 按 stream 最近写入 | 独立 stream 各自更新 | `life_engine.chatter_cursor:<stream_id>:<consumer_id>` |
| `pending_events` | list | 消息事实层 | append-only | 同 occurrence 幂等，异 hash 冲突 | `life_engine.pending:<stream_id>`（或直接由 inbound message 派生） |
| `event_history` | list | Life Event 时间线 | append-only | 同 occurrence 幂等，异 hash 冲突 | Life Event 表（权威），global 仅留投影 |

## 3. 合并规则判定

### 可自动合并（规范 8.5）

- `heartbeat_count`、`event_sequence`：数据库原子增量或 operation 去重后的派生结果；
- `subconscious_summary`：相同条目去重、覆盖范围取并集；
- `chatter_context_cursors` / `chatter_thought_cursors`：不同 stream 各自 CAS，互不影响；
- `pending_events` / `event_history`：不同 identity 的 append-only 并集；同 identity 同 hash 幂等。

### 不能自动合并（规范 8.5）

- 同一 cursor/sequence 存在缺口：必须停止，不得取最大序列掩盖；
- 两个结果声称完成同一 operation 但 digest 不同：进入 conflict，保留双方证据；
- 需要判断主观意义、事实真伪或价值优先级的内容：交主体反思或独立评估者，不由基础设施代判。

## 4. 拆分后的 global checkpoint 边界

迁移后 `global` 仅保留无法按独立业务 identity 拆分的**技术 checkpoint**：

```text
event_sequence
subconscious_summary（可重建投影）
last_wake_context_at / last_wake_context_size
consecutive_rest_count / last_leisure_seen_at
```

不再保存完整 `event_history`、`pending_events` 与所有 stream cursor（规范 8.6）。

## 5. 迁移顺序建议

1. 先落 inbound message / stream turn / heartbeat operation 权威表（存储合同已完成）；
2. 热路径切换时，`pending_events` 与 `event_history` 的写入改为经操作层去重后落权威表；
3. `chatter_context_cursors` / `chatter_thought_cursors` 改为 per-stream 独立表；
4. self-pause 拆为按实例的独立状态；
5. 最后把 `global` 收缩为技术 checkpoint，并解除 singleton claim（维护窗口执行）。

## 6. 验收方法

- 迁移前后 `global` payload 的 revision 与 hash 在维护窗口有记录；
- 拆分后每个新 namespace 的单测覆盖：幂等重放、异 hash 冲突、cursor 缺口拒绝；
- 双实例写入不相交域时双方结果均保留，且不再互相失效 revision；
- 任何一步不满足即停止，禁止"先递归合并，出问题再说"。
