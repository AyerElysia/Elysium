"""Same-process subject SYSTEM freshness, using synthetic in-memory authority.

These tests never start a service, invoke a model, or read/write subject data.
The request/context manager, authority reader, prefix builder and write-change
notification route are production implementations; only external boundaries
and selected-store snapshots are replaced.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.life_engine.core.chatter import LifeChatter, _Phase
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.storage.subject_contracts import subject_revision_from_contents
from plugins.life_engine.tools import file_tools
from src.core.components.base.chatter import Failure, Wait
from src.kernel.llm import ROLE, LLMContextManager, LLMPayload, Text
from src.kernel.llm.request import LLMRequest


class _SelectedAuthority:
    """Replace only selected storage, not the actual authority-reading code."""

    def __init__(self) -> None:
        self.memory = "fixture_memory_v1"
        self.soul = b"# SOUL\nSynthetic engineering fixture only.\n"
        self.read_count = 0

    async def read_subject_authority(self) -> SimpleNamespace:
        self.read_count += 1
        contents = {
            "SOUL.md": self.soul,
            "USER.md": b"# USER\nSynthetic user fixture only.\n",
            "MEMORY.md": f"# MEMORY\n\n### Durable\n- {self.memory}\n".encode(),
        }
        return SimpleNamespace(
            commits={
                name: SimpleNamespace(
                    version=SimpleNamespace(content_bytes=content)
                )
                for name, content in contents.items()
            },
            revision=subject_revision_from_contents(cast(Any, contents)),
        )


@pytest.fixture
def isolated_subject_runtime(monkeypatch):
    LifeChatter.reset_global_runtime()
    store = _SelectedAuthority()
    service = LifeEngineService.__new__(LifeEngineService)
    service._subject_document_store = cast(Any, store)
    service._selectable_storage_enabled = True
    router_projection = SimpleNamespace(notify_source_changed=Mock(return_value=True))
    subject_projection = SimpleNamespace(notify_source_changed=Mock(return_value=True))
    service._router_context_projection = cast(Any, router_projection)
    service._subject_context_projections = {"test": subject_projection}
    plugin = SimpleNamespace(config=LifeEngineConfig())
    created_requests: list[LLMRequest] = []
    history = [
        LLMPayload(ROLE.USER, Text("synthetic prior user payload")),
        LLMPayload(ROLE.ASSISTANT, Text("synthetic prior self-context payload")),
    ]

    def create_request(_self):
        request = LLMRequest(
            model_set=[],
            policy=cast(Any, object()),
            clients=cast(Any, object()),
            context_manager=LLMContextManager(),
            enable_metrics=False,
        )
        created_requests.append(request)
        return request

    monkeypatch.setattr(LifeChatter, "_create_global_request", create_request)
    monkeypatch.setattr(LifeChatter, "inject_usables", AsyncMock(return_value={}))
    monkeypatch.setattr(
        LifeChatter, "_load_rolling_context_snapshot", AsyncMock(return_value=history)
    )
    monkeypatch.setattr(
        LifeChatter, "_save_rolling_context_snapshot",
        AsyncMock(side_effect=AssertionError("No snapshot writes in isolated test")),
    )
    monkeypatch.setattr(
        LifeChatter, "_load_workspace_markdown", lambda *_args: ""
    )
    monkeypatch.setattr(
        LLMRequest, "send",
        AsyncMock(side_effect=AssertionError("No model requests in isolated test")),
    )
    monkeypatch.setattr(file_tools, "_get_life_engine_service", lambda _plugin: service)

    def make_chatter(stream_id="fixture-stream-a"):
        chatter = LifeChatter.__new__(LifeChatter)
        chatter.plugin = plugin
        chatter.stream_id = stream_id
        return chatter, SimpleNamespace(stream_id=stream_id)

    try:
        yield SimpleNamespace(
            store=store, service=service, plugin=plugin,
            router_projection=router_projection, subject_projection=subject_projection,
            created_requests=created_requests, history=history, make_chatter=make_chatter,
        )
    finally:
        LifeChatter.reset_global_runtime()


def _system_text(request: LLMRequest) -> str:
    system_payloads = [payload for payload in request.payloads if payload.role == ROLE.SYSTEM]
    assert len(system_payloads) == 1, "Refresh must keep exactly one SYSTEM payload"
    return "\n".join(part.text for part in system_payloads[0].content if isinstance(part, Text))


@pytest.mark.asyncio
async def test_fresh_prefix_reads_updated_selected_authority(isolated_subject_runtime):
    fixture = isolated_subject_runtime
    chatter, _ = fixture.make_chatter()
    first = await chatter._build_chat_system_prompt(fixture.service, None)
    assert "fixture_memory_v1" in first
    fixture.store.memory = "fixture_memory_v2"
    second = await chatter._build_chat_system_prompt(fixture.service, None)
    assert "fixture_memory_v2" in second
    assert "fixture_memory_v1" not in second
    assert fixture.store.read_count == 2
    assert fixture.created_requests == []


@pytest.mark.asyncio
async def test_unchanged_authority_preserves_cached_runtime(isolated_subject_runtime):
    fixture = isolated_subject_runtime
    chatter, stream = fixture.make_chatter()
    first, first_tools = await chatter._get_or_create_global_runtime(fixture.service, stream)
    original_payloads = tuple(first.response.payloads)
    second, second_tools = await chatter._get_or_create_global_runtime(fixture.service, stream)
    await chatter._refresh_subject_system_prompt(second.response, fixture.service)
    assert second is first
    assert second_tools is first_tools
    assert len(fixture.created_requests) == 1
    assert len(second.response.payloads) == len(original_payloads)
    assert all(current is old for current, old in zip(second.response.payloads, original_payloads))
    assert "fixture_memory_v1" in _system_text(second.response)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_chatter_instance", [False, True])
async def test_model_boundary_refreshes_cached_system_after_selected_memory_change(
    isolated_subject_runtime, new_chatter_instance,
):
    fixture = isolated_subject_runtime
    chatter, stream = fixture.make_chatter()
    runtime, tools = await chatter._get_or_create_global_runtime(fixture.service, stream)
    request = runtime.response
    manager = request.context_manager
    assert "fixture_memory_v1" in _system_text(request)
    original_history = tuple(payload for payload in request.payloads if payload.role != ROLE.SYSTEM)
    fixture.store.memory = "fixture_memory_v2"
    file_tools._notify_router_context_source_changed(fixture.plugin, "MEMORY.md")
    fixture.router_projection.notify_source_changed.assert_called_once_with("MEMORY.md")
    fixture.subject_projection.notify_source_changed.assert_called_once_with("MEMORY.md")
    if new_chatter_instance:
        chatter, stream = fixture.make_chatter("fixture-stream-b")

    reused, reused_tools = await chatter._get_or_create_global_runtime(fixture.service, stream)
    # The cache getter must remain safe during busy-stream and tool execution
    # paths. Authority refresh belongs at the actual model-turn boundary.
    await chatter._refresh_subject_system_prompt(reused.response, fixture.service)

    assert reused is runtime
    assert reused.response is request
    assert reused.response.context_manager is manager
    assert reused_tools is tools
    assert len(fixture.created_requests) == 1
    current_history = tuple(payload for payload in request.payloads if payload.role != ROLE.SYSTEM)
    assert len(current_history) == len(original_history)
    assert all(current is old for current, old in zip(current_history, original_history))
    system_text = _system_text(request)
    assert "fixture_memory_v2" in system_text, (
        "Same-process cached SYSTEM did not read the new selected MEMORY version; "
        f"authority reads={fixture.store.read_count}"
    )
    assert "fixture_memory_v1" not in system_text


@pytest.mark.asyncio
async def test_refresh_fails_closed_without_mutation_on_authority_read_failure(
    isolated_subject_runtime, monkeypatch,
):
    fixture = isolated_subject_runtime
    chatter, stream = fixture.make_chatter()
    runtime, _ = await chatter._get_or_create_global_runtime(fixture.service, stream)
    original_payloads = tuple(runtime.response.payloads)
    original_system = _system_text(runtime.response)
    monkeypatch.setattr(
        fixture.store, "read_subject_authority",
        AsyncMock(side_effect=RuntimeError("fixture selected authority unavailable")),
    )

    with pytest.raises(RuntimeError):
        await chatter._refresh_subject_system_prompt(runtime.response, fixture.service)

    assert len(runtime.response.payloads) == len(original_payloads)
    assert all(current is old for current, old in zip(runtime.response.payloads, original_payloads))
    assert _system_text(runtime.response) == original_system
    LLMRequest.send.assert_not_called()


class _ReachedBeforeModel(RuntimeError):
    """Stop the real driver before compaction, model invocation or persistence."""


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [_Phase.MODEL_TURN, _Phase.FOLLOW_UP])
async def test_real_driver_refreshes_authority_before_model_preparation(
    isolated_subject_runtime, monkeypatch, phase,
):
    fixture = isolated_subject_runtime
    chatter, stream = fixture.make_chatter()
    runtime, tools = await chatter._get_or_create_global_runtime(fixture.service, stream)
    request = runtime.response
    runtime.phase = phase
    runtime.active_stream_id = stream.stream_id
    original_history = tuple(payload for payload in request.payloads if payload.role != ROLE.SYSTEM)
    fixture.store.memory = "fixture_memory_v2"
    monkeypatch.setattr(chatter, "fetch_unreads", AsyncMock(return_value=(None, [])))
    monkeypatch.setattr(chatter, "_inject_delta_unreads_if_any", AsyncMock())
    stop_before_compaction = Mock(side_effect=_ReachedBeforeModel)
    monkeypatch.setattr(chatter, "_install_derived_rolling_projection", stop_before_compaction)

    with pytest.raises(_ReachedBeforeModel):
        await chatter._drive_global_runtime_until_yield(stream, fixture.service)

    stop_before_compaction.assert_called_once()
    assert fixture.store.read_count == 2
    assert "fixture_memory_v2" in _system_text(request)
    assert "fixture_memory_v1" not in _system_text(request)
    assert runtime.response is request
    assert LifeChatter._GLOBAL_USABLE_MAP is tools
    assert runtime.phase == phase
    assert runtime.active_stream_id == stream.stream_id
    current_history = tuple(payload for payload in request.payloads if payload.role != ROLE.SYSTEM)
    assert len(current_history) == len(original_history)
    assert all(current is old for current, old in zip(current_history, original_history))
    LLMRequest.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [_Phase.MODEL_TURN, _Phase.FOLLOW_UP])
async def test_real_driver_returns_failure_without_send_when_authority_unavailable(
    isolated_subject_runtime, monkeypatch, phase,
):
    fixture = isolated_subject_runtime
    chatter, stream = fixture.make_chatter()
    runtime, _ = await chatter._get_or_create_global_runtime(fixture.service, stream)
    runtime.phase = phase
    runtime.active_stream_id = stream.stream_id
    original_payloads = tuple(runtime.response.payloads)
    original_system = _system_text(runtime.response)
    monkeypatch.setattr(chatter, "fetch_unreads", AsyncMock(return_value=(None, [])))
    model_preparation = AsyncMock(side_effect=AssertionError("Preparation must not run"))
    monkeypatch.setattr(chatter, "_inject_delta_unreads_if_any", model_preparation)
    monkeypatch.setattr(
        fixture.store, "read_subject_authority",
        AsyncMock(side_effect=RuntimeError("fixture selected authority unavailable")),
    )

    result = await chatter._drive_global_runtime_until_yield(stream, fixture.service)

    assert isinstance(result, Failure)
    model_preparation.assert_not_called()
    assert runtime.phase == phase
    assert runtime.active_stream_id == stream.stream_id
    assert len(runtime.response.payloads) == len(original_payloads)
    assert all(current is old for current, old in zip(runtime.response.payloads, original_payloads))
    assert _system_text(runtime.response) == original_system
    LLMRequest.send.assert_not_called()


@pytest.mark.asyncio
async def test_busy_nonowner_stream_does_not_read_subject_authority(
    isolated_subject_runtime, monkeypatch,
):
    fixture = isolated_subject_runtime
    owner, stream = fixture.make_chatter()
    runtime, _ = await owner._get_or_create_global_runtime(fixture.service, stream)
    runtime.phase = _Phase.MODEL_TURN
    runtime.active_stream_id = stream.stream_id
    original_payloads = tuple(runtime.response.payloads)
    visitor, visitor_stream = fixture.make_chatter("fixture-stream-b")
    authority_read = AsyncMock(side_effect=AssertionError("Busy stream must not read authority"))
    monkeypatch.setattr(fixture.store, "read_subject_authority", authority_read)

    result = await visitor._drive_global_runtime_until_yield(visitor_stream, fixture.service)

    assert isinstance(result, Wait)
    authority_read.assert_not_called()
    assert runtime.active_stream_id == stream.stream_id
    assert len(runtime.response.payloads) == len(original_payloads)
    assert all(current is old for current, old in zip(runtime.response.payloads, original_payloads))
    LLMRequest.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("system_count", [0, 2])
async def test_refresh_rejects_missing_or_duplicate_system_without_mutation(
    isolated_subject_runtime, system_count,
):
    fixture = isolated_subject_runtime
    chatter, stream = fixture.make_chatter()
    runtime, _ = await chatter._get_or_create_global_runtime(fixture.service, stream)
    history = [payload for payload in runtime.response.payloads if payload.role != ROLE.SYSTEM]
    runtime.response.payloads = [
        LLMPayload(ROLE.SYSTEM, Text(f"malformed fixture SYSTEM {index}"))
        for index in range(system_count)
    ] + history
    original_payloads = tuple(runtime.response.payloads)
    fixture.store.memory = "fixture_memory_v2"

    with pytest.raises(RuntimeError):
        await chatter._refresh_subject_system_prompt(runtime.response, fixture.service)

    assert len(runtime.response.payloads) == len(original_payloads)
    assert all(current is old for current, old in zip(runtime.response.payloads, original_payloads))
    LLMRequest.send.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_rejects_unavailable_soul_without_stale_fallback(
    isolated_subject_runtime,
):
    fixture = isolated_subject_runtime
    chatter, stream = fixture.make_chatter()
    runtime, _ = await chatter._get_or_create_global_runtime(fixture.service, stream)
    original_payloads = tuple(runtime.response.payloads)
    original_system = _system_text(runtime.response)
    fixture.store.soul = b""

    with pytest.raises(RuntimeError):
        await chatter._refresh_subject_system_prompt(runtime.response, fixture.service)

    assert len(runtime.response.payloads) == len(original_payloads)
    assert all(current is old for current, old in zip(runtime.response.payloads, original_payloads))
    assert _system_text(runtime.response) == original_system
    LLMRequest.send.assert_not_called()
