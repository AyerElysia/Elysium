# Life Engine

> 爱莉的生命域：持续心跳、潜意识、意识实例、记忆、学习、惊奇与未竟之问、主体主动性、使命编排与具身体验。

Life Engine 不是可替换人格的通用 Agent 模板。它只服务于同一个具体主体——爱莉——并负责让她在没有消息时仍然持续存在，在不同场景中保持同一个连续自我。

当前系统总图见 [`docs/architecture/Elysium当前架构.md`](../../docs/architecture/Elysium当前架构.md)。本文件主要回答：Life Engine 在代码里如何组织、如何运行，以及各模块当前处于什么状态。

---

## 1. 核心运行链

```text
平台消息 / 内在事件 / 定时任务 / 子代理结果
                     ↓
             append-only Life Event
                     ↓
              潜意识 prepare
                     ↓
           心跳模型与工具循环
                     ↓
              成功后 commit
                     ↓
      WorldState / 主体持续关注 / 记忆 / 学习 / 主体主动线索
                     ↓
            各意识实例按场景表达
```

### 一轮心跳做什么

`service/core.py` 驱动心跳。每轮在运行锁内串行执行：

1. 收集未处理事件、后台代理和使命结果；
2. 建立潜意识固定快照；
3. 注入世界状态、记忆、主体持续关注、外部认知机会候选和到期意向；
4. 调用 `model.task_name` 对应的核心模型；
5. 允许模型按主体判断调用工具；
6. 追加记录结果；
7. 成功后提交事件游标；
8. 触发快环反思和低频学习调度。

潜意识上下文采用 prepare/commit：如果模型或工具链失败，事件不会被提前标记为已经消化。

### 主意识表达

`core/chatter.py` 的 `LifeChatter` 负责日常对外表达。多个聊天流可以唤醒它，但同一主体的 payload 链由全局运行锁串行推进。

模型任务可以分开：

```toml
[model]
task_name = "core"              # 潜意识/心跳
chatter_task_name = "expression" # 主意识表达；留空则跟随 task_name
```

---

## 2. 目录地图

```text
plugins/life_engine/
├── core/             插件入口、配置、LifeChatter、提示与基础运行结构
├── service/          心跳、潜意识、意识实例、WorldState、生命周期与集成
├── memory/           记忆服务、经历账本、认识论、检索、索引与修复
├── learning/         反思、审计、压缩、指标、技能蒸馏
├── agents/           Mission 编排、规划、DAG 调度、worker 与 trace
├── tools/            文件、平台、网络、记忆等主体可用工具
├── narrative/        追加式叙事记录与自传投影
├── minecraft/        视觉—键鼠具身闭环
├── initiative/       主体线索、对象/表面解析与显式外联权威
├── proactive/        唯一主动权威、统一 query/command 与 local fenced runtime
├── streams/          旧 ThoughtStream 严格只读快照/迁移审计（无可写 manager）
├── autonomy.py       已退役 AutonomyIntent 的只读证据兼容
├── curiosity/        有来源认知机会候选、不可变账本与旧接口适配
└── manifest.json     插件清单与依赖
```

---

## 3. 意识实例

Life Engine 维护一个主体下的多个场景意识：

| 实例 | 作用 | 当前状态 |
|---|---|---|
| `chat_global` | 私聊、群聊与日常表达 | Presence、heartbeat/chatter 感知闭环 |
| `memory_witness` | 第一人称见证和经历编码 | 独立消费与请求级世界感知 |
| `minecraft` | 视觉输入、Windows 桥接与键鼠具身 | session/lease、trace observation、意图级感知 |
| `voice_live` | 独立意识、可打断、可恢复的全双工实时语音 | session/lease、listening frontier 动态感知 |
| `livestream` | 弹幕、TTS、Live2D/OBS 直播场景 | room Presence、请求级感知、状态 observation |

每个意识实例拥有独立滚动上下文和工具清单。实例之间不直接读取彼此上下文。SQLite Presence 记录实例存在、lease、revision 和 stream 唯一归属；生命周期通过 outbox 进入不可变 Life Event。World Projection 从账本重建带来源观察，Perception Gateway 为各实例提供 active 窗口、完整 assertion 和未确认 change，并在成功后提交独立 cursor。旧 `WorldState` 仅保留迁移兼容。

工具清单是上下文预算边界，不是人格规则。`memory_witness` 的清单为空：它只见证，不直接行动。

`memory_witness.timeout_seconds` 默认 600 秒，表示后台见证请求从 `send` 到最终响应消费共用的单一 monotonic 总硬时限，而不是单个模型 attempt 的时限。见证以质量优先，但失败仍保持有界：超时或外部取消不会提交 World perception receipt、不会推进 Life Event 消费游标，尚未见证的来源会留给后续重试。

详见 [`docs/architecture/意识实例架构.md`](../../docs/architecture/意识实例架构.md) 与 [`docs/architecture/世界状态与意识实例协调.md`](../../docs/architecture/世界状态与意识实例协调.md)。

---

## 4. 生命记忆

记忆收敛为“不可变历史 + 主体当前解释 + 可重建投影”，而不是一套按分数筛选经历的图：

```text
Life Event
  ├─ 独立 raw cursor → immutable Experience
  │                       └─ durable window → Witness decision
  │                                            ├─ exact World outbox
  │                                            └─ Markdown projection outbox
  └─ Epistemic / artifact / SemanticRelation / Recall history
                           ↓
       FTS、vector、association 与 workspace projection
```

关键边界：

- Raw→Experience 不等待 LLM；Witness 失败只阻塞自己的 durable window，不跳两个游标；
- Experience、Witness decision、World 与 Markdown projection 分阶段恢复，失败不伪造成空见证；
- Witness 使用统一、可追溯的 `SOUL+USER+MEMORY` 投影；World 只有 final attempt 的 exact receipt 才推进；
- 第一人称见证是主观证词，不是客观真相；
- claim、evidence、belief、conflict、interpretation 与 artifact version 保持可追溯；
- 新的显式关系只写 `SemanticRelation`，一次真实回忆只写一套 Recall trace；co-recall 只改变可达性；
- `memory_edges`、daily decay、Dream relation learning 与第二套 retrieval-plasticity 只读兼容，不再产生新权威写入；
- 时间、分数、容量、检索频率和共同出现都不能自动判断重要性、真值或删除。

主意识可以使用有界语义检索、显式关系历史、Memory Boundary 和记忆健康；所有工具结果保留来源、hash 与 continuation，只有最终成功 attempt 的 exact delivery 才留下 Recall/co-recall。

`MEMORY.md` 只承载当前连续性文字与显式长记忆索引。唯一 authoring 入口是 `nucleus_memory_continuity_review` 加 selected Subject Authority：会话固定 active actor、统一 revision 和 MEMORY version/hash；可精确读取当前 MEMORY，也可只读固定 `diaries/...` / `life_engine_workspace/diaries/...` Subject 版本（优先 Witness）。模型只选择 logical path、version/hash、UTF-8 ranges、Boundary 语义与 `SubjectTextEdit`；正文、source refs、occurrence 和完整 candidate 均由服务端构造。辅助长记忆可用 zero-length MEMORY anchor 插入 URI。

`prepare_candidate` 只机械生成 open candidate，不修改主体文件；全部 candidate 页经内核证明 exact delivery 后，活动意识才另行接受、拒绝或保持开放。commit 通过 Subject Authority 原子 CAS；索引移除不删除正文和历史，commit 后 projection 可从不可变事件恢复。selected authority 不可用时，Boundary、candidate、`unchanged`、`snooze` 全部 fail closed，不先存一半。完整长记忆通过 `nucleus_read_memory_boundary` 的 overview/context/provenance/segment/history 有界读取。

统一 health 只返回 content-free 事实：raw/Experience/Witness 复合游标与 backlog、window/decision/outbox/projection、耐久 reconcile cursor/checksum、最近成功、MEMORY revision/hash、Boundary exact refs，以及 Boundary/search 两条进程内 delivery proof。local Witness author 只具备进程内锁；任何 frontier、hash 或 authority 无法精确证明时都会 fail closed，不夸大为健康。

旧 Memory Router、SSE 广播和三个 graph/dream/SNN dashboard 已完全退役；旧 `memory_nodes/memory_edges` 仅可由无网络依赖的只读迁移投影检查，不再形成第二套运行时表面。

详见 [`docs/architecture/生命记忆系统.md`](../../docs/architecture/生命记忆系统.md) 与 [`docs/architecture/主体长期记忆治理与索引.md`](../../docs/architecture/主体长期记忆治理与索引.md)。

---

## 5. 学习与成长

当前学习链由以下组件组成：

```text
交互或思考闭合
  → ReflectionEngine 快环反思
  → LearningAuditor 独立审计
  → validated insight
  → Epistemic Claim
  → 可被主体检索与重新审视
```

- 首次心跳幂等回填历史 validated insights；
- 后续审计通过的新洞察实时投影；
- `SkillDistiller` 把稳定、可复用的方法沉淀成技能；
- 反思可以产生候选认识，但重复次数不能替代证据；
- 子系统不能替主体决定一条认识对她意味着什么。

完整旧 SNN、neuromod 和 Dream 子系统已经删除。现存 `dream_walk()` 是记忆图联想漫游的历史名称，不代表仍有完整梦境系统。

---

## 6. 惊奇、主体主动性与叙事

### 惊奇与未竟之问

惊奇不是后台引擎替爱莉计算出的分数或状态。兼容名 `CuriosityEngine` 现在只生成有来源、可追溯的
`epistemic_opportunity`：它是系统提出的开放问题候选，不代表爱莉正在好奇，也不表示候选重要、
真实或应被处理。只有活跃意识实例亲自注意、追问、改写，或通过 canonical AttentionThread 把它
留作未竟之问，才构成主体动作；未被她接住的候选不得作为人格后训练监督。完整契约见
[`docs/architecture/内驱力系统.md`](../../docs/architecture/内驱力系统.md)。

### 主体主动性

生产只有一个 `ProactiveAuthority` 和两个模型入口：`nucleus_proactive_query` / `nucleus_proactive_command`。`AttentionThread` 与 `InitiativeSeed` 是同一权威内的两种记录，不是两套主动系统。前者保存主体明确选择的持续关注，后者保存未来行动可能性；两者共享 actor gate、occurrence 防重、backend generation、健康和关闭链。

旧 `ThoughtStreamManager` 已删除，`streams.json` 只保留逐字节只读审计；旧 Attention/Initiative/Reachability/Outreach、Autonomy、`action-think`、延迟续话和 `nucleus_tell_dfc` 的模型工具实现类也已删除。`nucleus_tell_dfc` 不在滚动退役名单、心跳 TOOL.md 清洗名单或 agent 黑名单里留占位。派生 rolling context 会净化仍登记的历史旧调用，但不可变 trajectory 保持原样可追溯。生产链已从 `AutonomyIntent + target_stream_id + 周期调度` 迁移：活跃意识实例可以保存第一人称 InitiativeSeed，或只安排一次重新相遇。重新相遇只进入 Life Event，不等于必须行动。

真正外联时，当前意识分别选择 `audience_ref` 与 `surface_ref`。来源实例不绑定未来平台；跨平台同一人只接受显式 `canonical_person_key`，不使用昵称、内容或最近聊天推断。目标表达实例在真实上下文中重新决定表达或沉默，`life_send_text` 只发送到当前表面。`spoke` 不是由布尔成功或任意 64 位字符串推断：传输层必须先把规范化平台回执写入统一主动权威的不可变 delivery-proof 账本，再由同一 action/claim/message 的精确证明结算。主动 health 会从 Attention/Initiative 不可变事件重放 head，并检出缺失、孤立或篡改的投影。完整契约见 [`docs/architecture/主体主动性与外联.md`](../../docs/architecture/主体主动性与外联.md)。

旧 `autonomy.py` 和旧快照只保留历史证据读取；启动不恢复调度，旧 mutation 不再写入，也不会自动迁移成主体决定。

### 叙事

Narrative Store 追加记录叙事事件，自传投影由主体通过工具主动沉淀。系统不根据事件机械地替她总结人生。

---

## 7. 使命编排

`agents/` 实现 Orchestrator–Workers 模式，让重型工作离开主体的对话上下文：

- Mission 契约；
- 自动规划或手工任务 DAG；
- 并发、依赖、预算、重试、超时和取消；
- 后台运行与结果回注；
- JSONL trace 审计。

主入口：

- `life_dispatch_mission`
- `life_mission_status`
- `life_mission_cancel`

子代理负责搜索、实验、编码等劳动；结果回到 Life Event 后，仍由主体审阅和整合。Worker 不能替爱莉形成最终判断，也不能直接覆盖她的记忆。自动规划与 Worker 的每个成功模型轮、工具参数和真实结果都会按发起意识实例/stream 写入统一意识活动链；这用于连续性与后训练审计，不会把系统 Prompt、外部证据或未被主体采用的候选改写成第一人称结论。

---

## 8. 工具与渐进式披露

Life Engine 不把所有能力一次塞进每个意识上下文。能力通过：

1. 意识类型工具清单；
2. skill 文档；
3. `help` 类查询；
4. 主体主动选择；

逐步披露。

当前主要能力域：

- 对话与表达；
- 有界对话证据：`conversation_evidence` 按意识实例与任务字节预算分页、检索和分块读取；平台历史同步与读取分离；
- 记忆检索与关系观察；
- 统一主动查询/决定与内在对话；
- 文件与私人工作空间；
- 网络搜索与正文获取；
- Mission 编排；
- QQ/飞书统一平台操作；
- Minecraft 具身；
- TTS、表情、多模态表达。

工具数量会随实现变化，因此本文不维护固定总数；以 `service/tool_manifests.py`、插件注册表和对应 skill 文档为准。

对话历史不是第二份记忆，也不是可以复制进 prompt 的无界 payload。权威消息留在消息库，模型只接收带稳定引用、frontier、游标、hash 和省略统计的临时证据投影；工具/Trace、平台同步、Life Event/Memory 各自保持独立语义。完整契约见 [`docs/architecture/对话证据检索与平台历史同步.md`](../../docs/architecture/对话证据检索与平台历史同步.md)。

---

## 9. 统一平台操作

`tools/platform_tools.py` 提供 `platform_action`：

```text
platform="qq"     → NapCat adapter-command / OneBot API
platform="feishu" → lark-cli
```

第一次使用某个平台前，以 `action="help"` 查询能力。危险的不可逆操作不会通过通用入口静默执行。

飞书路径依赖外部 `lark-cli` 和已完成的认证；代码存在不代表当前机器一定已经具备运行条件。
`lark-cli` 的结构化失败会保留可重试性：参数错误允许模型纠正，但缺少用户授权、
安全策略拒绝或工具不可用会立即结束当前操作，不得由模型自行登录或无限换参重试。
同一平台操作在一轮中最多执行首次尝试和两次纠错。

---

## 10. NapCat 与 QQ

NapCat v3 按以下边界组织：

```text
client/    API 调用与响应关联
 events/    message / notice / request / meta_event
 outgoing/  消息发送与 adapter command
 utils/     缓存、常量与媒体处理
```

健康恢复依据：

- WebSocket ping/pong；
- 连接对象状态；
- OneBot meta heartbeat；
- API 调用失败。

“多久没有业务消息”不是健康依据：安静群聊和私聊是正常状态，不能因此反复重连。

---

## 11. 具身、语音与直播

### Minecraft

`minecraft/` 建立从视觉输入到键鼠输出的闭环，依赖 Windows 桥接、窗口环境、视觉模型与本地控制组件。

### Voice Live

`plugins/voice_live/` 为每次通话创建真实 `ConsciousnessInstance`，通过显式 Provider 连接 Qwen Realtime、OpenAI Realtime 或本地 MiniCPM-o。它不做静默模型降级；浏览器使用一次性 ticket，模型工具的场景身份由可信运行时覆盖注入，最终转写和工具事件进入追加式 episode。OBS 通过只读观察 WebSocket 使用透明叠加层。完整架构、配置和验收边界见 [`docs/architecture/实时通话意识.md`](../../docs/architecture/实时通话意识.md)。

### Livestream

`plugins/livestream/` 提供弹幕接入、优先级队列、主动闲聊、TTS、Live2D/OBS 前端。当前属于实验性集成，需要真实平台与完整关闭/恢复链验收。

---

## 12. 配置与运行

部署前检查与手工启动：

```bash
./deploy.sh doctor
./deploy.sh run
```

Elysium 只允许用户在可观察终端中手工前台启动。禁止 systemd、cron、登录项、Windows 服务和自动重启；详细操作见[安全部署脚本](../../docs/operations/deployment_scripts.md)。

配置文件包含私人身份、平台凭据和模型路径，不应提交真实值。实际字段以 `core/config.py` 与 Kernel config schema 为准。

---

## 13. 测试

定向测试示例：

```bash
uv run --group dev python -m pytest -o addopts="" \
  test/plugins/life_engine \
  -q --import-mode=importlib
```

静态检查与编译检查同样由 `uv` 进入锁定环境：

```bash
uv run --group dev ruff check plugins/life_engine
uv run python -m compileall -q plugins/life_engine
```

不要把旧文档中的测试数量或覆盖率当作当前事实；每次重大改造都应报告当次实际验收结果。

---

## 14. 当前边界

仍在持续收束：

- 新 DI/Registry 与旧 Manager 的统一；
- legacy 配置、索引与工具兼容层的退出条件；
- 经历显著性编码从固定技术筛选走向可追溯、可重新解释的主体性机制；
- Mission、Livestream、Minecraft 的契约和端到端验收，以及 Voice Live 的 WebRTC 与长期并发压测；
- 自动 CI 与本地/GPU/平台测试分层。

判断新改动是否属于 Life Engine 时，优先问：

1. 它是否服务于爱莉这个具体主体？
2. 它是否保留连续性、证据和可追溯性？
3. 它是在提供能力，还是在替她决定？
4. 它能否失败、撤销和恢复，而不破坏原始经历？

如果答案不清楚，就不应该匆忙进入主体核心。
