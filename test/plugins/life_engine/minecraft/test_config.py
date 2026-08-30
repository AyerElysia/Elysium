"""Minecraft configuration compatibility tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plugins.life_engine.core.config import LifeEngineConfig


def test_production_minecraft_defaults_are_version_and_world_pinned() -> None:
    """An enabled default cannot silently launch another version or stop at a menu."""

    minecraft = LifeEngineConfig().minecraft

    assert minecraft.mc_version == "1.21.1"
    assert minecraft.world_name == "Elysian Realm"
    assert minecraft.require_quick_play is True
    assert minecraft.expected_bridge_version == "0.2.1"
    assert len(minecraft.expected_bridge_sha256) == 64
    assert len(minecraft.expected_baritone_sha256) == 64
    assert minecraft.intent_timeout_seconds == 300.0
    assert minecraft.consciousness_enabled is True
    assert minecraft.consciousness_task_name == "agent"
    assert minecraft.consciousness_subject_context_max_bytes == 16384
    assert minecraft.consciousness_observation_max_bytes == 16384
    assert minecraft.consciousness_subconscious_max_bytes == 8192
    assert minecraft.consciousness_min_wait_seconds == 2.0
    assert minecraft.consciousness_max_wait_seconds == 45.0


def test_zero_intent_timeout_is_normalized_to_unset() -> None:
    """An auto-generated TOML zero must preserve the optional timeout meaning."""

    config = LifeEngineConfig.model_validate(
        {"minecraft": {"intent_timeout_seconds": 0.0}}
    )

    assert config.minecraft.intent_timeout_seconds is None


def test_negative_intent_timeout_remains_invalid() -> None:
    """Negative execution lifetimes must not bypass validation."""

    with pytest.raises(ValidationError):
        LifeEngineConfig.model_validate({"minecraft": {"intent_timeout_seconds": -1.0}})


def test_consciousness_transport_budgets_fail_closed() -> None:
    """Identity, observation, and subconscious projections stay explicitly bounded."""

    for field, value in (
        ("consciousness_subject_context_max_bytes", 8191),
        ("consciousness_observation_max_bytes", 4095),
        ("consciousness_subconscious_max_bytes", 1023),
        ("consciousness_subconscious_group_limit", 0),
    ):
        with pytest.raises(ValidationError):
            LifeEngineConfig.model_validate({"minecraft": {field: value}})
