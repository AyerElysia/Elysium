"""Minecraft configuration compatibility tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plugins.life_engine.core.config import LifeEngineConfig


def test_zero_intent_timeout_is_normalized_to_unset() -> None:
    """An auto-generated TOML zero must preserve the optional timeout meaning."""

    config = LifeEngineConfig.model_validate(
        {"minecraft": {"intent_timeout_seconds": 0.0}}
    )

    assert config.minecraft.intent_timeout_seconds is None


def test_negative_intent_timeout_remains_invalid() -> None:
    """Negative execution lifetimes must not bypass validation."""

    with pytest.raises(ValidationError):
        LifeEngineConfig.model_validate(
            {"minecraft": {"intent_timeout_seconds": -1.0}}
        )
