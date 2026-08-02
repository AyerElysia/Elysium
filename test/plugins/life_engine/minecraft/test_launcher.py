"""Exact-process ownership tests for the Minecraft launcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from plugins.life_engine.minecraft.launcher import MCConfig, MinecraftLauncher


class _ExistingWindowBridge:
    """Expose one exact existing Minecraft window."""

    async def find_window(self) -> dict[str, Any]:
        """Return an already running client owned by a known process."""

        return {"pid": 4815, "title": "Minecraft NeoForge* 1.21.1"}


async def test_launcher_reuses_exact_existing_client(tmp_path: Path) -> None:
    """Starting a session must not create a competing second game client."""

    launcher = MinecraftLauncher(MCConfig(mc_home=tmp_path))
    launcher._bridge = _ExistingWindowBridge()

    result = await launcher.launch()

    assert result.success is True
    assert result.reused_existing is True
    assert result.pid == 4815
    assert launcher.is_running is True
