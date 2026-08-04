# 生命域可选存储阶段 2B：Life Memory 无损迁移报告

## 结论

Life Memory 已完成“只读快照 → 远程 MySQL shadow → 新 SQLite 恢复副本”的真实
往返验证。最终批次 `life-memory-shadow-v8-77435387f4acc59e` 为 `copied`，包含
32 张显式领域表与 210,104 条记录，冲突数为 0。源、MySQL 和反向 SQLite 的
聚合根均为：

```text
4703c2dc18470d16b9e4363f8b8c6a8b3d0f8cfda433baf1deddb9787951cf9c
```

这不是生产切换。来源快照没有冻结 writer，远端账号也不能安装数据库级不可变
trigger，因此 `generation_eligible=false`，`storage.enabled` 保持关闭，运行中的
Elysium 未停止、未重启、未切换后端。

## 数据来源与恢复资产

- 精确候选快照：`C:\Temp\Data\ElysiumBackups\life-domain-20260804T0615Z-candidate`
- manifest SHA-256：`77435387f4acc59e48ffe625015d07575398465534b132e428c47e4617124862`
- source snapshot SHA-256：`d8f800108c71203396f9e6c39e8aa0a386ce7521d2d60ea333ee4ca13ff5a724`
- Memory SQLite SHA-256：`f5e1c413aab877652d99e1adfd01d192f6d98643ecbbc237f62b7852c04ebd03`
- 反向恢复目录：`C:\Temp\Data\ElysiumBackups\life-memory-reverse-20260804T0920Z-v2`
- 反向 manifest SHA-256：`9375aca41ed9eef0ae50e87f6a2ce2b28e2daba2a9f17bf22570074989b4ac3a`
- 反向 FTS 可见投影行：14,675

原快照与原工作区均只读使用；失败候选和失败导出目录保留现场，没有删除、移动、
截断或覆盖任何源数据。

## 保真范围

本次选择合同覆盖 Document/Chunk/Index、Experience、Witness、Living Memory、
Epistemic、Recall/Corecall/Association、Legacy Graph 与 correction 共 32 张显式表。

关键实数：

- `memory_nodes`：1,497，其中删除历史节点 76；
- 与删除节点相连的 `memory_edges`：1,936，全部保留；
- `memory_edges` 总数：7,424；
- `memory_experiences`：86,093；
- `memory_witnesses`：2,906；
- `memory_witness_sources`：87,326；
- `memory_artifact_versions`：1,429；
- `memory_artifact_heads`：1,400；
- `memory_interpretations`：21；
- `memory_corecall_events`：8；
- `memory_association_projection`：251。

SQLite FTS 内部影子结构没有作为权威记录复制；可见正文和是否存在旧 FTS 投影由
显式字段保存，反向导出后重新建立 FTS5。Chroma 仍是可重建向量投影。

## 实库发现与修复

真实 MySQL 8.4 验证发现并关闭了四类只靠内存测试不易暴露的问题：

1. `projection_path VARCHAR(1024)` 无法建立 utf8mb4 唯一索引。v3 改为
   `projection_path_sha256` 派生索引；读写仍核对完整路径，hash 碰撞显式失败。
2. 多列 FULLTEXT 的字段 collation 不一致。需要共同检索的字段统一 binary
   collation，仍保留全文检索与子串降级路径。
3. `signal` 是 MySQL 保留字。schema、适配器、复制器和导出器的所有相关 SQL
   都统一引用反引号标识符，领域字段名和语义不变。
4. MySQL 原生 JSON 会改写高精度小数的文本表示。v8 将开放元数据保存为规范
   JSON `LONGTEXT`；失败候选重放只在不可变 `payload_sha256` 相同时修复表示层，
   不同 payload 仍产生冲突。

此外，目标规范化补齐了 `last_sequence` 与 `activation_count` 的整数语义；续跑先
比较整表根，一致时直接跳过，不再对 21 万条已一致记录执行空更新。

## 失败证据不是成功证据

远端 `life_storage_copy_runs` 保留了索引长度、全文索引、保留字、游标整数、JSON
精度和反向导出等失败批次。它们没有被删除或改写为成功。一次人工终止的慢速幂等
重放批次保留为未完成状态，需由租约过期协调器收束，不能冒充已复制。

最终只接受同时满足以下条件的 v8 批次：

- 32 表源/目标逐表 count 与 root 一致；
- 聚合根一致；
- 反向 SQLite `integrity_check=ok`；
- 反向 32 表再次得到同一聚合根；
- `EXPORT_INCOMPLETE` 已移除；
- 0 conflict；
- 不注册、不激活 backend generation。

## 操作与复核

复制入口：

```bash
uv run python scripts/migrate_life_memory.py \
  --snapshot /absolute/life-domain-candidate \
  --run-id life-memory-shadow-<manifest-prefix> \
  --reverse-export /new/life-memory-reverse-export
```

独立只读复核：

```bash
uv run python scripts/audit_life_memory_shadow.py \
  --snapshot /absolute/life-domain-candidate \
  --reverse-export /absolute/life-memory-reverse-export
```

该独立审计已在真实远端运行并返回 `verified=true`：32 表、210,104 行、21 个
Memory 外键、v1-v8 schema、全部开放 JSON 原文字段为 `LONGTEXT`，且
`mismatch_tables=[]`、反向未完成标记不存在。

数据库连接与密码只通过环境变量注入；报告、manifest、日志和仓库均不记录凭据。

## 尚未满足的生产切换门

- 当前快照为 `writer_frozen=false`，不能签发正式 generation；
- 远端账号不能创建不可变 trigger，当前只能声明 application-enforced shadow；
- 没有专用隔离 MySQL 测试库，破坏性并发合同仍必须 skip，不能借共享库冒充通过；
- Presence/World 与其他剩余生命域还需完成同等级真实复制、反向恢复与汇总验收；
- 最终切换仍需用户批准，并由用户手动启动 Elysium 做真实聊天、记忆、检索、
  Presence、World 与重启恢复闭环。
