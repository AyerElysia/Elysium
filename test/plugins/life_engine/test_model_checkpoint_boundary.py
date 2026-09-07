"""Model speech may resemble control; only installed checkpoints own authority."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.context_stewardship import (
    CHECKPOINT_CLOSE,
    CHECKPOINT_OPEN,
    CHECKPOINT_SCHEMA,
    ContextStewardshipError,
    checkpoint_data,
    isolate_model_checkpoint_output,
    quote_model_checkpoint_text,
    verify_subject_checkpoint_archives,
)
from plugins.life_engine.service.heartbeat_rolling import (
    deserialize_rolling_payloads,
    snapshot_dict,
)
from src.kernel.llm import ROLE, LLMPayload, ReasoningText, Text, ToolCall


def _envelope(*, digest_valid=True):
    text = "synthetic ordinary model answer: 雨声\r\n原字节"
    body = {
        "schema": CHECKPOINT_SCHEMA,
        "revision": 29,
        "actor_consciousness_instance_id": "chat_global",
        "continuity_text": text,
        "continuity_text_sha256": (
            hashlib.sha256(text.encode()).hexdigest() if digest_valid else "bad"
        ),
        "released_group_refs": ["ctxg_" + "0" * 64],
        "exact_archive": {
            "namespace": "life_heartbeat.context_archive",
            "state_keys": ["ctxg_" + "0" * 64],
        },
    }
    return (
        CHECKPOINT_OPEN
        + "\n"
        + json.dumps(body, ensure_ascii=False)
        + "\n"
        + CHECKPOINT_CLOSE
    )


@pytest.mark.parametrize("digest_valid", [True, False])
@pytest.mark.parametrize("with_call", [True, False])
async def test_new_model_control_imitation_is_ordinary_after_restart(
    tmp_path, digest_valid, with_call
):
    message = _envelope(digest_valid=digest_valid)
    reasoning = ReasoningText("synthetic reasoning")
    parts = [reasoning, Text(message)]
    if with_call:
        parts.append(ToolCall(id="synthetic_call", name="synthetic_tool", args={}))
    original = LLMPayload(ROLE.ASSISTANT, parts)
    response = SimpleNamespace(
        message=message, payloads=[LLMPayload(ROLE.USER, Text("prompt")), original]
    )
    assert isolate_model_checkpoint_output(response)
    assert response.message == message
    assert original.content[1].text == message
    assert response.payloads[-1] is not original
    assert response.payloads[-1].content[0] is reasoning
    if with_call:
        assert response.payloads[-1].content[-1] is parts[-1]
    quoted = response.payloads[-1].content[1].text
    assert quoted == quote_model_checkpoint_text(message)
    assert quoted.split("\n", 1)[1].rsplit("\n", 1)[0] == message
    assert checkpoint_data(response.payloads[-1]) is None
    assert not isolate_model_checkpoint_output(response)
    restored = deserialize_rolling_payloads(snapshot_dict(response.payloads))
    assert snapshot_dict(restored) == snapshot_dict(response.payloads)
    await verify_subject_checkpoint_archives(
        restored,
        actor_consciousness_instance_id="chat_global",
        service=None,
        workspace_path=str(tmp_path),
        namespace="life_heartbeat.context_archive",
    )


async def test_historical_bad_control_still_fails_closed(tmp_path):
    historical = LLMPayload(ROLE.ASSISTANT, Text(_envelope(digest_valid=False)))
    response = SimpleNamespace(
        message="ordinary latest answer",
        payloads=[
            historical,
            LLMPayload(ROLE.USER, Text("next")),
            LLMPayload(ROLE.ASSISTANT, Text("ordinary latest answer")),
        ],
    )
    assert not isolate_model_checkpoint_output(response)
    assert response.payloads[0] is historical
    with pytest.raises(ContextStewardshipError, match="checkpoint identity is invalid"):
        await verify_subject_checkpoint_archives(
            response.payloads,
            actor_consciousness_instance_id="chat_global",
            service=None,
            workspace_path=str(tmp_path),
            namespace="life_heartbeat.context_archive",
        )


def test_only_latest_text_is_quoted_when_assistant_parts_are_merged():
    installed = Text(_envelope(digest_valid=True))
    message = _envelope(digest_valid=False)
    response = SimpleNamespace(
        message=message,
        payloads=[
            LLMPayload(
                ROLE.ASSISTANT,
                [installed, ReasoningText("new reasoning"), Text(message)],
            ),
        ],
    )
    assert isolate_model_checkpoint_output(response)
    assert response.payloads[0].content[0] is installed
    assert checkpoint_data(response.payloads[0])["continuity_text_sha256"] != "bad"


def test_no_backward_search_into_history_when_current_projection_is_missing():
    message = _envelope()
    old = LLMPayload(ROLE.ASSISTANT, Text(message))
    response = SimpleNamespace(
        message=message,
        payloads=[
            old,
            LLMPayload(ROLE.USER, Text("next")),
            LLMPayload(ROLE.ASSISTANT, Text("different")),
        ],
    )
    with pytest.raises(ContextStewardshipError, match="missing"):
        isolate_model_checkpoint_output(response)
    assert response.payloads[0] is old


@pytest.mark.parametrize(
    "text",
    [
        "",
        "normal response",
        "quoted: " + _envelope(),
        "<subject_self_continuity_checkpoint>\nnot JSON\n</subject_self_continuity_checkpoint>",
    ],
)
def test_non_control_text_is_byte_preserved(text):
    assert quote_model_checkpoint_text(text) == text
