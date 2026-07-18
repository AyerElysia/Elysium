"""模型媒体能力配置归一化测试。"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from src.kernel.llm.exceptions import LLMConfigurationError
from src.kernel.llm.media_capabilities import (
    filter_model_set_for_media,
    normalize_media_capabilities,
)
from src.kernel.llm.payload.media import MediaKind, MediaRef


def test_empty_string_optional_values_are_treated_as_unset() -> None:
    """配置渲染器生成的空字符串不应让模型配置失效。"""
    capabilities = normalize_media_capabilities(
        {
            "modalities": ["text"],
            "accepted_mime_types": {},
            "max_item_bytes": "",
            "max_request_bytes": "  ",
            "max_count": "",
            "max_audio_seconds": "",
            "max_video_seconds": "\t",
            "wire_profile": "",
        }
    )

    assert capabilities == {
        "modalities": ["text"],
        "accepted_mime_types": {},
        "max_item_bytes": None,
        "max_request_bytes": None,
        "max_count": None,
        "max_audio_seconds": None,
        "max_video_seconds": None,
        "wire_profile": None,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_item_bytes", "1024"),
        ("max_request_bytes", "2048"),
        ("max_count", "2"),
        ("max_audio_seconds", "30"),
        ("max_video_seconds", "60"),
        ("wire_profile", 123),
    ],
)
def test_nonempty_invalid_optional_values_still_fail(
    field: str, value: Any
) -> None:
    """仅空字符串是兼容哨兵，其他错误类型仍应快速失败。"""
    with pytest.raises(LLMConfigurationError, match=field):
        normalize_media_capabilities({"modalities": ["text"], field: value})


def test_filter_model_set_keeps_only_explicitly_image_compatible_models() -> None:
    image = MediaRef.from_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        ),
        kind=MediaKind.IMAGE,
    )
    text_only = {"model_identifier": "text-only"}
    image_model = {
        "model_identifier": "image-model",
        "media_capabilities": {
            "modalities": ["text", "image"],
            "accepted_mime_types": {"image": ["image/png"]},
            "max_item_bytes": 1024,
        },
    }
    original_capabilities = dict(image_model["media_capabilities"])

    compatible = filter_model_set_for_media([text_only, image_model], [image])

    assert [model["model_identifier"] for model in compatible] == ["image-model"]
    assert compatible[0] is not image_model
    assert compatible[0]["media_capabilities"]["modalities"] == ["text", "image"]
    assert image_model["media_capabilities"] == original_capabilities
