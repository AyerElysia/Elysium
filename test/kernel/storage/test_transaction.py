from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.kernel.storage.transaction import AfterCommitError, AsyncUnitOfWork


async def test_unit_of_work_commit_rollback_and_fence_scope() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    events: list[str] = []

    @asynccontextmanager
    async def scope():
        events.append("scope-enter")
        try:
            yield
        finally:
            events.append("scope-exit")

    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE items (value INTEGER NOT NULL)"))

    async with AsyncUnitOfWork(
        sessions,
        before_commit=lambda _session: _record(events, "before-commit"),
        scope_factory=scope,
    ) as uow:
        await uow.session.execute(text("INSERT INTO items VALUES (1)"))
        uow.add_after_commit(lambda: events.append("after-commit"))

    with pytest.raises(RuntimeError):
        async with AsyncUnitOfWork(sessions) as uow:
            await uow.session.execute(text("INSERT INTO items VALUES (2)"))
            raise RuntimeError("rollback")

    async with engine.connect() as connection:
        values = list((await connection.execute(text("SELECT value FROM items"))).scalars())
    await engine.dispose()

    assert values == [1]
    assert events == [
        "scope-enter",
        "before-commit",
        "after-commit",
        "scope-exit",
    ]


async def _record(events: list[str], value: str) -> None:
    events.append(value)


async def test_after_commit_failure_is_not_reported_as_rollback() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE items (value INTEGER NOT NULL)"))

    def fail_after_commit() -> None:
        raise ValueError("projection unavailable")

    with pytest.raises(AfterCommitError) as captured:
        async with AsyncUnitOfWork(sessions) as uow:
            await uow.session.execute(text("INSERT INTO items VALUES (7)"))
            uow.add_after_commit(fail_after_commit)

    async with engine.connect() as connection:
        value = await connection.scalar(text("SELECT value FROM items"))
    await engine.dispose()

    assert value == 7
    assert isinstance(captured.value.failures[0], ValueError)
