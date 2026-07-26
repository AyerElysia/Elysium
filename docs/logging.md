# 日志系统

## 架构

```
┌─────────────────────────────────────────────────────┐
│  get_logger("module") → Logger 实例                  │
│  ├── rich 彩色控制台输出（不变）                       │
│  ├── LogStore.write() → SQLite (data/logs.db)       │
│  └── EventBus 广播（webui 实时推送）                  │
├─────────────────────────────────────────────────────┤
│  stdlib logging ──→ SQLiteLogHandler ──→ LogStore   │
│  （life_engine 等模块的 logging.getLogger() 统一入库） │
└─────────────────────────────────────────────────────┘
```

- **存储**：SQLite WAL 模式，单文件 `data/logs.db`
- **写入**：后台线程 + queue，批量 INSERT（每 100 条或每 1 秒 flush），不阻塞主线程
- **索引**：FTS5 全文索引（message 字段），支持快速搜索
- **保留策略**：启动时自动清理——DEBUG 保留 3 天，INFO+ 保留 30 天

## 基本用法

```python
from src.kernel.logger import get_logger, initialize_logger_system, COLOR

# 启动时初始化（bot.py 已自动调用）
initialize_logger_system(log_level="INFO", db_path="data/logs.db")

# 获取 logger（API 与旧版完全兼容）
logger = get_logger("my_module", display="我的模块", color=COLOR.BLUE)
logger.info("启动完成", user="elysia")
logger.error("连接失败", exc_info=True)
```

## 查询 API

```python
from src.kernel.logger import query_logs

# 按级别过滤
errors = query_logs(level="ERROR", limit=50)

# 按模块过滤（支持前缀匹配）
life_logs = query_logs(module="life_engine")

# 全文搜索（FTS5）
results = query_logs(search="内存溢出")

# 时间范围
recent = query_logs(since="2026-07-01T00:00:00", until="2026-07-02T00:00:00")

# 组合
critical = query_logs(level="CRITICAL", module="life_engine", search="crash", limit=10)
```

返回格式：

```python
[
    {
        "id": 12345,
        "timestamp": "2026-07-01T12:34:56.789",
        "level": "ERROR",
        "module": "life_engine.core",
        "message": "连接超时",
        "metadata": {"retry": 3},
        "session_id": "20260701_123456_a1b2c3d4",
    }
]
```

## 配置项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `log_level` | `"DEBUG"` | 控制台输出最低级别 |
| `enable_db` | `True` | 是否启用 SQLite 存储 |
| `db_path` | `"data/logs.db"` | 数据库文件路径 |
| `retention_debug_days` | `3` | DEBUG 日志保留天数 |
| `retention_info_days` | `30` | INFO+ 日志保留天数 |
| `enable_event_broadcast` | `True` | 是否广播到 EventBus |

## stdlib 桥接

初始化时自动安装 `SQLiteLogHandler` 到 Python root logger。所有通过 `logging.getLogger()` 输出的日志（包括第三方库）都会写入同一个 SQLite 数据库。

```python
# 手动控制（通常不需要）
from src.kernel.logger import install_stdlib_bridge, uninstall_stdlib_bridge, LogStore

store = LogStore("data/logs.db")
handler = install_stdlib_bridge(store)
# ...
uninstall_stdlib_bridge(handler)
```

## 与旧系统的对比

| | 旧系统 | 新系统 |
|---|---|---|
| 存储 | 纯文本文件（`logs/mofox_*.log`） | SQLite（`data/logs.db`） |
| 文件数量 | 每次启动新建一个，无清理 | 单文件，自动保留策略 |
| 查询 | 只能 grep | 结构化查询 + FTS5 全文搜索 |
| life_engine | 独立写 `logs/life_engine/life.log`（109MB） | 统一入库 |
| 体积控制 | 无（累积 179MB+） | DEBUG 3天 / INFO+ 30天自动清理 |
| 写入性能 | 同步文件 I/O | 后台线程 + 批量 INSERT |

## 文件结构

```
src/kernel/logger/
├── __init__.py       # 公共 API + query_logs()
├── logger.py         # Logger 类 + initialize/get/shutdown
├── db_store.py       # LogStore（SQLite 引擎）
├── stdlib_bridge.py  # logging.Handler → LogStore 桥接
└── color.py          # 颜色定义（不变）
```

## 数据库 Schema

```sql
CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,       -- ISO 格式
    level TEXT NOT NULL,           -- DEBUG/INFO/WARNING/ERROR/CRITICAL
    module TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    metadata TEXT DEFAULT '{}',    -- JSON
    session_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_logs_timestamp ON logs(timestamp);
CREATE INDEX idx_logs_level ON logs(level);
CREATE INDEX idx_logs_module ON logs(module);

-- FTS5 全文索引
CREATE VIRTUAL TABLE logs_fts USING fts5(message, content='logs', content_rowid='id');
```
