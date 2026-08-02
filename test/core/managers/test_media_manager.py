"""MediaManager 的单元测试。

测试覆盖：
- 初始化和 VLM/ASR 配置
- VLM 跳过/恢复功能
- 媒体识别（图片和表情包）
- 语音识别（ASR）
- 批量识别
- 媒体信息保存和查询
- 缓存机制
- 边界条件和异常处理
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.managers.media_manager import MediaManager, get_media_manager


class TestMediaManagerInit:
    """测试 MediaManager 初始化。"""
    
    def test_init_without_vlm(self) -> None:
        """测试无 VLM 配置时的初始化。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task') as mock_get_model:
            mock_get_model.return_value = None
            
            manager = MediaManager()
            
            assert manager._vlm_available is False
            assert manager._vlm_model_set is None
            assert manager._asr_available is False
            assert manager._asr_model_set is None
    
    def test_init_with_vlm(self) -> None:
        """测试有 VLM 配置时的初始化（ASR 未配置）。"""
        def side_effect(task: str):
            if task == "vision":
                return MagicMock()
            return None

        with patch('src.core.managers.media_manager.get_model_set_by_task', side_effect=side_effect):
            manager = MediaManager()
            
            assert manager._vlm_available is True
            assert manager._asr_available is False

    def test_init_with_asr(self) -> None:
        """测试有 ASR 配置时的初始化。"""
        def side_effect(task: str):
            if task == "voice":
                return MagicMock()
            return None

        with patch('src.core.managers.media_manager.get_model_set_by_task', side_effect=side_effect):
            manager = MediaManager()

            assert manager._asr_available is True
            assert manager._asr_model_set is not None
    
    def test_singleton_pattern(self) -> None:
        """验证单例模式实现。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task'):
            manager1 = get_media_manager()
            manager2 = get_media_manager()
            
            assert manager1 is manager2


class TestMediaManagerLegacySkipVLM:
    """旧跳过 API 保持兼容，但不得再保存 stream 级状态。"""

    def test_skip_api_is_stateless_noop(self) -> None:
        with patch('src.core.managers.media_manager.get_model_set_by_task'):
            manager = MediaManager()

            manager.skip_vlm_for_stream("stream_123", ["image"])
            manager.unskip_vlm_for_stream("stream_123", ["image"])

            assert manager.should_skip_vlm("stream_123", "image") is False
            assert not hasattr(manager, "_skip_vlm_stream_ids")
            assert not hasattr(manager, "_skip_vlm_media_types_by_stream")


class TestMediaManagerMimeType:
    """测试图片 MIME 类型提取。"""

    def test_extract_image_mime_type_from_data_url(self) -> None:
        """应保留 data URL 中的真实图片 MIME。"""
        mime_type = MediaManager._extract_image_mime_type(
            "data:image/jpeg;base64,ZmFrZQ=="
        )

        assert mime_type == "image/jpeg"

    def test_extract_image_mime_type_falls_back_to_png(self) -> None:
        """纯 base64 或无法识别时应回退为 PNG。"""
        mime_type = MediaManager._extract_image_mime_type("ZmFrZQ==")

        assert mime_type == "image/png"

    def test_compute_hash_uses_decoded_media_bytes(self) -> None:
        """同一媒体的兼容编码形式必须共享缓存键。"""
        encoded = base64.b64encode(b"same-media-bytes").decode("ascii")
        expected = MediaManager._compute_hash(encoded)

        assert MediaManager._compute_hash(f"base64|{encoded}") == expected
        assert MediaManager._compute_hash(f"base64://{encoded}") == expected
        assert (
            MediaManager._compute_hash(f"data:image/png;base64,{encoded}")
            == expected
        )
        assert MediaManager._compute_hash(f"{encoded[:8]}\n{encoded[8:]}") == expected

    async def test_recognition_lock_lru_keeps_active_lock(self) -> None:
        """容量清理不得让同一媒体在识别中获得第二把锁。"""
        manager = MediaManager.__new__(MediaManager)
        manager._recognition_locks = OrderedDict()

        with patch("src.core.managers.media_manager._MAX_RECOGNITION_LOCKS", 2):
            active_lock = manager._get_recognition_lock("active")
            await active_lock.acquire()
            try:
                manager._get_recognition_lock("second")
                manager._get_recognition_lock("third")

                assert manager._get_recognition_lock("active") is active_lock
                assert "active" in manager._recognition_locks
            finally:
                active_lock.release()

            manager._get_recognition_lock("fourth")
            assert len(manager._recognition_locks) <= 2


class TestMediaManagerRecognizeMedia:
    """测试媒体识别功能。"""
    
    async def test_recognize_media_with_cache(self) -> None:
        """测试使用缓存的媒体识别。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task'):
            manager = MediaManager()
            
            test_data = base64.b64encode(b"test_image_data").decode()
            
            with patch.object(manager, '_get_cached_description', new_callable=AsyncMock) as mock_cache:
                mock_cache.return_value = "Cached description"
                
                result = await manager.recognize_media(
                    base64_data=test_data,
                    media_type="image"
                )
                
                assert result == "Cached description"
    
    async def test_recognize_media_without_cache(self) -> None:
        """测试无缓存时进行 VLM 识别。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task') as mock_get_model:
            mock_model_set = MagicMock()
            mock_get_model.return_value = mock_model_set
            
            manager = MediaManager()
            test_data = base64.b64encode(b"test_image_data").decode()
            
            with patch.object(manager, '_get_cached_description', new_callable=AsyncMock) as mock_cache, \
                 patch.object(manager, '_recognize_with_vlm', new_callable=AsyncMock) as mock_vlm, \
                 patch.object(manager, '_save_description_cache', new_callable=AsyncMock):
                
                mock_cache.return_value = None
                mock_vlm.return_value = "VLM description"
                
                result = await manager.recognize_media(
                    base64_data=test_data,
                    media_type="image"
                )
                
                assert result == "VLM description"
    
    async def test_recognize_media_vlm_not_available(self) -> None:
        """测试 VLM 不可用时的降级处理。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task') as mock_get_model:
            mock_get_model.return_value = None
            
            manager = MediaManager()
            test_data = base64.b64encode(b"test_image_data").decode()
            
            with patch.object(manager, '_get_cached_description', new_callable=AsyncMock) as mock_cache:
                mock_cache.return_value = None
                
                result = await manager.recognize_media(
                    base64_data=test_data,
                    media_type="image"
                )
                
                # VLM 不可用时应返回默认描述或 None
                assert result is None or isinstance(result, str)

    async def test_recognize_with_vlm_uses_default_prompt_when_template_missing(self) -> None:
        class AwaitableResponse:
            message = "一张测试图片"

            def __await__(self):
                async def resolve():
                    return self.message

                return resolve().__await__()

        manager = MediaManager.__new__(MediaManager)
        manager._vlm_model_set = [{"model_identifier": "vision-model"}]
        request = MagicMock()
        request.send = AsyncMock(return_value=AwaitableResponse())
        prompt_manager = MagicMock()
        prompt_manager.get_template.return_value = None
        image_data = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )

        with (
            patch(
                "src.app.plugin_system.api.llm_api.create_llm_request",
                return_value=request,
            ),
            patch(
                "src.core.managers.media_manager.get_prompt_manager",
                return_value=prompt_manager,
            ),
        ):
            result = await manager._recognize_with_vlm(image_data, "image")

        assert result == "一张测试图片"
        payload = request.add_payload.call_args.args[0]
        assert payload.content[0].text.startswith("描述这张图片的内容")
        assert payload.content[1].mime_type == "image/png"


class TestMediaManagerRecognizeBatch:
    """测试批量识别功能。"""
    
    async def test_recognize_batch_empty_list(self) -> None:
        """测试空列表批量识别。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task'):
            manager = MediaManager()
            
            results = await manager.recognize_batch([])
            
            assert results == []
    
    async def test_recognize_batch_multiple_items(self) -> None:
        """测试多个项目批量识别。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task'):
            manager = MediaManager()
            
            items = [
                (base64.b64encode(b"data1").decode(), "image"),
                (base64.b64encode(b"data2").decode(), "emoji"),
            ]
            
            with patch.object(manager, 'recognize_media', new_callable=AsyncMock) as mock_recognize:
                mock_recognize.side_effect = ["Description 1", "Description 2"]
                
                results = await manager.recognize_batch(items)
                
                assert len(results) == 2
                assert results[0] == (0, "Description 1")
                assert results[1] == (1, "Description 2")


class TestMediaManagerSaveAndGetMediaInfo:
    """测试媒体信息保存和查询功能。"""

    def test_normalize_media_db_path_adds_hash_for_generic_video_filename(self) -> None:
        """普通视频文件名入库时应带 hash，避免 images.path 唯一约束冲突。"""
        path = MediaManager._normalize_media_db_path(
            "abcdef1234567890fedcba",
            "video",
            "video.mp4",
        )

        assert path == "video:abcdef1234567890:video.mp4"

    def test_normalize_media_db_path_keeps_real_image_path(self) -> None:
        """真实落盘图片路径保持不变。"""
        path = MediaManager._normalize_media_db_path(
            "abcdef1234567890",
            "image",
            "/tmp/image.jpg",
        )

        assert path == "/tmp/image.jpg"
    
    async def test_save_media_info(self) -> None:
        """测试保存媒体信息。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task'):
            manager = MediaManager()
            
            with patch('src.core.managers.media_manager.get_db_session') as mock_session:
                mock_session_ctx = MagicMock()
                mock_session_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
                mock_session_ctx.__aexit__ = AsyncMock()
                mock_session.return_value = mock_session_ctx
                
                await manager.save_media_info(
                    media_hash="abc123",
                    media_type="image",
                    file_path="/path/to/image.jpg",
                    description="Test image",
                    vlm_processed=True
                )
    
    async def test_get_media_info_exists(self) -> None:
        """测试获取已存在的媒体信息。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task'):
            manager = MediaManager()
            
            with patch('src.core.managers.media_manager.get_db_session') as mock_session:
                mock_media = MagicMock()
                mock_media.id = 1
                mock_media.image_id = "abc123"
                mock_media.path = "/tmp/test.png"
                mock_media.type = "image"
                mock_media.description = "Test image"
                mock_media.count = 2
                mock_media.timestamp = 123.0
                mock_media.vlm_processed = True

                mock_result = MagicMock()
                mock_result.scalars.return_value.first.return_value = mock_media

                mock_session_obj = MagicMock()
                mock_session_obj.execute = AsyncMock(return_value=mock_result)

                mock_session_ctx = MagicMock()
                mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session_obj)
                mock_session_ctx.__aexit__ = AsyncMock()
                mock_session.return_value = mock_session_ctx

                result = await manager.get_media_info("abc123")
                
                assert result is not None
                assert result["image_id"] == "abc123"
                assert result["type"] == "image"
    
    async def test_get_media_info_not_exists(self) -> None:
        """测试获取不存在的媒体信息。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task'):
            manager = MediaManager()
            
            with patch('src.core.managers.media_manager.get_db_session') as mock_session:
                mock_result = MagicMock()
                mock_result.scalars.return_value.first.return_value = None

                mock_session_obj = MagicMock()
                mock_session_obj.execute = AsyncMock(return_value=mock_result)

                mock_session_ctx = MagicMock()
                mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session_obj)
                mock_session_ctx.__aexit__ = AsyncMock()
                mock_session.return_value = mock_session_ctx

                result = await manager.get_media_info("non_existent_hash")
                
                assert result is None


class TestMediaManagerEdgeCases:
    """测试边界条件。"""
    
    async def test_recognize_empty_base64_data(self) -> None:
        """测试空 base64 数据。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task'):
            manager = MediaManager()
            
            result = await manager.recognize_media(
                base64_data="",
                media_type="image"
            )
            
            # 空数据应该返回 None 或错误
            assert result is None or result == ""
    
    async def test_recognize_invalid_media_type(self) -> None:
        """测试无效的媒体类型。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task'):
            manager = MediaManager()
            
            test_data = base64.b64encode(b"test_data").decode()
            
            with patch.object(manager, '_get_cached_description', new_callable=AsyncMock) as mock_cache:
                mock_cache.return_value = None
                
                result = await manager.recognize_media(
                    base64_data=test_data,
                    media_type="invalid_type"
                )
                
                # 应该能够处理无效类型
                assert isinstance(result, (str, type(None)))


class TestMediaManagerRecognizeVoice:
    """测试语音识别（ASR）功能。"""

    async def test_recognize_voice_asr_not_available(self) -> None:
        """测试 ASR 不可用时返回 None。"""
        with patch('src.core.managers.media_manager.get_model_set_by_task') as mock_get_model:
            mock_get_model.return_value = None

            manager = MediaManager()
            audio_b64 = base64.b64encode(b"fake_wav_data").decode()

            result = await manager.recognize_voice(audio_b64)

            assert result is None

    async def test_recognize_voice_success(self) -> None:
        """测试 ASR 识别成功返回文字。"""
        # model_set 是 list[dict]，与 get_model_set_by_task 返回格式一致
        mock_model_set = [{"model_identifier": "sensevoice-small", "api_key": "sk-test", "base_url": "http://localhost"}]

        def side_effect(task: str):
            if task == "voice":
                return mock_model_set
            return None

        with patch('src.core.managers.media_manager.get_model_set_by_task', side_effect=side_effect):
            manager = MediaManager()
            audio_b64 = base64.b64encode(b"fake_wav_data").decode()

            with patch.object(manager, '_recognize_with_asr', new_callable=AsyncMock) as mock_asr:
                mock_asr.return_value = "你好，世界"

                result = await manager.recognize_voice(audio_b64)

                assert result == "语音转写：你好，世界"
                mock_asr.assert_called_once_with(audio_b64, {})

    async def test_recognize_voice_asr_returns_none(self) -> None:
        """测试 ASR 识别返回 None 时行为。"""
        mock_model_set = [{"model_identifier": "sensevoice-small", "api_key": "sk-test"}]

        def side_effect(task: str):
            if task == "voice":
                return mock_model_set
            return None

        with patch('src.core.managers.media_manager.get_model_set_by_task', side_effect=side_effect):
            manager = MediaManager()
            audio_b64 = base64.b64encode(b"silence").decode()

            with patch.object(manager, '_recognize_with_asr', new_callable=AsyncMock) as mock_asr:
                mock_asr.return_value = None

                result = await manager.recognize_voice(audio_b64)

                assert result is None

    async def test_recognize_voice_exception_returns_none(self) -> None:
        """测试 ASR 识别抛出异常时返回 None。"""
        mock_model_set = [{"model_identifier": "sensevoice-small", "api_key": "sk-test"}]

        def side_effect(task: str):
            if task == "voice":
                return mock_model_set
            return None

        with patch('src.core.managers.media_manager.get_model_set_by_task', side_effect=side_effect):
            manager = MediaManager()
            audio_b64 = base64.b64encode(b"bad_data").decode()

            with patch.object(manager, '_recognize_with_asr', new_callable=AsyncMock) as mock_asr:
                mock_asr.side_effect = RuntimeError("ASR 连接失败")

                result = await manager.recognize_voice(audio_b64)

                assert result is None

    async def test_recognize_with_asr_calls_client(self) -> None:
        """测试 _recognize_with_asr 正确调用 ASR client。"""
        mock_model_entry = {"model_identifier": "sensevoice-small", "api_key": "sk-test", "base_url": "http://localhost"}
        mock_model_set = [mock_model_entry]

        def side_effect(task: str):
            if task == "voice":
                return mock_model_set
            return None

        with patch('src.core.managers.media_manager.get_model_set_by_task', side_effect=side_effect):
            manager = MediaManager()
            audio_b64 = base64.b64encode(b"wav_bytes").decode()

            mock_client = AsyncMock()
            mock_client.create_transcription = AsyncMock(return_value="识别文字")

            with patch(
                'src.core.managers.media_manager.get_default_model_client_registry'
            ) as mock_registry_cls:
                mock_registry = MagicMock()
                mock_registry_cls.return_value = mock_registry
                mock_registry.get_asr_client_for_model.return_value = mock_client

                result = await manager._recognize_with_asr(audio_b64)

                assert result == "识别文字"
                mock_registry.get_asr_client_for_model.assert_called_once_with(mock_model_entry)
                mock_client.create_transcription.assert_called_once()

    async def test_recognize_with_asr_no_models(self) -> None:
        """测试 model_set 中无模型时返回 None。"""
        mock_model_set = []  # 空列表

        def side_effect(task: str):
            if task == "voice":
                return mock_model_set
            return None

        with patch('src.core.managers.media_manager.get_model_set_by_task', side_effect=side_effect):
            manager = MediaManager()
            audio_b64 = base64.b64encode(b"wav_bytes").decode()

            result = await manager._recognize_with_asr(audio_b64)

            assert result is None
