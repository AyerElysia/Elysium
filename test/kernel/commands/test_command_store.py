"""Durability, idempotency, recovery, and dispatcher contracts."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from src.kernel.commands import (
    CommandDispatcher,
    CommandNotCancellable,
    CommandOutcome,
    CommandStatus,
    CommandStore,
    HandlerRegistry,
    IdempotencyConflict,
)

from src.kernel.concurrency import TaskManager


def _accept(store: CommandStore, *, key: str = "request-key-001", value: int = 1):
    request_hash = store.request_hash(
        command_type="test.command.run",
        schema_version=1,
        target={"resource_id": "resource-1"},
        payload={"value": value},
        correlation_id="corr-1",
        expected_revision=None,
    )
    return store.accept(
        idempotency_key=key,
        request_hash=request_hash,
        command_type="test.command.run",
        schema_version=1,
        actor_id="actor-1",
        caller_role="user",
        scopes=("jobs:operate", "jobs:read"),
        target={"resource_id": "resource-1"},
        payload={"value": value},
        correlation_id="corr-1",
        expected_revision=7,
    )


def test_same_actor_key_and_payload_is_idempotent_but_different_payload_conflicts(
    tmp_path,
) -> None:
    store = CommandStore(tmp_path / "commands.sqlite3")
    try:
        first, created = _accept(store)
        replay, replay_created = _accept(store)
        assert created is True
        assert replay_created is False
        assert replay.command_id == first.command_id
        assert replay.expected_revision == 7

        with pytest.raises(IdempotencyConflict):
            _accept(store, value=2)

        other_actor_hash = store.request_hash(
            command_type="test.command.run",
            schema_version=1,
            target={},
            payload={"value": 2},
            correlation_id=None,
            expected_revision=None,
        )
        other_actor, other_created = store.accept(
            idempotency_key="request-key-001",
            request_hash=other_actor_hash,
            command_type="test.command.run",
            schema_version=1,
            actor_id="actor-2",
            caller_role="user",
            scopes=("jobs:operate",),
            target={},
            payload={"value": 2},
        )
        assert other_created is True
        assert other_actor.command_id != first.command_id
    finally:
        store.close()


def test_concurrent_idempotent_accept_returns_one_command(tmp_path) -> None:
    database = tmp_path / "commands.sqlite3"

    def submit() -> tuple[str, bool]:
        store = CommandStore(database)
        try:
            record, created = _accept(store)
            return record.command_id, created
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: submit(), range(16)))

    assert len({command_id for command_id, _created in results}) == 1
    assert sum(created for _command_id, created in results) == 1


def test_accept_and_transition_events_share_existing_sync_outbox(tmp_path) -> None:
    database = tmp_path / "commands.sqlite3"
    store = CommandStore(database)
    try:
        command, _ = _accept(store)
        claimed = store.claim(command.command_id)
        assert claimed is not None
        finished = store.finish(
            command.command_id,
            status=CommandStatus.SUCCEEDED,
            result={"receipt_id": "receipt-1"},
        )
        assert finished.result == {"receipt_id": "receipt-1"}
    finally:
        store.close()

    with sqlite3.connect(database) as db:
        transitions = db.execute(
            "SELECT to_status, event_id FROM api_command_transitions ORDER BY transition_id"
        ).fetchall()
        outbox = db.execute(
            "SELECT event_type, state, payload_json FROM sync_outbox ORDER BY outbox_id"
        ).fetchall()
    assert [status for status, _event_id in transitions] == [
        "accepted",
        "executing",
        "succeeded",
    ]
    assert [event_type for event_type, _state, _payload in outbox] == [
        "command.accepted",
        "command.executing",
        "command.succeeded",
    ]
    assert {state for _event_type, state, _payload in outbox} == {"held"}
    assert all("receipt-1" not in payload for _event_type, _state, payload in outbox)


def test_restart_recovers_accepted_but_fences_executing_as_unknown(tmp_path) -> None:
    database = tmp_path / "commands.sqlite3"
    store = CommandStore(database)
    accepted, _ = _accept(store, key="accepted-key-001")
    executing, _ = _accept(store, key="executing-key-01")
    assert store.claim(executing.command_id) is not None
    store.close()

    restarted = CommandStore(database)
    try:
        recovered = restarted.recover()
        assert recovered == (accepted.command_id,)
        uncertain = restarted.get(executing.command_id)
        assert uncertain.status is CommandStatus.DELIVERY_UNKNOWN
        assert uncertain.error_code == "process_restarted"
        assert restarted.recover() == (accepted.command_id,)
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_dispatcher_uses_task_manager_and_persists_success(tmp_path) -> None:
    store = CommandStore(tmp_path / "commands.sqlite3")
    registry = HandlerRegistry()
    executed = asyncio.Event()

    async def handler(command):
        executed.set()
        return CommandOutcome(
            status=CommandStatus.SUCCEEDED,
            result={"value": command.payload["value"]},
        )

    registry.register(
        "test.command.run",
        handler,
        required_scopes=frozenset({"jobs:operate"}),
    )
    manager = TaskManager()
    dispatcher = CommandDispatcher(store, registry=registry, task_manager=manager)
    try:
        command, _ = _accept(store)
        dispatcher.schedule(command.command_id)
        await asyncio.wait_for(executed.wait(), timeout=1)
        await manager.wait_all_tasks()
        finished = store.get(command.command_id)
        assert finished.status is CommandStatus.SUCCEEDED
        assert finished.attempt_count == 1
        assert manager.get_all_tasks()[0].metadata == {"command_id": command.command_id}
    finally:
        await dispatcher.close()
        store.close()


@pytest.mark.asyncio
async def test_cancellation_requires_allowlisted_cancellable_handler(tmp_path) -> None:
    store = CommandStore(tmp_path / "commands.sqlite3")
    registry = HandlerRegistry()
    started = asyncio.Event()

    async def cancellable_handler(_command):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    registry.register(
        "test.command.run",
        cancellable_handler,
        required_scopes=frozenset({"jobs:operate"}),
        cancellable=True,
    )
    dispatcher = CommandDispatcher(store, registry=registry, task_manager=TaskManager())
    try:
        command, _ = _accept(store)
        dispatcher.schedule(command.command_id)
        await asyncio.wait_for(started.wait(), timeout=1)
        running = store.get(command.command_id)
        requested = await dispatcher.cancel(running)
        assert requested.cancellation_requested is True
        for _ in range(100):
            terminal = store.get(command.command_id)
            if terminal.status is CommandStatus.CANCELLED:
                break
            await asyncio.sleep(0)
        assert terminal.status is CommandStatus.CANCELLED

        terminal_command, _ = _accept(store, key="terminal-key-001")
        assert store.cancel_before_start(terminal_command.command_id).status is CommandStatus.CANCELLED
        with pytest.raises(CommandNotCancellable):
            await dispatcher.cancel(store.get(terminal_command.command_id))
    finally:
        await dispatcher.close()
        store.close()
