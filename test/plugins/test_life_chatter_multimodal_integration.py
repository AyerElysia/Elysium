"""life_chatter 多模态接入集成测试。

覆盖：
- LifeChatter._compose_unread_user_content：启用/禁用、image/voice/video 注入
- 跨轮 dedup（失败重试场景）：相同 unread 二次 compose 不重复 extend 媒体
- _strip_transient_context 精确匹配：用户原文含 marker 不被误删
- 协议级：构造好的 USER payload 经 openai_client._payloads_to_openai_messages
  转换后，input_audio.format / image_url / video_url 字段正确
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.core.chatter import (
    LifeChatter,
    _is_native_multimodal_unsupported_error,
    _Phase,
    _WorkflowRuntime,
)
from plugins.life_engine.core.config import LifeEngineConfig
from src.core.models.message import Message, MessageType
from src.kernel.llm import Audio, Image, LLMPayload, ROLE, Text, Video
from src.kernel.llm.exceptions import UnsupportedModalityError
from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

import base64


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _png_b64() -> str:
    return (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )


def _wav_b64() -> str:
    return base64.b64encode(b"RIFF$\x00\x00\x00WAVEfmt ").decode()


def _mp4_b64() -> str:
    return base64.b64encode(b"\x00\x00\x00\x18ftypmp42").decode()


def _msg(message_id: str, **kwargs: Any) -> SimpleNamespace:
    base = dict(message_id=message_id, content=None, extra={}, media=None, message_type=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


class _FakeResponse:
    def __init__(self, payloads: list[LLMPayload] | None = None) -> None:
        self.payloads = payloads or []

    def add_payload(self, payload: LLMPayload) -> None:
        self.payloads.append(payload)


def _make_chatter(*, multimodal_enabled: bool | None = None, **mm_overrides: Any) -> LifeChatter:
    config = LifeEngineConfig()
    if multimodal_enabled is not None:
        config.multimodal.enabled = multimodal_enabled
    for k, v in mm_overrides.items():
        setattr(config.multimodal, k, v)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config, _service=None)
    return chatter


def _new_runtime() -> _WorkflowRuntime:
    return _WorkflowRuntime(
        response=_FakeResponse(),
        phase=_Phase.WAIT_USER,
        history_merged=False,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
    )


# ─── compose_unread_user_content ────────────────────────────────


def test_compose_disabled_returns_text_only() -> None:
    chatter = _make_chatter()
    rt = _new_runtime()
    msgs = [_msg("m1", media=[{"type": "voice", "data": _b64("a")}])]
    out = chatter._compose_unread_user_content(rt, msgs, "user prompt")
    assert len(out) == 1 and isinstance(out[0], Text)


def test_compose_can_enable_all_native_modalities() -> None:
    chatter = _make_chatter(
        multimodal_enabled=True,
        native_audio=True,
        native_video=True,
    )
    rt = _new_runtime()
    msgs = [
        _msg("m1", media=[{"type": "image", "data": _png_b64()}]),
        _msg("m2", media=[{"type": "voice", "data": _wav_b64(), "format": "wav"}]),
        _msg("m3", media=[{"type": "video", "data": _mp4_b64(), "mime_type": "video/mp4"}]),
    ]
    out = chatter._compose_unread_user_content(rt, msgs, "hi")
    assert any(isinstance(p, Image) for p in out)
    assert any(isinstance(p, Audio) for p in out)
    assert any(isinstance(p, Video) for p in out)


def test_compose_can_disable_voice_video_explicitly() -> None:
    chatter = _make_chatter(multimodal_enabled=True, native_audio=False, native_video=False)
    rt = _new_runtime()
    msgs = [
        _msg("m1", media=[{"type": "image", "data": _png_b64()}]),
        _msg("m2", media=[{"type": "voice", "data": _wav_b64(), "format": "wav"}]),
        _msg("m3", media=[{"type": "video", "data": _mp4_b64(), "mime_type": "video/mp4"}]),
    ]
    out = chatter._compose_unread_user_content(rt, msgs, "hi")
    assert any(isinstance(p, Image) for p in out)
    assert all(not isinstance(p, Audio) for p in out)
    assert all(not isinstance(p, Video) for p in out)


def test_compose_includes_emoji_by_default() -> None:
    chatter = _make_chatter(multimodal_enabled=True)
    rt = _new_runtime()
    msgs = [_msg("m1", media=[{"type": "emoji", "data": _png_b64()}])]
    out = chatter._compose_unread_user_content(rt, msgs, "hi")
    assert any(isinstance(p, Image) for p in out)


def test_compose_can_disable_emoji_explicitly() -> None:
    chatter = _make_chatter(multimodal_enabled=True, native_emoji=False)
    rt = _new_runtime()
    msgs = [_msg("m1", media=[{"type": "emoji", "data": _png_b64()}])]
    out = chatter._compose_unread_user_content(rt, msgs, "hi")
    assert all(not isinstance(p, Image) for p in out)
    assert len(out) == 1 and isinstance(out[0], Text)


def test_compose_dedup_across_retries() -> None:
    """失败重试场景：相同 unread 二次 compose 不重复发出媒体。"""
    chatter = _make_chatter(multimodal_enabled=True)
    rt = _new_runtime()
    msgs = [_msg("m1", media=[{"type": "image", "data": _png_b64()}])]

    first = chatter._compose_unread_user_content(rt, msgs, "p1")
    second = chatter._compose_unread_user_content(rt, msgs, "p2")

    assert sum(isinstance(p, Image) for p in first) == 1
    assert sum(isinstance(p, Image) for p in second) == 0
    # 第二次应只剩纯文本
    assert all(isinstance(p, Text) for p in second)


@pytest.mark.asyncio
async def test_delta_unread_native_image_is_restored_after_model_failure(
    monkeypatch,
) -> None:
    """增量 unread 应走原生 compose，模型失败后同一图片仍可再次注入。"""
    LifeChatter.reset_global_runtime()
    image_seen_by_model: list[bool] = []

    class FailingResponse(_FakeResponse):
        async def send(self, *, stream: bool = False):
            assert stream is False
            image_seen_by_model.append(
                any(
                    isinstance(part, Image)
                    for payload in self.payloads
                    for part in payload.content
                )
            )
            raise RuntimeError("model failed")

    old_msg = Message(
        message_id="old-1",
        content="先前消息",
        processed_plain_text="先前消息",
        sender_role="other",
        stream_id="stream-a",
    )
    image_msg = Message(
        message_id="image-1",
        content={"media": [{"type": "image", "data": _png_b64()}]},
        processed_plain_text="请看这张新图",
        message_type=MessageType.IMAGE,
        sender_role="other",
        stream_id="stream-a",
    )
    response = FailingResponse([LLMPayload(ROLE.USER, Text("existing"))])
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.FOLLOW_UP,
        history_merged=True,
        unreads=[old_msg],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[old_msg],
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = _make_chatter(multimodal_enabled=True)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], [old_msg, image_msg]

    async def immediate_model_turn(awaitable):
        return await awaitable

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "_await_model_turn", immediate_model_turn)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert result.__class__.__name__ == "Failure"
    assert image_seen_by_model == [True]
    assert rt.phase == _Phase.WAIT_USER
    assert rt.media_seen == set()
    assert all(
        not isinstance(part, Image)
        for payload in response.payloads
        for part in payload.content
    )

    # 上游 unread 尚未 flush；恢复后再次注入时不能被 media_seen 错误去重。
    delta = await chatter._inject_delta_unreads_if_any(
        rt,
        SimpleNamespace(stream_id="stream-a"),
    )
    assert image_msg in delta
    assert any(
        isinstance(part, Image)
        for payload in response.payloads
        for part in payload.content
    )

    LifeChatter.reset_global_runtime()


def test_compose_skips_invalid_image_payload() -> None:
    chatter = _make_chatter(multimodal_enabled=True)
    rt = _new_runtime()
    msgs = [_msg("m1", media=[{"type": "image", "data": _b64("not an image")}])]
    out = chatter._compose_unread_user_content(rt, msgs, "hi")
    assert all(not isinstance(p, Image) for p in out)
    assert any(
        isinstance(p, Text) and "格式不支持" in p.text
        for p in out
    )


def test_compose_can_include_recent_history_image() -> None:
    chatter = _make_chatter(multimodal_enabled=True, include_history_media=True)
    rt = _new_runtime()
    history_image = _msg(
        "drawn-1",
        content=_png_b64(),
        processed_plain_text="[内部：已发送画作]",
        message_type=MessageType.IMAGE,
    )
    stream = SimpleNamespace(context=SimpleNamespace(history_messages=[history_image]))
    out = chatter._compose_unread_user_content(
        rt,
        [_msg("m2", content="刚才那张图你自己看看")],
        "hi",
        stream,
    )
    assert any(isinstance(p, Image) for p in out)
    assert any(
        isinstance(p, Text) and "drawn-1" in p.text
        for p in out
    )


def test_compose_silk_audio_downgraded_to_text_placeholder() -> None:
    chatter = _make_chatter(multimodal_enabled=True, native_audio=True)
    rt = _new_runtime()
    msgs = [_msg("m1", media=[{"type": "voice", "data": _b64("v"), "mime_type": "audio/silk"}])]
    out = chatter._compose_unread_user_content(rt, msgs, "hi")
    assert all(not isinstance(p, Audio) for p in out)
    assert any(isinstance(p, Text) and p.text == "[语音消息]" for p in out)


# ─── _strip_transient_context 精确匹配 ─────────────────────────


def test_strip_transient_context_does_not_remove_user_text_with_marker_inline() -> None:
    """用户原文中嵌 <transient_life_context> 字样不应被误删。"""
    chatter = LifeChatter.__new__(LifeChatter)
    user_authored = Text("注意：模板里我引用了 <transient_life_context> 这种标签作为示例")
    transient = Text("<transient_life_context>\nrt info\n</transient_life_context>")
    response = _FakeResponse([LLMPayload(ROLE.USER, [user_authored, transient])])
    chatter._strip_transient_context(response)
    remaining = response.payloads[0].content
    assert user_authored in remaining
    assert transient not in remaining


def test_strip_transient_context_removes_only_trailing_wrapper() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    transient_a = Text("<transient_life_context>\nA\n</transient_life_context>")
    transient_b = Text("<transient_life_context>\nB\n</transient_life_context>")
    response = _FakeResponse([LLMPayload(ROLE.USER, [Text("body"), transient_a, transient_b])])
    chatter._strip_transient_context(response)
    remaining = response.payloads[0].content
    assert transient_a not in remaining and transient_b not in remaining
    assert any(isinstance(p, Text) and p.text == "body" for p in remaining)


# ─── 协议级序列化 ───────────────────────────────────────────────


def test_user_payload_serializes_audio_with_correct_format() -> None:
    chatter = _make_chatter(multimodal_enabled=True, native_audio=True)
    rt = _new_runtime()
    msgs = [_msg("m1", media=[{"type": "voice", "data": _wav_b64(), "format": "wav"}])]
    content = chatter._compose_unread_user_content(rt, msgs, "请听这条语音")
    payload = LLMPayload(ROLE.USER, content)
    messages, _ = _payloads_to_openai_messages([payload])
    parts = messages[0]["content"]
    audio_parts = [p for p in parts if isinstance(p, dict) and p.get("type") == "input_audio"]
    assert audio_parts
    assert audio_parts[0]["input_audio"]["format"] == "wav"


def test_user_payload_keeps_video_for_provider_rejection() -> None:
    chatter = _make_chatter(multimodal_enabled=True, native_video=True)
    rt = _new_runtime()
    msgs = [
        _msg("m1", media=[{"type": "image", "data": _png_b64()}]),
        _msg("m2", media=[{"type": "video", "data": _mp4_b64(), "mime_type": "video/mp4"}]),
    ]
    content = chatter._compose_unread_user_content(rt, msgs, "看看")
    assert any(isinstance(part, Image) for part in content)
    assert any(isinstance(part, Video) for part in content)
    with pytest.raises(UnsupportedModalityError):
        _payloads_to_openai_messages([LLMPayload(ROLE.USER, content)])


def test_native_multimodal_error_detection_includes_wrapped_kernel_error() -> None:
    direct_error = UnsupportedModalityError("没有模型支持 image 模态")
    assert _is_native_multimodal_unsupported_error(direct_error) is True

    try:
        try:
            raise direct_error
        except UnsupportedModalityError as cause:
            raise RuntimeError("provider wrapper") from cause
    except RuntimeError as wrapped_error:
        assert _is_native_multimodal_unsupported_error(wrapped_error) is True


class _AwaitableFallbackResponse:
    """Minimal response double for the one-shot media fallback retry."""

    def __init__(
        self,
        payloads: list[LLMPayload],
        sent_snapshots: list[list[LLMPayload]] | None = None,
    ) -> None:
        self.payloads = payloads
        self.sent_snapshots = sent_snapshots if sent_snapshots is not None else []
        self.send_count = 0

    def __await__(self):
        async def resolve() -> "_AwaitableFallbackResponse":
            return self

        return resolve().__await__()

    async def send(self, *, stream: bool = False) -> "_AwaitableFallbackResponse":
        assert stream is False
        self.send_count += 1
        snapshot = [
            LLMPayload(payload.role, list(payload.content))
            for payload in self.payloads
        ]
        self.sent_snapshots.append(snapshot)
        return _AwaitableFallbackResponse(snapshot, self.sent_snapshots)


@pytest.mark.asyncio
async def test_media_text_fallback_replaces_all_native_media_once(monkeypatch) -> None:
    image = Image(_png_b64())
    audio = Audio(_wav_b64())
    video = Video(_mp4_b64(), mime_type="video/mp4")
    response = _AwaitableFallbackResponse(
        [
            LLMPayload(
                ROLE.USER,
                [Text("请处理"), image, audio, image, video],
            )
        ]
    )
    runtime = _new_runtime()
    runtime.response = response
    observed_kinds: list[str] = []

    async def describe(part: Image | Audio | Video) -> str:
        observed_kinds.append(part.kind.value)
        return f"{part.kind.value}-description"

    monkeypatch.setattr(
        LifeChatter,
        "_describe_native_media_for_fallback",
        staticmethod(describe),
    )

    fallback_response = await LifeChatter._retry_model_turn_with_media_text_fallback(
        runtime,
        "请基于观察结果回答",
    )
    runtime.response = fallback_response

    assert response.send_count == 1
    assert observed_kinds == ["image", "audio", "video"]
    sent_payloads = response.sent_snapshots[0]
    assert all(
        not isinstance(part, (Image, Audio, Video))
        for payload in sent_payloads
        for part in payload.content
    )
    sent_text = "\n".join(
        part.text
        for payload in sent_payloads
        for part in payload.content
        if isinstance(part, Text)
    )
    assert "image-description" in sent_text
    assert "audio-description" in sent_text
    assert "video-description" in sent_text
    assert LifeChatter._has_native_media(runtime.response) is False
    assert runtime.response.payloads[0].content[0].text == "请处理"
