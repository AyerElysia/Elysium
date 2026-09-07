from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.life_engine.core.context_assembly import (
    AssembledPrompt,
    LifeChatterContextAssembler,
    PromptLayer,
)
from src.kernel.llm import LLMPayload, ROLE, Text


def test_prompt_layers_are_explicit() -> None:
    assert PromptLayer.PREFIX.value == "prefix"
    assert PromptLayer.ROLLING.value == "rolling"
    assert PromptLayer.SUFFIX.value == "suffix"
    assembled = LifeChatterContextAssembler.assemble(
        prefix_text="a",
        rolling_text="b",
        suffix_text="c",
        metadata={"source": "test"},
    )
    assert isinstance(assembled, AssembledPrompt)
    assert assembled.prefix_text == "a"
    assert assembled.rolling_text == "b"
    assert assembled.suffix_text == "c"
    assert assembled.metadata == {"source": "test"}


def test_prefix_prompt_skips_empty_parts_and_preserves_order() -> None:
    prompt = LifeChatterContextAssembler.build_prefix_prompt(
        soul_text="SOUL",
        user_text="",
        memory_text="MEMORY",
        existence_text="EXISTENCE",
        tools_text="TOOLS",
        live_guidance="LIVE",
        primary_tool_guide="GUIDE",
    )

    assert prompt == "SOUL\n\nMEMORY\n\nEXISTENCE\n\nTOOLS\n\nLIVE\n\nGUIDE"


def test_rolling_prompt_contains_history_and_new_messages() -> None:
    prompt = LifeChatterContextAssembler.build_rolling_prompt(
        stream_name="Test",
        stream_id="stream-1",
        unread_lines="NEW",
        history_text="HISTORY",
        is_live_stream=False,
    )

    assert '你当前正在名为"Test"的对话中。' in prompt
    assert "<chat_history>\nHISTORY\n</chat_history>" in prompt
    assert "<new_messages>\nNEW\n</new_messages>" in prompt
    assert "<transient_life_context>" not in prompt


def test_rolling_prompt_can_include_live_guidance() -> None:
    prompt = LifeChatterContextAssembler.build_rolling_prompt(
        stream_name="Live",
        stream_id="live-1",
        unread_lines="弹幕",
        is_live_stream=True,
    )

    assert "当前场景：B站直播间接弹幕。" in prompt


def test_suffix_prompt_appends_and_strips_from_user_tail_only() -> None:
    response = SimpleNamespace(
        payloads=[
            LLMPayload(ROLE.USER, Text("USER_A")),
            LLMPayload(ROLE.ASSISTANT, Text("ASSISTANT")),
            LLMPayload(ROLE.USER, Text("USER_B")),
        ]
    )

    LifeChatterContextAssembler.append_suffix_to_last_user(response, "STATE_NOW")

    assert [part.text for part in response.payloads[0].content] == ["USER_A"]
    assert response.payloads[2].content[-1].text == (
        "<transient_life_context>\nSTATE_NOW\n</transient_life_context>"
    )

    LifeChatterContextAssembler.strip_suffix_from_user_payloads(response)

    assert [part.text for part in response.payloads[2].content] == ["USER_B"]


def test_rolling_payload_upsert_extends_last_user_or_creates_user() -> None:
    response = SimpleNamespace(payloads=[])
    response.add_payload = lambda payload: response.payloads.append(payload)

    LifeChatterContextAssembler.upsert_rolling_user_payload(response, "FIRST")
    LifeChatterContextAssembler.upsert_rolling_user_payload(response, [Text("SECOND")])

    assert len(response.payloads) == 1
    assert response.payloads[0].role == ROLE.USER
    assert [part.text for part in response.payloads[0].content] == ["FIRST", "SECOND"]


_PREFIX_PARTS = {
    "soul_text": "SOUL",
    "user_text": "USER",
    "memory_text": "MEMORY",
    "existence_text": "EXISTENCE",
    "tools_text": "TOOLS",
    "live_guidance": "LIVE",
    "primary_tool_guide": "GUIDE",
}
_NORMALIZED_PREFIX_PARTS = tuple(
    name for name in _PREFIX_PARTS if name != "memory_text"
)


@pytest.mark.parametrize("section", _NORMALIZED_PREFIX_PARTS)
@pytest.mark.parametrize(
    "empty_value", [None, "", " \r\n\t "], ids=["none", "empty", "whitespace"]
)
def test_prefix_normalized_empty_parts_are_skipped_without_reordering(
    section: str, empty_value: str | None,
) -> None:
    parts = {**_PREFIX_PARTS, section: empty_value}

    prompt = LifeChatterContextAssembler.build_prefix_prompt(**parts)

    expected = "\n\n".join(
        value for name, value in _PREFIX_PARTS.items() if name != section
    )
    assert prompt == expected


@pytest.mark.parametrize("section", _NORMALIZED_PREFIX_PARTS)
def test_prefix_other_parts_strip_only_boundary_whitespace(section: str) -> None:
    body = "fixture  inner\tgap\r\nnext line"
    parts = {**_PREFIX_PARTS, section: f" \t\r\n{body}\r\n\t "}

    prompt = LifeChatterContextAssembler.build_prefix_prompt(**parts)

    expected = "\n\n".join(
        body if name == section else value for name, value in _PREFIX_PARTS.items()
    )
    assert prompt == expected
    assert prompt.count(body) == 1


@pytest.mark.parametrize("memory", [None, ""], ids=["none", "empty"])
def test_prefix_absent_memory_is_skipped_without_reordering(memory: str | None) -> None:
    parts = {**_PREFIX_PARTS, "memory_text": memory}

    prompt = LifeChatterContextAssembler.build_prefix_prompt(**parts)

    expected = "\n\n".join(
        value for name, value in _PREFIX_PARTS.items() if name != "memory_text"
    )
    assert prompt == expected


def test_prefix_whitespace_only_memory_is_preserved_verbatim() -> None:
    memory = " \r\n\t "
    parts = {**_PREFIX_PARTS, "memory_text": memory}

    prompt = LifeChatterContextAssembler.build_prefix_prompt(**parts)

    assert prompt == "SOUL\n\nUSER\n\n" + memory + "\n\nEXISTENCE\n\nTOOLS\n\nLIVE\n\nGUIDE"
    assert prompt.count(memory) == 1
