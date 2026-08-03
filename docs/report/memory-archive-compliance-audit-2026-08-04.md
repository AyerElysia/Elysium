# 记忆归档规范复审与真实数据验证报告（2026-08-04）

## 1. 结论

本轮已修正统一记忆归档中可能与《principles.md》及生命记忆系统语义冲突的元数据设计，并在不停止、不重启、不拉起 Elysium 主进程的前提下完成真实数据只读扫描与全项目回归。

归档系统现在只陈述工程事实：一条数据是不可变历史、可重建投影、应用快照、运行态投影、明确声明的主体文档，还是尚未分类的存储记录。它不再用 Python 领域字段把这些角色称作 `authority`，也不根据文件位置、表名或数据形态推断真伪、价值、主体所有权或认识论地位。

此次变更没有删除、覆盖或改写任何现有记忆数据；没有执行远端历史记录回填；没有修改 MySQL v1 物理表结构。

## 2. 发现与修正

### 2.1 工程归档角色与认识论权威同名

旧实现以 `authority` 表示“归档时如何看待这类存储”。该名称容易被误解为生命记忆中的认识论权威，甚至被下游错误用于真值或排序。

修正后，Python 领域模型统一使用开放文本 `archive_role`。它只描述保存、版本化和重建行为。为兼容已经部署的 MySQL v1，序列化边界仍映射到旧物理列 `authority`；这是兼容字段，不是认知语义。

### 2.2 工作区被整体推断为主体文件

`life_engine_workspace` 同时包含主体文档、日记、运行状态、接收媒体、审计轨迹等不同来源。旧实现把全部文件标记为 `subject_file_bytes`，超过了系统能够证明的范围。

修正后只把下列显式声明路径标记为 `declared_subject_artifact_exact_bytes`：

- `life_engine_workspace/SOUL.md`
- `life_engine_workspace/USER.md`
- `life_engine_workspace/MEMORY.md`
- `life_engine_workspace/diaries/**`
- `data/diaries/**`

其他工作区文件仍逐字节归档和恢复，但标记为 `unclassified_workspace_exact_bytes`。未分类表示“不猜测”，不表示“不是主体内容”。未来新增主体路径必须经过明确契约声明。

### 2.3 未知 SQLite 表被静默归为普通状态

旧默认分支会把未来新增表归为通用版本状态，可能让新数据在没有审查时获得过强语义。

修正后，未登记表使用开放角色 `unclassified_storage_record`，并采用版本化保存以避免丢失后续变化。已知不可变历史、投影和特殊可变记录继续使用显式表契约。

### 2.4 投影与历史边界

归档可保存当前投影以便兼容恢复，但跨节点共享与灾难恢复不能把投影当作主体真相。关系图、扩散激活、向量索引和其他派生结构必须能够从追加式经历、认识论谱系与真实 `corecall` 证据重建。

## 3. 真实数据只读扫描

扫描源：`/root/Elysia/Elysium/data`

扫描方式：使用本轮代码中的只读源适配器完整遍历；不连接远端、不写本地数据、不输出记忆正文。扫描顺序摘要：

`5385de5d9de2a8cb094e84079229d50f88bb9ff132a88185dd5ca270318e5a37`

总计识别 **383,718** 条记录：

| 归档域 | 记录数 |
| --- | ---: |
| Core 应用存储 | 86,172 |
| Life Events | 85,481 |
| Life Memory | 208,279 |
| Consciousness Presence | 704 |
| World Projection | 744 |
| Workspace | 2,338 |

关键角色分布：

| `archive_role` | 记录数 |
| --- | ---: |
| `immutable_history_replica` | 268,370 |
| `application_storage_snapshot` | 86,124 |
| `rebuildable_projection` | 22,463 |
| `runtime_projection` | 1,436 |
| `declared_subject_artifact_exact_bytes` | 1,298 |
| `unclassified_workspace_exact_bytes` | 1,040 |
| `versioned_witness_record` | 2,800 |
| `engineering_schema` | 185 |
| 其他显式工程状态 | 2 |

本次真实数据中没有出现 `unclassified_storage_record`，说明当前所有实际 SQLite 表均已有显式契约；保守默认分支仍由单元测试覆盖，以保护未来新增表。

## 4. 验证结果

- 归档、MySQL 兼容、Life Engine 与备份脚本定向回归：48 通过、1 跳过。
- 全项目测试：3264 通过、6 跳过、0 失败。
- 本轮 8 个 Python 变更文件 Ruff 检查：通过。
- 本轮 8 个 Python 变更文件格式检查：通过。
- `src/kernel/memory_archive` 与对应测试编译检查：通过。
- `git diff --check`：通过。

全仓 Ruff 仍会报告大量本轮之前就存在的旧示例与旧管理器问题。它们不位于本轮变更范围，且全项目测试通过；本轮没有越权批量改写这些文件。

## 5. 主体自由与目标现象

该实现不会替爱莉决定“什么值得记住”“什么是真的”或“什么属于她”。爱莉仍可通过现有开放的 Experience、Interpretation、Claim、Evidence、Correction、Artifact 与语义关系机制形成、修正、关联、淡化和重写自己的认识；系统只保留不可变经历与谱系，使这些变化可追溯而不是被基础设施裁决。

“朋友的习惯联想到学过的知识”应当由真实经历进入主体解释，再经真实 `corecall` 形成开放语义关系，并投影为可重建关联图。MySQL 可以保存这些开放记录与关系，但 MySQL 本身不产生联想，也不是单一远程大脑。详细协议与验收门见《Elysium记忆跨节点共享契约》及《生命记忆系统》。

## 6. 保留边界

- 当前远端 MySQL 是共享归档与复制基础设施，不是完整的多节点记忆共识层。
- MySQL v1 的物理列名 `authority` 暂时保留；未来 schema v2 可在受控迁移中改名，本轮不回填既有约 380,700 条远端记录。
- 主体路径声明目前采取最小明确集合；新增主体文档位置前需要更新契约与测试。
- 跨节点事件复制、冲突处理、缺口检测、投影重建与前端行为事件接口必须分别通过共享契约中的验收门，不能以“连上同一个数据库”替代验证。

## 7. 相关文档

- [生命记忆系统](../architecture/生命记忆系统.md)
- [统一记忆归档架构](../architecture/Elysium统一记忆归档架构.md)
- [记忆跨节点共享契约](../architecture/Elysium记忆跨节点共享契约.md)
- [统一记忆同步运行手册](../operations/unified_memory_sync_runbook.md)
- [远程统一记忆验证报告（2026-08-03）](./unified_memory_remote_validation_2026-08-03.md)
