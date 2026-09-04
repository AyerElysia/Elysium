"""Positive contracts retained while removing unreferenced Memory wrappers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine import memory
from plugins.life_engine.memory.service import LifeMemoryService


@pytest.mark.parametrize(
    "method_name",
    ["get_or_create_file_node", "get_or_create_workspace_document_node"],
)
async def test_retired_node_authoring_still_fails_closed(method_name: str) -> None:
    # A never-initialized service has no storage: refusal must precede any I/O.
    service = object.__new__(LifeMemoryService)
    with pytest.raises(RuntimeError, match="^LegacyGraphNodeMutationRetired$"):
        await getattr(service, method_name)("notes/legacy.md")


def test_stable_lazy_exports_all_remain_resolvable() -> None:
    for name in memory.__all__:
        assert getattr(memory, name) is not None, name


async def test_reflective_recall_callbacks_preserve_arguments_and_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(LifeMemoryService)
    episode = object()
    events = (object(), object())
    corecall = object()
    living = SimpleNamespace(
        begin_recall=AsyncMock(return_value=episode),
        append_recall_events=AsyncMock(return_value=events),
        append_corecall=AsyncMock(return_value=corecall),
    )
    monkeypatch.setattr(
        service, "_require_memory_storage", lambda: SimpleNamespace(living=living)
    )
    context = {"source_occurrence_id": "cleanup-test-occurrence"}

    assert await getattr(service, "begin_memory_recall")(
        query="test query", context=context
    ) is episode
    assert await getattr(service, "append_memory_recall_events")(events) is events
    assert await getattr(service, "append_memory_corecall")(corecall) is corecall
    living.begin_recall.assert_awaited_once_with(query="test query", context=context)
    assert living.begin_recall.call_args.kwargs["context"] is context
    living.append_recall_events.assert_awaited_once_with(events)
    living.append_corecall.assert_awaited_once_with(corecall)
