# Elysium 插件双路径加载身份分裂：统一相对导入 + 治本实施文档

> 对应：2026-08-13 线上三次 memory_witness ERROR（11:01 ClaimConflict 漏捕、12:01 ClaimConflict 漏捕复现、12:40 PresenceRevisionConflict 漏捕）
> 环境：单机多实例 / 本机 + 远端 Linux（elysium-linux-primary）共享 MySQL（frp-one.com:65429），均运行 `plugins/life_engine`
> 本文档给新对话直接实施：现象 → 机制 → 已做治标 → 治本方案 → 实施步骤 → 验收标准。
> 写作时间：2026-08-13 12:5x，基于对 `plugin_manager`/`memory_witness`/`writer_claims`/`presence_*` 的代码核读与三次线上报错证据。

---

## 0. 总览

| # | 事项 | 状态 | 说明 |
|---|------|------|------|
| 一 | ClaimConflict 双类漏捕 | 已治标 `c2b447cb` | memory_witness 包内导入改相对导入 |
| 二 | Presence 双类漏捕 | 已治标 `d3008707` | 捕获面双身份类兼容（`_PRESENCE_CONFLICT_TYPES`/`_WRITER_CLAIM_TYPES`） |
| 三 | 包内显式导入统一相对导入 | **待做（阶段 1）** | life_engine 包内 ~12 文件 |
| 四 | 统一插件加载身份（治本） | **待做（阶段 2）** | 消除"同一模块两份类"的机制根因 |
| 五 | 双类兼容代码回收 | 待阶段 2 后评估 | 单身份达成后可简化/移除 |

**症状链**：plugin_manager 造顶层包身份 → 同一源码两份类 → except/isinstance 漏捕 → 常态并发竞争被刷成 fatal ERROR（每轮刷 traceback，日志噪音 + 掩盖真实故障）。

---

## 1. 机制（务必先理解，所有改动围绕它）

### 1.1 双身份从哪来

- `src/core/managers/plugin_manager.py` 的 `_load_from_folder`（约 708 行）加载插件时：
  ```python
  parent_dir = str(folder.parent)          # E:\...\Elysium\plugins
  sys.path.insert(0, parent_dir)           # 把 plugins 目录塞进 sys.path 且保留
  package_name = plugin_root.name          # "life_engine"（插件文件夹名）
  module_name = f"{package_name}.{...}"    # "life_engine.xxx"（顶层包身份！）
  spec = importlib.util.spec_from_file_location(module_name, entry_point, submodule_search_locations=[...])
  ```
  → life_engine 插件以**顶层包身份**（`life_engine.*`）加载。
- 同时 `main.py` / `src/app/api/v1/*` / `src/core/transport/*` 以常规 import 使用 **`plugins.life_engine.*`**（plugins 前缀身份）。
- 两种包路径都可达（`plugins/` 无 `__init__.py`，plugins 是 PEP 420 namespace 包；`plugins/life_engine/__init__.py` 存在，life_engine 是普通包）→ **同一份源码被加载两次，两个模块实例、两个异常类**。

### 1.2 为什么测试全过、线上必现

- pytest 环境 sys.path 无 `plugins` 目录 → life_engine 只以 `plugins.life_engine.*` 单身份加载 → 类唯一 → except 命中。
- 真实运行 plugin_manager 先造 `life_engine.*` 身份 → memory_witness 与 presence/writer_claims 可能落在不同身份 → 类分裂。

### 1.3 铁证（三次线上 traceback 的异常名）

| 时间 | 异常 | 抛出类 `__module__` | 结论 |
|------|------|-------------------|------|
| 11:01/12:01 | SingletonWriterClaimConflict | `life_engine.storage.writer_claims`（无前缀） | writer_claims 是顶层身份 |
| 12:40 | PresenceRevisionConflict | `plugins.life_engine.service.presence_store`（带前缀） | presence 链路是 plugins 身份 |

同一进程里两类身份并存。

---

## 2. 已做治标（可作范例，勿回退）

1. **`c2b447cb`**：`memory_witness.py` 包内导入改相对导入（`.presence_store` / `..storage`）——同身份下捕获类与抛出类恒同一。
2. **`d3008707`**：捕获面双身份类兼容——运行时按两种包路径导入异常类（try/except ImportError 兜底），元组常量：
   ```python
   _PRESENCE_CONFLICT_TYPES = (PresenceRevisionConflict, _PluginsPresenceRevisionConflict)
   _WRITER_CLAIM_TYPES = (SingletonWriterClaimConflict, _PluginsWriterClaimConflict,
                          SingletonWriterClaimLost, _PluginsWriterClaimLost)
   ```
   loop 的 isinstance 分类、run_once 的 except、`_ensure_authoring_claim` 的 except 全部改用兼容元组。
3. 防回归测试模式（可直接复用）：
   - `test_author_claim_capture_identity_holds_under_dual_import`（c2b447cb 引入）
   - `test_presence_conflict_capture_covers_plugins_identity`（d3008707 引入，含 sys.modules 清理防污染）

---

## 3. 治本目标

**让"同一模块只有一份类"**——无论从哪条路径 import，`life_engine` 相关模块都解析到同一身份。

---

## 4. 实施步骤

### 阶段 0：核实（先做，2 小时内）

1. 通读 `src/core/managers/plugin_manager.py` 的 `_load_from_folder` / `_load_from_archive`，确认：所有插件（14 个）都走顶层身份？哪些插件被 `src/` 或彼此以 `plugins.*` 引用？
2. 列出全仓 `plugins.life_engine` 显式导入清单（含 `src/`、`plugins/` 内），区分"包内自引用"（应改相对导入）与"包外跨包引用"（保持 plugins 前缀，是正确用法）。
3. 确认 `plugins/` 无 `__init__.py`（namespace 包）、`plugins/life_engine/__init__.py` 存在（普通包）——这是双身份可达的前提。

### 阶段 1：life_engine 包内显式导入统一为相对导入（前置清理）

- 范围：`plugins/life_engine/` 下所有 `from plugins.life_engine.xxx import` / `import plugins.life_engine.xxx`（约 12 个文件：event_bus、registry、schedule_tools、todo_tools、perception_gateway、storage/* 的引用方等）。
- 改法：等价相对导入（`.`/`..`），保持 ruff isort 排序。
- **不改**：`src/app/api/v1/*`、`src/core/transport/*`、`plugins/voice_live/*` 里的 `plugins.life_engine`（包外引用，正确）。
- 测试：跑 `test/plugins/life_engine/` 全量 + `test/app/api/v1/`（若涉及 event_bus 等被 src 引用模块）。Windows 沙箱参数见 §6。
- 交付：无行为变化、测试全绿。

### 阶段 2：统一插件加载身份（核心，先评估后实施）

三选一，按此优先级评估（**先写对比结论给用户确认再动代码**）：

- **方案 C（推荐）**：`plugin_manager` 不再往 sys.path 插 `plugins` 目录，插件入口以 `plugins.<package_name>.xxx` 身份加载（`module_name = f"plugins.{package_name}.{...}"`）。`plugins` 在仓库根下、根目录本就在 sys.path，普通 import 即可解析。收益：全仓统一 plugins 前缀 → 单身份 → 双类问题从根上消失，阶段 1 之后插件内部相对导入不受影响。
  - 风险点：插件间互引（如 A 插件 import B 插件）现在若用顶层名（`feishu_adapter.xxx`）会失效，需同步改 `plugins.feishu_adapter.xxx` 或相对路径；manifest `entry_point` 的相对解析依赖 `submodule_search_locations`，要确认不改 `spec_from_file_location` 的此参数。
- **方案 A**：`plugin_manager` 加载方式不动，把 `src/` 全部 `plugins.life_engine` 引用改为顶层 `life_engine.*`。影响面大（跨包改写 + 测试环境 sys.path 需加 plugins 目录），不推荐。
- **方案 B**：维持双身份，但把"双类兼容"下沉为公共工具（如 `life_engine._identity` 提供双身份异常合并元组），所有捕获点统一使用。治标不治本，仅当 A/C 均不可行时兜底。

**阶段 2 验收**：模拟"真实运行加载顺序"（main.py 先 import plugins 身份 → plugin_manager 再加载插件）后，全仓关键异常捕获点 `捕获类 is 抛出类`；memory_witness 双类兼容常量退化为单类（可保留，无副作用）。

### 阶段 3：防回归与全量验证

1. 保留/更新双身份模拟测试（c2b447cb、d3008707 的测试模式），断言单身份达成后捕获类 is 抛出类。
2. 全量跑 `test/plugins/life_engine/` + `test/app/api/v1/` + `test/core/`（涉及的）。
3. 真实 E2E：本机 + 远端协作者拉取后重启，观察 30 分钟无 memory_witness ERROR（常态双实例竞争应完全静默）。

---

## 5. 约束（硬性）

- **只改导入语句与 import 块排序，不改变任何业务逻辑**；ruff 只修自己引入的问题（他人遗留 TRY004/UP012/UP034/F401 不动）。
- 每个改动配与风险相称的契约测试（含双身份模拟）。
- **git 纪律**：本仓库多会话并发、git 对象库曾损坏（8-11/8-12/8-13 三次）——**禁止 `git stash`/`git reset`/`checkout --` 做任何验证**；基线对比只用只读（`git show HEAD:file`）；**未经用户确认不得 commit/push**；`git add` 前先 `git status` 核对只加自己的文件；工作区可能有其他会话并行改动。
- 不装环境、不改 venv；验证以主工作区 `.venv` 为基准。
- 阶段 2 方案选型必须先给用户结论再动代码。

## 6. 测试与工具

- Windows 沙箱 pytest：`PYTEST_DEBUG_TEM_ROOT=$TEMP/pytest_tmp_root` + `-p no:cacheprovider --no-cov -o cache_dir=/dev/null`（规避 atexit 批量删除 guard）。
- 双身份模拟验证脚本（阶段 2 用）：
  ```python
  import sys
  sys.path.insert(0, "<repo>/plugins")          # 模拟 plugin_manager 插入
  from life_engine.service.memory_witness import _PRESENCE_CONFLICT_TYPES as T
  from plugins.life_engine.service.presence_store import PresenceRevisionConflict as P
  assert P in T and isinstance(P("x"), T)        # 双身份下捕获命中
  ```

## 7. 参考材料

- 相关 commit：`c2b447cb`（相对导入）、`d3008707`（双类兼容）、`fdc5efd6`（Claim 归可恢复路径）
- 测试范例：`test/plugins/life_engine/test_memory_witness_recovery.py::test_author_claim_capture_identity_holds_under_dual_import`、`::test_presence_conflict_capture_covers_plugins_identity`
- 报错证据：`data/logs.db`（logs 表，session_id 区分实例）；`logs/elysium-2026-08-13.log`
- 排查日志：`Elysium/.workbuddy/memory/2026-08-13.md`（11:00-12:50 全程）
