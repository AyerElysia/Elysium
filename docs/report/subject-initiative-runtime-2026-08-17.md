# 主体主动性与外联重构报告（2026-08-17）

## 1. 触发背景

现场现象是：同一主体在 KOOK 与小希的对话中形成了未来想继续表达的决定，但旧主动系统把决定绑定到来源聊天流，并通过 `AutonomyIntent`、延迟、周期 occurrence、`target_key` 和最近 SendTargets 驱动后续消息。这既可能阻断跨平台连续性，也可能由基础设施替主体选择对象、平台和行动时机。

用户明确否定“来源 Kook 就只能先绑定 Kook”这类规则，并要求重新构思整个主动系统，而不是继续修补聊天流选择。

## 2. 根因

旧实现混合了六种不同责任：

1. 认知候选；
2. 主体未来连续性；
3. 技术调度；
4. 对象身份；
5. 平台/stream 路由；
6. 最终表达。

`target_stream_id` 和 `target_key` 让来源或最近聊天表面提前成为未来动作授权；周期 scheduler 又让一次主体选择被基础设施续写成多次行为。即使每次到点提示“重新判断”，执行结构仍然预先决定了行动表面和重复机制。继续审计还发现两条同源旁路：`schedule_followup_message` 会把几十秒后的再次表达绑定当前流，`nucleus_tell_dfc` 会按最近流或平台账号直接选择并唤醒表达层。

## 3. 本轮生产改造

### 3.1 新主体权威

新增 `plugins/life_engine/initiative/`：

- `contracts.py`：InitiativeSeed、一次性重新相遇、显式 outreach 与 ReachableSurface 契约；
- `authority.py`：service-owned Runtime Event Store 上的不可变决定账本、active actor gate、revision/CAS、occurrence 幂等和 content-free delivery receipt；
- `projection.py`：主体正文与列表摘要分离，固定 8KiB UTF-8 硬上限、hash/frontier 绑定和可无损续读的稳定 continuation；
- `reachability.py`：对象与物理表面分离，跨平台身份只使用显式 `canonical_person_key`；
- `tools.py`：主体线索、可达事实与显式外联三项工具。

### 3.2 一次性重新相遇

`reencounter` 不创建 scheduler recurrence。独立后台投递循环只扫描已到期且未投递的 open seed，按不可变 ledger 顺序生成一次 Life Event。事件不含行动规则、对象或平台。稳定 event identity 与独立 receipt 支持崩溃后补回执而不重复排队。

### 3.3 当下才选对象与表面

`nucleus_reachability` 提供稳定、有界、非显著性排序的对象/表面事实。私聊账号只有显式 canonical person key 才跨平台归并；没有映射时使用带平台域隔离的不可逆 account ref，即使 QQ/KOOK 原始账号 ID 相同也不合并。同名、内容、来源、当前流和最近活跃均不参与身份判断。

`nucleus_begin_outreach` 要求同一调用中明确提交 audience 与 surface。服务端只接受精确引用，并把主体公开行动意向作为不可变 decision；最终正文由目标表达实例在真实聊天上下文中重新决定。

### 3.4 表达边界

`life_send_text` 的模型 schema 删除 `target_key`，只发送当前表面。旧 Python 直接调用参数仍可解析以避免回放 TypeError，但任何非空 target 都显式拒绝。

chat、heartbeat、voice、livestream、Minecraft 等已声明意识实例共享同一套 initiative 工具，不按场景分裂人格契约。

### 3.5 旧系统只读退役

- 旧 schedule 工具不再注册；
- 旧 manage 工具只允许有界 list；
- service 的旧 mutation/trigger/claim/complete fail closed；
- 启动不恢复旧 scheduler，只移除遗留技术 callback；
- 旧快照、runtime rows 与事件历史不删除、不改写、不自动迁移；
- Conversation Evidence 不再用昵称、前缀或最近流解析主动目标，只接受 exact surface ref 或 exact stream ID 的证据读取。
- `schedule_followup_message`、`nucleus_tell_dfc` 与旧 AutonomyIntent 管理器从 manifest/生产工具池移除；历史直接调用 fail closed，旧 followup synthetic trigger 不进入表达层。

### 3.6 有界投影成本

Initiative authority 只在首次构建时回放已有账本，之后按不可变 event position 增量读取。15 秒一次的 reencounter 扫描不会反复 materialize 全部 seed、delivery 或 outreach 历史，且每次最多投递一个到期 seed，避免积压冲击心跳上下文；排序仍按账本顺序，不引入重要性评分。list 只给 content-free 摘要，完整正文由 read 使用固定 8KiB 分页无损续读，Life Event 也只携带有界第一页。

完全相同的已提交 occurrence 会先于 actor/seed 当前状态检查而幂等返回，以便数据库提交成功但调用响应丢失时完成恢复；这不会放宽新决定，任何新 occurrence 仍需 active actor。普通表面无法解析自己的意识实例时也不能借用 `chat_global`，只有 `core` 心跳的正式内部绑定例外。

## 4. 主体性与后训练边界

可用于未来主动性监督的数据，必须是主体明确提交的 seed/outreach 决定、真实表达或显式沉默，并保留 actor/source/causation/occurrence。外部认知候选、旧自动意向、最近流结果、技术重遇和工具可达列表都不能被标注成“她想要”或人格偏好。

## 5. 自动化验证

本轮新增或更新的合同覆盖：

- active actor、source actor 分离、revision/CAS 与 idempotency；
- seed hold/rewrite/reencounter/release；
- 一次性重遇、Life Event 幂等与 receipt 修复；
- Kook 来源在显式 canonical identity 下选择 QQ surface；
- 同名账号不归并、无最近流排序、群聊独立 place；
- surface/audience 错配 fail closed；
- 全意识实例 manifest 同构；
- `life_send_text` 无 target schema、历史参数 fail closed；
- 旧 snapshot 只读、无 scheduler 恢复、无 mutation；
- exact surface evidence 与模糊 target 退役。

最终自动化结果：

- Initiative / 兼容层专项：`90 passed`；
- Scheduler 与 Narrative 墙钟稳定性联合回归：`72 passed`；
- 最新本地主线上的 Initiative / Chatter / TTS 交叉回归：`197 passed`；
- 最新本地主线上的全仓并行回归：`4734 passed / 21 skipped / 3 warnings`；
- 总覆盖率：`71.15%`；
- Ruff F/E9（全部 Python 改动）、Ruff I（新 Initiative 边界与直接相关测试）、Ruff E9/I（含既有 F841 基线的 Scheduler 测试）、`compileall`、manifest JSON 与 `git diff --check`：通过。

全仓压力回归同时暴露了既有测试对固定 `sleep` 和 WSL/宿主墙钟单调性的错误假设。本轮只把测试改为等待回调、状态转换、租约到期等真实合同，并为 Narrative 测试固定不含业务含义的历史游标；没有修改 Scheduler、Presence 或 Narrative 的生产行为。

## 6. 人工启动门

依据仓库规范，本轮在自动化验证完成后只创建本地提交，不立即推送。必须由用户手动启动 Elysium，并验证：

1. Life Engine、selected storage、initiative authority 与 heartbeat 正常启动；
2. 旧 autonomy scheduler 没有恢复，旧快照字节未变；
3. 一次 seed 创建、rewrite 和 list 正常；
4. 一次 reencounter 只进入 Life Event 一次；
5. 一次显式 audience/surface outreach 唤醒正确表面，目标表达实例可独立选择发送或沉默；
6. 普通聊天、Voice、直播和 Minecraft manifest/启动不回归；
7. 日志无正文泄漏、无重复入队、无 revision/claim 异常。

只有用户确认这组真实启动证据后，才允许普通 push；禁止 force push。
