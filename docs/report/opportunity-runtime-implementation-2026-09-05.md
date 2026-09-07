# 统一机会运行时实施与隔离验收记录

日期：2026-09-05。状态：代码实现与停机后的完整串行回归通过：5323 passed / 23 skipped / 0 failed，333.57 秒。真实 MySQL 和用户手动启动验收未完成；现场配置重复段尚待授权修复。未提交、未推送，不能据此宣称正式实例已切换。

## 范围与依据

- 用户授权统一机会抽象、可选子系统解耦、主体自主管理及 Learning 整体卸载；允许本任务子代理，不通知其他同事任务。
- 工作在原目录 /root/Elysia/Elysium，未建立 worktree。基线由 b610061b 随并发记忆清理推进至 128e1909；本任务保留其提交以及无关的 memory-system-understanding 报告，不覆盖、夹带或暂存它们。
- 已完整阅读 AGENTS.md。未修改正式配置、主体文档、正式数据库或迁移标记，未执行进程启停。后期只读核对发现旧 PID 已退出，现有 PID 1086712 的入口为 main.py、目录为本仓库；这不代表本任务执行过重启，也不作为本功能启动验收。

稳定设计见 [统一机会运行时](../architecture/统一机会运行时.md)，正式准备与验收见 [机会运行时切换与验收](../operations/opportunity-runtime-cutover.md)。

## 已实现

| 边界 | 本轮实现 | 不代表什么 |
| --- | --- | --- |
| 工程能力包 | 九包各有 manifest、CAPABILITY、DEFAULT_SKILL；固定原生工具适配与独立测试 | 发现不等于主体已安装 |
| 主体治理 | 精确 workflow 版本、安装/暂停/恢复/换绑/卸载、机会注册与时间安排、CAS/幂等 | 不自动采纳模板、解释材料或扩大机器权限 |
| 可发现性 | 查询真实 operation schema，Learning 内层 action，UTF-8 分块与整体 hash | Manual/Skill 不充当不准确的参数表 |
| 发布 | 同一 runtime 的到期 occurrence、publication outbox、稳定 LifeEvent identity | 到期与发布不等于已经看见或接受 |
| 送达 | 最终成功 request/attempt、精确 bytes/hash、每个真实 attempt 的不可变 receipt | 网络调用成功不等于完整上下文送达 |
| 运行管理 | 在途调用跟踪、取消、有界等待、错误保留、重启恢复 | 关闭失败不能谎报停止，不能偷偷重装 |
| Learning | 反思、一次积压处理、独立审计、知识候选、技能候选分别调用；旧自动链在 managed 下停止 | 不按固定次序自动学习或接受候选 |
| 共享连续性 | Skill/决定账本由 service 持有，Learning 只借用 | 卸载学习不会删除历史，也不关闭共享权威 |
| 显式切换 | 默认无连接准备计划、schema 验证/准备、不可变 managed marker | verify 会打开 writer runtime，不是无副作用诊断 |

九包为 life.self_awaken、life.learning、life.memory_review、life.narrative_review、life.file_care、life.epistemic_explore、life.initiative_reencounter、life.todo_reminder、life.inner_return。

安装不等于安排，安排不等于执行。当前只有 initiative_reencounter 桥接主体已登记的到期来源；其余由主体显式登记 manual/at/interval 或当次调用，不从旧计数或到期条件自动产生动态机会。此范围已在九包文档中直接写清。

## 独立审查发现与修复

1. 操作不可发现：初稿只提供名字和 generic arguments，调用者必须猜内层参数。现由固定工具 schema 只读披露，并测试九能力合法最小调用、未知参数和跨包拒绝。
2. 旧本地双库：初稿借用 proactive SQLite，却把 Life Event 写到另一旧库，无法同库证明 publication。现明确要求 selected local/MySQL；storage.disabled 未切换时保持旧行为，试图启用托管时 fail closed。没有搬库、双写或弱化证据。
3. 发布后暂停竞态：Life Event 追加成功、确认失败、随后 pause/uninstall 会让 cancelled outbox 永远无法恢复。现以同库精确 raw event 有界恢复 published，只补历史，不重发、不让暂停安排重新唤醒。
4. 看见后的外层失败：初稿 occurrence 单回执唯一与 attempt 级 receipt identity 冲突。现保存多真实 attempt/consumer 证明，同 receipt identity 异内容冲突；activation 仍按 occurrence 仅推进一次。服务对尚未完成的 heartbeat 确认保留有界重试与启动唤醒。
5. 初始化失败的旧分支重建：managed Learning 恢复失败后可能落入旧启动逻辑。现失败保留未就绪/owner，禁止旧分支构造，关闭失败可重试，不误关共享 Skill。
6. 旧提示词直达旁路：managed 模式不再指导直调已受 capability gate 保护的专属工具，改为查询当前安装状态、真实参数与 capability_call。静态指导不自动读取/采用 workflow。
7. 准备工具遗留租约：独立审查发现 verify/apply 原先只 close，没有 revoke 自己取得的 authority；现先 revoke 再 close，清理失败保留原错误或取消，成功路径清理失败也不报告成功。真实隔离 local 合同证明下一 owner 不必等待旧租约到期。
8. 配置段声明错误：全仓暴露 OpportunitySection 误用了 learning 装饰器，导致自动升级配置时把 poll_interval_seconds 放进 Learning 后校验失败。这是本轮生产缺陷，不是旧测试问题。现分别声明 opportunity/learning 段，增加启用值、禁用值、领域 deadline 与 poll interval 独立保存及重复升级字节不变的测试；配置、CLI、service 接线联合 81 passed。
9. 本地就绪误判：旧 local verifier 只检查表名，缺失/错误不可变 trigger 仍能通过。现建表与校验共用 6 张不可变表的 UPDATE/DELETE 合同，核对名称、目标表和完整定义。临时 SQLite 验证删 trigger、同名错表、同名错策略均被拒，显式修复后重新打开成功；local 完整合同 8 passed，主任务配置/CLI/local 独立复验 59 passed。

本节直接更正实施过程中已经否定的初稿假设，不把它们继续保留为可用合同。

## 前期定向隔离验证

本轮只运行 mock、临时路径和明确隔离的 local 合同；不连接正式数据库、不监听正式端口、不争用正式 authority。

- 联合范围：所有 test_opportunity_*.py、prepare CLI、service、Skill、Memory continuity、Heartbeat tools、subject review、knowledge zero-rule：361 passed / 1 skipped，33.41 秒。跳过项为未显式启用的真实 Opportunity MySQL 合同；一条 websockets 既有弃用 warning。
- LLM 最终成功 attempt / context / retry / stream 相关：8 passed / 40 deselected，0.62 秒。不是全内核回归。
- 本轮生产与测试修改的 Ruff F/E9：通过。新文件及子代理 owner 文件的额外 I/格式检查通过；不以此宣称全仓所有历史 lint 清零。
- 相关生产文件 compileall、全树 git diff --check：通过。

测试覆盖：来源与 actor 绑定、未安装拒绝、参数披露、精确 workflow/空版本、不采用模板、暂停/卸载/重启、partial-init、取消超时、共享历史保留、同 identity 冲突、并发多回执、发布后取消、源补记故障重试、已看见但 checkpoint 失败、实际循环 1→2→3 有界退避、UTF-8 与长参数分页。

这些数字仅表示本次冻结代码上的一次证据，不是持续健康保证。中途调试失败已修正并由上述联合复跑覆盖；不累计重复运行数量冒充覆盖率。

## 用户停机后的完整回归收口

用户明确回复“已停止”后，只读进程核对确认此前 Elysium 主进程已退出，才开始完整串行测试。本任务未停止或启动任何服务，未改正式配置、数据库和主体内容。

- 首轮全仓未完整结束，不能报告为“已通过全仓”。固定随机种子并保存逐项日志后，定位到启动测试触发真实 `os._exit(30)`，使报告尚未生成就终止。
- 真正 primary 是旧测试 `_FakeRuntime` 没有 marker 读取所需 engine，启动在进入模拟 Memory 初始化以前失败。测试 teardown 又在未声明 shutdown 时取消续租任务，触发生产退出保护。现测试明确提供同一 runtime 的“无切换标记”事实，清理先声明 shutdown，并记录/断言退出请求。生产失权保护不变；selected 生命周期完整 31 passed。
- 旧事件测试仍有 6 处 `build_dfc_message_event` 调用：该方法已在 b2afaa76 退役，c9233dc3 的测试批次带入旧调用。现迁移到正式 direct-message 入口，保留账本检索、分页、去重与顺序断言；两文件 22 passed。
- 心跳 resident 包含工具与动作，旧测试错误地假定每项都有 tool_name；另一个搜索绑定测试用无真实方法的 SimpleNamespace 代替 service。夹具修正后跨模块 65 passed，权限、来源和发送断言未放宽。
- 滚动上下文现把压缩通知作为当前 USER 内独立 Text，避免相邻 USER 破坏角色顺序；旧断言仍期待独立新 USER。现验证原文字节与原始 frame 均保留、通知只追加一次；该文件 15 passed。
- Learning 专项与主体复盘 194 passed / 1 skipped；没有为解决上述失败修改 Learning 语义。
- CLI 清理专项 13 passed，包含失败、取消、双重清理异常和真实隔离 local 下一 writer 立即接续。

首次能完整结束的全仓结果为 2 failed / 5319 passed / 23 skipped / 2 warnings，355.29 秒：一个是上述本轮配置段缺陷，一个是旧 Attention→Learning 测试以 __new__ 构造 scheduler 后缺少 managed-mode 状态。两项均需修正后重新执行完整回归，不能拿单独复跑成功替代。测试日志只在本地临时目录保存，不提交运行日志或数据库。

上述两项已修正，连同本地不可变约束校验一起冻结后，最终全仓完整跑到 100%：**5323 passed / 23 skipped / 0 failed / 2 warnings，333.57 秒，退出码 0**。采用项目完整串行命令并固定 randomly seed=20260905，未删去或 skip 曾失败的用例。JUnit 结构化证据位于本地临时文件 /tmp/elysium-opportunity-complete-20260905.xml；对应逐项日志为同名 .log。两条 warning 都是 websockets 依赖的弃用提醒。

跳过项没有被当成通过，尤其真实 Opportunity MySQL 合同仍因未配置明确隔离环境而跳过。最终全部本任务 Python 变更的 Ruff F/E9、相关 compileall、git diff --check 通过。未通过重复成功计数、局部挑选或重置正式数据掩盖失败。

## 当前现场准备阻断（只读）

默认无连接准备入口返回 TOMLDecodeError，没有打开数据库。脱敏核对确认：

- config/elysium.toml 的 TOML 语法有效。
- config/plugins/life_engine/config.toml 在第 1258、1340 行各有一个 learning 段；重复声明使解析失败。两段 enabled 都为 true，maintenance_poll_seconds 分别为空字符串和 5.0。
- 此处第二段是 maintenance_poll_seconds，并非本轮 OpportunitySection 的 poll_interval_seconds，不能把这份现场重复段直接归因为上述装饰器缺陷。文件修改时间为 2026-09-04T20:38:34Z；本轮只读定位，没有修改该文件或猜测修改者。
- 已向用户请求“先备份、保留启用与 5 秒、合并重复段、其余值不变”的精确修复授权。未批准前保持原文件，不能建议直接启动或宣称准备计划可用。

本机 MySQL 8.0.46 服务已存在，但缺少显式隔离测试库与账号。全局 binlog 开启且 trust gate 为 0；本轮不改全局设置。真实 MySQL 合同仍需独立临时对象范围授权，不能用正式库或读取正式密码代替测试环境。

### 08:25 用户手动启动后的实际证据

用户随后反馈 13/14 插件成功、1 个失败。只读核对当前 main.py 进程启动于 2026-09-05 08:25:12，日志 logs/elysium-2026-09-05.log 在 08:25:17.389 明确记录 life_engine 加载失败：Cannot declare ('learning',) twice (at line 1340, column 10)。08:25:19.483 的“Elysium 已苏醒”属于应用外壳继续运行，并非 Life Engine 启动成功。

这次阻断发生在配置解析阶段，不能归因于远程数据库或宣称机会/学习服务已正常运行。此前 5323 passed 的证据覆盖代码与隔离测试配置，不覆盖尚未修复的这份现场重复配置。当前不满足真实启动验收门，仍不提交推送。

进一步仅在内存中合并末尾两个 Learning 值后，TOML 已可解析，但 LifeEngineConfig 仍报告 18 项 Learning 配置校验错误：inject_to_heartbeat、model_task_name、llm_timeout_seconds、reflection_cooldown_minutes、audit_interval_hours、audit_batch_size、compress_trigger_count、compress_interval_hours、subject_review_enabled、subject_review_soul_interval_hours、subject_review_user_interval_hours、subject_review_memory_interval_hours、subject_review_offer_cooldown_hours、knowledge_max_chars、skill_distill_trigger_count、skill_distill_interval_hours、skill_catalog_max_chars、skill_max_edits。这些值为字符串占位，不能满足当前布尔/数值/非空字符串合同；未发现 Learning 以外的模型校验错误。因此先前“只合并重复段，其余值不变”的修复范围不足，不能据此要求用户反复重启。

该轮诊断未修改配置、数据库或进程；后续最小修复须先备份，合并重复段并处理上述无效占位，明确保留合法配置与主体决定，完整验证配置后，再由用户手动重新启动。正式配置修复尚待用户授权。

## 已完成与后续验收门

1. 用户手动停机后的完整串行回归已完成；若 Elysium 再次运行，不得继续并行高负载测试。
2. 需要具备明确隔离环境时验证真实 MySQL 合同；当前只能报告未验证，不能用 SQLite 成功冒充。
3. 备份、schema 准备、切换标记、生产配置切换均未执行，仍需明确维护授权和范围核验。
4. 默认不为爱莉安装任何包或写 workflow。真实初始化由其 active consciousness 通过工具亲自决定；旧安排只读保留，不伪造采纳。
5. 用户手动启动后，验证发布→潜意识→最终 exact receipt→后续确认，以及主体修改/卸载/重启保持和跨意识工具权限。
6. 完成真实启动与关键链验收后，才能精确暂存、审查提交和推送。当前未暂存任何本轮文件。

回退不得删除 managed marker、抹掉主体决定或以旧配置恢复被卸载的认知循环；需要恢复数据时另走受控备份恢复，而不是在线覆盖。
