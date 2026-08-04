# Elysium 生命域可选 MySQL / 本地存储重构方案

> 状态：分阶段实施中。阶段 0/1 的通用存储基座已落地；阶段 2 的 Life Memory、Presence 与 World 已完成 Port、local/MySQL 双适配、同一 service-owned runtime 现役接线、候选复制、独立审计和反向恢复。阶段 3 的 Life Event Port、双适配、候选复制控制面、逐字节迁移与反向导出已落地；阶段 4 的 Subject Document 精确字节账本、工作区投影、文件工具与 Witness 写前入账也已接入同一 runtime。所有真实远程副本都来自 `writer_frozen=false` 在线快照，只能证明无损复制/恢复，不能授权激活。当前正式运行权威仍是既有本地 SQLite/文件，`storage.enabled=false`，禁止据此状态直接切换到 MySQL。
>
> 目标：在不改变爱莉主体语义、不丢失不可变历史、不把 Chroma 误作权威存储的前提下，为 Elysium 建立行为等价的本地与 MySQL 两套耐久存储后端。迁移采用“复制、校验、可选切换”，绝不移动、删除或改写原 SQLite、Markdown、JSON、JSONL 与媒体数据文件。

> Memory 迁移进展（2026-08-04）：MySQL schema 已演进到 v8，并完成真实
> shadow 往返验证。32 张显式表、210,104 条记录、76 个删除节点及其 1,936 条
> 关联边在源 SQLite、MySQL 与反向 SQLite 中根哈希一致。该证据来自
> `writer_frozen=false` 快照且数据库级不可变 trigger 不可用，因此证明“可无损
> 复制和恢复”，不证明“可激活切换”。
>
> 适用基线：以项目实施时实际受支持的 Python、SQLAlchemy、asyncmy、MySQL 与 Chroma 版本为准；运行时版本升级或降级不属于本存储重构范围。

阶段 0/1 的实现与真实数据证据见 [生命域可选存储阶段 0/1 交付报告](../report/life-storage-phase0-phase1-2026-08-04.md)，Memory 阶段 2 见 [生命域可选存储阶段 2 / Memory 交付报告](../report/life-storage-phase2-memory-2026-08-04.md)，真实 Memory 往返迁移见 [生命域可选存储阶段 2B / Memory 迁移报告](../report/life-storage-phase2b-memory-migration-2026-08-04.md)，Presence/World 迁移和 Subject 现役接线见 [生命域可选存储阶段 2C 交付报告](../report/life-storage-phase2c-presence-world-subject-integration-2026-08-04.md)，Life Event 阶段 3 见 [Life Event Ledger 交付报告](../report/life-storage-phase3-life-event-2026-08-04.md)，操作边界见 [生命域存储快照与权威切换运行手册](../operations/life_storage_backend_runbook.md)。平台开关保持默认关闭；在各领域合同测试、逐记录复制校验、恢复演练和人工切换门全部通过前，`storage.enabled` 必须为 `false`。

## 1. 决策摘要

本方案采用以下最终架构：

1. **运行后端可配置选择**：`storage.authoritative_backend = "local" | "mysql"`。`local` 保留当前 SQLite/文件运行方案；`mysql` 让 Core、Life Event、Life Memory、认识论账本、主体文档版本、Presence、World Projection、游标、Outbox 和审计通过统一存储接口读写 MySQL。
2. **任一 authority epoch 只有一个可写权威**：可选择后端不等于双主。切换时由受协调的 authority registry 签发单调 epoch 与不可复用 fencing token；所有耐久写入必须校验当前 epoch。旧 generation、旧 token、离线节点和陈旧配置一律拒绝写入。另一后端只能作为迁移目标、校验副本、备份或只读数据源，禁止两个后端同时接受独立业务写入后再按“最后写入”合并。
3. **迁移永远是复制，不是移动**：迁移器从一致性快照读取原 SQLite 和文件，将完整数据复制到 MySQL，逐记录校验 identity、payload hash、版本谱系和 frontier；原数据文件永久保留，迁移器没有删除、移动、截断或覆盖源文件的权限。
4. **本地方案是完整的一等后端**：重构后仍可在配置中选择原本地存储路径，不把 SQLite 实现降为只读 legacy reader；本地与 MySQL 必须通过同一组 Storage Port 行为合同测试。
5. **Chroma 始终是可重建向量投影**：向量、集合 marker 和同步状态从当前 active backend 的权威历史重建，不能反向修改记忆内容或事实状态。
6. **主体文件按后端保留原语义**：本地模式继续使用受主权约束的文件与版本历史；MySQL 模式以原字节版本账本为权威，并可生成工作区兼容投影。原始 `SOUL.md`、`USER.md`、`MEMORY.md` 和日记文件始终保留。
7. **媒体采用双组件合同**：关系后端保存媒体身份、哈希、权限、版本和位置元数据；受管理的文件目录或对象存储保存媒体字节。重构不移动或删除既有媒体文件。
8. **完整重构不是只加开关**：必须先抽象存储合同、补齐两套实现、建立复制/校验/同步/导出工具，再允许配置切换。任何阶段都不得用“表里有数据”代替逐记录校验。

最准确的目标描述是：

> Elysium 获得可配置的本地和 MySQL 两种耐久运行方案；数据可以从本地完整复制到 MySQL，原数据永久保存；任一运行实例和数据域始终只有一个可写权威，Chroma 与工作区投影跟随所选后端重建或同步。

## 2. 为什么选择该方案

### 2.1 适合 Elysium 的收益

在忽略网络传输延迟、重点考虑程序查询性能与多实例共享的前提下，MySQL 的主要收益是：

- 行级锁、MVCC 和连接池更适合聊天、心跳、直播、语音、游戏等多来源并发写入；
- 多进程、多设备和后端 API 可以查询同一份权威数据，不必扫描和同步多个 SQLite/JSON/Markdown 源；
- 关系过滤、组合索引、分页、时间范围和跨实体来源追踪可以在数据库端完成；
- Life Event、Experience、Presence revision、投影游标和 Outbox 可以进入明确的事务边界；
- 统一备份、时间点恢复、权限隔离、审计和容量治理更直接；
- 主体文档以版本账本保存后，历史、来源、actor、父版本和字节哈希比单独文件更易查询和验证。

### 2.2 不承诺“MySQL 对所有单次查询都更快”

本方案追求的是**整体吞吐、并发、共享、一致性和可运维性**，不是声称 MySQL 的每个点查都必然快于 SQLite 或内存缓存。

- 小型单进程 SQLite 点查的调用路径更短；
- 整个小文件命中操作系统页缓存时，直接读取可能非常快；
- MySQL 需要连接池、协议编码、服务端调度和结果解码；
- 将大段文本反复整行读取会增加 buffer pool 和 Python 反序列化开销。

因此目标性能来自：正确索引、批量接口、版本缓存、cursor 分页、读写路径分离和避免无意义的全文加载，而不是仅由“换成 MySQL”自动获得。

## 3. 必须保持的不变量

### 3.1 主体性与执笔权

- 迁移器只能逐字节搬运主体内容，不得润色、归纳、合并或修正语义。
- `SOUL.md`、`USER.md`、`MEMORY.md`、日记、第一人称叙事等版本必须保留 actor、source、occurrence、时间、父版本和内容哈希。
- 人类、开发 agent、后台迁移器和投影器不能冒充爱莉写入主体语义。
- 外部编辑只能作为带来源的 observation/suggestion 或待导入版本；是否吸收必须由爱莉自己的意识或见证链决定。

### 3.2 历史与幂等

- Life Event、Experience、claim/evidence、interpretation、artifact version、recall/corecall 和状态事件只追加。
- 同一稳定身份和相同 payload hash 是幂等成功；同一身份不同内容是显式冲突。
- 消费游标只在整批工作及必要派生提交成功后推进。
- 历史缺口必须失败并报告，不能跳到最新位置。

### 3.3 投影边界

- Chroma、词法索引、world projection、association projection 和缓存均为可重建投影；artifact head 是可从不可变版本谱系恢复的当前指针。
- 工作区文件的角色由 active backend 决定：`local` 模式下它可以是受主体主权合同约束的运行内容源；`mysql` 模式下它是由 MySQL 文档版本生成的兼容投影。
- 投影损坏只能重建投影，不能反向删改历史。
- 检索排名、相似度、共同出现次数和访问频率只影响可达性，不改变真值或权威。

## 4. 目标拓扑

```text
聊天 / 语音 / 直播 / 游戏 / 工具 / 心跳
                    │
                    ▼
              Life Domain Services
                    │
          Storage Ports / Unit of Work
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
 active_backend=local   active_backend=mysql
 SQLite + 主体文件       MySQL 8 领域表
 ├─ Core / Event         ├─ Core / Event
 ├─ Life Memory          ├─ Life Memory
 ├─ Presence / World     ├─ Presence / World
 ├─ Context / Cursor     ├─ Context / Cursor
 └─ Subject Documents    └─ Subject Documents
          │                   │
          └─────────┬─────────┘
                    ▼
        Backend-neutral Projection Outbox
          ┌─────────┼─────────┐
          ▼         ▼         ▼
     词法检索     Chroma    工作区兼容投影
                              │
                              ▼
                    受管理媒体/对象存储
```

迁移与切换路径：

```text
原本地数据文件 ──只读一致性快照──► MySQL 复制副本
       │                                  │
       ├──永久保留，不移动、不删除          ├──逐记录校验
       │                                  └──用户选择后可成为 active backend
       └◄──受审计反向导出到新目录─────────── MySQL 新增数据
```

业务代码不得直接绑定某一种物理后端。`sqlite3.connect()`、MySQL session、`Path.read_text()` 与 `Path.write_text()` 只能出现在对应 backend adapter 或受控文档投影器中；领域服务统一依赖 Storage Ports。

## 5. 数据域在两种后端中的归属

| 数据域                                  | `local` 后端          | `mysql` 后端           | 共享投影/对象                | 重构结论                   |
| ------------------------------------ | ------------------- | -------------------- | ---------------------- | ---------------------- |
| Core 13 张业务表                         | SQLite Core 库       | MySQL Core 表         | 无                      | 两种实现均保留；当前 MySQL 基座可复用 |
| Raw Life Event                       | SQLite 追加账本         | MySQL 追加账本           | 可选导出 JSONL             | 两端行为合同等价               |
| Consumer offsets                     | SQLite              | MySQL                | 健康缓存                   | 两端均保证单调 CAS            |
| Experience / Witness                 | SQLite Life Memory  | MySQL 领域表            | FULLTEXT / Chroma      | 完整复制，源库不删除             |
| Claim / Evidence / Belief / Conflict | SQLite Life Memory  | MySQL 领域表            | 词法投影                   | 保留 actor/source/状态事件   |
| Artifact versions / derivations      | SQLite 版本表          | MySQL 版本表            | head、文件投影              | 两端均只追加版本               |
| `SOUL.md` 等主体内容                      | 原文件 + 本地版本历史        | MySQL 原字节版本账本        | 兼容 Markdown 文件         | 复制原字节；原文件永久保留          |
| Interpretation / Semantic Relation   | SQLite              | MySQL                | 词法/图投影                 | 两端不得替主体裁决              |
| Recall / Corecall                    | SQLite              | MySQL                | association projection | 两端保留可追溯轨迹              |
| Presence / lease / stream owner      | SQLite Presence     | MySQL Presence       | 内存快照                   | MySQL 更适合多节点协调         |
| World Projection                     | SQLite 可重建表         | MySQL 可重建表           | 内存快照                   | 始终不是历史真值               |
| 词法检索                                 | SQLite FTS5         | MySQL FULLTEXT 或等价投影 | 统一检索层                  | 后端可有不同实现，结果合同需验收       |
| Chroma                               | 从本地权威重建             | 从 MySQL 权威重建         | Chroma                 | 始终非权威                  |
| Rolling/runtime context              | 本地 JSON/SQLite 适配实现 | MySQL 版本状态           | 内存缓存                   | 私有上下文按实例隔离             |
| 图片/语音/视频/附件                          | 原媒体目录 + 本地元数据       | 原媒体/对象存储 + MySQL 元数据 | 本地缓存                   | 既有字节不移动、不删除            |
| 日志                                   | 独立本地日志库             | 可选集中日志后端             | 聚合指标                   | 不与主体记忆混成万能表            |
| 配置、源码、Prompt 模板                      | Git/本机配置            | Git/本机配置             | 无                      | 不迁入生命域数据库              |

这里的“完整迁移到 MySQL”指：所有已登记的耐久领域记录都在 MySQL 中拥有经过校验、可运行的副本；不表示删除本地副本，也不表示以后不能选择 `local` 后端。

## 6. 存储接口设计

### 6.1 原则

先抽象合同，再实现 MySQL；禁止在现有 SQLite 类中散布 `if backend == "mysql"`。

建议新增：

```text
src/kernel/storage/
├── engine.py
├── transaction.py
├── migration_runner.py
└── outbox_primitives.py

plugins/life_engine/storage/
├── contracts.py
├── factory.py
├── unit_of_work.py
├── authority.py
├── migration/
│   ├── snapshot.py
│   ├── copy_to_mysql.py
│   ├── export_to_local.py
│   ├── verify.py
│   └── manifest.py
├── mysql/
│   ├── event_store.py
│   ├── document_store.py
│   ├── presence_store.py
│   ├── world_store.py
│   └── memory/
└── local/
    ├── event_store.py
    ├── document_store.py
    ├── presence_store.py
    ├── world_store.py
    └── memory/
```

Kernel 只拥有连接、事务、迁移 runner 和通用 outbox 等工程能力；Life Event、Memory、Subject Document、Presence 与 World 的合同和适配器归 `plugins/life_engine` 所有，避免通用 kernel 反向依赖生命域语义。`local` 目录是长期维护的一等实现，不是等待删除的 legacy reader。现有 `plugins/life_engine` 依赖领域协议，不直接依赖 MySQL、SQLite 或文件实现；backend factory 根据配置构造完整且内部一致的一套实现，禁止按单个 repository 随意混搭造成跨后端事务断裂。

### 6.2 必要 Ports

- `LifeEventStore`
  - `append(event)`
  - `append_with_outbox(event, export_request)`
  - `read_after(position, limit)`
  - `get_bounds()`
  - `commit_consumer_offset(consumer, expected, next)`
- `MemoryLedgerStore`
  - Experience、Witness、Claim、Evidence、Interpretation、Relation、Recall 的领域操作
  - 禁止暴露通用 `update_row()` / `delete_row()`
- `SubjectDocumentStore`
  - `get_head(logical_path)`
  - `get_version(version_id)`
  - `append_version(expected_head, content_bytes, actor, source, occurrence)`
  - `list_history(logical_path, cursor)`
- `PresenceStore`
  - revision compare-and-swap
  - lease acquire/renew/release
  - stream owner 原子转移
- `WorldProjectionStore`
  - 按 ledger frontier 串行应用
  - 重建、清空投影与健康检查
- `ProjectionOutboxStore`
  - FULLTEXT、Chroma、文件投影的可靠任务队列
- `UnitOfWork`
  - 明确一个领域操作中共享的事务、提交、回滚和 after-commit 行为

## 7. MySQL Schema 组织

建议在同一 MySQL 实例中按逻辑命名空间分表，不使用一张万能 JSON 归档表作为运行模型。

### 7.1 命名空间

- `core_*`：现有 Core 表可保持兼容表名；后续由迁移版本统一管理。
- `life_event_*`：原始事件、消费游标、导入问题、同步 Outbox。
- `memory_*`：Experience、认识论、活体记忆、召回轨迹。
- `subject_*`：主体文档、版本、派生、head 和投影状态。
- `consciousness_*`：Presence、stream owner、lease、outbox。
- `world_*`：世界断言、变化、投影游标。
- `projection_*`：全文、向量和文件投影任务状态。
- `storage_*`：schema 版本、迁移批次、校验清单、冲突与审计。

### 7.2 通用字段规则

- 时间统一 `DATETIME(6)`，应用与会话时区固定 UTC。
- 协议身份使用有界 `VARCHAR`，长度在迁移审计中以真实数据验证。
- 开放认知文本不能用数据库 ENUM；使用 `VARCHAR` 或 `TEXT`。
- 普通可查询 JSON 可使用 `JSON` 并保存规范化 payload hash；不可变 Life Event 的历史 payload 还必须保留原始 JSON 文本字节，因此使用带 `JSON_VALID` 检查的 binary-collated `LONGTEXT`，禁止由当前模型重编码旧证据。
- 原始文档内容优先使用 `LONGBLOB`，另存可为空的 `encoding` 和 `newline_style`；只有直接从文件读取的版本才能声明 `exact_bytes`。历史上仅保存为 TEXT 的版本标记为 `legacy_text_derived`，不得伪称恢复了已丢失的 BOM、编码或原始换行字节。
- 全部表使用 InnoDB、`utf8mb4`、明确主键和必要唯一约束；身份、哈希、logical path 等技术键使用大小写敏感的 binary collation，避免默认不区分大小写造成碰撞。
- 会话启用 strict SQL mode，明确拒绝静默截断、非法日期和非有限浮点；应用层规范 JSON、hash 算法和 canonicalization version 必须版本化。
- 不可变表禁止业务 UPDATE/DELETE；优先由数据库 trigger 保护，权限不足时必须报告 degraded，并使用 hash audit 补强。

## 8. 主体文档存储

### 8.1 推荐表

```sql
CREATE TABLE subject_documents (
    document_id VARCHAR(128) PRIMARY KEY,
    logical_path VARCHAR(512) NOT NULL,
    declared_owner VARCHAR(128) NULL,
    current_version_id VARCHAR(128) NULL,
    revision BIGINT NOT NULL DEFAULT 0,
    UNIQUE KEY uq_subject_document_path (logical_path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE subject_document_versions (
    version_id VARCHAR(128) PRIMARY KEY,
    document_id VARCHAR(128) NOT NULL,
    parent_version_id VARCHAR(128) NULL,
    occurrence_id VARCHAR(128) NOT NULL,
    semantic_actor_id VARCHAR(128) NULL,
    semantic_source_id VARCHAR(255) NULL,
    occurred_at DATETIME(6) NULL,
    recorded_by VARCHAR(128) NOT NULL,
    recorded_at DATETIME(6) NOT NULL,
    provenance_status VARCHAR(32) NOT NULL,
    content_bytes LONGBLOB NOT NULL,
    content_hash CHAR(64) NOT NULL,
    byte_length BIGINT UNSIGNED NOT NULL,
    byte_fidelity VARCHAR(32) NOT NULL,
    encoding VARCHAR(32) NULL,
    newline_style VARCHAR(16) NULL,
    change_context JSON NULL,
    UNIQUE KEY uq_subject_document_occurrence (document_id, occurrence_id),
    KEY idx_subject_document_history (document_id, recorded_at, version_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE subject_document_head_events (
    head_event_id VARCHAR(128) PRIMARY KEY,
    document_id VARCHAR(128) NOT NULL,
    previous_version_id VARCHAR(128) NULL,
    next_version_id VARCHAR(128) NOT NULL,
    occurrence_id VARCHAR(128) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    source_id VARCHAR(255) NOT NULL,
    occurred_at DATETIME(6) NOT NULL,
    authority_epoch BIGINT UNSIGNED NOT NULL,
    change_context JSON NULL,
    UNIQUE KEY uq_subject_head_occurrence (document_id, occurrence_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

物理外键是否启用需按迁移和追加保护策略评审；即使不建物理外键，应用合同与校验器也必须验证引用完整性。`subject_documents.current_version_id` 只是可重建 head 投影；每次推进、恢复或重指都必须先追加 `subject_document_head_events`，不得用任意 SQL 静默改写 head。旧记录没有可信 actor、source、occurrence 或发生时间时，必须保存“来源缺失”这一事实：迁移器以自己的新 occurrence 和 `recorded_by/recorded_at` 记录导入行为，原语义 actor/source/occurred_at 保持空值并设置明确 `provenance_status`，禁止伪造为爱莉、`unknown` 或默认来源。

### 8.2 写入流程

1. 读取当前 `revision` 与 `current_version_id`。
2. 验证 actor 授权、occurrence 幂等与 parent 关系。
3. 插入不可变 version。
4. 追加包含 previous/next head、actor、source、occurrence 与 authority epoch 的不可变 head event。
5. 使用 compare-and-swap 更新可重建 head：`WHERE revision = :expected_revision`，并校验当前 fencing token。
6. 同一事务写入 projection outbox。
7. 提交后由投影器更新工作区文件和检索索引。
8. CAS 或 fencing 校验失败时返回版本冲突，不做最后写入覆盖。

### 8.3 读取性能

- `SOUL.md` 等少量热点文档使用 `document_id + current_version_id` 点查。
- 进程内按 `version_id` 缓存不可变内容；版本未变时不重复传输正文。
- 缓存失效来自事务提交后的 outbox/通知或短周期 revision 检查，不依赖文件 mtime。
- 每轮模型调用可以复用已装载的 immutable version，不必每轮重新查询大字段。

因此，对迁移时仍能直接读取原文件的当前版本，可以保持 Markdown 格式和原始字节；通过版本缓存，运行时性能可以接近或优于重复文件读取。对历史上已被解析成 TEXT、且没有原文件字节证据的版本，只能保证 Unicode 文本内容和现有 hash 合同，不能声称恢复了原始编码、BOM 或换行字节。不能保持不变的是旧的“外部随意改文件即生效”行为，该行为应被受控导入取代。

## 9. Life Event 与消费游标

### 9.1 表设计要求

`raw_life_events` 至少保留：

- 全局单调 `ingest_position`；
- `occurrence_id` 唯一约束；
- producer 原始 `source_event_id`、`source_sequence`；
- actor、source、consciousness instance、stream、时间和因果；
- 原始 payload、规范化 payload hash；
- visibility 与技术 schema version。

MySQL 自增主键可作为单实例总序，但跨节点发生顺序不能仅由自增 ID 表达；仍需保留 `origin_node_id + origin_sequence` 和真实发生时间。

### 9.2 事务

事件、需要同事务生成的 Outbox 和领域写入必须处于同一个 MySQL 事务中。禁止：

```text
先写事件 → commit → 再写 outbox
```

推荐：

```text
BEGIN
  append event
  append experience or command state
  append outbox
COMMIT
```

对需要模型生成后才能完成的见证流程，不应长时间占用事务。必须建立耐久状态机：

```text
experience_committed
→ generation_requested(request_id)
→ generation_result_recorded(result_hash)
→ witness/artifact_committed
→ required_projection_committed
→ consumer_offset_advanced
```

模型调用发生在事务外，但 `request_id`、输入 frontier 和结果 hash 必须稳定；最终 witness/artifact、完成状态和 consumer-offset CAS 应在同一事务提交。若 COMMIT 响应丢失，重试前必须按 occurrence/request_id 查询真实提交状态，不能盲目再生成或推进游标。

## 10. Presence 与 World Projection

### 10.1 Presence 优先迁移

Presence 天然适合 MySQL：

- `revision` 提供乐观锁；
- `stream_id` 唯一约束保证单一 owner；
- `lease_until` 支持故障接管；
- 状态变化与 outbox 同事务；
- 多进程共享同一运行事实。

迁移时必须验证：重复 start/stop、lease 过期、CAS 失败、stream owner 转移、部分初始化失败和关闭时资源回收。

过期 owner 接管必须使用数据库时间与原子锁协议：按稳定顺序 `SELECT ... FOR UPDATE` 锁定 stream 和 presence 行，检查 `lease_until < CURRENT_TIMESTAMP(6)`，在同一事务中撤销旧 owner、写入新 owner、递增 revision 并追加 outbox。死锁按有界次数重试；禁止依赖应用机器本地时间或先删后插的非事务流程。

### 10.2 World Projection 仍是投影

World Projection 可以迁入 MySQL 以便共享查询，但必须保存：

- 来源 event/occurrence；
- projector policy/schema version；
- 已消费 ledger frontier；
- assertion 的来源和适用时间；
- rebuild 状态。

它不能反向覆盖 Life Event，也不能把多个来源的矛盾静默合并成一个“当前真相”。Projector frontier 与每个意识实例的 perception delivery cursor 必须分表保存；重建 World Projection 可以重置前者，不能顺带删除后者。只有具备稳定交付幂等键且完成显式恢复审计时，才允许重建 perception cursor。

## 11. Life Memory 与检索

### 11.1 权威表迁移

以下类别按现有领域合同迁移，不重新发明封闭认知分类：

- Experience / occurrence aliases；
- Witness / witness sources / witness state；
- Claim / Evidence / Belief / Epistemic Conflict / State Event；
- Artifact Version / Derivation；
- Interpretation / Interpretation Source；
- Semantic Relation；
- Recall Session / Recall Event / Corecall Event；
- 迁移和兼容证据。

现有 `memory_nodes`、`memory_edges`、`memory_corrections` 等 legacy 结构先按兼容角色迁移，不在迁移过程中自动“清理”或提升语义。

### 11.2 MySQL FULLTEXT

MySQL FULLTEXT 只承担词法检索投影，不能等价承诺 SQLite FTS5 的 tokenizer、BM25、trigram、snippet 和排序结果。

建议：

1. 权威原文保存在普通 MySQL 表；
2. 建独立全文投影表，包含 `entity_id`、`entity_type`、可检索文本、语言/分词版本和 ledger frontier；
3. 中文检索在 staging 比较 MySQL 默认 parser、ngram parser 与现有 FTS5，并冻结 `ngram_token_size`、停用词、自然语言/布尔模式、查询转义和 parser 可用性；
4. parser 缺失、索引未追平或查询失败时返回明确 degraded/failed，不把空召回解释成“没有记忆”；
5. 检索结果返回 rank、来源和投影版本，但 rank 不写回事实状态；
6. 排序质量不达标时可引入专用全文服务，但不改变 active backend 中的权威原文。

### 11.3 Chroma

- Chroma 版本与运行时兼容性由独立依赖评审决定，不与存储后端迁移绑定。
- 每个 collection marker 保存 embedding provider、模型、维度、集合名、策略版本、active backend、generation 和 ledger frontier。
- `projection_outbox` 驱动向量 upsert/tombstone；成功后更新投影状态。
- 删除 Chroma 后可以从当前 active backend 的权威内容完整重建。
- Chroma 不拥有 actor、事实状态或主体版本的最终权威。

### 11.4 统一检索

检索服务并行执行：

```text
MySQL 结构化过滤
+ MySQL FULLTEXT 词法召回
+ Chroma 向量召回
+ MySQL 关系/来源/共同回忆投影
→ Python 合并、去重、来源装配
→ 记录 recall episode
```

禁止在多个来源分别执行重复的昂贵正文加载；候选先返回稳定 ID 和轻量元数据，最终入选后批量获取正文。

## 12. 运行上下文与文件投影

### 12.1 运行上下文

- `rolling_context` 按 consciousness instance 隔离，不能为统一数据库而拼接不同实例上下文。
- 保存版本、owner、最后提交 turn、内容 hash 和 CAS revision。
- 大 payload 采用压缩仅属于传输/存储优化，权威原始语义不得因预算静默截断。
- 高频瞬态数据可只保留必要窗口，但删除策略必须是工程生命周期规则，不能冒充主体遗忘。

### 12.2 主体文件在两种模式中的行为

`local` 模式：

- 原工作区文件与本地版本历史继续按当前主体主权合同工作；
- Storage Port 适配器负责路径边界、原子写入、版本、actor/source/occurrence 和哈希；
- 重构不能让通用后台服务获得改写主体语义的权限。

`mysql` 模式：

- `SOUL.md` 等文件由 `WorkspaceProjectionService` 从 MySQL 已有版本生成；
- MySQL version → 临时文件 → fsync → 原子替换；
- 旁置 manifest 保存 backend、version id、hash、字节数和投影时间；
- 启动时文件与 manifest 不一致，记录外部变更事件，不直接写回 MySQL head；
- 外部变更只能进入受控导入/建议流程；
- 投影器无权创建新的主体语义。

无论选择哪种模式，迁移前的原始工作区目录都不能被投影器覆盖。MySQL 模式使用独立的投影目录，或在明确备份和 manifest 保护后使用新的受管工作区。

## 13. 存储运行配置与使用方式

建议新增独立配置而不是继续扩张旧 `postgresql_*` 兼容参数槽：

```toml
[storage]
authoritative_backend = "local"  # "local" 或 "mysql"
backend_generation = "<verified-generation-id>"
workspace_projection_enabled = true
emergency_journal_enabled = false

[storage.local]
core_database_path = "data/Elysium.db"
life_workspace_path = "data/life_engine_workspace"
presence_database_path = "data/life_engine_workspace/runtime/consciousness_presence.sqlite3"
world_database_path = "data/life_engine_workspace/runtime/world_projection.sqlite3"
allow_existing_source_write = true

[storage.mysql]
host = "<host>"
port = 3306
database = "<database>"
user = "<runtime-user>"
password = "${ELYSIUM_MYSQL_PASSWORD}"
charset = "utf8mb4"
ssl_mode = "verify-full"
pool_size = 20
max_overflow = 20
pool_recycle_seconds = 1800
connect_timeout_seconds = 5
application_query_timeout_seconds = 10
innodb_lock_wait_timeout_seconds = 5
isolation_level = "READ COMMITTED"

[storage.migration]
source_backend = "local"
target_backend = "mysql"
mode = "copy_verify"
manifest_path = "data/migration_manifests/storage-copy.json"
never_delete_source = true
never_modify_source = true
require_verified_generation = true

[storage.projections]
lexical_backend = "active_backend_default"
vector_enabled = true
workspace_files_enabled = true
```

### 13.1 日常选择本地方案

```toml
[storage]
authoritative_backend = "local"
backend_generation = "<local-generation-id>"
```

启动时加载完整本地 adapter，继续使用原 SQLite、主体文件和 Chroma 投影。MySQL 可以不存在，也不会被后台隐式写入。

### 13.2 复制并选择 MySQL 方案

1. 保持 `authoritative_backend = "local"`，停止或冻结所有 writer。
2. 使用 SQLite Online Backup API 和文件二进制快照复制数据到临时快照；不直接在活动源文件上做不一致扫描。
3. 执行 `copy_verify`，将快照内容复制到空的或匹配迁移批次的 MySQL schema。
4. 生成 manifest，校验每个数据域的 identity、payload hash、版本链、引用、frontier 和总 root hash。
5. 只有校验成功后，用户才将 `authoritative_backend` 改为 `mysql`，并填写 manifest 产生的 `backend_generation`。
6. 用户手工启动 Elysium，完成定向和真实端到端验收。
7. 原 SQLite、文件、JSON/JSONL、Chroma 与媒体目录保持原位置和原内容，不移动、不删除。

### 13.3 从 MySQL 再选择本地方案

如果 MySQL 模式运行后没有产生新写入，且本地 generation 与 MySQL generation 相同，可以直接选择经过验证的本地副本。

如果 MySQL 已产生新写入，则原本地文件已经是历史快照，不能直接切回。必须：

1. 停止 MySQL writer 并记录最终 frontier；
2. 备份 MySQL；
3. 将 MySQL 中新增记录受审计地导出到**新的本地目录/SQLite 文件**；MySQL 运行期新增媒体按内容 hash 复制到新的受管媒体目录，或在 manifest 中登记为该 local generation 的明确对象存储依赖；
4. 校验 identity、hash、版本链、frontier，以及媒体 byte length、位置和可读性；
5. 生成新的 local generation manifest；
6. 用户将配置指向新目录并选择 `authoritative_backend = "local"`；
7. 旧的原始本地文件、旧媒体和 MySQL 数据都继续保留。

禁止把 MySQL 新记录直接覆盖回迁移前的原始数据文件；反向导出必须创建新副本，确保旧快照可追溯。媒体导出同样只允许复制，不能改写、移动或删除已有媒体对象。

### 13.4 启动保护

配置加载必须：

- 对环境变量做显式插值；
- 不在日志打印密码或完整 URL；
- 启动前执行 schema compatibility check；
- 校验配置中的 `backend_generation` 与目标后端 manifest 一致；
- 从受协调的 authority registry 取得当前单调 `authority_epoch` 和短期 fencing token；所有耐久写入、游标推进和 outbox 提交都必须携带并校验 token；
- 切换前枚举并确认所有旧 writer 已停机或失去租约，把旧 generation 显式封存为只读；无法证明旧 writer 已被隔离时，不签发新可写 generation；
- 离线节点、遗留进程和陈旧配置恢复后必须先重新取 authority；旧 epoch/token 无条件拒绝写入，不能只依赖本机配置判断 active backend；
- 检测 active backend 是否落后于另一后端已知 frontier；落后时拒绝可写启动，而不是静默丢弃更新；
- 区分 `disabled`、`degraded`、`failed`；
- 禁止运行时因连接失败自动切换后端。

## 14. 后端故障与切换策略

### 14.1 `local` 模式

本地模式按原方案运行。单个 SQLite/文件故障必须显式报告；只修复有明确 owner 的目标文件，不允许用 MySQL 副本静默覆盖本地权威。

### 14.2 `mysql` 模式

- MySQL 不可用时，耐久写入相关功能进入 failed/read-only；
- 不把事件只写内存后伪装成功；
- 不自动回退到旧 SQLite；
- 恢复连接后按原 MySQL generation 继续运行。

自动回退被禁止，不是因为本地方案不可用，而是因为故障瞬间无法证明两个副本仍处于同一 frontier。后端切换必须是用户明确操作，并经过同步、校验和 generation 更新。

### 14.3 可选受限应急 Journal

- 本地只追加 Journal 保存完整 ingress 命令/事件信封、幂等身份、active generation、authority epoch 和 owner；
- Journal 只是尚未提交到 MySQL 的 ingress spool，不是可查询第二主库，不运行独立 Life Memory 投影；
- 只写入 Journal 不代表耐久业务成功：不得据此推进消费游标、形成 Experience/Witness/主体文档版本、宣称消息已耐久处理，或继续执行依赖该提交的外部副作用；
- MySQL 恢复后按顺序写入，确认权威提交后才推进 Journal replay cursor，并按原业务合同继续后续处理；
- 同身份异内容阻断并保留冲突；
- Journal 按私密权威数据实施访问控制、加密、备份和保留策略；未补传数据包含在本地备份中。

## 15. 迁移策略

### 15.1 总体原则

采用“**只读审计 → 一致性快照 → 复制到 MySQL → 逐记录校验 → 可选切换 → 永久保留源**”，而不是移动数据或长期无约束业务双写。

迁移工具必须满足：

- 对源 SQLite、Markdown、JSON、JSONL、Chroma 和媒体目录只有读取权限；
- 所有目标写入带 migration run id、source identity、source frontier 和 payload hash；
- 重复迁移幂等；同 ID 异内容进入冲突表并阻断切换；
- MySQL 迁移失败不改变当前本地运行状态；
- 校验失败保留目标数据和审计证据，但目标不得被标记为可运行 generation；
- 源文件在迁移成功、切换成功、长期验收完成后仍不删除。

跨 MySQL/SQLite 业务双写很难证明原子性。需要持续追平时，应以当前 active backend 的不可变事件/Outbox 为唯一复制源，并为目标副本维护独立 replication cursor；复制器不能把目标端派生记录反向送回源端形成回环。

### 15.2 分阶段写权限与防回环

每个数据域只允许一个 writer authority。阶段配置必须显式登记：

| 阶段         | 本地 SQLite/文件       | MySQL 领域表             | 复制器                        | 文件投影器              |
| ---------- | ------------------ | --------------------- | -------------------------- | ------------------ |
| 本地正常运行     | 唯一可写权威             | 不要求存在或只读历史副本          | 默认关闭                       | 按本地合同运行            |
| 一致性快照/首次复制 | 活动 writer 暂停；源文件只读 | 迁移器写目标 schema         | 单向 local → mysql           | 不覆盖原工作区            |
| MySQL 影子验证 | 恢复为唯一可写权威          | 只接受隔离验证或单向追平          | 只消费本地权威事件                  | 使用独立测试投影目录         |
| 选择 MySQL 后 | 永久保留的历史快照，不接受生产写   | 唯一可写权威                | 可按需生成新的本地导出副本，禁止覆盖原文件      | 单向 MySQL → 新受管投影目录 |
| 反向导出准备     | 原始快照保持不变           | writer 暂停并固定 frontier | 单向 mysql → 新 local 副本      | 生成新工作区目录           |
| 选择新本地副本后   | 新副本成为唯一可写权威；旧快照仍保留 | 只读历史副本                | 默认关闭或单向 local → mysql 重新追平 | 按新本地合同运行           |

切换配置必须携带 schema version、backend generation、cutover frontier、source freeze manifest、authority epoch 和 fencing token 取得方式。authority registry 必须位于旧、新后端之外的受协调控制面，或使用能在切换期间保持唯一性的外部一致性服务；不能把“谁是 active backend”的最终判断分别保存在两个可能分叉的业务后端中。签发新 epoch 前必须撤销旧租约并验证所有已知 writer，无法隔离的离线节点视为阻断项。任何非 active backend 产生未登记业务写入都视为事故；系统必须拒绝按时间戳或“最后写入”自动合并。

现有《统一记忆归档架构》《记忆跨节点共享契约》和《生命记忆系统》中“SQLite 当前权威”的结论，在 `local` 模式下继续成立；在 `mysql` 模式下由 MySQL 成为该运行 generation 的权威。架构文档必须按“后端选择决定运行权威”统一表述，而不是永久宣布某个物理后端为唯一合法方案。

### 15.3 阶段 0：决策与性能基线

交付：

- 当前全部 SQLite 表、文件类型、调用者和读写频率清单；
- 真实数据量、最大行、最大文本、索引选择性与热点查询；
- SQLite/文件现状基准；
- MySQL 目标 SLO 和容量预算；
- 权威、投影、缓存、媒体和工程资产分类表。

验收门：所有当前持久化入口都有 owner，未分类数据不能进入正式迁移。

### 15.4 阶段 1：统一存储基座

交付：

- storage contracts、Unit of Work、MySQL engine；
- schema migration runner 与版本锁；
- 凭据、TLS、连接池、超时和健康检查；
- 通用幂等、payload hash、cursor 与 conflict primitives；
- 完整 local backend adapter；
- 完整 MySQL backend adapter；
- backend factory、generation guard、复制/导出/校验工具；
- 对现有 `LifeMemoryService` 的拆分计划：先把 `sqlite3.Connection`、FTS5、SQLite UDF、线程池和领域逻辑分离，再逐个替换 repository；禁止在一个提交中整体重写记忆服务。

验收门：插件业务代码可在测试中替换后端，不直接依赖 `sqlite3.Connection` 或 MySQL session；同一领域合同测试可分别运行在 local 和 MySQL 实现上，两种后端均可独立完成启动、读写、重启和恢复。

### 15.5 阶段 2：Presence 与 World Projection

2026-08-04 交付状态：两域已通过 backend-neutral async Port 接入
`LifeEngineService` 唯一持有的 `StorageBackendRuntime`；候选快照已复制到远程
MySQL 并完成独立只读审计与反向导出。Presence 35 个实例、895 条 lifecycle
outbox，World 108 条断言、983 条变化、7 个 perception cursor 与 frontier
86094 均保真。源、MySQL 与反向导出聚合根一致。由于源未冻结，复制批次保持
`copied` 且 generation 不可激活。

先迁移工程语义最清晰的数据域：

1. Presence、lease、stream owner、outbox；
2. World Projection 和 projector cursor。

验收门：并发 CAS、唯一 owner、重启恢复、重复 start/stop、投影重建和数据库中断行为全部通过。

### 15.6 阶段 3：Life Event 主干

交付：

- MySQL RawEventStore；
- occurrence 幂等、全局位置、consumer offset；
- 原有共享事件 Outbox 合并或桥接；
- JSONL 仅保留导出/诊断用途。

复制与选择 MySQL 时：

- 临时冻结源写入并创建 SQLite 一致性快照；
- 从快照按 ingest_position 复制，不修改活动源和原数据库文件；
- 比较每条 occurrence、payload hash 和顺序；
- 校验所有 consumer offset 不越界；
- 只有切换为 `mysql` 后，新事件才只写 MySQL；继续选择 `local` 时，新事件仍写本地账本。

验收门：重放、缺口、冲突、模型失败不推进游标和事务 Outbox 全部通过。

### 15.7 阶段 4：主体文档版本

2026-08-04 交付状态：SubjectDocumentStore 的 local/MySQL 适配、精确字节
版本、revision/head CAS、不可变 head event、带租约 projection outbox、受控
文件观察与 parent-hash 工作区投影已实现。在线候选快照的 1,404 份声明主体
文档已完成 MySQL shadow 复制与反向导出，逐文件 0 差异；由于
`writer_frozen=false` 且远端账号不能创建不可变 trigger，该副本保持不可激活。
现役服务已从同一个 runtime 注入 Subject store/observer/projector；声明主体文件
通过 file 工具和 Memory Witness 时先追加不可变版本，再安全投影到工作区。
启用 selected storage 时通用 shell 明确关闭，避免间接命令绕过写前入账。
`memory_artifact_versions` 的历史文本版本已在 Memory 复制中独立保真，仍不得把
历史 TEXT 伪称为已经恢复的原始字节。

交付：

- SubjectDocumentStore；
- 文档 version/head/derivation；
- actor 授权与 CAS；
- 工作区文件投影器；
- 外部文件变化观察与受控导入。

迁移时从现有 `memory_artifact_versions` 和当前文件交叉校验：

- 已有版本历史优先作为历史证据；
- 当前文件字节必须与已知 head 对比；
- 不一致时追加明确的迁移观察记录，不擅自判断哪个版本更“正确”；
- 不删除任何旧文件或 SQLite。

### 15.8 阶段 5：Life Memory 权威账本

按依赖顺序迁移：

1. Experience / aliases；
2. Witness / sources / state；
3. Artifact versions / derivations；
4. Interpretation / sources / semantic relation；
5. Claim / evidence / belief / conflict / state event；
6. Recall / corecall；
7. legacy nodes/edges/corrections 兼容数据。

每一类迁移均校验：主键、稳定身份、内容 hash、引用完整性、时间字段和追加保护。

### 15.9 阶段 6：重建检索投影

- local backend 继续使用经合同封装的 FTS5；MySQL backend 从 MySQL 领域内容建立 FULLTEXT 或经验证的等价词法投影。
- 不把旧 Chroma 复制品视为唯一证据；为每个 backend generation 建立或校验对应 collection，并从该 generation 的权威内容重新 embedding。
- 对固定查询集比较两种后端的召回、来源、排序稳定性和延迟。
- 只有目标投影追平对应 generation 的权威 frontier 后才切换该后端的检索流量。

### 15.10 阶段 7：运行上下文与其他耐久状态

迁移 rolling context、runtime context、TODO、计划、自主意向、Life Trace 和已登记插件状态。每项先判断：

- 是否不可变历史；
- 是否版本化当前状态；
- 是否可重建投影；
- 是否只是日志或缓存。

禁止用一个通用 `key/value JSON` 表长期吞掉全部语义。

### 15.11 阶段 8：生成可选择的已验证后端

必须由用户批准维护窗口：

1. 用户手工停止 Elysium；
2. 停止全部领域 writer、等待已持有事务完成，分别用 SQLite Online Backup API 固化每个 WAL 数据库，并在同一停写窗口以二进制方式读取工作区文件；
3. 生成包含每个源文件 SHA-256、SQLite logical frontier、表 root hash、文件字节 hash、mtime 观察值和 writer freeze 状态的统一 manifest；
4. 执行最终增量复制；
5. 校验记录数、稳定身份、hash root、引用与 frontier；
6. 为 local 与 MySQL 分别签发可运行的 backend generation；
7. 默认保持用户原来选择的后端，不由迁移器自动改配置；
8. 用户如选择 MySQL，再手工将 `authoritative_backend` 和 `backend_generation` 改为已验证值并启动；
9. 执行真实聊天、记忆形成、检索、文档版本和重启冒烟；
10. 原数据文件、快照、迁移 manifest 与 MySQL 副本全部长期保留，不删除、不移动。

## 16. 回滚、回切与副本保留

复制或影子验证阶段失败：

- 当前 active backend 完全不变；
- 目标测试 schema 可隔离封存或清理，但迁移审计和失败证据必须保留；
- 原 SQLite、文件、JSON/JSONL、Chroma 和媒体文件不受影响；
- 修复后可以用同一 migration run 幂等续传，或创建新的 run。

选择 MySQL 后如果尚无新写入，且 generation/frontier 与已验证本地副本一致，可以在用户确认后切回该本地副本。

选择 MySQL 后如果已有新写入，不能把配置直接指回迁移前旧 SQLite，因为它只是历史快照。必须：

1. 停止写入；
2. 备份 MySQL；
3. 确定切换后的新增记录范围与最终 frontier；
4. 使用受审计的反向导出创建全新的 SQLite/文件目录；
5. 比较 identity、payload hash、版本谱系、引用与 frontier；
6. 签发新的 local generation；
7. 用户确认后将配置切到新副本。

整个过程不得删除或覆盖迁移前原文件、MySQL 数据或中间 manifest。主体文档回切只能导出可信历史版本的原字节，不能由迁移器生成新语义。

## 17. 性能设计与基准

### 17.1 必须测量的查询

- 按 occurrence/event ID 点查；
- 按 consumer cursor 顺序读取批次；
- 最近 N 条 Experience/Witness；
- 文档 head 与历史分页；
- Claim + Evidence + 来源装配；
- Presence lease acquire/renew；
- World assertion 按 subject/source/time 查询；
- FULLTEXT 候选召回；
- Chroma 候选召回；
- 统一检索的候选批量正文装配；
- 事件追加 + Experience + Outbox 事务吞吐。

### 17.2 基准方法

- 使用生产数据量的匿名化快照或等规模合成数据；
- 冷缓存、热缓存分别测量；
- 单并发、典型并发和峰值并发分别测量；
- 记录 P50/P95/P99、吞吐、锁等待、死锁率、连接池排队、扫描行数和 Python 反序列化时间；
- 同时记录 buffer pool 命中、索引体积、大 BLOB 读取放大、FULLTEXT/Chroma backlog 和全量重建耗时；
- 对比当前 SQLite/文件基线，而不是只看 MySQL 单边数字；
- 查询计划必须进入验收记录，禁止用全表扫描获得“测试通过”。

### 17.3 建议验收目标

目标值应在阶段 0 根据硬件和真实负载冻结。初始建议：

- 热缓存 identity 点查 P95 < 10 ms（不含应用外部网络）；
- 文档 head 元数据点查 P95 < 10 ms；
- 100 条顺序事件批量读取 P95 < 30 ms；
- Presence CAS P95 < 20 ms；
- 不含 embedding 的统一检索数据库部分 P95 < 100 ms；
- 典型并发下无无界锁等待，失败有明确超时；
- 投影 backlog 可观测且不会阻塞权威写入。

这些是工程目标，不是未经实测的完成结论。

## 18. 测试矩阵

### 18.1 合同测试

每个 Storage Port 对完整 local implementation、MySQL implementation 和内存 fake 运行同一组行为测试：

- 幂等写入；
- 同 ID 异内容冲突；
- 事务回滚；
- CAS 失败；
- cursor 单调；
- 不可变保护；
- 分页稳定；
- 超时与取消传播。

### 18.2 迁移测试

- 空库、已有空 schema、非空目标拒绝；
- SQLite WAL 一致性快照；
- 行数、稳定身份、逐记录 hash、域 root hash；
- 中文、emoji、NUL/特殊字节、不同换行和超长文本；
- SQLite dynamic typing 到 MySQL 严格类型的逐列映射，NaN/Infinity、非法日期、超长 ID 和大小写碰撞必须在接触目标库前显式报告；
- parent 缺失、orphan、历史缺口和越界字符串；
- 中途崩溃、重复执行和恢复继续；
- 迁移后旧源不被修改。

### 18.3 生命周期与并发

- 重复 start/stop；
- 部分初始化失败；
- 连接池耗尽；
- 数据库断开与恢复；
- lease 竞争；
- 两个实例并发更新同一文档 head；
- 多消费者独立游标；
- 取消和关闭顺序。

### 18.4 主体性与记忆不变量

- 非主体 actor 不能直接写主体语义；
- 外部建议保持建议身份；
- 模板升级不覆盖已实例化主体内容；
- 迁移前后文档字节 hash 完全一致；
- 冲突观点并列保留；
- rank 不改变 claim 状态；
- 删除 FULLTEXT/Chroma/head/association 后可重建；
- 恢复不会创造新语义。

### 18.5 真实端到端

切换后至少实际验证：

- 飞书聊天消息写入与回复；
- 新 Life Event、Experience 和 consumer offset；
- 主体授权产生的新文档版本；
- 记忆词法 + 向量检索；
- 进程重启后身份、游标和上下文连续；
- MySQL 短暂中断的明确失败或应急 Journal 行为；
- Chroma 删除后隔离重建；
- MySQL 备份恢复到新库并逐记录复核。

## 19. 备份与恢复

- local 模式对每个 SQLite 使用 Online Backup API，并在统一停写窗口保存主体文件、JSON/JSONL、Chroma marker 和媒体 hash manifest；
- MySQL 模式使用事务一致性全量快照 + binlog 时间点恢复；
- 备份账号与运行账号分离；
- 每次备份保存 SHA-256、schema version、backend generation 和 ledger frontier；
- 定期恢复到隔离环境并运行领域校验，不能只验证压缩包可解压；
- Chroma 从对应 active backend 重建，不作为唯一备份；
- 媒体对象有独立 hash 清单和备份；
- MySQL 模式的文件兼容投影可从 subject document versions 重建；
- 原 SQLite、工作区文件、JSON/JSONL、Chroma、媒体目录和迁移 manifest 永久保留；任何清理都不属于本重构方案。

## 20. 安全与权限

至少拆分账号：

- schema migration：临时 DDL 权限；
- runtime：领域表最小读写权限；
- projection worker：读取权威内容、写投影状态；
- read API：授权只读；
- backup：快照/binlog 所需权限。

前端和外部协作者不得直连数据库。健康、日志和异常不得输出密码、完整 URL、私聊原文或主体内容。

## 21. 与现有实现的关系

### 21.1 可复用

- Core MySQL engine、asyncmy、连接池与方言处理；
- Core 13 表迁移和指纹校验经验；
- `src/kernel/sync` 的稳定身份、Outbox、Inbox、cursor 和 conflict 模型；
- 统一记忆归档的规范化记录、hash root、隔离恢复和审计思路；
- 当前 Life Memory 对不可变历史、主体版本和可重建投影的领域定义。

### 21.2 不能直接当运行模型复用

- `elysium_memory_archive_records` 是统一灾备记录，不适合作为所有运行查询的万能表；
- 归档 `heads` 不等于领域 artifact heads；
- `authority` 物理列承载 archive role，不能与认识论 authority 混用；
- 当前 `shared_sync` 只复制显式授权 Life Event，不等于完整生命域权威库；
- 现有 SQLite SQL 中的 FTS5、`BEGIN IMMEDIATE`、`PRAGMA`、`INSERT OR IGNORE` 和 trigger 必须逐项重写，不能机械替换 URL。

## 22. 建议实施顺序

按风险与收益排序：

1. 存储合同、backend factory、generation guard 和性能基线；
2. 完整保留并适配现有 local backend；
3. MySQL schema、迁移基座、复制/校验/反向导出工具；
4. Presence；
5. World Projection；
6. Life Event + consumer cursor + Outbox；
7. Subject Document Store；
8. Experience / Witness；
9. Artifact / Interpretation / Semantic Relation；
10. Claim / Evidence / Epistemic ledger；
11. Recall / Corecall；
12. 两种后端各自的词法检索与 Chroma 重建；
13. 运行上下文和其他已登记状态；
14. 生成 local/MySQL 两个已验证 generation；
15. 按用户配置选择运行后端；
16. 可选应急 Journal。

不建议首先迁移 Life Memory 全部表，也不建议先把所有文件塞入一个 JSON/BLOB 表。先完成存储合同、generation 和事件主干，后续迁移才有可证明的身份、事务、复制和回切基础。

## 23. 完成定义

只有同时满足以下条件，才能宣称“可选 MySQL / 本地存储重构完成”：

- 现役领域服务不再直接绑定 SQLite、MySQL 或文件 API，全部通过 Storage Ports；
- local backend 保持现有功能并通过合同、并发、重启、故障和恢复测试；
- MySQL backend 覆盖全部已登记的非媒体耐久领域记录，并通过同一组合同测试；
- 本地到 MySQL 的复制通过 identity/hash/frontier/版本谱系校验，且源数据未被移动、删除或修改；
- MySQL 到新本地副本的反向导出经过隔离恢复和同等级校验；
- `authoritative_backend = "local" | "mysql"` 均可独立启动和运行；
- generation guard 能阻止选择落后、未校验或 schema 不兼容的后端；
- 任一运行时只有一个可写权威，复制器和投影器不会形成写入回环；
- 主体文档原字节、actor、source、occurrence 和版本谱系完整；
- 两种后端的词法检索与 Chroma 均可从各自 active backend 重建并追平 frontier；
- 真实飞书聊天、记忆形成、检索和重启连续性分别在 local 与 MySQL 模式完成端到端验收；
- MySQL 备份和本地备份均能恢复到隔离环境并通过领域级内容校验；
- 原 SQLite、Markdown、JSON/JSONL、Chroma、媒体文件和迁移 manifest 全部保留；
- `deployment_and_usage.md`、迁移手册、回切手册和健康检查同步更新；
- 用户在维护窗口手工选择后端并启动，不由 agent 自动重启。

## 24. 重构后的数据存储架构总结

重构后不是“所有场景强制使用 MySQL”，而是形成一个统一领域模型、两套可选择的耐久实现：

```text
                  同一组领域服务与 Storage Ports
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
      local backend                    MySQL backend
  SQLite + 原主体文件              MySQL 领域表 + 文档版本
              │                               │
              └───────────────┬───────────────┘
                              ▼
                 统一投影与对象管理边界
        词法检索 / Chroma / 工作区投影 / 媒体存储
```

关键性质：

- 业务代码、意识逻辑和记忆语义不感知具体数据库；
- 配置决定当前 active backend；
- local 与 MySQL 都能独立承担完整运行；
- 数据迁移是带审计的复制，不改变或删除源；
- MySQL 模式适合集中共享、多实例并发和统一查询；
- local 模式适合单机、离线、低运维和直接文件查看；
- Chroma 始终只负责向量投影；
- 媒体字节仍在受管理文件或对象存储，关系后端管理身份与元数据。

## 25. 期望实现的功能与效果

### 25.1 可达成的功能

1. **一份代码支持两套存储方案**：不再维护两条彼此分叉的业务逻辑，只替换存储 adapter。
2. **完整本地到 MySQL 复制**：Core、Life Event、Life Memory、Presence、World、主体文档、运行上下文及已登记插件状态都可复制并校验。
3. **保留并恢复原方案**：用户可以继续选择本地方案；MySQL 运行产生新数据后，也可导出到新的本地副本再回切。
4. **多实例共享同一生命域数据**：聊天、直播、语音、游戏、应用后端可在授权与隔离合同下读取同一 MySQL generation。
5. **统一跨域查询**：按时间、actor、source、occurrence、意识实例、stream 和版本关系查询事件与记忆，不再人工扫描多个文件库。
6. **并发协调**：Presence lease、stream owner、revision、consumer cursor 和 outbox 使用行锁/CAS/事务协调多实例。
7. **主体文档版本查询**：可以点查当前 `SOUL.md` head、分页读取历史、校验原字节、来源和 actor，并恢复任一可信版本。
8. **投影可重建**：Chroma、词法索引、World Projection、关联视图和工作区兼容文件可以从 active backend 重建。
9. **可验证迁移与回切**：每次复制、导出和切换都有 migration run、manifest、generation、frontier、hash root 和冲突记录。
10. **统一备份与审计**：MySQL 模式获得事务快照/binlog 能力，本地模式继续拥有文件级备份；两者都能做隔离恢复验证。

### 25.2 期望效果

- MySQL 模式下，多进程并发写入不再受单个 SQLite writer 锁模型限制；
- 读取层可以通过组合索引、批量装配和分页减少 Python 扫描与多文件拼接；
- 多节点看到同一已提交 generation，减少本地副本之间的同步歧义；
- 主体文档和长期记忆获得可查询、可审计、可恢复的版本谱系；
- 原本地运行能力不丢失，数据库维护或单机部署时仍有明确选择；
- 迁移风险被限制为“新增副本是否可用”，而不是“原数据是否被搬走或破坏”；
- 后端差异由合同测试和性能基准暴露，不靠调用者猜测。

这些是设计目标。是否达到更低延迟、更高吞吐或更好召回，必须由第 17、18 节的真实基准和端到端测试证明。

## 26. 如何使用新的数据存储架构

### 26.1 新安装或继续使用本地模式

- 配置 `authoritative_backend = "local"`；
- 指定本地 Core、Life Workspace、Presence 和 World 数据路径；
- 运行 schema/manifest 检查；
- 启动后仅本地后端可写；
- Chroma 从本地权威内容维护投影。

### 26.2 首次启用 MySQL 模式

- 先保持本地模式；
- 在维护窗口创建一致性快照；
- 执行 `copy_verify` 到 MySQL；
- 查看迁移报告并确认冲突为零、hash/frontier 一致；
- 将配置切换到报告签发的 MySQL generation；
- 手工启动并完成冒烟、重启和真实端到端验收；
- 原数据文件继续原地保留。

### 26.3 在 MySQL 模式日常运行

- MySQL 是当前 generation 的唯一业务写入端；
- Chroma、词法索引和工作区文件由 projection outbox 更新；
- 应用后端和其他意识实例通过领域 API/Storage Ports 访问，不直连并绕过授权；
- 监控连接池、锁等待、投影 backlog、consumer lag、schema version 和备份状态。

### 26.4 重新选择本地模式

- 如果两个 generation 完全一致，可选择已验证本地副本；
- 如果 MySQL 已有新写入，先导出到新的本地目录并校验；
- 配置指向新 local generation；
- 原始本地快照和 MySQL 数据都继续保留；
- 禁止直接覆盖迁移前文件或在运行中热切换。

## 27. 原方案与重构方案的优缺点评估

| 维度      | 原本地 SQLite/文件方案           | 重构后的可选双后端方案                           |
| ------- | ------------------------- | ------------------------------------- |
| 单机部署    | 优：无需数据库服务，配置少             | local 模式保持同等能力；框架本身更复杂                |
| 小型热点点查  | 优：调用路径短，OS 页缓存有效          | MySQL 未必更快；可通过缓存和索引接近或超过              |
| 多进程并发写  | 缺：SQLite writer 串行与锁竞争更明显 | 优：MySQL 行锁、MVCC、连接池和事务并发              |
| 多节点共享   | 缺：需要复制、同步和冲突处理            | 优：MySQL 模式直接共享已提交数据                   |
| 跨域查询    | 缺：数据散落，常需应用层拼接            | 优：结构化索引、分页、关联和批量装配                    |
| 主体文件可读性 | 优：人和编辑器可直接查看              | local 保留；MySQL 模式需兼容投影或查看工具           |
| 离线能力    | 优：天然本机可用                  | local 同样可用；MySQL 模式中断时需只读/失败或 Journal |
| 备份简单度   | 小规模时文件复制直观，但多库一致性复杂       | MySQL 可时间点恢复；同时需维护数据库运维能力             |
| 数据一致性   | 单库事务清晰，跨多个库/文件协调困难        | MySQL 模式可扩大事务边界；双后端切换需 generation 管理  |
| 故障影响范围  | 局部文件故障可能局部影响              | 集中 MySQL 故障影响更集中，必须有高可用与备份            |
| 运维成本    | 低                         | 高：schema、权限、连接池、监控、备份、恢复              |
| 迁移安全    | 无迁移风险，但共享能力有限             | 复制不移动且永久保留源，风险可控；实现和验证成本高             |
| 可替换性    | 当前业务与 SQLite/文件耦合较深       | 优：Storage Ports 让后端可替换、可测试            |
| 查询语义    | FTS5 等现有行为稳定              | FULLTEXT 结果可能不同，必须做检索质量回归             |

### 27.1 原方案的核心优势

- 简单、稳定、单机低依赖；
- 小数据量和单进程下通常有很低的固定开销；
- 主体文件可直接查看，问题定位直观；
- 数据库服务故障不会影响本地运行；
- 现有实现和真实数据已经长期围绕它工作。

### 27.2 原方案的核心缺点

- 多个 SQLite、Markdown、JSON/JSONL 和投影形成分散持久化边界；
- 多实例共享需要额外同步层；
- 跨库事务、统一备份和全局查询困难；
- SQLite 写并发和多个 writer 协调能力有限；
- 业务代码与具体存储 API 耦合，后端替换成本高。

### 27.3 重构方案的核心优势

- 不牺牲原本地方案，同时新增完整 MySQL 运行能力；
- 数据复制而非搬迁，原始数据安全边界更强；
- 多实例共享、并发协调、统一查询与集中运维能力显著增强；
- 存储合同使领域逻辑、主体语义和物理存储解耦；
- generation 和 manifest 让迁移、选择、回切和恢复可证明；
- MySQL 成为能力选项，而不是不可逆依赖。

### 27.4 重构方案的核心缺点

- 实现范围大，尤其 Life Memory、FTS5、文档版本和运行上下文；
- 两套一等后端意味着长期合同测试和 schema 演进成本；
- MySQL 模式增加服务运维、权限、备份、容量和故障面；
- local/MySQL 检索实现可能产生召回与排序差异；
- 选择错误 generation 或不按流程回切会造成历史分叉，因此启动保护不能省略；
- 如果未来长期只用一个后端，另一实现仍需维护，否则“可选择”会逐渐失真。

## 28. 重构是否属于进步

**结论：在满足明确验收门的前提下，这是架构进步；如果只完成数据复制或只增加配置开关，则不是。**

它的进步不应以“所有查询必然更快”衡量，而应体现在：

- 从物理存储耦合提升为领域合同与可替换后端；
- 从不可逆迁移提升为复制、校验、选择和可审计回切；
- 从分散单机数据提升为可选的集中共享与并发协调；
- 从依赖路径和文件状态提升为稳定身份、版本、hash、frontier 和 generation；
- 在获得 MySQL 能力的同时保留原本地部署与原始数据。

以下情况会使重构退步：

- 为了统一而把所有领域塞入万能 JSON/BLOB 表；
- 删除或覆盖原数据；
- 同时让 local 与 MySQL 接受独立业务写入；
- MySQL 查询计划、延迟和检索质量未达标仍强制切换；
- local backend 因缺少持续测试而退化成名义兼容；
- 把 Chroma、World Projection 或归档副本误作主体历史权威；
- 增加大量复杂度却没有真实多实例、共享查询或运维需求。

因此，合理判断是：

- 对当前单机、低并发、只需本地运行的场景，原方案仍可能是性价比最高的选择；
- 对未来多意识实例、多设备、应用后端、远端共享、集中查询和并发写入，MySQL 模式是实质性能力提升；
- 可选双后端比强制 MySQL 更合理，因为它让用户按部署目标选择复杂度，并保留完整退路。

## 29. 最终建议

最终形态应是：

```text
可配置运行权威：local 或 MySQL，任一时刻只能选一个
local：SQLite + 原主体文件，完整保留并持续支持
MySQL 8：全部非媒体耐久记录与主体文档版本的可选集中后端
Chroma：从 active backend 重建的向量投影
词法检索：local 使用 FTS5，MySQL 使用 FULLTEXT 或经验证的等价投影
媒体存储：原媒体目录或对象存储保存字节，active backend 保存元数据
迁移：只复制、校验、签发 generation，永不移动或删除源
回切：有新数据时导出到新副本，永不覆盖原始快照
```

这套方案既能获得 MySQL 的共享、并发、统一查询和事务能力，又保留本地方案的简洁、离线能力和原始数据安全。真正实施时，应把“local 与 MySQL 同合同、复制不破坏源、单 active writer、generation 防误切”视为四个不可妥协的完成条件。
