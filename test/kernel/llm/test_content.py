"""Tests for the typed media payload facades."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from io import BytesIO
from pathlib import Path

import pytest

from src.kernel.llm.exceptions import MediaLimitError, MediaValidationError
from src.kernel.llm.payload import media as media_module
from src.kernel.llm.payload.content import Audio, Content, File, Image, Text, Video
from src.kernel.llm.payload.media import (
    ABSOLUTE_MAX_ITEM_BYTES,
    DEFAULT_MAX_ITEM_BYTES,
    MediaKind,
    MediaPart,
    MediaRef,
)


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF"
_MP3_BYTES = b"ID3\x04\x00\x00"
_WAV_BYTES = b"RIFF$\x00\x00\x00WAVEfmt "
_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42"
_WEBM_BYTES = b"\x1a\x45\xdf\xa3\x01\x00\x00\x00webm"
_AMR_BYTES = b"#!AMR\n\x00\x01"
_AMR_WB_BYTES = b"#!AMR-WB\n\x00\x01"
_SILK_BYTES = b"#!SILK_V3\x00\x01"
_TENCENT_SILK_BYTES = b"\x02#!SILK_V3\x00\x01"
_SAMPLE_BYTES = b"hello, file content"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class TestContent:
    """Test cases for Content base class."""

    def test_content_is_frozen(self) -> None:
        content = Content()
        with pytest.raises(Exception):
            content.some_attr = "value"

    def test_content_has_slots(self) -> None:
        content = Content()
        with pytest.raises(AttributeError):
            content.__dict__


class TestText:
    """Test cases for Text content."""

    def test_text_creation_and_subclass(self) -> None:
        text = Text("Hello, world!")
        assert text.text == "Hello, world!"
        assert isinstance(text, Content)

    def test_text_is_frozen_and_has_slots(self) -> None:
        text = Text("test")
        with pytest.raises(Exception):
            text.text = "modified"
        with pytest.raises(AttributeError):
            text.__dict__

    def test_text_equality_and_unicode(self) -> None:
        assert Text("hello") == Text("hello")
        assert Text("hello") != Text("world")
        assert Text("Hello 世界! 🌍").text == "Hello 世界! 🌍"


class TestImage:
    """Test validated image construction."""

    def test_image_creation_with_explicit_managed_path(self, tmp_path: Path) -> None:
        image_path = tmp_path / "pic.png"
        image_path.write_bytes(_PNG_BYTES)

        image = Image(image_path)
        managed = Image.from_managed_path(str(image_path))

        assert image == managed
        assert image.mime_type == "image/png"
        assert image.value == _b64(_PNG_BYTES)
        assert image.data_url == f"data:image/png;base64,{_b64(_PNG_BYTES)}"

    def test_plain_string_is_not_a_path(self, tmp_path: Path) -> None:
        image_path = tmp_path / "pic.png"
        image_path.write_bytes(_PNG_BYTES)
        with pytest.raises(MediaValidationError, match="base64"):
            Image(str(image_path))

    def test_image_creation_with_data_url_and_prefix(self) -> None:
        payload = _b64(_PNG_BYTES)
        assert Image(f"data:image/png;base64,{payload}").mime_type == "image/png"
        assert Image(f"base64|{payload}").value == payload
        assert Image(f"base64://{payload}").value == payload

    def test_image_creation_from_bytes_and_stream(self) -> None:
        image = Image.from_bytes(_PNG_BYTES)
        streamed = Image(BytesIO(_PNG_BYTES))
        assert image == streamed
        assert image.kind is MediaKind.IMAGE
        assert image.size_bytes == len(_PNG_BYTES)
        assert len(image.sha256) == 64

    def test_image_rejects_unknown_or_mismatched_signature(self) -> None:
        with pytest.raises(MediaValidationError, match="识别"):
            Image.from_bytes(b"not an image")
        with pytest.raises(MediaValidationError, match="不匹配"):
            Image.from_bytes(_JPEG_BYTES, mime_type="image/png")

    def test_image_is_content_file_and_immutable(self) -> None:
        image = Image.from_bytes(_PNG_BYTES)
        assert isinstance(image, Content)
        assert isinstance(image, File)
        with pytest.raises(AttributeError):
            image.value = "modified"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            image.__dict__

    def test_image_equality_uses_validated_media(self) -> None:
        assert Image.from_bytes(_PNG_BYTES) == Image(_b64(_PNG_BYTES))
        assert Image.from_bytes(_PNG_BYTES) != Image.from_bytes(_JPEG_BYTES)


class TestAudio:
    """Test true audio MIME handling."""

    def test_audio_infers_mp3_mime_from_signature(self) -> None:
        audio = Audio.from_bytes(_MP3_BYTES)
        assert audio.mime_type == "audio/mpeg"
        assert audio.kind is MediaKind.AUDIO

    def test_audio_accepts_wav_alias_and_data_url(self) -> None:
        payload = _b64(_WAV_BYTES)
        audio = Audio(f"data:audio/x-wav;base64,{payload}")
        assert audio.mime_type == "audio/wav"
        assert audio.value == payload
        assert Audio.from_bytes(_WAV_BYTES, mime_type="audio/x-wav").mime_type == "audio/wav"

    def test_audio_supports_explicit_managed_path(self, tmp_path: Path) -> None:
        audio_path = tmp_path / "speech.mp3"
        audio_path.write_bytes(_MP3_BYTES)
        audio = Audio.from_managed_path(audio_path)
        assert audio.mime_type == "audio/mpeg"
        with pytest.raises(MediaValidationError, match="base64"):
            Audio(str(audio_path))

    def test_audio_requires_a_real_signature_and_mime(self) -> None:
        with pytest.raises(MediaValidationError, match="识别"):
            Audio.from_bytes(b"fake_audio_bytes")
        with pytest.raises(MediaValidationError, match="不匹配"):
            Audio.from_bytes(_MP3_BYTES, mime_type="audio/wav")
        with pytest.raises(MediaValidationError, match="不匹配"):
            Audio.from_bytes(_MP3_BYTES, mime_type="audio/silk")

    @pytest.mark.parametrize(
        ("data", "mime_type"),
        [
            (_AMR_BYTES, "audio/amr"),
            (_AMR_WB_BYTES, "audio/amr"),
            (_SILK_BYTES, "audio/silk"),
            (_TENCENT_SILK_BYTES, "audio/silk"),
        ],
    )
    def test_audio_detects_amr_and_silk_magic(
        self, data: bytes, mime_type: str
    ) -> None:
        audio = Audio.from_bytes(data)
        assert audio.mime_type == mime_type
        with pytest.raises(MediaValidationError, match="不匹配"):
            Audio.from_bytes(data, mime_type="audio/mpeg")

    def test_audio_is_file_content_and_immutable(self) -> None:
        audio = Audio.from_bytes(_MP3_BYTES)
        assert isinstance(audio, Content)
        assert isinstance(audio, File)
        assert audio == Audio(_b64(_MP3_BYTES))
        with pytest.raises(AttributeError):
            audio.mime_type = "audio/wav"  # type: ignore[misc]


class TestVideo:
    """Test true video MIME handling."""

    def test_video_infers_mp4_mime_from_signature(self) -> None:
        video = Video.from_bytes(_MP4_BYTES)
        assert video.mime_type == "video/mp4"
        assert isinstance(video, File)
        assert isinstance(video, Content)

    def test_video_accepts_webm_data_url(self) -> None:
        payload = _b64(_WEBM_BYTES)
        video = Video(f"data:video/webm;base64,{payload}")
        assert video.mime_type == "video/webm"
        assert video.value == payload

    def test_video_supports_explicit_managed_path(self, tmp_path: Path) -> None:
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(_MP4_BYTES)
        video = Video(video_path)
        assert video.mime_type == "video/mp4"
        with pytest.raises(MediaValidationError, match="base64"):
            Video(str(video_path))

    def test_video_rejects_unknown_signature(self) -> None:
        with pytest.raises(MediaValidationError, match="识别"):
            Video.from_bytes(b"fake_video_bytes")

    def test_video_is_immutable_and_repr_contains_mime(self) -> None:
        video = Video.from_bytes(_MP4_BYTES)
        with pytest.raises(AttributeError):
            video.value = "modified"  # type: ignore[misc]
        assert "video/mp4" in repr(video)


class TestFile:
    """Test generic File content and strict string handling."""

    def test_file_accepts_bytes_stream_and_empty_stream(self) -> None:
        file_content = File(BytesIO(_SAMPLE_BYTES))
        assert file_content.value == _b64(_SAMPLE_BYTES)
        assert File(BytesIO(b"")).size_bytes == 0
        assert file_content.mime_type == "application/octet-stream"

    def test_file_accepts_base64_and_data_url(self) -> None:
        payload = _b64(_SAMPLE_BYTES)
        assert File(payload).value == payload
        assert File(f"base64|{payload}").value == payload
        assert File(f"data:application/octet-stream;base64,{payload}").value == payload

    def test_file_path_requires_explicit_managed_path(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.bin"
        file_path.write_bytes(_SAMPLE_BYTES)
        assert File(file_path) == File.from_managed_path(str(file_path))
        with pytest.raises(MediaValidationError, match="base64"):
            File(str(file_path))
        with pytest.raises(MediaValidationError, match="不存在"):
            File.from_managed_path("/nonexistent/path/to/file.bin")

    def test_file_rejects_invalid_base64_and_whitespace(self) -> None:
        with pytest.raises(MediaValidationError, match="非法 base64"):
            File("not a path or base64")
        with pytest.raises(MediaValidationError, match="非法 base64"):
            File(f"  {_b64(_SAMPLE_BYTES)}  ")

    def test_file_size_limit_and_types(self) -> None:
        with pytest.raises(MediaLimitError):
            File.from_bytes(_SAMPLE_BYTES, max_item_bytes=1)
        with pytest.raises(TypeError):
            File(12345)  # type: ignore[arg-type]

    def test_file_is_frozen_and_equal_by_media_ref(self) -> None:
        first = File(_b64(_SAMPLE_BYTES))
        second = File(BytesIO(_SAMPLE_BYTES))
        assert first == second
        with pytest.raises(Exception):
            first.value = "new_value"  # type: ignore[misc]


class TestMixedContent:
    """Test compatibility relationships among content types."""

    def test_content_type_discrimination(self) -> None:
        contents = [
            Text("Hello"),
            Image.from_bytes(_PNG_BYTES),
            Audio.from_bytes(_MP3_BYTES),
            Video.from_bytes(_MP4_BYTES),
        ]
        assert all(isinstance(content, Content) for content in contents)
        assert isinstance(contents[1], Image)
        assert isinstance(contents[2], Audio)
        assert isinstance(contents[3], Video)


class TestMediaRef:
    """Test the lower-level immutable media IR."""

    def test_media_ref_metadata_is_consistent(self) -> None:
        ref = MediaRef.from_bytes(_PNG_BYTES, kind=MediaKind.IMAGE)
        assert ref.kind is MediaKind.IMAGE
        assert ref.size_bytes == len(_PNG_BYTES)
        assert ref.sha256
        assert ref.mime_type == "image/png"

    def test_media_ref_rejects_mismatched_metadata(self) -> None:
        with pytest.raises(MediaValidationError, match="size_bytes"):
            MediaRef(
                kind=MediaKind.FILE,
                mime_type="application/octet-stream",
                size_bytes=1,
                sha256="0" * 64,
                data=b"abc",
            )

    def test_descriptor_roundtrip_is_json_safe_and_materializes(
        self, tmp_path: Path
    ) -> None:
        original = MediaRef.from_bytes(
            _PNG_BYTES,
            kind=MediaKind.IMAGE,
            source_message_id="message-1",
            origin="attachment",
            persistence_policy="managed",
            duration=1.25,
            dimensions=(1, 1),
        )

        descriptor = original.to_descriptor()
        assert json.loads(json.dumps(descriptor)) == descriptor
        assert {"data", "base64", "path"}.isdisjoint(descriptor)

        detached = MediaRef.from_descriptor(descriptor)
        assert not detached.is_materialized
        assert detached.data is None
        assert detached.to_descriptor() == descriptor

        from_bytes = detached.materialize(memoryview(_PNG_BYTES))
        assert from_bytes.is_materialized
        assert from_bytes.data == _PNG_BYTES
        assert from_bytes.to_descriptor() == descriptor

        image_path = tmp_path / "detached.png"
        image_path.write_bytes(_PNG_BYTES)
        assert detached.materialize(image_path).data == _PNG_BYTES
        with pytest.raises(TypeError, match="PathLike"):
            detached.materialize(str(image_path))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("kind", "unknown"),
            ("mime_type", "not-a-mime"),
            ("size_bytes", -1),
            ("sha256", "A" * 64),
            ("source_message_id", 1),
            ("origin", ""),
            ("persistence_policy", ""),
            ("duration", -1),
            ("dimensions", [0, 1]),
        ],
    )
    def test_descriptor_rejects_invalid_metadata(
        self, field: str, value: object
    ) -> None:
        descriptor = MediaRef.from_bytes(_PNG_BYTES, kind=MediaKind.IMAGE).to_descriptor()
        descriptor[field] = value
        with pytest.raises(MediaValidationError):
            MediaRef.from_descriptor(descriptor)

    def test_descriptor_validates_semantics_without_magic(self) -> None:
        descriptor = {
            "kind": "image",
            "mime_type": "image/png",
            "size_bytes": 4,
            "sha256": "0" * 64,
            "source_message_id": None,
            "origin": "remote",
            "persistence_policy": "managed",
            "duration": None,
            "dimensions": [1, 1],
        }
        assert not MediaRef.from_descriptor(descriptor).is_materialized

        mismatched = dict(descriptor, mime_type="audio/mpeg")
        with pytest.raises(MediaValidationError, match="kind"):
            MediaRef.from_descriptor(mismatched)

        file_descriptor = dict(descriptor, kind="file", mime_type="image/png")
        assert MediaRef.from_descriptor(file_descriptor).kind is MediaKind.FILE

        with pytest.raises(MediaValidationError, match="不支持的字段"):
            MediaRef.from_descriptor(dict(descriptor, data="forbidden"))

    def test_materialize_verifies_size_hash_and_magic(self) -> None:
        detached = MediaRef.from_descriptor(
            MediaRef.from_bytes(_PNG_BYTES, kind=MediaKind.IMAGE).to_descriptor()
        )
        with pytest.raises(MediaValidationError, match="大小"):
            detached.materialize(b"short")
        with pytest.raises(MediaValidationError, match="sha256"):
            detached.materialize(b"x" * len(_PNG_BYTES))

        fake_image = b"not a png"
        magic_descriptor = {
            "kind": "image",
            "mime_type": "image/png",
            "size_bytes": len(fake_image),
            "sha256": hashlib.sha256(fake_image).hexdigest(),
        }
        with pytest.raises(MediaValidationError, match="识别"):
            MediaRef.from_descriptor(magic_descriptor).materialize(fake_image)

    def test_descriptor_only_ref_cannot_be_wrapped_or_encoded(self) -> None:
        detached = MediaRef.from_descriptor(
            MediaRef.from_bytes(_PNG_BYTES, kind=MediaKind.IMAGE).to_descriptor()
        )
        with pytest.raises(MediaValidationError, match="未物化"):
            MediaPart(detached)
        with pytest.raises(MediaValidationError, match="未物化"):
            Image._from_ref(detached)

        bypassed = object.__new__(MediaPart)
        object.__setattr__(bypassed, "media_ref", detached)
        with pytest.raises(MediaValidationError, match="未物化"):
            _ = bypassed.value
        with pytest.raises(MediaValidationError, match="未物化"):
            _ = bypassed.data_url

    def test_repr_never_exposes_bytes_or_base64(self) -> None:
        secret = b"VERY_SECRET_MEDIA_BYTES"
        ref = MediaRef.from_bytes(secret, kind=MediaKind.FILE)
        file_part = File._from_ref(ref)
        encoded = _b64(secret)

        for rendered in (repr(ref), repr(MediaPart(ref)), repr(file_part)):
            assert "VERY_SECRET_MEDIA_BYTES" not in rendered
            assert encoded not in rendered
        assert "application/octet-stream" in repr(file_part)

    def test_base64_size_preflight_skips_decode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_decode(*args: object, **kwargs: object) -> bytes:
            raise AssertionError("b64decode must not be called")

        monkeypatch.setattr(media_module.base64, "b64decode", fail_decode)
        with pytest.raises(MediaLimitError):
            MediaRef.from_base64("AAAA", max_item_bytes=1)

    def test_managed_path_stats_before_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        media_path = tmp_path / "too-large.bin"
        calls: list[str] = []

        def fake_stat(path: Path) -> object:
            del path
            calls.append("stat")
            return type(
                "FakeStat",
                (),
                {"st_mode": stat.S_IFREG, "st_size": DEFAULT_MAX_ITEM_BYTES + 1},
            )()

        def fail_read(path: Path) -> bytes:
            del path
            calls.append("read")
            raise AssertionError("read_bytes must not be called")

        monkeypatch.setattr(Path, "stat", fake_stat)
        monkeypatch.setattr(Path, "read_bytes", fail_read)
        with pytest.raises(MediaLimitError):
            MediaRef.from_managed_path(media_path)
        assert calls == ["stat"]

    def test_custom_limit_and_absolute_cap_without_large_allocation(self) -> None:
        assert (
            MediaRef.from_bytes(
                _SAMPLE_BYTES,
                max_item_bytes=DEFAULT_MAX_ITEM_BYTES + 1,
            ).data
            == _SAMPLE_BYTES
        )
        with pytest.raises(MediaLimitError, match="绝对上限"):
            MediaRef.from_bytes(
                b"x",
                max_item_bytes=ABSOLUTE_MAX_ITEM_BYTES + 1,
            )

        descriptor = {
            "kind": "file",
            "mime_type": "application/octet-stream",
            "size_bytes": DEFAULT_MAX_ITEM_BYTES + 1,
            "sha256": hashlib.sha256(b"x").hexdigest(),
        }
        assert MediaRef.from_descriptor(descriptor).size_bytes > DEFAULT_MAX_ITEM_BYTES
        with pytest.raises(MediaLimitError):
            MediaRef.from_descriptor(
                dict(descriptor, size_bytes=ABSOLUTE_MAX_ITEM_BYTES + 1)
            )

        class ClaimedSizeBytes(bytes):
            def __new__(cls, value: bytes, size: int) -> "ClaimedSizeBytes":
                instance = super().__new__(cls, value)
                instance.size = size
                return instance

            def __len__(self) -> int:
                return self.size

        over_default = ClaimedSizeBytes(b"x", DEFAULT_MAX_ITEM_BYTES + 1)
        assert MediaRef(
            kind=MediaKind.FILE,
            mime_type="application/octet-stream",
            size_bytes=len(over_default),
            sha256=hashlib.sha256(over_default).hexdigest(),
            data=over_default,
        ).is_materialized

        over_absolute = ClaimedSizeBytes(b"x", ABSOLUTE_MAX_ITEM_BYTES + 1)
        with pytest.raises(MediaLimitError):
            MediaRef(
                kind=MediaKind.FILE,
                mime_type="application/octet-stream",
                size_bytes=len(over_absolute),
                sha256=hashlib.sha256(over_absolute).hexdigest(),
                data=over_absolute,
            )
