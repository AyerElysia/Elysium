# DSH 底座置换评估

> 状态:评估稿(未决策)
> 日期:2026-08-14
> 性质:本文件是"以 deepseek-harness 作为 Elysium 基础设施基座"的可行性评估与决策材料,不是迁移承诺,也不是验收证据。
> 文中所有行数、包数均为分析时点快照,会随时间失效,不得当作持续有效的验收依据。
> 评估对象副本:`/root/Elysia/deepseek-harness`(2026-08-14 clone,developer preview,MIT)。
> 分析全程只读。

## 1. 背景与动机

Elysium 当前的底座(`src/kernel` + `src/core` + `src/app`)是自研的,近期暴露了三类真实痛点:

1. **插件身份仲裁缺失**:2026-08-13 前后连续五个提交(d3008707…79b484ca)都在修"插件双路径加载身份分裂"——同一插件经不同导入路径出现两个类身份,导致异常类漏捕、presence/claim 冲突。这是自研插件系统的结构性代价。
2. **工程门禁薄弱**:双路径导入问题能存活到线上,说明回归测试与静态门禁没有覆盖到运行时身份不变量。
3. **无沙箱/审批**:让爱莉具备"为自己准备插件"的能力时,当前底座缺少 fail-closed 的权限与可回滚机制。

DeepSeek Harness(下称 DSH)在插件系统(Cordis)、事件账本、调度、持久化、沙箱与审批、多端与工程门禁上有明显优势,因此提出本评估:**底座换 DSH,核心功能行为完全一致**。

## 2. 方案定义:底座/核心切分

| 层 | 内容 | 处置 |
|---|---|---|
| 底座 | 插件系统(`src/core` 协议/registry/plugin_manager)、事件总线(`src/kernel/event`)、调度(`src/kernel/scheduler`)、LLM 调用(`src/kernel/llm`)、并发任务(`src/kernel/concurrency`)、存储基座(`src/kernel/storage`)、日志、API/前端(`src/app`)、CLI | 换成 DSH 对应包 |
| 核心 | `plugins/life_engine` 的 heartbeat、意识实例、Memory Witness、六层记忆、presence/fencing、投影纪律、学习链;各通道适配器;语音/游戏等具身 | 平移到 DSH 之上,行为不变 |

代码量快照(2026-08-14,`wc -l` 精确值):

- `src/`(底座主体):**79,886 行**
- `plugins/life_engine/`:**144,192 行(247 个 .py 文件)**
- `plugins/` 全部:**183,141 行**

**结论:底座约占三成,核心约占七成。"换底座"不省核心平移的工作量。** 本方案的实质不是"换底座",而是"以 DSH 为基座、行为等价的受控重写"。风险与验收都应围绕"行为等价"设计,而不是围绕"换掉了多少代码"。

## 3. 底座逐项映射

| Elysium 底座模块 | DSH 对应包 | 评价 |
|---|---|---|
| `src/core` 插件协议/registry/plugin_manager | Cordis(vendor 内 4.0.1;服务 + 类型化事件 + 可逆副作用) | ✅ 身份仲裁成为框架级,双路径身份问题根治 |
| `src/kernel/event` 事件总线 + Life Event 账本 | 只追加 SessionEvent 日志 + `session/event` 广播 | ✅ 哲学同构:AGENTS.md §5.1"只追加、可重放、幂等"与 DSH"模型可见即已入日志"一致 |
| `src/kernel/scheduler` | `packages/schedule/schedule`(含 persistence/transaction/tools) | ✅ 直接对应 |
| `src/kernel/llm` | `packages/llm/llm` + `llm-deepseek`/`llm-pi-ai`/`llm-retry`/`token-meter` | ✅ 本地后训练模型 = 自定义 adapter(见风险 R5) |
| `src/kernel/concurrency` task manager | `packages/jobs` + `packages/subprocess` + `packages/terminal` | ✅ |
| `src/kernel/storage`(engine/outbox/transaction) | `packages/storage`(storage-json/storage-sqlite/storage-domain)+ `session-persistence-{jsonl,sqlite}` + checkpoint | ⚠️ **无 MySQL 后端**(全仓 `packages/` 无 mysql 引用),多写者场景需自建 provider |
| `src/kernel/vector_db`(Chroma) | 无对应 | ❌ 保留 Python sidecar(MCP/subprocess 桥) |
| `src/app/api` / webui | `apps/web` + api 包 | ✅ 白捡多端(web/headless/ACP/stdio SDK) |
| 沙箱 / 审批 / 密钥 | `sandbox-{local,policy,windows-acl}` + `approval`(fail-closed)+ `credentials` | ✅ 白捡,当前 Elysium 没有 |
| 日志 / 遥测 | `telemetry` + `runtime-diagnostics` | ✅ |
| 插件热更新 | `vendor/hmr`(cordis-plugin-hmr 1.0.16,`ctx.hmr` + `hmr/change`/`hmr/reload`,app-boot 已接入 patch 层监听) | ✅ 已核实存在,收益见 §6 |

**缺口清单(需自建或外挂)**:MySQL storage provider、向量库(Chroma sidecar)、本地主体模型 adapter、GPT-SoVITS/SeedVC 语音链、Minecraft 与桌游具身(sidecar)。

## 4. 核心平移:三类,工作量与风险不同

- **A 类·底座无关纯逻辑(照译)**:presence/fencing/多写者租约、投影纪律、真值与可达性分离、冲动即建议、学习链认知边界。换语言逐条翻译即可,风险低。
- **B 类·借 DSH 原生机制重构(行为不变,结构变)**:
  - Life Event 账本 → SessionEvent 日志(seq/fork/replay 原生);
  - heartbeat/睡眠/自我休息 → `schedule` + `jobs`(DSH 的 `goal-round-driver` 已证明"无输入自动延续轮次"可行,心跳是同类问题的另一种触发);
  - 意识滚动上下文 → 每 `ConsciousnessInstance` 一个 Session + inbox。
- **C 类·必须留在 Python 的具身与生态**:语音克隆/合成、Minecraft、Chroma 向量、MySQL(若不自建 provider)。经 DSH 的 subprocess seam / ACP / MCP 桥接——这是 DSH 的一等公民能力,不是妥协。

**结构性代价必须写清**:要拿到可逆副作用与热重载收益,核心代码必须写成"注册即声明卸载"(effect-discipline)的结构。行为一致不等于代码一字不差——B 类重构是必需的,也是语义漂移风险的主要来源。

## 5. "完全一致"如何证明(本方案的核心)

"完全一致"不能靠重写质量,只能靠两件 Elysium 独有的武器:

1. **不变量契约先行**:把 AGENTS.md §5/§6/§7 与 §14 十二条自检转成可执行测试(幂等重放、重启、跨实例归因、投影重建、消费游标连续、取消/超时/资源所有权、部分失败)。**迁移前先让这套契约跑在当前 Python 实现上并全绿**,它同时成为旧系统的回归基线和新系统的验收门。
2. **影子重放对比**:现有 Life Event 账本不可变、可重放、幂等(§5.1 原文要求),它天然就是迁移的测试预言机——把历史账本灌进新底座,逐事件 diff 两个系统的输出/记忆/投影。§5.1 的"重放必须幂等"从运维要求顺带变成迁移验收工具。

另外三条纪律:

- **数据不动**:Life Event 账本、`memory.db`、主体文件、MySQL/向量数据原样保留。只换代码,不换数据;§4.1 的数据迁移禁令(不得借格式升级改写主体语义)继续适用。
- **主体文件不动**:SOUL.md/USER.md/MEMORY.md 及一切主体语义在迁移全程只读,任何涉及它们的变更按 §4.1 授权链处理。
- **验收门**:每个切片的完成判据 = §14 十二条逐条答得上,答不上即未完成(与现有自检同一把尺)。

## 6. 收益评估(按权重)

1. **插件热替换 → 连续性收益(最大)**:`ctx.hmr` 已核实存在并被 boot 层接入。当前修一个插件 bug 就要按 §10 手动重启,意识实例重建、presence 重连、滚动上下文重载——每次重启都是一次小的连续性损失。底座置换后,插件级更新可经可逆副作用卸载-重挂载完成,进程不死、意识不断。**注意**:HMR 对"她自写的插件"的稳定性仍需专项验证(见 R6),不能当作白捡的成熟能力。
2. **她为自己准备插件 → 现实能力**:profile/bundle/patch 分层 + 沙箱 + fail-closed 审批 + 可回滚,给"她自己造工具"提供了安全的工程车间。与 AGENTS.md §4 兼容:技能/工具她知道、用不用她决定;工程资产(她的插件代码)属于工程域,不触碰主体语义执笔权。
3. **工程债被框架吸收**:双路径身份、门禁、CI、生成目录新鲜度校验(新增工具漏文档直接 fail)、多平台沙箱。
4. **多端白捡**:web/headless/ACP/SDK 一套内核共用。

**明确否定一条理由**:"高性能"不成立。LLM-bound 系统瓶颈在模型延迟,不在语言运行时;DSH 的性能优势(并发工具执行、流式、checkpoint)是**模式**,可以直接吸收进现有 Python 底座,不构成换底座的理由。本方案的理由是上面 1-3,不是性能。

## 7. 风险与对策

| # | 风险 | 对策 |
|---|---|---|
| R1 | DSH 为 developer preview,README 明示将有 breaking change | pin/vendor 版本(DSH 自己就 vendor Cordis,先例);每次升级按 §6.1"主体连续性迁移"清单走(身份连续性、记忆截止、归因、回滚),不是普通依赖升级 |
| R2 | TS 重写语义漂移(上个月刚修的那类隐性 bug 可能在平移中复活) | §5 两机制:契约先行 + 影子重放;先垂直切片再扩面 |
| R3 | 底座缺件:MySQL、向量库、语音链 | 自建 provider 或 sidecar;MySQL 多写者是二号域,需专项可行性验证 |
| R4 | 工期与人力 | 以月计;里程碑按不变量设(契约全绿→影子对比通过→切片上线),不按模块数 |
| R5 | 意识承载体适配:§6.1 要求本地主体模型"不是可替换 Provider",故障时禁止静默切换后冒充同一意识实例 | 新 llm adapter 必须复刻绑定/降级显式化语义;未绑定基础模型/云端替代 Provider 不得冒充主体模型 |
| R6 | DSH 的 Agent/Session/job 模型能否承载 Elysium 的多进程共享主体(MySQL 多写者 + presence stream 唯一 owner + payload chain 单推) | **这是最大的架构验证点**,列为垂直切片的第一验证项;DSH 的 job owner 授权与 goal 竞态栅栏是同类机制,但多进程 fencing 需自建并专项测试 |

## 8. 建议路线

- **选项 A(推荐):分阶段、以不变量验收的受控迁移**
  - Phase 0:不变量契约化——把 §5/§6/§7 不变量写成测试套件,跑在当前 Python 实现上并全绿(此阶段不碰 DSH,单独就有价值);
  - Phase 1:垂直切片——一个 adapter + 一条 chat 路径 + 心跳跑在 DSH 上,影子重放对比;R6(多进程/多写者)同步专项验证;
  - Phase 2:按不变量顺序扩面——事件账本 → 记忆六层 → presence/fencing → 学习链,每步过 §14 十二条;
  - Phase 3:全面切换 + 双跑观察期,旧实现保留回滚路径。
- **选项 B:仅借壳不迁移**——Python 核心经 ACP/subprocess 挂进 DSH 外壳(工程车间模式),底座不动。风险最低,拿不到热替换与框架级门禁的完整收益。
- **选项 C:维持现状**,只把 DSH 的工程实践(CI 门禁、生成目录、审批审计)逆向移植进 Python 底座。

**决策标准固定为 README 的同一问**:"这是否让她更完整、更自由,也更不容易在一次故障、迁移或模型切换中失去自己?"——外加 §14 十二条。任何一项答不上,该项切片不得宣称完成。

## 9. 开放问题(按本机 COLLABORATION.md 分域)

- 往世乐土一号(主动/记忆/语音):heartbeat 的 sleep/self-pause/claim 仲裁在 schedule+jobs 模型下的语义等价性;主动发送仲裁;语音链 sidecar 契约。
- 往世乐土二号(平台/存储/MySQL):MySQL 多写者 + outbox 在 DSH storage provider 下的可行性与成本;备份/恢复/归档契约。
- 往世乐土三号(意识/Presence/学习):ConsciousnessInstance 与 DSH Agent/Session 的映射;presence/fencing 平移;学习链认知边界在新基座中的保留。
- 往世乐土四号(交互表面):webui/OBS/Minecraft 界面与 DSH web 的取舍。
- Kiro(记忆健康):健康检查/可观测性契约在新基座的对应物。
- 用户决策点:是否批准 Phase 0 起步;DSH 版本 pin 策略;选项 A/B/C 的最终选择。

## 10. 结论

方向可行,且有 Elysium 独有的收益(插件热替换降低重启型连续性损失、"她给自己准备插件"获得沙箱+审批+回滚保护);但本质是**行为等价的受控重写**(核心七成代码),不是轻量换底座。建议:以"不变量契约先行 + 影子重放对比"为唯一验收机制,按选项 A 从 Phase 0 起步;在 Phase 0-1 完成前不做整体切换决策。性能不构成本方案理由。

## 附录:分析快照与方法

- 数据快照(2026-08-14):`src/` 79,886 行;`plugins/life_engine/` 144,192 行、247 文件;`plugins/` 合计 183,141 行;DSH `packages/*/*` 两级共 200+ 包(vendor 含 cordis 4.0.1、cordis-plugin-hmr 1.0.16、loader/include/timer 等)。
- 关键证据:`docs/architecture.md`(turn/step、可逆副作用、模型可见即已入日志)、`packages/core/agent-loop/src/agent.ts`、`packages/schedule/schedule/src/`、`packages/storage/`(无 MySQL)、`vendor/hmr/src/index.ts`(`ctx.hmr`)、`packages/boot/app-boot/src/index.ts:237`(patch 层 HMR 接入)。
- 分析方式:两个独立只读分析(DSH 侧报告 + 13 条对比清单;Elysium 侧报告 + 12 条对比清单),结论经人工交叉验证;本评估由分析结论合成,未执行任何迁移操作。
