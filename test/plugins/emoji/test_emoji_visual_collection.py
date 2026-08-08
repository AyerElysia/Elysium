"""emoji 视觉检索 + 仿生收藏测试（mock 视觉嵌入与存储）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from plugins.emoji.config import EmojiConfig
from plugins.emoji.sender.meme_store import MemeCandidate as StoreCandidate
from plugins.emoji.sender.service import EmojiSenderService


def _make_service(*, temperature: float = 0.0) -> EmojiSenderService:
    config = EmojiConfig()
    config.sender.visual.embed_enabled = True
    config.sender.vector.temperature = temperature
    plugin = SimpleNamespace(config=config)
    return EmojiSenderService(plugin=cast(Any, plugin))


def _mock_store(*, collected: int = 3, search_result: dict | None = None) -> MagicMock:
    store = MagicMock()
    store.initialize = AsyncMock()
    store.count_collected = AsyncMock(return_value=collected)
    store.search_visual = AsyncMock(
        return_value=search_result
        or {
            "ids": [["meme_a", "meme_b"]],
            "distances": [[0.8, 1.0]],  # L2 距离 → cosine 0.68 / 0.5
            "metadatas": [[
                {"path": "/tmp/a.png", "note": "开心"},
                {"path": "/tmp/b.png", "note": "大笑"},
            ]],
        }
    )
    store.get_candidate = AsyncMock(return_value=None)
    store.is_visual_duplicate = AsyncMock(return_value=False)
    store.save_image = MagicMock(return_value="/tmp/stored.png")
    store.store_visual = AsyncMock()
    store.mark_collected = AsyncMock()
    store.mark_dismissed = AsyncMock()
    store.list_unreviewed = AsyncMock(return_value=[])
    store.count_unreviewed = AsyncMock(return_value=0)
    return store


def _mock_embedder() -> MagicMock:
    embedder = MagicMock()
    embedder.embed_text = AsyncMock(return_value=[0.1] * 16)
    embedder.embed_image_bytes = AsyncMock(return_value=[0.2] * 16)
    return embedder


def test_visual_embedding_timeout_covers_cold_model_load() -> None:
    config = EmojiConfig()

    assert config.sender.visual.request_timeout == 60.0


# ── 纯视觉检索 ──────────────────────────────────────────────────


async def test_search_best_visual_returns_match() -> None:
    service = _make_service()
    with patch.object(service, "_meme_store", return_value=_mock_store()), \
         patch.object(service, "_visual_embedder", return_value=_mock_embedder()):
        result = await service.search_best("开心地笑")

    assert result is not None
    assert result["meme_id"] == "meme_a"  # cosine 0.68 最高
    assert result["path"] == "/tmp/a.png"
    assert result["fallback_used"] is False


async def test_search_best_falls_back_when_visual_empty() -> None:
    """视觉库为空时回退到文本检索。"""
    service = _make_service()
    empty_store = _mock_store(collected=0)
    with patch.object(service, "_meme_store", return_value=empty_store), \
         patch.object(service, "_visual_embedder", return_value=_mock_embedder()), \
         patch.object(service, "_search_best_text", new=AsyncMock(return_value={"meme_id": "text_x"})) as text_mock:
        result = await service.search_best("开心地笑")

    assert result == {"meme_id": "text_x"}
    text_mock.assert_awaited_once()


async def test_search_best_visual_disabled_uses_text() -> None:
    config = EmojiConfig()
    config.sender.visual.embed_enabled = False
    service = EmojiSenderService(plugin=cast(Any, SimpleNamespace(config=config)))
    with patch.object(service, "_search_best_text", new=AsyncMock(return_value={"meme_id": "t"})) as text_mock:
        result = await service.search_best("x")
    assert result == {"meme_id": "t"}
    text_mock.assert_awaited_once()


# ── 仿生收藏 ──────────────────────────────────────────────────


async def test_collect_meme_stores_visual() -> None:
    service = _make_service()
    store = _mock_store()
    candidate = StoreCandidate(
        candidate_id="cand1", source_hash="hash1",
        source_path="/tmp/src.png", mime="image/png", brief="开心",
    )
    store.get_candidate = AsyncMock(return_value=candidate)

    with patch.object(service, "_meme_store", return_value=store), \
         patch.object(service, "_visual_embedder", return_value=_mock_embedder()), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("pathlib.Path.read_bytes", return_value=b"img"):
        ok, msg = await service.collect_meme("cand1", note="这张好用")

    assert ok is True
    store.store_visual.assert_awaited_once()
    store.mark_collected.assert_awaited_once()
    kwargs = store.store_visual.call_args.kwargs
    assert kwargs["note"] == "这张好用"
    assert kwargs["meme_id"] == "hash1"


async def test_collect_meme_rejects_visual_duplicate() -> None:
    service = _make_service()
    store = _mock_store()
    candidate = StoreCandidate(
        candidate_id="cand1", source_hash="hash1",
        source_path="/tmp/src.png", mime="image/png",
    )
    store.get_candidate = AsyncMock(return_value=candidate)
    store.is_visual_duplicate = AsyncMock(return_value=True)

    with patch.object(service, "_meme_store", return_value=store), \
         patch.object(service, "_visual_embedder", return_value=_mock_embedder()), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("pathlib.Path.read_bytes", return_value=b"img"):
        ok, msg = await service.collect_meme("cand1")

    assert ok is False
    assert "一样" in msg
    store.store_visual.assert_not_awaited()
    store.mark_dismissed.assert_awaited_once()


async def test_collect_meme_missing_candidate() -> None:
    service = _make_service()
    store = _mock_store()
    store.get_candidate = AsyncMock(return_value=None)
    with patch.object(service, "_meme_store", return_value=store):
        ok, msg = await service.collect_meme("nonexistent")
    assert ok is False


async def test_browse_candidates_returns_list() -> None:
    service = _make_service()
    store = _mock_store()
    store.list_unreviewed = AsyncMock(return_value=[
        StoreCandidate(candidate_id="c1", source_hash="h1", brief="卖萌", source_path="/tmp/1.png"),
        StoreCandidate(candidate_id="c2", source_hash="h2", brief="大笑", source_path="/tmp/2.png"),
    ])
    with patch.object(service, "_meme_store", return_value=store):
        result = await service.browse_candidates(limit=5)
    assert len(result) == 2
    assert result[0]["candidate_id"] == "c1"
    assert result[0]["brief"] == "卖萌"


async def test_get_unreviewed_count() -> None:
    service = _make_service()
    store = _mock_store()
    store.count_unreviewed = AsyncMock(return_value=7)
    with patch.object(service, "_meme_store", return_value=store):
        assert await service.get_unreviewed_count() == 7
