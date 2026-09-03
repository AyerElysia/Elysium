"""Parallel-execution policy for life_engine tool calls."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

_PARALLEL_SAFE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "conversation_evidence",
        "fetch_life_memory",
        "grep_life_events",
        "nucleus_browser_fetch",
        "tool-nucleus_browser_fetch",
        "nucleus_grep_events",
        "nucleus_grep_file",
        "nucleus_list_files",
        "nucleus_list_schedules",
        "nucleus_memory_stats",
        "nucleus_proactive_query",
        "nucleus_read_file",
        "nucleus_web_search",
        "tool-nucleus_web_search",
    }
)


def _tool_call_name(call: Any) -> str:
    return str(getattr(call, "name", "") or "").strip().lower()


def is_life_tool_call_parallel_safe(call: Any) -> bool:
    """Return whether a life_engine tool call can be grouped for parallel execution."""
    name = _tool_call_name(call)
    if not name or name.startswith("action-"):
        return False

    return name in _PARALLEL_SAFE_TOOL_NAMES


def iter_life_tool_call_batches(calls: Iterable[Any]) -> Iterator[tuple[list[Any], bool]]:
    """Yield consecutive parallel-safe batches, with unsafe calls as singleton batches."""
    pending_parallel: list[Any] = []
    for call in calls:
        if is_life_tool_call_parallel_safe(call):
            pending_parallel.append(call)
            continue

        if pending_parallel:
            yield pending_parallel, True
            pending_parallel = []
        yield [call], False

    if pending_parallel:
        yield pending_parallel, True
