# 日志系统（Logging System）

> 文档状态：权威文档，与代码同步截至 2026-07-31。
> 代码位置：`src/kernel/logger/`（1427 行）。
> 数据位置：`data/logs.db`（SQLite，~435MB）。
> 本文是日志专题的权威文档；凡与本文冲突，以本文和当前代码为准。

---

## 0. 一句话定位

日志系统是统一的结构化日志基础设施：所有模块（内核、插件、适配器）通过 `get_logger()` 获取 Logger，日志同时输出到彩色终端（rich）和 SQLite 数据库（FTS5 全文检索），stdlib logging 通过桥接器统一汇入，不再产生独立文件。

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      调用方                                       │
│  get_logger("life_engine") / logging.getLogger("httpx")          │
├────────────────────────────┬────────────────────────────────────┤
│     Logger（rich 终端）     │     stdlib_bridge（桥接器）          │
│  彩色 Panel 输出            │  SQLiteLogHandler                   │
│  事件总线广播               │  噪音过滤（第三方库）                │
├────────────────────────────┴────────────────────────────────────┤
│                      LogStore（SQLite 引擎）                      │
│  WAL 模式 / 后台写入队列 / 批量 INSERT / FTS5 / 自动保留          │
├─────────────────────────────────────────────────────────────────┤
│                      data/logs.db                                 │
│  logs 表 + logs_fts 虚拟表                                        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Logger

**文件**：`logger.py`（781 行）

### 2.1 获取 Logger

```python
from src.kernel.logger import get_logger, COLOR, initialize_logger_system

# 全局初始化（核心启动时调用一次）
initialize_logger_system(log_level="INFO", db_path="data/logs.db")

# 获取 logger
logger = get_logger("life_engine", display="life_engine", color=COLOR.PINK)
logger.info("心跳完成", metadata={"heartbeat_count": 42})
```

### 2.2 Logger 特性

| 特性 | 说明 |
| --- | --- |
| 彩色终端输出 | 基于 rich Panel，每个 logger 有独立颜色 |
| 自动颜色映射 | 未指定颜色时按 name hash 从 16 色池分配 |
| 元数据跟踪 | `metadata={}` 附加结构化数据 |
| SQLite 落库 | 动态读取全局配置，初始化后自动启用 |
| 事件总线广播 | 日志同时发布到 EventBus（`LOG_OUTPUT_EVENT`），供 WebUI 实时展示 |
| 级别控制 | 全局 + 每 logger 独立级别 |

### 2.3 日志级别颜色

| 级别 | 颜色 |
| --- | --- |
| DEBUG | dim（灰色） |
| INFO | blue |
| WARNING | yellow |
| ERROR | red |
| CRITICAL | bold red |

### 2.4 事件广播

日志可通过 EventBus 广播到订阅者（如 WebUI 日志面板）：
- 事件名：`LOG_OUTPUT_EVENT = "log_output"`
- 异步非阻塞：通过 `asyncio.Task` 后台发布
- 可禁用：`enable_event_broadcast=False`（高频模块如 LLM API）

---

## 3. SQLite 存储引擎（LogStore）

**文件**：`db_store.py`（380 行）

### 3.1 核心设计

| 特性 | 实现 |
| --- | --- |
| 并发安全 | WAL 模式（读写不互斥） |
| 非阻塞写入 | 后台线程 + `queue.Queue`（maxsize=10000） |
| 批量写入 | 每 100 条或每 1 秒 flush |
| 全文检索 | FTS5 虚拟表（`logs_fts`） |
| 自动保留 | 启动时清理过期日志 |
| 会话标识 | 进程级 `SESSION_ID`（时间戳+随机） |

### 3.2 表结构

```sql
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,       -- ISO 格式
    level TEXT NOT NULL,           -- DEBUG/INFO/WARNING/ERROR/CRITICAL
    module TEXT NOT NULL,          -- 模块名
    message TEXT NOT NULL,         -- 日志消息
    metadata TEXT DEFAULT '{}',    -- JSON 元数据
    session_id TEXT NOT NULL       -- 会话 ID
);

CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts
    USING fts5(message, module, content=logs, content_rowid=id);
```

### 3.3 写入流程

```
logger.info("msg")
    │
    ▼
Logger._write_db()
    │
    ▼ queue.put_nowait()
LogStore._queue（内存队列）
    │
    ▼ 后台 worker 线程
批量 INSERT（每 100 条 / 每 1 秒）
    │
    ▼
SQLite WAL → logs 表 + logs_fts
```

队列满时丢弃（`dropped_count` 计数），不阻塞调用方。

### 3.4 查询接口

```python
from src.kernel.logger import query_logs

# 全文搜索
results = query_logs(search="心跳超时", limit=20)

# 按级别+模块过滤
results = query_logs(level="ERROR", module="life_engine", since="2026-07-30")

# 组合查询
results = query_logs(
    level="WARNING",
    module="llm",
    search="timeout",
    since="2026-07-31T00:00:00",
    until="2026-07-31T23:59:59",
    limit=50,
    offset=0,
)
```

### 3.5 自动保留策略

| 级别 | 保留天数 | 说明 |
| --- | --- | --- |
| DEBUG | 3 天 | 高频低价值，快速清理 |
| INFO+ | 30 天 | 正常运行记录 |

清理在启动时执行（`cleanup()`），删除超期记录。

### 3.6 统计信息

```python
store.stats()
# → {
#     "total_entries": 1234567,
#     "by_level": {"DEBUG": 800000, "INFO": 400000, ...},
#     "db_size_bytes": 435000000,
#     "session_id": "20260731_080000_ab12cd34",
#     "queue_size": 0,
#     "queued_count": ...,
#     "written_count": ...,
#     "dropped_count": ...,
#     "write_failure_count": ...,
# }
```

---

## 4. stdlib 桥接

**文件**：`stdlib_bridge.py`

### 4.1 作用

将 Python 标准库 `logging` 的输出统一写入 SQLite，消除各模块独立写文件的冗余。

### 4.2 噪音过滤

第三方库的 DEBUG/INFO 日志不写入数据库，只有 WARNING+ 才记录：

| 库 | 最低记录级别 |
| --- | --- |
| aiosqlite | WARNING |
| websockets | WARNING |
| httpcore / httpx | WARNING |
| openai | WARNING |
| urllib3 | WARNING |
| asyncio | WARNING |
| charset_normalizer | WARNING |

### 4.3 安装/卸载

```python
from src.kernel.logger.stdlib_bridge import install_stdlib_bridge, uninstall_stdlib_bridge

handler = install_stdlib_bridge(store)   # 挂到 root logger
uninstall_stdlib_bridge(handler)         # 移除
```

由 `initialize_logger_system()` 自动安装，`shutdown_logger_system()` 自动卸载。

---

## 5. 彩色终端输出

**文件**：`color.py`

### 5.1 COLOR 枚举

提供 16 种命名颜色 + 明亮变体 + 特殊颜色（gray/orange/purple/pink），基于 rich 库渲染。

### 5.2 自动颜色分配

未显式指定颜色的 logger，按 name 的 MD5 hash 从默认 16 色池中分配，保证同名 logger 颜色稳定。

---

## 6. 初始化与关闭

### 6.1 初始化

```python
initialize_logger_system(
    log_level="INFO",              # 全局日志级别
    db_path="data/logs.db",        # SQLite 路径
    enable_db=True,                # 启用数据库
    enable_event_broadcast=True,   # 启用事件广播
    retention_debug_days=3,        # DEBUG 保留天数
    retention_info_days=30,        # INFO+ 保留天数
)
```

### 6.2 关闭

```python
# 异步环境（推荐）
await shutdown_logger_system_async(timeout=1.0)

# 同步环境
shutdown_logger_system()
```

关闭流程：停止事件广播 → 等待广播任务完成 → 关闭 LogStore（flush 队列）→ 卸载 stdlib bridge。

---

## 7. 文件索引

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `__init__.py` | ~100 | 包导出 + `query_logs()` 便捷函数 |
| `logger.py` | 781 | Logger 类 + 全局初始化/关闭 + 事件广播 |
| `db_store.py` | 380 | SQLite 存储引擎（WAL + FTS5 + 后台写入） |
| `stdlib_bridge.py` | ~80 | stdlib logging → LogStore 桥接 |
| `color.py` | ~90 | COLOR 枚举 + 颜色工具 |
