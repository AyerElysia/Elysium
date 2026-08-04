from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.voice_live.seedvc_residency import OnDemandResource


def test_on_demand_resource_loads_for_first_lease_and_unloads_after_last() -> None:
    events: list[str] = []
    resource = OnDemandResource(
        lambda: events.append("load") or object(),
        lambda _value: events.append("unload"),
        name="test model",
    )

    assert resource.snapshot().state == "unloaded"
    first = resource.acquire()
    second = resource.acquire()
    assert first.value is second.value
    assert resource.snapshot().lease_count == 2
    assert events == ["load"]

    first.close()
    assert resource.snapshot().state == "loaded"
    second.close()
    second.close()
    snapshot = resource.snapshot()
    assert snapshot.state == "unloaded"
    assert snapshot.load_count == 1
    assert snapshot.unload_count == 1
    assert events == ["load", "unload"]


def test_on_demand_resource_concurrent_acquire_is_single_flight() -> None:
    load_started = threading.Event()
    allow_load = threading.Event()
    load_count = 0
    unload_count = 0

    def load() -> object:
        nonlocal load_count
        load_count += 1
        load_started.set()
        assert allow_load.wait(timeout=2)
        return object()

    def unload(_value: object) -> None:
        nonlocal unload_count
        unload_count += 1

    resource = OnDemandResource(load, unload, name="test model")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(resource.acquire)
        assert load_started.wait(timeout=2)
        second_future = pool.submit(resource.acquire)
        allow_load.set()
        first = first_future.result(timeout=2)
        second = second_future.result(timeout=2)

    assert first.value is second.value
    assert load_count == 1
    assert resource.snapshot().lease_count == 2
    first.close()
    assert unload_count == 0
    second.close()
    assert unload_count == 1


def test_on_demand_resource_retries_a_failed_load() -> None:
    attempts = 0

    def load() -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary load failure")
        return object()

    resource = OnDemandResource(load, lambda _value: None, name="test model")
    with pytest.raises(RuntimeError, match="temporary load failure"):
        resource.acquire()

    failed = resource.snapshot()
    assert failed.state == "unloaded"
    assert failed.load_count == 0
    assert "temporary load failure" in failed.last_error

    lease = resource.acquire()
    assert resource.snapshot().state == "loaded"
    lease.close()
    assert attempts == 2


def test_on_demand_resource_surfaces_unload_ownership_failure() -> None:
    def fail_unload(_value: object) -> None:
        raise RuntimeError("cuda cleanup failed")

    resource = OnDemandResource(lambda: object(), fail_unload, name="test model")
    lease = resource.acquire()
    with pytest.raises(RuntimeError, match="cuda cleanup failed"):
        lease.close()

    snapshot = resource.snapshot()
    assert snapshot.state == "failed"
    assert snapshot.lease_count == 0
    with pytest.raises(RuntimeError, match="restart required"):
        resource.acquire()
