"""记忆层 SQLite 运行时的契约测试。

覆盖三条会直接表现为"系统总在阻塞"的性质：

1. **WAL + 逐连接 pragma。** journal_mode 写在数据库头里，其余 pragma 是
   连接作用域的。任何一条没被配置的连接都会退回 ``synchronous=FULL``，
   每次提交多一次 fsync。
2. **读写分离。** 读路径必须拿到自己的 query_only 句柄，既不与写连接争
   语句锁，也不阻塞提交；写操作走到读句柄上必须报错，而不是在一条对写锁
   不可见的连接上悄悄提交。
3. **代际校验。** 执行线程把读句柄缓存在 thread-local 里。仅比较路径无法
   区分"活着的句柄"和"上一次 bind_reader_pool 已经关掉的句柄"——服务在同
   一个文件上关闭再打开时，会拿到一条死连接并抛
   ``Cannot operate on a closed database``。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from plugins.life_engine.memory import sqlite_runtime


@pytest.fixture
def memory_db(tmp_path: Path):
    """提供一个已建表并绑定读连接池的临时记忆库。

    Args:
        tmp_path: pytest 提供的临时目录。

    Yields:
        tuple[Path, sqlite3.Connection]: 数据库路径与写连接。
    """
    db_path = tmp_path / "memory.db"
    writer = sqlite_runtime.open_memory_connection(db_path, role="writer")
    writer.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)")
    writer.execute("INSERT INTO probe (id, value) VALUES (1, 'a')")
    writer.commit()
    sqlite_runtime.bind_reader_pool(db_path)
    try:
        yield db_path, writer
    finally:
        sqlite_runtime.bind_reader_pool(None)
        writer.close()


def _pragma(db: sqlite3.Connection, name: str):
    """读取一个 pragma 的当前值。

    Args:
        db: 连接。
        name: pragma 名。

    Returns:
        Any: pragma 的第一列值。
    """
    return db.execute(f"PRAGMA {name}").fetchone()[0]


class TestConnectionConfiguration:
    """每条句柄都必须被完整配置。"""

    def test_writer_connection_pragmas(self, memory_db) -> None:
        _, writer = memory_db
        assert _pragma(writer, "journal_mode") == "wal"
        # 1 == NORMAL，WAL 下的正确搭配；2 == FULL 表示每次提交都 fsync
        assert _pragma(writer, "synchronous") == 1
        assert _pragma(writer, "foreign_keys") == 1
        assert _pragma(writer, "busy_timeout") == 10000
        # 2 == SQLITE_TEMP_STORE MEMORY
        assert _pragma(writer, "temp_store") == 2
        assert _pragma(writer, "query_only") == 0

    def test_reader_connection_pragmas(self, memory_db) -> None:
        db_path, _ = memory_db
        reader = sqlite_runtime.open_memory_connection(db_path, role="reader")
        try:
            assert _pragma(reader, "journal_mode") == "wal"
            assert _pragma(reader, "synchronous") == 1
            assert _pragma(reader, "busy_timeout") == 10000
            assert _pragma(reader, "query_only") == 1
        finally:
            reader.close()

    async def test_reader_rejects_writes(self, memory_db) -> None:
        """读句柄上的写必须报错，而不是提交到一条写锁看不见的连接上。"""

        def _attempt_write(db: sqlite3.Connection) -> None:
            db.execute("INSERT INTO probe (id, value) VALUES (99, 'x')")

        with pytest.raises(sqlite3.OperationalError):
            await sqlite_runtime.run_read(_attempt_write)


class TestReaderWriterIsolation:
    """WAL 的核心收益：写事务在途时读不被阻塞。"""

    async def test_read_succeeds_while_writer_holds_open_transaction(
        self, memory_db
    ) -> None:
        db_path, writer = memory_db

        # 打开一个未提交的写事务并持有它
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO probe (id, value) VALUES (2, 'b')")

        def _read(db: sqlite3.Connection) -> list[str]:
            return [row["value"] for row in db.execute("SELECT value FROM probe")]

        try:
            # 写事务未提交，读连接应当立刻看到提交前的快照而不是等锁超时
            rows = await asyncio.wait_for(
                sqlite_runtime.run_read(_read), timeout=3.0
            )
        finally:
            writer.rollback()

        assert rows == ["a"]

    async def test_concurrent_reads_use_distinct_thread_local_handles(
        self, memory_db
    ) -> None:
        """并发读各自持有句柄，不在同一条连接上排队。"""
        barrier = threading.Barrier(2, timeout=5.0)

        def _read(db: sqlite3.Connection) -> tuple[int, str]:
            # 两个读必须真正同时在飞，否则 barrier 会超时
            barrier.wait()
            db.execute("SELECT value FROM probe").fetchall()
            return id(db), threading.current_thread().name

        handles = await asyncio.gather(
            sqlite_runtime.run_read(_read), sqlite_runtime.run_read(_read)
        )

        assert handles[0][0] != handles[1][0]
        assert handles[0][1] != handles[1][1]
        assert all(name.startswith("life-memory-db") for _, name in handles)


class TestReaderPoolGeneration:
    """代际校验：关闭再打开同一个文件不得复用死句柄。"""

    async def test_rebinding_same_path_replaces_closed_handles(
        self, memory_db
    ) -> None:
        db_path, _ = memory_db

        def _count(db: sqlite3.Connection) -> int:
            return db.execute("SELECT COUNT(*) FROM probe").fetchone()[0]

        # 先让执行线程把读句柄缓存进 thread-local
        first = await asyncio.gather(
            *[sqlite_runtime.run_read(_count) for _ in range(4)]
        )
        assert first == [1, 1, 1, 1]

        # 服务关闭：句柄被立即关掉（它们钉住 WAL，不能等线程自己回收）
        sqlite_runtime.bind_reader_pool(None)
        # 服务在同一个文件上重新打开
        sqlite_runtime.bind_reader_pool(db_path)

        # 仅比较路径的实现会在这里抛 "Cannot operate on a closed database"
        second = await asyncio.gather(
            *[sqlite_runtime.run_read(_count) for _ in range(4)]
        )
        assert second == [1, 1, 1, 1]

    async def test_rebinding_to_another_database_is_observed(
        self, tmp_path: Path, memory_db
    ) -> None:
        other_path = tmp_path / "other.db"
        other = sqlite_runtime.open_memory_connection(other_path, role="writer")
        other.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)")
        other.executemany(
            "INSERT INTO probe (id, value) VALUES (?, ?)", [(1, "x"), (2, "y")]
        )
        other.commit()

        def _count(db: sqlite3.Connection) -> int:
            return db.execute("SELECT COUNT(*) FROM probe").fetchone()[0]

        try:
            assert await sqlite_runtime.run_read(_count) == 1
            sqlite_runtime.bind_reader_pool(other_path)
            assert await sqlite_runtime.run_read(_count) == 2
        finally:
            other.close()

    async def test_read_without_binding_raises(self) -> None:
        sqlite_runtime.bind_reader_pool(None)
        with pytest.raises(RuntimeError, match="尚未绑定数据库"):
            await sqlite_runtime.run_read(lambda db: None)


class TestDedicatedExecutor:
    """记忆库的线程池必须与解释器默认 executor 隔离。"""

    async def test_run_db_uses_dedicated_pool(self) -> None:
        def _who() -> str:
            return threading.current_thread().name

        name = await sqlite_runtime.run_db(_who)
        default_name = await asyncio.to_thread(_who)

        assert name.startswith("life-memory-db")
        assert not default_name.startswith("life-memory-db")

    @pytest.mark.parametrize("value", ["0", "-3", "many"])
    def test_invalid_worker_count_raises_instead_of_falling_back(
        self, monkeypatch, value: str
    ) -> None:
        monkeypatch.setenv(sqlite_runtime._EXECUTOR_ENV_VAR, value)
        with pytest.raises(ValueError):
            sqlite_runtime._resolve_max_workers()

    def test_blank_worker_count_uses_declared_default(self, monkeypatch) -> None:
        monkeypatch.setenv(sqlite_runtime._EXECUTOR_ENV_VAR, "  ")
        assert (
            sqlite_runtime._resolve_max_workers()
            == sqlite_runtime._DEFAULT_MAX_WORKERS
        )

    async def test_submission_queue_is_actually_bounded(self, monkeypatch) -> None:
        """A stalled disk must not create an unbounded executor backlog."""
        sqlite_runtime.shutdown_db_runtime()
        monkeypatch.setenv(sqlite_runtime._EXECUTOR_ENV_VAR, "1")
        monkeypatch.setenv(sqlite_runtime._QUEUE_ENV_VAR, "1")
        started = threading.Event()
        release = threading.Event()

        def _block() -> None:
            started.set()
            release.wait(timeout=5.0)

        first = asyncio.create_task(sqlite_runtime.run_db(_block))
        try:
            assert await asyncio.to_thread(started.wait, 2.0)
            second = asyncio.create_task(sqlite_runtime.run_db(lambda: None))
            for _ in range(100):
                if sqlite_runtime.get_db_runtime_stats()["inflight"] == 2:
                    break
                await asyncio.sleep(0.01)

            with pytest.raises(sqlite_runtime.MemoryDatabaseOverloaded):
                await sqlite_runtime.run_db(lambda: None)

            assert sqlite_runtime.get_db_runtime_stats()["rejected_total"] >= 1
            release.set()
            await asyncio.gather(first, second)
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            sqlite_runtime.shutdown_db_runtime()
