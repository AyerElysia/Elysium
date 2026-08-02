"""Evidence-driven execution kernel shared by Minecraft bodies."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from .embodiment_contracts import (
    ActionCommand,
    ActionReceipt,
    EmbodiedIntent,
    ExecutionResult,
    PlannerTurn,
    WorldObservation,
)
from .embodiment_trace import EmbodimentTrace


class EvidenceReferenceError(RuntimeError):
    """Raised when a conclusion cites evidence that was never observed."""


class BodySelectionError(RuntimeError):
    """Raised when an unknown or unavailable body is selected."""


class EmbodimentBusyError(RuntimeError):
    """Raised when a second intention is submitted without an interruption."""


class GameBody(Protocol):
    """Operational interface implemented by any Minecraft body."""

    @property
    def name(self) -> str:
        """Return the explicit body name used in intentions."""

    async def open(self) -> None:
        """Connect the body and make it ready to observe and act."""

    async def observe(self, after_sequence: int | None = None) -> WorldObservation:
        """Return a complete observation newer than ``after_sequence`` if supplied."""

    async def act(self, command: ActionCommand) -> ActionReceipt:
        """Execute one operation and return its terminal factual receipt."""

    async def interrupt(self, intent_id: str, reason: str) -> None:
        """Stop work for one intention and release held controls."""

    async def close(self) -> None:
        """Close body resources after releasing controls."""


class IntentPlanner(Protocol):
    """Planner chosen by Elysia to turn observations into operations."""

    async def decide(
        self,
        intent: EmbodiedIntent,
        observations: tuple[WorldObservation, ...],
        receipts: tuple[ActionReceipt, ...],
    ) -> PlannerTurn:
        """Return one command or one evidence-backed conclusion."""


TraceListener = Callable[[str, dict[str, object]], Awaitable[None] | None]


class EmbodimentRuntime:
    """Coordinate explicit body choice, interruption, evidence, and tracing."""

    def __init__(
        self,
        trace: EmbodimentTrace,
        trace_listener: TraceListener | None = None,
    ) -> None:
        """Create a runtime without selecting or opening a body."""

        self._trace = trace
        self._trace_listener = trace_listener
        self._bodies: dict[str, GameBody] = {}
        self._selected: GameBody | None = None
        self._run_lock = asyncio.Lock()
        self._active_intent: EmbodiedIntent | None = None
        self._interrupted = asyncio.Event()

    @property
    def selected_body_name(self) -> str | None:
        """Return the explicitly selected body name."""

        return self._selected.name if self._selected else None

    def register_body(self, body: GameBody) -> None:
        """Register a body without choosing it implicitly."""

        if not body.name.strip():
            raise ValueError("body name must not be empty")
        if body.name in self._bodies:
            raise ValueError(f"body already registered: {body.name}")
        self._bodies[body.name] = body

    async def select_body(self, name: str) -> None:
        """Select one body by exact name and open it."""

        if self._active_intent is not None:
            raise EmbodimentBusyError("cannot switch body while an intention is active")
        body = self._bodies.get(name)
        if body is None:
            raise BodySelectionError(f"body is not registered: {name}")
        if self._selected is body:
            return
        if self._selected is not None:
            await self._selected.close()
        await body.open()
        self._selected = body
        await self._record("body.selected", {"body_name": name})

    async def execute(
        self,
        intent: EmbodiedIntent,
        planner: IntentPlanner,
        *,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult:
        """Pursue one intention until the planner concludes or it is interrupted."""

        if self._run_lock.locked():
            raise EmbodimentBusyError("another intention is active")
        async with self._run_lock:
            body = self._selected
            if body is None:
                raise BodySelectionError("no body has been selected")
            if body.name != intent.body_name:
                raise BodySelectionError(
                    f"intent requested {intent.body_name}, selected body is {body.name}"
                )
            self._active_intent = intent
            self._interrupted.clear()
            observations: list[WorldObservation] = []
            receipts: list[ActionReceipt] = []
            try:
                if timeout_seconds is None:
                    return await self._execute(
                        intent,
                        planner,
                        body,
                        observations,
                        receipts,
                    )
                async with asyncio.timeout(timeout_seconds):
                    return await self._execute(
                        intent,
                        planner,
                        body,
                        observations,
                        receipts,
                    )
            except TimeoutError:
                await body.interrupt(intent.intent_id, "caller deadline elapsed")
                await self._record(
                    "intent.deadline",
                    {"intent_id": intent.intent_id, "timeout_seconds": timeout_seconds},
                )
                return ExecutionResult(
                    intent=intent,
                    observations=tuple(observations),
                    receipts=tuple(receipts),
                    conclusion=None,
                    interrupted=True,
                    error="caller deadline elapsed",
                )
            finally:
                self._active_intent = None

    async def interrupt(self, reason: str) -> None:
        """Interrupt the active intention and release controls immediately."""

        intent = self._active_intent
        body = self._selected
        if intent is None or body is None:
            return
        self._interrupted.set()
        await body.interrupt(intent.intent_id, reason)
        await self._record(
            "intent.interrupted",
            {"intent_id": intent.intent_id, "reason": reason},
        )

    async def close(self) -> None:
        """Interrupt active work and close the selected body."""

        await self.interrupt("runtime closing")
        if self._selected is not None:
            await self._selected.close()
            self._selected = None

    async def _execute(
        self,
        intent: EmbodiedIntent,
        planner: IntentPlanner,
        body: GameBody,
        observations: list[WorldObservation],
        receipts: list[ActionReceipt],
    ) -> ExecutionResult:
        """Run the unbounded planner-body loop under caller-owned lifetime limits."""

        await self._record("intent.issued", intent.to_wire())
        observation = await body.observe()
        observations.append(observation)
        await self._record("observation", observation.to_wire())

        while not self._interrupted.is_set():
            turn = await planner.decide(intent, tuple(observations), tuple(receipts))
            if turn.conclusion is not None:
                known_evidence = {
                    item.observation_id for item in observations
                } | {item.receipt_id for item in receipts}
                missing = set(turn.conclusion.evidence_ids) - known_evidence
                if missing:
                    raise EvidenceReferenceError(
                        f"conclusion cites unknown evidence: {sorted(missing)}"
                    )
                await self._record("intent.conclusion", turn.conclusion.to_wire())
                return ExecutionResult(
                    intent=intent,
                    observations=tuple(observations),
                    receipts=tuple(receipts),
                    conclusion=turn.conclusion,
                )

            command = turn.command
            if command is None:
                raise RuntimeError("planner returned an empty turn")
            if command.intent_id != intent.intent_id:
                raise ValueError("command intent_id does not match the active intention")
            if command.intent_revision != intent.revision:
                raise ValueError("command revision does not match the active intention")
            await self._record("command.issued", command.to_wire())
            receipt = await body.act(command)
            if receipt.command_id != command.command_id:
                raise ValueError("receipt command_id does not match the issued command")
            if receipt.intent_id != intent.intent_id:
                raise ValueError("receipt intent_id does not match the active intention")
            receipts.append(receipt)
            await self._record("command.receipt", receipt.to_wire())
            if self._interrupted.is_set() or receipt.interrupted:
                return ExecutionResult(
                    intent=intent,
                    observations=tuple(observations),
                    receipts=tuple(receipts),
                    conclusion=None,
                    interrupted=True,
                )
            observation = await body.observe(after_sequence=observation.sequence)
            observations.append(observation)
            await self._record("observation", observation.to_wire())

        return ExecutionResult(
            intent=intent,
            observations=tuple(observations),
            receipts=tuple(receipts),
            conclusion=None,
            interrupted=True,
        )

    async def _record(self, kind: str, payload: dict[str, object]) -> None:
        """Persist and optionally publish one complete trace event."""

        await self._trace.append(kind, payload)
        if self._trace_listener is not None:
            result = self._trace_listener(kind, payload)
            if result is not None:
                await result
