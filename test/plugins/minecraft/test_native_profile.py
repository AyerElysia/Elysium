"""Independent account, directory, credential and process-ownership boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.minecraft import native_profile
from plugins.minecraft.launcher import MCConfig, MinecraftLauncher

TEMPLATE = (
    '@echo off\n"G:\\Java\\java.exe" -Xmx12G -Xms4G -cp "G:\\MC\\libraries\\client.jar" '
    '--username Human --uuid human-uuid --accessToken human-secret '
    '--clientId human-client --xuid human-xuid --userType msa '
    '--gameDir "G:\\MC\\human" --assetsDir "G:\\MC\\assets" '
    '--width 1280 --height 720 --quickPlaySingleplayer "Human World"\npause\n'
)
TARGET = r"G:\MC\Elysia"


def test_native_template_replaces_every_account_field_without_copying_the_world():
    output = native_profile.render_launch_script(
        TEMPLATE, game_directory=TARGET, username="Elysia", address="127.0.0.1:25565",
    )
    native_profile.validate_launch_script(
        output, game_directory=TARGET, username="Elysia", address="127.0.0.1:25565",
    )
    for private_value in ("human-secret", "human-client", "human-xuid", "human-uuid", "Human World"):
        assert private_value not in output
    assert "quickPlaySingleplayer" not in output
    assert "-Xmx4G" in output and "-Xms1G" in output
    assert 'G:\\MC\\libraries\\client.jar' in output
    assert '--accessToken "0"' in output
    assert native_profile.offline_uuid("Elysia") != native_profile.offline_uuid("Human")


@pytest.mark.parametrize("target", [r"G:\MC\human", r"G:\MC\human\child", r"G:\MC", r"G:\MC\bad&dir"])
def test_native_directory_cannot_share_or_nest_the_human_profile(target):
    with pytest.raises(ValueError):
        native_profile.render_launch_script(
            TEMPLATE, game_directory=target, username="Elysia", address="127.0.0.1:25565",
        )


def test_ambiguous_arguments_and_same_human_account_fail_closed():
    for template, username in ((TEMPLATE + TEMPLATE, "Elysia"), (TEMPLATE, "Human"),
                               (TEMPLATE.replace("--uuid human-uuid", "--uuid one --uuid two"), "Elysia")):
        with pytest.raises(ValueError):
            native_profile.render_launch_script(
                template, game_directory=TARGET, username=username, address="127.0.0.1:25565",
            )


async def test_shared_launcher_reuses_only_the_exact_isolated_process(monkeypatch):
    launcher = MinecraftLauncher(MCConfig())
    launcher._bridge = SimpleNamespace(find_window=AsyncMock(side_effect=AssertionError("human UI queried")))
    launcher.check_installation = AsyncMock(return_value={
        "isolated_profile_ready": True, "bridge_mod_ready": True, "baritone_mod_ready": True,
    })
    process_check = AsyncMock(return_value=123)
    launch = AsyncMock()
    monkeypatch.setattr(native_profile, "find_process", process_check)
    monkeypatch.setattr(native_profile, "launch_process", launch)
    result = await launcher.launch()
    assert result.success and result.reused_existing and result.pid == 123
    assert result.window is None
    launch.assert_not_awaited()
    launcher._bridge.find_window.assert_not_awaited()
    process_check.assert_awaited_once_with(
        launcher._cfg.agent_game_directory, launcher._cfg.agent_shared_username,
    )


async def test_shared_launcher_dispatches_only_the_prepared_isolated_script(monkeypatch):
    launcher = MinecraftLauncher(MCConfig())
    launcher.check_installation = AsyncMock(return_value={
        "isolated_profile_ready": True, "bridge_mod_ready": True, "baritone_mod_ready": True,
    })
    monkeypatch.setattr(native_profile, "find_process", AsyncMock(return_value=None))
    launch = AsyncMock()
    monkeypatch.setattr(native_profile, "launch_process", launch)
    result = await launcher.launch()
    assert result.success and not result.reused_existing and result.window is None
    launch.assert_awaited_once_with(launcher._cfg.agent_launch_bat)
