"""Run an evidence-producing smoke test against the live NeoForge bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
from typing import Any

from plugins.life_engine.minecraft.bridge_client import BridgeConfig, MinecraftBridgeClient
from plugins.life_engine.minecraft.embodiment_contracts import ActionCommand


def _arguments() -> argparse.Namespace:
    """Parse explicit live-test configuration."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--listen-uri", required=True)
    parser.add_argument("--wait-for-world-seconds", type=float, default=300.0)
    parser.add_argument("--exercise", action="store_true")
    return parser.parse_args()


def _emit(event: str, **facts: Any) -> None:
    """Emit one flush-safe JSON line without authentication material."""

    print(
        json.dumps({"event": event, **facts}, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


def _position(facts: dict[str, Any]) -> tuple[float, float, float]:
    """Extract an exact player position from one world observation."""

    player = dict(facts["player"])
    return float(player["x"]), float(player["y"]), float(player["z"])


async def _command(
    client: MinecraftBridgeClient,
    operation: str,
    parameters: dict[str, Any],
) -> None:
    """Dispatch one operation and emit its correlated terminal receipt."""

    receipt = await client.act(
        ActionCommand(
            intent_id="live_smoke",
            intent_revision=1,
            operation=operation,
            parameters=parameters,
            timeout_seconds=10.0,
        )
    )
    _emit("receipt", operation=operation, receipt=receipt.to_wire())
    if not receipt.accepted or not receipt.completed or receipt.error is not None:
        raise RuntimeError(f"live command failed: {operation}: {receipt.error}")


async def _wait_for_world(
    client: MinecraftBridgeClient,
    timeout_seconds: float,
) -> Any:
    """Wait for a factual world-loaded observation without guessing UI state."""

    async with asyncio.timeout(timeout_seconds):
        sequence: int | None = None
        while True:
            observation = await client.observe(sequence)
            sequence = observation.sequence
            world_loaded = bool(observation.facts.get("world_loaded"))
            screen = dict(observation.facts.get("screen") or {}).get("class")
            _emit(
                "observation",
                observation_id=observation.observation_id,
                sequence=observation.sequence,
                world_loaded=world_loaded,
                screen=screen,
            )
            if world_loaded:
                return observation


async def _exercise_agent(client: MinecraftBridgeClient, initial: Any) -> None:
    """Exercise Baritone and prove movement with later structured observations."""

    start = _position(dict(initial.facts))
    latest = initial
    try:
        await _command(client, "baritone.command", {"command": "thisway 4"})
        await _command(client, "baritone.command", {"command": "path"})
        async with asyncio.timeout(30.0):
            while True:
                latest = await client.observe(latest.sequence)
                current = _position(dict(latest.facts))
                horizontal_delta = math.hypot(current[0] - start[0], current[2] - start[2])
                if horizontal_delta > 0.25:
                    _emit(
                        "movement_evidence",
                        start=start,
                        finish=current,
                        horizontal_delta=horizontal_delta,
                        observation_id=latest.observation_id,
                        baritone=dict(latest.facts.get("baritone") or {}),
                    )
                    return
    finally:
        try:
            await _command(client, "baritone.command", {"command": "stop"})
        finally:
            await _command(client, "control.release_all", {})
    raise RuntimeError("Baritone produced no observed movement before the deadline")


async def _run(args: argparse.Namespace) -> None:
    """Authenticate the live client and optionally run a movement exercise."""

    raw_config = json.loads(args.config.read_text(encoding="utf-8"))
    token = str(raw_config["authentication_token"])
    client = MinecraftBridgeClient(
        BridgeConfig(
            uri=str(raw_config["bridge_uri"]),
            listen_uri=args.listen_uri,
            token=token,
            open_timeout_seconds=args.wait_for_world_seconds,
            observation_timeout_seconds=30.0,
        )
    )
    await client.open()
    try:
        _emit(
            "authenticated",
            instance_id=client.instance_id,
            capabilities=client.capabilities,
        )
        initial = await _wait_for_world(client, args.wait_for_world_seconds)
        if args.exercise:
            await _exercise_agent(client, initial)
        _emit("complete", exercised=args.exercise)
    finally:
        await client.close()


def main() -> None:
    """Run the asynchronous live bridge smoke test."""

    asyncio.run(_run(_arguments()))


if __name__ == "__main__":
    main()
