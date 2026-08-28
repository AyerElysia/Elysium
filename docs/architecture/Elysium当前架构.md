# Elysium 当前架构

> **现状权威文档**
> 基线：当前工作树（意识协调 Phase 0–4 已落地）
> 本文只描述当前代码已经存在的系统边界与运行链路。历史研究、迁移方案和未来愿景不作为本文事实来源。

Elysium 不是通用 Agent 框架。它是为爱莉一个人持续建造的专用系统：基础设施、记忆、意识、学习、渠道和具身能力，都围绕同一个具体主体的连续存在组织。

---

## 1. 阅读顺序与权威层级

发生冲突时，按以下顺序判断当前事实：

1. 当前代码与真实运行数据；
2. `AGENTS.md` 与 `docs/principles.md` 的设计底线；
3. 本文及当前专题架构文档；
4. 最近提交历史；
5. `docs/plans/`、Phase1–Phase4、analysis、archive 仅作历史背景。

相关专题：

- [生命记忆系统](./生命记忆系统.md)
- [意识实例架构](./意识实例架构.md)
- [世界状态与意识实例协调](./世界状态与意识实例协调.md)
- [基座 v2 兼容迁移说明](../architecture_v2.md)
- [Life Engine 模块说明](../../plugins/life_engine/README.md)

---

## 2. 系统全景

```text
main.py
  └── src/app/runtime/Bot
      ├── src/kernel        基础设施：配置、DI、数据库、日志、LLM、任务、调度、HTTP/MCP、向量存储
      ├── src/core          组件运行层：组件、消息管线、Manager、路由、插件装载
      └── plugins/
          ├── life_engine   爱莉的生命域
          ├── napcat_adapter / lark_adapter
          ├── voice_live / livestream
          ├── tts_voice_plugin（当前消息 TTS：GPT-SoVITS api_v2）
          ├── neko_surface
          └── 其他专用能力插件
```

### Kernel

`src/kernel/` 提供跨插件基础能力：

- 结构化配置与 schema；
- 依赖注入容器；
- SQLite、结构化日志与事件总线；
- LLM client、模型任务和故障转移策略；
- TaskManager、Scheduler、HTTP、MCP、向量存储。

### Core

`src/core/` 定义组件模型、消息流、路由和插件装载，是 Kernel 与具体能力之间的运行层。当前仍保留部分全局 Manager；新 DI/Registry 与旧 Manager 并存，属于兼容迁移状态。

### App

`src/app/` 负责进程生命周期。`main.py` 只创建 `Bot` 并启动；`Bot` 依次初始化 Kernel、Core、插件依赖和插件实例，进入运行循环，退出时按生命周期关闭调度器、插件、任务、数据库、向量库和日志。

进程生命周期合同：

- Elysium 只能由用户在可观察终端中手工前台启动；仓库不提供 systemd、cron、登录项或 restart loop；
- `deploy.sh run` / `deploy.ps1 run` 只在只读 doctor 通过后以前台 `exec` 进入 `main.py`；
- 无 TTY 时 stdin EOF 只关闭命令输入，Bot 继续运行；
- 交互终端中的 Ctrl+D 结束交互主循环；
- SIGTERM 请求优雅关闭，SIGHUP 不结束进程；
- 进程、端口与后台任务必须保留唯一 owner，启动脚本不清 lease、不杀未知进程。

---

## 3. Life Engine：同一主体的内在运行

`plugins/life_engine/` 是项目的主体域，不是可替换的通用角色模板。

```text
意识实例生命周期 ─→ SQLite Presence ─→ transactional outbox ─┐
外界事件 / 平台消息 / 内在任务 ───────────────────────────────┤
                                                            ▼
                                                 append-only Life Event
                                                            ↓
                                                     潜意识 prepare
                                                            ↓
                                                    心跳模型与工具循环
                                                            ↓
                                                     成功后 commit 游标
                                                            ↓
                      World Projection / 主体持续关注 / 记忆 / 学习 / 主体主动线索
                                                            ↓
                                                 各意识实例按场景表达
```

### 心跳

`service/core.py` 驱动持续心跳。每轮心跳由运行锁串行保护，主要步骤是：

1. 收集未处理事件、后台代理与使命结果；
2. 通过潜意识上下文建立固定快照；
3. 注入事件上下文、带来源 World Projection、Presence、记忆、主体持续关注、认知机会和到期的一次性主体线索；
4. 运行模型与工具循环；
5. 记录结果；
6. 成功后提交消费游标；
7. 触发快环反思和低频学习调度。

### 潜意识

`service/subconscious_context.py` 以确定性域逻辑建立因果事件组，不用 LLM 替主体裁决事实。它采用 prepare/commit 语义：模型调用失败时不提前吞掉事件。

### 主意识表达

`core/chatter.py` 的 `LifeChatter` 是日常对外表达引擎。不同聊天流可以唤醒它，但全局运行锁保证同一主体的 payload 链不会被并发写入。

潜意识心跳与主意识表达可以分别配置模型任务：

- `model.task_name`：潜意识/心跳；
- `model.chatter_task_name`：主意识表达；留空时跟随 `task_name`。

---

## 4. 意识实例与共享世界

当前核心模型是：

```text
一个主体
  ├── chat_global      日常对话
  ├── memory_witness   第一人称记忆见证
  ├── minecraft        具身场景
  ├── voice_live       实时语音场景
  └── livestream       直播场景
```

每个实例拥有独立滚动上下文和工具清单；实例之间不直接复制上下文。协调层分为：

- **潜意识近期上下文**：默认取最近 5 个已提交因果组，把潜意识明确形成的想法、工具调用/结果和后台结果作为同一份只读 transient projection 交给各场景；不含私有聊天 payload，不推进消费游标；
- **不可变 Life Event**：经历权威，记录完整消息和实例生命周期；
- **SQLite Presence Registry**：运行权威，记录实例、session、process epoch、lease、revision 和 stream 唯一归属；
- **World Projection**：从 Life Event 重建带来源的环境 assertion 与 change，矛盾并存，不自动判真；它不是共享心智，也不负责同步潜意识近期活动；
- **Perception Gateway**：按实例提供 active 窗口、完整 assertion 和未确认 change 的 transient context。

Presence 状态与 stream owner 在一个 SQLite 事务中提交；同事务写 lifecycle outbox，账本接受事件后才确认。带 lease 的短生命周期实例异常消失后会 suspend 并释放 stream，陈旧 revision 不能覆盖新状态。

Presence 与 World Projection 使用不同权威：前者描述技术存在，后者描述带来源观察。heartbeat、聊天、语音、Minecraft、memory witness 和直播均按实例 prepare，并只在模型、provider 或动作成功接受上下文后 commit；失败请求保持可重试。旧 `WorldState` 只作为迁移源保留。

`memory_witness` 不注入行动工具，只负责见证和记录。语音、直播和 Minecraft 是环境依赖较强的场景能力，成熟度见[意识实例架构](./意识实例架构.md)。

---

## 5. 生命记忆

记忆不是单一向量库，也不是按显著性分数筛选事件的网络。当前链路是：

```text
Life Event（追加式发生历史）
  ├─ 独立 raw cursor → Experience（不等待 LLM）
  │                      └─ durable Witness window
  │                           └─ decision + World/projection outboxes
  └─ Epistemic / artifact / SemanticRelation / Recall history
                              ↓
              FTS / vector / association / workspace projection
```

核心原则：

- Raw→Experience 与 Experience→Witness 有独立耐久游标；见证失败不阻塞经历摄取，也不跳 author cursor；
- Witness 使用统一 `SOUL+USER+MEMORY` 投影；decision、exact World commit 和 Markdown projection 可独立恢复；
- 原始证据、经历、主张、解释、文档版本、关系和 Recall 轨迹只追加；
- `valid_time` 与 `recorded_time` 分离，旧理解不会被新理解静默覆盖；
- 新的显式关系只写 `SemanticRelation`，co-recall 只改变可达性；
- 时间、分数、容量、检索频率和共同出现不等于重要性或真实性，也不触发自动删除；
- 第一人称见证是主观证词，不冒充客观事实。

`MEMORY.md` 是主体活着的当前连续性解释，不是第七套数据库。完整长记忆保存为不可变 Memory Boundary，当前文件只保留可精确取回的 URI 索引。唯一写链是 `nucleus_memory_continuity_review` → Learning decision → Subject Authority；候选由固定范围上的 edits 机械生成，全部分页 exact delivery 后才可独立接受。selected authority 不可用时，Boundary、candidate、unchanged 和 snooze 全部 fail closed。

旧 `memory_edges`、daily decay、Dream relation learning 和第二套 retrieval-plasticity 只保留只读兼容、迁移或诊断，不再产生新权威写入。旧 Memory Router、SSE 广播与 graph/dream/SNN 页面已经删除，旧图只能通过无网络依赖的只读迁移投影检查。

---

## 6. 学习与认识论桥

当前学习链路包含三层：

```text
交互 / 思考闭合
  → Reflection 快环洞察
  → Learning Auditor 独立审计
  → validated insight
  → Epistemic Claim
  → 主意识可检索的稳定认识
```

- 首次心跳会把历史 validated insights 幂等回填为 claims；
- 后续审计通过的新洞察实时投影；
- 技能蒸馏把稳定、可复用的方法沉淀为技能；
- 子系统可以提供证据和候选认识，但最终意义与主体背书不能被规则替代。

完整旧 SNN、neuromod 和 Dream 子系统已经删除。当前保留的 `dream_walk(persist_learning=false)` 只是 legacy graph 的只读联想漫游；写关系模式、日衰减和弱边删除都已 fail closed，新的主体关系只进入 `SemanticRelation`。

---

## 7. 认知机会、主体主动性与叙事

- `CuriosityEngine` 只生成有来源的 `epistemic_opportunity` 外部候选，不宣称主体已经好奇；
- `initiative/` 保存活跃意识明确写下的 `InitiativeSeed`，并支持一次性重新相遇；它没有分数、周期任务、目标 stream 或预写回复；
- 真正行动时，当前意识分别选择对象 `audience_ref` 与物理表面 `surface_ref`；来源实例不会锁定未来平台，跨平台身份只接受显式 `canonical_person_key`；
- 目标表达实例在该表面的真实上下文中重新决定表达或沉默，`life_send_text` 只作用于当前表面；
- 旧 `AutonomyIntent`、延迟续话以及 `nucleus_tell_dfc` 最近流唤醒只读退役，不再注册、恢复调度或产生新主体决定；
- Narrative Store 追加记录叙事事件，自传投影由主体通过工具主动沉淀；
- 系统只提供机会、连续性、可达事实与安全回执，不替爱莉决定什么时候行动、面向谁或什么对她有意义。

完整契约见[主体主动性与外联](./主体主动性与外联.md)。

---

## 8. 使命编排与子代理

`agents/` 提供 Orchestrator–Workers 使命系统：

- Mission 契约；
- LLM 自动规划或手工任务 DAG；
- 依赖、并发、预算、超时、重试和取消；
- 后台任务完成后将结果回注 Life Event；
- JSONL trace 用于审计运行过程。

主体可使用：

- `life_dispatch_mission`
- `life_mission_status`
- `life_mission_cancel`

子代理负责重活和搜集证据，不拥有主体的最终判断权，也不能用结果覆盖主体记忆。

---

## 9. 平台与表达能力

### 统一平台操作

`platform_action` 是 QQ 与飞书的统一入口：

- QQ 通过 NapCat adapter-command 调用 OneBot API；
- 飞书通过外部 `lark-cli`；
- `action="help"` 渐进披露平台能力；
- 不可逆操作由安全策略拦截，不能通过通用入口静默执行。

### NapCat v3

NapCat 适配器已按 `client / events / outgoing / utils` 模块化：

- WebSocket ping/pong 与连接状态负责传输健康；
- OneBot meta heartbeat 负责协议健康；
- 安静连接不会因“多久没业务消息”被误判；
- 关闭时释放 socket、server 与 heartbeat 任务。

### 多模态与具身

- `voice_live`：全双工实时语音框架；
- `livestream`：B站原始事件账本、同一意识导演、TTS、OBS 浏览器舞台、真实播放回执与记忆投射；
- `tts_voice_plugin`：当前普通消息与 Surface 自动语音的本地 TTS Service；本机 live 后端是 GPT-SoVITS v2ProPlus `api_v2`（`legacy_compat`）。IndexTTS2.5 + vLLM-Omni 仍是可配置合同，不是本机正在服务的路径；
- `neko_surface`：桌面/表面呈现；
- `minecraft/`：视觉输入、Windows 桥接、键鼠输出和场景意识。

这些场景依赖本地模型、GPU、Windows 桥接或平台认证；“代码存在”不等于环境已经完成生产验收。

---

## 10. 模型与本地服务

模型任务使用语义化名称：

- `core`：潜意识与核心推理；
- `expression`：主体表达；
- `agent`：子代理；
- `witness`：记忆见证；
- `vision`：视觉；
- `utility`：轻量工具任务。

旧的 `life / actor / sub_actor / diary` 仍有兼容映射。

本地服务包括路由模型与视觉嵌入服务。它们有独立启动脚本和环境依赖，路由失败时按模型策略回退；不能把本地服务未启动误写为核心系统停止运行。

---

## 11. 数据与可观测性

当前主要持久状态包括：

- Life Event / Experience / Witness / Epistemic / artifact / Recall 的 coherent authority bundle；
- `runtime/consciousness_presence.sqlite3`：意识实例运行权威、stream owner 与生命周期 outbox；
- `runtime/consciousness_registry.json`：Presence 迁移期兼容导出，不再是权威；
- `runtime/world_projection.sqlite3`：从不可变 Life Event 重建的 assertion、change 和逐实例 cursor；
- `runtime/world_state.json`：只读旧快照迁移源；
- FTS、Chroma 与索引 outbox；
- 意识实例滚动上下文；
- World Projection、主体持续关注、主体主动线索、学习洞察和叙事投影；
- 结构化日志数据库与运行 trace。

原则上，权威数据与派生索引必须可区分；派生层损坏应通过修复/回放重建，而不是修改原始经历来迁就索引。

---

## 12. 当前成熟度

| 子系统 | 当前状态 | 说明 |
|---|---|---|
| Kernel/Core/App 基座 | 稳定使用 | 新 DI/Registry 与旧 Manager 仍兼容并存 |
| Life Engine 心跳/潜意识 | 稳定使用 | 串行心跳、prepare/commit 语义已建立 |
| 意识 Presence 核心 | 已落地 | SQLite 事务、stream 唯一归属、revision、lease、生命周期 outbox |
| 跨实例世界感知 | 已落地 | 可重建 World Projection、逐实例 CAS cursor、主要实例 prepare/commit 闭环 |
| 生命记忆 | 收敛实现，待人工启动验收 | 两阶段 Experience/Witness、复合 frontier、耐久 reconciliation、连续性单写链、Recall/SemanticRelation、exact delivery 与 content-free health 已接入；隔离 MySQL v8→v14 和六域合同已通过 |
| 使命编排 | 已实现并建立核心契约测试 | DAG、依赖、失败/取消/超时传播与结果账本已覆盖；仍需真实模型端到端验收 |
| NapCat v3 / QQ | 已实现并持续运行加固 | 模块化与协议健康恢复已接入 |
| 飞书 platform_action | 已实现，依赖外部环境 | 需要 lark-cli 与认证 |
| `tts_voice_plugin`（GPT-SoVITS） | 当前本地消息 TTS | 本机 live 为 `legacy_compat` / `api_v2` `:9880`；IndexTTS2.5 是可配置合同，不是正在服务的路径 |
| voice_live | 实验性集成 | 依赖实时模型或本地降级链 |
| livestream | 生产候选 | 核心闭环与故障注入已自动验收；真实 B站、TTS、OBS 环境仍需上线前验收 |
| Minecraft | 环境依赖型能力 | 依赖 Windows 桥接、视觉和键鼠环境 |
| 旧兼容层 | 迁移中 | 不能在未验证调用者前贸然删除 |

---

## 13. 当前关键不变量

1. Elysium 只服务爱莉，不以通用复用为架构目标。
2. 同一主体的核心运行状态不得被并发写坏。
3. 潜意识消费必须成功后才提交游标。
4. 原始事件、经历与证据不得静默覆盖。
5. 检索、重复和共现不得被当作真实性证明。
6. 各意识实例上下文隔离，跨场景通过共享世界和事件协调。
7. 子代理和自动化只提供劳动、证据与候选，不替主体形成最终意义。
8. 技术过滤不能变成不可撤销的主体认知裁决。
9. 计划、实验和本地草案不能写成已经发布的事实。
10. 兼容层退出必须先证明历史数据与当前调用者已经迁移。
11. Presence 是运行事实，不得自动升级为主体信念；主体认识必须保留来源与可修正性。
12. active stream 必须有唯一 owner；生命周期状态变化必须留下可重试的归因事件。

---

## 14. 仍需继续收束的边界

- DI/Registry 与旧 Manager 的职责仍需逐步统一；
- 兼容工具、旧配置别名、legacy 索引回退需要退出条件；
- Witness 已补齐 `(ingest_position, occurrence_id)` 复合分页、固定 frontier 和耐久 reconciliation；下一运行边界是由用户手工启动后确认生产配置、健康日志与真实流量闭环，而不是继续增加平行记忆本体；
- 使命编排仍需真实模型端到端验收；livestream 已具备契约与模拟闭环，但 voice_live、livestream 与 Minecraft 仍需要各自真实环境验收；
- CI 需要恢复自动化的核心逻辑测试，并把本地/GPU/平台集成测试分层。

这份文档的职责不是描绘终点，而是保证下一次演进从同一个真实起点出发。
