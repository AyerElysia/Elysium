"""Thread-safe on-demand residency for heavyweight Voice Live resources."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Generic, Literal, TypeVar

T = TypeVar("T")
ResidencyState = Literal["unloaded", "loading", "loaded", "unloading", "failed"]


@dataclass(frozen=True, slots=True)
class ResidencySnapshot:
    """Content-free lifecycle telemetry for one managed resource."""

    state: ResidencyState
    lease_count: int
    generation: int
    load_count: int
    unload_count: int
    last_load_ms: float
    last_unload_ms: float
    last_error: str

    def as_dict(self) -> dict[str, str | int | float]:
        """Return a JSON-safe health projection."""

        return asdict(self)


class ResourceLease(Generic[T]):
    """One idempotently releasable claim on a resident resource."""

    def __init__(
        self,
        owner: OnDemandResource[T],
        value: T,
        generation: int,
    ) -> None:
        self._owner = owner
        self._value: T | None = value
        self._generation = generation
        self._released = False
        self._release_lock = threading.Lock()

    @property
    def value(self) -> T:
        """Return the resource while this lease remains active."""

        with self._release_lock:
            if self._released:
                raise RuntimeError("resource lease has already been released")
            value = self._value
            if value is None:
                raise RuntimeError("resource lease has no owned value")
            return value

    def close(self) -> None:
        """Release this lease once; unload after the final owner leaves."""

        with self._release_lock:
            if self._released:
                return
            self._released = True
            self._value = None
        self._owner._release(self._generation)

    def __enter__(self) -> T:
        return self.value

    def __exit__(self, *_args: object) -> None:
        self.close()


class OnDemandResource(Generic[T]):
    """Load on the first lease and unload after the final lease.

    Loading and unloading run outside the condition lock. Concurrent acquirers
    wait for one owner instead of starting duplicate heavyweight resources.
    A failed load is retryable; a failed unload is terminal because resource
    ownership can no longer be proven safe without restarting its process.
    """

    def __init__(
        self,
        loader: Callable[[], T],
        unloader: Callable[[T], None],
        *,
        name: str,
    ) -> None:
        if not name.strip():
            raise ValueError("on-demand resource name must not be empty")
        self._loader = loader
        self._unloader = unloader
        self._name = name
        self._condition = threading.Condition()
        self._state: ResidencyState = "unloaded"
        self._resource: T | None = None
        self._lease_count = 0
        self._generation = 0
        self._load_count = 0
        self._unload_count = 0
        self._last_load_ms = 0.0
        self._last_unload_ms = 0.0
        self._last_error = ""

    def acquire(self) -> ResourceLease[T]:
        """Acquire one lease, single-flight loading when currently idle."""

        while True:
            with self._condition:
                while self._state in {"loading", "unloading"}:
                    self._condition.wait()
                if self._state == "failed":
                    raise RuntimeError(
                        f"{self._name} residency is failed; restart required: "
                        f"{self._last_error}"
                    )
                if self._state == "loaded":
                    resource = self._resource
                    if resource is None:
                        raise RuntimeError(
                            f"{self._name} residency invariant violated: no resource"
                        )
                    self._lease_count += 1
                    return ResourceLease(self, resource, self._generation)
                self._state = "loading"
                break

        started = time.perf_counter()
        try:
            resource = self._loader()
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            with self._condition:
                self._state = "unloaded"
                self._last_load_ms = round(elapsed_ms, 3)
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._condition.notify_all()
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self._condition:
            self._resource = resource
            self._state = "loaded"
            self._generation += 1
            self._load_count += 1
            self._lease_count = 1
            self._last_load_ms = round(elapsed_ms, 3)
            self._last_error = ""
            generation = self._generation
            self._condition.notify_all()
        return ResourceLease(self, resource, generation)

    def snapshot(self) -> ResidencySnapshot:
        """Return a consistent content-free lifecycle snapshot."""

        with self._condition:
            return ResidencySnapshot(
                state=self._state,
                lease_count=self._lease_count,
                generation=self._generation,
                load_count=self._load_count,
                unload_count=self._unload_count,
                last_load_ms=self._last_load_ms,
                last_unload_ms=self._last_unload_ms,
                last_error=self._last_error,
            )

    def _release(self, generation: int) -> None:
        with self._condition:
            if self._state != "loaded" or generation != self._generation:
                raise RuntimeError(
                    f"{self._name} lease generation is no longer owned"
                )
            if self._lease_count <= 0:
                raise RuntimeError(f"{self._name} lease count underflow")
            self._lease_count -= 1
            if self._lease_count:
                return
            resource = self._resource
            if resource is None:
                raise RuntimeError(
                    f"{self._name} residency invariant violated during unload"
                )
            self._state = "unloading"

        started = time.perf_counter()
        try:
            self._unloader(resource)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            with self._condition:
                self._state = "failed"
                self._last_unload_ms = round(elapsed_ms, 3)
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._condition.notify_all()
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self._condition:
            self._resource = None
            self._state = "unloaded"
            self._unload_count += 1
            self._last_unload_ms = round(elapsed_ms, 3)
            self._last_error = ""
            self._condition.notify_all()


__all__ = [
    "OnDemandResource",
    "ResidencySnapshot",
    "ResidencyState",
    "ResourceLease",
]
