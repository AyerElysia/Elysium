"""Explicit stream scopes must not expand through consciousness activities.

All fixtures are isolated engineering data, not subject memories or providers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.life_engine.service import registry
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.tools.event_grep_tools import (
    LifeEngineGrepEventsTool,
    grep_life_events,
)


@pytest.fixture
def scoped_activity_service(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(registry, "_life_engine_registry", registry.ServiceRegistry())
    service = LifeEngineService(SimpleNamespace(config=None))
    assert service._life_event_store is None
    registry.register_life_engine_service(service)
    return service


def _activity(service: LifeEngineService, stream: str, kind: str):
    """Use the real builder, including its legacy type and raw content mapping."""
    builder = service._event_builder
    identity = f"isolated-{stream or 'global'}-{kind}"
    shared = {
        "activity_id": identity,
        "stream_id": stream,
        "source_instance_id": "isolated-same-instance",
        "turn_occurrence_id": "isolated-engineering-turn",
    }
    if kind == "model":
        return builder.build_conscious_model_turn_event(
            **shared,
            transport_request_id=identity,
            provider_reasoning_content="",
            assistant_message="isolated fixture marker",
            tool_call_ids=[],
        )
    if kind == "call":
        return builder.build_conscious_tool_call_event(
            "tool-isolated-fixture",
            {"fixture": "isolated fixture marker"},
            **shared,
            model_turn_activity_id="isolated-model",
            call_id=identity,
        )
    assert kind == "result"
    return builder.build_conscious_tool_result_event(
        "tool-isolated-fixture",
        {"fixture": "isolated fixture marker"},
        False,
        **shared,
        call_id=identity,
    )


@pytest.mark.parametrize("kind", ["call", "result", "model"])
async def test_named_other_stream_activity_is_not_unscoped_internal(
    scoped_activity_service: LifeEngineService, kind: str
) -> None:
    service = scoped_activity_service
    service._event_history = [
        _activity(service, "scope-a", kind),
        _activity(service, "scope-b", kind),
        _activity(service, "", kind),
    ]
    result = await grep_life_events(
        stream_ids=["scope-a"],
        include_life_internal=True,
        context_before=0,
        context_after=0,
    )
    assert {match["event"]["stream_id"] for match in result["matches"]} == {
        "scope-a",
        "",
    }


async def test_internal_opt_out_keeps_only_requested_stream(
    scoped_activity_service: LifeEngineService,
) -> None:
    service = scoped_activity_service
    service._event_history = [
        _activity(service, stream, "result") for stream in ("scope-a", "scope-b", "")
    ]
    result = await grep_life_events(
        stream_ids=["scope-a"], include_life_internal=False, event_types=["tool"]
    )
    assert [match["event"]["stream_id"] for match in result["matches"]] == ["scope-a"]
    assert result["matches"][0]["event"]["tool_success"] is False


async def test_explicit_multi_stream_and_cross_stream_still_reach_activities(
    scoped_activity_service: LifeEngineService,
) -> None:
    service = scoped_activity_service
    service._event_history = [
        _activity(service, stream, "call") for stream in ("scope-a", "scope-b", "")
    ]
    explicit = await grep_life_events(
        stream_ids=["scope-a", "scope-b"], include_life_internal=False
    )
    assert {match["event"]["stream_id"] for match in explicit["matches"]} == {
        "scope-a",
        "scope-b",
    }
    tool = LifeEngineGrepEventsTool(plugin=SimpleNamespace())
    tool.chat_stream = SimpleNamespace(stream_id="scope-a")
    ok, payload = await tool.execute(
        cross_stream=True, context_before=0, context_after=0
    )
    assert ok and isinstance(payload, dict)
    assert payload["scope"] == "all_streams"
    assert payload["stats"]["matched_events"] == 3


async def test_default_current_stream_scope_also_filters_neighbor_context(
    scoped_activity_service: LifeEngineService,
) -> None:
    service = scoped_activity_service
    service._event_history = [
        _activity(service, "scope-b", "call"),
        _activity(service, "scope-a", "result"),
        _activity(service, "scope-b", "model"),
    ]
    tool = LifeEngineGrepEventsTool(plugin=SimpleNamespace())
    tool.chat_stream = SimpleNamespace(stream_id="scope-a")
    ok, payload = await tool.execute(
        query="isolated-scope-a-result", context_before=2, context_after=2
    )
    assert ok and isinstance(payload, dict)
    assert payload["scope"] == "filtered_streams"
    assert payload["stream_ids"] == ["scope-a"]
    assert payload["stats"]["matched_events"] == 1
    assert "scope-b" not in json.dumps(payload, ensure_ascii=False)
