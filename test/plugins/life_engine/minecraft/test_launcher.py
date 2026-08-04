"""Exact-process ownership tests for the Minecraft launcher."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from plugins.life_engine.minecraft.launcher import MCConfig, MinecraftLauncher


class _ExistingWindowBridge:
    """Expose one exact existing Minecraft window."""

    async def find_window(self) -> dict[str, Any]:
        """Return an already running client owned by a known process."""

        return {"pid": 4815, "title": "Minecraft NeoForge* 1.21.1"}


class _NoWindowBridge:
    """Capture the exact Windows-native launch request."""

    def __init__(self) -> None:
        self.launches: list[tuple[str, str]] = []

    async def find_window(self) -> None:
        return None

    async def launch_minecraft(self, script: str, directory: str) -> int:
        self.launches.append((script, directory))
        return 8123


class _TestableLauncher(MinecraftLauncher):
    """Bind installation checks to a temporary launch script."""

    def __init__(self, config: MCConfig, launch_script: Path) -> None:
        super().__init__(config)
        self._test_launch_script = launch_script

    def _launch_script_path(self) -> Path:
        return self._test_launch_script


async def test_launcher_reuses_exact_existing_client(tmp_path: Path) -> None:
    """Starting a session must not create a competing second game client."""

    launcher = MinecraftLauncher(MCConfig(mc_home=tmp_path))
    launcher._bridge = _ExistingWindowBridge()

    result = await launcher.launch()

    assert result.success is True
    assert result.reused_existing is True
    assert result.pid == 4815
    assert launcher.is_running is True


async def test_launcher_dispatches_through_windows_native_helper(
    tmp_path: Path,
) -> None:
    """A new client avoids fragile cmd.exe command-line composition in WSL."""

    config = MCConfig(mc_home=tmp_path)
    launcher = MinecraftLauncher(config)
    bridge = _NoWindowBridge()
    launcher._bridge = bridge

    result = await launcher.launch()

    assert result.success is True
    assert bridge.launches == [(config.launch_bat, config.launch_dir)]


async def test_installation_requires_exact_quick_play_world(tmp_path: Path) -> None:
    """Only the configured save name satisfies the non-interactive launch gate."""

    version = tmp_path / "versions" / "neoforge-21.1.219"
    world = tmp_path / "saves" / "Elysian Realm"
    version.mkdir(parents=True)
    world.mkdir(parents=True)
    script = tmp_path / "LaunchElysia.bat"
    script.write_text(
        "java BootstrapLauncher --launchTarget forgeclient "
        '--quickPlaySingleplayer "Other World"',
        encoding="utf-8",
    )
    launcher = _TestableLauncher(MCConfig(mc_home=tmp_path), script)

    wrong = await launcher.check_installation()
    script.write_text(
        "java BootstrapLauncher --launchTarget forgeclient "
        '--quickPlaySingleplayer "Elysian Realm"',
        encoding="utf-8",
    )
    exact = await launcher.check_installation()

    assert wrong["quick_play_world"] == "Other World"
    assert wrong["quick_play_configured"] is False
    assert exact["quick_play_world"] == "Elysian Realm"
    assert exact["quick_play_configured"] is True


async def test_installation_requires_one_hash_pinned_bridge_and_baritone(
    tmp_path: Path,
) -> None:
    """Wrong, stale, or duplicate gameplay artifacts block production readiness."""

    (tmp_path / "versions" / "neoforge-21.1.219").mkdir(parents=True)
    (tmp_path / "saves" / "Elysian Realm").mkdir(parents=True)
    mods = tmp_path / "mods"
    mods.mkdir()
    bridge = mods / "elysium_bridge-0.2.1.jar"
    baritone = mods / "baritone-unoptimized-neoforge-1.11.2.jar"
    bridge.write_bytes(b"bridge-test-artifact")
    baritone.write_bytes(b"baritone-test-artifact")
    script = tmp_path / "LaunchElysia.bat"
    script.write_text(
        '--launchTarget forgeclient --quickPlaySingleplayer "Elysian Realm"',
        encoding="utf-8",
    )
    config = MCConfig(
        mc_home=tmp_path,
        expected_bridge_sha256=hashlib.sha256(bridge.read_bytes()).hexdigest(),
        expected_baritone_sha256=hashlib.sha256(baritone.read_bytes()).hexdigest(),
    )
    launcher = _TestableLauncher(config, script)

    ready = await launcher.check_installation()
    (mods / "elysium_bridge-0.1.0.jar").write_bytes(b"stale")
    ambiguous = await launcher.check_installation()

    assert ready["bridge_mod_ready"] is True
    assert ready["baritone_mod_ready"] is True
    assert ambiguous["bridge_mod_ready"] is False
