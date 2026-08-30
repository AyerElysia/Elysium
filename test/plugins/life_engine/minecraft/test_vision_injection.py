"""Vision and cadence boundaries for the dedicated Minecraft consciousness."""

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
    session._state = SimpleNamespace(active=active, body_name="agent")
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


class TestIndependentCoreCadence:
    """Minecraft must not hijack the core heartbeat's thought chain or rhythm."""

    def _service(self, session: object | None, game_interval: int | None) -> object:
        minecraft = None
        if game_interval is not None:
            minecraft = SimpleNamespace(game_turn_interval_seconds=game_interval)
        cfg = SimpleNamespace(
            settings=SimpleNamespace(heartbeat_interval_seconds=30),
            minecraft=minecraft,
        )
        return SimpleNamespace(minecraft_session=session, _cfg=lambda: cfg)

    def test_idle_keeps_base_interval(self) -> None:
        service = self._service(session=None, game_interval=5)
        assert LifeEngineService._effective_heartbeat_interval(service) == 30

    def test_active_session_keeps_base_interval(self) -> None:
        session = _bare_session(active=True, frame=None)
        service = self._service(session=session, game_interval=5)
        assert LifeEngineService._effective_heartbeat_interval(service) == 30

    def test_inactive_session_keeps_base_interval(self) -> None:
        session = _bare_session(active=False, frame=None)
        service = self._service(session=session, game_interval=5)
        assert LifeEngineService._effective_heartbeat_interval(service) == 30

    def test_missing_minecraft_section_keeps_base_interval(self) -> None:
        session = _bare_session(active=True, frame=None)
        service = self._service(session=session, game_interval=None)
        assert LifeEngineService._effective_heartbeat_interval(service) == 30
