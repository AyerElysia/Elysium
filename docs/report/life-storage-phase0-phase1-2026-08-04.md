# 生命域可选存储阶段 0/1 交付报告（2026-08-04）

## 1. 结论

阶段 0/1 已建立可选择 local/MySQL 的**平台基座**：严格配置、连接与事务内核、同代 backend bundle、generation/authority/fencing、哈希链审计、无损快照、manifest 和独立复核都已实现并通过本地与真实远程 MySQL 验证。

本次没有迁移或切换爱莉的正式生命数据，没有修改任何既有 SQLite、Markdown、JSON/JSONL 或媒体文件，没有启动、停止或重启 Elysium。现有本地数据仍是唯一正式权威；`storage.enabled` 默认且必须保持关闭。Life Memory、Subject Document、Life Event、Presence、World 等领域 Port 与双后端适配器属于后续阶段，不能把本报告理解为“MySQL 版记忆系统已经上线”。

## 2. 真实数据只读盘点

盘点时间：2026-08-04 UTC。全部 SQLite `PRAGMA quick_check` 均为 `ok`。

| 数据源 | 大小 | 表数 | 总行数 | 只读计数扫描 |
| --- | ---: | ---: | ---: | ---: |
| `data/Elysium.db` | 65,724,416 B | 13 | 86,158 | 3.789 ms |
| `life_engine_workspace/.memory/memory.db` | 289,198,080 B | 62 | 289,094 | 54.667 ms |
| `life_engine_workspace/life_events.sqlite3` | 141,905,920 B | 10 | 86,017 | 2.605 ms |
| `runtime/consciousness_presence.sqlite3` | 1,081,344 B | 3 | 908 | 0.166 ms |
| `runtime/world_projection.sqlite3` | 1,196,032 B | 4 | 1,079 | 0.191 ms |
| `.memory/archive_sync_state.sqlite3` | 114,098,176 B | 3 | 380,702 | 13.653 ms |

关键规模：Core 消息 82,712、图片 1,572、图片描述 1,753；Life Event 86,014；Memory artifact versions 1,428、artifact heads 1,400、witness 2,892、witness sources 87,249、document chunks 10,259、claims 39、claim evidence 148、legacy nodes 1,483、corecall 8；Presence 35、生命周期 outbox 873；World assertions 108、changes 961；归档已知记录 380,700。

精确文件资产聚合：

| 根目录 | 文件数 | 聚合大小 |
| --- | ---: | ---: |
| `diaries` | 128 | 660,894 B |
| `life_engine_workspace` | 2,333 | 756,619,372 B |
| `media_cache` | 1,559 | 1,415,101,118 B |
| `emoji_sender/memes` | 145 | 71,218,011 B |

工作区聚合包含上表中的 SQLite 与可重建投影，因此不能直接相加作为备份净大小。快照器会去重六个数据库，排除 Chroma 等可重建投影，并保留但不递归复制旧备份目录。

## 3. 已实现内容

### 3.1 通用内核

- SQLite：WAL、foreign key、busy timeout 与只读健康诊断；
- MySQL：`utf8mb4`、UTC、strict SQL mode、READ COMMITTED、TLS 模式、连接池和有界连接/查询/锁等待；
- `AsyncUnitOfWork`：明确 commit/rollback/after-commit，after-commit 失败不会谎称数据库回滚；
- versioned/checksummed MySQL migration runner：数据库 advisory lock、防 checksum 漂移，主异常不会被解锁异常覆盖；
- 通用 canonical JSON/hash、不可变 identity 冲突和 position+revision 双条件 cursor CAS。

### 3.2 一致后端与单写权威

- factory 一次构造同 backend、generation、authority 的完整 runtime，禁止 repository 各自混搭；
- 配置默认关闭、缺少 generation/owner/epoch/环境变量秘密时 fail closed；
- file authority 支持单主机 local/MySQL 切换，在整个事务期间持有 fence；
- MySQL authority 支持多主机 MySQL writer，在同一写事务提交前锁行复核 generation、epoch、owner、lease 和 token；
- register/activate/renew/revoke 全部进入哈希链审计；篡改、过期、撤销、schema/generation 不匹配均拒绝写入；
- 健康输出不含密码或 token，inactive authority 不会被误报为 healthy。

### 3.3 无损快照与复核

- 六个 SQLite 数据源使用 Online Backup API；
- 逐表记录 schema hash、无序行集合逻辑根、行数和可识别 frontier；
- NULL、整数、浮点（含 NaN/Infinity）、文本、BLOB 使用显式类型编码，避免字符串碰撞；
- 主体/工作区/媒体文件逐字节复制并记录 SHA-256；复制前后源 stat 变化会失败；
- 目标已存在时拒绝覆盖，过程中保留 `SNAPSHOT_INCOMPLETE`，完整结束后才移除；
- manifest 有规范化哈希和总 source root；校验器防路径穿越并重新计算文件与 SQLite 逻辑根；
- 在线快照只能是 candidate；只有明确冻结 writer 且独立复核通过才可成为 verified generation。

## 4. 验证证据

- 定向本地合同与旧备份兼容：25 项功能用例通过；
- 静态检查：storage kernel、Life Engine storage、配置、脚本和测试全部通过；
- 真实远程 MySQL：使用独立测试 registry/generation/probe 表完成 schema migration、注册、激活、事务内 fencing、旧 token 拒写、续租、审计链健康和撤销，测试通过；
- 变基到 `soul/main` 后全仓回归：3,351 passed、7 skipped，覆盖率 66.77%；
- 远程测试没有写入正式生命业务表，没有激活 `life-domain` registry，不把测试 generation 作为正式候选；
- 真实数据盘点仅用只读 SQLite URI，没有读取正文到报告，也没有写回源库。

远程连接凭据仅在测试进程环境中短暂注入，没有写入源码、文档、日志或 Git。

## 5. 未完成项与明确阻断

以下事项未完成，因此不能切换：

- Life Event、Subject Document、Memory、Presence、World 的 backend-neutral Port 和 local/MySQL adapter；
- 全量本地→MySQL 逐记录复制、谱系/引用/visibility/frontier 校验与反向导出；
- Chroma/FTS/World 从所选权威重建的统一 projection outbox；
- Presence 使用数据库时间的 lease/takeover；
- World rebuild 与 perception cursor 分离、同 position 异内容冲突保护；
- Living Memory artifact head expected-revision CAS；
- Witness consumer offset/镜像游标单调 CAS；
- 隔离恢复库演练和正式业务全链路验收。

这些不是可以由平台层“猜实现”的细节：Memory 与 Presence/World 的主体语义由对应领域 owner 先冻结合同，再由平台层完成跨模块迁移和最终验收。

## 6. 下一阶段顺序

1. Presence/World 先修正现有 local P0/P1 语义并建立 local/fake/MySQL 共用合同；
2. Memory 先拆文档索引投影，再按 Experience → Witness → Living Memory → Epistemic → legacy graph 顺序迁移；
3. Subject Document 与 Life Event 建立不可变版本/事件 Port、outbox 和逐记录复制；
4. 生成冻结快照，复制到隔离 MySQL，做逐记录/root/frontier/谱系校验；
5. 做失败注入、并发、断连、重启、恢复、反向导出与真实性能验收；
6. 用户批准后才执行一次人工 authority 切换，并由用户手动启动 Elysium 完成真实聊天/记忆/检索/Presence/World 闭环。

## 7. 运行与审查入口

- 只读盘点：`scripts/audit_life_storage.py`；
- 无损快照与即时复核：`scripts/backup_life_data.py`；
- 存储内核：`src/kernel/storage/`；
- Life Engine 工厂、authority 与迁移：`plugins/life_engine/storage/`；
- 操作边界：`docs/operations/life_storage_backend_runbook.md`。
