"""Minecraft configuration compatibility tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from plugins.minecraft.config import MinecraftConfig
from plugins.minecraft.launcher import MCConfig


def test_production_minecraft_defaults_are_version_and_world_pinned() -> None:
    """An enabled default cannot silently launch another version or stop at a menu."""

    minecraft = MinecraftConfig().settings

    assert minecraft.mc_version == "1.21.1"
    assert minecraft.world_name == "Elysian Realm"
    assert minecraft.require_quick_play is True
    assert minecraft.expected_bridge_version == "0.2.1"
    assert len(minecraft.expected_bridge_sha256) == 64
    assert len(minecraft.expected_baritone_sha256) == 64
    assert minecraft.intent_timeout_seconds == 300.0
    assert minecraft.consciousness_enabled is True
    assert minecraft.consciousness_task_name == "agent"
    assert minecraft.default_body == "bot"
    assert minecraft.consciousness_subject_context_max_bytes == 8192
    assert minecraft.consciousness_observation_max_bytes == 8192
    assert minecraft.consciousness_subconscious_max_bytes == 4096
    assert minecraft.consciousness_subconscious_group_limit == 3
    assert minecraft.consciousness_recent_turn_limit == 4
    assert minecraft.consciousness_min_wait_seconds == 2.0
    assert minecraft.consciousness_max_wait_seconds == 45.0
    assert minecraft.offline_username == "AyerElysia"
    assert minecraft.agent_shared_username == "Elysia"
    assert minecraft.bot_username == "Elysia"
    assert minecraft.offline_username != minecraft.agent_shared_username
    assert minecraft.offline_username != minecraft.bot_username


def test_enabled_minecraft_defaults_construct_distinct_native_identities() -> None:
    """Enabling Minecraft with schema defaults must not collide with the human account."""

    section = MinecraftConfig.SettingsSection(enabled=True)
    fields = set(MCConfig.__dataclass_fields__)
    values = {
        name: getattr(section, name) for name in fields if hasattr(section, name)
    }
    for name in ("mc_home", "agent_token_file", "biomimetic_token_file"):
        values[name] = Path(values[name])
    MCConfig(**values)


def test_enabled_minecraft_rejects_reused_human_account_name() -> None:
    with pytest.raises(ValidationError, match="native client must not reuse"):
        MinecraftConfig.SettingsSection(
            enabled=True,
            offline_username="Elysia",
            agent_shared_username="Elysia",
        )


def test_disabled_minecraft_allows_legacy_colliding_names_until_enabled() -> None:
    section = MinecraftConfig.SettingsSection(
        enabled=False,
        offline_username="Elysia",
        agent_shared_username="Elysia",
    )

    assert section.enabled is False
    assert section.offline_username == section.agent_shared_username


def test_zero_intent_timeout_is_normalized_to_unset() -> None:
    """An auto-generated TOML zero must preserve the optional timeout meaning."""

    config = MinecraftConfig.model_validate(
        {"settings": {"intent_timeout_seconds": 0.0}}
    )

    assert config.settings.intent_timeout_seconds is None


def test_negative_intent_timeout_remains_invalid() -> None:
    """Negative execution lifetimes must not bypass validation."""

    with pytest.raises(ValidationError):
        MinecraftConfig.model_validate({"settings": {"intent_timeout_seconds": -1.0}})


def test_consciousness_transport_budgets_fail_closed() -> None:
    """Identity, observation, and subconscious projections stay explicitly bounded."""

    for field, value in (
        ("consciousness_subject_context_max_bytes", 8191),
        ("consciousness_observation_max_bytes", 4095),
        ("consciousness_subconscious_max_bytes", 1023),
        ("consciousness_subconscious_group_limit", 0),
    ):
        with pytest.raises(ValidationError):
            MinecraftConfig.model_validate({"settings": {field: value}})
