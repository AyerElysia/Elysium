"""emoji sender 服务层测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.emoji.config import EmojiConfig
from plugins.emoji.plugin import EmojiPlugin, build_emoji_sender_actor_reminder
from plugins.emoji.sender.action import (
    RecallEmojiAction,
    SendEmojiByIdAction,
    SendEmojiMemeAction,
)
from plugins.emoji.sender.service import EmojiSenderService, MemeCandidate


async def test_new_emoji_install_has_no_components_or_lifecycle_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = EmojiConfig()
    plugin = EmojiPlugin(config)
    missing_config_plugin = EmojiPlugin(config=None)
    task_manager = MagicMock(side_effect=AssertionError("must not schedule"))
    reminder = MagicMock(side_effect=AssertionError("must not inject reminder"))
    vector_service = MagicMock(side_effect=AssertionError("must not open vector DB"))

    monkeypatch.setattr("plugins.emoji.plugin.get_task_manager", task_manager)
    monkeypatch.setattr(
        "plugins.emoji.plugin.sync_emoji_sender_actor_reminder",
        reminder,
    )
    monkeypatch.setattr(
        "plugins.emoji.sender.service.get_vector_db_service",
        vector_service,
    )

    assert config.plugin.enabled is False
    assert plugin.get_components() == []
    assert missing_config_plugin.get_components() == []
    assert build_emoji_sender_actor_reminder(plugin) == ""
    assert build_emoji_sender_actor_reminder(missing_config_plugin) == ""

    await plugin.on_plugin_loaded()
    await plugin.on_plugin_unloaded()
    await missing_config_plugin.on_plugin_loaded()
    await missing_config_plugin.on_plugin_unloaded()

    task_manager.assert_not_called()
    reminder.assert_not_called()
    vector_service.assert_not_called()
    assert plugin._register_task_id is None
    assert plugin._schedule_ids == []
    assert plugin.emoji_service is None


def test_explicitly_enabled_emoji_keeps_sender_components() -> None:
    config = EmojiConfig(plugin={"enabled": True})

    assert EmojiPlugin(config).get_components() == [
        EmojiSenderService,
        SendEmojiMemeAction,
        RecallEmojiAction,
        SendEmojiByIdAction,
    ]


def _make_service(*, temperature: float = 0.12, visual_enabled: bool = False) -> EmojiSenderService:
    """创建一个带最小配置的 EmojiSenderService。

    默认关闭视觉检索（visual_enabled=False），以便确定性地测试文本检索路径。
    """
    config = EmojiConfig()
    config.sender.vector.temperature = temperature
    config.sender.visual.embed_enabled = visual_enabled
    plugin = SimpleNamespace(config=config)
    return EmojiSenderService(plugin=cast(Any, plugin))


def test_select_candidate_returns_best_when_temperature_disabled() -> None:
    """temperature <= 0 时应固定返回距离最近的候选。"""
    service = _make_service(temperature=0.0)
    candidates = [
        MemeCandidate("m2", "开心", "/tmp/2.png", "第二张", 0.18),
        MemeCandidate("m1", "开心", "/tmp/1.png", "第一张", 0.04),
    ]

    selected = service._select_candidate(candidates)

    assert selected is not None
    assert selected.meme_id == "m1"


def test_select_candidate_uses_temperature_weights() -> None:
    """temperature > 0 时应按距离权重调用随机采样。"""
    service = _make_service(temperature=0.2)
    candidates = [
        MemeCandidate("m2", "开心", "/tmp/2.png", "第二张", 0.18),
        MemeCandidate("m1", "开心", "/tmp/1.png", "第一张", 0.04),
        MemeCandidate("m3", "开心", "/tmp/3.png", "第三张", 0.31),
    ]

    with patch("plugins.emoji.sender.service.random.choices", return_value=[candidates[1]]) as choices_mock:
        selected = service._select_candidate(candidates)

    assert selected is candidates[1]
    ordered_candidates = choices_mock.call_args.kwargs["population"] if "population" in choices_mock.call_args.kwargs else choices_mock.call_args.args[0]
    weights = choices_mock.call_args.kwargs["weights"]

    assert [candidate.meme_id for candidate in ordered_candidates] == ["m1", "m2", "m3"]
    assert weights[0] > weights[1] > weights[2]


async def test_search_best_samples_within_threshold() -> None:
    """阈值内存在多个候选时，应交给温度采样函数决定。"""
    service = _make_service(temperature=0.12)
    mock_vdb = MagicMock()
    mock_vdb.get_or_create_collection = AsyncMock()
    mock_vdb.query = AsyncMock(
        return_value={
            "ids": [["m1:开心", "m2:开心", "m3:开心"]],
            "distances": [[0.04, 0.08, 0.42]],
            "metadatas": [[
                {"meme_id": "m1", "tag": "开心", "path": "/tmp/1.png", "description": "第一张"},
                {"meme_id": "m2", "tag": "开心", "path": "/tmp/2.png", "description": "第二张"},
                {"meme_id": "m3", "tag": "开心", "path": "/tmp/3.png", "description": "第三张"},
            ]],
        }
    )

    embedding_request = MagicMock()
    embedding_request.send = AsyncMock(return_value=SimpleNamespace(embeddings=[[0.1, 0.2, 0.3]]))

    chosen = MemeCandidate("m2", "开心", "/tmp/2.png", "第二张", 0.08)

    with (
        patch("plugins.emoji.sender.service.get_model_set_by_task", return_value=object()),
        patch("plugins.emoji.sender.service.create_embedding_request", return_value=embedding_request),
        patch("plugins.emoji.sender.service.get_vector_db_service", return_value=mock_vdb),
        patch.object(service, "_select_candidate", return_value=chosen) as select_mock,
    ):
        result = await service.search_best("开心地笑", ["开心"])

    assert result is not None
    assert result["meme_id"] == "m2"
    assert result["fallback_used"] is False
    sampled_candidates = select_mock.call_args.args[0]
    assert [candidate.meme_id for candidate in sampled_candidates] == ["m1", "m2"]


async def test_search_best_uses_temperature_sampling_for_tagged_fallback() -> None:
    """阈值外但带有效标签时，fallback 也应走温度采样而不是固定第一名。"""
    service = _make_service(temperature=0.2)
    mock_vdb = MagicMock()
    mock_vdb.get_or_create_collection = AsyncMock()
    mock_vdb.query = AsyncMock(
        return_value={
            "ids": [["m1:开心", "m2:开心"]],
            "distances": [[0.44, 0.49]],
            "metadatas": [[
                {"meme_id": "m1", "tag": "开心", "path": "/tmp/1.png", "description": "第一张"},
                {"meme_id": "m2", "tag": "开心", "path": "/tmp/2.png", "description": "第二张"},
            ]],
        }
    )

    embedding_request = MagicMock()
    embedding_request.send = AsyncMock(return_value=SimpleNamespace(embeddings=[[0.1, 0.2, 0.3]]))
    chosen = MemeCandidate("m2", "开心", "/tmp/2.png", "第二张", 0.49)

    with (
        patch("plugins.emoji.sender.service.get_model_set_by_task", return_value=object()),
        patch("plugins.emoji.sender.service.create_embedding_request", return_value=embedding_request),
        patch("plugins.emoji.sender.service.get_vector_db_service", return_value=mock_vdb),
        patch.object(service, "_select_candidate", return_value=chosen) as select_mock,
    ):
        result = await service.search_best("开心地笑", ["开心"])

    assert result is not None
    assert result["meme_id"] == "m2"
    assert result["fallback_used"] is True
    sampled_candidates = select_mock.call_args.args[0]
    assert [candidate.meme_id for candidate in sampled_candidates] == ["m1", "m2"]


async def test_search_best_without_tags_still_requires_threshold_match() -> None:
    """未指定有效标签时，阈值外结果不应触发 fallback。"""
    service = _make_service(temperature=0.2)
    mock_vdb = MagicMock()
    mock_vdb.get_or_create_collection = AsyncMock()
    mock_vdb.query = AsyncMock(
        return_value={
            "ids": [["m1:开心"]],
            "distances": [[0.44]],
            "metadatas": [[
                {"meme_id": "m1", "tag": "开心", "path": "/tmp/1.png", "description": "第一张"},
            ]],
        }
    )

    embedding_request = MagicMock()
    embedding_request.send = AsyncMock(return_value=SimpleNamespace(embeddings=[[0.1, 0.2, 0.3]]))

    with (
        patch("plugins.emoji.sender.service.get_model_set_by_task", return_value=object()),
        patch("plugins.emoji.sender.service.create_embedding_request", return_value=embedding_request),
        patch("plugins.emoji.sender.service.get_vector_db_service", return_value=mock_vdb),
        patch.object(service, "_select_candidate") as select_mock,
    ):
        result = await service.search_best("开心地笑", None)

    assert result is None
    select_mock.assert_not_called()
