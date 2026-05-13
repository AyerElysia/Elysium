"""Tests for LLM token counting."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from src.kernel.llm.context import LLMContextManager
from src.kernel.llm.payload import LLMPayload, Text
from src.kernel.llm.payload.content import Audio, Image, Video
from src.kernel.llm.roles import ROLE
from src.kernel.llm.token_counter import _get_tiktoken_encoding, count_payload_tokens


def test_tiktoken_load_failure_uses_fallback(monkeypatch) -> None:
    """Token counting should degrade to an estimate when tiktoken cannot load data."""

    def raise_load_error(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("tiktoken data unavailable")

    _get_tiktoken_encoding.cache_clear()
    monkeypatch.setitem(
        sys.modules,
        "tiktoken",
        SimpleNamespace(
            encoding_for_model=raise_load_error,
            get_encoding=raise_load_error,
        ),
    )

    payloads = [LLMPayload(ROLE.USER, Text("网络不通时也不能因为 token 计数崩溃"))]

    try:
        tokens = count_payload_tokens(payloads, model_identifier="gpt-4")
    finally:
        _get_tiktoken_encoding.cache_clear()

    assert tokens > 0


def test_multimodal_base64_is_counted_as_placeholder() -> None:
    """Media base64 bodies must not be counted as plain text tokens."""
    huge_b64 = "QUJD" * 100_000
    payloads = [
        LLMPayload(
            ROLE.USER,
            [
                Text("看看这些媒体"),
                Image(f"base64|{huge_b64}"),
                Audio(f"base64|{huge_b64}", mime_type="audio/wav"),
                Video(f"base64|{huge_b64}", mime_type="video/mp4"),
            ],
        )
    ]

    tokens = count_payload_tokens(payloads, model_identifier="gpt-4")

    assert tokens < 300


def test_multimodal_payload_does_not_force_context_tail_drop() -> None:
    """A large media body should not make token trimming discard prior turns."""
    huge_b64 = "QUJD" * 100_000
    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("system")),
        LLMPayload(ROLE.USER, Text("上一轮用户消息")),
        LLMPayload(ROLE.ASSISTANT, Text("上一轮回复")),
        LLMPayload(ROLE.USER, [Text("这一轮发了表情包"), Image(f"base64|{huge_b64}")]),
    ]
    manager = LLMContextManager(max_payloads=100)

    trimmed = manager.maybe_trim(
        payloads,
        max_token_budget=500,
        token_counter=lambda items: count_payload_tokens(
            items,
            model_identifier="gpt-4",
        ),
    )

    assert trimmed == payloads
