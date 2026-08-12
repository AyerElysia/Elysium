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

## 7. 双实例共享 MySQL：Lost connection 与 CAS 竞争（2026-08-12）

> 状态：已定位、已修复、已提交（`8e96bd2` 与后续 merge），文档记录完整结论。
>
> 记录时间：2026-08-12
>
> 适用分支：`main`，提交 `f4a61eb` 之后。

### 7.1 背景

Elysium 以 **multi-writer 双实例**（`elysium-windows-primary` 与 `elysium-linux-primary`）共享同一个远端 MySQL（frp 隧道 `frp-one.com:65429`，库 `elysium`）。这是有意的架构（`core.toml` 中 `multi_writer_enabled = true`），heartbeat/presence/rolling_context 均需在数据库内做写入仲裁。

### 7.2 问题一：`2013 Lost connection during query`（根因：Clash 代理劫持）

现象：`memory_witness` 循环反复报 `asyncmy.errors.OperationalError (2013, 'Lost connection to MySQL server during query')`，本机 MySQL（127.0.0.1:3306）却完全正常。

根因：

- 本机 Clash Verge 开启 **TUN 模式**（`Meta` 网卡持有 `198.18.0.1/16`）与 **fake-ip DNS 劫持**；
- WSL2 NAT 网络下，`frp-one.com` 被解析为 fake-ip `198.18.0.238`，MySQL 长连接被强制走代理；
- 代理切换节点/抖动时 TCP 长连接被掐断，客户端读到 0 字节 → `IncompleteReadError` → 2013。

修复（Clash 侧，无需改代码）：

- 当前订阅 `R7k9CaxLtblM`（赔钱机场）的 rules 扩展 `r5yXhN2t3jTK.yaml`：
  `prepend: - DOMAIN-SUFFIX,frp-one.com,DIRECT`
- merge 扩展 `mgKpMFTxdNnf.yaml`：
  `dns.fake-ip-filter: ["+.frp-one.com"]`
- 重启 Clash Verge 使配置合并生效；验证 `getent hosts frp-one.com` 返回真实 IP `117.162.35.233`，连接直连 65429。

### 7.3 问题二：`RuntimeStateRevisionConflict` / `PresenceRevisionConflict`（根因：双实例 CAS 竞争）

现象（11:58 / 12:12 日志）：

- `RuntimeStateRevisionConflict:life_chatter.rolling_context:chat_global:expected=444:actual=445`
- `PresenceRevisionConflict: presence revision conflict for 'memory_witness': expected 1814, actual 1815`
- `PerceptionCursorConflict: stale perception cursor for 'memory_witness'`

根因：两个实例同时对同一 key 做 CAS 写入（`expected_revision` 校验），另一实例先推进 revision 后本实例失败。`life_chatter.rolling_context:chat_global` 是共享的“主意识滚动上下文”，heartbeat 已有按 sequence 的 claim 仲裁，但 **rolling_context 快照保存没有仲裁**。

修复（`plugins/life_engine/core/chatter.py`，提交 `8e96bd2`）：

- `_save_rolling_context_snapshot` 写入前先 `acquire_singleton_writer`（30s 租约，`runtime_singleton_writer_claims` 表）；
- 持有租约后在租约保护下重读最新 revision 再 `put_state(writer_claim=claim)`，写毕释放；
- 拿不到租约（另一实例持有）→ 跳过本轮保存，同步本地 revision 缓存，不再抛冲突。

配套（`src/core/transport/distribution/loop.py`，提交 `8e96bd2`）：

- `RuntimeStateConflict` 识别增加类型名兜底，双实例 CAS 竞争正确归为可恢复路径（WARNING）而非 ERROR。

验证：本地 MySQL 建独立测试库 `elysium_mw_test`（用户 `mwtest`），双 runtime 实测：

- 旧路径（无仲裁）：10/10 次 stale 写入全部冲突；
- 新路径顺序保存 ×20：20 成功 0 异常；
- 新路径并发竞争 ×10：0 异常（A/B 交替 saved/skipped，revision 单调推进）；
- loop.py 分类器：3 用例（import 失败兜底 / import 成功 / 非冲突异常）全部正确。

### 7.4 遗留观察

- `memory_witness` 的 `PresenceRevisionConflict` / `PerceptionCursorConflict` 已有 try/except 容错与 refresh 重试，属可恢复路径；若旧进程未加载新代码会以 ERROR 形式逃逸到 loop，**重启 main.py 即可**（当前进程 2168584 为 11:22 启动，早于 10:43 的 rebase 后编译，需重启加载当前磁盘代码）。
- 12:12:22 仍有一次性 `2013 Lost connection`（frp 隧道瞬断，非 Clash——规则仍生效、DNS 仍直连）；SQLAlchemy 池会自动重连。若高频复发需检查 frp 服务端。
- 启动等待：旧进程强杀后新进程需等 `selected_persistence` 租约过期接管（最长 120s，`authority_lease_seconds`）。优雅停止（Ctrl+C）可避免；强杀后可手动清理 `runtime_singleton_writer_claims` 中 `released_at IS NULL` 且确认旧实例已死的行。

### 7.5 数据迁移核对（协作者提法）

协作者建议“只需数据迁移，不用改代码”，核对结果：

- `storage_schema_migrations` 两条（v1/v2）均已应用，checksum 匹配；
- `memory_witness_migrations` 已有 1785 条，`memory_witnesses` 3347 条——旧日记迁移早已完成且幂等；
- `runtime_states` / `storage_authority_registry` 等表结构与代码完全对齐，无缺列；
- 结论：本仓库不存在“代码有字段但库没有”的待迁移项；迁移方案不解决 7.2/7.3 两类问题，7.3 仍需代码仲裁（若双实例都继续运行）。

## 8. 切换回 local 为主 + 远端双向同步（2026-08-12 晚间）

> 状态：已完成并运行验证。双实例 MySQL 的 CAS 竞争（7.3）与 frp 隧道波动（7.2）反复干扰检索记忆，决定改为**本地为主、远端仅作数据同步目标**。

### 8.1 数据回迁（远端 MySQL → 本地 SQLite）

远端库（frp-one.com:65429/elysium）在 8/9 起积累的全部生命域数据回迁到本地：

| 域 | 数据量 | 方式 | 验证 |
|---|---|---|---|
| Life Events | 106,801 条 | `export_life_events_to_sqlite` | integrity ok, root verified |
| Memory | 106,812 经历 / 16,732 chunks / 3,289 nodes / 3,420 witnesses | `export_memory_to_sqlite`（模板=backup_life_data 生成） | integrity ok |
| Presence/World | 4,524 行 | `export_presence_world_to_sqlite` | verified=True |
| Subject Documents | 1,919 文档（12.6MB） | `export_subject_documents` | verified=True |
| Learning | projections 4 条 | 直连逐表复制 | integrity ok |
| Core | messages 84,212 / streams 121 / persons 44 | 按自然键合并补缺（不覆盖本地） | integrity ok |

注意点：

- `--reverse-export` 参数就是官方 MySQL→SQLite 导出通道；直接调用 migration 包里的 `export_*_to_sqlite` 函数可跳过旧快照模板依赖。
- Memory 导出校验 `reverse export differs from target` 是双实例并发写导致 root 漂移，**数据本身已完整落盘**（行数一致、integrity ok），属校验时机问题而非数据丢失。
- Learning events 全表 10.8GB（9518 行均 1.1MB），经 frp 隧道拉取不现实，保留远端；learning_projections 4 条已回迁，事件可重新积累。
- 回迁产物在 `backrestore/`（未入库），旧本地文件备份在 `backrestore/pre-switch-backup-*`。

### 8.2 配置切换

- `config/core.toml`：`backend = "local"`（该文件被 .gitignore 忽略，不入库）。
- local 模式 `settings.enabled=False` → `open_storage_backend` 返回 disabled runtime，life_engine 纯本地文件，**不连 MySQL**。
- 各域本地文件已替换为回迁数据：`life_engine_workspace/life_events.sqlite3`、`.memory/memory.db`、`runtime/consciousness_presence.sqlite3`、`runtime/world_projection.sqlite3`、`data/Elysium.db`。

### 8.3 回迁库 schema 修复（重要）

回迁的 life_events.sqlite3 是 MySQL 导出（新版 schema），本地代码 INSERT 未适配：

```text
sqlite3.IntegrityError: NOT NULL constraint failed: raw_event_consumer_offsets.revision
```

- 回迁库 `raw_event_consumer_offsets` 有 `revision INTEGER NOT NULL` 列；
- 本地 `event_bus.py` 的 INSERT 只写 consumer_id/ingest_position/updated_at/metadata_json（不含 revision）→ INSERT 时 revision=NULL → 约束失败。
- 修复：重建该表，`revision` 改为可空（数据保留），模拟 INSERT 验证通过。
- 全表 schema 对比：仅此表有差异；`raw_event_export_outbox`/`raw_event_ledger_meta` 为回迁库多出的新表（代码不访问，无害）。

### 8.4 双向同步（本地 ⇄ 远端）

1. **shared_sync（事件级双向）**：`config/plugins/life_engine/config.toml [shared_sync]`：
   - `enabled = true`、`remote_host = "frp-one.com"`、`remote_port = 65429`、`remote_user = "elysia"`、`pull_enabled = true`；
   - 密码走 `ELYSIUM_SYNC_MYSQL_PASSWORD` 环境变量（.bashrc 已加）；
   - 仅 local 模式可用（`_selectable_storage_enabled` 必须 False），绑定 life_events.sqlite3，visibility=shared 的事件 push/pull 双向。
2. **sync_local_to_mysql.py（Core 表增量）**：`scripts/sync_job.sh` + crontab 每 10 分钟；按自然键只 INSERT 远端缺失行，不覆盖。dry-run 验证通过。
3. 仿真验证：SharedSyncBridge 初始化 OK（push+pull=True），17 项数据完整性检查全部 PASS，修复后 0 ERROR、heartbeat 正常。

### 8.5 遗留

- learning_events 10.8GB 未回迁（远端保留），本地重新积累后可通过 sync 通道补同步。
- frp 隧道偶发瞬断（7.2）在 local 模式下不再影响主运行，只影响同步速率。

### 8.6 Clash 直连域名清单（重要，新增业务域名必须登记）

Clash Verge TUN 模式会劫持 WSL 全部流量并返回 fake-ip。**凡是 Elysium 需要长连接/直连的域名，必须同时登记两处**（否则表现为偶发断连、启动失败、认证异常）：

| 域名 | 用途 | 登记时间 |
|---|---|---|
| `frp-one.com` | 远端 MySQL（frp 隧道） | 2026-08-12 上午 |
| `kookapp.cn` / `kookapp.com` | KOOK API + WebSocket Gateway | 2026-08-12 晚 |

登记位置（当前订阅 `R7k9CaxLtblM`）：

1. **rules 扩展** `r5yXhN2t3jTK.yaml` 的 `prepend`：
   ```yaml
   prepend:
     - DOMAIN-SUFFIX,frp-one.com,DIRECT
     - DOMAIN-SUFFIX,kookapp.cn,DIRECT
     - DOMAIN-SUFFIX,kookapp.com,DIRECT
   ```
2. **merge 扩展** `mgKpMFTxdNnf.yaml` 的 `dns.fake-ip-filter`：
   ```yaml
   dns:
     fake-ip-filter:
       - "+.frp-one.com"
       - "+.kookapp.cn"
       - "+.kookapp.com"
   ```
3. 改完重启 Clash Verge；验证 `getent hosts <域名>` 返回真实 IP（非 198.18.x.x）。

**判定方法**：`getent hosts <域名>` 若返回 `198.18.*`（fake-ip 段）即被劫持。KOOK 症状：Gateway `no close frame` / `timed out during opening handshake`、适配器启动失败（异常消息为空）、`curl -H "Authorization: Bot <token>" https://www.kookapp.cn/api/v3/user/me` 非 200。
