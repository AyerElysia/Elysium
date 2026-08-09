"""Explicit command handler registry and managed asynchronous dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from src.kernel.concurrency import TaskManager

from .models import (
    CommandNotCancellable,
    CommandOutcome,
    CommandRecord,
    CommandStatus,
)
from .store import CommandStore

CommandHandler = Callable[[CommandRecord], Awaitable[CommandOutcome]]


@dataclass(frozen=True, slots=True)
class HandlerSpec:
    """One explicitly exported handler and its technical execution policy."""

    handler: CommandHandler
    required_scopes: frozenset[str]
    cancellable: bool = False
    timeout_seconds: float | None = None


class TaskOwner(Protocol):
    """TaskManager subset used by the dispatcher."""

    def create_task(
        self,
        coro,
        name: str | None = None,
        daemon: bool = False,
        timeout: float | None = None,
        group_name: str | None = None,
        metadata: dict | None = None,
    ): ...


class HandlerRegistry:
    """Allowlist registry; registration never infers handlers from input text."""

    def __init__(self) -> None:
        self._handlers: dict[str, HandlerSpec] = {}

    def register(
        self,
        command_type: str,
        handler: CommandHandler,
        *,
        required_scopes: frozenset[str],
        cancellable: bool = False,
        timeout_seconds: float | None = None,
    ) -> None:
        normalized = command_type.strip()
        if not normalized or normalized in self._handlers:
            raise ValueError(f"invalid or duplicate command handler: {command_type}")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("handler timeout must be positive")
        self._handlers[normalized] = HandlerSpec(
            handler=handler,
            required_scopes=required_scopes,
            cancellable=cancellable,
            timeout_seconds=timeout_seconds,
        )

    def get(self, command_type: str) -> HandlerSpec | None:
        return self._handlers.get(command_type)

    @property
    def command_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))


class CommandDispatcher:
    """Claim durable commands and execute them through the project TaskManager."""

    def __init__(
        self,
        store: CommandStore,
        *,
        registry: HandlerRegistry | None = None,
        task_manager: TaskOwner | None = None,
        max_concurrency: int = 8,
        max_backlog: int = 1000,
    ) -> None:
        if max_concurrency < 1 or max_backlog < 1:
            raise ValueError("command budgets must be positive")
        self.store = store
        self.registry = registry or HandlerRegistry()
        self.task_manager = task_manager or TaskManager()
        self._tasks: dict[str, object] = {}
        self._closing = False
        self._max_backlog = max_backlog
        self._execution_budget = asyncio.Semaphore(max_concurrency)

    @property
    def max_backlog(self) -> int:
        """Return the durable pending-command budget."""

        return self._max_backlog

    @property
    def has_active_tasks(self) -> bool:
        """Return whether command work still requires asynchronous shutdown."""

        return any(
            task_info.task is not None and not task_info.task.done()
            for task_info in self._tasks.values()
        )

    async def recover(self) -> None:
        """Fence uncertain executions and reschedule only accepted commands."""

        command_ids = await asyncio.to_thread(self.store.recover)
        for command_id in command_ids:
            self.schedule(command_id)

    def schedule(self, command_id: str) -> None:
        """Schedule one accepted command without creating an untracked task."""

        if self._closing or command_id in self._tasks:
            return
        if len(self._tasks) >= self._max_backlog:
            raise RuntimeError("command backlog limit reached")
        task_info = self.task_manager.create_task(
            self._execute(command_id),
            name=f"api-command:{command_id}",
            daemon=False,
            group_name="api-v1-commands",
            metadata={"command_id": command_id},
        )
        self._tasks[command_id] = task_info
        if task_info.task is not None:
            task_info.task.add_done_callback(
                lambda _task, current=command_id: self._tasks.pop(current, None)
            )

    async def cancel(self, command: CommandRecord) -> CommandRecord:
        """Cancel only commands whose registered handler allows propagation."""

        spec = self.registry.get(command.command_type)
        if spec is None or not spec.cancellable:
            raise CommandNotCancellable(command.command_id)
        if command.status is CommandStatus.ACCEPTED:
            return await asyncio.to_thread(
                self.store.cancel_before_start,
                command.command_id,
            )
        if command.status is not CommandStatus.EXECUTING:
            raise CommandNotCancellable(command.command_id)
        task_info = self._tasks.get(command.command_id)
        if task_info is None:
            raise CommandNotCancellable(command.command_id)
        await asyncio.to_thread(
            self.store.request_running_cancellation,
            command.command_id,
        )
        if not task_info.cancel_threadsafe():
            raise CommandNotCancellable(command.command_id)
        return await asyncio.to_thread(self.store.get, command.command_id)

    async def close(self) -> None:
        """Stop accepting work and wait for owned command tasks to finish."""

        self._closing = True
        tasks = [
            task_info.task
            for task_info in tuple(self._tasks.values())
            if task_info.task is not None and not task_info.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _execute(self, command_id: str) -> None:
        async with self._execution_budget:
            await self._execute_bounded(command_id)

    async def _execute_bounded(self, command_id: str) -> None:
        command = await asyncio.to_thread(self.store.get, command_id)
        spec = self.registry.get(command.command_type)
        if spec is None or not spec.required_scopes.issubset(command.scope_snapshot):
            await asyncio.to_thread(self.store.reject_unhandled, command_id)
            return
        try:
            command = await asyncio.to_thread(self.store.claim, command_id)
            if command is None:
                return
            task_info = self._tasks.get(command_id)
            if task_info is not None:
                await asyncio.to_thread(
                    self.store.bind_task,
                    command_id,
                    task_info.task_id,
                )
            execution = spec.handler(command)
            if spec.timeout_seconds is not None:
                outcome = await asyncio.wait_for(execution, timeout=spec.timeout_seconds)
            else:
                outcome = await execution
        except asyncio.CancelledError:
            if spec.cancellable:
                await asyncio.to_thread(self.store.mark_cancelled, command_id)
            else:
                await asyncio.to_thread(
                    self.store.finish,
                    command_id,
                    status=CommandStatus.DELIVERY_UNKNOWN,
                    error_code="execution_interrupted",
                    safe_error_detail="执行被中断，无法确认外部副作用结果。",
                )
            raise
        except TimeoutError:
            await asyncio.to_thread(
                self.store.finish,
                command_id,
                status=CommandStatus.DELIVERY_UNKNOWN,
                error_code="execution_timeout",
                safe_error_detail="执行超时，无法确认外部副作用结果。",
            )
        except Exception:  # noqa: BLE001 - public handlers need a safe failure boundary
            await asyncio.to_thread(
                self.store.finish,
                command_id,
                status=CommandStatus.FAILED,
                error_code="handler_failed",
                safe_error_detail="命令执行失败。",
            )
        else:
            await asyncio.to_thread(
                self.store.finish,
                command_id,
                status=outcome.status,
                result=outcome.result,
                error_code=outcome.error_code,
                safe_error_detail=outcome.safe_error_detail,
            )


__all__ = [
    "CommandDispatcher",
    "CommandHandler",
    "HandlerRegistry",
    "HandlerSpec",
]
