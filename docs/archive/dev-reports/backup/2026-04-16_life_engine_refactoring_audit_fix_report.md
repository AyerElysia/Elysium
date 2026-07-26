# life_engine 重构审查修复报告

> 日期: 2026-04-16
> 分支: `refactor/life_engine-directory-structure`
> 基准: `bcf4eb1` → 修复后测试: **53 passed / 2 failed**（2 个失败为预存在的 protobuf 环境问题）

## 修复清单

### Round 1: 导入路径修复（14 处）

| 文件 | 修复内容 |
|------|---------|
| `core/plugin.py` L65-67 | `from .snn_router` → `from ..snn.router` (×3 routers) |
| `dream/router.py` | TYPE_CHECKING import 修正 + static path 修正 + 移除断裂的 memory_router import |
| `dream/scheduler.py` L174 | `from .seeds import _seed_to_dict` → `from .residue import _seed_to_dict` |
| `dream/scenes.py` L152 | 同上 + `max_chars` kwarg→positional 修复 |
| `snn/router.py` L14,30 | TYPE_CHECKING import + static path |
| `memory/router.py` L182 | static path 修正 |
| `tools/file_tools.py` L1149 | `from .memory_tools` → `from ..memory.tools` |

### Round 2: 测试导入修复（7 个测试文件）

| 文件 | 修改数 |
|------|-------|
| `test_config_validation.py` | 1 import |
| `test_service.py` | 1 import |
| `test_web_tools.py` | 2 imports + 8 monkeypatch paths |
| `test_memory_service.py` | 2 imports + 1 monkeypatch |
| `test_memory_service_exceptions.py` | 1 import |
| `test_tell_dfc_tool.py` | 1 monkeypatch |
| `test_dream.py` | 3 imports |

### Round 3: 梦系统测试回归修复

| 文件 | 问题 | 修复 |
|------|------|------|
| `test_dream.py` | `_generate_scene_payload` mock 指向不存在的属性 | 改为 mock `_build_dream_scene`，返回 `(DreamTrace, text, DreamResidue)` 元组 |
| `dream/scheduler.py` L230 | `archive_dream(workspace=None)` 崩溃 | 添加 `if self._workspace is not None:` 守卫 |

### Round 4: 内存模块 Critical 修复（3 处）

| 文件 | 问题 | 修复 |
|------|------|------|
| `memory/search.py:517` | `filter_existing_scores` 作为回调缺少 `db` 参数 | 用闭包 `_bound_filter` 绑定 `db` |
| `memory/search.py:121` | `sync_embedding` 对 bound method 传多余 `db` 参数 | 拆分为 if/else：bound→1arg, unbound→2args |
| `memory/service.py:346` | `_get_node_by_id` 重命名为 `_wrapper` 但 router 仍用旧名 | 添加 `_get_node_by_id = _get_node_by_id_wrapper` 别名 |

### Round 5: 行为回退 + 服务模块修复

| 文件 | 问题 | 修复 |
|------|------|------|
| `memory/service.py:112` | `_emit_visual_event` 对意外异常 re-raise | 恢复静默处理（可视化非关键路径） |
| `memory/decay.py:30` | `PRUNE_THRESHOLD` 从 0.1 改为 0.08 | 恢复为 0.1 |
| `memory/service.py:78` | 同上（class 属性） | 恢复为 0.1 |
| `service/state_manager.py:85` | `event_from_dict` 总是覆盖 sequence | 仅在 `event_id` 缺失时使用 fallback |
| `service/core.py` prompt | ~80 行心跳护栏被删除 | 恢复 tell_dfc 判定规则、输出格式、禁止事项、TODO 提醒 |
| `service/core.py:975` | 工具调用日志丢失 `参数` 字段 | 恢复 `参数: {args}` |
| `service/core.py:604` | 硬编码常量与 state_manager 不同步 | 改为引用 `_TARGET_REMINDER_BUCKET/NAME` |
| `manifest.json` | `entry_point: "plugin.py"` 指向不存在的文件 | 改为 `"core/plugin.py"` |
| `test_memory_service_exceptions.py` | 测试检查 re-raise 行为 | 更新为检查安全处理行为 |

## 测试结果对比

| 阶段 | Passed | Failed | 说明 |
|------|--------|--------|------|
| 修复前 | 0 | 55 | 所有导入全部断裂 |
| Round 1+2 | 51 | 4 | 2 protobuf + 2 dream mock |
| Round 3 | 53 | 2 | 仅剩 protobuf |
| Round 4+5 | 53 | 2 | 保持稳定，修复运行时 bug |

2 个残留失败均为 **protobuf 环境依赖问题**（`google.protobuf.internal.builder` ImportError），非回归。

## SNN / neuromod 审查结论

`snn_core.py → snn/core.py`、`snn_bridge.py → snn/bridge.py`、`neuromod.py → neuromod/engine.py`：
**完全等价**，仅导入路径调整 + 文档补充。无需修复。
