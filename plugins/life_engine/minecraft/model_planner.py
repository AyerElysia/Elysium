"""Model-backed, open-vocabulary planner for Minecraft embodiment."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from src.app.plugin_system.api.llm_api import (
    create_llm_request,
    get_model_set_by_task,
)
from src.kernel.llm import ROLE, Image, LLMPayload, Text

from .embodiment_contracts import (
    ActionCommand,
    ActionReceipt,
    EmbodiedIntent,
    IntentConclusion,
    PlannerTurn,
    WorldObservation,
)

DecisionSource = Callable[[dict[str, Any]], Awaitable[str]]
CapabilitySource = Callable[[], Sequence[str]]

_SYSTEM_PROMPT = """\
You are the operational planner for Elysia's currently selected Minecraft body.
Elysia authored the intention. You execute that intention; you do not replace,
reinterpret, prioritize, or invent her desires.

At each turn you receive the full intention, current advertised operations,
factual world observations, and factual action receipts. Choose exactly one:

1. Issue one operation:
{"command":{"operation":"an advertised operation","parameters":{},
"based_on_observation":"observation_id","timeout_seconds":null}}

2. Conclude what the evidence supports:
{"conclusion":{"statement":"factual conclusion",
"evidence_ids":["observation_id or receipt_id", "..."]}}

Rules:
- Output one JSON object and nothing else.
- Operation names must be copied exactly from advertised_operations.
- Treat accepted/dispatched actions only as dispatch facts, never as proof that
  a world goal was achieved.
- Use a post-action world observation to verify changes when the intention is
  about the world.
- Keep uncertainty explicit. If the evidence does not prove completion, act,
  inspect, wait through an available operation, or conclude only the limited
  fact that is supported.
- The operation parameter object is open vocabulary but must obey the body's
  advertised operation contract supplied in planner_guidance.
- Do not write feelings for Elysia. Structured facts are perceptions, not her
  subjective response to them.
- An interruption or revised intention supersedes this planning turn.
"""


class PlannerOutputError(ValueError):
    """Raised when a model decision violates the planner contract."""


class JsonIntentPlanner:
    """Convert strict model JSON into evidence-kernel planner turns."""

    def __init__(
        self,
        decision_source: DecisionSource,
        capability_source: CapabilitySource,
        planner_guidance: str,
    ) -> None:
        """Bind model inference, live capabilities, and operational guidance."""

        self._decision_source = decision_source
        self._capability_source = capability_source
        self._planner_guidance = planner_guidance

    async def decide(
        self,
        intent: EmbodiedIntent,
        observations: tuple[WorldObservation, ...],
        receipts: tuple[ActionReceipt, ...],
    ) -> PlannerTurn:
        """Request and validate one exact command or conclusion."""

        capabilities = tuple(self._capability_source())
        if not capabilities:
            raise PlannerOutputError("selected body advertised no operations")
        input_document = {
            "planner_guidance": self._planner_guidance,
            "advertised_operations": list(capabilities),
            "intent": intent.to_wire(),
            "observations": [item.to_wire() for item in observations],
            "receipts": [item.to_wire() for item in receipts],
        }
        raw = (await self._decision_source(input_document)).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exception:
            raise PlannerOutputError("planner output was not strict JSON") from exception
        if not isinstance(payload, dict):
            raise PlannerOutputError("planner output must be a JSON object")
        has_command = "command" in payload
        has_conclusion = "conclusion" in payload
        if has_command == has_conclusion:
            raise PlannerOutputError(
                "planner output requires exactly one command or conclusion"
            )
        if has_conclusion:
            return PlannerTurn(conclusion=self._parse_conclusion(payload["conclusion"]))
        return PlannerTurn(
            command=self._parse_command(payload["command"], intent, capabilities)
        )

    @staticmethod
    def _parse_conclusion(value: Any) -> IntentConclusion:
        """Parse one explicit evidence-backed conclusion."""

        if not isinstance(value, dict):
            raise PlannerOutputError("conclusion must be a JSON object")
        evidence = value.get("evidence_ids")
        if not isinstance(evidence, list):
            raise PlannerOutputError("conclusion evidence_ids must be an array")
        return IntentConclusion(
            statement=str(value.get("statement") or ""),
            evidence_ids=tuple(str(item) for item in evidence),
        )

    @staticmethod
    def _parse_command(
        value: Any,
        intent: EmbodiedIntent,
        capabilities: Sequence[str],
    ) -> ActionCommand:
        """Parse one command and enforce live transport capabilities."""

        if not isinstance(value, dict):
            raise PlannerOutputError("command must be a JSON object")
        operation = str(value.get("operation") or "")
        if operation not in capabilities:
            raise PlannerOutputError(
                f"body did not advertise planner operation: {operation!r}"
            )
        parameters = value.get("parameters")
        if not isinstance(parameters, dict):
            raise PlannerOutputError("command parameters must be a JSON object")
        timeout_raw = value.get("timeout_seconds")
        timeout_seconds = float(timeout_raw) if timeout_raw is not None else None
        based_on = value.get("based_on_observation")
        return ActionCommand(
            intent_id=intent.intent_id,
            intent_revision=intent.revision,
            operation=operation,
            parameters=parameters,
            based_on_observation=(str(based_on) if based_on else None),
            timeout_seconds=timeout_seconds,
        )


class ElysiumModelDecisionSource:
    """Use Elysium's configured model stack for one planner decision."""

    def __init__(self, model_task_name: str) -> None:
        """Bind an explicit configured model task."""

        if not model_task_name.strip():
            raise ValueError("model_task_name must not be empty")
        self._model_task_name = model_task_name

    async def __call__(self, input_document: dict[str, Any]) -> str:
        """Send the complete planner document and return model text."""

        model_set = get_model_set_by_task(self._model_task_name)
        if not model_set:
            raise RuntimeError(
                f"Minecraft planner model is unavailable: {self._model_task_name}"
            )
        request = create_llm_request(
            model_set=model_set,
            request_name="life_minecraft_embodiment_planner",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_SYSTEM_PROMPT)))
        user_content: list[Text | Image] = [
            Text(json.dumps(input_document, ensure_ascii=False, separators=(",", ":")))
        ]
        observations = input_document.get("observations")
        if isinstance(observations, list) and observations:
            latest = observations[-1]
            frame_path = latest.get("frame_path") if isinstance(latest, dict) else None
            if frame_path:
                path = Path(str(frame_path))
                frame_bytes = await asyncio.to_thread(path.read_bytes)
                suffix = path.suffix.casefold()
                media_type = "image/png" if suffix == ".png" else "image/jpeg"
                encoded = base64.b64encode(frame_bytes).decode("ascii")
                user_content.append(Image(f"data:{media_type};base64,{encoded}"))
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                user_content,
            )
        )
        response = await request.send(stream=False)
        await response
        return str(getattr(response, "message", "") or "")


AGENT_BRIDGE_GUIDANCE = """\
- baritone.command: parameters {"command": "a Baritone command without the # prefix"}.
  Dispatch is not world-goal completion; inspect later observations. Commands
  such as `goal x y z`, `path`, `mine minecraft:oak_log`, `follow player NAME`,
  and `stop` are available when supported by the installed Baritone build.
- native.input_batch: parameters may contain a complete `holds` object for
  forward/back/left/right/jump/sneak/sprint/attack/use/drop, a `pulses` array,
  `look_delta` with yaw/pitch degrees, `hotbar_slot` from 0 through 8, and chat.
- chat.send: parameters {"message": "exact message"}.
- player.respawn: parameters {}. Dispatch only when the latest player facts
  report dead_or_dying=true; a living player rejects this operation.
- control.release_all: parameters {}.
"""


BIOMIMETIC_GUIDANCE = """\
- native.input_batch is the motor command. Supply the complete held-control
  state on every action. Use small observable movements, inspect the following
  first-person frame and structured proprioception, and correct the next action.
- Use `mouse_delta` with relative physical x/y counts for view movement. The
  `pulses` array accepts forward/back/left/right/jump/sneak/sprint/drop/inventory,
  attack/use/middle, chat, escape, toggle_hud, toggle_debug, toggle_camera,
  toggle_fullscreen, and player_list. `hotbar_slot` is 0 through 8.
- Do not substitute Baritone or a structured high-level task command on this
  body. The point of this route is first-person perception and low-level motor
  control.
"""
