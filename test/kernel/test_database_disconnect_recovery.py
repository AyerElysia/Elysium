"""Contracts for transient database disconnect recovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.managers.stream_manager import StreamManager
from src.kernel.db import DatabaseTransactionError, is_database_disconnect
from src.kernel.db.core import engine as engine_module


class _MySQLDisconnect(Exception):
    """Small DBAPI-shaped disconnect used without a live database."""


def _wrapped_disconnect() -> DatabaseTransactionError:
    try:
        raise _MySQLDisconnect(2013, "Lost connection to MySQL server during query")
    except _MySQLDisconnect as exc:
        wrapped = DatabaseTransactionError("database transaction failed")
        wrapped.__cause__ = exc
        return wrapped


def test_disconnect_classifier_follows_wrapped_causes() -> None:
    assert is_database_disconnect(_wrapped_disconnect()) is True
    assert is_database_disconnect(DatabaseTransactionError("lock timeout")) is False


def test_session_optimization_disconnect_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners: list[Any] = []

    def _listens_for(_target: Any, event_name: str) -> Any:
        assert event_name == "connect"

        def _capture(listener: Any) -> Any:
            listeners.append(listener)
            return listener

        return _capture

    monkeypatch.setattr(engine_module.event, "listens_for", _listens_for)
    engine_module._install_session_optimizations(
        SimpleNamespace(sync_engine=object()),
        ("SET SESSION wait_timeout = 180",),
        "MySQL",
    )

    class _Cursor:
        closed = False

        def execute(self, _statement: str) -> None:
            raise _MySQLDisconnect(
                2013,
                "Lost connection to MySQL server during query",
            )

        def close(self) -> None:
            self.closed = True

    cursor = _Cursor()
    connection = SimpleNamespace(cursor=lambda: cursor)

    with pytest.raises(_MySQLDisconnect):
        listeners[0](connection, object())
    assert cursor.closed is True


class _FlakyMessageCRUD:
    def __init__(self, *, permanent: bool = False) -> None:
        self.get_calls = 0
        self.create_calls = 0
        self.permanent = permanent
        self.persisted = SimpleNamespace(id=7)

    async def get_by(self, **_filters: Any) -> Any:
        self.get_calls += 1
        if self.get_calls == 1:
            if self.permanent:
                raise DatabaseTransactionError("integrity failure")
            raise _wrapped_disconnect()
        return self.persisted

    async def create(self, _message_data: dict[str, Any]) -> Any:
        self.create_calls += 1
        return self.persisted


def _message() -> Any:
    return SimpleNamespace(
        message_id="message-1",
        stream_id="stream-1",
        person_id="person-1",
        time=1.0,
        message_type=SimpleNamespace(value="text"),
        content="private body must not enter retry logs",
        processed_plain_text="private body must not enter retry logs",
        reply_to=None,
        platform="kook",
    )


def _manager(crud: _FlakyMessageCRUD) -> StreamManager:
    manager = StreamManager()
    manager._messages_crud = crud  # type: ignore[assignment]
    manager._resolve_person_id_from_message = lambda _message: "person-1"  # type: ignore[method-assign]
    manager._update_stream_active_time = AsyncMock()  # type: ignore[method-assign]
    return manager


async def test_message_persistence_retries_proven_disconnect_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crud = _FlakyMessageCRUD()
    manager = _manager(crud)
    sleep = AsyncMock()
    warnings: list[str] = []
    monkeypatch.setattr("src.core.managers.stream_manager.asyncio.sleep", sleep)
    monkeypatch.setattr(
        "src.core.managers.stream_manager.logger.warning",
        warnings.append,
    )

    result = await manager.add_message(_message(), add_to_unread=False)

    assert result is crud.persisted
    assert crud.get_calls == 2
    assert crud.create_calls == 0
    manager._update_stream_active_time.assert_awaited_once_with("stream-1")  # type: ignore[attr-defined]
    sleep.assert_awaited_once()
    assert len(warnings) == 1
    assert "private body" not in warnings[0]


async def test_message_persistence_recovers_unknown_create_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted = SimpleNamespace(id=8)

    class _UnknownCreateOutcomeCRUD:
        get_calls = 0
        create_calls = 0

        async def get_by(self, **_filters: Any) -> Any:
            self.get_calls += 1
            return None if self.get_calls == 1 else persisted

        async def create(self, _message_data: dict[str, Any]) -> Any:
            self.create_calls += 1
            raise _wrapped_disconnect()

    crud = _UnknownCreateOutcomeCRUD()
    manager = _manager(crud)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "src.core.managers.stream_manager.asyncio.sleep",
        AsyncMock(),
    )

    result = await manager.add_message(_message(), add_to_unread=False)

    assert result is persisted
    assert crud.get_calls == 2
    assert crud.create_calls == 1


async def test_retry_updates_unread_only_after_durable_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crud = _FlakyMessageCRUD()
    crud.get_calls = 1
    manager = _manager(crud)
    manager._update_stream_active_time = AsyncMock(  # type: ignore[method-assign]
        side_effect=[_wrapped_disconnect(), None]
    )
    context = SimpleNamespace(add_unread_message=Mock())
    chat_stream = SimpleNamespace(context=context, update_active_time=Mock())
    manager._streams["stream-1"] = chat_stream
    monkeypatch.setattr(
        "src.core.managers.stream_manager.asyncio.sleep",
        AsyncMock(),
    )

    await manager.add_message(_message(), add_to_unread=True)

    assert manager._update_stream_active_time.await_count == 2  # type: ignore[attr-defined]
    context.add_unread_message.assert_called_once()
    chat_stream.update_active_time.assert_called_once()


async def test_message_persistence_does_not_retry_other_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crud = _FlakyMessageCRUD(permanent=True)
    manager = _manager(crud)
    sleep = AsyncMock()
    monkeypatch.setattr("src.core.managers.stream_manager.asyncio.sleep", sleep)

    with pytest.raises(DatabaseTransactionError, match="integrity failure"):
        await manager.add_message(_message(), add_to_unread=False)

    assert crud.get_calls == 1
    sleep.assert_not_awaited()
