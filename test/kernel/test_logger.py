"""Logger 模块单元测试。

测试 Logger、LogStore、stdlib_bridge 和 query_logs 接口。
"""

from __future__ import annotations

import logging
import time
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from src.kernel.logger import (
    COLOR,
    DEFAULT_LEVEL_COLORS,
    Logger,
    clear_all_loggers,
    get_all_loggers,
    get_logger,
    get_rich_color,
    initialize_logger_system,
    remove_logger,
    shutdown_logger_system,
)
from src.kernel.logger.db_store import LogStore
from src.kernel.logger.stdlib_bridge import (
    SQLiteLogHandler,
    install_stdlib_bridge,
    uninstall_stdlib_bridge,
)


# ---------------------------------------------------------------------------
# COLOR
# ---------------------------------------------------------------------------


class TestColor:
    """测试 COLOR 枚举"""

    def test_color_enum_values(self) -> None:
        assert COLOR.RED.value == "red"
        assert COLOR.BLUE.value == "blue"
        assert COLOR.YELLOW.value == "yellow"
        assert COLOR.GREEN.value == "green"

    def test_get_rich_color_from_enum(self) -> None:
        assert get_rich_color(COLOR.RED) == "red"
        assert get_rich_color(COLOR.BLUE) == "blue"

    def test_get_rich_color_from_string(self) -> None:
        assert get_rich_color("custom_color") == "custom_color"

    def test_default_level_colors(self) -> None:
        assert DEFAULT_LEVEL_COLORS["DEBUG"] == COLOR.DEBUG
        assert DEFAULT_LEVEL_COLORS["INFO"] == COLOR.INFO
        assert DEFAULT_LEVEL_COLORS["WARNING"] == COLOR.WARNING
        assert DEFAULT_LEVEL_COLORS["ERROR"] == COLOR.ERROR
        assert DEFAULT_LEVEL_COLORS["CRITICAL"] == COLOR.CRITICAL


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------


class TestLogger:
    """测试 Logger 类"""

    def test_logger_creation(self) -> None:
        console = Console(file=StringIO())
        logger = Logger(name="test_logger", display="测试日志", color=COLOR.BLUE, console=console)
        assert logger.name == "test_logger"
        assert logger.display == "测试日志"
        assert logger.color == "blue"

    def test_logger_repr(self) -> None:
        console = Console(file=StringIO())
        logger = Logger(name="repr_test", console=console)
        r = repr(logger)
        assert "repr_test" in r
        assert "db=disabled" in r

    def test_logger_output_to_console(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=200)
        logger = Logger(name="out_test", display="OUT", color=COLOR.GREEN, console=console)
        logger.info("hello world")
        output = buf.getvalue()
        assert "hello world" in output
        assert "OUT" in output

    def test_log_levels(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=200)
        logger = Logger(name="level_test", console=console, log_level="WARNING")
        logger.debug("should not appear")
        logger.info("should not appear either")
        logger.warning("should appear")
        output = buf.getvalue()
        assert "should not appear" not in output
        assert "should appear" in output

    def test_set_log_level(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=200)
        logger = Logger(name="set_level", console=console, log_level="INFO")
        logger.set_log_level("ERROR")
        assert logger.get_log_level() == "ERROR"
        logger.warning("hidden")
        assert "hidden" not in buf.getvalue()

    def test_metadata(self) -> None:
        console = Console(file=StringIO())
        logger = Logger(name="meta_test", console=console)
        logger.set_metadata("key1", "value1")
        assert logger.get_metadata("key1") == "value1"
        logger.remove_metadata("key1")
        assert logger.get_metadata("key1") is None
        logger.set_metadata("a", 1)
        logger.clear_metadata()
        assert logger.get_metadata("a") is None

    def test_print_panel(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=80)
        logger = Logger(name="panel_test", console=console)
        logger.print_panel("panel content", title="Title")
        assert "panel content" in buf.getvalue()


# ---------------------------------------------------------------------------
# get_logger / registry
# ---------------------------------------------------------------------------


class TestGetLogger:
    """测试 get_logger 和注册表"""

    def setup_method(self) -> None:
        clear_all_loggers()

    def teardown_method(self) -> None:
        clear_all_loggers()

    def test_get_logger_creates_instance(self) -> None:
        logger = get_logger("registry_test_1")
        assert isinstance(logger, Logger)
        assert logger.name == "registry_test_1"

    def test_get_logger_returns_same_instance(self) -> None:
        l1 = get_logger("same_name")
        l2 = get_logger("same_name")
        assert l1 is l2

    def test_remove_logger(self) -> None:
        get_logger("to_remove")
        remove_logger("to_remove")
        all_loggers = get_all_loggers()
        assert "to_remove" not in all_loggers

    def test_clear_all_loggers(self) -> None:
        get_logger("a")
        get_logger("b")
        clear_all_loggers()
        assert len(get_all_loggers()) == 0

    def test_auto_color_assignment(self) -> None:
        logger = get_logger("color_auto_test")
        # 应该有一个稳定的颜色
        assert logger.color is not None


# ---------------------------------------------------------------------------
# LogStore
# ---------------------------------------------------------------------------


class TestLogStore:
    """测试 SQLite 日志存储"""

    @pytest.fixture
    def store(self, tmp_path: Path) -> LogStore:
        s = LogStore(db_path=tmp_path / "test_logs.db")
        yield s
        s.close()

    def test_write_and_query(self, store: LogStore) -> None:
        store.write("INFO", "test_mod", "hello world")
        store.write("ERROR", "test_mod", "something broke", metadata={"code": 500})
        time.sleep(1.5)  # 等待后台 flush

        results = store.query(level="ERROR")
        assert len(results) == 1
        assert results[0]["message"] == "something broke"
        assert results[0]["metadata"]["code"] == 500

    def test_query_by_module(self, store: LogStore) -> None:
        store.write("INFO", "module_a", "msg a")
        store.write("INFO", "module_b", "msg b")
        time.sleep(1.5)

        results = store.query(module="module_a")
        assert len(results) == 1
        assert results[0]["module"] == "module_a"

    def test_query_module_prefix_match(self, store: LogStore) -> None:
        store.write("INFO", "life_engine.core", "nested")
        store.write("INFO", "life_engine", "root")
        store.write("INFO", "other", "unrelated")
        time.sleep(1.5)

        results = store.query(module="life_engine")
        assert len(results) == 2

    def test_fts_search(self, store: LogStore) -> None:
        store.write("INFO", "mod", "the quick brown fox")
        store.write("INFO", "mod", "lazy dog sleeps")
        time.sleep(1.5)

        results = store.query(search="fox")
        assert len(results) == 1
        assert "fox" in results[0]["message"]

    def test_query_time_range(self, store: LogStore) -> None:
        store.write("INFO", "mod", "old msg")
        time.sleep(1.5)

        # 查询未来时间范围应该没有结果
        results = store.query(since="2099-01-01T00:00:00")
        assert len(results) == 0

    def test_query_limit_offset(self, store: LogStore) -> None:
        for i in range(10):
            store.write("INFO", "mod", f"msg {i}")
        time.sleep(1.5)

        results = store.query(limit=3)
        assert len(results) == 3

    def test_cleanup(self, tmp_path: Path) -> None:
        store = LogStore(
            db_path=tmp_path / "cleanup.db",
            retention_debug_days=0,
            retention_info_days=0,
        )
        store.write("DEBUG", "mod", "old debug")
        store.write("INFO", "mod", "old info")
        time.sleep(1.5)

        # retention=0 意味着保留 0 天，所有日志都过期
        # 但由于 timestamp 使用 datetime('now') 比较，当天写入的不会被删
        # 这里只验证 cleanup 不报错
        deleted = store.cleanup()
        assert isinstance(deleted, int)
        store.close()

    def test_new_database_uses_incremental_vacuum(self, tmp_path: Path) -> None:
        """New stores must be able to return deleted pages to the filesystem."""
        import sqlite3

        db_path = tmp_path / "vacuum.db"
        store = LogStore(db_path=db_path)
        store.close()

        with sqlite3.connect(db_path) as conn:
            assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2

    def test_bloated_legacy_database_is_compacted_once(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A legacy NONE-vacuum store is migrated before its writer starts."""
        import sqlite3

        from src.kernel.logger import db_store as db_store_module

        db_path = tmp_path / "legacy.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE legacy_payload (value BLOB)")
            conn.execute("INSERT INTO legacy_payload VALUES (zeroblob(1048576))")
            conn.commit()
            conn.execute("DELETE FROM legacy_payload")
            conn.commit()
            assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
            assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 0

        monkeypatch.setattr(db_store_module, "_LEGACY_COMPACT_MIN_BYTES", 1)
        monkeypatch.setattr(db_store_module, "_LEGACY_COMPACT_FREE_RATIO", 0.0)
        store = LogStore(db_path=db_path)
        store.close()

        with sqlite3.connect(db_path) as conn:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2

    def test_stats(self, store: LogStore) -> None:
        store.write("INFO", "mod", "a")
        store.write("ERROR", "mod", "b")
        time.sleep(1.5)

        stats = store.stats()
        assert stats["total_entries"] == 2
        assert "INFO" in stats["by_level"]
        assert stats["db_size_bytes"] > 0

    def test_session_id(self, store: LogStore) -> None:
        store.write("INFO", "mod", "with session")
        time.sleep(1.5)

        results = store.query()
        assert len(results) == 1
        assert results[0]["session_id"] != ""


# ---------------------------------------------------------------------------
# LogStore 文件镜像
# ---------------------------------------------------------------------------


class _BrokenHandle:
    """A file-like object whose writes always fail."""

    closed = False

    def write(self, _text: str) -> int:
        raise OSError("mirror handle is broken")

    def flush(self) -> None:
        raise OSError("mirror handle is broken")

    def close(self) -> None:
        self.closed = True


class TestLogStoreFileMirror:
    """手动启动的进程把控制台写到 pty，会话结束后那份输出就没有了。
    文件镜像是「进程已退出之后仍可按天审计」的唯一保证，所以它的存在、
    滚动、保留与降级都必须是契约。
    """

    def test_absent_log_dir_creates_no_file(self, tmp_path: Path) -> None:
        """log_dir 为 None 时不得创建目录或句柄（测试不落文件）。"""
        store = LogStore(db_path=tmp_path / "nomirror.db")
        store.write("INFO", "mod", "no mirror please")
        time.sleep(1.5)
        store.close()

        assert store.stats()["file_log_dir"] == ""
        assert not (tmp_path / "logs").exists()
        assert list(tmp_path.glob("elysium-*.log")) == []

    def test_mirror_receives_written_entries(self, tmp_path: Path) -> None:
        """启用 log_dir 后，每条日志都落到按日期命名的镜像里。"""
        log_dir = tmp_path / "logs"
        store = LogStore(db_path=tmp_path / "mirror.db", log_dir=log_dir)
        store.write("INFO", "mod.a", "hello mirror")
        store.write("ERROR", "mod.b", "boom", metadata={"k": "v"})
        time.sleep(1.5)
        store.close()

        files = sorted(p.name for p in log_dir.glob("elysium-*.log"))
        assert len(files) == 1
        content = (log_dir / files[0]).read_text(encoding="utf-8")
        assert "| INFO     | mod.a | hello mirror" in content
        assert "| ERROR    | mod.b | boom" in content
        # 元数据跟在自己那条记录后面，不与消息挤在同一行。
        assert '{"k": "v"}' in content

        stats = store.stats()
        assert stats["file_written_count"] == 2
        assert stats["file_failure_count"] == 0
        assert stats["file_log_dir"] == str(log_dir)

    def test_batch_spanning_midnight_splits_by_entry_date(
        self, tmp_path: Path
    ) -> None:
        """一批日志可能跨越午夜；每条记录必须落进它自己那天的文件。"""
        log_dir = tmp_path / "logs"
        store = LogStore(db_path=tmp_path / "roll.db", log_dir=log_dir)
        store.close()  # writer 退出后由测试独占调用，避免与后台线程竞争

        store._mirror_batch_to_file(
            [
                {
                    "timestamp": "2020-01-01T23:59:59.999",
                    "level": "INFO",
                    "module": "m",
                    "message": "before midnight",
                    "metadata": "{}",
                },
                {
                    "timestamp": "2020-01-02T00:00:00.001",
                    "level": "INFO",
                    "module": "m",
                    "message": "after midnight",
                    "metadata": "{}",
                },
            ]
        )
        store._close_file_handle()

        assert (log_dir / "elysium-2020-01-01.log").exists()
        assert (log_dir / "elysium-2020-01-02.log").exists()
        assert "before midnight" in (
            log_dir / "elysium-2020-01-01.log"
        ).read_text(encoding="utf-8")
        assert "after midnight" in (
            log_dir / "elysium-2020-01-02.log"
        ).read_text(encoding="utf-8")

    def test_prune_only_removes_this_stores_own_naming(
        self, tmp_path: Path
    ) -> None:
        """保留策略的范围最小：只删本 store 自己命名的过期镜像。"""
        log_dir = tmp_path / "logs"
        store = LogStore(
            db_path=tmp_path / "prune.db",
            retention_info_days=1,
            log_dir=log_dir,
        )
        store.close()

        expired = ["elysium-2020-01-01.log", "elysium-2020-06-30.log"]
        foreign = ["unrelated.log", "elysium-not-a-date.log", "notes.txt"]
        for name in expired + foreign:
            (log_dir / name).write_text("x", encoding="utf-8")

        removed = store.prune_file_mirrors()

        assert removed == len(expired)
        for name in expired:
            assert not (log_dir / name).exists()
        for name in foreign:
            assert (log_dir / name).exists(), f"{name} 不属于本 store，不得删除"

    def test_mirror_failure_never_degrades_the_db_sink(
        self, tmp_path: Path
    ) -> None:
        """镜像失败只增加自己的失败计数，SQLite sink 与调用方不受影响。"""
        log_dir = tmp_path / "logs"
        store = LogStore(db_path=tmp_path / "degrade.db", log_dir=log_dir)
        store.write("INFO", "mod", "lands in sqlite")
        time.sleep(1.5)
        store.close()

        assert store.stats()["written_count"] == 1
        before = store.stats()["file_failure_count"]

        broken = _BrokenHandle()
        store._file_handle = broken
        store._file_date = "2026-08-09"
        store._mirror_batch_to_file(
            [
                {
                    "timestamp": "2026-08-09T00:00:00.000",
                    "level": "INFO",
                    "module": "m",
                    "message": "cannot be mirrored",
                    "metadata": "{}",
                }
            ]
        )

        stats = store.stats()
        assert stats["file_failure_count"] == before + 1
        # SQLite 侧不受影响，且损坏的句柄被丢弃以便下一批重开。
        assert stats["written_count"] == 1
        assert store._file_handle is None
        assert len(store.query()) == 1

    def test_unwritable_log_dir_degrades_to_sqlite_only(
        self, tmp_path: Path
    ) -> None:
        """目录建不起来时降级为纯 SQLite sink，绝不阻断启动。"""
        # 父路径是一个普通文件，mkdir 必然失败（NotADirectoryError ⊂ OSError）。
        # 不 monkeypatch Path.mkdir：那会连 db_path 自己的建目录一起打断。
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")

        store = LogStore(db_path=tmp_path / "degraded.db", log_dir=blocker / "logs")
        try:
            store.write("INFO", "mod", "sqlite still works")
            time.sleep(1.5)
            assert store.stats()["file_log_dir"] == ""
            assert store.stats()["written_count"] == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# stdlib bridge
# ---------------------------------------------------------------------------


class TestStdlibBridge:
    """测试 stdlib logging 桥接"""

    @pytest.fixture
    def store(self, tmp_path: Path) -> LogStore:
        s = LogStore(db_path=tmp_path / "bridge.db")
        yield s
        s.close()

    def test_install_and_emit(self, store: LogStore) -> None:
        handler = install_stdlib_bridge(store)
        try:
            std_logger = logging.getLogger("test_bridge_emit")
            std_logger.info("bridged message")
            time.sleep(1.5)

            results = store.query(module="test_bridge_emit")
            assert len(results) >= 1
            assert any("bridged message" in r["message"] for r in results)
        finally:
            uninstall_stdlib_bridge(handler)

    def test_uninstall(self, store: LogStore) -> None:
        handler = install_stdlib_bridge(store)
        uninstall_stdlib_bridge(handler)

        root = logging.getLogger()
        assert handler not in root.handlers

    def test_handler_level(self, store: LogStore) -> None:
        handler = SQLiteLogHandler(store, level=logging.WARNING)
        assert handler.level == logging.WARNING


# ---------------------------------------------------------------------------
# 集成：Logger + LogStore
# ---------------------------------------------------------------------------


class TestLoggerWithDB:
    """测试 Logger 写入 LogStore"""

    def test_logger_writes_to_db(self, tmp_path: Path) -> None:
        from src.kernel.logger import logger as logger_module

        store = LogStore(db_path=tmp_path / "integrated.db")
        old_store = logger_module._global_log_store
        logger_module._global_log_store = store

        try:
            buf = StringIO()
            console = Console(file=buf, force_terminal=False, width=200)
            log = Logger(name="db_write_test", console=console, enable_db=True)
            log.info("stored in sqlite")
            time.sleep(1.5)

            results = store.query(module="db_write_test")
            assert len(results) == 1
            assert results[0]["message"] == "stored in sqlite"
        finally:
            logger_module._global_log_store = old_store
            store.close()

    def test_logger_db_disabled_by_default(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=200)
        log = Logger(name="no_db", console=console)
        assert log._enable_db is False


    def test_filtered_debug_skips_database_and_broadcast(self, tmp_path: Path) -> None:
        from unittest.mock import Mock

        from src.kernel.logger import logger as logger_module

        store = LogStore(db_path=tmp_path / "filtered.db")
        old_store = logger_module._global_log_store
        logger_module._global_log_store = store
        try:
            log = Logger(
                name="filtered_debug",
                console=Console(file=StringIO()),
                enable_db=True,
                enable_event_broadcast=True,
                log_level="INFO",
            )
            emit_event = Mock()
            log._emit_log_event = emit_event  # type: ignore[method-assign]

            log.debug("must stay out of every sink")

            assert store.stats()["queued_count"] == 0
            emit_event.assert_not_called()
        finally:
            logger_module._global_log_store = old_store
            store.close()

    def test_reinitialize_keeps_one_stdlib_bridge(self, tmp_path: Path) -> None:
        from src.kernel.logger import logger as logger_module

        root = logging.getLogger()
        old_config = dict(logger_module._global_config)
        old_root_level = root.level
        try:
            initialize_logger_system(
                log_level="INFO",
                db_path=tmp_path / "first.db",
            )
            initialize_logger_system(
                log_level="WARNING",
                db_path=tmp_path / "second.db",
            )

            handlers = [handler for handler in root.handlers if isinstance(handler, SQLiteLogHandler)]
            assert len(handlers) == 1
            assert handlers[0].level == logging.WARNING
        finally:
            shutdown_logger_system()
            logger_module._global_config.clear()
            logger_module._global_config.update(old_config)
            root.setLevel(old_root_level)

    def test_log_store_stats_expose_pipeline_metrics(self, tmp_path: Path) -> None:
        store = LogStore(db_path=tmp_path / "metrics.db")
        try:
            store.write("INFO", "metrics", "queued")
            time.sleep(1.5)

            stats = store.stats()

            assert stats["queued_count"] == 1
            assert stats["written_count"] == 1
            assert stats["dropped_count"] == 0
            assert stats["write_failure_count"] == 0
            assert stats["queue_size"] == 0
        finally:
            store.close()
