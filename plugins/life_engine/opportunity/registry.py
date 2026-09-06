"""Explicit install/enable gates for opportunity capability executors."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .catalog import CapabilityCatalog, CapabilityDescriptor


class CapabilityRuntimeError(RuntimeError):
    """Base class for capability runtime gate failures."""


class CapabilityFactoryConflict(CapabilityRuntimeError):
    """One capability id was bound to two different factories."""


class CapabilityFactoryMissing(CapabilityRuntimeError):
    """A catalogued capability has no injected executor factory."""


class CapabilityExecutorContractError(CapabilityRuntimeError):
    """A factory or dispatcher violated the asynchronous runtime contract."""


class CapabilityNotInstalled(CapabilityRuntimeError):
    """Execution was requested before installation or after uninstall."""


class CapabilityDisabled(CapabilityRuntimeError):
    """Execution was requested while a capability was disabled."""


class CapabilityOperationDenied(CapabilityRuntimeError):
    """An operation is absent from the immutable package manifest."""


class CapabilityRegistryClosed(CapabilityRuntimeError):
    """The registry has entered permanent shutdown admission."""


class CapabilityQuiescenceInProgress(CapabilityRuntimeError):
    """A capability still owns calls from an earlier stop attempt."""


class CapabilityQuiescenceTimeout(CapabilityRuntimeError):
    """A capability ignored cancellation beyond the bounded stop window."""

    def __init__(self, capability_id: str, in_flight: int) -> None:
        self.capability_id = capability_id
        self.in_flight = in_flight
        super().__init__(
            f"capability did not quiesce after cancellation: {capability_id} "
            f"in_flight={in_flight}"
        )


@runtime_checkable
class CapabilityExecutor(Protocol):
    """A thin domain executor supplied by the owning service."""

    async def execute(
        self,
        operation_id: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        """Execute one manifest-declared operation."""


@dataclass(frozen=True, slots=True)
class CapabilityInvocation:
    """One immutable request passed through the trusted permission dispatcher."""

    capability_id: str
    operation_id: str
    arguments: Mapping[str, Any]
    caller_context: Any


CapabilityExecutorFactory = Callable[
    [CapabilityDescriptor], CapabilityExecutor | Awaitable[CapabilityExecutor]
]
CapabilityDispatch = Callable[
    [CapabilityExecutor, CapabilityInvocation], Awaitable[Any]
]


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeState:
    """Content-free state suitable for health and management surfaces."""

    capability_id: str
    package_sha256: str
    installed: bool
    enabled: bool
    operation_count: int
    in_flight: int = 0
    completion_exempt_in_flight: int = 0
    quiescence_error: str = ""


@dataclass(slots=True)
class _InstalledCapability:
    descriptor: CapabilityDescriptor
    executor: CapabilityExecutor
    enabled: bool = False
    dispatch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    completion_exempt_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    quiescence_error: str = ""


def _is_async_callable(callback: Any) -> bool:
    if not callable(callback):
        return False
    return bool(
        inspect.iscoroutinefunction(callback)
        or inspect.iscoroutinefunction(type(callback).__call__)
    )


class CapabilityRuntimeRegistry:
    """Manage executors without owning shared storage or caller permissions.

    Factories receive only the descriptor. The Life Engine owner injects any
    already-open shared runtime or domain ports into factory closures. Every
    invocation goes through one trusted dispatcher supplied at construction,
    so manifest declaration never bypasses the caller consciousness' current
    tool permissions. Skill text is data and is never evaluated by this class.
    """

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        dispatch: CapabilityDispatch,
        factories: Mapping[str, CapabilityExecutorFactory] | None = None,
        quiescence_timeout_seconds: float = 5.0,
    ) -> None:
        if not callable(dispatch) or not _is_async_callable(dispatch):
            raise TypeError("capability dispatch must be an async callable")
        self._catalog = catalog
        self._dispatch = dispatch
        self._factories: dict[str, CapabilityExecutorFactory] = {}
        self._installed: dict[str, _InstalledCapability] = {}
        self._state_lock = asyncio.Lock()
        self._install_lock = asyncio.Lock()
        timeout = float(quiescence_timeout_seconds)
        if not 0.001 <= timeout <= 60.0:
            raise ValueError("quiescence_timeout_seconds must be between 0.001 and 60")
        self._quiescence_timeout_seconds = timeout
        self._closed = False
        self._close_complete = False
        if factories:
            for capability_id, factory in factories.items():
                self._catalog.require(capability_id)
                if not callable(factory):
                    raise TypeError("capability executor factory must be callable")
                self._factories[capability_id] = factory

    async def register_factory(
        self,
        capability_id: str,
        factory: CapabilityExecutorFactory,
    ) -> None:
        """Inject a factory; registering it does not install the capability."""

        self._catalog.require(capability_id)
        if not callable(factory):
            raise TypeError("capability executor factory must be callable")
        async with self._install_lock, self._state_lock:
            self._require_admission_open()
            existing = self._factories.get(capability_id)
            if existing is None:
                self._factories[capability_id] = factory
                return
            if existing is not factory:
                raise CapabilityFactoryConflict(
                    f"capability factory already registered: {capability_id}"
                )

    async def unregister_factory(self, capability_id: str) -> bool:
        """Remove an unused factory; installed capabilities fail closed."""

        self._catalog.require(capability_id)
        async with self._install_lock, self._state_lock:
            if capability_id in self._installed:
                raise CapabilityRuntimeError(
                    "cannot remove a factory while its capability is installed"
                )
            return self._factories.pop(capability_id, None) is not None

    async def install(self, capability_id: str) -> CapabilityRuntimeState:
        """Create an executor in the disabled state.

        Installation is explicit and idempotent. Enabling always requires a
        separate call, so discovery and process startup cannot silently grant
        an operation surface.
        """

        descriptor = self._catalog.require(capability_id)
        async with self._install_lock:
            async with self._state_lock:
                self._require_admission_open()
                existing = self._installed.get(capability_id)
                if existing is not None:
                    return self._state_for(existing)
                factory = self._factories.get(capability_id)
            if factory is None:
                raise CapabilityFactoryMissing(
                    f"capability has no injected factory: {capability_id}"
                )
            executor_or_awaitable = factory(descriptor)
            if inspect.isawaitable(executor_or_awaitable):
                executor = await executor_or_awaitable
            else:
                executor = executor_or_awaitable
            execute = getattr(executor, "execute", None)
            if not callable(execute) or not inspect.iscoroutinefunction(execute):
                raise CapabilityExecutorContractError(
                    "factory must return an executor with async execute"
                )
            installed = _InstalledCapability(
                descriptor=descriptor,
                executor=executor,
            )
            async with self._state_lock:
                self._installed[capability_id] = installed
            return self._state_for(installed)

    async def enable(self, capability_id: str) -> CapabilityRuntimeState:
        entry = await self._require_installed(capability_id)
        async with entry.lifecycle_lock, self._state_lock:
            self._require_admission_open()
            self._require_current(capability_id, entry)
            if entry.active_tasks:
                raise CapabilityQuiescenceInProgress(
                    f"capability still has in-flight calls: {capability_id}"
                )
            entry.quiescence_error = ""
            entry.enabled = True
            return self._state_for(entry)

    async def disable(self, capability_id: str) -> CapabilityRuntimeState:
        entry = await self._require_installed(capability_id)
        async with entry.lifecycle_lock:
            async with self._state_lock:
                self._require_current(capability_id, entry)
            await self._quiesce(capability_id, entry)
            return self._state_for(entry)

    async def uninstall(self, capability_id: str) -> bool:
        """Remove an executor without closing dependencies owned elsewhere."""

        self._catalog.require(capability_id)
        async with self._install_lock:
            async with self._state_lock:
                entry = self._installed.get(capability_id)
            if entry is None:
                return False
            async with entry.lifecycle_lock:
                async with self._state_lock:
                    if self._installed.get(capability_id) is not entry:
                        return False
                await self._quiesce(capability_id, entry)
                async with self._state_lock:
                    if self._installed.get(capability_id) is not entry:
                        return False
                    del self._installed[capability_id]
                return True

    async def execute(
        self,
        capability_id: str,
        operation_id: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        caller_context: Any,
    ) -> Any:
        """Dispatch an installed, enabled, manifest-declared operation.

        The dispatcher is mandatory and fixed at construction. Callers and
        workflow documents cannot supply a replacement or add permissions.
        """

        operation = str(operation_id or "")
        if not isinstance(arguments, Mapping) and arguments is not None:
            raise TypeError("capability operation arguments must be a mapping")
        task = asyncio.current_task()
        if task is None:
            raise CapabilityRuntimeError(
                "capability execution requires an asyncio task"
            )
        entry = await self._require_installed(capability_id)
        invocation: CapabilityInvocation
        async with self._state_lock:
            self._require_admission_open()
            self._require_current(capability_id, entry)
            if not entry.enabled:
                raise CapabilityDisabled(f"capability is disabled: {capability_id}")
            if not entry.descriptor.declares_operation(operation):
                raise CapabilityOperationDenied(
                    "operation is not declared by the capability manifest: "
                    f"{capability_id}/{operation}"
                )
            if task in entry.active_tasks:
                raise CapabilityRuntimeError(
                    f"reentrant capability execution is not allowed: {capability_id}"
                )
            entry.active_tasks.add(task)
            executor = entry.executor
            invocation = CapabilityInvocation(
                capability_id=capability_id,
                operation_id=operation,
                arguments=MappingProxyType(dict(arguments or {})),
                caller_context=caller_context,
            )
        try:
            # Preserve one atomic operation at a time per capability, but do not
            # make lifecycle calls wait for this lock.  Quiescence cancels both
            # the current owner and admitted waiters by their exact task ids.
            async with entry.dispatch_lock:
                async with self._state_lock:
                    self._require_current(capability_id, entry)
                    if not entry.enabled:
                        raise CapabilityDisabled(
                            f"capability is disabled: {capability_id}"
                        )
                dispatched = self._dispatch(executor, invocation)
                if not inspect.isawaitable(dispatched):
                    raise CapabilityExecutorContractError(
                        "capability dispatch must return an awaitable"
                    )
                return await dispatched
        finally:
            # Synchronous cleanup is cancellation-safe and touches only the
            # exact entry/task admitted above.
            entry.active_tasks.discard(task)
            entry.completion_exempt_tasks.discard(task)

    async def state(self, capability_id: str) -> CapabilityRuntimeState:
        descriptor = self._catalog.require(capability_id)
        async with self._state_lock:
            entry = self._installed.get(capability_id)
            if entry is None:
                return CapabilityRuntimeState(
                    capability_id=capability_id,
                    package_sha256=descriptor.package_sha256,
                    installed=False,
                    enabled=False,
                    operation_count=len(descriptor.operations),
                )
            return self._state_for(entry)

    async def list_states(self) -> tuple[CapabilityRuntimeState, ...]:
        states: list[CapabilityRuntimeState] = []
        for descriptor in self._catalog.list_descriptors():
            states.append(await self.state(descriptor.capability_id))
        return tuple(states)

    async def health_snapshot(self) -> dict[str, Any]:
        """Return content-free catalog and gate state, never manual/Skill text."""

        states = await self.list_states()
        installed = sum(int(state.installed) for state in states)
        enabled = sum(int(state.enabled) for state in states)
        in_flight = sum(state.in_flight for state in states)
        degraded = any(state.quiescence_error for state in states)
        if self._close_complete:
            status = "closed"
        elif degraded:
            status = "degraded"
        elif self._closed:
            status = "closing"
        else:
            status = "ready" if states else "empty"
        return {
            "component": "opportunity_capability_registry",
            "status": status,
            "catalogued": len(states),
            "installed": installed,
            "enabled": enabled,
            "in_flight": in_flight,
            "capabilities": [
                {
                    "capability_id": state.capability_id,
                    "package_sha256": state.package_sha256,
                    "installed": state.installed,
                    "enabled": state.enabled,
                    "operation_count": state.operation_count,
                    "in_flight": state.in_flight,
                    "completion_exempt_in_flight": (state.completion_exempt_in_flight),
                    "quiescence_error": state.quiescence_error,
                }
                for state in states
            ],
        }

    async def close(self) -> None:
        """Permanently stop admission and remove only fully quiesced entries.

        Executors and injected storage remain owned by the service.  A task
        which ignores cancellation keeps its entry installed and makes close
        fail explicitly; a later idempotent close may finish after it exits.
        """

        if self._close_complete:
            return
        async with self._install_lock:
            self._closed = True
            async with self._state_lock:
                entries = tuple(self._installed.items())
            failures: list[Exception] = []
            for capability_id, entry in entries:
                async with entry.lifecycle_lock:
                    async with self._state_lock:
                        if self._installed.get(capability_id) is not entry:
                            continue
                    try:
                        await self._quiesce(capability_id, entry)
                    except CapabilityQuiescenceTimeout as exc:
                        failures.append(exc)
                        continue
                    async with self._state_lock:
                        if self._installed.get(capability_id) is entry:
                            del self._installed[capability_id]
            if failures:
                raise ExceptionGroup(
                    "capability registry shutdown did not quiesce", failures
                )
            self._close_complete = True

    async def _quiesce(
        self,
        capability_id: str,
        entry: _InstalledCapability,
    ) -> None:
        current = asyncio.current_task()
        async with self._state_lock:
            self._require_current(capability_id, entry)
            entry.enabled = False
            targets = tuple(
                task
                for task in entry.active_tasks
                if not task.done() and task is not current
            )
            if current is not None and current in entry.active_tasks:
                # A self-management operation may pause/uninstall its own
                # capability. It is the decision being committed, not stale
                # background work, and is explicitly allowed to return.
                entry.completion_exempt_tasks.add(current)
        for task in targets:
            task.cancel(f"capability quiescing: {capability_id}")
        if not targets:
            entry.quiescence_error = ""
            return
        try:
            _done, pending = await asyncio.wait(
                targets,
                timeout=self._quiescence_timeout_seconds,
            )
        except asyncio.CancelledError:
            entry.quiescence_error = "CapabilityQuiescenceCancelled"
            raise
        if pending:
            entry.quiescence_error = "CapabilityQuiescenceTimeout"
            raise CapabilityQuiescenceTimeout(capability_id, len(pending))
        entry.quiescence_error = ""

    async def _require_installed(self, capability_id: str) -> _InstalledCapability:
        self._catalog.require(capability_id)
        async with self._state_lock:
            entry = self._installed.get(capability_id)
        if entry is None:
            raise CapabilityNotInstalled(
                f"capability is not installed: {capability_id}"
            )
        return entry

    def _require_admission_open(self) -> None:
        if self._closed:
            raise CapabilityRegistryClosed("capability registry is closed")

    def _require_current(
        self,
        capability_id: str,
        entry: _InstalledCapability,
    ) -> None:
        if self._installed.get(capability_id) is not entry:
            raise CapabilityNotInstalled(
                f"capability is not installed: {capability_id}"
            )

    @staticmethod
    def _state_for(entry: _InstalledCapability) -> CapabilityRuntimeState:
        descriptor = entry.descriptor
        return CapabilityRuntimeState(
            capability_id=descriptor.capability_id,
            package_sha256=descriptor.package_sha256,
            installed=True,
            enabled=entry.enabled,
            operation_count=len(descriptor.operations),
            in_flight=sum(not task.done() for task in entry.active_tasks),
            completion_exempt_in_flight=sum(
                not task.done() for task in entry.completion_exempt_tasks
            ),
            quiescence_error=entry.quiescence_error,
        )
