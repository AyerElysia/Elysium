# Elysium 冗余审计报告

> 2026-09-01 · 三路并行静态审计（死代码 / 死配置 / 冗余文件）
> 全程只读，**未删除或修改任何项目文件**。所有"可清理"项均需人工确认后执行。

---

## 0. 一句话结论

**冗余确实存在，但不是随机垃圾，而是三个可命名的成因**（见第 5 节）。
代码质量本身不差——真正缺的是**"删除"的纪律**：这个项目擅长添加（设计、注释、观测、备份），
不擅长移除。

---

## 1. 规模更正（先纠正一个常见误解）

| 项 | 实际值 |
|---|---|
| 生产 Python 文件 | **693**（非 10000+） |
| 含测试总文件 | 1092 |
| 提取的定义（def/class） | 10984 |
| 项目总体量 | 35G（`.git` 另占 1.6G） |

`services/` 名义上 39148 个 `.py`，但**第一方仅 4 个**，其余全是
`router_model` / `visual_embedding` 两个子项目的 `.venv` 第三方包（19G）。

---

## 2. 死代码：228 确认 + 132 疑似

### 2.1 最高价值：`memory/service.py` epistemic 家族（9 个全死）

`plugins/life_engine/memory/service.py`

| 行号 | 名称 |
|---|---|
| 1840 | `record_epistemic_claim` |
| 1879 | `append_memory_belief` |
| 1883 | `append_epistemic_conflict` |
| 1890 | `append_memory_state_event` |
| 1923 | `list_memory_claim_states` |
| 1941 | `project_current_memory_facts` |
| 1959 | `get_memory_audit_trail` |
| 1973 | `list_memory_state_events` |
| 1987 | `list_memory_claim_evidence` |

**这是今日诊断中"beliefs/conflicts 表 0 条数据"的根因定位**——
此前判断为"认知升华断裂"，现已精确到底层：

> `epistemic.py` 的 `append_belief`/`append_conflict` **本身不是死代码**，
> 调用链完整（`service.py:1881` → `storage/memory/local.py:1369` → `epistemic.py:532`，
> MySQL 侧 `mysql.py:4871` 亦有实现）。
> 真正的断点在**上一层**：`service.py` 的 9 个包装方法全死 ⇒ 整条写入路径从未被触发。

### 2.2 其余集中区

| 区域 | 数量 | 备注 |
|---|---|---|
| `plugins/life_engine/memory/service.py`（除 epistemic 外） | 13 | 含 `append_experiences`(L2000)、`search_memory_simple`(L3533) |
| `plugins/life_engine/minecraft/` | 28 | `input_control`8 / `social`8 / `motor_loop`7 / `capture`3 |
| `plugins/life_engine/service/core.py` | 14 | |
| `src/app/plugin_system/api/message_api.py` | 13 | |
| `plugins/life_engine/agents/guardrails.py` | 3 | `check_output` / `check_mission_budget` / `check_write_path` |
| `plugins/life_engine/learning/` | 3 | |

### 2.3 需优先处置的模块

**`storage/multi_writer_health.py` 整个模块无生产调用点**——
仅被 `storage/__init__.py:66` 导出 + 测试引用，与「双实例共享未启用」完全吻合。

### 2.4 一处更正

`storage/` 的 `multi_writer_protocol` 被 `service/core.py:2383` 真实引用、
`instance_identity` 被 `hot_path_bridge.py:36` 引用 —— **不算死代码**，
此前"双实例相关全部是死代码"的粗判需收窄。

---

## 3. 死配置：37 确认（21 零引用 + 16 MySQL 不可达）

### 3.1 MySQL 整套不可达（16 条，`core.toml`）

`mysql_host`(:225) / `mysql_port`(:229) / `mysql_database`(:233) / `mysql_user`(:237) /
`mysql_password`(:241) / `mysql_charset` / `mysql_ssl_*`(4) / `mysql_max_overflow` /
`mysql_pool_recycle_seconds` / `mysql_idle_session_timeout_seconds` /
`mysql_pool_timeout_seconds` / `mysql_query_timeout_seconds` / `mysql_lock_wait_timeout_seconds`

**证据链**：`bot.py:392 use_mysql = backend=="mysql"` → `core.toml:132 backend="local"`
→ 走 SQLite 分支，`engine.py:534` 的 MySQL 分支不进。
这些键虽在 `factory.py:149-168` 被无条件**构造**，但只在 `BackendKind.MYSQL` 分支消费。

⚠️ 其中 `mysql_host="frp-one.com"` / `port=65429` 是 frp 公网穿透，
**TCP 握手 61.5ms（本地 0.3ms，200 倍）**。留着是隐患：一旦切 `backend=mysql` 会直接打死系统。

### 3.2 零引用（21 条）

- **`[permissions]` 整块 9 键从未接线**（仅 `default_permission_level` 被用）
- `models.toml` 中 9 条 `tasks.*` 的 `attempt_timeout_seconds` / `model_extra` 声明未装配
- 零散 3 条：`bot.ui_refresh_interval`、`bot.logs_dir`、`chat.default_chat_mode`

### 3.3 疑似（14 条）

`connection_pool_size` / `connection_timeout`（仅 pg/mysql 分支用）、
`multi_writer_enabled` / `multi_writer_protocol_version`、`postgresql_*` 10 条。

> 注：`authority_*` / `registry_id` / `require_verified_generation` / `backend_generation`
> **不是死配置** —— `local_selectable_enabled=true` 使其真实生效。

---

## 4. 过时注释：13 处，同一错误前提

共同前提：「双实例共享 MySQL / 共享多写者模式下 CAS 冲突是合法竞争」。
**该前提不存在**：`multi_writer_enabled=false`、`backend="local"`、
`multi_writer_hooks.py:7`（bridge 仅在 `multi_writer_enabled=true` 时注册）。

| 位置 | 数量 |
|---|---|
| `plugins/life_engine/service/core.py`（含 `:5729-5732` `_save_runtime_context` 上方） | 6 |
| `src/core/transport/distribution/loop.py` | 3 |
| `plugins/life_engine/learning/selectable.py` | 2 |
| `plugins/life_engine/service/memory_witness.py` | 2 |
| `plugins/life_engine/storage/memory/mysql.py` | 1 |

**危害**：这类注释会主动误导后续维护者（今天我自己就被它带偏过一次）。

**标记统计**：TODO 59 处（57 集中在 `plugins/life_engine`）、FIXME/HACK/XXX 均为 0。

---

## 5. 三类成因（本报告最有价值的部分）

冗余不是随机产生的，全部来自三个可命名的机制：

1. **为从未启用的机制准备的代码**
   双实例共享 MySQL、multi-writer 协议、permissions 权限块。
   设计阶段完整写就，启用开关始终为 false ⇒ 代码成为化石。

2. **架构先行但未接线的层**
   epistemic（claim/evidence/belief/conflict）整层设计精良、
   双时间继任、遗忘多维状态、审计回放一应俱全 —— 但**包装方法没接**，
   于是 0 条数据、无调用者。典型"建而未接"。

3. **缺乏清理纪律**
   `.bak-*` 堆积 3.7G、`__pycache__` 255M、死配置不删、过时注释不更新。
   装了仪表盘却不清读数。

**这与今日的架构判断完全同源**：作者有很强的设计自觉
（`catch_up` 主动 `to_thread`、分阶段计时、慢日志分级、登记已知风险），
但**"添加"的能力远强于"移除"**。

---

## 6. 冗余文件与磁盘（96% 满，仅剩 16G）

| 类别 | 数量 | 占用 | 判定 |
|---|---|---|---|
| `*.bak-*` 备份 | 16 | **3.71G** | ✅ 可安全清理 |
| 数据库备份（backrestore 1.9G 等） | 20 | 2.08G | ⚠️ 需确认 |
| 归档/缓存（training_lake/archive 1.8G、media_cache 1.4G、temp_images 390M） | — | 3.59G | ⚠️ 需确认 |
| `__pycache__` / `.pyc` | 1652 / 20976 | 255M | ✅ 可安全清理 |
| 两处 `.venv`（torch+nvidia+triton 字节级相同） | 4 | 19.7G | ⚠️ 需确认 |
| `logs/` | 37 | 67M | ❌ 不值得动（运行中） |

**可归类冗余合计 ≈ 9.7G（不含 venv）；含未运行的 router_model/.venv 则 21.7G。**

### 释放空间性价比 TOP 5

1. **删 `*.bak-*` 16 个 → 3.71G**（零风险，主库健在）
2. **`router_model/.venv` 12G** —— 服务**未运行**，全库仅被自身 `install_service.sh` 引用
3. **两 venv 硬链接去重 ~6.4G**（风险低于删除，推荐作为第 2 项的保守替代）
4. **`backrestore/` 1.9G + `training_data_lake/archive` 1.8G → 3.7G**（8 月切换前快照）
5. **零碎 690M**：`temp_images` 390M + `__pycache__` 255M + `chroma.broken/.empty` 42M

### 🔴 绝对不可动（正在被主进程持有）

`memory.db`(940M)、`local.sqlite3`(580M)、`chroma/chroma.sqlite3`、
`logs.db`、`Elysium.db`、`runtime/auth.sqlite3`

未被持有但仍需谨慎：`life_events.sqlite3`(381M)、
`archive_sync_state.sqlite3`(109M，8/3 起停更)。

---

## 7. 建议处置顺序

| 优先级 | 动作 | 收益 | 风险 |
|---|---|---|---|
| P0 | 磁盘：删 `*.bak-*` + `__pycache__` | +3.9G | 极低 |
| P0 | **清理过时注释**（13 处） | 消除误导 | 极低 |
| P1 | 决定 epistemic 层命运：接线 或 明确标记实验性 | 消除"建而未接" | 中（需设计决策） |
| P1 | 决定 `multi_writer_health.py` 去留 | 与"双实例是否还要"绑定 | 中 |
| P2 | 清理死配置（尤其 MySQL 16 条） | 消除隐患 | 低（但配置不入库，改了也不传播） |
| P2 | 清理死代码 228 处 | 降低认知负担 | 中（需逐个确认无反射调用） |
| P3 | 磁盘：venv 去重 / router_model 去留 | +6.4G ~ 12G | 中 |

⚠️ **死代码删除前务必确认无 `getattr` / 字符串反射调用**——静态扫描识别不了这类动态分发，
132 项"疑似"中很可能有相当比例属于此列。

---

## 附：今日另一项发现的连带结论

`scratch/repeat_one.py` 已改为从 `models.toml` 读 api_key（api_key 新旧两份相同）；
`scratch/test_gpt55.py` 标注废弃（所测 gpt-5.4/5.5 在配置中 0 匹配）。
`scratch/` 与 `config/` 均在 gitignore 中，这两处改动不入库。
