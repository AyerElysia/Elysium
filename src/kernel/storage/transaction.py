"""Explicit async unit-of-work lifecycle for durable domain operations."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from enum import StrEnum
from types import TracebackType
from typing import Any, Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

AfterCommit = Callable[[], Awaitable[None] | None]
BeforeCommit = Callable[[AsyncSession], Awaitable[None]]
ScopeFactory = Callable[[], AbstractAsyncContextManager[None]]


class UnitOfWorkState(StrEnum):
    """Observable lifecycle states for one unit of work."""

    NEW = "new"
    ACTIVE = "active"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    CLOSED = "closed"


class AfterCommitError(RuntimeError):
    """Raised after durable commit when a caller-owned follow-up fails."""

    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = tuple(failures)
        super().__init__(
            f"durable commit succeeded but {len(failures)} after-commit callback(s) failed"
        )


class AsyncUnitOfWork:
    """Own one SQLAlchemy session, transaction, and after-commit callback set.

    Repositories receive :attr:`session` from this object and must never commit
    independently.  Callback failure is reported as a committed-side-effect
    failure; it is never misrepresented as a rolled-back database transaction.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        before_commit: BeforeCommit | None = None,
        scope_factory: ScopeFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._before_commit = before_commit
        self._scope_factory = scope_factory
        self._session: AsyncSession | None = None
        self._transaction: Any = None
        self._scope: AbstractAsyncContextManager[None] | None = None
        self._after_commit: list[AfterCommit] = []
        self.state = UnitOfWorkState.NEW

    @property
    def session(self) -> AsyncSession:
        """Return the active session or reject use outside the UoW lifetime."""

        if self._session is None or self.state != UnitOfWorkState.ACTIVE:
            raise RuntimeError("unit of work has no active session")
        return self._session

    async def __aenter__(self) -> Self:
        if self.state != UnitOfWorkState.NEW:
            raise RuntimeError(f"unit of work cannot enter from state {self.state}")
        scope = self._scope_factory() if self._scope_factory is not None else nullcontext()
        self._scope = scope
        await scope.__aenter__()
        try:
            self._session = self._session_factory()
            self._transaction = await self._session.begin()
            self.state = UnitOfWorkState.ACTIVE
            return self
        except BaseException:
            await scope.__aexit__(None, None, None)
            self._scope = None
            raise

    def add_after_commit(self, callback: AfterCommit) -> None:
        """Register a callback that runs only after the database commit succeeds."""

        if self.state != UnitOfWorkState.ACTIVE:
            raise RuntimeError("after-commit callback requires an active unit of work")
        self._after_commit.append(callback)

    async def commit(self) -> None:
        """Commit exactly once, then execute all registered callbacks."""

        if self.state != UnitOfWorkState.ACTIVE or self._transaction is None:
            raise RuntimeError("unit of work is not active")
        if self._before_commit is not None:
            await self._before_commit(self.session)
        await self._transaction.commit()
        self.state = UnitOfWorkState.COMMITTED
        failures: list[BaseException] = []
        for callback in self._after_commit:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:  # noqa: BLE001 - aggregate all callbacks
                failures.append(exc)
        if failures:
            raise AfterCommitError(failures)

    async def rollback(self) -> None:
        """Roll back one active transaction; repeated rollback is harmless."""

        if self.state == UnitOfWorkState.ACTIVE and self._transaction is not None:
            await self._transaction.rollback()
            self.state = UnitOfWorkState.ROLLED_BACK

    async def close(self) -> None:
        """Close only resources owned by this unit of work."""

        if self.state == UnitOfWorkState.ACTIVE:
            await self.rollback()
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._transaction = None
        if self._scope is not None:
            await self._scope.__aexit__(None, None, None)
            self._scope = None
        self.state = UnitOfWorkState.CLOSED

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, traceback
        try:
            if exc is None:
                await self.commit()
            else:
                await self.rollback()
        finally:
            await self.close()
        return False


__all__ = [
    "AfterCommit",
    "AfterCommitError",
    "AsyncUnitOfWork",
    "BeforeCommit",
    "ScopeFactory",
    "UnitOfWorkState",
]
