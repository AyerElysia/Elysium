"""Contract tests: her game frame reaches her LLM as a native image payload.

The frame must never be reduced to a textual retelling before entering her
natively multimodal model — the heartbeat request carries the raw image.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from PIL import Image as PILImage

from plugins.life_engine.minecraft.capture import Frame
from plugins.life_engine.minecraft.session import MinecraftSession
from plugins.life_engine.service.core import LifeEngineService


def _jpeg_bytes(width: int = 1600, height: int = 900) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), color=(80, 160, 90)).save(
        buffer, format="JPEG", quality=85
    )
    return buffer.getvalue()


class _CaptureStub:
    def __init__(self, frame: Frame | None) -> None:
        self._frame = frame

    async def grab_consciousness_frame(self) -> Frame | None:
        return self._frame


def _bare_session(active: bool, frame: Frame | None) -> MinecraftSession:
    session = object.__new__(MinecraftSession)
    session._state = SimpleNamespace(active=active)
    session._runtime = object() if active else None
    session._capture = _CaptureStub(frame)
    return session


class TestVisionFrameBytes:
    async def test_active_session_returns_jpeg_bytes(self) -> None:
        image = PILImage.open(io.BytesIO(_jpeg_bytes()))
        frame = Frame(image=image, width=image.width, height=image.height)
        session = _bare_session(active=True, frame=frame)

        payload = await session.grab_vision_frame_bytes()

        assert payload is not None
        assert payload.startswith(b"\xff\xd8")  # JPEG magic: raw pixels, not words

    async def test_inactive_session_returns_none(self) -> None:
        session = _bare_session(active=False, frame=None)
        assert await session.grab_vision_frame_bytes() is None

    async def test_missing_window_returns_none_without_raising(self) -> None:
        session = _bare_session(active=True, frame=None)
        assert await session.grab_vision_frame_bytes() is None


class TestHeartbeatVisionPayload:
    async def test_payload_carries_media_part(self) -> None:
        from src.kernel.llm.payload.media import MediaPart

        service = SimpleNamespace(minecraft_session=None)
        image = PILImage.open(io.BytesIO(_jpeg_bytes()))
        frame = Frame(image=image, width=image.width, height=image.height)
        service.minecraft_session = _bare_session(active=True, frame=frame)

        payload = await LifeEngineService._build_minecraft_vision_payload(service)

        assert payload is not None
        parts = list(payload.content)
        assert any(isinstance(part, MediaPart) for part in parts)

    async def test_no_session_means_no_vision(self) -> None:
        service = SimpleNamespace(minecraft_session=None)
        assert await LifeEngineService._build_minecraft_vision_payload(service) is None
