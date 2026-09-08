"""Full selected MEMORY reaches SYSTEM without rendering or byte normalization.

Only the selected store and workspace-file boundary are replaced. The actual
authority reader, chat prefix builder, assembler and refresh helper run against
synthetic in-memory text; no service, model or persistent storage is started.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.core.chatter import LifeChatter
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.storage.subject_contracts import subject_revision_from_contents
from src.kernel.llm import ROLE, LLMContextManager, LLMPayload, Text
from src.kernel.llm.request import LLMRequest


class _SelectedAuthority:
    """Expose synthetic bytes through the real selected-authority read path."""

    def __init__(self) -> None:
        self.memory = ""
        self.read_count = 0

    async def read_subject_authority(self) -> SimpleNamespace:
        self.read_count += 1
        contents = {
            "SOUL.md": b"# SOUL\nSynthetic engineering fixture only.\n",
            "USER.md": b"# USER\nSynthetic user fixture only.\n",
            "MEMORY.md": self.memory.encode("utf-8"),
        }
        return SimpleNamespace(
            commits={
                name: SimpleNamespace(version=SimpleNamespace(content_bytes=content))
                for name, content in contents.items()
            },
            revision=subject_revision_from_contents(cast(Any, contents)),
        )


@pytest.fixture
def full_memory_runtime(monkeypatch):
    store = _SelectedAuthority()
    service = LifeEngineService.__new__(LifeEngineService)
    service._subject_document_store = cast(Any, store)
    service._selectable_storage_enabled = True
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=LifeEngineConfig())
    chatter.stream_id = "synthetic-full-memory-stream"
    monkeypatch.setattr(LifeChatter, "_GLOBAL_ROLLING_CONTEXT_RECOVERY_REQUIRED", "")
    monkeypatch.setattr(LifeChatter, "_load_workspace_markdown", lambda *_args: "")
    monkeypatch.setattr(
        LLMRequest,
        "send",
        AsyncMock(side_effect=AssertionError("No model requests in isolated test")),
    )
    return SimpleNamespace(store=store, service=service, chatter=chatter)


def _long_memory(tail: str = "synthetic_tail_v1") -> str:
    # Fixed historical limits were 32 Durable and 10 Active items. Keep both
    # over those limits; only the final, formerly omitted Active item changes.
    durable = "\n".join(f"- synthetic_durable_{index:02d}" for index in range(1, 41))
    active = "\n".join(f"- synthetic_active_{index:02d}" for index in range(1, 13))
    return f"# MEMORY\n\n### Durable\n{durable}\n\n### Active\n{active}\n- {tail}\n"


def _system_payload(request: LLMRequest) -> LLMPayload:
    payloads = [payload for payload in request.payloads if payload.role == ROLE.SYSTEM]
    assert len(payloads) == 1
    assert len(payloads[0].content) == 1
    assert isinstance(payloads[0].content[0], Text)
    return payloads[0]


@pytest.mark.asyncio
async def test_memory_without_standard_headings_preserves_every_character(
    full_memory_runtime,
):
    fixture = full_memory_runtime
    memory = (
        " \r\n# Synthetic free-form memory\r\n"
        "First paragraph has  double spaces.\r\n\r\n"
        "Second paragraph\tkeeps a tab and punctuation.\r\n \t"
    )
    fixture.store.memory = memory

    system = await fixture.chatter._build_chat_system_prompt(fixture.service, None)

    assert memory in system, "The original MEMORY string must survive without strip()"
    assert system.count(memory) == 1
    assert fixture.store.read_count == 1


@pytest.mark.asyncio
async def test_preface_custom_sections_and_fading_stay_in_original_order(
    full_memory_runtime,
):
    fixture = full_memory_runtime
    memory = (
        "# MEMORY\nSynthetic preface before any recognized section.\n\n"
        "### Durable\n- synthetic durable  item\n\n"
        "### Custom engineering section\nSynthetic custom paragraph.\n\n"
        "### Fading\n- synthetic fading item\n\n"
        "### Active\n- synthetic active item\n"
    )
    fixture.store.memory = memory

    system = await fixture.chatter._build_chat_system_prompt(fixture.service, None)

    assert memory in system, "MEMORY must not be reconstructed from selected sections"
    assert system.count(memory) == 1
    assert fixture.store.read_count == 1


@pytest.mark.asyncio
async def test_all_durable_and_active_items_survive_former_prompt_limits(
    full_memory_runtime,
):
    fixture = full_memory_runtime
    memory = _long_memory()
    fixture.store.memory = memory

    system = await fixture.chatter._build_chat_system_prompt(fixture.service, None)

    assert "synthetic_durable_40" in system
    assert "synthetic_active_12" in system
    assert "synthetic_tail_v1" in system
    assert memory in system, (
        "The complete MEMORY must occur contiguously, not as excerpts"
    )
    assert system.count(memory) == 1
    assert fixture.store.read_count == 1


@pytest.mark.asyncio
async def test_tail_only_change_refreshes_system_without_replacing_other_payloads(
    full_memory_runtime,
):
    fixture = full_memory_runtime
    previous_memory = _long_memory("synthetic_tail_v1")
    fixture.store.memory = previous_memory
    first_system = await fixture.chatter._build_chat_system_prompt(
        fixture.service, None
    )
    request = LLMRequest(
        model_set=[],
        policy=cast(Any, object()),
        clients=cast(Any, object()),
        context_manager=LLMContextManager(),
        enable_metrics=False,
    )
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(first_system)))
    request.add_payload(LLMPayload(ROLE.USER, Text("synthetic prior user payload")))
    request.add_payload(
        LLMPayload(ROLE.ASSISTANT, Text("synthetic prior assistant payload"))
    )
    request.add_payload(LLMPayload(ROLE.USER, Text("synthetic current user payload")))
    original_payloads = tuple(request.payloads)
    manager = request.context_manager
    current_memory = _long_memory("synthetic_tail_v2")
    assert current_memory == previous_memory.replace(
        "synthetic_tail_v1", "synthetic_tail_v2"
    )
    fixture.store.memory = current_memory

    await fixture.chatter._refresh_subject_system_prompt(request, fixture.service)

    refreshed = _system_payload(request)
    assert refreshed is not original_payloads[0], (
        "An omitted-tail edit must replace SYSTEM"
    )
    system = cast(Text, refreshed.content[0]).text
    assert current_memory in system
    assert system.count(current_memory) == 1
    assert "synthetic_tail_v1" not in system
    assert request.context_manager is manager
    assert len(request.payloads) == len(original_payloads)
    assert all(
        current is old
        for current, old in zip(request.payloads[1:], original_payloads[1:])
    )
    assert fixture.store.read_count == 2
    LLMRequest.send.assert_not_called()
