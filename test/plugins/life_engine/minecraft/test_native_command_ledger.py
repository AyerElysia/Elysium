"""Pure replay-protection tests for the Windows native body."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

INTEGRATION_ROOT = (
    Path(__file__).resolve().parents[4] / "integrations" / "windows_native_body"
)
sys.path.insert(0, str(INTEGRATION_ROOT))

from command_ledger import CommandLedger, DecisionKind


def _command(command_id: str, value: int) -> dict[str, object]:
    return {
        "command_id": command_id,
        "intent_id": "intent_1",
        "operation": "native.input_batch",
        "parameters": {"look_dx": value},
    }


def test_native_command_replay_is_idempotent_and_conflicts_are_rejected() -> None:
    ledger = CommandLedger(maximum_terminal_entries=4)
    command = _command("command_1", 2)

    assert ledger.begin("command_1", command).kind is DecisionKind.NEW
    assert ledger.begin("command_1", command).kind is DecisionKind.PENDING_REPLAY
    ledger.complete("command_1", {"receipt": {"receipt_id": "receipt_1"}})
    replay = ledger.begin("command_1", command)

    assert replay.kind is DecisionKind.TERMINAL_REPLAY
    assert replay.terminal_receipt == {"receipt": {"receipt_id": "receipt_1"}}
    assert ledger.begin("command_1", _command("command_1", 9)).kind is (
        DecisionKind.CONFLICT
    )


def test_native_pending_replay_receives_original_terminal_receipt() -> None:
    ledger = CommandLedger(maximum_terminal_entries=4)
    command = _command("command_1", 2)
    ledger.begin("command_1", command)
    replay = ledger.begin("command_1", command)
    terminal = {"type": "receipt", "receipt": {"receipt_id": "receipt_1"}}

    ledger.complete("command_1", terminal)

    assert replay.pending_completion is not None
    assert replay.pending_completion.result(timeout=0.1) == terminal


def test_native_terminal_receipt_cannot_be_replaced() -> None:
    ledger = CommandLedger(maximum_terminal_entries=4)
    command = _command("command_1", 2)
    ledger.begin("command_1", command)
    ledger.complete("command_1", {"receipt": {"receipt_id": "original"}})

    with pytest.raises(RuntimeError, match="already has a terminal receipt"):
        ledger.complete("command_1", {"receipt": {"receipt_id": "replacement"}})


def test_native_terminal_cache_is_bounded_without_losing_pending_work() -> None:
    ledger = CommandLedger(maximum_terminal_entries=2)
    pending = _command("pending", 0)
    ledger.begin("pending", pending)
    for index in range(4):
        command_id = f"command_{index}"
        command = _command(command_id, index)
        ledger.begin(command_id, command)
        ledger.complete(command_id, {"receipt": {"receipt_id": command_id}})

    assert ledger.begin("pending", pending).kind is DecisionKind.PENDING_REPLAY
