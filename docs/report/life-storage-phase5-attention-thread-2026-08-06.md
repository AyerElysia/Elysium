# 生命域可选存储阶段 5：AttentionThread 迁移交付报告

## 结论

ThoughtStream→AttentionThread 的领域生产者、兼容 facade 与平台迁移链已经在代码层收口。旧 `streams.json` 被严格视为 snapshot-only 证据：完整字节、原顺序、原字段、旧状态和逐行哈希可归档、复制、校验、反向导出，但不会生成任何虚构的 AttentionThread 事件。

本次未修改生产 `storage.enabled`，未复制或改写正式数据，未激活 generation，未启动、停止或重启 Elysium/NapCat。

## 已实现

- canonical Port、local/MySQL adapter、Presence DB-time actor gate、append-only event、head CAS、稳定分页与 UTF-8 chunk；
- 旧 ThoughtStream 写入口退役为 canonical facade；没有 canonical authority 时 mutation fail closed，查询优先 canonical、仅保留有界旧快照 fallback；
- `attention_legacy_snapshots` 与 `attention_legacy_candidates` 两张 snapshot-only 迁移表；
- SQLite/MySQL 数据库级 UPDATE/DELETE 拒绝，MySQL schema migration v1/v2 与 trigger contract 校验；
- 新目录原字节 archive、incomplete marker、manifest SHA-256、row root 和防篡改加载；
- fenced `CANDIDATE_COPY` 导入、同证据幂等/异证据冲突、canonical authority 写前后不变证明；
- 精确反向导出；
- `scripts/migrate_life_attention_threads.py` 正式迁移入口；
- `audit_life_storage_cutover.py` 六域 generation 门，新增 `--attention-run`，明确 legacy 不可激活而 canonical 必须从空域开始。

## 安全语义

- `active/dormant/completed` 不映射到 `open/paused/closed`；
- `last_thought` 不复制到 `public_statement`；
- 分数、访问次数、时间衰减和容量淘汰不构成主体决定；
- 旧 snapshot 永远 `generation_eligible=false`；
- 只有活跃意识实例未来通过 canonical command 作出的明确决定才进入事件账本；
- 迁移失败保留源与失败证据，不清表、不覆盖、不自动重试非断线错误。

## 验证状态

- Attention/迁移/切换审计：`22 passed / 2 skipped`；两项 skip 是默认未启用隔离 MySQL 的显式安全门；
- 真实旧快照临时 local 往返：`1 passed`。只读源为当前 `data/life_engine_workspace/thoughts/streams.json`，379,446 字节，SHA-256 `dace6b2e2375f3c23d7a7fb5c8ff97946b5d2cd2c8859bcb362e2ea1a6a07b36`，schema 2，global revision 2558，422 行（completed 284、dormant 138）；archive、candidate import、verify、reverse export 逐字节一致，源文件未改变；
- 本机全新隔离 MySQL 8.0.46：`2 passed`。覆盖 canonical actor/CAS/restart，以及 legacy exact import、幂等 replay、trigger 不可变和精确 reverse export；
- 正式 CLI 对 2026-08-05 在线候选快照完成实跑：365,127 字节、407 行，archive/import/reverse export 全部同 SHA/root，copy run 为 `copied` 而不是 `verified`；因为源 `writer_frozen=false`，且隔离库此前的 canonical 合同已留下 1 event/1 head，所以 `canonical_authority.generation_eligible=false`，汇总门不会误签署；
- 临时 MySQL 数据库和账号验收后已删除，活动连接为 0；临时 `log_bin_trust_function_creators` 已从 1 恢复为原值 0；未连接远程共享库；
- Thought/Attention/迁移/审计整组：`56 passed / 3 skipped`；Life Engine：`1016 passed / 11 skipped`；
- 全仓最终回归：`3732 passed / 17 skipped / 3 warnings`，覆盖率 `68.25%`；
- Ruff、format check、compileall 与全树 diff-check 均通过。所有 skip 均保留显式环境安全门，不以在线 shadow 冒充生产切换。

## 尚未完成

生产 MySQL 切换仍未执行。它必须等待用户维护窗口手动停止 Elysium、生成 `writer_frozen=true` 快照、使用全新隔离 generation 数据库完成六域复制/反向导出/汇总审计，由用户选择配置后再手动启动并完成真实链路验收。
