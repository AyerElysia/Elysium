# MySQL 基座迁移与恢复验证报告（2026-08-03）

## 1. 结论

Core SQLAlchemy 已支持 MySQL 8，现有 Core SQLite 数据已迁移到远端共享库并通过源/目标逐表内容指纹校验；远端逻辑备份已恢复到本地隔离 MySQL，并再次通过相同校验。运行中的 Elysium 未停止、未重启、未切换配置。

这次交付证明了“能连接、能建表、能无损迁移、失败能回滚、备份能恢复”。它不声称已经完成离线优先双向同步，也不声称 Life Engine 长期记忆已经迁入 MySQL。

## 2. 正式迁移清单

- SQLite 快照：`/root/Elysia/backups/mysql-migration-20260803-0925/Elysium.db`
- 文件 SHA-256：`56eab2a1e34b5eadd40e1410e87da42661f0d47d6d1cf88c1dbeca532e591abc`
- 逻辑聚合 SHA-256：`d1cae6d72571ca1b1d5d20fef321590f2cd381b477043bc467854e35e1c6dc7b`
- 完成迁移运行 ID：`a82ca22a-20b2-4365-86d4-e7d6be59aca8`

关键行数：

| 表 | 行数 |
|---|---:|
| `messages` | 82,575 |
| `chat_streams` | 86 |
| `person_info` | 34 |
| `images` | 1,572 |
| `image_descriptions` | 1,753 |

13 张业务表的源/目标行数和内容 SHA-256 均相同。重复迁移返回同一运行 ID 且 `already_applied: true`。

## 3. 真实问题与修复

### 3.1 SQLite 宽松长度隐藏了协议越界

第一次正式迁移在写入前被拒绝：`chat_streams.stream_id` 实际最长 69，旧声明为 64。业务事务回滚，远端业务表保持 0 行。

修复：

- 增加全部有界字符串的迁移前真实长度扫描；
- 一次性报告所有越界，不等复制到一半才发现；
- 3 个 `stream_id` 协议列统一为 128。

### 3.2 MySQL `FLOAT` 导致真实时间戳精度损失

第二次迁移复制完成后，逐表指纹不同，事务再次全量回滚。根因是 SQLAlchemy 通用 `Float` 在 MySQL 建成 32 位 `FLOAT`，而 SQLite 保存的是双精度值；整数测试数据无法暴露这个问题。

修复：所有持久化浮点列改为 `Double`，MySQL DDL 明确为 `DOUBLE`；新增方言测试防止回归。修复后正式源/目标聚合指纹完全一致。

## 4. 失败回滚与读写验证

- 本地真实 MySQL 故障注入：复制 `chat_streams` 后强制异常；13 张业务表全部为 0 行，审计状态为 `failed`；
- 远端两次真实失败：业务数据均回滚；清理前精确确认业务行数为 0，只删除本次失败创建的空表与失败审计；
- 远端事务探针：写入前 0、事务内 1、回滚后 0，无测试记录残留；
- 只读 `verify` 成功；
- 幂等重复迁移成功。

## 5. 备份与恢复证据

### 5.1 远端 MySQL 逻辑备份

- 路径：`/root/Elysia/backups/mysql-remote/elysium-mysql-20260803T014814Z.sql.gz`
- 压缩大小：7,877,414 bytes
- 解压 SQL：39,686,966 bytes
- SHA-256：`787089765f6dc59859fb9d84edc38786a801f4734281ae0671f4788075d469b9`
- gzip CRC 与 manifest 校验：通过。

该备份已恢复到本地隔离库 `elysium_restore_20260803`。恢复库与正式 SQLite 快照逐表内容指纹完全一致，不是仅做“文件能解压”的浅验证。

### 5.2 生命域本地备份

路径：`/root/Elysia/backups/life-domain-20260803-0950`

已通过 `integrity_check` 的主要文件：

| 数据 | 大小 | SHA-256 |
|---|---:|---|
| Core `Elysium.db` | 65,724,416 | `56eab2a1e34b5eadd40e1410e87da42661f0d47d6d1cf88c1dbeca532e591abc` |
| Life Memory `memory.db` | 174,784,512 | `59be74a30d975a21dcc851b932e239d25b926e806b4c9c683aee7d9e52058b65` |
| Life Event `life_events.sqlite3` | 137,179,136 | `b2c7e69b601bc95dadd43eebd245fc8a1a0e43b967c9c16a61446ef7f1687634` |
| 意识在场 | 303,104 | `3a133cc55b3087a764149a8beefa2b9084e92ddeef9cd3cf0681e726a438740c` |
| 世界投影 | 299,008 | `549712a67840b84b66b52270c0383d0adc0211a1d54e51455bd403743dd72974` |

另备份 1,811 个生命工作区文件。Chroma 向量数据按可重建投影处理。

## 6. 测试结果

- 数据库、迁移与备份合并测试集：118 passed；
- 包含配置契约、MySQL DDL、无 `TEXT` 键、64 位浮点、快照不可覆盖、旧可空列兼容、真实 MySQL 原子回滚、幂等迁移、只读复核；
- 语法编译：通过；
- 其中包含真实本地 MySQL 的建表、迁移、回滚与会话参数验证；
- 新增文件 Ruff、依赖锁与 `git diff --check`：通过；
- 运行中实例：保持不动。

一次直接对运行中 SQLite 和远端做跨公网全量复核时遇到 MySQL 连接瞬断；随后本地只读审计确认当前 SQLite 逻辑聚合指纹仍为正式迁移指纹。该网络中断未产生任何写入，也没有被当作数据不一致。

## 7. 尚未完成的边界

- 当前账号不能读取 binlog 位点，秒级本地增量归档尚未启用；目前已具备可自动调度的事务一致性全量快照；
- 运行实例仍使用 SQLite，最终配置切换需要用户批准的人工停写窗口；
- 远端不可用时的完整 Core 离线写入与双向同步属于后续 Outbox/Inbox 阶段；
- 前端应用表、API 契约和 Life Event 跨节点复制不在本次 Core MySQL 基座提交内。
