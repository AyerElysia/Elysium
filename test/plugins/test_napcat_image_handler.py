from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from plugins.napcat_adapter.src.handlers.to_core.message_handler import MessageHandler


_IMAGE_HANDLER_PATH = "plugins.napcat_adapter.src.handlers.to_core.message_handler.get_image_base64"
_IMAGE_URL = "https://example.test/image.gif"


def _image_segment(sub_type: object) -> dict:
    return {
        "type": "image",
        "data": {
            "sub_type": sub_type,
            "url": _IMAGE_URL,
        },
    }


@pytest.mark.parametrize(
    ("sub_type", "expected_type"),
    [
        (0, "image"),
        (4, "emoji"),  # KSMART
        (9, "emoji"),  # NapCat-compatible extension value
        (1, "emoji"),
        (7, "emoji"),
        (None, "emoji"),  # marketFace/旧版 image 段可能省略 sub_type
    ],
)
@pytest.mark.asyncio
async def test_image_subtypes_are_converted_to_base64_segments(
    sub_type: object,
    expected_type: str,
) -> None:
    handler = MessageHandler(SimpleNamespace(plugin=None))
    get_image_base64 = AsyncMock(return_value="encoded-image")

    with patch(_IMAGE_HANDLER_PATH, get_image_base64):
        result = await handler._handle_image_message(_image_segment(sub_type))

    assert result == {"type": expected_type, "data": "encoded-image"}
    get_image_base64.assert_awaited_once_with(_IMAGE_URL)
    assert result["data"] != _IMAGE_URL


@pytest.mark.parametrize("sub_type", [99, "not-a-subtype", True])
@pytest.mark.asyncio
async def test_invalid_image_subtypes_use_placeholder_without_download(sub_type: object) -> None:
    handler = MessageHandler(SimpleNamespace(plugin=None))
    get_image_base64 = AsyncMock()

    with patch(_IMAGE_HANDLER_PATH, get_image_base64):
        result = await handler._handle_image_message(_image_segment(sub_type))

    assert result == {"type": "text", "data": "[无法解析的图片]"}
    get_image_base64.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_download_failure_keeps_existing_drop_semantics() -> None:
    handler = MessageHandler(SimpleNamespace(plugin=None))
    get_image_base64 = AsyncMock(side_effect=RuntimeError("download failed"))

    with patch(_IMAGE_HANDLER_PATH, get_image_base64):
        result = await handler._handle_image_message(_image_segment(4))

    assert result is None
    get_image_base64.assert_awaited_once_with(_IMAGE_URL)


@pytest.mark.asyncio
async def test_image_download_timeout_returns_placeholder() -> None:
    handler = MessageHandler(SimpleNamespace(plugin=None))
    get_image_base64 = AsyncMock(side_effect=TimeoutError)

    with patch(_IMAGE_HANDLER_PATH, get_image_base64):
        result = await handler._handle_image_message(_image_segment(9))

    assert result == {"type": "text", "data": "[图片处理超时]"}
    get_image_base64.assert_awaited_once_with(_IMAGE_URL)
