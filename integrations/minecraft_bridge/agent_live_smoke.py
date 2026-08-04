"""Launch and prove one safe structured Minecraft evidence loop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from plugins.life_engine.minecraft.embodiment_contracts import (
    ActionCommand,
    ActionReceipt,
    EmbodiedIntent,
    IntentConclusion,
    PlannerTurn,
    WorldObservation,
)
from plugins.life_engine.minecraft.launcher import MCConfig
from plugins.life_engine.minecraft.session import MinecraftSession


def _emit(event: str, **facts: Any) -> None:
    print(
        json.dumps({"event": event, **facts}, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


class _SafeYawPlanner:
    """Apply one bounded look delta, then conclude only from changed state."""

    async def decide(
        self,
        intent: EmbodiedIntent,
        observations: tuple[WorldObservation, ...],
        receipts: tuple[ActionReceipt, ...],
    ) -> PlannerTurn:
        if not receipts:
            return PlannerTurn(
                command=ActionCommand(
                    intent_id=intent.intent_id,
                    intent_revision=intent.revision,
                    operation="movement.input",
                    parameters={
                        "holds": {},
                        "look_delta": {"yaw": 5.0, "pitch": 0.0},
                    },
                    based_on_observation=observations[-1].observation_id,
                    timeout_seconds=10.0,
                )
            )
        before = float(observations[0].facts["player"]["yaw"])
        after = float(observations[-1].facts["player"]["yaw"])
        delta = (after - before + 180.0) % 360.0 - 180.0
        if abs(delta) < 1.0:
            raise RuntimeError(
                f"bounded look action had no observed effect: yaw_delta={delta}"
            )
        return PlannerTurn(
            conclusion=IntentConclusion(
                statement=f"A bounded look action changed observed yaw by {delta:.3f} degrees.",
                evidence_ids=(
                    receipts[-1].receipt_id,
                    observations[-1].observation_id,
                ),
            )
        )


async def _run() -> None:
    session = MinecraftSession(
        workspace=Path("data/life_engine_workspace"),
        mc_config=MCConfig(),
    )
    started = await session.start(
        goal="production bridge validation",
        body_name="agent",
    )
    _emit("session_start", result=started)
    if not started.get("success"):
        raise RuntimeError(str(started.get("error") or "Minecraft session failed"))
    try:
        session._planner = _SafeYawPlanner()
        result = await session.do_intent(
            "Apply one safe bounded look adjustment and verify it from fresh state.",
            timeout=30.0,
        )
        _emit("evidence_loop", result=result)
        if not result.get("success"):
            raise RuntimeError(str(result.get("error") or "evidence loop failed"))
    finally:
        stopped = await session.close()
        _emit("session_close", result=stopped)
        if not stopped.get("success"):
            raise RuntimeError(f"Minecraft session cleanup failed: {stopped}")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
