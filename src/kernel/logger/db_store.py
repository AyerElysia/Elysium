"""SQLite 结构化日志存储引擎。

WAL 模式 + 后台写入队列 + FTS5 全文索引 + 自动保留策略。
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


# 当前会话 ID（进程级唯一）
SESSION_ID: str = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

# 批量写入阈值
_BATCH_SIZE = 100
_FLUSH_INTERVAL = 1.0  # 秒
_LEGACY_COMPACT_MIN_BYTES = 64 * 1024 * 1024
_LEGACY_COMPACT_FREE_RATIO = 0.25
_INCREMENTAL_VACUUM_PAGES = 8192


class LogStore:
    """SQLite 结构化日志存储。

    特性：
    - WAL 模式，读写不互斥
    - 后台线程 + queue 异步写入，不阻塞日志调用方
    - 批量 INSERT（每 100 条或每 1 秒 flush）
    - FTS5 全文索引，支持消息搜索
    - 自动保留策略（启动时清理过期日志）
    """

    def __init__(
        self,
        db_path: str | Path = "data/logs.db",
        retention_debug_days: int = 3,
        retention_info_days: int = 30,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._retention_debug_days = retention_debug_days
        self._retention_info_days = retention_info_days

        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=10000)
        self._stopped = threading.Event()
        self._conn: sqlite3.Connection | None = None
        self._metrics_lock = threading.Lock()
        self._queued_count = 0
        self._written_count = 0
        self._dropped_count = 0
        self._write_failure_count = 0

        # 初始化数据库。清理必须发生在 writer 启动前，避免启动阶段两个
        # SQLite 连接互相争用写锁。
        self._init_db()
        self.cleanup()

        # 启动后台写入线程
        self._worker = threading.Thread(
            target=self._worker_loop, name="log-store-writer", daemon=True
        )
        self._worker.start()

    def _init_db(self) -> None:
        """初始化数据库 schema。"""
        is_new_database = not self._db_path.exists() or self._db_path.stat().st_size == 0
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        original_auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        if is_new_database:
            # Must be selected before the first table is created.
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=3000")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                module TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                metadata TEXT DEFAULT '{}',
                session_id TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
            CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
            CREATE INDEX IF NOT EXISTS idx_logs_module ON logs(module);
        """)

        # FTS5 全文索引（独立表，通过 trigger 同步）
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts
                USING fts5(message, content='logs', content_rowid='id')
            """)
            # 同步 trigger
            conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS logs_ai AFTER INSERT ON logs BEGIN
                    INSERT INTO logs_fts(rowid, message) VALUES (new.id, new.message);
                END;
                CREATE TRIGGER IF NOT EXISTS logs_ad AFTER DELETE ON logs BEGIN
                    INSERT INTO logs_fts(logs_fts, rowid, message)
                        VALUES ('delete', old.id, old.message);
                END;
            """)
        except sqlite3.OperationalError:
            # FTS5 不可用时降级（某些 SQLite 编译版本不含 FTS5）
            pass

        conn.commit()

        # Older databases were created with auto_vacuum=NONE. Only migrate a
        # materially bloated file: VACUUM runs before the writer thread starts
        # and therefore happens at most once.
        if not is_new_database and original_auto_vacuum == 0:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            size_bytes = page_size * page_count
            free_ratio = free_pages / page_count if page_count else 0.0
            if (
                size_bytes >= _LEGACY_COMPACT_MIN_BYTES
                and free_ratio >= _LEGACY_COMPACT_FREE_RATIO
            ):
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                conn.execute("VACUUM")
        conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接。"""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), timeout=5.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=3000")
        return self._conn

    def _worker_loop(self) -> None:
        """后台写入线程主循环。"""
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()

        while not self._stopped.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                # 超时，检查是否需要 flush
                if batch and (time.monotonic() - last_flush) >= _FLUSH_INTERVAL:
                    self._flush_batch(batch)
                    batch = []
                    last_flush = time.monotonic()
                continue

            if item is None:
                # 关闭信号
                break

            batch.append(item)

            # 达到批量阈值或时间阈值时 flush
            if len(batch) >= _BATCH_SIZE or (time.monotonic() - last_flush) >= _FLUSH_INTERVAL:
                self._flush_batch(batch)
                batch = []
                last_flush = time.monotonic()

        # 关闭前 flush 剩余
        if batch:
            self._flush_batch(batch)

        # 排空队列中剩余的
        remaining: list[dict[str, Any]] = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    remaining.append(item)
            except queue.Empty:
                break
        if remaining:
            self._flush_batch(remaining)

        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _flush_batch(self, batch: list[dict[str, Any]]) -> None:
        """批量写入数据库。"""
        try:
            conn = self._get_conn()
            conn.executemany(
                """INSERT INTO logs (timestamp, level, module, message, metadata, session_id)
                   VALUES (:timestamp, :level, :module, :message, :metadata, :session_id)""",
                batch,
            )
            conn.commit()
            with self._metrics_lock:
                self._written_count += len(batch)
        except Exception:
            # 写入失败不影响主程序，但必须可观测。
            with self._metrics_lock:
                self._write_failure_count += len(batch)

    def write(
        self,
        level: str,
        module: str,
        message: str,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        """写入一条日志（非阻塞，放入队列）。

        Args:
            level: 日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）
            module: 模块名（logger name）
            message: 日志消息
            metadata: 额外元数据
            session_id: 会话 ID（默认使用当前进程会话）
        """
        if self._stopped.is_set():
            return

        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
            "level": level.upper(),
            "module": module,
            "message": message,
            "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            "session_id": session_id or SESSION_ID,
        }

        try:
            self._queue.put_nowait(entry)
            with self._metrics_lock:
                self._queued_count += 1
        except queue.Full:
            # 队列满时丢弃（日志不应阻塞主程序），并记录指标。
            with self._metrics_lock:
                self._dropped_count += 1

    def query(
        self,
        level: str | None = None,
        module: str | None = None,
        since: str | None = None,
        until: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """查询日志。

        Args:
            level: 过滤级别（如 "ERROR"）
            module: 过滤模块（支持前缀匹配，如 "life_engine"）
            since: 起始时间（ISO 格式）
            until: 截止时间（ISO 格式）
            search: 全文搜索关键词（FTS5）
            limit: 返回条数上限
            offset: 偏移量

        Returns:
            日志记录列表
        """
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row

        conditions: list[str] = []
        params: list[Any] = []

        if search:
            # FTS5 搜索
            try:
                conditions.append("id IN (SELECT rowid FROM logs_fts WHERE logs_fts MATCH ?)")
                params.append(search)
            except sqlite3.OperationalError:
                # FTS5 不可用，降级为 LIKE
                conditions.append("message LIKE ?")
                params.append(f"%{search}%")

        if level:
            conditions.append("level = ?")
            params.append(level.upper())

        if module:
            conditions.append("(module = ? OR module LIKE ?)")
            params.extend([module, f"{module}.%"])

        if since:
            conditions.append("timestamp >= ?")
            params.append(since)

        if until:
            conditions.append("timestamp <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM logs {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        try:
            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "level": row["level"],
                    "module": row["module"],
                    "message": row["message"],
                    "metadata": json.loads(row["metadata"] or "{}"),
                    "session_id": row["session_id"],
                }
                for row in rows
            ]
        except Exception:
            return []
        finally:
            conn.close()

    def cleanup(self) -> int:
        """执行保留策略清理，返回删除条数。"""
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=5.0)
            conn.execute("PRAGMA busy_timeout=3000")
            cursor = conn.execute(
                "DELETE FROM logs WHERE level = 'DEBUG' AND timestamp < datetime('now', ?)",
                (f"-{self._retention_debug_days} days",),
            )
            deleted_debug = cursor.rowcount

            cursor = conn.execute(
                "DELETE FROM logs WHERE timestamp < datetime('now', ?)",
                (f"-{self._retention_info_days} days",),
            )
            deleted_all = cursor.rowcount

            conn.commit()

            # DELETE only adds free pages. Incremental vacuum returns a bounded
            # number of them without a long full-file lock.
            auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
            free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            if auto_vacuum == 2 and free_pages > 0:
                pages = min(free_pages, _INCREMENTAL_VACUUM_PAGES)
                conn.execute(f"PRAGMA incremental_vacuum({pages})")
            return deleted_debug + deleted_all
        except Exception:
            return 0
        finally:
            if conn is not None:
                conn.close()

    def stats(self) -> dict[str, Any]:
        """获取日志存储统计信息。"""
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=5.0)
            total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            by_level = dict(
                conn.execute("SELECT level, COUNT(*) FROM logs GROUP BY level").fetchall()
            )
            size_bytes = self._db_path.stat().st_size if self._db_path.exists() else 0
            conn.close()
            with self._metrics_lock:
                metrics = {
                    "queued_count": self._queued_count,
                    "written_count": self._written_count,
                    "dropped_count": self._dropped_count,
                    "write_failure_count": self._write_failure_count,
                }
            return {
                "total_entries": total,
                "by_level": by_level,
                "db_size_bytes": size_bytes,
                "db_path": str(self._db_path),
                "session_id": SESSION_ID,
                "queue_size": self._queue.qsize(),
                **metrics,
            }
        except Exception:
            with self._metrics_lock:
                metrics = {
                    "queued_count": self._queued_count,
                    "written_count": self._written_count,
                    "dropped_count": self._dropped_count,
                    "write_failure_count": self._write_failure_count,
                }
            return {
                "total_entries": 0,
                "by_level": {},
                "db_size_bytes": 0,
                "queue_size": self._queue.qsize(),
                **metrics,
            }

    def close(self) -> None:
        """关闭存储（发送停止信号，等待 worker 退出）。"""
        self._stopped.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=3.0)
