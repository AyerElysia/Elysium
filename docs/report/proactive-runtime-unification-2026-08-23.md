# 主动系统单一权威收口报告（2026-08-23）

## 结论

生产运行时现在只有一个主动系统：`ProactiveAuthority`。模型只看到 `nucleus_proactive_query` 与 `nucleus_proactive_command`。`AttentionThread` 和 `InitiativeSeed` 是统一权威内语义不同的记录族，不拥有独立生命周期、writer、backend 或模型入口。

旧 `ThoughtStream` 可写 manager/model 已删除。原 `streams.json` 仍作为可追溯历史证据保留，由严格 UTF-8/schema/hash 快照读取器按原字节只读；旧 create/advance/retire/reactivate 永不映射为当前主体决定。

## 为什么不是把两类记录硬合成一张表

- Attention 回答“我愿意持续看见什么”，允许 open/note/pause/resume/close。
- Initiative 回答“我愿意给未来保留什么行动可能性”，允许 hold/rewrite/reencounter/release，并在行动当下另选 audience/surface。
- 把二者压成一个状态机会制造无意义的状态组合，并诱使基础设施把关注误当任务。
- 统一的是权威、主体门、幂等身份、存储代际、运行生命周期、健康和模型入口；保留的是必要领域差异。

## 关键安全合同

1. 只有 active consciousness instance 可提交决定；来源实例不能冒充 actor。
2. 稳定 occurrence 只由 tool call/source identity 派生，不含可变正文；相同 occurrence 改参数或跨记录族复用均冲突。
3. Initiative event 与 head 在一个 SQL 事务内提交；head 使用 revision CAS，两个 writer 竞争同一 revision 只有一个成功。
4. local 模式使用进程锁、authority lease、epoch/fencing token 与 SQL runtime；selected 模式消费 LifeEngineService 拥有的同一 runtime。
5. workspace backend binding 固定精确 backend identity/generation；模式切换无验证迁移时 fail closed，不自动复制或回退 JSON。
6. outreach decision 先耐久提交；平台确认后，传输层通过唯一 bridge 把规范化回执写入不可变 delivery-proof 账本。只有 proof 与 inbox/turn/claim/action/message 精确一致才可结算 `spoke`，任意 64 位字符串不能冒充送达。
7. 实例暂停/结束只清技术 focus，不改主体 AttentionThread。
8. open 线索、认知候选或时间流逝都不会重置 idle、制造周期 thought_deepen，或替主体推进状态。
9. 工具携带文本但未产生无工具最终轮时，基础设施不编写第一人称“已完成”兜底。
10. health/log 只含状态、计数、frontier、revision 与 error type，不输出主体正文；Attention/Initiative health 在同一事务快照中从不可变事件重放 head，并检查 proof/claim/turn/resolution 的对应关系。

## 兼容边界

- 旧 Attention/Initiative/Reachability/Outreach 与 Autonomy 模型工具实现已删除；生产只能构造统一 query/command。
- ThoughtStream 仅保留未注册的 archive reader，供离线证据审计；它没有 manager、writer 或运行时 prompt provider。
- rolling context 会移除旧主动工具 call/result，避免历史失败教模型继续尝试；权威 trajectory 不改写。
- 旧 AutonomyIntent、follow-up、tell_dfc、ThoughtStream mutation 均 fail closed。
- 旧快照不会被自动转成 AttentionThread/InitiativeSeed；未来若要采纳，必须由活跃主体读取证据后重新表述并显式提交。
- 旧健康键 `attention_threads` 只是统一权威健康中的只读子视图，不是第二个组件或 writer。

## 后端迁移与回滚

- local 与 selected backend 都由 workspace binding 固定到精确 backend identity、generation manifest、registry/provider identity 与 scope；binding schema 为 v3，迁移证据也进入 binding hash chain。
- v1/v2 marker 只允许在完全无主动历史时升级；旧 marker 已有历史则拒绝启动，不能靠删除 marker 或空库重建绕过。
- 冻结快照显式包含 proactive SQLite。`scripts/migrate_life_proactive.py` 将 Attention/Initiative/runtime decision/outreach/inbox/turn 作为一个域复制，并在逐表 root 与 aggregate root 完全一致后写迁移证书。
- runtime 从不自动复制、改绑或激活。切换审计必须消费 `proactive_authority` copy run 与证书；证书必须绑定当前 workspace 的源 marker、源 binding identity、源历史 root 和目标验证证据，另一工作区证书不能复用。旧 ThoughtStream archive 不能替代当前域迁移。
- marker 已经推进到新后端后，配置切回旧后端也不能让旧 binding chain 重新取得权威；回退必须先完成新的反向迁移，并追加单调的新 binding epoch。
- 切换前源快照保持不可改写，可直接作为回退证据；目标 backend 开始产生新写入后，禁止把配置直接指回旧源。此时回退必须先停写、生成新的全域反向导出/快照、逐项校验并登记新的 verified generation。本轮不声称提供自动反向同步。

## 发布门

代码提交前必须通过主动系统定向、Life Engine 全集、全仓回归、静态检查、编译和 diff check。根据项目规范，本变更不会由 AI 启动或重启 Elysium；完成本地提交后由用户手动启动，确认唯一权威启动日志、一次 query、一次决定、一次重启恢复以及正常聊天，再允许推送。

## 本地验收证据

- 主动权威与迁移核心：36 passed。
- 冻结快照、主动域复制校验与 cutover：18 passed。
- Life Engine 全集：1800 passed、15 skipped。
- 全仓（仅排除一条已证明与本分支零差异的 NapCat 基线用例）：4827 passed、21 skipped。
- 全部改动 Python 文件的 Ruff `F` / `E9` / `I`：通过；`compileall` 与 `git diff --check`：通过。
- 隔离虚拟环境中的 `lark-oapi==1.6.8` 安装曾缺少 wheel RECORD 明确列出的 `apaas` 目录；按 `uv.lock` 精确重装后，两条飞书长连接生命周期测试 2 passed。该操作只修复未跟踪的 `.venv`，不改变仓库或生产进程。

唯一未纳入通过数的 `test_napcat_fetch_does_not_bypass_proxy_after_http_status_error` 来自主分支 `48841d9d`：该提交把图片下载改成最多三次重试，但测试夹具仍只提供一次 HTTP 503 结果并期待原异常直接抛出。本分支对 NapCat 实现与该测试均无差异；为避免把无关适配器修复混入主动系统提交，本轮保留为明确基线，不把它伪装成通过。

真实启动验收尚未执行。按 `AGENTS.md`，本变更只能先形成本地提交；必须由用户手动启动 Elysium 并提供关键链路证据后才能推送。
