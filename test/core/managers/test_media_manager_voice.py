from __future__ import annotations

from collections import OrderedDict
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Lock as ThreadLock
import sys
from unittest.mock import AsyncMock, patch

import pytest


_MEDIA_MANAGER_PATH = Path(__file__).resolve().parents[3] / "src/core/managers/media_manager.py"
_MEDIA_MANAGER_SPEC = spec_from_file_location("_media_manager_under_test", _MEDIA_MANAGER_PATH)
assert _MEDIA_MANAGER_SPEC is not None and _MEDIA_MANAGER_SPEC.loader is not None
_MEDIA_MANAGER_MODULE = module_from_spec(_MEDIA_MANAGER_SPEC)
sys.modules[_MEDIA_MANAGER_SPEC.name] = _MEDIA_MANAGER_MODULE
_MEDIA_MANAGER_SPEC.loader.exec_module(_MEDIA_MANAGER_MODULE)

MediaChainStats = _MEDIA_MANAGER_MODULE.MediaChainStats
MediaManager = _MEDIA_MANAGER_MODULE.MediaManager
MAX_MEDIA_DATA_BYTES = _MEDIA_MANAGER_MODULE._MAX_MEDIA_DATA_BYTES
MAX_VIDEO_DATA_BYTES = _MEDIA_MANAGER_MODULE._MAX_VIDEO_DATA_BYTES


@pytest.mark.asyncio
async def test_recognize_voice_uses_cache() -> None:
    """语音命中缓存时应直接返回，不再触发 ASR。"""
    manager = MediaManager.__new__(MediaManager)
    manager._media_chain_stats = MediaChainStats()
    manager._media_stats_lock = ThreadLock()
    manager._recognition_locks = OrderedDict()
    manager._voice_model_set = [
        {
            "model_identifier": "sensevoice-small",
            "api_key": "key",
            "base_url": "https://example.com",
            "timeout": 30,
        }
    ]
    manager._voice_available = True
    manager._skip_vlm_stream_ids = set()
    manager._get_cached_description = AsyncMock(return_value="缓存转写")
    manager._recognize_with_asr = AsyncMock(return_value="不会被调用")
    manager._save_description_cache = AsyncMock()
    manager.save_media_info = AsyncMock()

    result = await MediaManager.recognize_voice(manager, "base64|QUJD")

    assert result == "缓存转写"
    manager._get_cached_description.assert_awaited_once()
    manager._recognize_with_asr.assert_not_awaited()
    stats = await MediaManager.get_media_chain_stats(manager)
    assert stats["received"] == 1
    assert stats["cache_hits"] == 1
    assert stats["dedup_hits"] == 1
    assert stats["cache_misses"] == 0


@pytest.mark.asyncio
async def test_recognize_voice_runs_asr_and_persists() -> None:
    """语音未命中缓存时优先调用原生音频理解，并保存结果。"""
    manager = MediaManager.__new__(MediaManager)
    manager._media_chain_stats = MediaChainStats()
    manager._media_stats_lock = ThreadLock()
    manager._recognition_locks = OrderedDict()
    manager._voice_model_set = [
        {
            "model_identifier": "sensevoice-small",
            "api_key": "key",
            "base_url": "https://example.com",
            "timeout": 30,
        }
    ]
    manager._voice_available = True
    manager._audio_understanding_model_set = [{"model_identifier": "MiMo-V2.5"}]
    manager._skip_vlm_stream_ids = set()
    manager._get_cached_description = AsyncMock(return_value=None)
    manager._recognize_with_audio_understanding = AsyncMock(return_value="声音理解摘要")
    manager._recognize_with_asr = AsyncMock(return_value="你好世界")
    manager._save_description_cache = AsyncMock()
    manager.save_media_info = AsyncMock()

    result = await MediaManager.recognize_voice(manager, "base64|QUJD", use_cache=True)

    assert result == "声音理解摘要"
    manager._recognize_with_audio_understanding.assert_awaited_once()
    manager._recognize_with_asr.assert_not_awaited()
    manager._save_description_cache.assert_awaited_once()
    manager.save_media_info.assert_awaited_once()
    stats = await MediaManager.get_media_chain_stats(manager)
    assert stats["received"] == 1
    assert stats["cache_misses"] == 1
    assert stats["success"] == 1


@pytest.mark.asyncio
async def test_recognize_media_rejects_oversized_payload() -> None:
    """超大媒体应在进入识别前被拒绝。"""
    manager = MediaManager.__new__(MediaManager)
    manager._media_chain_stats = MediaChainStats()
    manager._media_stats_lock = ThreadLock()
    manager._recognition_locks = OrderedDict()
    manager._voice_model_set = None
    manager._vlm_model_set = None
    manager._video_model_set = None
    manager._vlm_available = False
    manager._voice_available = False
    manager._skip_vlm_stream_ids = set()
    manager._get_cached_description = AsyncMock(return_value=None)
    manager._save_to_pending = AsyncMock()
    manager._recognize_with_vlm = AsyncMock(return_value="不会被调用")

    huge_data = "base64|" + ("a" * (12 * 1024 * 1024))
    result = await MediaManager.recognize_media(manager, huge_data, "image")

    assert result is None
    manager._save_to_pending.assert_not_awaited()
    manager._recognize_with_vlm.assert_not_awaited()
    stats = await MediaManager.get_media_chain_stats(manager)
    assert stats["rejected_too_large"] == 1


@pytest.mark.asyncio
async def test_recognize_video_allows_payload_above_generic_media_limit() -> None:
    """视频摘要应使用视频专用上限，而不是图片/语音的 8MB 通用上限。"""
    manager = MediaManager.__new__(MediaManager)
    manager._media_chain_stats = MediaChainStats()
    manager._media_stats_lock = ThreadLock()
    manager._recognition_locks = OrderedDict()
    manager._video_model_set = None
    manager._vlm_model_set = None
    manager._skip_vlm_stream_ids = set()
    manager._estimate_media_size_bytes = lambda _data: 191_370_820
    manager._get_cached_description = AsyncMock(return_value=None)
    manager._extract_video_keyframes = AsyncMock(return_value=[])
    manager._summarize_video_frames = AsyncMock(return_value="不会被调用")
    manager._save_description_cache = AsyncMock()
    manager.save_media_info = AsyncMock()

    result = await MediaManager.recognize_video(
        manager,
        {"base64": "base64|QUJD", "filename": "clip.mp4"},
        use_cache=False,
    )

    assert result is None
    assert 191_370_820 > MAX_MEDIA_DATA_BYTES
    assert 191_370_820 <= MAX_VIDEO_DATA_BYTES
    manager._extract_video_keyframes.assert_awaited_once()
    manager._summarize_video_frames.assert_not_awaited()
    stats = await MediaManager.get_media_chain_stats(manager)
    assert stats["rejected_too_large"] == 0
    assert stats["failure_types"]["extract_frames_failed"] == 1


@pytest.mark.asyncio
async def test_recognize_video_rejects_payload_above_video_limit() -> None:
    """超过 200MB 的视频仍应在抽帧前被拒绝。"""
    manager = MediaManager.__new__(MediaManager)
    manager._media_chain_stats = MediaChainStats()
    manager._media_stats_lock = ThreadLock()
    manager._recognition_locks = OrderedDict()
    manager._skip_vlm_stream_ids = set()
    manager._estimate_media_size_bytes = lambda _data: MAX_VIDEO_DATA_BYTES + 1
    manager._get_cached_description = AsyncMock(return_value=None)
    manager._extract_video_keyframes = AsyncMock(return_value=["base64|QUJD"])
    manager._summarize_video_frames = AsyncMock(return_value="不会被调用")
    manager._save_description_cache = AsyncMock()
    manager.save_media_info = AsyncMock()

    result = await MediaManager.recognize_video(
        manager,
        {"base64": "base64|QUJD", "filename": "clip.mp4"},
        use_cache=False,
    )

    assert result is None
    manager._extract_video_keyframes.assert_not_awaited()
    stats = await MediaManager.get_media_chain_stats(manager)
    assert stats["rejected_too_large"] == 1
    assert stats["bytes_rejected"] == MAX_VIDEO_DATA_BYTES + 1


@pytest.mark.asyncio
async def test_voice_failure_alert_triggers() -> None:
    """连续语音失败应触发失败告警。"""
    manager = MediaManager.__new__(MediaManager)
    manager._media_chain_stats = MediaChainStats()
    manager._media_stats_lock = ThreadLock()
    manager._recognition_locks = OrderedDict()
    manager._voice_model_set = [
        {
            "model_identifier": "sensevoice-small",
            "api_key": "key",
            "base_url": "https://example.com",
            "timeout": 30,
        }
    ]
    manager._voice_available = True
    manager._audio_understanding_model_set = None
    manager._skip_vlm_stream_ids = set()
    manager._get_cached_description = AsyncMock(return_value=None)
    manager._recognize_with_asr = AsyncMock(return_value=None)
    manager._save_description_cache = AsyncMock()
    manager.save_media_info = AsyncMock()

    with patch("src.core.managers.media_manager.logger.warning") as warn_mock:
        for _ in range(5):
            await MediaManager.recognize_voice(manager, "base64|QUJD", use_cache=True)

    assert warn_mock.call_count >= 1
    stats = await MediaManager.get_media_chain_stats(manager)
    assert stats["failure"] >= 5
    assert stats["failure_types"]["audio_understanding_failed"] >= 5
