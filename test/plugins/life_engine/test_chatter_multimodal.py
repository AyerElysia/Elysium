"""life_engine multimodal 模块单元测试。

覆盖：
- MediaBudget 三类预算独立、互不抢占
- extract_media_from_messages 的类型归一化（voice→audio）/ silk-amr 数据保留 + build 阶段降级
- audio_max_seconds 时长过滤
- _classify_media 的简短 format ("wav") 自动补全为 "audio/wav"
- build_multimodal_content 的占位文本与降级路径
- get_media_list 的多源（content.media / extra.media / msg.media）合并去重
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.core.multimodal import (
    MediaBudget,
    MediaItem,
    build_multimodal_content,
    extract_media_from_messages,
    get_media_list,
)
from src.kernel.llm import Audio, Image, Text, Video


def _b64(s: str = "hello") -> str:
    return base64.b64encode(s.encode()).decode()


def _png_b64() -> str:
    return (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )


def _wav_b64() -> str:
    return base64.b64encode(b"RIFF$\x00\x00\x00WAVEfmt ").decode()


def _mp3_b64() -> str:
    return base64.b64encode(b"ID3\x04\x00\x00\x00\x00\x00\x00").decode()


def _amr_b64() -> str:
    return base64.b64encode(b"#!AMR\n").decode()


def _mp4_b64() -> str:
    return base64.b64encode(b"\x00\x00\x00\x18ftypmp42").decode()


def _msg(message_id: str, *, content: Any = None, extra: Any = None, media: Any = None,
         message_type: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        content=content,
        extra=extra or {},
        media=media,
        message_type=message_type,
    )


# ─── MediaBudget ────────────────────────────────────────────────


class TestMediaBudget:
    def test_independent_quotas(self) -> None:
        b = MediaBudget(max_images=2, max_videos=1, max_audios=2)
        assert b.consume("image") and b.consume("emoji") and not b.consume("image")
        assert b.consume("video") and not b.consume("video")
        assert b.consume("audio") and b.consume("audio") and not b.consume("audio")

    def test_can_take_does_not_consume(self) -> None:
        b = MediaBudget(max_images=1, max_videos=0, max_audios=0)
        assert b.can_take("image")
        assert b.can_take("image")  # 仍未被消耗
        assert b.consume("image")
        assert not b.can_take("image")

    def test_unknown_type_rejected(self) -> None:
        b = MediaBudget()
        assert not b.can_take("file")
        assert not b.consume("file")

    def test_is_exhausted(self) -> None:
        b = MediaBudget(max_images=1, max_videos=1, max_audios=1)
        b.consume("image"); b.consume("video"); b.consume("audio")
        assert b.is_exhausted()


# ─── extract_media_from_messages ────────────────────────────────


class TestExtract:
    def test_voice_normalized_to_audio_with_default_mime(self) -> None:
        m = _msg("m1", media=[{"type": "voice", "data": _b64()}])
        items = extract_media_from_messages([m], MediaBudget(max_audios=2))
        assert len(items) == 1
        assert items[0].media_type == "audio"
        assert items[0].mime_type == "audio/wav"
        assert items[0].source_message_id == "m1"

    def test_record_alias_maps_to_audio(self) -> None:
        m = _msg("m1", media=[{"type": "record", "data": _b64()}])
        items = extract_media_from_messages([m], MediaBudget())
        assert items and items[0].media_type == "audio"

    def test_short_format_completed_to_full_mime(self) -> None:
        m = _msg("m1", media=[{"type": "voice", "data": _b64(), "format": "wav"}])
        items = extract_media_from_messages([m], MediaBudget())
        assert items[0].mime_type == "audio/wav"

    def test_silk_mime_preserved_then_downgraded_in_build(self) -> None:
        """silk 在 extract 阶段保留 mime；build 阶段才降级。"""
        m = _msg("m1", media=[{"type": "voice", "data": _b64(), "mime_type": "audio/silk"}])
        items = extract_media_from_messages([m], MediaBudget())
        assert items[0].mime_type == "audio/silk"
        out = build_multimodal_content("hi", items)
        # 期待：[Text("hi"), Text("[语音消息]")] —— 没有 Audio
        assert all(not isinstance(p, Audio) for p in out)
        assert any(isinstance(p, Text) and p.text == "[语音消息]" for p in out)

    def test_audio_too_long_filtered_out(self) -> None:
        m = _msg("m1", media=[{"type": "voice", "data": _b64(), "duration": 999}])
        items = extract_media_from_messages([m], MediaBudget(), audio_max_seconds=60)
        assert items == []

    def test_disabled_audio_skips(self) -> None:
        m = _msg("m1", media=[
            {"type": "voice", "data": _b64()},
            {"type": "image", "data": _b64()},
        ])
        items = extract_media_from_messages([m], MediaBudget(), enable_audio=False)
        assert all(it.media_type != "audio" for it in items)
        assert any(it.media_type == "image" for it in items)

    def test_disabled_emoji_skips_but_keeps_image(self) -> None:
        m = _msg("m1", media=[
            {"type": "emoji", "data": _b64("e")},
            {"type": "image", "data": _b64("i")},
        ])
        items = extract_media_from_messages([m], MediaBudget(), enable_emoji=False)
        assert all(it.media_type != "emoji" for it in items)
        assert any(it.media_type == "image" for it in items)

    def test_budget_caps_per_type(self) -> None:
        msgs = [
            _msg(f"m{i}", media=[{"type": "image", "data": _b64(str(i))}]) for i in range(5)
        ]
        items = extract_media_from_messages(msgs, MediaBudget(max_images=2))
        assert len(items) == 2

    def test_budget_does_not_block_other_types(self) -> None:
        m = _msg("m1", media=[
            {"type": "image", "data": _b64("a")},
            {"type": "image", "data": _b64("b")},  # 第 2 张超额（max_images=1）
            {"type": "voice", "data": _b64("c")},
        ])
        items = extract_media_from_messages([m], MediaBudget(max_images=1, max_audios=2))
        types = [it.media_type for it in items]
        assert types.count("image") == 1
        assert types.count("audio") == 1

    def test_empty_data_skipped(self) -> None:
        m = _msg("m1", media=[{"type": "voice", "data": ""}])
        items = extract_media_from_messages([m], MediaBudget())
        assert items == []

    def test_data_url_passes_through(self) -> None:
        url = f"data:audio/wav;base64,{_b64()}"
        m = _msg("m1", media=[{"type": "voice", "data": url}])
        items = extract_media_from_messages([m], MediaBudget())
        assert items[0].raw_data == url


# ─── build_multimodal_content ──────────────────────────────────


class TestBuild:
    def test_image_emoji_video_audio_mixed(self) -> None:
        items = [
            MediaItem("image", _png_b64(), "m1"),
            MediaItem("emoji", _png_b64(), "m2"),
            MediaItem("video", _mp4_b64(), "m3", mime_type="video/mp4"),
            MediaItem("audio", _wav_b64(), "m4", mime_type="audio/wav"),
        ]
        out = build_multimodal_content("hi", items)
        types = [type(p).__name__ for p in out]
        # hi + (Text,Image) + (Text,Image) + (Text,Video) + (Text,Audio)
        assert types == ["Text", "Text", "Image", "Text", "Image", "Text", "Video", "Text", "Audio"]

    def test_empty_text_omitted(self) -> None:
        items = [MediaItem("image", _png_b64(), "m1")]
        out = build_multimodal_content("", items)
        assert not any(isinstance(p, Text) and p.text == "" for p in out)
        assert any(isinstance(p, Image) for p in out)

    def test_audio_mp3_supported(self) -> None:
        items = [MediaItem("audio", _mp3_b64(), "m1", mime_type="audio/mpeg")]
        out = build_multimodal_content("", items)
        assert any(isinstance(p, Audio) for p in out)

    def test_audio_amr_downgraded(self) -> None:
        items = [MediaItem("audio", _amr_b64(), "m1", mime_type="audio/amr")]
        out = build_multimodal_content("", items, unsupported_audio_placeholder="[未知语音]")
        assert all(not isinstance(p, Audio) for p in out)
        assert any(isinstance(p, Text) and p.text == "[未知语音]" for p in out)


# ─── get_media_list ─────────────────────────────────────────────


class TestGetMediaList:
    def test_dedup_across_sources(self) -> None:
        same = {"type": "image", "data": _b64()}
        m = _msg("m1", content={"media": [same]}, extra={"media": [same]}, media=[same])
        items = get_media_list(m)
        assert len(items) == 1

    def test_emoji_string_content_promoted(self) -> None:
        long_content = "a" * 200
        m = _msg("m1", content=long_content, message_type="emoji")
        items = get_media_list(m)
        assert items and items[0]["type"] == "emoji"
        assert items[0]["data"].startswith("base64|")

    def test_no_media_returns_empty(self) -> None:
        m = _msg("m1", content="just text")
        assert get_media_list(m) == []
