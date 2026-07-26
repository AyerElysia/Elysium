from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.models.media import MediaAttachment, MediaSegmentType
from src.core.models.message import Message, MessageType
from src.core.transport.message_receive import converter as converter_module
from src.core.transport.message_receive.converter import MessageConverter
from src.kernel.llm.payload.media import MediaKind, MediaRef


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_WAV_BYTES = b"RIFF$\x00\x00\x00WAVEfmt "
_FILE_BYTES = b"canonical file payload"


def _legacy_base64(data: bytes) -> str:
    return "base64|" + base64.b64encode(data).decode("ascii")


def _build_private_envelope(segments: list[dict], *, message_id: str = "msg-media-1") -> dict:
    return {
        "message_info": {
            "message_id": message_id,
            "time": 1710000000.0,
            "platform": "qq",
            "user_info": {
                "user_id": "user-media",
                "user_nickname": "Alice",
            },
            "extra": {},
        },
        "message_segment": segments,
        "raw_message": {"source": "unit-test"},
    }


def _get_segment_types(envelope: dict) -> list[str]:
    return [str(segment.get("type", "")) for segment in _get_segments(envelope)]


def _get_segments(envelope: dict) -> list[dict]:
    segments = envelope.get("message_segment")
    assert isinstance(segments, list)
    return [segment for segment in segments if isinstance(segment, dict)]


def _get_media_segments(envelope: dict) -> list[dict]:
    return [
        segment
        for segment in _get_segments(envelope)
        if segment.get("type") in {"image", "emoji", "voice", "video", "file"}
    ]


async def test_message_to_envelope_preserves_text_and_media_from_dict() -> None:
    """纯 legacy 出站仍保留文本、content.media 与 extra.media。"""
    converter = MessageConverter()
    message = Message(
        message_id="msg-100",
        content={
            "text": "请看图",
            "media": [
                {"type": "image", "data": "base64|QUJD", "filename": "photo.png"},
                {
                    "type": "video",
                    "data": {"base64": "base64|RkZGRg==", "filename": "clip.mp4"},
                },
            ],
        },
        processed_plain_text=None,
        message_type=MessageType.TEXT,
        sender_id="user-100",
        sender_name="Alice",
        platform="qq",
        chat_type="private",
        stream_id="stream-100",
        target_user_id="user-100",
        media=[
            {"type": "emoji", "data": "base64|R0hJ", "filename": "face.gif"},
        ],
    )

    envelope = await converter.message_to_envelope(message)

    assert _get_segment_types(envelope) == ["text", "image", "video", "emoji"]
    segments = _get_segments(envelope)
    assert segments[0]["data"] == "请看图"
    assert segments[1]["data"] == "QUJD"
    assert segments[2]["data"] == "RkZGRg=="
    assert segments[3]["data"] == "R0hJ"


async def test_message_to_envelope_strips_base64_prefix_for_media() -> None:
    """既有非 file 媒体前缀剥离行为保持不变。"""
    converter = MessageConverter()
    message = Message(
        message_id="msg-101",
        content={"data": "base64|iVBORw0KGgoAAA"},
        message_type=MessageType.IMAGE,
        sender_id="user-101",
        sender_name="Bob",
        platform="qq",
        chat_type="private",
        stream_id="stream-101",
        target_user_id="user-101",
    )

    envelope = await converter.message_to_envelope(message)

    assert _get_segment_types(envelope)[0] == "image"
    assert _get_segments(envelope)[0]["data"] == "iVBORw0KGgoAAA"


async def test_envelope_to_message_builds_nested_canonical_attachments() -> None:
    """嵌套媒体只保留占位文本，并统一构造带 source message ID 的附件。"""
    converter = MessageConverter()
    envelope = _build_private_envelope(
        [
            {"type": "text", "data": "请看："},
            {
                "type": "seglist",
                "data": [{"type": "image", "data": _legacy_base64(_PNG_BYTES)}],
            },
            {
                "type": "reply",
                "data": [
                    {
                        "type": "voice",
                        "data": {
                            "base64": _legacy_base64(_WAV_BYTES),
                            "filename": "sample.wav",
                            "mime_type": "audio/wav",
                        },
                    }
                ],
            },
        ],
        message_id="msg-nested-media",
    )

    message = await converter.envelope_to_message(envelope)

    assert message.message_type is MessageType.IMAGE
    assert message.processed_plain_text == "请看：[图片]「回复：[语音文件:sample.wav]」"
    assert [attachment.segment_type for attachment in message.attachments] == [
        MediaSegmentType.IMAGE,
        MediaSegmentType.VOICE,
    ]
    assert message.attachments[0].media_ref.data == _PNG_BYTES
    assert message.attachments[1].media_ref.data == _WAV_BYTES
    assert all(
        attachment.media_ref.source_message_id == "msg-nested-media"
        for attachment in message.attachments
    )
    assert isinstance(message.content, dict)
    assert message.content["media"] == message.extra["media"]
    assert "media_errors" not in message.extra


async def test_envelope_to_message_prefixes_extra_keys_that_collide_with_message_fields() -> None:
    """适配器 extra 不能覆盖显式 Message 字段或 converter 兼容字段。"""
    converter = MessageConverter()
    envelope = _build_private_envelope(
        [{"type": "text", "data": "爱莉爱莉"}],
        message_id="msg-extra-collision",
    )
    envelope["message_info"]["platform"] = "feishu"
    envelope["message_info"]["extra"] = {
        "message_type": "adapter-message-type",
        "platform": "adapter-platform",
        "group_id": "adapter-group",
        "attachments": ["adapter-attachment"],
        "extra": {"adapter": True},
        "media": ["adapter-media"],
        "media_errors": ["adapter-error"],
        "custom_field": "custom-value",
    }

    message = await converter.envelope_to_message(envelope)

    assert message.platform == "feishu"
    assert message.message_type is MessageType.TEXT
    assert message.attachments == []
    assert message.extra["media"] == []
    assert message.extra["adapter_message_type"] == "adapter-message-type"
    assert message.extra["adapter_platform"] == "adapter-platform"
    assert message.extra["adapter_group_id"] == "adapter-group"
    assert message.extra["adapter_attachments"] == ["adapter-attachment"]
    assert message.extra["adapter_extra"] == {"adapter": True}
    assert message.extra["adapter_media"] == ["adapter-media"]
    assert message.extra["adapter_media_errors"] == ["adapter-error"]
    assert message.extra["custom_field"] == "custom-value"


async def test_invalid_media_is_retained_with_body_safe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法媒体不丢消息或 legacy item，错误和日志均不包含媒体 body。"""
    warnings: list[str] = []
    monkeypatch.setattr(
        converter_module,
        "logger",
        SimpleNamespace(warning=warnings.append),
    )
    secret_payload = "U0VDUkVULVBBWUxPQUQ="
    legacy_value = f"base64|{secret_payload}"
    envelope = _build_private_envelope(
        [{"type": "image", "data": legacy_value}],
        message_id="msg-invalid-media",
    )

    message = await MessageConverter().envelope_to_message(envelope)

    assert message.message_type is MessageType.IMAGE
    assert message.processed_plain_text == "[图片]"
    assert message.attachments == []
    assert isinstance(message.content, dict)
    assert message.content["media"] == [{"type": "image", "data": legacy_value}]
    assert message.extra["media"] == message.content["media"]
    assert message.extra["media_errors"] == [
        {"index": 0, "type": "image", "error": "MediaValidationError"}
    ]
    safe_output = json.dumps(message.extra["media_errors"], ensure_ascii=False) + "".join(warnings)
    assert secret_payload not in safe_output
    assert legacy_value not in safe_output
    assert "SECRET-PAYLOAD" not in safe_output
    assert "base64" not in safe_output.lower()


@pytest.mark.parametrize("encode_as_json", [False, True])
async def test_file_handler_preserves_source_fields_without_mutating_input(
    encode_as_json: bool,
) -> None:
    """file 字典及 JSON 字典保留所有 source 字段，并补齐 aliases。"""
    converter = MessageConverter()
    file_data = {
        "filename": "report.bin",
        "file_size": len(_FILE_BYTES),
        "file_id": "file-42",
        "path": "/managed/report.bin",
        "url": "https://example.invalid/report.bin",
        "base64": _legacy_base64(_FILE_BYTES),
        "nested": {"storage": {"bucket": "archive"}},
    }
    original = copy.deepcopy(file_data)
    wire_data: object = json.dumps(file_data) if encode_as_json else file_data
    envelope = _build_private_envelope(
        [{"type": "file", "data": wire_data}],
        message_id=f"msg-file-{encode_as_json}",
    )

    message = await converter.envelope_to_message(envelope)

    assert file_data == original
    assert isinstance(message.content, dict)
    preserved = message.content["media"][0]["data"]
    assert preserved["filename"] == "report.bin"
    assert preserved["file_size"] == len(_FILE_BYTES)
    assert preserved["file_id"] == "file-42"
    assert preserved["path"] == "/managed/report.bin"
    assert preserved["url"] == "https://example.invalid/report.bin"
    assert preserved["base64"] == _legacy_base64(_FILE_BYTES)
    assert preserved["nested"] == {"storage": {"bucket": "archive"}}
    assert preserved["name"] == "report.bin"
    assert preserved["size"] == len(_FILE_BYTES)
    assert preserved["id"] == "file-42"
    if not encode_as_json:
        assert preserved is not file_data
        assert preserved["nested"] is not file_data["nested"]
    assert len(message.attachments) == 1
    assert message.attachments[0].segment_type is MediaSegmentType.FILE
    assert message.attachments[0].media_ref.data == _FILE_BYTES


async def test_canonical_only_attachments_are_sent_including_file_base64() -> None:
    """没有 legacy 字段时，物化 canonical 附件也能直接发送。"""
    converter = MessageConverter()
    image_attachment = MediaAttachment(
        MediaSegmentType.IMAGE,
        MediaRef.from_bytes(_PNG_BYTES, kind=MediaKind.IMAGE),
    )
    file_attachment = MediaAttachment(
        MediaSegmentType.FILE,
        MediaRef.from_bytes(_FILE_BYTES, kind=MediaKind.FILE),
        filename="report.bin",
    )
    message = Message(
        message_id="msg-canonical-only",
        content="",
        message_type=MessageType.TEXT,
        sender_id="user-canonical",
        sender_name="Alice",
        platform="qq",
        chat_type="private",
        attachments=[image_attachment, file_attachment],
        target_user_id="user-canonical",
    )

    envelope = await converter.message_to_envelope(message)

    assert _get_segment_types(envelope) == ["image", "file"]
    media_segments = _get_media_segments(envelope)
    assert media_segments[0]["data"] == base64.b64encode(_PNG_BYTES).decode("ascii")
    assert media_segments[1]["data"] == _legacy_base64(_FILE_BYTES)


async def test_descriptor_only_attachment_does_not_leak_and_legacy_falls_back() -> None:
    """descriptor-only 附件不发送数据，但相同 legacy fallback 仍可发送。"""
    converter = MessageConverter()
    materialized = MediaAttachment(
        MediaSegmentType.IMAGE,
        MediaRef.from_bytes(_PNG_BYTES, kind=MediaKind.IMAGE),
    )
    detached = MediaAttachment.from_descriptor(materialized.to_descriptor())
    legacy = materialized.to_legacy()
    message = Message(
        message_id="msg-detached",
        content={"media": [legacy]},
        message_type=MessageType.TEXT,
        sender_id="user-detached",
        sender_name="Alice",
        platform="qq",
        chat_type="private",
        attachments=[detached],
        extra={"media": [legacy], "target_user_id": "user-detached"},
    )

    envelope = await converter.message_to_envelope(message)

    media_segments = _get_media_segments(envelope)
    assert media_segments == [
        {"type": "image", "data": base64.b64encode(_PNG_BYTES).decode("ascii")}
    ]
    assert detached.media_ref.sha256 not in json.dumps(envelope)


async def test_inbound_legacy_dual_write_is_deduplicated_on_outbound() -> None:
    """canonical、content.media 与 extra.media 的同一入站媒体只发送一次。"""
    converter = MessageConverter()
    message = await converter.envelope_to_message(
        _build_private_envelope(
            [{"type": "image", "data": _legacy_base64(_PNG_BYTES)}],
            message_id="msg-roundtrip-media",
        )
    )
    message.extra["target_user_id"] = "user-media"

    envelope = await converter.message_to_envelope(message)

    media_segments = _get_media_segments(envelope)
    assert len(media_segments) == 1
    assert media_segments[0] == {
        "type": "image",
        "data": base64.b64encode(_PNG_BYTES).decode("ascii"),
    }


@pytest.mark.parametrize("segment_type", ["image", "file"])
async def test_path_only_adapter_media_is_rejected_without_file_access(
    segment_type: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """不可信适配器路径只保留为 legacy 元数据，不能读取为附件。"""
    media_path = tmp_path / f"private-{segment_type}.bin"
    media_path.write_bytes(_PNG_BYTES if segment_type == "image" else _FILE_BYTES)
    read_paths: list[Path] = []

    def track_read(path: Path) -> bytes:
        read_paths.append(path)
        return b"unexpected"

    monkeypatch.setattr(Path, "read_bytes", track_read)
    message = await MessageConverter().envelope_to_message(
        _build_private_envelope(
            [
                {
                    "type": segment_type,
                    "data": {
                        "path": str(media_path),
                        "filename": media_path.name,
                    },
                }
            ],
            message_id=f"msg-path-only-{segment_type}",
        )
    )

    assert message.attachments == []
    assert message.extra["media_errors"] == [
        {"index": 0, "type": segment_type, "error": "MediaValidationError"}
    ]
    assert read_paths == []
