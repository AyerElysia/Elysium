"""Cross-validate native keyboard, mouse, and frame capture in Minecraft."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from plugins.life_engine.minecraft.bridge_client import BridgeConfig, MinecraftBridgeClient
from plugins.life_engine.minecraft.embodiment_contracts import ActionCommand


def _arguments() -> argparse.Namespace:
    """Parse explicit agent and native bridge endpoints."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-config", type=Path, required=True)
    parser.add_argument("--native-config", type=Path, required=True)
    parser.add_argument("--agent-listen-uri", required=True)
    parser.add_argument("--native-listen-uri", required=True)
    parser.add_argument("--open-timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--prepare-windowed",
        action="store_true",
        help="Pulse Minecraft's fullscreen toggle before collecting evidence.",
    )
    return parser.parse_args()


def _load(path: Path, listen_uri: str, timeout_seconds: float) -> BridgeConfig:
    """Load one authentication token without exposing it to process arguments."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    return BridgeConfig(
        uri=str(raw.get("bridge_uri") or raw.get("controller_uri")),
        listen_uri=listen_uri,
        token=str(raw["authentication_token"]),
        open_timeout_seconds=timeout_seconds,
        observation_timeout_seconds=30.0,
    )


def _emit(event: str, **facts: Any) -> None:
    """Write one flush-safe machine-readable validation event."""

    print(
        json.dumps({"event": event, **facts}, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


def _frame_digest(observation: Any) -> tuple[str, int]:
    """Hash one immutable sidecar frame and return its byte length."""

    if observation.frame_path is None:
        raise RuntimeError("native observation has no frame_path")
    path = Path(observation.frame_path)
    payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest(), len(payload)


async def _world_observation(client: MinecraftBridgeClient) -> Any:
    """Wait until the in-game bridge proves that a world is loaded."""

    sequence: int | None = None
    while True:
        observation = await client.observe(sequence)
        sequence = observation.sequence
        if observation.facts.get("world_loaded"):
            return observation


async def _frame_observation(
    client: MinecraftBridgeClient,
    after_sequence: int | None = None,
) -> Any:
    """Wait for a successfully persisted native frame and report sensor errors."""

    sequence = after_sequence
    while True:
        observation = await client.observe(sequence)
        sequence = observation.sequence
        if observation.frame_path is not None:
            return observation
        _emit(
            "native_sensor_pending",
            observation_id=observation.observation_id,
            facts=observation.facts,
        )


def _yaw_delta(initial: float, current: float) -> float:
    """Return the shortest signed angular difference in degrees."""

    return (current - initial + 180.0) % 360.0 - 180.0


async def _exercise(
    agent: MinecraftBridgeClient,
    native: MinecraftBridgeClient,
) -> None:
    """Issue physical input and prove its effect through independent sensors."""

    agent_before, native_before = await asyncio.gather(
        _world_observation(agent),
        _frame_observation(native),
    )
    player_before = dict(agent_before.facts["player"])
    yaw_before = float(player_before["yaw"])
    slot_before = int(player_before["selected_hotbar_slot"])
    frame_before_sha, frame_before_bytes = _frame_digest(native_before)

    try:
        receipt = await native.act(
            ActionCommand(
                intent_id="native_live_smoke",
                intent_revision=1,
                operation="native.input_batch",
                parameters={
                    "holds": {},
                    "mouse_delta": {"x": 96, "y": 0},
                    "hotbar_slot": 0,
                },
                based_on_observation=native_before.observation_id,
                timeout_seconds=10.0,
            )
        )
        _emit("native_receipt", receipt=receipt.to_wire())
        if not receipt.accepted or not receipt.completed or receipt.error is not None:
            raise RuntimeError(f"native input failed: {receipt.error}")

        agent_after = agent_before
        async with asyncio.timeout(30.0):
            while True:
                agent_after = await agent.observe(agent_after.sequence)
                player_after = dict(agent_after.facts["player"])
                yaw_after = float(player_after["yaw"])
                slot_after = int(player_after["selected_hotbar_slot"])
                if abs(_yaw_delta(yaw_before, yaw_after)) > 0.0 and slot_after == 0:
                    break

        native_after = await _frame_observation(native, native_before.sequence)
        frame_after_sha, frame_after_bytes = _frame_digest(native_after)
        if frame_after_sha == frame_before_sha:
            raise RuntimeError("native frame did not change after physical input")
    finally:
        await native.act(
            ActionCommand(
                intent_id="native_live_smoke",
                intent_revision=1,
                operation="control.release_all",
                parameters={},
                timeout_seconds=10.0,
            )
        )
    _emit(
        "cross_validated",
        native_instance=native.instance_id,
        agent_instance=agent.instance_id,
        input_events=receipt.facts.get("send_input_events"),
        initial_hotbar_slot=slot_before,
        final_hotbar_slot=slot_after,
        yaw_delta=_yaw_delta(yaw_before, yaw_after),
        agent_observation_id=agent_after.observation_id,
        initial_frame={
            "observation_id": native_before.observation_id,
            "sha256": frame_before_sha,
            "bytes": frame_before_bytes,
        },
        final_frame={
            "observation_id": native_after.observation_id,
            "sha256": frame_after_sha,
            "bytes": frame_after_bytes,
        },
    )


async def _run(args: argparse.Namespace) -> None:
    """Open both reverse bridges and run the cross-validation."""

    agent = MinecraftBridgeClient(
        _load(args.agent_config, args.agent_listen_uri, args.open_timeout_seconds)
    )
    native = MinecraftBridgeClient(
        _load(args.native_config, args.native_listen_uri, args.open_timeout_seconds)
    )
    await asyncio.gather(agent.open(), native.open())
    try:
        _emit(
            "authenticated",
            agent_capabilities=agent.capabilities,
            native_capabilities=native.capabilities,
        )
        if args.prepare_windowed:
            receipt = await native.act(
                ActionCommand(
                    intent_id="native_window_preparation",
                    intent_revision=1,
                    operation="native.input_batch",
                    parameters={"holds": {}, "pulses": ["toggle_fullscreen"]},
                    timeout_seconds=10.0,
                )
            )
            _emit("window_prepared", receipt=receipt.to_wire())
            if not receipt.accepted or not receipt.completed or receipt.error is not None:
                raise RuntimeError(f"window preparation failed: {receipt.error}")
        await _exercise(agent, native)
        _emit("complete")
    finally:
        await asyncio.gather(agent.close(), native.close())


def main() -> None:
    """Run the asynchronous native-body smoke test."""

    asyncio.run(_run(_arguments()))


if __name__ == "__main__":
    main()
