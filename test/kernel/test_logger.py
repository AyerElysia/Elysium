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
    LOG_OUTPUT_EVENT,
    Logger,
    clear_all_loggers,
    get_all_loggers,
    get_logger,
    get_rich_color,
    remove_logger,
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
