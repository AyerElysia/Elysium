# MySQL 核心持久化边界

状态：已实现并完成真实远端迁移验证；尚未切换运行中 Elysium 的数据源。

## 1. 目标

本次重构让 Kernel/Core 的 SQLAlchemy 业务库同时支持 SQLite、PostgreSQL 与 MySQL 8，并把现有 `Elysium.db` 无损迁移到共享 MySQL。它解决的是聊天、人物、图片索引、权限和使用统计等核心关系数据的共享，不等价于把所有生命域存储强行改写为 MySQL。

核心原则：

- SQLite 源库只读，迁移前必须在线快照；
- MySQL 业务复制在单个 InnoDB 事务内完成；
- 逐表行数和规范化内容 SHA-256 完全一致才提交；
- 失败不切换数据源，不留下半份业务数据；
- 运行中的 Elysium 只能由用户手工停止、启动和切换配置。

## 2. 数据边界

### 2.1 已进入 MySQL 的 Core 数据

当前 ORM 共 13 张业务表：

- `chat_streams`、`messages`：聊天流与消息；
- `person_info`：平台身份、人物信息和关系摘要；
- `images`、`image_descriptions`：图片元数据与描述；
- `action_records`、`llm_usage`、`online_time`；
- `ban_users`、`permission_nodes`、`user_permissions`、`permission_groups`、`command_permissions`。

迁移器另建 `elysium_core_migration_runs`，只保存迁移来源指纹、状态和校验清单，不属于业务模型。

### 2.2 保持在本地生命域的数据

以下数据没有被一次性塞入 MySQL：

- `life_engine_workspace/.memory/memory.db`：结构化生命记忆、FTS、认识论和谱系；
- `life_engine_workspace/life_events.sqlite3`：不可变 Life Event 历史；
- `runtime/consciousness_presence.sqlite3`：意识在场状态；
- `runtime/world_projection.sqlite3`：世界状态投影；
- 日记、叙事、技能、思考等生命工作区文件；
- Chroma/HNSW 向量索引。

Life Event 与结构化记忆是权威数据；Chroma 和世界投影是可重建投影。这个边界遵循“历史不可变、投影可重建”，也避免 MySQL 改造破坏 SQLite FTS 和现有记忆语义。

后续若要多节点共享长期记忆，应先定义 Life Event 复制协议、节点身份、幂等键、冲突模型与投影重建流程，不能用数据库双写替代领域协议。

## 3. 方言契约

### 3.1 字符串

协议标识符使用有界 `VARCHAR`，正文使用 `TEXT`。这避免 MySQL 对无前缀 `TEXT` 索引和唯一约束的限制。

迁移前会扫描所有有界字符串的真实最大长度。真实数据曾发现 `chat_streams.stream_id` 长 69，而旧声明只有 64；现在同一协议的 3 个 `stream_id` 均为 `VARCHAR(128)`。任何新越界都会在接触目标库前一次性报出。

### 3.2 浮点数

所有持久化浮点值使用 SQLAlchemy `Double`：MySQL 为 64 位 `DOUBLE`，避免通用 `Float` 被编译为 32 位 MySQL `FLOAT` 后损失时间戳和计量值精度。

### 3.3 字符集和事务

- 数据库和连接固定 `utf8mb4`；
- 业务表必须是 InnoDB；
- 连接启用 `pool_pre_ping` 与回收；
- 会话时区为 UTC；
- 会话隔离级别为 `READ COMMITTED`；
- 锁等待有明确上限。

## 4. 配置与兼容桥

`CoreConfig.DatabaseSection.database_type` 是闭合枚举：`sqlite | postgresql | mysql`。MySQL 使用独立的 `mysql_*` 字段。

当前应用启动器仍通过历史 `postgresql_*` 参数槽调用数据库内核。由于该启动器属于并行生命周期修复范围，本次没有修改它；配置校验器只在 `database_type = "mysql"` 时把 `mysql_*` 值映射到旧参数槽。等生命周期改造完成后可删除桥接，用户配置无需迁移。

## 5. 迁移状态与切换边界

远端已保存一个经过完整指纹校验的 Core 基线，本地也已完成 MySQL 逻辑备份与恢复演练。运行中实例仍使用 SQLite，且没有被停止或重启。

最终切换必须在用户批准的人工停写窗口完成：

1. 用户手工停止 Elysium；
2. 创建最后一份 SQLite 在线快照；
3. 比对当前逻辑指纹与远端；若变化，执行受审计的最终增量/重迁移；
4. 校验远端和本地备份；
5. 手工修改 `config/core.toml`；
6. 用户手工启动并执行读写冒烟；
7. 保留原 SQLite 与所有 manifest，不删除。

本次验证结束时，运行中 SQLite 的逻辑数据指纹与已迁移基线相同；SQLite 文件物理 SHA 因 WAL/checkpoint 可不同，不能用文件字节相等替代逻辑数据相等。

## 6. 离线与共享的后续阶段

当前 MySQL 支持解决了共享 Core 数据，但“远端不可用时仍可完整运行”需要独立的离线优先同步层：本地事件 Outbox、远端 Inbox、节点游标、幂等投递和冲突诊断。详见 [离线优先共享后端重构计划](./offline_first_shared_backend_plan.md)。在该阶段完成前，切换到远端 MySQL 后，远端中断会影响 Core 关系数据功能；生命域本地存储仍可独立保存。
