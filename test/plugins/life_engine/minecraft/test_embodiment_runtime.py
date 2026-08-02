"""Contract tests for evidence-driven Minecraft execution."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from plugins.life_engine.minecraft.embodiment_contracts import (
    ActionCommand,
    ActionReceipt,
    EmbodiedIntent,
    IntentConclusion,
    PlannerTurn,
    WorldObservation,
    utc_now,
)
from plugins.life_engine.minecraft.embodiment_runtime import (
    BodySelectionError,
    EmbodimentRuntime,
    EvidenceReferenceError,
)
from plugins.life_engine.minecraft.embodiment_trace import (
    EmbodimentTrace,
    TraceIntegrityError,
)


class _RecordingBody:
    """Deterministic body that records lifecycle and action calls."""

    def __init__(self, name: str = "agent") -> None:
        """Create a closed body with no observations emitted."""

        self._name = name
        self.opened = False
        self.closed = False
        self.interruptions: list[tuple[str, str]] = []
        self.commands: list[ActionCommand] = []
        self.sequence = 0

    @property
    def name(self) -> str:
        """Return the configured test body name."""

        return self._name

    async def open(self) -> None:
        """Record body opening."""

        self.opened = True

    async def observe(self, after_sequence: int | None = None) -> WorldObservation:
        """Emit the next contiguous factual observation."""

        self.sequence += 1
        return WorldObservation(
            instance_id="world-test",
            sequence=self.sequence,
            observed_at=utc_now(),
            source="test",
            facts={"position": {"x": self.sequence, "y": 64, "z": 0}},
        )

    async def act(self, command: ActionCommand) -> ActionReceipt:
        """Record a command and return a terminal factual receipt."""

        self.commands.append(command)
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
        """Record one interruption request."""

        self.interruptions.append((intent_id, reason))

    async def close(self) -> None:
        """Record body closure."""

        self.closed = True


class _OneActionPlanner:
    """Issue one open-vocabulary action and then conclude from evidence."""

    async def decide(
        self,
        intent: EmbodiedIntent,
        observations: tuple[WorldObservation, ...],
        receipts: tuple[ActionReceipt, ...],
    ) -> PlannerTurn:
        """Produce a command first and an evidence-backed conclusion second."""

        if not receipts:
            return PlannerTurn(
                command=ActionCommand(
                    intent_id=intent.intent_id,
                    intent_revision=intent.revision,
                    operation="baritone.goal",
                    parameters={"goal": intent.text},
                    based_on_observation=observations[-1].observation_id,
                )
            )
        return PlannerTurn(
            conclusion=IntentConclusion(
                statement="The requested action was dispatched and the world was observed again.",
                evidence_ids=(
                    receipts[-1].receipt_id,
                    observations[-1].observation_id,
                ),
            )
        )


class _InvalidConclusionPlanner:
    """Return a conclusion citing evidence that does not exist."""

    async def decide(
        self,
        intent: EmbodiedIntent,
        observations: tuple[WorldObservation, ...],
        receipts: tuple[ActionReceipt, ...],
    ) -> PlannerTurn:
        """Return an invalid conclusion for contract validation."""

        return PlannerTurn(
            conclusion=IntentConclusion(
                statement="Unsupported claim",
                evidence_ids=("missing-evidence",),
            )
        )


async def _runtime(tmp_path: Path) -> tuple[EmbodimentRuntime, EmbodimentTrace]:
    """Create a runtime with an opened trace."""

    trace = EmbodimentTrace(tmp_path / "trace.jsonl")
    await trace.open()
    return EmbodimentRuntime(trace), trace


async def test_runtime_requires_explicit_body_selection(tmp_path: Path) -> None:
    """An intention cannot silently select a body or fallback route."""

    runtime, _ = await _runtime(tmp_path)
    intent = EmbodiedIntent(text="explore", body_name="agent")

    with pytest.raises(BodySelectionError):
        await runtime.execute(intent, _OneActionPlanner())


async def test_runtime_records_command_receipt_observation_and_conclusion(
    tmp_path: Path,
) -> None:
    """A completed run retains its full evidence chain."""

    runtime, trace = await _runtime(tmp_path)
    body = _RecordingBody()
    runtime.register_body(body)
    await runtime.select_body("agent")
    intent = EmbodiedIntent(text="walk to the cherry tree", body_name="agent")

    result = await runtime.execute(intent, _OneActionPlanner())

    assert result.conclusion is not None
    assert len(result.receipts) == 1
    assert len(result.observations) == 2
    assert body.commands[0].operation == "baritone.goal"
    kinds = [record.kind for record in await trace.verify()]
    assert kinds == [
        "body.selected",
        "intent.issued",
        "observation",
        "command.issued",
        "command.receipt",
        "observation",
        "intent.conclusion",
    ]


async def test_runtime_rejects_unobserved_success_claim(tmp_path: Path) -> None:
    """The kernel never converts a planner assertion into fake evidence."""

    runtime, _ = await _runtime(tmp_path)
    runtime.register_body(_RecordingBody())
    await runtime.select_body("agent")

    with pytest.raises(EvidenceReferenceError):
        await runtime.execute(
            EmbodiedIntent(text="obtain diamonds", body_name="agent"),
            _InvalidConclusionPlanner(),
        )


async def test_runtime_interrupt_reaches_selected_body(tmp_path: Path) -> None:
    """An explicit interruption is delivered with the active intent identity."""

    runtime, _ = await _runtime(tmp_path)
    body = _RecordingBody()
    runtime.register_body(body)
    await runtime.select_body("agent")
    intent = EmbodiedIntent(text="keep walking", body_name="agent")
    entered = asyncio.Event()

    class WaitingPlanner:
        """Wait until the test interrupts the active intention."""

        async def decide(
            self,
            current: EmbodiedIntent,
            observations: tuple[WorldObservation, ...],
            receipts: tuple[ActionReceipt, ...],
        ) -> PlannerTurn:
            """Block until cancellation after exposing planner entry."""

            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    task = asyncio.create_task(runtime.execute(intent, WaitingPlanner()))
    await entered.wait()
    await runtime.interrupt("Elysia changed her mind")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert body.interruptions == [(intent.intent_id, "Elysia changed her mind")]


async def test_runtime_deadline_preserves_partial_evidence(tmp_path: Path) -> None:
    """A caller deadline returns observations and receipts collected before it."""

    runtime, _ = await _runtime(tmp_path)
    body = _RecordingBody()
    runtime.register_body(body)
    await runtime.select_body("agent")
    intent = EmbodiedIntent(text="inspect the nearby area", body_name="agent")

    class WaitingAfterActionPlanner(_OneActionPlanner):
        """Issue one action, then wait beyond the caller-owned deadline."""

        async def decide(
            self,
            current: EmbodiedIntent,
            observations: tuple[WorldObservation, ...],
            receipts: tuple[ActionReceipt, ...],
        ) -> PlannerTurn:
            """Delegate the first action and block after evidence returns."""

            if receipts:
                await asyncio.Event().wait()
            return await super().decide(current, observations, receipts)

    result = await runtime.execute(
        intent,
        WaitingAfterActionPlanner(),
        timeout_seconds=0.05,
    )

    assert result.interrupted is True
    assert len(result.observations) == 2
    assert len(result.receipts) == 1
    assert body.interruptions == [(intent.intent_id, "caller deadline elapsed")]


async def test_trace_detects_persisted_tampering(tmp_path: Path) -> None:
    """A modified historical payload invalidates the hash chain."""

    trace = EmbodimentTrace(tmp_path / "trace.jsonl")
    await trace.open()
    await trace.append("fact", {"health": 20})
    raw = json.loads(trace.path.read_text(encoding="utf-8"))
    raw["payload"]["health"] = 1
    trace.path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(TraceIntegrityError):
        await trace.verify()
