"""Structured task-group context for manager-tracked asyncio tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from .exceptions import TaskGroupError
from .task_info import TaskInfo

TaskFactory = Callable[
    [Coroutine[Any, Any, Any], str | None, str],
    TaskInfo,
]


@dataclass
class TaskGroup:
    """Own a set of child tasks and enforce structured completion.

    Child failures are always observed.  With ``cancel_on_error`` enabled, the
    first failed child promptly cancels its siblings.  A group timeout cancels
    unfinished children and raises :class:`asyncio.TimeoutError` instead of
    silently continuing with partial work.
    """

    name: str
    timeout: float | None = None
    cancel_on_error: bool = True
    tasks: list[TaskInfo] = field(default_factory=list)
    _owner_task: asyncio.Task[Any] | None = None
    _exception: BaseException | None = None
    _active: bool = False
    _task_factory: TaskFactory | None = field(default=None, repr=False)

    def create_task(
        self,
        coro: Coroutine[Any, Any, Any],
        name: str | None = None,
    ) -> TaskInfo:
        """Create one non-daemon child inside the active group.

        Args:
            coro: Coroutine to execute.
            name: Optional diagnostic task name.

        Returns:
            TaskInfo: Metadata for the tracked child.

        Raises:
            TaskGroupError: If the group is not active.
        """
        if not self._active:
            coro.close()
            raise TaskGroupError(
                f"TaskGroup '{self.name}' is not active. "
                "Use 'async with' to activate the group."
            )

        if self._task_factory is not None:
            task_info = self._task_factory(coro, name, self.name)
            task = task_info.task
            assert task is not None
        else:
            loop = asyncio.get_running_loop()
            task_info = TaskInfo(
                name=name,
                coro=coro,
                daemon=False,
                group_name=self.name,
                loop=loop,
            )
            task = loop.create_task(coro, name=name)
            task_info.task = task

        self.tasks.append(task_info)
        task.add_done_callback(self._on_child_done)
        return task_info

    async def __aenter__(self) -> "TaskGroup":
        """Activate a fresh task scope."""
        if self._active:
            raise TaskGroupError(f"TaskGroup '{self.name}' is already active")
        self.tasks.clear()
        self._active = True
        self._owner_task = asyncio.current_task()
        self._exception = None
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        """Wait for children and propagate context or child failures."""
        try:
            if exc_val is not None:
                await self._cancel_all_tasks()
                return False

            await self._wait_all_tasks()
            if self._exception is not None:
                raise self._exception
            return False
        finally:
            self._active = False
            self._owner_task = None

    def _on_child_done(self, task: asyncio.Task[Any]) -> None:
        """Observe a child result and trigger fail-fast cancellation."""
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            self._record_exception(exception)

    async def _wait_all_tasks(self) -> None:
        """Wait for all children, enforcing the configured group timeout."""
        live = [
            info.task
            for info in self.tasks
            if info.task is not None and not info.task.done()
        ]
        if live:
            try:
                _done, pending = await asyncio.wait(
                    live,
                    timeout=self.timeout,
                    return_when=asyncio.ALL_COMPLETED,
                )
            except asyncio.CancelledError:
                await self._cancel_all_tasks()
                raise

            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise asyncio.TimeoutError(
                    f"TaskGroup '{self.name}' timed out after {self.timeout}s"
                )

        # Done callbacks normally record failures first.  Inspecting here as
        # well makes the contract deterministic even when a custom task
        # implementation delays callback delivery.
        for info in self.tasks:
            task = info.task
            if task is None or not task.done() or task.cancelled():
                continue
            exception = task.exception()
            if exception is not None:
                self._record_exception(exception)

    async def _cancel_all_tasks(self) -> None:
        """Cancel unfinished children and wait for cancellation cleanup."""
        pending: list[asyncio.Task[Any]] = []
        for task_info in self.tasks:
            task = task_info.task
            if task is not None and not task.done():
                task.cancel()
                pending.append(task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _record_exception(self, exc: BaseException) -> None:
        """Record the first child error and optionally cancel siblings."""
        if self._exception is None:
            self._exception = exc
        if not self._active or not self.cancel_on_error:
            return
        current = asyncio.current_task()
        for info in self.tasks:
            task = info.task
            if task is not None and task is not current and not task.done():
                task.cancel()

    def is_active(self) -> bool:
        """Return whether the group currently accepts children."""
        return self._active

    def get_task_count(self) -> int:
        """Return the number of children in the current scope."""
        return len(self.tasks)

    def get_active_task_count(self) -> int:
        """Return the number of unfinished children."""
        return sum(1 for info in self.tasks if not info.is_done())

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        status = "active" if self._active else "inactive"
        return (
            f"TaskGroup(name={self.name}, status={status}, "
            f"tasks={self.get_active_task_count()}/{len(self.tasks)})"
        )
