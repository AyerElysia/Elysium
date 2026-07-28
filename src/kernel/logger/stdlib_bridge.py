"""stdlib logging 桥接到 LogStore。

将 Python 标准库 logging 的输出统一写入 SQLite 日志存储，
消除 life_engine 等模块独立写文件的冗余。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db_store import LogStore

# 第三方库噪音过滤：这些模块的 DEBUG/INFO 日志不写入数据库
# 只有 WARNING+ 才会记录
_NOISY_LOGGERS: dict[str, int] = {
    "aiosqlite": logging.WARNING,
    "websockets": logging.WARNING,
    "httpcore": logging.WARNING,
    "httpx": logging.WARNING,
    "openai": logging.WARNING,
    "urllib3": logging.WARNING,
    "asyncio": logging.WARNING,
    "charset_normalizer": logging.WARNING,
}


class SQLiteLogHandler(logging.Handler):
    """将 stdlib logging 记录桥接到 LogStore。"""

    def __init__(self, store: LogStore, level: int = logging.DEBUG) -> None:
        super().__init__(level=level)
        self._store = store

    def emit(self, record: logging.LogRecord) -> None:
        """将日志记录写入 LogStore。"""
        try:
            # 第三方库噪音过滤：只记录 WARNING+
            min_level = _NOISY_LOGGERS.get(record.name)
            if min_level is None:
                # 检查父级 logger 是否在噪音列表中
                for prefix, level in _NOISY_LOGGERS.items():
                    if record.name.startswith(prefix + "."):
                        min_level = level
                        break
            if min_level is not None and record.levelno < min_level:
                return

            message = record.getMessage()
            metadata: dict[str, object] = {}

            if record.exc_info and record.exc_info[1] is not None:
                metadata["exception"] = self.format(record) if self.formatter else str(record.exc_info[1])

            if hasattr(record, "extra_data"):
                metadata["extra"] = record.extra_data  # type: ignore[attr-defined]

            self._store.write(
                level=record.levelname,
                module=record.name,
                message=message,
                metadata=metadata or None,
            )
        except Exception:
            # 日志桥接失败不应影响主程序
            self.handleError(record)


def install_stdlib_bridge(store: LogStore, level: int = logging.DEBUG) -> SQLiteLogHandler:
    """将 stdlib root logger 桥接到 LogStore。

    挂载后，所有通过 logging.getLogger() 输出的日志都会写入 SQLite。

    Args:
        store: LogStore 实例
        level: 最低捕获级别

    Returns:
        安装的 handler（可用于后续移除）
    """
    handler = SQLiteLogHandler(store, level=level)
    handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)

    return handler


def uninstall_stdlib_bridge(handler: SQLiteLogHandler) -> None:
    """移除 stdlib 桥接 handler。"""
    root = logging.getLogger()
    root.removeHandler(handler)
