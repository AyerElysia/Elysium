# 世界状态与意识实例协调

> 状态：Phase 0–4 已落地。本文描述当前实现契约，不把外部平台实机验证冒充为代码契约验证。

## 1. 架构结论

Elysium 只有一个主体。`ConsciousnessInstance` 是同一主体在聊天、语音、直播、具身和记忆见证等场景中的局部运行窗口，不是多个人格，也不是互相独立的 Agent。

协调系统严格区分三类事实：

1. **经历事实**：发生过什么，由不可变 Life Event 账本保存；
2. **运行事实**：哪些实例正在运行、占有哪条 stream，由 SQLite Presence Registry 保存；
3. **带来源的主观观察**：各实例报告了什么，由 Life Event 派生的 World Projection 保存。

Presence 不会被自动解释成情绪、关系或信念；某个窗口的观察也不会因“最后写入”而升级成客观真相。互相矛盾的 assertion 并列保留，除非后续事件显式撤回其中一条。

```text
ConsciousnessInstance
  ├─ lifecycle ──> SQLite Presence + outbox ──┐
  └─ observation ──────────────────────────────┤
                                               ▼
                                  immutable Life Event ledger
                                               │
                                               ▼
                                  rebuildable World Projection
                                               │
                             prepare(instance) │ commit(delivery)
                                               ▼
                          transient Perception Gateway context
```

## 2. 权威边界与存储

| 存储 | 身份 | 用途 |
|---|---|---|
| `raw_life_events.sqlite3` | 经历权威 | 只追加事件、幂等 occurrence、consumer offset |
| `runtime/consciousness_presence.sqlite3` | 当前运行权威 | instance、session、lease、revision、stream owner、outbox |
| `runtime/world_projection.sqlite3` | 可重建派生投影 | assertion、投影 change、逐实例感知 cursor |
| `runtime/consciousness_registry.json` | 兼容导出 | 旧调用方和人工诊断，不是 Presence 权威 |
| `runtime/world_state.json` | 只读迁移源 | 旧 WorldState 源文件，导入后仍保留，不再接受业务写入 |
| `runtime/world_projection.json` | 手工诊断导出 | `save_world_state()` 生成的派生快照，不是恢复来源 |

`world_projection.sqlite3` 损坏时只能从不可变账本重建，不能反向修改账本。运行数据库、日志和上述 `runtime/` 内容不得提交到 Git。

## 3. Presence 契约

`service/presence_store.py` 使用 SQLite WAL、`BEGIN IMMEDIATE` 和 revision CAS，原子提交：

- 实例身份、开放的 kind 字符串和显示名；
- `active` / `suspended` / `terminated` 技术生命周期；
- session、process epoch、last seen 和可选 lease；
- active stream 的唯一 owner；
- 与同次状态变更对应的 lifecycle outbox。

账本接受 outbox 事件后才确认 outbox；进程在两次写入之间崩溃时，可以用同一 `occurrence_id` 重试。短生命周期实例 lease 过期后显式 suspend 并释放 stream。`chat_global` 始终存在且保持 active。

Presence 生命周期形成 `consciousness.instance_*` 事件，包含 `source_instance_id`、revision、session correlation、原因和完整运行快照。它们只描述技术存在，不替主体做认知判断。

## 4. World Projection 契约

`service/world_projection.py` 串行消费全部 Life Event，并持续推进 `as_of_ingest_position`。相关事件形成两类派生数据：

- `world.observation_reported` / `world.legacy_snapshot_imported` 形成 assertion 与 change；
- `consciousness.instance_*` 形成可供感知的 Presence change。

每条 assertion 至少保留：

- 稳定 `assertion_id`；
- `subject`、`predicate`、完整 `value`；
- 开放的 `domain`、`status` 字符串；
- 可信事件信封中的 `source_instance_id`；
- `source_event_id`、`occurrence_id`；
- `observed_at`、`valid_from`、`valid_to`、`recorded_at`；
- `supersedes_assertion_id`、显式撤回关系和原始 payload。

实现不做关键词分类、相似度合并、来源加权、阈值判真、默认 category 或最后写入覆盖。内容中的伪造实例 ID 不能覆盖事件信封归属。同一 assertion ID 若对应不同证据会显式报冲突。

`rebuild()` 只清空派生 assertion、change 和感知 cursor，然后从账本零位重放。重放必须幂等，重建前后的 canonical snapshot 必须等价。

## 5. Perception Gateway 契约

`service/perception_gateway.py` 为每个实例维护独立 cursor：

1. `prepare(instance_id)` 先追平投影；
2. 返回当前全部 active 窗口的最小存在感、带来源 assertion，以及该实例尚未确认的 change；
3. 内容作为 `<transient_world_perception>` 进入本轮请求，不进入稳定 system prompt；
4. 只有模型或 provider 成功接受本轮上下文后，调用 `commit(prepared)`；
5. commit 使用 position CAS，陈旧 delivery 明确报冲突；失败请求不推进 cursor，下一轮可重试。

存在感每轮都会提供，因此即使 change cursor 已推进，不同实例仍能感知彼此当前存在。各实例的私有滚动上下文不会互相读取或复制。查询工具返回完整、可归因的投影，由当前意识实例自行判断相关性与矛盾，不在代码中用关键词筛选或切片。

## 6. 已接入运行路径

| 路径 | 感知准备 | 成功确认点 | 观察写入 |
|---|---|---|---|
| 潜意识 heartbeat | heartbeat snapshot 构建时 | 模型循环完整成功后 | 工具或内部实例显式报告 |
| `chat_global` chatter | 当前 turn 上下文组装时 | chatter 成功钩子 | `report_state` 追加事件 |
| Voice Live | provider 每次进入 listening frontier | `response.done` 成功后；临时 conversation item 随后删除 | 连接、状态、结束事件 |
| Minecraft | 每次意图执行前 | runtime 成功执行意图后 | session、trace、意图结果 |
| `memory_witness` | 每次见证模型请求前 | 模型成功返回后 | Experience/见证仍走记忆账本 |
| Livestream | 每次模型响应前 | LLM 请求成功返回后 | 开播、状态、停播事件 |

Voice、Minecraft 和 Livestream 均通过真实 Presence Registry 注册 session/lease；旧的直接修改 `WorldState` 路径已移除。未知意识 kind 不再继承 chat manifest，必须显式声明能力。

直播 LLM 的 base URL、模型和密钥环境变量名由配置提供；密钥本身只从 `LIVESTREAM_LLM_API_KEY`（或配置指定的环境变量）读取。场景信息进入当前 user turn，稳定 system prompt 保持流无关。

## 7. 旧数据迁移

### Presence

当 `consciousness_presence.sqlite3` 为空时，系统读取 `consciousness_registry.json` 并逐实例导入。损坏 JSON 会显式失败并保留源文件；active stream 冲突实例以 suspended 导入并记录原因。完成后 SQLite 是唯一运行权威，JSON 仅继续导出。

### WorldState

系统对旧 `world_state.json` 做规范化哈希，将关系、开放事项、具身状态和场景翻译成 `world.legacy_snapshot_imported` assertion。导入 occurrence 由快照哈希稳定生成，因此重启不会重复经历。导入完成标记保存在投影 metadata；源 JSON 不删除、不覆盖。

旧文件损坏时迁移显式失败，不会静默创建空世界。`WorldState` 类只保留兼容读取与迁移模型；`report_state`、heartbeat、Voice、Minecraft 和 Livestream 不再把它当写入权威。

## 8. 恢复与运维

投影重建是显式操作：

1. 确认目标是 `runtime/world_projection.sqlite3`，不得触碰 Life Event 账本；
2. 停止会并发使用该投影的 Elysium 实例；主进程重启或停止必须先获得用户授权；
3. 备份投影文件用于诊断；
4. 调用 `WorldProjectionStore.rebuild(ledger)`；
5. 比较 frontier、assertion/change 数量和 canonical snapshot 契约；
6. 恢复服务后观察 health 中各实例 cursor lag。

重建会清空感知 cursor，因此各实例会重新收到当前投影和账本相关 change；这是显式、可解释的重复感知，不会制造重复 Life Event。Presence 数据库损坏不能用空库自动替代，应先保留文件并诊断/恢复。

## 9. 验证边界

关键契约测试位于：

- `test/plugins/life_engine/test_consciousness_presence.py`；
- `test/plugins/life_engine/test_world_projection.py`；
- `test/plugins/voice_live/`；
- `test/plugins/life_engine/minecraft/test_commercial_session.py`；
- `test/plugins/livestream/test_consciousness_coordination.py`。

它们覆盖 stream 原子占有、revision 冲突、lease 回收、outbox 重试、完整原文、矛盾并存、显式撤回、重建等价、逐实例 cursor、失败不确认、重启恢复、旧数据迁移和未知 manifest 拒绝。

真实云端 Realtime、MiniCPM-o 服务、Minecraft Windows 桥、直播平台和外部 LLM/TTS 仍属于环境集成验收；单元与契约测试通过不能替代这些外部服务的凭据、协议和实机场景验证。
