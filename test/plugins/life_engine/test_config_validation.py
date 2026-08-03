"""life_engine 配置验证测试。

测试 P0 修复：配置格式验证
"""

from __future__ import annotations

import pytest

from plugins.life_engine.core.config import LifeEngineConfig


def test_sleep_time_format_validation() -> None:
    """sleep_time 必须是 HH:MM 格式（24小时制）。"""
    with pytest.raises(ValueError) as exc_info:
        LifeEngineConfig(
            settings=LifeEngineConfig.SettingsSection(
                sleep_time="11PM",  # 错误格式
                wake_time="07:00",
            )
        )
    assert "sleep_time 格式必须是 HH:MM" in str(exc_info.value)


def test_wake_time_format_validation() -> None:
    """wake_time 必须是 HH:MM 格式（24小时制）。"""
    with pytest.raises(ValueError) as exc_info:
        LifeEngineConfig(
            settings=LifeEngineConfig.SettingsSection(
                sleep_time="23:00",
                wake_time="7AM",  # 错误格式
            )
        )
    assert "wake_time 格式必须是 HH:MM" in str(exc_info.value)


def test_sleep_wake_pair_required() -> None:
    """sleep_time 和 wake_time 必须同时设置或同时留空。"""
    with pytest.raises(ValueError) as exc_info:
        LifeEngineConfig(
            settings=LifeEngineConfig.SettingsSection(
                sleep_time="23:00",
                wake_time="",  # 另一个为空
            )
        )
    assert "sleep_time 和 wake_time 必须同时设置或同时留空" in str(exc_info.value)


def test_sleep_wake_cannot_be_equal() -> None:
    """sleep_time 和 wake_time 不能相同。"""
    with pytest.raises(ValueError) as exc_info:
        LifeEngineConfig(
            settings=LifeEngineConfig.SettingsSection(
                sleep_time="23:00",
                wake_time="23:00",  # 相同
            )
        )
    assert "sleep_time 和 wake_time 不能相同" in str(exc_info.value)


def test_valid_sleep_wake_times() -> None:
    """有效的 sleep_time 和 wake_time 应该正常工作。"""
    config = LifeEngineConfig(
        settings=LifeEngineConfig.SettingsSection(
            sleep_time="23:00",
            wake_time="07:00",
        )
    )
    assert config.settings.sleep_time == "23:00"
    assert config.settings.wake_time == "07:00"


def test_empty_sleep_wake_times_allowed() -> None:
    """留空的 sleep_time 和 wake_time 应该允许（禁用睡眠功能）。"""
    config = LifeEngineConfig(
        settings=LifeEngineConfig.SettingsSection(
            sleep_time="",
            wake_time="",
        )
    )
    assert config.settings.sleep_time == ""
    assert config.settings.wake_time == ""


def test_invalid_hour_in_time() -> None:
    """小时必须在 00-23 范围内。"""
    with pytest.raises(ValueError) as exc_info:
        LifeEngineConfig(
            settings=LifeEngineConfig.SettingsSection(
                sleep_time="25:00",  # 无效小时
                wake_time="07:00",
            )
        )
    assert "格式必须是 HH:MM" in str(exc_info.value)


def test_invalid_minute_in_time() -> None:
    """分钟必须在 00-59 范围内。"""
    with pytest.raises(ValueError) as exc_info:
        LifeEngineConfig(
            settings=LifeEngineConfig.SettingsSection(
                sleep_time="23:70",  # 无效分钟
                wake_time="07:00",
            )
        )
    assert "格式必须是 HH:MM" in str(exc_info.value)


def test_cross_day_sleep_window() -> None:
    """跨日睡眠窗口应该被允许（例如 23:00 ~ 07:00）。"""
    config = LifeEngineConfig(
        settings=LifeEngineConfig.SettingsSection(
            sleep_time="23:00",
            wake_time="07:00",
        )
    )
    assert config.settings.sleep_time == "23:00"
    assert config.settings.wake_time == "07:00"


def test_model_section_allows_dedicated_chatter_task() -> None:
    """主意识可以使用不同于潜意识心跳的模型任务。"""
    model = LifeEngineConfig.ModelSection(
        task_name="core",
        chatter_task_name="expression_large",
    )

    assert model.task_name == "core"
    assert model.chatter_task_name == "expression_large"


def test_model_section_keeps_chatter_task_optional() -> None:
    """未配置独立主意识任务时保留空值，由运行时跟随 task_name。"""
    model = LifeEngineConfig.ModelSection(task_name="core")

    assert model.task_name == "core"
    assert model.chatter_task_name == ""


def test_memory_archive_sync_exposes_every_operational_field() -> None:
    section = LifeEngineConfig.MemoryArchiveSyncSection(
        enabled=True,
        remote_host="mysql.example.test",
        remote_port=3307,
        remote_database="elysium",
        remote_user="archive",
        mysql_ssl_mode="verify-full",
        mysql_ssl_ca="/certs/ca.pem",
        mysql_ssl_cert="/certs/client.pem",
        mysql_ssl_key="/certs/client.key",
        connect_timeout_seconds=7,
        interval_seconds=60,
        retry_max_seconds=600,
        local_state_path=".memory/test-archive.sqlite3",
    )

    visible = LifeEngineConfig.__config_schema_visible_fields__["memory_archive_sync"]
    assert set(type(section).model_fields) <= visible
    assert section.mysql_ssl_mode == "verify-full"
    assert section.connect_timeout_seconds == 7
