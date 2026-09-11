"""life_engine 配置验证测试。

测试 P0 修复：配置格式验证
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.life_engine.core.config import LifeEngineConfig


def test_heartbeat_tool_round_safety_defaults() -> None:
    settings = LifeEngineConfig.SettingsSection()

    assert settings.max_rounds_per_heartbeat == 5
    assert settings.max_consecutive_tool_stalls_per_heartbeat == 2
    assert settings.heartbeat_panel_sink == "file"
    assert settings.heartbeat_panel_path == "logs/heartbeat.console"


def test_background_cognition_uses_quality_first_total_deadlines() -> None:
    config = LifeEngineConfig()

    assert config.memory_witness.enabled is False
    assert config.memory_witness.timeout_seconds == 600.0
    assert config.curiosity.timeout_seconds == 300.0


def test_memory_witness_defaults_to_operator_retirement() -> None:
    section = LifeEngineConfig.MemoryWitnessSection()

    assert section.enabled is False


def test_chatter_uses_subject_authored_context_stewardship_defaults() -> None:
    chatter = LifeEngineConfig.ChatterSection()

    assert chatter.max_rounds_per_chat == 0
    assert chatter.context_stewardship_enabled is True
    assert chatter.context_pressure_ratio == 0.75
    assert chatter.context_pressure_max_groups == 24
    assert chatter.self_continuity_checkpoint_max_bytes == 32 * 1024
    assert chatter.context_emergency_reference_max_bytes == 8 * 1024


@pytest.mark.parametrize("limit", [0, 1, 5, 100])
def test_chatter_round_limit_explicit_values(limit: int) -> None:
    assert LifeEngineConfig.ChatterSection(max_rounds_per_chat=limit).max_rounds_per_chat == limit


def test_chatter_round_limit_toml_round_trip(tmp_path: Path) -> None:
    config_path = tmp_path / "rounds.toml"
    config_path.write_text("[chatter]\nmax_rounds_per_chat = 0\n", encoding="utf-8")
    config = LifeEngineConfig.load(config_path, auto_update=True)
    assert config.chatter.max_rounds_per_chat == 0
    assert LifeEngineConfig.load(config_path).chatter.max_rounds_per_chat == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context_pressure_ratio", 0.09),
        ("max_rounds_per_chat", -1),
        ("context_pressure_ratio", 1.0),
        ("context_pressure_max_groups", 0),
        ("context_pressure_max_groups", 65),
        ("self_continuity_checkpoint_max_bytes", 1023),
        ("self_continuity_checkpoint_max_bytes", 65 * 1024),
        ("context_emergency_reference_max_bytes", 255),
        ("context_emergency_reference_max_bytes", 33 * 1024),
    ],
)
def test_chatter_context_stewardship_rejects_invalid_bounds(
    field: str,
    value: float,
) -> None:
    with pytest.raises(ValueError):
        LifeEngineConfig.ChatterSection(**{field: value})


def test_retired_compaction_thresholds_no_longer_govern_subject_continuity() -> None:
    chatter = LifeEngineConfig.ChatterSection(
        context_compaction_trigger_chars=2_000,
        context_compaction_target_chars=3_000,
    )

    assert chatter.context_compaction_target_chars == 3_000
    assert chatter.context_stewardship_enabled is True


@pytest.mark.parametrize(
    "section_type",
    [
        LifeEngineConfig.MemoryWitnessSection,
        LifeEngineConfig.CuriositySection,
    ],
)
def test_background_cognition_deadlines_allow_up_to_fifteen_minutes(
    section_type: type,
) -> None:
    assert section_type(timeout_seconds=900.0).timeout_seconds == 900.0
    with pytest.raises(ValueError):
        section_type(timeout_seconds=900.1)


def test_learning_uses_quality_first_background_model_contract() -> None:
    learning = LifeEngineConfig.LearningSection()

    assert learning.model_task_name == "learning"
    assert learning.llm_timeout_seconds == 900.0
    visible = LifeEngineConfig.__config_schema_visible_fields__["learning"]
    assert {"model_task_name", "llm_timeout_seconds"} <= visible


@pytest.mark.parametrize("timeout", [29.0, 3601.0])
def test_learning_rejects_out_of_range_llm_timeout(timeout: float) -> None:
    with pytest.raises(ValueError):
        LifeEngineConfig.LearningSection(llm_timeout_seconds=timeout)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_rounds_per_heartbeat", 0),
        ("max_rounds_per_heartbeat", 6),
        ("max_consecutive_tool_stalls_per_heartbeat", 0),
        ("max_consecutive_tool_stalls_per_heartbeat", 6),
    ],
)
def test_heartbeat_tool_round_safety_rejects_out_of_range(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValueError):
        LifeEngineConfig.SettingsSection(**{field: value})


def test_heartbeat_panel_sink_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="heartbeat_panel_sink"):
        LifeEngineConfig.SettingsSection(heartbeat_panel_sink="tty")


def test_life_config_rejects_removed_storage_section() -> None:
    """生命域配置不得重新取得 generation 或后端选择配置。"""
    with pytest.raises(ValueError):
        LifeEngineConfig(  # type: ignore[call-arg]
            storage={"authoritative_backend": "mysql"}
        )


def test_life_config_rejects_removed_mysql_connection_section() -> None:
    """MySQL 连接只能配置在全局 Core 配置中。"""
    with pytest.raises(ValueError):
        LifeEngineConfig(  # type: ignore[call-arg]
            storage_mysql={"host": "duplicate.example"}
        )


def test_auto_update_retires_legacy_thought_authority_sections(
    tmp_path: Path,
) -> None:
    """旧思考流/冲动配置不得继续成为可启用的第二套主体权威。"""

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[streams]
enabled = true
sync_to_chatter = true

[drives]
enabled = true

[runtime_sync]
latest_action_think_enabled = false
recent_chat_messages = 7
""".lstrip(),
        encoding="utf-8",
    )

    loaded = LifeEngineConfig.load(config_path, auto_update=True)
    assert loaded.opportunity.enabled is False
    assert loaded.opportunity.poll_interval_seconds == 5.0
    assert "poll_interval_seconds" not in loaded.learning.model_dump()
    assert LifeEngineConfig.load(config_path).model_dump() == loaded.model_dump()


def test_opportunity_config_round_trip_preserves_distinct_learning_values(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "opportunity.toml"
    config_path.write_text(
        "[opportunity]\nenabled = true\npoll_interval_seconds = 11.0\n"
        "[learning]\nenabled = false\nllm_timeout_seconds = 777.0\n",
        encoding="utf-8",
    )

    loaded = LifeEngineConfig.load(config_path, auto_update=True)
    assert loaded.opportunity.enabled is True
    assert loaded.opportunity.poll_interval_seconds == 11.0
    assert loaded.learning.enabled is False
    assert loaded.learning.llm_timeout_seconds == 777.0
    assert "poll_interval_seconds" not in loaded.learning.model_dump()
    generated = config_path.read_bytes()
    assert LifeEngineConfig.load(config_path).model_dump() == loaded.model_dump()
    assert LifeEngineConfig.load(config_path, auto_update=True) == loaded
    assert config_path.read_bytes() == generated


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


def test_life_engine_toml_section_names_are_unique() -> None:
    """learning and opportunity must remain distinct TOML tables."""

    from src.kernel.config.core import _iter_sections

    names = [section.name for section in _iter_sections(LifeEngineConfig)]
    assert len(names) == len(set(names))
    assert "learning" in names
    assert "opportunity" in names


def test_opportunity_runtime_defaults_disabled() -> None:
    assert LifeEngineConfig().opportunity.enabled is False
