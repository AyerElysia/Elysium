from __future__ import annotations

import base64
from types import SimpleNamespace

from plugins.life_engine.core.multimodal import MediaItem, build_multimodal_content
from src.core.media_planner import (
    build_media_text,
    build_native_content,
    iter_message_attachments,
    media_dedup_key,
    plan_media,
)
from src.core.models.media import MediaAttachment, MediaSegmentType
from src.kernel.llm import Audio, Image, Text, Video
from src.kernel.llm.payload.media import MediaKind, MediaRef


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_WAV_BYTES = b"RIFF$\x00\x00\x00WAVEfmt "
_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42"
_AMR_BYTES = b"#!AMR\n"


def _attachment(
    segment_type: MediaSegmentType,
    data: bytes,
    *,
    duration: float | None = None,
    source_message_id: str | None = None,
) -> MediaAttachment:
    kind = {
        MediaSegmentType.IMAGE: MediaKind.IMAGE,
        MediaSegmentType.EMOJI: MediaKind.IMAGE,
        MediaSegmentType.VOICE: MediaKind.AUDIO,
        MediaSegmentType.VIDEO: MediaKind.VIDEO,
    }[segment_type]
    mime_type = {
        MediaSegmentType.IMAGE: "image/png",
        MediaSegmentType.EMOJI: "image/png",
        MediaSegmentType.VOICE: "audio/wav",
        MediaSegmentType.VIDEO: "video/mp4",
    }[segment_type]
    return MediaAttachment(
        segment_type,
        MediaRef.from_bytes(
            data,
            kind=kind,
            mime_type=mime_type,
            duration=duration,
            source_message_id=source_message_id,
        ),
    )


def _message(message_id: str, *attachments: MediaAttachment, **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        attachments=list(attachments),
        content=kwargs.get("content"),
        extra=kwargs.get("extra", {}),
    )


def test_canonical_attachments_take_precedence_and_plan_in_order() -> None:
    image = _attachment(MediaSegmentType.IMAGE, _PNG_BYTES, source_message_id="canonical")
    message = _message(
        "canonical",
        image,
        content={"media": [{"type": "image", "data": "base64|not-a-real-image"}]},
    )

    assert list(iter_message_attachments(message)) == [image]
    planned = plan_media([message])
    assert len(planned) == 1
    assert planned[0].attachment is image
    assert planned[0].segment_type is MediaSegmentType.IMAGE
    assert planned[0].source_message_id == "canonical"


def test_sha256_dedup_is_stable_and_keeps_segment_type_boundary() -> None:
    image_a = _attachment(MediaSegmentType.IMAGE, _PNG_BYTES)
    image_b = _attachment(MediaSegmentType.IMAGE, _PNG_BYTES)
    emoji = _attachment(MediaSegmentType.EMOJI, _PNG_BYTES)
    assert media_dedup_key(image_a) == ("image", image_a.media_ref.sha256)

    planned = plan_media(
        [_message("one", image_a), _message("two", image_b), _message("three", emoji)],
        max_images=4,
    )
    assert [(item.segment_type, item.attachment.media_ref.sha256) for item in planned] == [
        (MediaSegmentType.IMAGE, image_a.media_ref.sha256),
        (MediaSegmentType.EMOJI, emoji.media_ref.sha256),
    ]


def test_independent_budgets_total_bytes_item_bytes_and_duration() -> None:
    image = _attachment(MediaSegmentType.IMAGE, _PNG_BYTES)
    video = _attachment(MediaSegmentType.VIDEO, _MP4_BYTES)
    audio = _attachment(MediaSegmentType.VOICE, _WAV_BYTES, duration=61)
    planned = plan_media(
        [_message("image", image), _message("video", video), _message("audio", audio)],
        max_images=1,
        max_videos=1,
        max_audios=1,
        max_total_bytes=len(_PNG_BYTES) + len(_MP4_BYTES),
        audio_max_seconds=60,
    )
    assert [item.segment_type for item in planned] == [
        MediaSegmentType.IMAGE,
        MediaSegmentType.VIDEO,
    ]

    item_limited = plan_media(
        [_message("image", image), _message("video", video)],
        max_item_bytes=len(_MP4_BYTES),
    )
    assert [item.segment_type for item in item_limited] == [MediaSegmentType.VIDEO]


def test_real_png_wav_mp4_build_native_content_and_text_fallback() -> None:
    planned = plan_media(
        [
            _message("image", _attachment(MediaSegmentType.IMAGE, _PNG_BYTES)),
            _message("audio", _attachment(MediaSegmentType.VOICE, _WAV_BYTES)),
            _message("video", _attachment(MediaSegmentType.VIDEO, _MP4_BYTES)),
        ]
    )
    content = build_native_content("hello", planned)
    assert [type(part) for part in content] == [
        Text,
        Text,
        Image,
        Text,
        Audio,
        Text,
        Video,
    ]
    assert build_media_text("hello", planned) == "hello[图片][语音][视频]"


def test_unsupported_audio_and_invalid_legacy_data_only_fall_back_to_text() -> None:
    unsupported = MediaAttachment(
        MediaSegmentType.VOICE,
        MediaRef.from_bytes(
            _AMR_BYTES,
            kind=MediaKind.AUDIO,
            mime_type="audio/amr",
        ),
    )
    planned = plan_media([_message("voice", unsupported)])
    content = build_native_content("", planned)
    assert [type(part) for part in content] == [Text]
    assert content[0].text == "[语音消息]"

    invalid = MediaItem("image", "base64|not-a-real-image", "bad")
    fallback = build_multimodal_content("", [invalid])
    assert [type(part) for part in fallback] == [Text]
    assert "格式不支持" in fallback[0].text
