"""Durable API command ledger and explicit dispatcher."""

from .dispatcher import CommandDispatcher, CommandHandler, HandlerRegistry, HandlerSpec
from .models import (
    TERMINAL_STATUSES,
    CommandBacklogFull,
    CommandNotCancellable,
    CommandNotFound,
    CommandOutcome,
    CommandRecord,
    CommandStatus,
    IdempotencyConflict,
)
from .store import CommandStore

__all__ = [
    "TERMINAL_STATUSES",
    "CommandBacklogFull",
    "CommandDispatcher",
    "CommandHandler",
    "CommandNotCancellable",
    "CommandNotFound",
    "CommandOutcome",
    "CommandRecord",
    "CommandStatus",
    "CommandStore",
    "HandlerRegistry",
    "HandlerSpec",
    "IdempotencyConflict",
]
