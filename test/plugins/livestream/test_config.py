from __future__ import annotations

import pytest
from pydantic import ValidationError

from plugins.livestream.config import LivestreamConfig
from plugins.livestream.platform.factory import create_platform_adapter
from plugins.livestream.plugin import LivestreamPlugin


def test_new_livestream_install_registers_no_router_until_explicitly_enabled() -> None:
    config = LivestreamConfig()
    assert config.plugin.enabled is False
    assert LivestreamPlugin(config).get_components() == []

    enabled_config = LivestreamConfig(plugin={"enabled": True})
    assert [
        component.__name__
        for component in LivestreamPlugin(enabled_config).get_components()
    ] == ["LivestreamRouter"]


def test_livestream_can_only_be_started_manually() -> None:
    config = LivestreamConfig()
    assert config.plugin.auto_start is False

    with pytest.raises(ValidationError, match="auto_start is forbidden"):
        LivestreamConfig(plugin={"enabled": True, "auto_start": True})


def test_resource_limits_are_validated() -> None:
    with pytest.raises(ValidationError):
        LivestreamConfig(director={"batch_limit": 0})
    with pytest.raises(ValidationError):
        LivestreamConfig(tts={"max_audio_bytes": 1})
    with pytest.raises(ValidationError):
        LivestreamConfig(
            platform={
                "platform_type": "bilibili",
                "reconnect_interval": 5,
                "max_reconnect_interval": 4,
            }
        )
    with pytest.raises(ValidationError):
        LivestreamConfig(server={"allowed_origins": ["https://example.com/path"]})


def test_bilibili_credentials_are_loaded_only_from_environment(monkeypatch) -> None:
    config = LivestreamConfig(
        platform={
            "room_id": "42",
            "sessdata": "plaintext-must-not-be-used",
            "buvid3": "plaintext-must-not-be-used",
        }
    )
    adapter = create_platform_adapter(config)
    assert adapter._sessdata == ""
    assert adapter._buvid3 == ""

    monkeypatch.setenv("ELYSIUM_BILIBILI_SESSDATA", "from-env")
    monkeypatch.setenv("ELYSIUM_BILIBILI_BUVID3", "from-env-too")
    adapter = create_platform_adapter(config)
    assert adapter._sessdata == "from-env"
    assert adapter._buvid3 == "from-env-too"
