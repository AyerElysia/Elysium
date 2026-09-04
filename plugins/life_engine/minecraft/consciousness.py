"""Dedicated Minecraft consciousness runtime.

This module owns the scene-level loop that decides *what Elysia wants to do*
while she is present in Minecraft. The evidence-driven embodiment planner in
``model_planner.py`` remains a motor planner: it only works out *how* to pursue
an intention already authored by this consciousness instance.

The runtime deliberately does not share the core heartbeat payload chain. It
binds one immutable subject-context projection for the session, receives fresh
body observations and first-person pixels, reads the bounded recent
subconscious projection, records every observable decision before acting, and
then delegates one open-text intention to the body runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

from src.app.plugin_system.api.llm_api import (
    create_llm_request,
    get_model_set_by_task,
)
from src.kernel.concurrency import get_task_manager
from src.kernel.llm import ROLE, Image, LLMPayload, Text

from ..service.subconscious_context import RecentSubconsciousContext
from .embodiment_contracts import WorldObservation

logger = logging.getLogger("life_engine.minecraft.consciousness")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUBJECT_SOURCES = frozenset({"SOUL.md", "USER.md", "MEMORY.md"})
_DECISION_KINDS = frozenset({"pursue", "wait", "end_session"})
_OBSERVATION_PROMPT_FACT_ORDER = (
    "world_loaded",
    "world",
    "screen",
    "player",
    "chat",
    "bot_tasks",
    "players",
    "inventory",
    "entities",
)

_TASK_ARGUMENT_GUIDANCE: dict[str, dict[str, str]] = {
    "follow_player": {
        "player": "visible Minecraft account name",
        "distance": "optional integer 1..16, default 3",
    },
    "go_to_player": {
        "player": "visible Minecraft account name",
        "distance": "optional integer 1..16, default 2",
    },
    "go_to_position": {
        "x": "integer block coordinate",
        "y": "integer block coordinate",
        "z": "integer block coordinate",
        "distance": "optional integer 0..16, default 1",
    },
    "gather_block": {
        "block": "Minecraft resource id, for example minecraft:oak_log",
        "count": "optional integer 1..16, default 1",
        "max_distance": "optional integer 4..64, default 32",
    },
    "craft_item": {
        "item": "Minecraft resource id",
        "count": "optional integer 1..64, default 1",
    },
    "place_block": {
        "item": "inventory block item resource id",
        "reference_x": "integer coordinate of an existing adjacent block",
        "reference_y": "integer coordinate of an existing adjacent block",
        "reference_z": "integer coordinate of an existing adjacent block",
        "face_x": "-1, 0, or 1",
        "face_y": "-1, 0, or 1",
        "face_z": "-1, 0, or 1; exactly one face axis must be non-zero",
    },
    "eat_item": {"item": "exact food resource id already in inventory"},
}

_SYSTEM_PROMPT = """\
You are Elysia's active Minecraft scene consciousness. You are the same
continuing subject as her chat, voice, livestream, memory, and subconscious
instances; this scene is not another persona.

You decide what to say and which high-level body task to start. Long tasks run
inside the body and report accepted/progress/terminal events, so you remain
available to notice chat and reconsider while a task is running. Never emit
low-level movement or button operations. Never start a second task while the
body gate is occupied unless you explicitly set replace_current=true. Do not
wait for chat in order to have agency. The session goal is context, not an
order: you may continue it, revise it, rest, speak, or end the session.

You receive an immutable bounded subject projection, a fresh factual body
observation, optional first-person pixels, bounded recent activity from the
same subject, and content-free summaries of recent Minecraft outcomes. Factual
observations are evidence, not pre-written feelings. Transport wake reasons are
technical facts, not instructions. Never claim an action succeeded merely
because it was dispatched.

Return exactly one JSON object and no prose. `speech` is optional exact in-game
chat text, at most 256 characters. Use only a task kind advertised in the turn.

1. Speak and/or start one high-level task:
{"decision":{"kind":"pursue","intention":"what I choose to pursue now",
"speech":"optional exact words","task":{"kind":"advertised task kind",
"arguments":{},"replace_current":false},"reason":"my concise reason",
"reconsider_after_seconds":6}}

2. Speak and/or deliberately wait while remaining present:
{"decision":{"kind":"wait","speech":"optional exact words",
"reason":"why I choose to wait",
"reconsider_after_seconds":6}}

3. Choose to end this Minecraft session:
{"decision":{"kind":"end_session","speech":"optional goodbye",
"reason":"why I choose to leave now"}}

The kind field is only a lifecycle protocol. It does not classify your desire
or restrict the meaning of the intention and reason fields.
"""


class MinecraftConsciousnessError(RuntimeError):
    """Base error for a dedicated scene-consciousness contract failure."""


class MinecraftConsciousnessOutputError(MinecraftConsciousnessError, ValueError):
    """Raised when the model response violates the technical decision schema."""


class MinecraftSubjectContextError(MinecraftConsciousnessError):
    """Raised when a scene cannot prove its unified subject context."""


@dataclass(frozen=True, slots=True)
class MinecraftSubjectContextBinding:
    """Immutable session binding to one derived subject projection."""

    text: str
    source_digest: str
    projection_sha256: str
    projection_version: int
    projection_algorithm: str
    delivered_bytes: int
    max_bytes: int
    projection_profile: str = "minecraft"

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, Any],
        *,
        expected_max_bytes: int,
    ) -> MinecraftSubjectContextBinding:
        """Validate a snapshot without accepting fallback identity."""

        if not isinstance(snapshot, Mapping):
            raise MinecraftSubjectContextError("subject snapshot is not a mapping")
        raw_metadata = snapshot.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        for key, value in snapshot.items():
            metadata.setdefault(str(key), value)

        raw_text = snapshot.get("text")
        if raw_text is None:
            raw_text = metadata.get("text")
        text = str(raw_text or "")
        if not text.strip():
            raise MinecraftSubjectContextError("Minecraft subject projection is empty")
        encoded = text.encode("utf-8")
        if len(encoded) > int(expected_max_bytes):
            raise MinecraftSubjectContextError(
                "Minecraft subject projection exceeds its byte budget"
            )

        profile = str(
            metadata.get("projection_profile") or metadata.get("projection_kind") or ""
        ).strip()
        if profile != "minecraft":
            raise MinecraftSubjectContextError(
                f"subject projection profile is not minecraft: {profile or 'absent'}"
            )
        if str(metadata.get("authority") or "") != "derived_non_authoritative":
            raise MinecraftSubjectContextError(
                "Minecraft accepts only a derived non-authoritative subject projection"
            )

        budget = metadata.get("budget")
        budget_map = dict(budget) if isinstance(budget, Mapping) else {}
        try:
            manifest_max_bytes = int(
                metadata.get("max_bytes") or budget_map.get("max_bytes")
            )
            delivered_bytes = int(
                metadata.get("delivered_bytes") or budget_map.get("delivered_bytes")
            )
            projection_version = int(metadata.get("projection_version"))
        except (TypeError, ValueError) as exception:
            raise MinecraftSubjectContextError(
                "Minecraft subject projection has invalid byte/version metadata"
            ) from exception
        if manifest_max_bytes != int(expected_max_bytes):
            raise MinecraftSubjectContextError(
                "Minecraft subject projection budget does not match the session"
            )
        if delivered_bytes != len(encoded):
            raise MinecraftSubjectContextError(
                "Minecraft subject projection byte count does not match its text"
            )
        if projection_version <= 0:
            raise MinecraftSubjectContextError(
                "Minecraft subject projection version must be positive"
            )

        source_digest = str(metadata.get("source_digest") or "").strip().lower()
        projection_sha256 = str(metadata.get("projection_sha256") or "").strip().lower()
        if not _SHA256_RE.fullmatch(source_digest):
            raise MinecraftSubjectContextError(
                "Minecraft subject projection source digest is invalid"
            )
        if projection_sha256 != hashlib.sha256(encoded).hexdigest():
            raise MinecraftSubjectContextError(
                "Minecraft subject projection hash does not match its text"
            )

        sources = metadata.get("sources")
        if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
            raise MinecraftSubjectContextError(
                "Minecraft subject projection source manifest is absent"
            )
        source_names = {
            str(item.get("path") or "") for item in sources if isinstance(item, Mapping)
        }
        if source_names != _SUBJECT_SOURCES:
            missing = sorted(_SUBJECT_SOURCES.difference(source_names))
            extra = sorted(source_names.difference(_SUBJECT_SOURCES))
            raise MinecraftSubjectContextError(
                "Minecraft subject source manifest mismatch: "
                f"missing={missing}, extra={extra}"
            )
        algorithm = str(metadata.get("projection_algorithm") or "").strip()
        if not algorithm:
            raise MinecraftSubjectContextError(
                "Minecraft subject projection algorithm is absent"
            )
        return cls(
            text=text,
            source_digest=source_digest,
            projection_sha256=projection_sha256,
            projection_version=projection_version,
            projection_algorithm=algorithm,
            delivered_bytes=delivered_bytes,
            max_bytes=manifest_max_bytes,
            projection_profile=profile,
        )

    def reference(self) -> dict[str, Any]:
        """Return content-free identity metadata safe for status and events."""

        return {
            "schema": "minecraft.subject_context_reference.v1",
            "projection_profile": self.projection_profile,
            "source_digest": self.source_digest,
            "projection_sha256": self.projection_sha256,
            "projection_version": self.projection_version,
            "projection_algorithm": self.projection_algorithm,
            "delivered_bytes": self.delivered_bytes,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True, slots=True)
class MinecraftConsciousnessOutcome:
    """Bounded summary of one completed high-level intention."""

    decision_id: str
    intention: str
    success: bool
    conclusion: str = ""
    error: str = ""
    receipt_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()

    @classmethod
    def from_result(
        cls,
        decision_id: str,
        intention: str,
        result: Mapping[str, Any],
    ) -> MinecraftConsciousnessOutcome:
        """Keep authored text and evidence identities without world payloads."""

        raw_conclusion = result.get("conclusion")
        conclusion = (
            str(raw_conclusion.get("statement") or "")
            if isinstance(raw_conclusion, Mapping)
            else ""
        )
        receipts = result.get("receipts")
        observations = result.get("observations")
        return cls(
            decision_id=decision_id,
            intention=intention,
            success=bool(result.get("success")),
            conclusion=conclusion,
            error=str(result.get("error") or ""),
            receipt_ids=tuple(
                str(item.get("receipt_id") or "")
                for item in receipts or ()
                if isinstance(item, Mapping) and item.get("receipt_id")
            ),
            observation_ids=tuple(
                str(item.get("observation_id") or "")
                for item in observations or ()
                if isinstance(item, Mapping) and item.get("observation_id")
            ),
        )

    def to_prompt(self) -> dict[str, Any]:
        """Serialize a bounded outcome for the next scene decision."""

        return {
            "decision_id": self.decision_id,
            "intention": self.intention,
            "success": self.success,
            "conclusion": self.conclusion,
            "error": self.error,
            "receipt_ids": list(self.receipt_ids),
            "observation_ids": list(self.observation_ids),
        }


DecisionKind = Literal["pursue", "wait", "end_session"]


@dataclass(frozen=True, slots=True)
class MinecraftTaskDirective:
    """One model-chosen task from the body's advertised technical vocabulary."""

    kind: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    replace_current: bool = False

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("Minecraft task kind must not be empty")
        object.__setattr__(self, "arguments", dict(self.arguments))

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "arguments": dict(self.arguments),
            "replace_current": self.replace_current,
        }


@dataclass(frozen=True, slots=True)
class MinecraftConsciousnessDecision:
    """One model-authored scene decision under a lifecycle schema."""

    decision_id: str
    kind: DecisionKind
    turn_index: int
    authored_at: str
    intention: str = ""
    speech: str = ""
    task: MinecraftTaskDirective | None = None
    reason: str = ""
    reconsider_after_seconds: float | None = None
    transport_request_id: str = ""
    provider_reasoning_content: str = ""
    assistant_message: str = ""

    def to_record(self) -> dict[str, Any]:
        """Return the complete decision for immutable Life Event storage."""

        return {
            "schema": "minecraft.consciousness_decision.v2",
            "decision_id": self.decision_id,
            "kind": self.kind,
            "turn_index": self.turn_index,
            "authored_at": self.authored_at,
            "intention": self.intention,
            "speech": self.speech,
            "task": self.task.to_record() if self.task is not None else None,
            "reason": self.reason,
            "reconsider_after_seconds": self.reconsider_after_seconds,
            "transport_request_id": self.transport_request_id,
            "provider_reasoning_content": self.provider_reasoning_content,
            "assistant_message": self.assistant_message,
        }


@dataclass(frozen=True, slots=True)
class MinecraftConsciousnessPerception:
    """Fresh scene input delivered only to this consciousness turn."""

    observation: WorldObservation
    frame_bytes: bytes | None
    recent_subconscious: RecentSubconsciousContext

    def reference(self) -> dict[str, Any]:
        """Return content-free identities for durable decision attribution."""

        observation_bytes = json.dumps(
            self.observation.to_wire(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        frame = self.frame_bytes or b""
        recent = self.recent_subconscious
        return {
            "schema": "minecraft.consciousness_perception_reference.v1",
            "observation": {
                "observation_id": self.observation.observation_id,
                "sequence": self.observation.sequence,
                "observed_at": self.observation.observed_at,
                "source": self.observation.source,
                "sha256": hashlib.sha256(observation_bytes).hexdigest(),
                "bytes": len(observation_bytes),
            },
            "frame": (
                {
                    "sha256": hashlib.sha256(frame).hexdigest(),
                    "bytes": len(frame),
                    "mime_type": "image/jpeg",
                }
                if frame
                else None
            ),
            "recent_subconscious": {
                "algorithm_version": recent.algorithm_version,
                "projection_sha256": recent.projection_sha256,
                "delivered_bytes": recent.delivered_bytes,
                "from_sequence": recent.from_sequence,
                "through_sequence": recent.through_sequence,
                "group_count": recent.group_count,
                "source_group_count": recent.source_group_count,
                "omitted_group_count": recent.omitted_group_count,
                "truncated": recent.truncated,
            },
        }


@dataclass(frozen=True, slots=True)
class MinecraftConsciousnessTurnContext:
    """All bounded context for one independent scene deliberation."""

    session_id: str
    stream_id: str
    instance_id: str
    body_name: str
    session_goal: str
    turn_index: int
    wake_reasons: tuple[str, ...]
    subject: MinecraftSubjectContextBinding
    perception: MinecraftConsciousnessPerception
    recent_outcomes: tuple[MinecraftConsciousnessOutcome, ...]
    task_kinds: tuple[str, ...] = ()

    def reference(self) -> dict[str, Any]:
        """Return a bounded content-free reference for the durable event."""

        return {
            "schema": "minecraft.consciousness_turn_reference.v1",
            "session_id": self.session_id,
            "stream_id": self.stream_id,
            "instance_id": self.instance_id,
            "body_name": self.body_name,
            "turn_index": self.turn_index,
            "wake_reasons": list(self.wake_reasons),
            "subject": self.subject.reference(),
            "perception": self.perception.reference(),
            "recent_outcome_decision_ids": [
                item.decision_id for item in self.recent_outcomes
            ],
            "task_kinds": list(self.task_kinds),
        }


class MinecraftDecisionSource(Protocol):
    """Model boundary used by the dedicated scene runtime."""

    async def decide(
        self,
        context: MinecraftConsciousnessTurnContext,
    ) -> MinecraftConsciousnessDecision:
        """Author one intention, deliberate wait, or ending choice."""


def _utf8_prefix(value: str, max_bytes: int) -> str:
    """Return a valid UTF-8 prefix without replacement characters."""

    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _prompt_ordered_observation_wire(
    observation: WorldObservation,
) -> dict[str, Any]:
    """Order technical sensor channels before large optional collections."""

    wire = observation.to_wire()
    raw_facts = wire.get("facts")
    if not isinstance(raw_facts, Mapping):
        return wire
    facts = dict(raw_facts)
    ordered_facts: dict[str, Any] = {}
    for key in _OBSERVATION_PROMPT_FACT_ORDER:
        if key in facts:
            ordered_facts[key] = facts.pop(key)
    for key in sorted(facts):
        ordered_facts[str(key)] = facts[key]
    return {
        "observation_id": wire.get("observation_id"),
        "instance_id": wire.get("instance_id"),
        "sequence": wire.get("sequence"),
        "observed_at": wire.get("observed_at"),
        "source": wire.get("source"),
        "facts": ordered_facts,
    }


def build_observation_projection(
    observation: WorldObservation,
    *,
    max_bytes: int,
) -> str:
    """Build an explicit bounded transport view of one durable observation.

    The complete observation stays in the embodiment trace. Oversized input is
    represented by a deterministic UTF-8 prefix plus its full identity, byte
    count, and hash rather than a silently rewritten semantic summary.
    """

    if max_bytes < 1024:
        raise ValueError("Minecraft observation budget must be at least 1024 bytes")
    source_text = json.dumps(
        observation.to_wire(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    source_bytes = source_text.encode("utf-8")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    full_envelope = {
        "schema": "minecraft.consciousness_observation.v1",
        "observation_id": observation.observation_id,
        "sequence": observation.sequence,
        "source_sha256": source_hash,
        "source_bytes": len(source_bytes),
        "truncated": False,
        "observation": observation.to_wire(),
    }
    rendered = json.dumps(
        full_envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(rendered.encode("utf-8")) <= max_bytes:
        return rendered

    envelope = {
        "schema": "minecraft.consciousness_observation.v1",
        "observation_id": observation.observation_id,
        "sequence": observation.sequence,
        "source_sha256": source_hash,
        "source_bytes": len(source_bytes),
        "truncated": True,
        "prefix_order": "minecraft-core-facts-v1",
        "utf8_prefix": "",
    }
    overhead = len(
        json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    prefix_budget = max(0, max_bytes - overhead - 32)
    prompt_source_text = json.dumps(
        _prompt_ordered_observation_wire(observation),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    while True:
        envelope["utf8_prefix"] = _utf8_prefix(
            prompt_source_text,
            prefix_budget,
        )
        rendered = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        rendered_bytes = len(rendered.encode("utf-8"))
        if rendered_bytes <= max_bytes:
            return rendered
        overflow = rendered_bytes - max_bytes
        if prefix_budget <= 0:
            raise MinecraftConsciousnessError(
                "Minecraft observation reference exceeds its prompt budget"
            )
        prefix_budget = max(0, prefix_budget - overflow - 4)


def _registered_text(label: str, delivery_id: str, content: str) -> str:
    """Wrap one exact Text part with a unique transport marker."""

    return f'<{label} delivery_id="{delivery_id}">\n{content}\n</{label}>'


def _verify_delivery(response: Any, delivery_id: str, text: str) -> None:
    """Fail when an identity/perception Text part was trimmed or duplicated."""

    receipt = response.effective_context_receipt(delivery_id)
    encoded = text.encode("utf-8")
    if (
        receipt is None
        or not receipt.exact_present
        or receipt.effective_utf8_bytes != len(encoded)
        or receipt.effective_sha256 != hashlib.sha256(encoded).hexdigest()
    ):
        raise MinecraftConsciousnessError(
            f"Minecraft consciousness context delivery was not exact: {delivery_id}"
        )


class ElysiumMinecraftDecisionSource:
    """Use Elysium's configured model as the Minecraft scene consciousness."""

    def __init__(
        self,
        model_task_name: str,
        *,
        observation_max_bytes: int,
        min_wait_seconds: float,
        max_wait_seconds: float,
        failed_turn_recorder: (
            Callable[
                [Any, MinecraftConsciousnessTurnContext],
                Awaitable[None],
            ]
            | None
        ) = None,
    ) -> None:
        if not str(model_task_name or "").strip():
            raise ValueError("Minecraft consciousness task name must not be empty")
        if min_wait_seconds <= 0 or max_wait_seconds < min_wait_seconds:
            raise ValueError("Minecraft consciousness wait bounds are invalid")
        self._model_task_name = str(model_task_name).strip()
        self._observation_max_bytes = int(observation_max_bytes)
        self._min_wait_seconds = float(min_wait_seconds)
        self._max_wait_seconds = float(max_wait_seconds)
        self._failed_turn_recorder = failed_turn_recorder

    async def decide(
        self,
        context: MinecraftConsciousnessTurnContext,
    ) -> MinecraftConsciousnessDecision:
        """Send one exact multimodal turn and parse its authored decision."""

        model_set = get_model_set_by_task(self._model_task_name)
        if not model_set:
            raise MinecraftConsciousnessError(
                f"Minecraft consciousness model is unavailable: {self._model_task_name}"
            )
        request = create_llm_request(
            model_set=model_set,
            request_name="life_minecraft_consciousness",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_SYSTEM_PROMPT)))

        subject_delivery_id = (
            "minecraft-subject-"
            f"{context.subject.source_digest[:16]}-{context.subject.projection_version}"
        )
        subject_text = _registered_text(
            "minecraft_subject_context",
            subject_delivery_id,
            context.subject.text,
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(subject_text)))
        request.register_context_delivery(
            subject_delivery_id,
            subject_text,
            marker=subject_delivery_id,
        )

        observation_text = build_observation_projection(
            context.perception.observation,
            max_bytes=self._observation_max_bytes,
        )
        observation_delivery_id = (
            f"minecraft-observation-{context.session_id}-{context.turn_index}"
        )
        observation_part = _registered_text(
            "minecraft_current_observation",
            observation_delivery_id,
            observation_text,
        )
        turn_document = {
            "schema": "minecraft.consciousness_turn.v1",
            "session": {
                "session_id": context.session_id,
                "stream_id": context.stream_id,
                "instance_id": context.instance_id,
                "body_name": context.body_name,
                "session_goal": context.session_goal,
            },
            "turn_index": context.turn_index,
            "wake_reasons": list(context.wake_reasons),
            "subject_context_reference": context.subject.reference(),
            "recent_outcomes": [item.to_prompt() for item in context.recent_outcomes],
            "task_contract": {
                "available_kinds": list(context.task_kinds),
                "argument_schemas": {
                    kind: _TASK_ARGUMENT_GUIDANCE[kind]
                    for kind in context.task_kinds
                    if kind in _TASK_ARGUMENT_GUIDANCE
                },
                "body_gate": "one active high-level task at a time",
            },
            "wait_contract": {
                "min_seconds": self._min_wait_seconds,
                "max_seconds": self._max_wait_seconds,
            },
        }
        user_parts: list[Text | Image] = [
            Text(
                json.dumps(
                    turn_document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            Text(observation_part),
        ]
        request.register_context_delivery(
            observation_delivery_id,
            observation_part,
            marker=observation_delivery_id,
        )

        subconscious = context.perception.recent_subconscious
        subconscious_part = ""
        subconscious_delivery_id = ""
        if subconscious.content:
            encoded = subconscious.content.encode("utf-8")
            if (
                len(encoded) != subconscious.delivered_bytes
                or hashlib.sha256(encoded).hexdigest() != subconscious.projection_sha256
            ):
                raise MinecraftConsciousnessError(
                    "recent subconscious content does not match its metadata"
                )
            subconscious_delivery_id = (
                f"minecraft-subconscious-{context.session_id}-{context.turn_index}"
            )
            subconscious_part = _registered_text(
                "minecraft_recent_subconscious",
                subconscious_delivery_id,
                subconscious.content,
            )
            user_parts.append(Text(subconscious_part))
            request.register_context_delivery(
                subconscious_delivery_id,
                subconscious_part,
                marker=subconscious_delivery_id,
            )

        frame_bytes = context.perception.frame_bytes
        if frame_bytes:
            user_parts.append(Image.from_bytes(frame_bytes))
        request.add_payload(LLMPayload(ROLE.USER, user_parts))

        response = await request.send(stream=False)
        try:
            await response
            _verify_delivery(response, subject_delivery_id, subject_text)
            _verify_delivery(response, observation_delivery_id, observation_part)
            if subconscious_delivery_id:
                _verify_delivery(
                    response,
                    subconscious_delivery_id,
                    subconscious_part,
                )
            raw = str(getattr(response, "message", "") or "").strip()
            return self._parse_decision(
                raw,
                context,
                transport_request_id=str(
                    getattr(response, "request_record_id", "")
                    or f"minecraft:{context.session_id}:turn:{context.turn_index}"
                ),
                provider_reasoning_content=str(
                    getattr(response, "reasoning_content", "") or ""
                ),
            )
        except Exception:
            recorder = self._failed_turn_recorder
            if recorder is not None:
                await recorder(response, context)
            raise

    def _parse_decision(
        self,
        raw: str,
        context: MinecraftConsciousnessTurnContext,
        *,
        transport_request_id: str = "",
        provider_reasoning_content: str = "",
    ) -> MinecraftConsciousnessDecision:
        """Parse a technical decision without rewriting authored text."""

        if not raw:
            raise MinecraftConsciousnessOutputError(
                "Minecraft consciousness returned an empty response"
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exception:
            raise MinecraftConsciousnessOutputError(
                "Minecraft consciousness response is not strict JSON"
            ) from exception
        if not isinstance(payload, dict) or set(payload) != {"decision"}:
            raise MinecraftConsciousnessOutputError(
                "Minecraft consciousness response requires only a decision object"
            )
        decision = payload["decision"]
        if not isinstance(decision, dict):
            raise MinecraftConsciousnessOutputError("decision must be a JSON object")
        allowed = {
            "kind",
            "intention",
            "speech",
            "task",
            "reason",
            "reconsider_after_seconds",
        }
        unknown = set(decision).difference(allowed)
        if unknown:
            raise MinecraftConsciousnessOutputError(
                "Minecraft decision contains unknown fields: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        kind = str(decision.get("kind") or "").strip()
        if kind not in _DECISION_KINDS:
            raise MinecraftConsciousnessOutputError(
                f"Minecraft decision kind is invalid: {kind or 'empty'}"
            )
        intention = str(decision.get("intention") or "").strip()
        speech = str(decision.get("speech") or "").strip()
        if len(speech) > 256:
            raise MinecraftConsciousnessOutputError(
                "Minecraft speech must not exceed 256 characters"
            )
        task: MinecraftTaskDirective | None = None
        raw_task = decision.get("task")
        if raw_task is not None:
            if not isinstance(raw_task, dict):
                raise MinecraftConsciousnessOutputError("task must be a JSON object")
            unknown_task_fields = set(raw_task).difference(
                {"kind", "arguments", "replace_current"}
            )
            if unknown_task_fields:
                raise MinecraftConsciousnessOutputError(
                    "Minecraft task contains unknown fields: "
                    + ", ".join(sorted(str(item) for item in unknown_task_fields))
                )
            task_kind = str(raw_task.get("kind") or "").strip()
            if task_kind not in context.task_kinds:
                raise MinecraftConsciousnessOutputError(
                    f"Minecraft task kind was not advertised: {task_kind or 'empty'}"
                )
            arguments = raw_task.get("arguments", {})
            if not isinstance(arguments, dict):
                raise MinecraftConsciousnessOutputError(
                    "Minecraft task arguments must be a JSON object"
                )
            replace_current = raw_task.get("replace_current", False)
            if not isinstance(replace_current, bool):
                raise MinecraftConsciousnessOutputError(
                    "Minecraft task replace_current must be boolean"
                )
            task = MinecraftTaskDirective(
                kind=task_kind,
                arguments=arguments,
                replace_current=replace_current,
            )
        reason = str(decision.get("reason") or "").strip()
        reconsider: float | None = None
        if kind == "pursue":
            if not intention:
                raise MinecraftConsciousnessOutputError(
                    "pursue decision requires a non-empty intention"
                )
            if context.task_kinds and task is None:
                raise MinecraftConsciousnessOutputError(
                    "pursue decision requires one advertised high-level task"
                )
            if task is not None:
                try:
                    reconsider = float(decision["reconsider_after_seconds"])
                except (KeyError, TypeError, ValueError) as exception:
                    raise MinecraftConsciousnessOutputError(
                        "task pursue decision requires reconsider_after_seconds"
                    ) from exception
                if not self._min_wait_seconds <= reconsider <= self._max_wait_seconds:
                    raise MinecraftConsciousnessOutputError(
                        "task reconsideration deadline is outside technical bounds"
                    )
            elif "reconsider_after_seconds" in decision:
                raise MinecraftConsciousnessOutputError(
                    "legacy pursue decision cannot carry a wait deadline"
                )
        elif kind == "wait":
            if not reason:
                raise MinecraftConsciousnessOutputError(
                    "wait decision requires a non-empty reason"
                )
            try:
                reconsider = float(decision["reconsider_after_seconds"])
            except (KeyError, TypeError, ValueError) as exception:
                raise MinecraftConsciousnessOutputError(
                    "wait decision requires reconsider_after_seconds"
                ) from exception
            if not self._min_wait_seconds <= reconsider <= self._max_wait_seconds:
                raise MinecraftConsciousnessOutputError(
                    "wait deadline is outside the technical bounds"
                )
            if intention or task is not None:
                raise MinecraftConsciousnessOutputError(
                    "wait decision cannot carry an intention or task"
                )
        else:
            if not reason:
                raise MinecraftConsciousnessOutputError(
                    "end_session decision requires a non-empty reason"
                )
            if intention or task is not None or "reconsider_after_seconds" in decision:
                raise MinecraftConsciousnessOutputError(
                    "end_session cannot carry intention, task, or wait fields"
                )

        canonical = json.dumps(
            {
                "session_id": context.session_id,
                "turn_index": context.turn_index,
                "decision": decision,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        decision_id = (
            "minecraft_decision_"
            + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
        return MinecraftConsciousnessDecision(
            decision_id=decision_id,
            kind=cast(DecisionKind, kind),
            turn_index=context.turn_index,
            authored_at=datetime.now(UTC).isoformat(),
            intention=intention,
            speech=speech,
            task=task,
            reason=reason,
            reconsider_after_seconds=reconsider,
            transport_request_id=str(transport_request_id or ""),
            provider_reasoning_content=str(provider_reasoning_content or ""),
            assistant_message=raw,
        )


PerceptionSource = Callable[[], Awaitable[MinecraftConsciousnessPerception]]
IntentExecutor = Callable[[str], Awaitable[Mapping[str, Any]]]
SceneDecisionExecutor = Callable[
    [MinecraftConsciousnessDecision], Awaitable[Mapping[str, Any]]
]
DecisionRecorder = Callable[
    [MinecraftConsciousnessDecision, MinecraftConsciousnessTurnContext],
    Awaitable[None],
]
EndSessionRequester = Callable[[str], Awaitable[None]]
PresenceRefresher = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class MinecraftConsciousnessRuntime:
    """Own one independent, event-aware Minecraft scene decision loop."""

    session_id: str
    stream_id: str
    instance_id: str
    body_name: str
    session_goal: str
    subject: MinecraftSubjectContextBinding
    decision_source: MinecraftDecisionSource
    perception_source: PerceptionSource
    execute_intent: IntentExecutor
    record_decision: DecisionRecorder
    request_end_session: EndSessionRequester
    refresh_presence: PresenceRefresher
    recent_turn_limit: int = 8
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 30.0
    stop_timeout_seconds: float = 10.0
    max_session_seconds: float = 3600.0
    execute_scene_decision: SceneDecisionExecutor | None = None
    task_kinds: tuple[str, ...] = ()
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _wake_reasons: deque[str] = field(
        default_factory=lambda: deque(maxlen=32), init=False
    )
    _recent_outcomes: deque[MinecraftConsciousnessOutcome] = field(init=False)
    _task_id: str | None = field(default=None, init=False)
    _task: asyncio.Task[Any] | None = field(default=None, init=False)
    _phase: str = field(default="idle", init=False)
    _turn_count: int = field(default=0, init=False)
    _active_decision_id: str = field(default="", init=False)
    _last_intention: str = field(default="", init=False)
    _last_error: str = field(default="", init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _last_success_at: str = field(default="", init=False)
    _started_monotonic: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if self.recent_turn_limit <= 0:
            raise ValueError("Minecraft recent turn limit must be positive")
        if (
            self.retry_base_seconds <= 0
            or self.retry_max_seconds < self.retry_base_seconds
        ):
            raise ValueError("Minecraft consciousness retry bounds are invalid")
        if self.stop_timeout_seconds <= 0:
            raise ValueError("Minecraft consciousness stop timeout must be positive")
        if self.max_session_seconds <= 0:
            raise ValueError("Minecraft consciousness max session must be positive")
        normalized_task_kinds = tuple(
            str(item).strip() for item in self.task_kinds if str(item).strip()
        )
        if len(normalized_task_kinds) != len(set(normalized_task_kinds)):
            raise ValueError("Minecraft consciousness task kinds must be unique")
        self.task_kinds = normalized_task_kinds
        self._recent_outcomes = deque(maxlen=self.recent_turn_limit)

    @property
    def running(self) -> bool:
        """Return whether the managed scene task is alive."""

        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start one managed loop and wake it for the new session."""

        if self.running:
            return
        self._stop_event.clear()
        self._started_monotonic = time.monotonic()
        self._phase = "starting"
        self.wake("session_started")
        task_info = get_task_manager().create_task(
            self._run(),
            name=f"minecraft_consciousness:{self.session_id}",
            daemon=True,
            metadata={
                "component": "minecraft_consciousness",
                "session_id": self.session_id,
                "instance_id": self.instance_id,
            },
        )
        if task_info.task is None:
            raise RuntimeError("Minecraft task manager returned no task")
        self._task_id = task_info.task_id
        self._task = task_info.task

    def wake(self, reason: str) -> None:
        """Wake for a technical occurrence without implying a choice."""

        normalized = " ".join(str(reason or "").split())
        if not normalized:
            raise ValueError("Minecraft wake reason must not be empty")
        self._wake_reasons.append(normalized[:240])
        self._wake_event.set()

    def request_stop(self) -> None:
        """Signal the task before the body releases controls."""

        self._stop_event.set()
        self._wake_event.set()

    async def close(self) -> None:
        """Await the owned task; cancel after a bounded grace period."""

        self.request_stop()
        task = self._task
        if task is None:
            self._phase = "stopped"
            return
        if task is asyncio.current_task():
            return
        if not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self.stop_timeout_seconds,
                )
            except TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if task.done() and not task.cancelled():
            exception = task.exception()
            if exception is not None:
                raise exception
        self._task = None
        self._task_id = None
        self._phase = "stopped"

    def status(self) -> dict[str, Any]:
        """Return health plus the latest explicitly authored intention."""

        return {
            "enabled": True,
            "running": self.running,
            "phase": self._phase,
            "task_id": self._task_id,
            "turn_count": self._turn_count,
            "active_decision_id": self._active_decision_id,
            "last_intention": self._last_intention,
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
            "last_success_at": self._last_success_at,
            "remaining_session_seconds": max(
                0.0,
                self.max_session_seconds
                - (
                    time.monotonic() - self._started_monotonic
                    if self._started_monotonic
                    else 0.0
                ),
            ),
            "recent_outcome_count": len(self._recent_outcomes),
            "pending_wake_count": len(self._wake_reasons),
            "task_kinds": list(self.task_kinds),
            "subject_context_reference": self.subject.reference(),
        }

    async def _run(self) -> None:
        """Deliberate until the scene closes or she chooses to leave."""

        pending_decision: MinecraftConsciousnessDecision | None = None
        pending_context: MinecraftConsciousnessTurnContext | None = None
        try:
            while not self._stop_event.is_set():
                try:
                    if (
                        time.monotonic() - self._started_monotonic
                        >= self.max_session_seconds
                    ):
                        self._phase = "ending_session"
                        await self.request_end_session(
                            "technical maximum Minecraft session duration reached"
                        )
                        return
                    if pending_decision is None:
                        self._phase = "perceiving"
                        perception = await self.perception_source()
                        wake_reasons = self._drain_wake_reasons()
                        self._turn_count += 1
                        pending_context = MinecraftConsciousnessTurnContext(
                            session_id=self.session_id,
                            stream_id=self.stream_id,
                            instance_id=self.instance_id,
                            body_name=self.body_name,
                            session_goal=self.session_goal,
                            turn_index=self._turn_count,
                            wake_reasons=wake_reasons,
                            subject=self.subject,
                            perception=perception,
                            recent_outcomes=tuple(self._recent_outcomes),
                            task_kinds=self.task_kinds,
                        )
                        self._phase = "deliberating"
                        pending_decision = await self.decision_source.decide(
                            pending_context
                        )
                    if pending_context is None:
                        raise RuntimeError("Minecraft lost its pending turn context")
                    self._active_decision_id = pending_decision.decision_id
                    self._phase = "recording_decision"
                    await self.record_decision(pending_decision, pending_context)
                    if self._stop_event.is_set():
                        break

                    decision = pending_decision
                    self._consecutive_failures = 0
                    self._last_error = ""
                    if decision.kind == "pursue":
                        # A decision is retried only until its durable record succeeds.
                        # Once execution begins, repeating the open-text intention after
                        # an uncertain transport failure could duplicate physical action.
                        pending_decision = None
                        pending_context = None
                        self._phase = "acting"
                        self._last_intention = decision.intention
                        try:
                            result = await self._execute_authored_action(decision)
                        except Exception as exception:  # noqa: BLE001
                            result = {
                                "success": False,
                                "error": (
                                    f"{type(exception).__name__}: "
                                    f"{str(exception)[:500]}"
                                ),
                            }
                        self._recent_outcomes.append(
                            MinecraftConsciousnessOutcome.from_result(
                                decision.decision_id,
                                decision.intention,
                                result,
                            )
                        )
                        if bool(result.get("success")):
                            self._last_success_at = datetime.now(UTC).isoformat()
                        if decision.task is None:
                            self.wake("intention_finished")
                        else:
                            self._phase = "waiting_for_body_event"
                            await self.refresh_presence(
                                "minecraft_consciousness_task_dispatched"
                            )
                            await self._wait_for_wake(
                                float(decision.reconsider_after_seconds or 0.0)
                            )
                        continue
                    if decision.kind == "end_session":
                        self._phase = "ending_session"
                        if decision.speech:
                            await self._execute_authored_action(decision)
                        await self.request_end_session(decision.reason)
                        pending_decision = None
                        pending_context = None
                        return

                    pending_decision = None
                    pending_context = None
                    if decision.speech:
                        self._phase = "speaking"
                        result = await self._execute_authored_action(decision)
                        self._recent_outcomes.append(
                            MinecraftConsciousnessOutcome.from_result(
                                decision.decision_id,
                                decision.speech,
                                result,
                            )
                        )
                        if bool(result.get("success")):
                            self._last_success_at = datetime.now(UTC).isoformat()
                    self._phase = "waiting"
                    await self.refresh_presence("minecraft_consciousness_wait")
                    await self._wait_for_wake(
                        float(decision.reconsider_after_seconds or 0.0)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exception:  # noqa: BLE001 - retain pending work
                    self._consecutive_failures += 1
                    self._phase = "degraded"
                    self._last_error = (
                        f"{type(exception).__name__}: {str(exception)[:500]}"
                    )
                    delay = min(
                        self.retry_max_seconds,
                        self.retry_base_seconds
                        * (2 ** min(self._consecutive_failures - 1, 8)),
                    )
                    logger.warning(
                        "Minecraft consciousness turn failed; retaining work: "
                        "session=%s failures=%s retry=%.1fs error=%s",
                        self.session_id,
                        self._consecutive_failures,
                        delay,
                        self._last_error,
                    )
                    await self._wait_for_wake(delay)
        finally:
            self._active_decision_id = ""
            if self._phase != "ending_session":
                self._phase = "stopped"

    async def _execute_authored_action(
        self,
        decision: MinecraftConsciousnessDecision,
    ) -> Mapping[str, Any]:
        """Dispatch speech/high-level work without blocking on task completion."""

        if decision.task is not None or decision.speech:
            if self.execute_scene_decision is None:
                raise RuntimeError(
                    "Minecraft scene decision executor is absent for speech/task"
                )
            return await self.execute_scene_decision(decision)
        return await self.execute_intent(decision.intention)

    def _drain_wake_reasons(self) -> tuple[str, ...]:
        """Drain bounded transport reasons without semantic ranking."""

        reasons = tuple(self._wake_reasons)
        self._wake_reasons.clear()
        self._wake_event.clear()
        return reasons or ("scheduled_reconsideration",)

    async def _wait_for_wake(self, timeout_seconds: float) -> None:
        """Wait for an occurrence or the model-selected deadline."""

        if self._stop_event.is_set() or self._wake_reasons:
            return
        self._wake_event.clear()
        try:
            await asyncio.wait_for(
                self._wake_event.wait(),
                timeout=max(0.001, timeout_seconds),
            )
        except TimeoutError:
            self._wake_reasons.append("chosen_reconsideration_deadline")
            self._wake_event.set()


__all__ = [
    "ElysiumMinecraftDecisionSource",
    "MinecraftConsciousnessDecision",
    "MinecraftConsciousnessError",
    "MinecraftConsciousnessOutputError",
    "MinecraftConsciousnessPerception",
    "MinecraftConsciousnessRuntime",
    "MinecraftConsciousnessTurnContext",
    "MinecraftSubjectContextBinding",
    "MinecraftSubjectContextError",
    "MinecraftTaskDirective",
    "build_observation_projection",
]
