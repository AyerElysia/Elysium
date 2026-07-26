# life_engine 目录重构审查评价

> 审查日期: 2026-04-16
> 审查分支: `refactor/life_engine-directory-structure`
> 基准代码: `bcf4eb1`（重构前最后一次提交）

## 总体评价

重构将 20 个扁平 `.py` 文件拆分成 7 个子包（core/dream/memory/neuromod/service/snn/tools），共 37 个文件。
**结构意图是好的**，但执行质量参差不齐。

| 维度 | 评分 | 说明 |
|------|------|------|
| 目录组织 | ⭐⭐⭐⭐ | 子包划分合理，职责清晰 |
| 导入正确性 | ⭐⭐ | 14 处断裂的相对导入，3 处 static 路径错误 |
| 逻辑等价性 | ⭐⭐ | 3 处 Critical 内存模块 bug、2 处 Critical 服务模块 bug |
| 测试适配 | ⭐⭐ | 所有测试导入路径未更新；2 处 mock 失配 |
| 新功能引入 | ⭐⭐⭐ | 新增 DreamRouter/config validator 等，但混在重构 PR 中 |
| manifest/入口 | ⭐ | entry_point 仍指向已不存在的 `plugin.py` |

## 关键问题分类

### 🔴 Critical（运行时崩溃 / 逻辑错误）

| # | 位置 | 问题 | 根因 |
|---|------|------|------|
| 1 | `memory/search.py:517` | `filter_existing_scores` 缺少 `db` 参数 | 模块函数签名需要 `(db, scores)` 但被直接传递为回调 |
| 2 | `memory/search.py:121` | `sync_embedding` 中 bound method 被当 unbound 调用 | `self.get_node_by_file_path` 只需 1 参数但被传 2 个 |
| 3 | `memory/router.py:262` | `_get_node_by_id` 被重命名为 `_get_node_by_id_wrapper` | 外部 API 名称变更但调用方未同步 |
| 4 | `service/state_manager.py:85` | `event_from_dict` 总是覆盖 sequence | 丢失了"仅在 event_id 缺失时使用 fallback"的守卫 |
| 5 | `service/core.py` | 心跳 prompt ~80 行护栏被删除 | tell_dfc 判定规则、输出格式、禁止事项、TODO 提醒 |
| 6 | `manifest.json` | `entry_point: "plugin.py"` 指向不存在的文件 | 文件移至 `core/plugin.py` 但未更新 |
| 7 | `dream/scheduler.py:230` | `archive_dream` 未守卫 `workspace=None` | 新增的 archive 功能缺少空值检查 |

### 🟡 Medium（行为变更 / 可观测性下降）

| # | 位置 | 问题 |
|---|------|------|
| 1 | `memory/service.py:112` | `_emit_visual_event` 对未知异常 re-raise（原代码静默吞掉） |
| 2 | `memory/decay.py:30` | `PRUNE_THRESHOLD` 从 0.1 改为 0.08（无记录） |
| 3 | `service/core.py:975` | 工具调用日志丢失 `参数` 字段 |
| 4 | `core.py:604` | 硬编码 `"actor"` / `"生命中枢唤醒上下文"` 与 state_manager 常量不同步 |

### 🟢 SNN / neuromod 模块

`snn_core.py → snn/core.py` 和 `neuromod.py → neuromod/engine.py` 是**干净的重构**，仅有导入路径调整和文档补充，无逻辑变更。

## 共性问题分析

重构 AI 的主要失误模式：

1. **函数签名适配器断裂**：将 class method 提取为 module function 时，忘记了 bound method 已包含 `self`/`db`
2. **外部 API 重命名不同步**：内部重命名后未检查所有调用方
3. **测试未随代码同步更新**：导入路径和 mock 目标全部失效
4. **行为变更未标记**：参数值修改、护栏删除混在"纯重构"中

## 结论

**不可直接部署**。修复后可用。核心逻辑（SNN、neuromod、config、tools）未受损；
高危区域集中在 **memory** 和 **service** 的拆分中。
