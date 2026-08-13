# 活体记忆迁移与健康检查

> 适用版本：2026-08-02 起的“长河·活忆闭环”。
> 原则：迁移只追加权威历史或重建派生投影，不删除旧日记、旧 JSONL、旧边或记忆文件。

## 1. 启动策略

Elysium 由用户手工启动，不得为它新建或恢复 systemd service、Windows 计划任务、登录启动项、shell profile 启动命令或后台守护拉起。NapCat/QQNT 可以由具有明确 owner 的部署机制自动启动和自动恢复；本迁移工具不拥有或操作其进程生命周期。

本地 New API 中转站是模型基础设施，按当前机器约定需要自动启动；它不属于 Elysium 自启动禁令。记忆系统的请求重试仍不负责拉起该进程，启动与健康由中转站自己的生命周期配置负责。

推荐顺序：

1. 确认自动启动的本地 New API 已监听并可访问；
2. 需要 QQ 时手工启动 NapCat；
3. 在实际仓库根目录执行 `deploy.sh doctor`，通过后由用户前台执行 `deploy.sh run`（Windows 使用 `deploy.ps1`）；
4. 从启动日志确认 raw ledger、memory schema、index recovery 和 memory witness 完成初始化。

## 2. 首次升级会发生什么

首次由新代码打开工作区时会自动执行幂等迁移：

1. `life_events.jsonl` 与现存轮转归档按时间顺序导入 `life_events.sqlite3`；
2. 建立 durable occurrence、ingest position 和 consumer offset 表；
3. 在 `.memory/memory.db` 中增加 artifact、interpretation、semantic relation、recall 和 co-recall 表；
4. 为已有 interpretation/claim 补 FTS 投影；
5. 为现有记忆文档建立 `startup_baseline`；
6. 若活动 Chroma 集合 marker 指向缺失或不兼容集合，废弃 marker 并重新入队文档；
7. memory witness 从 durable offset 继续，不再通过“跳到最早保留事件”绕过缺口。

迁移标记和稳定 identity 保证重复启动不会重复导入同一 occurrence、同一文件 baseline 或同一学习证据。

## 3. 迁移前检查

在前台停止 Elysium 后：

```bash
cd /root/Elysia/Elysium
git status --short
du -sh data/life_engine_workspace
find data/life_engine_workspace -maxdepth 3 -type f \
  \( -name 'memory.db*' -o -name 'life_events.sqlite3*' -o -name 'life_events*.jsonl*' \) \
  -printf '%p %s bytes\n'
```

推荐使用 SQLite 在线备份 API或在进程完全停止后同时备份 DB、WAL 和 SHM。不要只复制正在写入的主 DB 文件，也不要删除旧 JSONL 来“节省迁移时间”。

## 4. 启动后健康检查

记忆 HTTP 路由启用时读取：

```text
GET /api/health
```

重点字段：

| 字段 | 健康含义 |
| --- | --- |
| `sqlite.integrity_ok` | SQLite 完整性检查通过 |
| `sqlite.foreign_key_check_count` | 应为 0 |
| `index.coverage` | 工作区文档索引覆盖率 |
| `outbox.pending/processing/failed` | 向量索引恢复进度与失败量 |
| `vector_degraded` | 启用 vector 后，活动 projection 是否不可用；显式关闭时应为 `false` |
| `vector.expected` / `vector.disabled` | 区分“配置关闭”与“应该存在但损坏” |
| `living_memory.artifact_head_mismatch_count` | 应为 0 |
| `living_memory.invalid_corecall_payload_count` | 应为 0 |
| `living_memory.association_projection_drift` | 应为 `false` |
| `living_memory.claims_without_evidence` | 质量指标，不要求机械归零；需逐条判断 |

Life Engine 的轻量 health 在事件总线已创建后还包含：

- raw event 最早/最新 ingest position；
- legacy import issue 数；
- memory witness 等消费者的位置与 lag。

consumer lag 可以暂时非零；首次导入大量旧 JSONL 后，见证意识会按配置的批量和间隔逐步追赶，不能直接跳到尾部。判断是否健康要连续观察：lag 应总体下降，或至少其消费速度长期不低于新事件产生速度。若上游模型/投影失败，offset 应保持不动；恢复后即使对应 Experience 已在账本中，也必须重试同一见证窗口。`import_issue_count > 0` 必须查看 `raw_event_import_issues`，不能静默忽略。

## 5. 显式修复

### 5.1 共同回忆投影漂移

`memory_association_projection` 是派生表，可以从 `memory_corecall_events` 完整重建。通过服务的 `rebuild_memory_association_projection()` 执行；重建前后不可变超边数量不应变化。

### 5.2 向量集合丢失

无需删除记忆数据库。下一次手工启动会清除失效的活动 marker，把全部活动文档重新入队，并由 worker 创建当前模型/维度的新集合。保留旧 Chroma 目录，直到新集合完成并通过 health 对比。

### 5.3 raw event 导入问题

先查询问题，不修改 consumer offset：

```sql
SELECT source_path, line_number, error_type, detail, recorded_at
FROM raw_event_import_issues
ORDER BY issue_id;
```

如果源 JSONL 仍在，修复副本后应使用专门迁移工具追加缺失 occurrence。不得直接把 witness offset 改到尾部；那会把“未见证的历史”伪装成“已处理”。

### 5.4 文件版本

人工修改文件后无需伪造 tool trace。下一次手工启动会追加 `workspace_change_observed` derivation。人工删除会追加 tombstone；恢复文件后会从 tombstone 继续。tombstone 不应被当作文档内容送入普通全文检索。

## 6. 回滚

代码回滚不会自动删除新表。旧代码可继续忽略附加表，但可能只读取 JSONL 镜像和旧索引，因此不能宣称具备完整历史。

如果必须暂时回滚：

1. 停止进程；
2. 保留 `life_events.sqlite3`、`.memory/memory.db`、WAL/SHM、JSONL 与 Chroma；
3. 记录回滚时间；
4. 恢复新版本后让 occurrence 幂等导入补齐期间镜像事件；
5. 检查 consumer lag、artifact history 和 association projection。

禁止通过 DROP 新表、清空 offset、删除工作区或重建 `memory.db` 来回滚。

## 7. 验收清单

- [ ] Elysium 没有 systemd/计划任务/登录项自启动；NapCat/QQNT 与 New API 的独立生命周期 owner 健康且未制造重复实例。
- [ ] 同一个事件重放不会产生第二条 raw occurrence。
- [ ] 生产者 sequence 重置后 ingest position 仍继续增长。
- [ ] memory witness offset 单调且缺口时不推进。
- [ ] 当前记忆文件已有 baseline；人工修改后旧/新内容都能查询。
- [ ] interpretation 可按 recorded-as-of 查询并返回来源。
- [ ] corecall projection 重建前后一致。
- [ ] 关联检索记录 seed，重复 seed 可重放。
- [ ] claim、解释和文档不按类型获得硬编码优先级。
- [ ] Chroma 集合缺失时会重新入队，而不是永久卡在 stale marker。
- [ ] `git diff --check`、定向测试和全量测试记录本轮真实结果。
