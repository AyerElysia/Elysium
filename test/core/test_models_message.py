"""测试 src.core.models.message 模块。"""

import base64
from dataclasses import FrozenInstanceError
from datetime import datetime
import json

import pytest

from src.core.models.media import MediaAttachment, MediaSegmentType
from src.core.models.message import Message, MessageType
from src.kernel.llm.exceptions import MediaValidationError
from src.kernel.llm.payload.media import MediaKind, MediaRef


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_WAV_BYTES = b"RIFF$\x00\x00\x00WAVEfmt "


class TestMessageType:
    """测试 MessageType 枚举。"""

    def test_message_type_values(self):
        """测试 MessageType 枚举值。"""
        assert MessageType.TEXT.value == "text"
        assert MessageType.IMAGE.value == "image"
        assert MessageType.VOICE.value == "voice"
        assert MessageType.VIDEO.value == "video"
        assert MessageType.FILE.value == "file"
        assert MessageType.LOCATION.value == "location"
        assert MessageType.EMOJI.value == "emoji"
        assert MessageType.NOTICE.value == "notice"
        assert MessageType.UNKNOWN.value == "unknown"


class TestMessage:
    """测试 Message 类。"""

    def test_message_minimal_initialization(self):
        """测试最小初始化。"""
        message = Message()
        assert message.message_id == ""
        assert message.content == ""
        assert message.sender_id == ""
        assert message.sender_name == ""
        assert message.platform == ""
        assert message.chat_type == ""
        assert message.stream_id == ""

    def test_message_full_initialization(self):
        """测试完整初始化。"""
        now = datetime.now()
        message = Message(
            message_id="msg_123",
            time=now,
            reply_to="msg_456",
            content="Hello world",
            processed_plain_text="Hello",
            message_type=MessageType.TEXT,
            sender_id="user_001",
            sender_name="Alice",
            sender_cardname="Alice_Card",
            platform="telegram",
            chat_type="private",
            stream_id="stream_789",
            extra={"key": "value", "platform_hint": "old"},
            platform_hint="direct",
        )

        assert message.message_id == "msg_123"
        assert message.reply_to == "msg_456"
        assert message.content == "Hello world"
        assert message.processed_plain_text == "Hello"
        assert message.message_type == MessageType.TEXT
        assert message.sender_id == "user_001"
        assert message.sender_name == "Alice"
        assert message.sender_cardname == "Alice_Card"
        assert message.platform == "telegram"
        assert message.chat_type == "private"
        assert message.stream_id == "stream_789"
        assert message.extra == {"key": "value", "platform_hint": "direct"}

    def test_message_time_conversion(self):
        """测试时间转换为时间戳。"""
        dt = datetime(2024, 1, 1, 12, 0, 0)
        message = Message(time=dt)
        assert isinstance(message.time, float)
        assert message.time == dt.timestamp()

    def test_message_time_none_uses_now(self):
        """测试 time 为 None 时使用当前时间。"""
        message = Message(time=None)
        assert isinstance(message.time, float)
        assert message.time > 0

    def test_message_repr(self):
        """测试 __repr__ 方法。"""
        message = Message(
            message_id="msg_123",
            content="This is a long message that should be truncated in repr",
            sender_name="TestUser",
            message_type=MessageType.TEXT,
        )
        repr_str = repr(message)
        assert "msg_123" in repr_str
        assert "TestUser" in repr_str
        assert "text" in repr_str

    def test_message_to_dict(self):
        """测试 to_dict 方法。"""
        message = Message(
            message_id="msg_123",
            content="Test content",
            sender_id="user_001",
            sender_name="Alice",
            platform="telegram",
        )
        result = message.to_dict()

        assert isinstance(result, dict)
        assert result["message_id"] == "msg_123"
        assert result["content"] == "Test content"
        assert result["sender_id"] == "user_001"
        assert result["sender_name"] == "Alice"
        assert result["platform"] == "telegram"

    def test_message_with_raw_data(self):
        """测试带原始数据的消息。"""
        raw_data = {"original_json": {"key": "value"}}
        message = Message(
            message_id="msg_raw",
            raw_data=raw_data,
        )
        assert message.raw_data == raw_data

    def test_message_different_types(self):
        """测试不同类型的消息。"""
        types_and_content = [
            (MessageType.TEXT, "Text message"),
            (MessageType.IMAGE, {"url": "http://example.com/image.jpg"}),
            (MessageType.VOICE, {"url": "http://example.com/voice.mp3"}),
            (MessageType.FILE, {"filename": "document.pdf", "size": 1024}),
        ]

        for msg_type, content in types_and_content:
            message = Message(message_type=msg_type, content=content)
            assert message.message_type == msg_type
            assert message.content == content

    def test_message_extra_metadata(self):
        """测试额外的元数据。"""
        message = Message(
            message_id="msg_extra",
            reply_count=5,
            forward_count=2,
            edit_count=1,
            custom_field="custom_value",
        )
        # 额外的关键字参数被收集到 extra 字典中
        assert message.extra["reply_count"] == 5
        assert message.extra["forward_count"] == 2
        assert message.extra["edit_count"] == 1
        assert message.extra["custom_field"] == "custom_value"

    def test_message_processed_plain_text_priority(self):
        """测试 processed_plain_text 优先于 content。"""
        message = Message(
            content="<b>Raw HTML</b>",
            processed_plain_text="Raw HTML",
        )
        assert message.content == "<b>Raw HTML</b>"
        assert message.processed_plain_text == "Raw HTML"

    def test_message_runtime_field_validation(self):
        """测试显式运行时字段拒绝错误容器。"""
        with pytest.raises(TypeError, match="extra"):
            Message(extra=[("key", "value")])
        with pytest.raises(TypeError, match="attachments"):
            Message(attachments=())
        with pytest.raises(TypeError, match="MediaAttachment"):
            Message(attachments=[object()])

    def test_message_from_dict_roundtrip_and_raw_data_policy(self):
        """恢复 raw_data，但安全序列化时不再输出它。"""
        attachment = MediaAttachment(
            MediaSegmentType.IMAGE,
            MediaRef.from_bytes(_PNG_BYTES, kind=MediaKind.IMAGE),
            filename="pixel.png",
        )
        source = {
            "message_id": "msg_roundtrip",
            "time": 1710000000.0,
            "content": "with image",
            "message_type": "image",
            "raw_data": {"adapter": "private"},
            "attachments": [attachment.to_descriptor()],
            "extra": {"trace_id": "trace-1"},
        }

        restored = Message.from_dict(source)
        assert restored.raw_data == {"adapter": "private"}
        assert not restored.attachments[0].media_ref.is_materialized

        serialized = restored.to_dict()
        assert "raw_data" not in serialized
        assert serialized["attachments"] == [attachment.to_descriptor()]

        roundtripped = Message.from_dict(serialized)
        assert roundtripped.message_id == restored.message_id
        assert roundtripped.message_type is MessageType.IMAGE
        assert roundtripped.raw_data is None
        assert roundtripped.extra == {"trace_id": "trace-1"}
        assert roundtripped.attachments[0].to_descriptor() == attachment.to_descriptor()

    def test_message_from_dict_merges_unknown_fields(self):
        """未知顶层字段覆盖显式 extra 中的同名项。"""
        message = Message.from_dict(
            {
                "content": "hello",
                "message_type": "not-a-message-type",
                "extra": {"inside": 1, "shared": "explicit"},
                "future_field": 2,
                "shared": "top-level",
            }
        )

        assert message.message_type is MessageType.TEXT
        assert message.extra == {
            "inside": 1,
            "future_field": 2,
            "shared": "top-level",
        }

    def test_message_from_dict_rejects_invalid_explicit_extra(self):
        """显式 extra 必须保持字典形状。"""
        with pytest.raises(TypeError, match="extra"):
            Message.from_dict({"extra": ["invalid"]})

    def test_message_dict_is_json_safe_and_does_not_leak_media(self):
        """附件序列化不包含 data/base64/path/raw bytes。"""
        encoded = base64.b64encode(_PNG_BYTES).decode("ascii")
        message = Message(
            content="image",
            raw_data={"bytes": _PNG_BYTES},
            attachments=[
                MediaAttachment(
                    MediaSegmentType.IMAGE,
                    MediaRef.from_bytes(_PNG_BYTES, kind=MediaKind.IMAGE),
                )
            ],
            extra={"safe": True},
        )

        serialized = message.to_dict()
        dumped = json.dumps(serialized)
        assert encoded not in dumped
        assert "raw_data" not in serialized

        def assert_safe(value):
            if isinstance(value, dict):
                assert not ({"data", "base64", "path", "raw_data"} & value.keys())
                for nested in value.values():
                    assert_safe(nested)
            elif isinstance(value, list):
                for nested in value:
                    assert_safe(nested)
            else:
                assert not isinstance(value, (bytes, bytearray, memoryview))

        assert_safe(serialized)

    def test_message_repr_and_dict_redact_nested_media_sources(self):
        """消息日志与持久化结构只保留媒体元数据和文本。"""
        image_secret = "base64|image-secret"
        path_secret = "/tmp/private-image.png"
        message = Message(
            message_id="media-message",
            message_type=MessageType.IMAGE,
            content={
                "text": "请看这张图",
                "media": [
                    {
                        "type": "image",
                        "data": image_secret,
                        "filename": "visible.png",
                    }
                ],
            },
            extra={
                "preview": {
                    "type": "image",
                    "path": path_secret,
                    "width": 100,
                }
            },
        )

        safe_dict = message.to_dict()
        safe_repr = repr(message)
        dumped = json.dumps(safe_dict, ensure_ascii=False)

        assert image_secret not in dumped
        assert path_secret not in dumped
        assert image_secret not in safe_repr
        assert path_secret not in safe_repr
        assert safe_dict["content"]["text"] == "请看这张图"
        assert safe_dict["content"]["media"][0]["filename"] == "visible.png"
        assert safe_dict["content"]["media"][0]["data"] == "[removed]"
        assert safe_dict["extra"]["preview"] == {
            "type": "image",
            "path": "[removed]",
            "width": 100,
        }


class TestMediaAttachment:
    """测试核心消息附件模型。"""

    @pytest.mark.parametrize(
        ("segment_type", "kind", "payload"),
        [
            (MediaSegmentType.IMAGE, MediaKind.IMAGE, _PNG_BYTES),
            (MediaSegmentType.EMOJI, MediaKind.IMAGE, _PNG_BYTES),
            (MediaSegmentType.VOICE, MediaKind.AUDIO, _WAV_BYTES),
            (MediaSegmentType.VIDEO, MediaKind.VIDEO, b"\x00\x00\x00\x18ftypmp42"),
            (MediaSegmentType.FILE, MediaKind.FILE, b"plain file"),
        ],
    )
    def test_segment_kind_mapping(self, segment_type, kind, payload):
        """每种消息段只接受对应的 MediaKind。"""
        attachment = MediaAttachment(
            segment_type,
            MediaRef.from_bytes(payload, kind=kind),
        )
        assert attachment.segment_type is segment_type
        assert attachment.media_ref.kind is kind

    def test_rejects_kind_mismatch_and_invalid_metadata(self):
        """拒绝 kind 不匹配以及空白或非字符串 metadata。"""
        file_ref = MediaRef.from_bytes(b"file", kind=MediaKind.FILE)
        with pytest.raises(MediaValidationError, match="需要 kind"):
            MediaAttachment(MediaSegmentType.IMAGE, file_ref)
        with pytest.raises(MediaValidationError, match="filename"):
            MediaAttachment(MediaSegmentType.FILE, file_ref, filename="  ")
        with pytest.raises(MediaValidationError, match="resource_id"):
            MediaAttachment(MediaSegmentType.FILE, file_ref, resource_id=123)

    def test_is_frozen_and_slotted(self):
        """附件对象不可变且没有实例字典。"""
        attachment = MediaAttachment(
            MediaSegmentType.FILE,
            MediaRef.from_bytes(b"file", kind=MediaKind.FILE),
        )
        with pytest.raises(FrozenInstanceError):
            attachment.filename = "changed.txt"
        with pytest.raises(AttributeError):
            attachment.__dict__

    def test_descriptor_roundtrip_is_detached_and_json_safe(self):
        """descriptor 往返只保留 JSON-safe 元数据。"""
        attachment = MediaAttachment(
            MediaSegmentType.IMAGE,
            MediaRef.from_bytes(
                _PNG_BYTES,
                kind=MediaKind.IMAGE,
                source_message_id="msg-1",
            ),
            filename="pixel.png",
            resource_id="resource-1",
            storage_key="media/pixel.png",
        )

        descriptor = attachment.to_descriptor()
        json.dumps(descriptor)
        restored = MediaAttachment.from_descriptor(descriptor)

        assert restored.to_descriptor() == descriptor
        assert not restored.media_ref.is_materialized
        assert restored.filename == "pixel.png"
        assert restored.resource_id == "resource-1"
        assert restored.storage_key == "media/pixel.png"

    @pytest.mark.parametrize("dangerous_key", ["data", "base64", "path", "file"])
    def test_descriptor_rejects_dangerous_top_level_fields(self, dangerous_key):
        """attachment descriptor 不接受任何可携带原始 source 的字段。"""
        attachment = MediaAttachment(
            MediaSegmentType.FILE,
            MediaRef.from_bytes(b"file", kind=MediaKind.FILE),
        )
        descriptor = attachment.to_descriptor()
        descriptor[dangerous_key] = "forbidden"
        with pytest.raises(MediaValidationError, match="不支持的字段"):
            MediaAttachment.from_descriptor(descriptor)

    @pytest.mark.parametrize("dangerous_key", ["data", "base64", "path"])
    def test_descriptor_rejects_dangerous_media_ref_fields(self, dangerous_key):
        """MediaRef 的严格 descriptor 校验同样适用于附件。"""
        attachment = MediaAttachment(
            MediaSegmentType.IMAGE,
            MediaRef.from_bytes(_PNG_BYTES, kind=MediaKind.IMAGE),
        )
        descriptor = attachment.to_descriptor()
        descriptor["media_ref"][dangerous_key] = "forbidden"
        with pytest.raises(MediaValidationError, match="不支持的字段"):
            MediaAttachment.from_descriptor(descriptor)

    @pytest.mark.parametrize(
        "legacy_data",
        [
            base64.b64encode(_PNG_BYTES).decode("ascii"),
            "base64|" + base64.b64encode(_PNG_BYTES).decode("ascii"),
            "base64://" + base64.b64encode(_PNG_BYTES).decode("ascii"),
            "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode("ascii"),
            {"data": base64.b64encode(_PNG_BYTES).decode("ascii")},
            {"base64": base64.b64encode(_PNG_BYTES).decode("ascii")},
        ],
    )
    def test_from_legacy_accepts_inline_encodings(self, legacy_data):
        """旧结构支持所有约定的内联编码形状。"""
        attachment = MediaAttachment.from_legacy(
            {"type": "image", "data": legacy_data},
            source_message_id="source-msg",
        )
        assert attachment.media_ref.data == _PNG_BYTES
        assert attachment.media_ref.source_message_id == "source-msg"
        assert attachment.media_ref.mime_type == "image/png"

    def test_from_legacy_accepts_audio_base64_and_metadata(self):
        """字典 voice source 支持 audio_base64，并保留外层 metadata。"""
        attachment = MediaAttachment.from_legacy(
            {
                "type": "voice",
                "mime_type": "audio/wav",
                "filename": "outer.wav",
                "resource_id": "resource-voice",
                "storage_key": "voice/1",
                "data": {
                    "audio_base64": base64.b64encode(_WAV_BYTES).decode("ascii"),
                    "content_type": "audio/mpeg",
                    "filename": "inner.mp3",
                },
            }
        )

        assert attachment.media_ref.data == _WAV_BYTES
        assert attachment.media_ref.mime_type == "audio/wav"
        assert attachment.filename == "outer.wav"
        assert attachment.resource_id == "resource-voice"
        assert attachment.storage_key == "voice/1"

    @pytest.mark.parametrize("path_key", ["path", "file"])
    def test_from_legacy_managed_paths_require_explicit_opt_in(
        self, tmp_path, path_key
    ):
        """Adapter 默认无本地读取权限，内部调用必须显式授权。"""
        media_path = tmp_path / "pixel.png"
        media_path.write_bytes(_PNG_BYTES)
        direct_item = {"type": "image", "data": media_path}
        nested_item = {
            "type": "image",
            "data": {path_key: str(media_path), "name": "pixel.png"},
        }

        with pytest.raises(MediaValidationError, match="allow_managed_paths"):
            MediaAttachment.from_legacy(direct_item)
        with pytest.raises(MediaValidationError, match="allow_managed_paths"):
            MediaAttachment.from_legacy(nested_item)

        direct = MediaAttachment.from_legacy(
            direct_item,
            allow_managed_paths=True,
        )
        nested = MediaAttachment.from_legacy(
            nested_item,
            allow_managed_paths=True,
        )

        assert direct.media_ref.data == _PNG_BYTES
        assert nested.media_ref.data == _PNG_BYTES
        assert nested.filename == "pixel.png"
        assert direct.media_ref.origin == "managed_path"
        assert nested.media_ref.origin == "managed_path"

    def test_from_legacy_never_treats_plain_string_as_path(self, tmp_path):
        """即使普通字符串指向现存文件，也只会按 base64 解析。"""
        media_path = tmp_path / "pixel.png"
        media_path.write_bytes(_PNG_BYTES)
        with pytest.raises(MediaValidationError, match="base64"):
            MediaAttachment.from_legacy(
                {"type": "image", "data": str(media_path)}
            )

    @pytest.mark.parametrize(
        "legacy_data",
        [
            "https://example.invalid/pixel.png",
            {"url": "http://example.invalid/pixel.png"},
        ],
    )
    def test_from_legacy_rejects_remote_urls(self, legacy_data):
        """旧消息 URL 不会触发网络下载。"""
        with pytest.raises(MediaValidationError, match="远程 URL"):
            MediaAttachment.from_legacy(
                {"type": "image", "data": legacy_data}
            )

    @pytest.mark.parametrize(
        "legacy_data",
        [
            {},
            {"filename": "only-a-name.png"},
            {"file": "missing-explicit-path.png", "name": "display.png"},
        ],
    )
    def test_from_legacy_requires_real_source(self, legacy_data):
        """没有 bytes 或有效显式路径时不能伪造 descriptor 哈希。"""
        with pytest.raises(MediaValidationError):
            MediaAttachment.from_legacy(
                {"type": "image", "data": legacy_data}
            )

    def test_legacy_roundtrip_and_descriptor_only_output(self):
        """物化附件输出 base64|，detached 附件永不伪造 data。"""
        attachment = MediaAttachment(
            MediaSegmentType.IMAGE,
            MediaRef.from_bytes(_PNG_BYTES, kind=MediaKind.IMAGE),
            filename="pixel.png",
            resource_id="resource-1",
            storage_key="images/pixel.png",
        )

        legacy = attachment.to_legacy()
        assert legacy == {
            "type": "image",
            "mime_type": "image/png",
            "data": "base64|" + base64.b64encode(_PNG_BYTES).decode("ascii"),
            "filename": "pixel.png",
            "resource_id": "resource-1",
            "storage_key": "images/pixel.png",
        }
        restored = MediaAttachment.from_legacy(legacy)
        assert restored.media_ref.data == _PNG_BYTES
        assert restored.filename == attachment.filename
        assert restored.resource_id == attachment.resource_id
        assert restored.storage_key == attachment.storage_key

        detached = MediaAttachment.from_descriptor(attachment.to_descriptor())
        assert "data" not in detached.to_legacy()
        assert "data" not in attachment.to_legacy(include_data=False)


class TestMessageReplyChain:
    """测试消息回复链。"""

    def test_message_with_reply_to(self):
        """测试带回复的消息。"""
        parent = Message(message_id="parent_123", content="Parent message")
        child = Message(message_id="child_456", content="Child message", reply_to=parent.message_id)

        assert child.reply_to == "parent_123"
        assert parent.message_id == "parent_123"

    def test_message_reply_to_none(self):
        """测试 reply_to 为 None。"""
        message = Message(message_id="msg_no_reply")
        assert message.reply_to is None


class TestMessageUserFields:
    """测试消息用户字段。"""

    def test_sender_cardname(self):
        """测试 sender_cardname 字段。"""
        # 有名片名
        message1 = Message(
            sender_name="RealName",
            sender_cardname="Nickname",
        )
        assert message1.sender_name == "RealName"
        assert message1.sender_cardname == "Nickname"

        # 无名片名
        message2 = Message(
            sender_name="RealName",
            sender_cardname=None,
        )
        assert message2.sender_name == "RealName"
        assert message2.sender_cardname is None

    def test_sender_fields_combinations(self):
        """测试用户字段组合。"""
        test_cases = [
            {"sender_id": "id1", "sender_name": "name1"},
            {"sender_id": "id2", "sender_name": "name2", "sender_cardname": "card2"},
            {"sender_id": "id3", "sender_name": "name3", "sender_cardname": None},
        ]

        for i, fields in enumerate(test_cases, 1):
            message = Message(**fields)
            assert message.sender_id == fields["sender_id"]
            assert message.sender_name == fields["sender_name"]
            if "sender_cardname" in fields:
                assert message.sender_cardname == fields["sender_cardname"]


class TestMessageContextFields:
    """测试消息上下文字段。"""

    def test_chat_context(self):
        """测试聊天上下文字段。"""
        contexts = [
            ("private", "telegram", "stream_private_123"),
            ("group", "discord", "stream_group_456"),
            ("discuss", "qq", "stream_discuss_789"),
        ]

        for chat_type, platform, stream_id in contexts:
            message = Message(
                chat_type=chat_type,
                platform=platform,
                stream_id=stream_id,
            )
            assert message.chat_type == chat_type
            assert message.platform == platform
            assert message.stream_id == stream_id
