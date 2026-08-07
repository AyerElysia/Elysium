# 不稳定错误交接记录（暂不修复）

> 状态：仅记录，不修改运行逻辑，不把以下问题标记为已解决。
>
> 记录时间：2026-08-07
>
> 适用分支：当前本地 `main`，提交 `897f9ee0` 之后；本文新增/更新内容尚未提交。

本文用于交接给下一位开发者。以下四个问题都保留现状，交接者应先补充观测和最小复现，再决定修复方案。

## 0. 2026-08-07 后续处置记录（linux-primary 实例）

| 问题 | 处置 | 内容 |
|---|---|---|
| #1 收集器 5 秒超时 | 补充观测 | `record_message()` 增加 enqueue/facts/context 三阶段计时，总耗时 ≥4s 时告警并附 message_id/stream_id；未改 EventBus 超时与提交顺序 |
| #2 Session committed | 补充诊断 | `message_collect_failed` 审计与日志现携带 `error_type` 与完整 traceback，可定位异常发生阶段；根因仍待复现确认 |
| #3 墓碑重复 ID | 已修复 | `consume_vector_tombstones()` 外部删除前对 chunk_id 去重，删除成功后仍逐条确认每个原始 tombstone_id；真实验收待运行中观察批次报告 |
| #4 心跳撞冷却 | 已修复 | `_send_followup_request()` 捕获 `LLMModelsCoolingDownError`，按 `retry_after` 等待（受步进预算约束）后真实重发；冷却内二次失败归一化为 TimeoutError，不伪装成功 |

#3/#4 尚未经真实运行验收；#1/#2 仅增加观测能力，根因未定论。

## 1. Life Engine 消息收集器偶发 5 秒超时

### 现象

此前飞书私聊日志中出现：

```text
EventBus | WARNING | 处理器 'life_engine:event_handler:message_collector'
在事件 'on_message_received' 中执行超时，已跳过
```

对应的首条消息已被飞书适配器和消息接收器收到，但没有出现完整的 Life Engine `message_received` 审计记录。随后聊天流完成 WatchDog 注册、恢复滚动上下文，第二条相同内容消息正常进入 Life Engine。

### 当前判断

这是一个典型的执行耗时在统一 5 秒硬截止两侧波动的冷路径/热路径竞态，不是飞书漏收：

- `src/kernel/event/core.py` 中全局默认 `EVENT_HANDLER_TIMEOUT_SECONDS = 5.0`；
- EventBus 使用 `asyncio.wait_for()` 等待异步处理器，超时后记录“已跳过”，并继续后续处理器；
- `plugins/life_engine/service/event_handler.py` 的 `message_collector` 会直接调用 `record_message()`；
- `plugins/life_engine/service/core.py:record_message()` 在同一个回调内依次执行 pending queue 变更、Life Event/聊天事实持久化、World projection catch-up、runtime context 保存，最后才写审计日志。

冷启动、新 stream、数据库连接/锁等待、World backlog 或上下文保存变慢时，整段路径可能超过 5 秒；热路径通常更快，所以无法稳定复现。

### 一致性风险

超时不是原子回滚。`record_message()` 先把事件追加到内存 pending queue，再执行后续持久化和投影。因此超时后可能出现：

- 内存 pending queue 已变化；
- Life Event 或聊天事实已经写入，或只写入部分；
- World projection 尚未追上；
- runtime context 尚未保存；
- 最终 `message_received` 审计记录没有生成。

日志中的 `pending_message_count` 只能证明某个时点的队列数量，不能单独证明该条消息完整落库。

### 暂不修复边界

本记录不调整 EventBus 超时，不拆分接收回调，也不改变 pending/ledger/projection/context 的提交顺序。

### 交接建议

下一位开发者优先补充分阶段耗时与事件身份日志，至少区分：

1. 入队耗时；
2. raw Life Event/聊天事实提交耗时；
3. World catch-up 耗时；
4. runtime context 保存耗时；
5. 回调是否收到 `CancelledError`，以及取消时各阶段的完成状态。

修复方向应优先考虑“最小耐久接收提交 + 受监管后台投影/上下文任务 + message_id 幂等”，而不是只把 5 秒改大。

## 2. MySQL 模式下 Session 已提交后继续执行 SQL

### 现象

2026-08-07 15:15:25，Life Engine 收集出站发送请求时出现：

```text
life_engine 收集消息失败: This session is in 'committed' state; no further SQL can be emitted within this transaction.
```

随后记录了：

```json
{"event":"message_collect_failed","event_name":"on_message_sent"}
```

但飞书平台发送在 15:15:27 成功；同一轮后续出站消息也最终成功。约 15:16:02，`on_message_delivered` 收集器再次出现 5 秒超时。

这说明该错误没有阻断实际飞书发送，但可能丢失“发送请求”事实或使出站收集链部分完成。

### 已确认的代码路径

出站流程位于 `src/core/transport/message_send/message_sender.py`：

1. `_emit_send_event()` 发布 `ON_MESSAGE_SENT`；
2. Life Engine handler 调用 `record_send_requested()`；
3. `record_send_requested()` 构造 `requested` 聊天事实并调用 `LifeEventBus.publish()`；
4. 发布成功后再调用 `catch_up_world_projection()`；
5. 平台发送成功、历史写入完成后，再发布 `ON_MESSAGE_DELIVERED`；
6. Life Engine handler 对 delivered 事件调用 `record_message(direction="sent")`。

当前 selected storage 的通用事务边界位于 `src/kernel/storage/transaction.py`：

- 每次 `runtime.unit_of_work()` 新建一个 `AsyncSession`；
- `__aenter__()` 显式 `session.begin()`；
- 业务操作不应自行 commit；
- `__aexit__()` 在正常退出时 commit，随后 close session；
- MySQL shared writer 在 commit 前通过 `before_commit` 使用同一事务连接执行 authority/generation fence 校验。

`plugins/life_engine/storage/event_adapters.py`、`world_adapters.py`、`learning_adapters.py` 等 selected adapter 的常规写路径均按上述 UoW 使用 Session，当前源码搜索没有发现 Life Engine adapter 在该 UoW 内直接调用 `session.commit()`，也没有发现项目内 SQLAlchemy `after_commit` 监听器。

### 当前判断：已定位现象，根因未最终证明

这条错误明确表示：某个 SQL 发出时，所用 SQLAlchemy Session/事务已经处于 committed 状态。当前不能直接下结论说“全局 Session 被共享”，因为当前 UoW 工厂按调用创建新 Session。

需要保留的候选原因：

1. **运行中的进程与当前源码不一致**：日志来自未重启的旧进程，旧版可能仍存在跨调用 Session、commit 后查询或旧存储路径；
2. **同一 UoW 内存在隐式/间接 commit**：某个未覆盖的存储操作或旧适配器在 `record_send_requested()` 相关调用链中对 Session/transaction commit 后继续执行 SQL；
3. **事务完成与外层取消/并发交错**：`ON_MESSAGE_SENT` 收集、LifeEventBus 锁、World catch-up 和统一 EventBus 5 秒取消相互交错，可能使旧调用在事务提交后仍继续使用其 Session；
4. **事务 fencing 或连接边界问题**：shared-writer commit 前通过 `session.connection()` 获取连接并执行校验；目前代码语义上应仍属于同一事务，但需要真实 MySQL 日志确认没有在其他分支/旧进程中使用已完成事务的连接。

目前没有足够证据把上述某一项定为唯一根因。

### 重要的不确定性

现有 `message_collect_failed` 只记录异常字符串和事件名，没有 traceback、Session identity、UoW identity、transaction state 或阶段名。因此无法仅凭这条日志判断异常发生在：

- Life Event append 的 commit 前/commit 后；
- EventBus publish 返回阶段；
- World projection catch-up；
- 或旧运行时代码的其他路径。

### 暂不修复边界

本记录不改 UoW、不增加 commit 兼容、不调整 shared-writer fence、不改变出站事件语义，也不把发送失败伪装成成功。

### 交接建议

在可复现环境中记录以下无正文诊断字段：

- `process_start_time`、当前代码 commit、插件版本；
- `event_name`、`message_id`、`occurrence_id`；
- `uow_id`、`session_id`、`transaction_id`；
- `uow.state` 在 operation 前、commit 前、commit 后、close 前的值；
- `record_send_requested()` 的阶段标记；
- `LifeEventBus.publish()` 进入/退出时间；
- `catch_up_world_projection()` 进入/退出时间；
- 是否发生 `CancelledError`；
- MySQL connection/thread/process identity。

同时应确认日志对应的 Elysium 进程确实由当前 `main` 启动，并在更改存储代码后完全重启，排除旧进程继续运行。

## 3. MySQL 墓碑批次包含重复向量 ID

### 状态

该问题的去重修复已经回退，当前明确为“暂不修复，仅记录”。不要把历史修复提交或历史测试结果当成当前实现能力。

### 现象

日志示例：

```text
life_engine 记忆索引批次完成: claimed=1 completed=0 failed=1 stale=0
```

历史错误表现为 Chroma/向量后端拒绝包含重复 ID 的删除批次，典型异常为 `DuplicateIDError`。

### 当前代码事实

MySQL schema `plugins/life_engine/storage/memory/schema.py` 中：

- `memory_vector_tombstones` 使用自增 `tombstone_id` 作为主键；
- `chunk_id` 没有唯一约束；
- 同一个旧向量 ID 可以因为多次文档替换/删除产生多条未消费墓碑记录。

当前 `plugins/life_engine/storage/memory/mysql.py:consume_vector_tombstones()`：

1. 按 `tombstone_id` 读取未消费行；
2. 直接把每行的 `chunk_id` 组成列表；
3. 一次调用外部 `collection.delete(ids=[...])`；
4. 外部删除成功后，再按各自 `tombstone_id` 标记 consumed。

因此数据库层允许重复，外部 Chroma 删除接口通常要求一次批次中的 ID 唯一，二者合同不一致。重复目标会使整批外部删除失败，随后墓碑不应被确认，worker 会继续重试，造成 `failed` 增长或批次持续失败。

### 暂不修复边界

不修改 schema，不在消费前去重，不改变墓碑确认语义，不清理现有数据库重复记录，不宣称 MySQL 记忆索引批次稳定。

### 交接建议

下一位开发者修复前应先确认：

- 同一批 `tombstone_id` 与 `chunk_id` 的数量及重复分布；
- Chroma 客户端对重复 ID 的确切行为和版本；
- 外部删除失败时所有原始墓碑是否保持未消费；
- 多 worker/多进程同时消费时是否需要 claim/fence；
- 去重后是否仍逐条确认原始墓碑，避免重复墓碑永久堆积。

## 4. 心跳模型请求超时后进入全候选冷却

### 现象

2026-08-07 15:29:23 至 15:29:26，心跳 `#248` 出现以下链路：

```text
LLM 请求暂时失败: model=ark-code-latest,
request=life_engine_heartbeat, error_type=TimeoutError
LLM 请求重试已耗尽: request=life_engine_heartbeat,
retry_count=1, last_error=TimeoutError
life_engine.audit: heartbeat_model_failed
all candidate models are cooling down: request=life_engine_heartbeat,
retry_after=28.0s, routing_task=core
```

栈显示失败发生在：

```text
_heartbeat_loop
  -> _run_heartbeat_round
  -> _run_heartbeat_model
  -> _send_followup_request
  -> LLMResponse.send
  -> LLMRequest.send
```

### 当前判断

这不是单一“火山模型返回冷却错误”，而是三个阶段连续发生：

1. **Provider/请求阶段首次超时**：`ark-code-latest` 的一次心跳请求在请求层超时；
2. **LLM 路由层记录跨请求冷却**：`src/kernel/llm/policy/failover.py` 对可恢复错误调用 `record_failure()`，默认首轮冷却约 30 秒。日志从 15:29:23 到 15:29:26 还剩约 28 秒，与该机制吻合；
3. **Life Engine 外层恢复立即撞冷却窗口**：`_send_followup_request()` 通过 `retry_with_backoff(max_retries=1, initial_delay=2.0)` 重发。重发时唯一候选模型仍在冷却，`_FailoverSession.next_after_error()/first()` 返回 `LLMModelsCoolingDownError`，因此第二次并未真正访问 Provider，而是直接失败。

### 代码路径事实

- `plugins/life_engine/service/core.py:_run_heartbeat_model()` 的首次心跳请求使用 `retry_with_backoff(... max_retries=0 ...)`，超时后进入 utility fallback；
- 同一函数的工具调用续轮 `_send_followup_request()` 使用 `max_retries=1`，只捕获 `asyncio.TimeoutError`，不会把 `LLMModelsCoolingDownError` 变成有效等待；
- 续轮失败时，当前代码在 `core.py:7122-7124` 将 `TimeoutError` 或 `RetryExhaustedError` 统一转成新的 `TimeoutError`；但 `LLMModelsCoolingDownError` 不属于这两个类型，所以会原样向上传播；
- `src/kernel/llm/policy/failover.py` 的冷却表是进程级、按请求类型和模型维护的；`core` 任务的冷却会影响后续同任务心跳；
- `_run_heartbeat_round()` 的外层总预算为 `heartbeat_timeout_seconds * 2 + 60`，本次异常不是外层总预算超时，而是内部候选冷却状态主动拒绝请求。

### 为什么会表现为不稳定

触发条件需要同时满足：

- 心跳某次请求确实超过单模型请求超时；
- 此时 `core` 任务可用模型数量不足，或者其他候选也已冷却；
- 下一次心跳/续轮发生在冷却窗口内。

如果下一轮发生在约 30 秒冷却结束后，模型会重新被探测；如果有未冷却的备用模型，路由会切换；如果超时未发生，则不会进入该状态。因此不是每次心跳都复现。

### 影响与边界

- 本轮心跳没有模型响应，`heartbeat_model_failed` 被记录；
- 由本轮准备的上下文不会完成模型提交/消费；下一轮仍需按现有事务语义重新处理；
- 该错误本身没有证据表明 Life Event 被删除或游标被错误推进；
- 日志中的 `retry_count=1` 是请求层/续轮重试信息，不等于已经成功切换到第二个可用 Provider；
- 当前没有证据说明火山接口永久不可用，只有一次请求超时与约 30 秒临时冷却。

### 暂不修复边界

本记录不调整模型 timeout、冷却时长、retry 次数、候选模型配置、utility fallback 或心跳总预算，不清除运行中的冷却状态，也不把本轮失败伪装成成功心跳。

### 交接建议

后续排查应保留以下无密钥字段：

- `request_name`、`heartbeat_run_id`、`heartbeat_count`、`routing_task`、`routing_snapshot`；
- 实际尝试的模型序列，以及每个模型的开始/结束/耗时；
- 每个模型被冷却的原因、failure_count、cooldown_until 和剩余秒数；
- 首次请求、工具续轮、utility fallback 分别是否进入；
- `LLMModelsCoolingDownError` 是否发生在请求创建前，避免误判为 Provider 再次超时；
- 心跳上下文的 prepare/commit 游标是否保持不变。

修复候选方向由接手者评估：续轮与首次请求统一 fallback 合同；识别冷却错误并按 `retry_after` 进入受总预算约束的等待；或保证 `core` 至少有一个独立可用的备用模型。不能简单无限增加重试，否则只会在冷却窗口内空转并延长心跳占用。

## 5. 四个问题的共同关系

这些问题可能互相放大，但不能在没有证据时合并成一个根因：

- 事件总线 5 秒超时会取消收集器，并使出站 `on_message_delivered` 也可能被跳过；
- MySQL Session 错误发生在 `on_message_sent`，它与后续飞书发送成功并不矛盾，因为发送前事件收集失败不会阻止 `_send_platform_message()`；
- 记忆索引墓碑批次是后台 worker 的外部向量删除问题，与消息发送事务不是同一条直接调用链；
- 心跳模型冷却错误属于 LLM 请求/路由恢复链，与前三项没有直接共同调用点；
- 四者都涉及“外部/持久化副作用已经部分发生，或恢复状态跨请求保留，但上层日志容易把一次处理理解为原子成功或失败”，后续应分别建立幂等、阶段观测和失败恢复合同。

## 6. 当前验证边界

本轮只读检查完成：

- 当前工作树干净，没有带入上述问题的修复改动；
- 当前分支为 `main`，领先 `origin/main` 6 个提交、落后 1 个提交；
- 没有执行真实 MySQL 写入、没有重启 Elysium、没有进行故障注入；
- 没有把历史“重复墓碑修复”的测试结果当作当前结论；
- 本文是交接调查记录，不是修复方案，也不是稳定性验收结论。
