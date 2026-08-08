from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import InvalidRequestError
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


class _CommittedTransaction:
    """模拟底层事务已结束：commit 抛原始异常，rollback 报 committed state。

    真实场景：``AsyncSessionTransaction.commit()`` 到达驱动后抛异常（如连接
    丢失、提交响应丢失），底层事务实际已提交；随后 close() 里的 rollback()
    对已结束事务再次 rollback 会抛 ``InvalidRequestError``。
    """

    async def commit(self) -> None:
        raise RuntimeError("boom-commit")

    async def rollback(self) -> None:
        raise InvalidRequestError(
            "This session is in 'committed' state; no further SQL "
            "can be emitted within this transaction."
        )


async def test_rollback_tolerates_finished_transaction_and_keeps_original_error() -> None:
    """commit() 抛异常且底层事务已提交时，close 的 rollback 不得二次异常掩盖原始异常。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    uow = AsyncUnitOfWork(sessions)
    await uow.__aenter__()
    uow._transaction = _CommittedTransaction()  # 模拟底层事务已提交
    try:
        with pytest.raises(RuntimeError, match="boom-commit"):
            await uow.__aexit__(None, None, None)
    finally:
        await engine.dispose()
    assert uow.state == "closed"


async def test_close_failure_does_not_mask_body_exception() -> None:
    """close()（scope 退出）失败时不得覆盖事务体抛出的原始异常。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def exploding_scope():
        yield
        raise RuntimeError("scope-exit-boom")

    try:
        with pytest.raises(RuntimeError, match="body-boom"):
            async with AsyncUnitOfWork(sessions, scope_factory=exploding_scope):
                raise RuntimeError("body-boom")
    finally:
        await engine.dispose()
