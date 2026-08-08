"""Contract tests for the headless bot body route of the Minecraft session."""

from __future__ import annotations

import asyncio
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.minecraft.bot_launcher import MinecraftBotLauncher
from plugins.life_engine.minecraft.bridge_client import (
    BridgeConfig,
    MinecraftBridgeClient,
)
from plugins.life_engine.minecraft.embodiment_contracts import (
    ActionCommand,
    ActionReceipt,
    WorldObservation,
    utc_now,
)
from plugins.life_engine.minecraft.launcher import MCConfig
from plugins.life_engine.minecraft.session import MinecraftSession
from plugins.life_engine.minecraft.tools import LifeEngineMinecraftTool


class _ServerWorldBridge:
    """In-memory body endpoint reporting a playable shared server world."""

    def __init__(self, *, world_loaded: bool = True) -> None:
        """Create a connected body with an empty observation sequence."""

        self.connected = False
        self.instance_id = "bot-test"
        self.capabilities = (
            "chat.send",
            "control.release_all",
            "movement.input",
            "navigation.goto",
            "navigation.stop",
            "player.respawn",
            "world.mine",
        )
        self.hello_metadata = {"bridge_version": "0.2.1"}
        self.sequence = 0
        self.closed = False
        self.world_loaded = world_loaded

    async def open(self) -> None:
        """Mark the endpoint connected."""

        self.connected = True

    async def close(self) -> None:
        """Mark the endpoint closed."""

        self.connected = False
        self.closed = True

    async def observe(self, after_sequence: int | None = None) -> WorldObservation:
        """Emit the next factual server-world observation."""

        self.sequence += 1
        if not self.world_loaded:
            facts: dict[str, Any] = {
                "world_loaded": False,
                "screen": {"class": "headless_connecting", "title": "connecting"},
            }
        else:
            facts = {
                "world_loaded": True,
                "world": {"mode": "multiplayer", "server_address": "127.0.0.1:25565"},
                "player": {
                    "uuid": "00000000-0000-0000-0000-000000000042",
                    "name": "AyerElysia",
                    "x": self.sequence,
                    "y": 64,
                    "z": 0,
                },
                "chat": [],
            }
        return WorldObservation(
            instance_id=self.instance_id,
            sequence=self.sequence,
            observed_at=utc_now(),
            source="mineflayer-bot",
            facts=facts,
        )

    async def act(self, command: ActionCommand) -> ActionReceipt:
        """Return a terminal dispatch receipt for one command."""

        return ActionReceipt(
            command_id=command.command_id,
            intent_id=command.intent_id,
            accepted=True,
            completed=True,
            interrupted=False,
            facts={"operation_dispatched": command.operation},
            observation_sequence=self.sequence,
        )

    async def interrupt(self, intent_id: str, reason: str) -> None:
        """Record one interruption without owning its meaning."""


class _BotLauncher:
    """Fake owned-process launcher recording exact lifecycle calls."""

    def __init__(
        self,
        *,
        dependencies_installed: bool = True,
        fail_stop: bool = False,
    ) -> None:
        """Create a fake launcher with configurable dependency facts."""

        self.directory = Path("/fake/minecraft_bot")
        self.dependencies_installed = dependencies_installed
        self.fail_stop = fail_stop
        self.starts: list[dict[str, Any]] = []
        self.stops = 0
        self.pid: int | None = None

    async def check_node(self) -> dict[str, Any]:
        """Report an available node runtime."""

        return {"available": True, "version": "v20.20.2", "error": None}

    def check_dependencies(self) -> dict[str, Any]:
        """Report configured dependency facts."""

        return {
            "directory": str(self.directory),
            "entrypoint_exists": True,
            "lockfile_exists": True,
            "dependencies_installed": self.dependencies_installed,
            "missing_modules": () if self.dependencies_installed else ("mineflayer",),
        }

    async def start(self, **kwargs: Any) -> dict[str, Any]:
        """Record one launch request with its exact environment contract."""

        self.starts.append(dict(kwargs))
        self.pid = 4242
        return {"success": True, "reused_existing": False, "pid": 4242, "error": None}

    async def stop(self) -> dict[str, Any]:
        """Record one stop request."""

        self.stops += 1
        if self.fail_stop:
            return {
                "success": False,
                "already_stopped": False,
                "pid": self.pid,
                "error": "injected bot stop failure",
            }
        self.pid = None
        return {"success": True, "already_stopped": False, "pid": 4242}


def _session(
    tmp_path: Path,
    launcher: _BotLauncher,
    bridge: _ServerWorldBridge,
) -> MinecraftSession:
    """Build one bot-routed session with injected fakes."""

    config = MCConfig(bridge_ready_timeout_seconds=1)
    session = MinecraftSession(workspace=tmp_path, mc_config=config)
    session._bot_launcher = launcher

    async def wait_for_bridge(profile: Any) -> _ServerWorldBridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge
    return session


def test_bot_profile_contract() -> None:
    """The bot body keeps the exact shared protocol and readiness route."""

    session = MinecraftSession(workspace=Path("/tmp"), mc_config=MCConfig())
    profile = session._body_profiles()["bot"]
    assert profile.readiness_kind == "server_world"
    assert profile.listen_uri == "ws://127.0.0.1:18767/elysium"
    assert profile.required_operations == frozenset(
        {
            "chat.send",
            "control.release_all",
            "movement.input",
            "navigation.goto",
            "navigation.stop",
            "player.respawn",
            "world.mine",
        }
    )


def test_tool_schema_exposes_bot_as_same_world_body() -> None:
    """The formal tool contract lets the model select the shared-world body."""

    body_schema = LifeEngineMinecraftTool.parameters["properties"]["body_name"]
    assert body_schema["enum"] == ["agent", "bot", "biomimetic"]


@pytest.mark.parametrize(
    "token_path",
    ["/tmp/token.json", "../token.json", r"C:\\token.json"],
)
def test_bot_token_path_cannot_escape_workspace(token_path: str) -> None:
    """The generated bridge secret never follows an absolute or parent path."""

    with pytest.raises(ValueError, match="workspace-relative"):
        MCConfig(bot_token_file=token_path)


async def test_node_body_speaks_the_python_bridge_protocol(
    unused_tcp_port: int,
) -> None:
    """The shipped Node endpoint authenticates and executes over the real client."""

    launcher = MinecraftBotLauncher()
    if (
        shutil.which("node") is None
        or not launcher.check_dependencies()["dependencies_installed"]
    ):
        pytest.skip("Node bot dependencies are not installed")
    token = "cross-runtime-test-token"
    instance_id = "bot_cross_runtime_test"
    bridge_uri = f"ws://127.0.0.1:{unused_tcp_port}/elysium"
    script = """
import { BridgeBodyEndpoint } from './src/protocol.js';
const endpoint = new BridgeBodyEndpoint(
  {
    bridgeUri: process.env.TEST_BRIDGE_URI,
    token: process.env.TEST_BRIDGE_TOKEN,
    instanceId: process.env.TEST_INSTANCE_ID,
    bodyType: 'mineflayer-bot-test',
    minecraftVersion: '1.21.1',
    capabilities: ['observation.wait'],
  },
  {
    onAuthenticated: () => endpoint.broadcastObservation({world_loaded: true}),
    onCommand: async (operation) => ({operation_dispatched: operation}),
  },
  () => {},
);
process.on('SIGTERM', () => { endpoint.stop(); process.exit(0); });
endpoint.start();
"""
    client = MinecraftBridgeClient(
        BridgeConfig(
            uri=bridge_uri,
            listen_uri=bridge_uri,
            token=token,
            expected_instance_id=instance_id,
            open_timeout_seconds=5,
        )
    )
    open_task = asyncio.create_task(client.open())
    await asyncio.sleep(0.05)
    process = await asyncio.create_subprocess_exec(
        "node",
        "--input-type=module",
        "--eval",
        script,
        cwd=str(launcher.directory),
        env={
            **os.environ,
            "TEST_BRIDGE_URI": bridge_uri,
            "TEST_BRIDGE_TOKEN": token,
            "TEST_INSTANCE_ID": instance_id,
        },
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await open_task
        observation = await client.observe()
        assert observation.instance_id == instance_id
        assert observation.facts["world_loaded"] is True

        receipt = await client.act(
            ActionCommand(
                command_id="cross_runtime_command",
                intent_id="cross_runtime_intent",
                intent_revision=1,
                operation="observation.wait",
                timeout_seconds=2,
            )
        )
        assert receipt.accepted is True
        assert receipt.completed is True
        assert receipt.facts == {"operation_dispatched": "observation.wait"}
    finally:
        await client.close()
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


def test_resolve_server_host_auto_returns_ipv4() -> None:
    """Auto host resolution yields a concrete IPv4 address, never "auto"."""

    resolved = MinecraftBotLauncher.resolve_server_host("auto")
    parts = resolved.split(".")
    assert len(parts) == 4
    assert all(part.isdigit() for part in parts)
    assert MinecraftBotLauncher.resolve_server_host("192.0.2.10") == "192.0.2.10"


def test_ensure_token_is_idempotent(tmp_path: Path) -> None:
    """Token bootstrap never replaces an existing generated token."""

    token_file = tmp_path / "minecraft" / "bot_bridge_token.json"
    first = MinecraftBotLauncher.ensure_token(token_file)
    second = MinecraftBotLauncher.ensure_token(token_file)
    assert first == second and first


def test_ensure_token_is_atomic_under_first_start_race(tmp_path: Path) -> None:
    """Concurrent first starts converge on one authoritative token."""

    token_file = tmp_path / "minecraft" / "bot_bridge_token.json"
    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(
            executor.map(
                MinecraftBotLauncher.ensure_token,
                [token_file] * 32,
            )
        )
    assert len(set(tokens)) == 1
    assert token_file.stat().st_mode & 0o777 == 0o600


async def test_bot_preflight_reports_missing_dependencies(tmp_path: Path) -> None:
    """Preflight names the exact blocker instead of guessing readiness."""

    session = _session(
        tmp_path, _BotLauncher(dependencies_installed=False), _ServerWorldBridge()
    )
    result = await session.preflight(body_name="bot")
    assert result["ready_to_start"] is False
    assert any("npm ci" in blocker for blocker in result["blockers"])


async def test_bot_start_and_stop_own_the_process(tmp_path: Path) -> None:
    """Session start launches the owned bot process and stop releases it."""

    launcher = _BotLauncher()
    bridge = _ServerWorldBridge()
    session = _session(tmp_path, launcher, bridge)

    result = await session.start(goal="play together", body_name="bot")

    assert result["success"] is True
    assert session.state.body_name == "bot"
    assert session.state.launch_pid == 4242
    assert len(launcher.starts) == 1
    start_request = launcher.starts[0]
    assert start_request["bridge_uri"] == "ws://127.0.0.1:18767/elysium"
    assert start_request["server_host"] == "auto"
    assert start_request["server_port"] == 25565
    assert start_request["token"]

    stop_result = await session.stop()

    assert stop_result["success"] is True
    assert stop_result["game_left_running"] is False
    assert launcher.stops == 1
    assert bridge.closed is True


async def test_bot_start_fails_without_playable_server_world(
    tmp_path: Path,
) -> None:
    """Connectivity alone is not readiness for the headless body."""

    launcher = _BotLauncher()
    session = _session(
        tmp_path,
        launcher,
        _ServerWorldBridge(world_loaded=False),
    )
    session._config = MCConfig(
        bridge_ready_timeout_seconds=1,
        world_ready_timeout_seconds=1,
    )

    result = await session.start(goal="play together", body_name="bot")

    assert result["success"] is False
    assert "did not become ready" in result["error"]
    assert session.state.readiness == "failed"
    assert launcher.stops == 1
    assert launcher.pid is None


async def test_bot_start_cleanup_failure_remains_retryable(tmp_path: Path) -> None:
    """A failed partial-start cleanup remains owned until stop succeeds."""

    launcher = _BotLauncher(fail_stop=True)
    session = _session(
        tmp_path,
        launcher,
        _ServerWorldBridge(world_loaded=False),
    )
    session._config = MCConfig(
        bridge_ready_timeout_seconds=1,
        world_ready_timeout_seconds=1,
    )

    result = await session.start(goal="play together", body_name="bot")

    assert result["success"] is False
    assert "bot cleanup failed: injected bot stop failure" in result["error"]
    assert session._has_cleanup_pending() is True

    launcher.fail_stop = False
    retry = await session.stop()

    assert retry["success"] is True
    assert launcher.pid is None
    assert session._has_cleanup_pending() is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
