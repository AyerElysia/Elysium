"""Concurrency tests for life_engine runtime state persistence."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from plugins.life_engine.service import state_manager as state_manager_module
from plugins.life_engine.service.event_builder import LifeEngineState
from plugins.life_engine.service.state_manager import StatePersistence


async def test_runtime_context_writes_are_serialized(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Concurrent saves must not replace staging files in parallel."""

    original_write = state_manager_module.atomic_write_text
    counter_lock = threading.Lock()
    active_writers = 0
    maximum_writers = 0

    def tracked_write(path: Path, content: str) -> None:
        nonlocal active_writers, maximum_writers
        with counter_lock:
            active_writers += 1
            maximum_writers = max(maximum_writers, active_writers)
        try:
            time.sleep(0.01)
            original_write(path, content)
        finally:
            with counter_lock:
                active_writers -= 1

    monkeypatch.setattr(state_manager_module, "atomic_write_text", tracked_write)
    persistence = StatePersistence(str(tmp_path), lambda: 100)
    state = LifeEngineState()

    await asyncio.gather(
        *(
            persistence.save_runtime_context(state, [], [])
            for _ in range(12)
        )
    )

    assert maximum_writers == 1
    payload = json.loads((tmp_path / "life_engine_context.json").read_text())
    assert payload["version"] == 2
    assert not list(tmp_path.glob("*.tmp"))


async def test_independent_persistence_instances_use_unique_staging_files(
    tmp_path: Path,
) -> None:
    """Accidental duplicate instances must not collide on a fixed .tmp path."""

    first = StatePersistence(str(tmp_path), lambda: 100)
    second = StatePersistence(str(tmp_path), lambda: 100)
    state = LifeEngineState()

    await asyncio.gather(
        *(
            writer.save_runtime_context(state, [], [])
            for writer in (first, second)
            for _ in range(10)
        )
    )

    payload = json.loads((tmp_path / "life_engine_context.json").read_text())
    assert payload["version"] == 2
    assert not list(tmp_path.glob("*.tmp"))
