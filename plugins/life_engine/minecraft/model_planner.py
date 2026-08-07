"""Model-backed, open-vocabulary planner for Minecraft embodiment."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
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
    PerceptionReference,
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


@dataclass(frozen=True, slots=True)
class VerifiedPerceptionDelivery:
    """Content-free proof that one projection reached the effective request."""

    delivery_id: str
    projection_sha256: str
    delivered_bytes: int
    transport_request_id: str = ""


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
            "intent": intent.to_prompt(),
            "observations": [item.to_wire() for item in observations],
            "receipts": [item.to_wire() for item in receipts],
        }
        raw = (await self._decision_source(input_document)).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exception:
            raise PlannerOutputError(
                "planner output was not strict JSON"
            ) from exception
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

    def reset_perception_delivery(self, reference: PerceptionReference) -> None:
        """Discard any proof left by an earlier intent with the same reference."""

        discard = getattr(self._decision_source, "discard_context_delivery", None)
        if not callable(discard):
            raise TypeError(
                "Minecraft planner cannot prove transient Perception delivery"
            )
        discard(reference.delivery_id)

    def consume_perception_delivery(
        self,
        reference: PerceptionReference,
    ) -> VerifiedPerceptionDelivery:
        """Consume and validate the proof produced by the final planner turn."""

        consume = getattr(self._decision_source, "consume_context_delivery", None)
        if not callable(consume):
            raise TypeError(
                "Minecraft planner cannot prove transient Perception delivery"
            )
        proof = consume(reference.delivery_id)
        if not isinstance(proof, VerifiedPerceptionDelivery):
            raise TypeError(
                "Minecraft planner produced no exact Perception delivery proof"
            )
        if (
            proof.delivery_id != reference.delivery_id
            or proof.projection_sha256 != reference.content_sha256
            or proof.delivered_bytes != reference.content_bytes
        ):
            raise RuntimeError(
                "Minecraft planner Perception delivery proof does not match the "
                "prepared projection"
            )
        return proof

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
        self._verified_context_deliveries: dict[
            str, VerifiedPerceptionDelivery
        ] = {}

    def discard_context_delivery(self, delivery_id: str) -> None:
        """Drop stale proof before a new intent starts using one reference."""

        self._verified_context_deliveries.pop(str(delivery_id), None)

    def consume_context_delivery(
        self,
        delivery_id: str,
    ) -> VerifiedPerceptionDelivery | None:
        """Return and remove one verified, content-free delivery proof."""

        return self._verified_context_deliveries.pop(str(delivery_id), None)

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
        (
            durable_document,
            perception_text,
            perception_reference,
        ) = self._split_transient_perception(input_document)
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_SYSTEM_PROMPT)))
        durable_text = json.dumps(
            durable_document,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        user_content: list[Text | Image] = [Text(durable_text)]
        if perception_text is not None and perception_reference is not None:
            delivery_id = str(perception_reference["delivery_id"])
            self.discard_context_delivery(delivery_id)
            user_content.append(
                Text(
                    "The next Text part is the transient World Perception named by "
                    f"perception_reference delivery_id={delivery_id}."
                )
            )
            marker = self._unique_delivery_marker(
                perception_text,
                other_texts=(_SYSTEM_PROMPT, durable_text, user_content[-1].text),
            )
            user_content.append(Text(perception_text))
            request.register_context_delivery(
                delivery_id,
                perception_text,
                marker=marker,
            )
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
        if perception_text is not None and perception_reference is not None:
            delivery_id = str(perception_reference["delivery_id"])
            receipt = response.effective_context_receipt(delivery_id)
            expected_bytes = int(perception_reference["bytes"])
            expected_sha256 = str(perception_reference["hash"])
            if (
                receipt is None
                or not receipt.exact_present
                or receipt.effective_utf8_bytes != expected_bytes
                or receipt.effective_sha256 != expected_sha256
            ):
                raise RuntimeError(
                    "Minecraft planner transient Perception was absent, duplicated, "
                    "or trimmed from the effective model request"
                )
            request_record_id = getattr(response, "request_record_id", None)
            self._verified_context_deliveries[delivery_id] = (
                VerifiedPerceptionDelivery(
                    delivery_id=delivery_id,
                    projection_sha256=expected_sha256,
                    delivered_bytes=expected_bytes,
                    transport_request_id=(
                        str(request_record_id)
                        if request_record_id is not None
                        else ""
                    ),
                )
            )
        return str(getattr(response, "message", "") or "")

    @staticmethod
    def _split_transient_perception(
        input_document: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
        """Remove prompt-only projection text from the durable planner document."""

        durable_document = dict(input_document)
        raw_intent = durable_document.get("intent")
        if not isinstance(raw_intent, dict):
            raise PlannerOutputError("planner intent document must be a JSON object")
        intent = dict(raw_intent)
        durable_document["intent"] = intent
        raw_transient = intent.pop("transient_prompt_context", {})
        if not isinstance(raw_transient, dict):
            raise PlannerOutputError(
                "intent transient_prompt_context must be a JSON object"
            )
        unknown = set(raw_transient).difference({"world_perception"})
        if unknown:
            raise PlannerOutputError(
                "unregistered transient planner context: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        perception_text = raw_transient.get("world_perception")
        raw_reference = intent.get("perception_reference")
        if perception_text is None:
            if raw_reference is not None:
                raise PlannerOutputError(
                    "perception_reference exists without transient projection text"
                )
            return durable_document, None, None
        if not isinstance(perception_text, str) or not perception_text:
            raise PlannerOutputError(
                "transient world_perception must be non-empty text"
            )
        if not isinstance(raw_reference, dict):
            raise PlannerOutputError(
                "transient world_perception requires a Perception reference"
            )
        reference = dict(raw_reference)
        delivery_id = str(reference.get("delivery_id") or "")
        expected_sha256 = str(reference.get("hash") or "")
        try:
            expected_bytes = int(reference.get("bytes"))
        except (TypeError, ValueError) as exception:
            raise PlannerOutputError(
                "Perception reference bytes must be an integer"
            ) from exception
        encoded = perception_text.encode("utf-8")
        if not delivery_id:
            raise PlannerOutputError("Perception reference delivery_id is empty")
        if (
            expected_bytes != len(encoded)
            or expected_sha256 != hashlib.sha256(encoded).hexdigest()
        ):
            raise PlannerOutputError(
                "transient world_perception does not match its Perception reference"
            )
        return durable_document, perception_text, reference

    @staticmethod
    def _unique_delivery_marker(
        expected_text: str,
        *,
        other_texts: Sequence[str],
    ) -> str:
        """Choose a prefix that identifies only the exact transient Text part."""

        candidate_lengths = (16, 32, 64, 128, 256, 512, len(expected_text))
        seen: set[int] = set()
        for length in candidate_lengths:
            bounded = min(len(expected_text), length)
            if bounded in seen:
                continue
            seen.add(bounded)
            candidate = expected_text[:bounded]
            if candidate and all(candidate not in text for text in other_texts):
                return candidate
        raise RuntimeError(
            "transient Perception has no marker unique to its effective Text part"
        )


AGENT_BRIDGE_GUIDANCE = """\
- navigation.goto: parameters {"x": integer, "y": integer, "z": integer}.
- navigation.follow: parameters {"player": "exact Minecraft account name"}.
- navigation.stop: parameters {}.
- world.mine: parameters {"block": "minecraft resource identifier"}. This
  starts deterministic mining; later observations must prove block or inventory
  changes before concluding that anything was collected.
- movement.input: parameters may contain a complete `holds` object for
  forward/back/left/right/jump/sneak/sprint/attack/use/drop, a `pulses` array,
  `look_delta` with bounded yaw/pitch degrees, and `hotbar_slot` from 0 through 8.
- interaction.attack / interaction.use / item.drop: parameters {}.
- inventory.select_hotbar: parameters {"slot": integer from 0 through 8}.
- observation.wait: parameters {}. It performs no world action; use it to await
  the next structured state while navigation or mining is still in progress.
- chat.send: parameters {"message": "exact message"}.
- player.respawn: parameters {}. Dispatch only when the latest player facts
  report dead_or_dying=true; a living player rejects this operation.
- control.release_all: parameters {}.
- A receipt proves only dispatch. Navigation, mining, interaction, inventory,
  and survival outcomes require a later observation with the relevant factual
  position, block, entity, or inventory change.
"""


BOT_BRIDGE_GUIDANCE = """\
- This body is a headless player inside a shared server world; other players
  (including her human) can be nearby. Observations include a bounded `chat`
  ring buffer with recent in-game chat, join, and leave events.
- navigation.goto: parameters {"x": integer, "y": integer, "z": integer}.
- navigation.follow: parameters {"player": "exact in-game account name"}.
  The player must be visible from this body; an invisible player is rejected.
- navigation.stop: parameters {}.
- world.mine: parameters {"block": "minecraft resource identifier"}. Finds the
  nearest matching block within 32 blocks; later observations must prove block
  or inventory changes before concluding that anything was collected.
- movement.input: parameters may contain a complete `holds` object for
  forward/back/left/right/jump/sneak/sprint, a `pulses` array,
  `look_delta` with bounded yaw/pitch degrees, and `hotbar_slot` from 0 through 8.
- interaction.attack / interaction.use / item.drop: parameters {}.
- inventory.select_hotbar: parameters {"slot": integer from 0 through 8}.
- observation.wait: parameters {}. It performs no world action; use it to await
  the next structured state while navigation or mining is still in progress.
- chat.send: parameters {"message": "exact message"}. In a shared world this is
  heard by other players on the server.
- player.respawn: parameters {}. Dispatch only when the latest player facts
  report dead_or_dying=true; a living player rejects this operation.
- control.release_all: parameters {}.
- A receipt proves only dispatch. Navigation, mining, interaction, inventory,
  and survival outcomes require a later observation with the relevant factual
  position, block, entity, or inventory change.
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
