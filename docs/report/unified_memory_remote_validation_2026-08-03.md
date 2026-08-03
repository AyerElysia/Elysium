# 统一记忆与远端 MySQL 迁移验证报告（2026-08-03）

## 1. 目标与约束

本次工作为 Elysium 分散的技术记忆建立统一、可校验、可恢复的远端归档，同时保持本地离线优先。全程不改写主体语义文件，不重写不可变历史，不把投影当权威，不停止、重启或自动拉起正在运行的 Elysium。

## 2. 迁移前证据

- `/root/Elysia/Elysium-backups/unified-memory-20260803-205350`：SQLite online backup、主体文件 tar、SHA256SUMS 和远端 MySQL 迁移前逻辑备份。
- `/root/Elysia/Elysium-backups/unified-memory-canonical-20260803-211838`：5 个 SQLite 一致性快照、2,210 个 workspace 文件和 manifest。

五个 SQLite 快照的 `integrity_check` 均为 `ok`。规范化预扫描得到 379,458 个唯一记录、377,622,132 字节 payload，未发现本地 ID 碰撞：

| 域 | 记录数 |
|---|---:|
| `core` | 86,114 |
| `life_events` | 84,566 |
| `life_memory` | 205,683 |
| `consciousness_presence` | 441 |
| `world_projection` | 446 |
| `workspace` | 2,208 |

后续把 workspace/runtime 下的非数据库 JSON 也纳入文件归档，最终正式清单可能多出少量记录；以最终验证清单为准。

## 3. 已实现内容

- 统一稳定记录模型与 immutable/versioned 冲突语义；
- 五个 SQLite 域的只读事务扫描和 schema 归档；
- workspace 文件递归备份、内联/分块传输和字节哈希；
- 远端追加记录、Heads、Outbox、冲突表和运行清单；
- 本地独立确认状态、增量跳过和内容哈希根复算；
- 全新目录恢复、SQLite integrity 与记录身份复扫；
- Life Engine 受管 worker、健康状态和连接关闭；
- manifest 逐文件大小/SHA-256 校验；
- 有界并发发布和 1062/1205/1213 整事务重读/重试。

## 4. 远端实连发现与处置

1. 远端允许建表，但应用账号受 binary logging 权限限制，创建 trigger 返回 1419；实现明确降级为 `application_hash_audit`，待 DBA 创建 trigger 后自动升级。
2. 小样本首次发布 accepted，原样重放全部 duplicate；两个清单的计数和 `record_id:payload_hash` 根均验证通过。
3. 并发实测发现 SQLite 游标跨通用线程池线程推进会被拒绝；扫描器已改为每轮专用单线程并加入回归测试。
4. `FOR UPDATE` 大范围唯一键查询会产生 InnoDB 间隙锁等待；查询阶段已取消范围写锁，最终仍由唯一约束裁决。
5. 并发重放出现 1062 时，不使用 `INSERT IGNORE`；事务回滚重读，精确相同转 duplicate，不同内容转 conflict。
6. 全量写入逐批维护 Heads/Outbox 会在随机哈希索引上形成锁竞争；实现改为“记录与清单先完成、投影按域后构建”，日常增量仍保持事务内即时投影。
7. 真实远端 8 路并发提交同一记录的集成测试在 4.17 秒内完成：1 次 accepted、7 次 duplicate、正文唯一、清单关联唯一、冲突为 0。会话锁等待上限为 5 秒，1205/1213/1062 均走有界整事务重试。

失败运行均保留为审计记录；事务失败没有改写本地权威数据。

## 5. 验证状态

### 5.1 正式远端全量清单

- manifest：`be51ec01-d9dd-4776-827c-8c598a8535d7`
- source node：`elysium-965bfe37-4a32-4f68-b622-63658faf403b`
- 扫描/关联：379,469 / 379,469
- accepted / duplicate / conflict：138,131 / 241,338 / 0
- root hash：`0cdcc11930ff2eb9121c3b9d4b48486d9675ff328f55f5ac8a682287e8ede634`
- 远端逐条复算：payload hash mismatch 为 0，calculated root 与清单 root 相同，`verified=true`

全量清单按域计数：

| 域 | 记录数 |
|---|---:|
| `core` | 86,114 |
| `life_events` | 84,566 |
| `life_memory` | 205,683 |
| `consciousness_presence` | 441 |
| `world_projection` | 446 |
| `workspace` | 2,219 |

### 5.2 隔离恢复演练

恢复目标为 `/root/Elysia/Elysium-restore-drill-20260803-2220`，没有覆盖正式 `data/`。结果：

- 5 个 SQLite 数据域均通过 `integrity_check`、foreign key、record identity 和 payload hash 复扫；
- `core` 86,114 条、`life_events` 84,566 条、`life_memory` 205,683 条、`consciousness_presence` 441 条、`world_projection` 446 条全部通过；
- workspace 重组 2,216 个文件、18,140,765 字节，文件与分块字节哈希全部一致；
- 整体恢复结果 `verified=true`。

### 5.3 当前运行数据增量追平

在不重启 Elysium 的前提下，从当前本地权威数据做了一次只读扫描并增量写入：

- manifest：`31267f68-f156-413b-b473-f24a402134dc`
- 扫描/accepted/duplicate/conflict：1,126 / 1,126 / 0 / 0
- root hash：`3037b8f4c96fee16bcd9eef2cea3e8af5ffc6c41afa5f44988da03f613c19aeb`
- 远端逐条复算 payload mismatch 为 0，`verified=true`。

交付前再次追平运行期间产生的新数据：manifest `3b6dbfa3-0c7c-46b8-9643-17e78466ba46`，扫描/accepted/conflict 为 103 / 103 / 0，payload mismatch 为 0，root hash `73e9819992124ca99cb1398d09c60608154d99cd029b2e6d5915b32b209c402e` 复算一致，`verified=true`。

最终审计时，该主节点共有 380,700 条归档记录、380,557 个 Heads，开放冲突为 0，运行中清单为 0。Outbox 的 pending 项是给未来下游 API/事件消费者保留的投递游标，不表示正文尚未同步；归档正文和运行清单均已在 MySQL。

### 5.4 自动化验证

- 定向回归：**47 passed、1 skipped、0 failed**；skip 是未注入远端凭据时按设计跳过的 MySQL 集成项；
- 同一 MySQL 集成项使用真实远端凭据单独执行：**1 passed**；
- 合入最新 `main` 后的项目全量回归：**3,263 passed、6 skipped、0 failed**（189.77 秒）；
- Ruff format/check 与 Python compileall 均通过。

## 6. 运行实例状态

本次没有停止、重启或自动拉起 Elysium/NapCat。当前实例继续使用原本地权威库。持续增量 worker 默认关闭；合并代码并配置环境变量后，仍须由用户在维护窗口手动重启才会加载。

## 7. 2026-08-04 主体性边界复审附记

后续复审确认：2026-08-03 的逐行/逐字节迁移、哈希验证和隔离恢复结论仍然有效，没有主体内容损坏；但旧归档元数据使用了容易与认识论权威混淆的 `authority` 名称，并把全部 workspace 文件统一描述成主体文件。

修订后的代码把领域术语改为开放文本 `archive_role`：只有规范明确声明的 `SOUL.md`、`USER.md`、`MEMORY.md` 和日记标注为 `declared_subject_artifact_exact_bytes`；其他 workspace 字节继续无损归档，但标为 `unclassified_workspace_exact_bytes`，不猜测作者或所有权。未知 SQLite 表使用 `unclassified_storage_record`，禁止静默晋升为权威。

为了保持既有 380,700 条追加式记录的身份与哈希，MySQL v1 物理列 `authority` 不做 UPDATE 或批量回填；它仅作为旧 wire/storage 列承载 archive role。该列从未接入 claim 真值、检索排名或主体文件写入链，因此本次修订不要求删除、覆盖或重迁历史数据。
