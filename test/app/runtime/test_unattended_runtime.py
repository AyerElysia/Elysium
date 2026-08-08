"""无人值守运行时契约测试。"""

from __future__ import annotations

import asyncio
import queue
import signal
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from src.app.runtime.command_parser import CommandParser
from src.app.runtime.signal_handler import SignalHandler


def _command_parser_without_thread() -> CommandParser:
    """构造不启动 stdin 线程的解析器。"""
    parser = CommandParser.__new__(CommandParser)
    parser._input_stop_event = Mock()
    return parser


def test_stdin_eof_keeps_noninteractive_service_running(monkeypatch) -> None:
    """无 TTY 服务收到 EOF 后应关闭输入读取，但继续运行 Bot。"""
    parser = _command_parser_without_thread()
    monkeypatch.setattr(
        "src.app.runtime.command_parser.sys.stdin",
        SimpleNamespace(isatty=lambda: False),
    )

    assert parser._handle_stdin_eof() is True
    parser._input_stop_event.set.assert_called_once_with()


def test_stdin_eof_stops_interactive_session(monkeypatch) -> None:
    """真实终端中的 Ctrl+D 应保留结束交互主循环的传统语义。"""
    parser = _command_parser_without_thread()
    monkeypatch.setattr(
        "src.app.runtime.command_parser.sys.stdin",
        SimpleNamespace(isatty=lambda: True),
    )

    assert parser._handle_stdin_eof() is False
    parser._input_stop_event.set.assert_called_once_with()


def test_sighup_keeps_manually_managed_process_running() -> None:
    """Terminal detachment must not stop a manually managed service."""
    bot = SimpleNamespace(logger=Mock(), _running=True)
    handler = SignalHandler(bot)

    handler._handle_sighup(getattr(signal, "SIGHUP", 1), None)

    assert bot._running is True
    assert handler.is_shutdown_requested() is False


def test_sigterm_requests_graceful_shutdown() -> None:
    """首次 SIGTERM 应请求优雅关闭，而不是直接退出进程。"""
    bot = SimpleNamespace(logger=Mock(), _running=True)
    handler = SignalHandler(bot)
    handler._schedule_log = Mock()  # type: ignore[method-assign]

    handler._handle_signal(signal.SIGTERM, None)

    assert bot._running is False
    assert handler.is_shutdown_requested() is True
    assert handler.signal_count == 1
    handler._schedule_log.assert_called_once_with(
        "info",
        "已收到关闭信号 SIGTERM。再次按下 Ctrl+C 强制退出...",
    )


def test_wait_for_shutdown_signal_observes_request() -> None:
    """异步等待方应能观察到关闭请求。"""
    bot = SimpleNamespace(logger=Mock(), _running=True)
    handler = SignalHandler(bot)
    handler.shutdown_requested.set()

    asyncio.run(handler.wait_for_shutdown_signal())


def test_posix_input_worker_stops_without_waiting_for_tty_input(monkeypatch) -> None:
    """关闭时不能留下一个永远阻塞在 input() 的后台线程。"""
    parser = CommandParser.__new__(CommandParser)
    parser._input_queue = queue.Queue()
    parser._input_stop_event = threading.Event()
    select_entered = threading.Event()

    def _select_with_timeout(_readers, _writers, _errors, timeout):
        assert timeout == 0.2
        select_entered.set()
        time.sleep(0.01)
        return [], [], []

    monkeypatch.setattr("src.app.runtime.command_parser.os.name", "posix")
    monkeypatch.setattr(
        "src.app.runtime.command_parser.select.select",
        _select_with_timeout,
    )

    worker = threading.Thread(target=parser._input_worker)
    parser._input_thread = worker
    worker.start()
    assert select_entered.wait(timeout=1.0)

    parser.close()

    assert worker.is_alive() is False
    assert parser._input_queue.empty()


def test_posix_input_worker_delivers_complete_line(monkeypatch) -> None:
    """可读 TTY 仍应把一整行命令交给主循环。"""
    parser = CommandParser.__new__(CommandParser)
    parser._input_queue = queue.Queue()
    parser._input_stop_event = threading.Event()
    stdin = SimpleNamespace(readline=Mock(return_value="/status\n"))

    monkeypatch.setattr("src.app.runtime.command_parser.os.name", "posix")
    monkeypatch.setattr("src.app.runtime.command_parser.sys.stdin", stdin)
    monkeypatch.setattr(
        "src.app.runtime.command_parser.select.select",
        lambda readers, _writers, _errors, _timeout: (readers, [], []),
    )

    worker = threading.Thread(target=parser._input_worker)
    parser._input_thread = worker
    worker.start()
    assert parser._input_queue.get(timeout=1.0) == "/status"
    parser.close()

    assert worker.is_alive() is False
