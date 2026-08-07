"""主入口配置读取测试。"""

import tomllib

import pytest

from main import load_ui_level_from_config, runtime_startup_guard
from src.app.runtime import UILevel
from src.app.runtime.single_instance import AlreadyRunningError, SingleInstanceLock


def test_load_ui_level_defaults_when_config_missing(tmp_path) -> None:
    """缺少配置文件时使用标准 UI 级别。"""
    missing_config = tmp_path / "missing.toml"

    assert load_ui_level_from_config(str(missing_config)) is UILevel.STANDARD


def test_load_ui_level_reads_valid_config(tmp_path) -> None:
    """读取有效 UI 级别。"""
    config_path = tmp_path / "core.toml"
    config_path.write_text('[bot]\nui_level = "verbose"\n', encoding="utf-8")

    assert load_ui_level_from_config(str(config_path)) is UILevel.VERBOSE


def test_load_ui_level_rejects_invalid_value(tmp_path) -> None:
    """非法 UI 级别必须显式报错。"""
    config_path = tmp_path / "core.toml"
    config_path.write_text('[bot]\nui_level = "debug"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid bot.ui_level"):
        load_ui_level_from_config(str(config_path))


def test_load_ui_level_rejects_invalid_toml(tmp_path) -> None:
    """损坏 TOML 不应被静默降级。"""
    config_path = tmp_path / "core.toml"
    config_path.write_text("[bot\n", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        load_ui_level_from_config(str(config_path))


def test_mysql_runtime_startup_guard_allows_concurrent_processes(tmp_path) -> None:
    """MySQL coordinates writers, so process-wide file exclusion is disabled."""

    config_path = tmp_path / "core.toml"
    config_path.write_text('[storage]\nbackend = "mysql"\n', encoding="utf-8")

    first = runtime_startup_guard(
        str(config_path), lock_path=tmp_path / "runtime" / "elysium.lock"
    )
    second = runtime_startup_guard(
        str(config_path), lock_path=tmp_path / "runtime" / "elysium.lock"
    )
    with first, second:
        pass


def test_local_runtime_startup_guard_keeps_single_process(tmp_path) -> None:
    """Local SQLite still rejects concurrent runtimes sharing one data path."""

    config_path = tmp_path / "core.toml"
    config_path.write_text('[storage]\nbackend = "local"\n', encoding="utf-8")

    first = runtime_startup_guard(
        str(config_path), lock_path=tmp_path / "runtime" / "elysium.lock"
    )
    second = runtime_startup_guard(
        str(config_path), lock_path=tmp_path / "runtime" / "elysium.lock"
    )
    with first, pytest.raises(AlreadyRunningError, match="Elysium 已在运行"):
        second.__enter__()


def test_single_instance_lock_rejects_second_owner_and_recovers(tmp_path) -> None:
    """Only one process may hold the runtime lock at a time."""
    lock_path = tmp_path / "runtime" / "elysium.lock"
    first = SingleInstanceLock(lock_path)
    second = SingleInstanceLock(lock_path)

    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError, match="Elysium 已在运行"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
