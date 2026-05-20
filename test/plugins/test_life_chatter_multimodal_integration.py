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

from plugins.life_engine.core.chatter import LifeChatter, _Phase, _WorkflowRuntime
from plugins.life_engine.core.config import LifeEngineConfig
from src.core.models.message import MessageType
from src.kernel.llm import Audio, Image, LLMPayload, ROLE, Text, Video
from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

import base64


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _png_b64() -> str:
    return (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )


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
    chatter = _make_chatter(multimodal_enabled=True)
    rt = _new_runtime()
    msgs = [
        _msg("m1", media=[{"type": "image", "data": _png_b64()}]),
        _msg("m2", media=[{"type": "voice", "data": _b64("vo"), "format": "wav"}]),
        _msg("m3", media=[{"type": "video", "data": _b64("vid"), "mime_type": "video/mp4"}]),
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
        _msg("m2", media=[{"type": "voice", "data": _b64("vo"), "format": "wav"}]),
        _msg("m3", media=[{"type": "video", "data": _b64("vid"), "mime_type": "video/mp4"}]),
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
    msgs = [_msg("m1", media=[{"type": "voice", "data": _b64("au"), "format": "wav"}])]
    content = chatter._compose_unread_user_content(rt, msgs, "请听这条语音")
    payload = LLMPayload(ROLE.USER, content)
    messages, _ = _payloads_to_openai_messages([payload])
    parts = messages[0]["content"]
    audio_parts = [p for p in parts if isinstance(p, dict) and p.get("type") == "input_audio"]
    assert audio_parts
    assert audio_parts[0]["input_audio"]["format"] == "wav"


def test_user_payload_serializes_image_and_video() -> None:
    chatter = _make_chatter(multimodal_enabled=True, native_video=True)
    rt = _new_runtime()
    msgs = [
        _msg("m1", media=[{"type": "image", "data": _png_b64()}]),
        _msg("m2", media=[{"type": "video", "data": _b64("vi"), "mime_type": "video/webm"}]),
    ]
    payload = LLMPayload(ROLE.USER, chatter._compose_unread_user_content(rt, msgs, "看看"))
    messages, _ = _payloads_to_openai_messages([payload])
    parts = [p for p in messages[0]["content"] if isinstance(p, dict)]
    image_url_parts = [p for p in parts if p.get("type") == "image_url"]
    urls = [p["image_url"]["url"] for p in image_url_parts]
    # OpenAI 兼容协议下 Video 也通过 image_url 通道携带 data:video/... URL
    assert any(u.startswith("data:image/") for u in urls)
    assert any(u.startswith("data:video/") for u in urls)
