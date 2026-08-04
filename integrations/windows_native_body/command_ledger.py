"""Bounded idempotency ledger for the Windows native body."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DecisionKind(StrEnum):
    NEW = "new"
    PENDING_REPLAY = "pending_replay"
    TERMINAL_REPLAY = "terminal_replay"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Decision:
    kind: DecisionKind
    terminal_receipt: dict[str, Any] | None = None


class CommandLedger:
    """Keep pending commands and a bounded LRU set of terminal receipts."""

    def __init__(self, maximum_terminal_entries: int = 1024) -> None:
        if maximum_terminal_entries < 1:
            raise ValueError("maximum_terminal_entries must be positive")
        self._maximum_terminal_entries = maximum_terminal_entries
        self._entries: OrderedDict[str, tuple[str, dict[str, Any] | None]] = (
            OrderedDict()
        )

    def begin(self, command_id: str, command: dict[str, Any]) -> Decision:
        fingerprint = self._fingerprint(command)
        existing = self._entries.get(command_id)
        if existing is None:
            self._entries[command_id] = (fingerprint, None)
            return Decision(DecisionKind.NEW)
        self._entries.move_to_end(command_id)
        existing_fingerprint, terminal = existing
        if not hmac.compare_digest(existing_fingerprint, fingerprint):
            return Decision(DecisionKind.CONFLICT)
        if terminal is None:
            return Decision(DecisionKind.PENDING_REPLAY)
        return Decision(
            DecisionKind.TERMINAL_REPLAY,
            json.loads(json.dumps(terminal)),
        )

    def complete(self, command_id: str, terminal_receipt: dict[str, Any]) -> None:
        existing = self._entries.get(command_id)
        if existing is None:
            raise RuntimeError(f"command was not reserved: {command_id}")
        self._entries[command_id] = (
            existing[0],
            json.loads(json.dumps(terminal_receipt)),
        )
        self._entries.move_to_end(command_id)
        self._prune()

    def _prune(self) -> None:
        terminal_count = sum(
            terminal is not None for _, terminal in self._entries.values()
        )
        if terminal_count <= self._maximum_terminal_entries:
            return
        for command_id, (_, terminal) in tuple(self._entries.items()):
            if terminal is not None:
                del self._entries[command_id]
                terminal_count -= 1
                if terminal_count <= self._maximum_terminal_entries:
                    break

    @staticmethod
    def _fingerprint(command: dict[str, Any]) -> str:
        canonical = json.dumps(
            command,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()
