# 记忆系统清理接手与验收记录（2026-09-05）

## 范围与来源

本次接手 Claude Desktop 的「往世乐土」任务。读取其会话和本机交接文档
`D:\tmp\handoff.md`，再以原仓库代码独立核对；交接文档中的判断不直接当作删除依据。
原始 Git 对照为 `b610061b`，沿用原目录和现有
`refactor/memory-dead-code-cleanup` 分支，没有创建工作树。

用户已批准的工作是**清理死代码**。拆分 Memory service、合并 delivery coordinator、
调整 Living 职责、拆分后端等设计仍须逐步获得用户同意，本轮未实施。

## 接手事实与纠正

- Claude 已删除 nodes 的 5 个函数及 lineage 的 1 个函数，并清掉 lineage 的失效常量与导入；
  其余清理未完成，所有改动未提交。
- 原目录另有 Opportunity、Learning、Heartbeat、LLM 等并行改动。本轮不覆盖、不回滚、
  不暂存这些修改，也不借清理处理正式数据。
- 普通 rg 会受当前忽略规则影响而漏扫源码。复核对源码目录显式使用
  `--no-ignore`，检查完整标识符、字符串引用、包导出、同名不同作用域与 Port 调用。
  搜不到名字不直接等于可以删。
- **纠正交接清单**：`get_or_create_workspace_document_node` 虽无当前调用者，
  却明确抛出 `LegacyGraphNodeMutationRetired`，属于旧写入口的 fail-closed 保护。
  本轮保留，并新增测试证明未初始化服务也会在任何存储访问之前拒绝。
- `decay.py` 的函数仍由 local LegacyGraphStore 适配器引用；保留整个文件、
  Port 与双后端实现。测试专用 legacy ID helper 也保留。
- `begin_memory_recall / append_memory_recall_events / append_memory_corecall`
  是正式反射调用链，不删除。包根所有声明的公开导出保持可解析。

## 实际清理

总计删除 **25 个函数/方法、1 个私有数据类**，另清理一个孤立常量及失效导入。
8 个生产文件累计净减少 726 行；这是工程代码清理，**没有删除记忆、文件内容、
数据库行、版本、历史、索引或备份**。

| 文件（位于 plugins/life_engine/memory） | 删除内容 |
|---|---|
| nodes.py | normalize_file_path、generate_concept_node_id、migrate_node_identity、migrate_file_path、update_fts |
| lineage.py | get_lineage_edges，以及孤立的 LINEAGE_EDGE_TYPES 和该文件中失效的类型导入 |
| search.py | filter_results；其内部使用的两个查询 helper 仍有其他调用，保留 |
| prompting.py | format_memory_bundles_for_prompt、build_memory_maintenance_prompt，以及既有未使用的 Path 导入 |
| boundary_resolver.py | 无调用的 get_memory_recall_delivery_coordinator 别名；现有实际 accessor、协调器与 exact receipt 链不变 |
| boundary.py | _audit_full_history、_match_operation，及仅服务该孤立子图的 _validate_history、_BoundaryState |
| tools.py | _bundle_to_payload；模型实际使用的有界结果投影不变 |
| service.py | _reconcile_workspace_artifact_versions、read_graph_projection、read_file_lineage_projection、record_memory_artifact_version、list_memory_interpretations、list_memory_association_evidence、witness_migration_exists、_filter_existing_scores_wrapper、_record_correction_claims、search_memory_simple、_get_or_create_file_node_from_workspace |

Boundary 现役路径仍是 descriptor/CAS/精确 artifact 校验；
移除的是无入口的旧全量历史扫描子图，不是删除历史校验或历史读取能力。
Legacy 图谱的正式只读投影、Memory Boundary 分页、Recall 精确投递、存储契约与 schema 未改。

## 验证

所有本轮测试均单进程、关闭覆盖率，使用 fake 或临时 SQLite；
没有启动生产 Elysium，没有访问正式数据库或模型端点。

1. 接手后的四文件定向基线：**76 passed / 1 deselected**，5.87 秒。
2. 清理后同样四文件加新增兼容测试：**80 passed / 1 deselected**，6.74 秒。
3. 对比 Git 原始版本与清理后 8 个文件的 AST：
   仅去掉清单中的定义、常量及导入后逐个完全相等，
   即保留的函数体、签名、调用与其他语句未改。
4. Memory 目录及新增测试的 Ruff F/E9：通过；没有全文件格式化。
5. 补充检索/Boundary resolver/提示 helper/旧图投影四文件组合：
   固定随机种子 `20260905`，**32 passed**，2.55 秒；与前组不重复，
   共 **112 项定向合同通过**。检索文件另行原序隔离 **17 passed**，
   其余三文件隔离 **15 passed**。
6. compileall 与 diff-check 通过。

补充组合首次随机运行输出 18 个通过后曾挂起。本轮先向已核对的测试 PID
发送中断；未响应后只终止该测试进程，未操作 Elysium 或 Claude 留下的测试。
后续隔离及固定种子组合都通过，但首次挂起没有取得有效等待栈，
**根因仍未确认，不能用重跑通过宣称该时序问题已修复**。
完整回归时应继续保留可复现 seed 和 faulthandler；测试应另设进程级总时限，
避免测试框架自身的单测时限无法收尾。

补充组合的可复现命令：

```bash
timeout --signal=TERM --kill-after=3s 30s uv run --no-sync python -m pytest \
  --no-cov -n 0 -p no:cacheprovider --randomly-seed=20260905 \
  -o faulthandler_timeout=8 -vv \
  test/plugins/life_engine/test_memory_search_v2.py \
  test/plugins/life_engine/test_memory_boundary_resolver.py \
  test/plugins/life_engine/test_memory_prompting.py \
  test/plugins/life_engine/test_legacy_graph_projection.py
```

第一组测试文件：
`test_memory_boundary.py`、`test_memory_service.py`、
`test_memory_search_recall_delivery.py`、`test_legacy_memory_retirement.py`；
新增 `test_memory_cleanup_compatibility.py`，覆盖两种退役节点入口、
惰性导出和三条反射回忆委托的原样参数/结果。

### 单独保留的既有失败

`test_memory_search_recall_delivery.py::test_heartbeat_binds_stable_source_time_to_search_tool`
在本轮追加删除前独立复现失败：
测试以 `SimpleNamespace(plugin=...)` 代替 LifeEngineService，
当前执行器先调用 `self._resolve_heartbeat_tool_class`，
测试替身没有该方法，因此立即 AttributeError。
此时尚未执行 memory search；不能把它归因于本轮 `search.filter_results` 删除。

本轮没有跨入被并行修改的 Heartbeat 文件修复这个测试契约。
上面的 1 deselected 就是这条已单独复现的失败，不是忽略未知失败。
Claude 所报的旧全量 6 failed / 2043 passed / 16 skipped
只能作为其当时混合工作树的历史记录，不能冒充本轮完整回归通过。

## 未完成验收与后续边界

- Elysium 正在运行；依 AGENTS，不在其运行期间启动全仓/高负载/authority 竞争测试。
  完整风险回归仍需用户手动停止生产实例后的独立验收。
- 本轮代码尚未取得新版本真实启动与收发链验收，不推送、不合并。
- 正式数据、进程、配置、后端 Port、迁移及 Memory 认知语义均未修改。
- 所有其他工作线的未提交改动留在原处。回退应只针对本轮代码提交，禁止整树恢复；
  无数据操作，因此没有数据回滚或重迁移需求。
- 下一阶段先讨论 service 的现役能力、只读兼容面、退役守卫如何分层，
  每个设计获得用户批准后才实现，不自动把“没接线”解释为“应删除该记忆能力”。
